"""Surveille l'inbox kDrive, analyse chaque nouveau scan et le range.

Pour chaque fichier de l'inbox pas encore traité :
téléchargement → analyse Claude Code → base de données → déplacement dans
archives/<niveau>/<matière>/<type> (ou a-verifier/) + transcription .md à côté.
"""
import os
import shutil
import time
from pathlib import Path, PurePosixPath

import datetime as dt
import json

from claunote import db, rangement, revision
from claunote.agent import ErreurAgent, LimiteAtteinte, analyser
from claunote.kdrive import KDrive

INBOX_ID = int(os.environ["KDRIVE_INBOX_ID"])
ETUDES_ID = int(os.environ["KDRIVE_ETUDES_ID"])
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/cache"))
INTERVALLE = 60          # secondes entre deux vérifications de l'inbox
PAUSE_LIMITE = 30 * 60   # pause quand la limite du forfait Pro est atteinte
LOT = int(os.environ.get("WORKER_LOT", "5"))  # documents analysés par passage


def log(msg: str):
    print(msg, flush=True)


def traiter(kd: KDrive, item: dict):
    doc_id = db.creer(item["id"], item["name"])
    dossier_local = CACHE_DIR / str(item["id"])
    chemin = kd.download(item["id"], dossier_local / item["name"])
    try:
        a = analyser(chemin)
    except ErreurAgent as e:
        db.maj(doc_id, statut="erreur", erreur=str(e))
        log(f"erreur d'analyse : {item['name']} : {e}")
        return
    finally:
        shutil.rmtree(dossier_local, ignore_errors=True)

    classe = rangement.est_classe(a)
    parts = rangement.dossier(a.niveau, a.matiere, a.type) if classe else rangement.A_VERIFIER
    dest_id = kd.ensure_path(ETUDES_ID, parts)
    nom = rangement.nouveau_nom(a, item["name"])
    kd.move(item["id"], dest_id)  # en cas d'échec ici, le scan reste dans l'inbox et sera repris
    # Une fois sorti de l'inbox, un échec secondaire ne doit pas perdre l'analyse
    if nom != item["name"]:
        try:
            kd.rename(item["id"], nom)
        except Exception as e:
            log(f"renommage impossible ({e}), nom conservé : {item['name']}")
            nom = item["name"]
    md = {}
    try:
        md = kd.upload(dest_id, f"{PurePosixPath(nom).stem}.md", a.transcription.encode())
    except Exception as e:
        log(f"envoi de la transcription impossible : {e}")
    db.maj(doc_id, nom=nom, statut="classe" if classe else "a_verifier", titre=a.titre,
           niveau=a.niveau, matiere=a.matiere, type=a.type, chapitre=a.chapitre, date_doc=a.date,
           mots_cles=a.mots_cles, resume=a.resume, transcription=a.transcription,
           confiance=a.confiance, erreur=None, kdrive_md_id=md.get("id"))
    log(f"{'classé' if classe else 'à vérifier'} : {item['name']} → {'/'.join(parts)}/{nom}")


def traiter_revision(kd: KDrive, rev: dict):
    """Génère une fiche, un quiz ou un plan, puis en dépose une copie dans Etudes/fiches/."""
    try:
        contenu, reperes = revision.generer(rev["genre"], rev["sujet"], db.documents(rev["doc_ids"]),
                                            rev["date_eval"])
    except ErreurAgent as e:
        db.maj_revision(rev["id"], "erreur", erreur=str(e))
        log(f"erreur de révision {rev['id']} : {e}")
        return
    texte = revision.quiz_en_markdown(json.loads(contenu)) if rev["genre"] == "quiz" else contenu
    kdrive_id = None
    try:
        dossier = kd.ensure_path(ETUDES_ID, ["fiches"])
        nom = revision.nom_fichier(rev["genre"], rev["sujet"], dt.date.today())
        kdrive_id = kd.upload(dossier, nom, f"# {rev['sujet']}\n\n{texte}".encode()).get("id")
    except Exception as e:
        log(f"copie kDrive de la révision impossible : {e}")
    db.maj_revision(rev["id"], "pret", contenu=contenu, reperes=reperes, kdrive_id=kdrive_id)
    log(f"révision prête : {rev['genre']} « {rev['sujet']} »")


def a_traiter(kd: KDrive) -> list[dict]:
    """Fichiers de l'inbox jamais vus, ou restés 'nouveau' (ex. limite atteinte)."""
    items = []
    for item in kd.list_dir(INBOX_ID):
        if item["type"] != "file":
            continue
        doc = db.par_kdrive(item["id"])
        if doc is None or doc["statut"] == "nouveau":
            items.append(item)
    return items


def attendre(secondes: int):
    """Dort, mais se réveille tout de suite si le site demande un scan."""
    fin = time.monotonic() + secondes
    while time.monotonic() < fin:
        if db.scan_demande():
            log("scan demandé depuis le site")
            return
        time.sleep(5)


def main():
    db.init()
    kd = KDrive.from_env()
    log(f"worker : surveillance du dossier kDrive {INBOX_ID}")
    while True:
        pause = INTERVALLE
        try:
            # Les révisions passent avant les scans : tu attends le résultat sur le site
            for rev in db.revisions_en_attente():
                log(f"révision : {rev['genre']} « {rev['sujet']} »")
                traiter_revision(kd, rev)
            for item in a_traiter(kd)[:LOT]:
                log(f"analyse : {item['name']}")
                traiter(kd, item)
        except LimiteAtteinte as e:
            log(f"limite du forfait atteinte, pause {PAUSE_LIMITE // 60} min : {e}")
            pause = PAUSE_LIMITE
        except Exception as e:
            log(f"erreur : {e}")
        attendre(pause)


if __name__ == "__main__":
    main()
