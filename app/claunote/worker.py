"""Surveille l'inbox kDrive, analyse chaque nouveau scan et le range.

Pour chaque fichier de l'inbox (sous-dossiers compris) pas encore traité :
téléchargement → analyse Claude Code → base de données → déplacement dans
archives/<niveau>/<matière>/<type> (ou a-verifier/) + transcription .md à côté.
Un sous-dossier nommé avec l'année d'étude (« BTS1 », « Terminale »…) fixe le niveau.
"""
import datetime as dt
import json
import os
import shutil
import time
from pathlib import Path, PurePosixPath

from claunote import db, pronote, rangement, revision
from claunote.agent import ErreurAgent, LimiteAtteinte, analyser
from claunote.kdrive import KDrive

INBOX_ID = int(os.environ["KDRIVE_INBOX_ID"])
ETUDES_ID = int(os.environ["KDRIVE_ETUDES_ID"])
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/cache"))
INTERVALLE = 60          # secondes entre deux vérifications de l'inbox
PAUSE_LIMITE = 30 * 60   # pause quand la limite du forfait Pro est atteinte
LOT = int(os.environ.get("WORKER_LOT", "5"))  # documents analysés par passage
SYNCHRO_PRONOTE = int(os.environ.get("PRONOTE_INTERVALLE_H", "3")) * 3600


def log(msg: str):
    print(msg, flush=True)


def appliquer_dossier(a, dossiers: list[str]) -> str | None:
    """Le niveau du dossier l'emporte ; un désaccord avec le contenu part en vérification.

    Renvoie la note à afficher sur la page « À vérifier », ou None.
    """
    niveau = rangement.niveau_depuis_dossier(dossiers)
    if niveau is None:
        return None
    if a.niveau and a.niveau != niveau:
        note = (f"Le dossier « {'/'.join(dossiers)} » indique {niveau}, "
                f"mais le contenu ressemble à du {a.niveau}.")
        a.niveau = niveau
        a.confiance = min(a.confiance, rangement.SEUIL_CONFIANCE - 0.01)
        return note
    a.niveau = niveau
    return None


def traiter(kd: KDrive, item: dict):
    dossiers = item.get("dossiers", [])
    doc_id = db.creer(item["id"], item["name"])
    dossier_local = CACHE_DIR / str(item["id"])
    chemin = kd.download(item["id"], dossier_local / item["name"])
    try:
        a = analyser(chemin, dossiers)
    except ErreurAgent as e:
        db.maj(doc_id, statut="erreur", erreur=str(e))
        log(f"erreur d'analyse : {item['name']} : {e}")
        return
    finally:
        shutil.rmtree(dossier_local, ignore_errors=True)

    note = appliquer_dossier(a, dossiers)
    if note is None and not rangement.est_classe(a):
        note = f"L'agent n'est pas sûr du classement (confiance {a.confiance:.0%})."
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
           confiance=a.confiance, erreur=None, kdrive_md_id=md.get("id"),
           origine="/".join(dossiers) or None, note=None if classe else note)
    log(f"{'classé' if classe else 'à vérifier'} : {item['name']} → {'/'.join(parts)}/{nom}")


def traiter_revision(kd: KDrive, rev: dict):
    """Génère une fiche, un quiz, des flashcards ou un plan, puis en dépose une copie dans Etudes/fiches/."""
    try:
        contenu, reperes = revision.generer(rev["genre"], rev["sujet"], db.documents(rev["doc_ids"]),
                                            rev["date_eval"])
    except ErreurAgent as e:
        db.maj_revision(rev["id"], "erreur", erreur=str(e))
        log(f"erreur de révision {rev['id']} : {e}")
        return
    texte = contenu
    if rev["genre"] == "quiz":
        texte = revision.quiz_en_markdown(json.loads(contenu))
    elif rev["genre"] == "cartes":
        db.creer_cartes(rev["id"], json.loads(contenu))
        texte = revision.cartes_en_markdown(json.loads(contenu))
    kdrive_id = None
    try:
        dossier = kd.ensure_path(ETUDES_ID, ["fiches"])
        nom = revision.nom_fichier(rev["genre"], rev["sujet"], dt.date.today())
        kdrive_id = kd.upload(dossier, nom, f"# {rev['sujet']}\n\n{texte}".encode()).get("id")
    except Exception as e:
        log(f"copie kDrive de la révision impossible : {e}")
    db.maj_revision(rev["id"], "pret", contenu=contenu, reperes=reperes, kdrive_id=kdrive_id)
    log(f"révision prête : {rev['genre']} « {rev['sujet']} »")


def parcourir(kd: KDrive, dossier_id: int, dossiers: list[str], profondeur: int = 0):
    """Fichiers d'un dossier et de ses sous-dossiers, avec le chemin des dossiers traversés."""
    for item in kd.list_dir(dossier_id):
        if item["type"] == "dir" and profondeur < 4:
            yield from parcourir(kd, item["id"], dossiers + [item["name"]], profondeur + 1)
        elif item["type"] == "file":
            yield {**item, "dossiers": dossiers}


def a_traiter(kd: KDrive) -> list[dict]:
    """Fichiers de l'inbox jamais vus, ou restés 'nouveau' (ex. limite atteinte)."""
    items = []
    for item in parcourir(kd, INBOX_ID, []):
        doc = db.par_kdrive(item["id"])
        if doc is None or doc["statut"] == "nouveau":
            items.append(item)
    return items


DELAI_NETTOYAGE = 10 * 60  # un dossier modifié récemment est peut-être encore en cours d'envoi


def nettoyer_inbox(kd: KDrive, dossier_id: int = INBOX_ID, racine: bool = True) -> bool:
    """Met à la corbeille les sous-dossiers de l'inbox vidés par le rangement. Vrai si vide."""
    vide = True
    for item in kd.list_dir(dossier_id):
        ancien = time.time() - item.get("last_modified_at", 0) > DELAI_NETTOYAGE
        if item["type"] == "dir" and nettoyer_inbox(kd, item["id"], racine=False) and ancien:
            kd.delete(item["id"])
            log(f"dossier vide supprimé de l'inbox : {item['name']}")
        else:
            vide = False
    return vide and not racine


def attendre(secondes: int):
    """Dort, mais se réveille tout de suite si le site demande un scan."""
    fin = time.monotonic() + secondes
    while time.monotonic() < fin:
        if db.scan_demande():
            log("scan demandé depuis le site")
            return
        time.sleep(5)


def synchro_pronote(prochaine: float) -> float:
    """Synchronise Pronote si c'est l'heure ; renvoie l'heure (monotonic) de la suivante."""
    if time.monotonic() < prochaine and not db.etat_consommer("pronote_demande"):
        return prochaine
    try:
        r = pronote.synchroniser()
        if r["connecte"]:
            log(f"pronote : {r['evenements']} évaluation(s)/devoir(s), {len(r['preparees'])} révision(s) lancée(s)"
                + (f", erreurs : {r['erreurs']}" if r["erreurs"] else ""))
            b = pronote.importer_documents(KDrive.from_env(), INBOX_ID)
            if b:
                log(f"pronote : {b['deposes']} document(s) des profs déposé(s) dans l'inbox, {b['liens']} lien(s)"
                    + (f", {b['restant']} au prochain passage" if b["restant"] else "")
                    + (f", erreurs : {b['erreurs']}" if b["erreurs"] else ""))
    except Exception as e:
        log(f"pronote : synchro impossible : {e}")
    return time.monotonic() + SYNCHRO_PRONOTE


def main():
    db.init()
    kd = KDrive.from_env()
    log(f"worker : surveillance du dossier kDrive {INBOX_ID}")
    prochaine_synchro = 0.0
    while True:
        pause = INTERVALLE
        prochaine_synchro = synchro_pronote(prochaine_synchro)
        try:
            # Les révisions passent avant les scans : tu attends le résultat sur le site
            for rev in db.revisions_en_attente():
                log(f"révision : {rev['genre']} « {rev['sujet']} »")
                traiter_revision(kd, rev)
            items = a_traiter(kd)
            for item in items[:LOT]:
                log(f"analyse : {'/'.join(item['dossiers'] + [item['name']])}")
                traiter(kd, item)
            nettoyer_inbox(kd)
        except LimiteAtteinte as e:
            log(f"limite du forfait atteinte, pause {PAUSE_LIMITE // 60} min : {e}")
            pause = PAUSE_LIMITE
        except Exception as e:
            log(f"erreur : {e}")
        attendre(pause)


if __name__ == "__main__":
    main()
