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
import threading
import time
from pathlib import Path, PurePosixPath

from claunote import agent, db, notifications, pdf, pronote, rangement, revision
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
    statut, note = statut_et_note(a, note)
    parts = rangement.dossier_doc({"statut": statut, "niveau": a.niveau, "matiere": a.matiere, "type": a.type})
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
    db.maj(doc_id, nom=nom, statut=statut, titre=a.titre,
           niveau=a.niveau, matiere=a.matiere, type=a.type, chapitre=a.chapitre, date_doc=a.date,
           mots_cles=a.mots_cles, resume=a.resume, transcription=a.transcription,
           confiance=a.confiance, erreur=None, kdrive_md_id=md.get("id"),
           origine="/".join(dossiers) or None, note=note)
    log(f"{statut.replace('_', ' ')} : {item['name']} → {'/'.join(parts)}/{nom}")


def statut_et_note(a, note: str | None) -> tuple[str, str | None]:
    """classe, hors_cours ou a_verifier, avec la raison du doute à afficher."""
    sur = a.confiance >= rangement.SEUIL_CONFIANCE
    if a.hors_cours:
        return ("hors_cours", None) if sur else (
            "a_verifier", note or "L'agent pense que ce document est hors cours (administratif…), sans en être sûr.")
    if rangement.est_classe(a):
        return "classe", None
    return "a_verifier", note or f"L'agent n'est pas sûr du classement (confiance {a.confiance:.0%})."


def reclasser(doc: dict, matieres: list[str]):
    """Reclassement demandé depuis la page Organiser : relit la transcription, pas le scan."""
    try:
        a = agent.reclasser(doc, matieres)
    except ErreurAgent as e:
        db.maj(doc["id"], statut="a_verifier", note=f"Reclassement impossible : {e}")
        db.marquer_a_ranger([doc["id"]])
        log(f"reclassement impossible : {doc['nom']} : {e}")
        return
    statut, note = statut_et_note(a, None)
    db.maj(doc["id"], statut=statut, niveau=a.niveau, matiere=a.matiere, type=a.type,
           chapitre=a.chapitre or doc["chapitre"], titre=a.titre or doc["titre"], confiance=a.confiance, note=note)
    db.marquer_a_ranger([doc["id"]])
    log(f"reclassé : {doc['nom']} → {statut} {a.niveau} / {a.matiere} / {a.type}")


def ranger_en_attente(kd: KDrive):
    """Déplace dans kDrive les documents modifiés sur le site (actions de masse, reclassements)."""
    dossiers: dict[tuple, int] = {}
    for doc in db.a_ranger():
        parts = tuple(rangement.dossier_doc(doc))
        try:
            if parts not in dossiers:
                dossiers[parts] = kd.ensure_path(ETUDES_ID, list(parts))
            kd.move(doc["kdrive_id"], dossiers[parts])
            if doc["kdrive_md_id"]:
                kd.move(doc["kdrive_md_id"], dossiers[parts])
        except Exception as e:
            log(f"déplacement kDrive impossible : {doc['nom']} : {e}")
        db.marquer_a_ranger([doc["id"]], False)


def traiter_revision(kd: KDrive, rev: dict):
    """Génère une fiche, un quiz, des flashcards, une épreuve ou un plan, puis en dépose le PDF dans Etudes/fiches/."""
    try:
        contenu, reperes, web = revision.generer(rev["genre"], rev["sujet"], db.documents(rev["doc_ids"]),
                                                 rev["date_eval"], rev.get("consigne") or "")
    except ErreurAgent as e:
        db.maj_revision(rev["id"], "erreur", erreur=str(e))
        log(f"erreur de révision {rev['id']} : {e}")
        return
    if rev["genre"] == "cartes":
        db.creer_cartes(rev["id"], json.loads(contenu))
    db.maj_revision(rev["id"], "pret", contenu=contenu, reperes=reperes, web=web)
    kdrive_id = copier_pdf(kd, db.revision(rev["id"]))
    if kdrive_id:
        db.maj_revision(rev["id"], "pret", contenu=contenu, reperes=reperes, kdrive_id=kdrive_id, web=web)
    log(f"révision prête : {rev['genre']} « {rev['sujet']} »" + (f" ({len(web)} source(s) web)" if web else ""))
    try:
        if not rev.get("campagne_id"):  # une campagne prévient une seule fois, quand tout est prêt
            notifications.revision_prete(rev)
        else:
            notifications.campagne_prete(rev["campagne_id"])
    except Exception as e:
        log(f"notification impossible : {e}")


def copier_pdf(kd: KDrive, rev: dict) -> int | None:
    """PDF de la révision dans Etudes/fiches/<campagne ou matière>/, pour l'ouvrir ou le partager depuis kDrive."""
    try:
        sources = {d["id"]: d["titre"] or d["nom"] for d in db.documents(list((rev["reperes"] or {}).values()))}
        dossier = kd.ensure_path(ETUDES_ID, ["fiches", rev.get("campagne_titre") or rev.get("matiere") or "divers"])
        nom = revision.nom_fichier(rev["genre"], rev["sujet"], dt.date.today(), "pdf")
        return kd.upload(dossier, nom, pdf.revision_pdf(rev, sources)).get("id")
    except Exception as e:
        log(f"copie kDrive de la révision impossible : {e}")
        return None


def traiter_campagne(campagne: dict):
    """Programme jour par jour d'une campagne, à partir des matières et des supports demandés."""
    revs = db.revisions_campagne(campagne["id"])
    matieres = {}
    for r in revs:
        m = matieres.setdefault((r["niveau"], r["matiere"]), {
            "niveau": r["niveau"] or "?", "matiere": r["matiere"] or "?", "sujet": "", "supports": [], "cours": []})
        m["supports"].append(r["genre"])
        m.setdefault("ids", set()).update(r["doc_ids"])
        sujet = r["sujet"].split(" : ", 1)[-1]
        if sujet != r["matiere"]:
            m["sujet"] = sujet
    for m in matieres.values():
        m["cours"] = [d["titre"] or d["nom"] for d in db.documents(sorted(m.pop("ids")))]
    try:
        programme = revision.generer_programme(campagne, list(matieres.values()),
                                               db.evenements_entre(campagne["debut"], campagne["fin"]))
    except ErreurAgent as e:
        db.maj_campagne(campagne["id"], "erreur", erreur=str(e))
        log(f"erreur de programme pour la campagne {campagne['id']} : {e}")
        return
    db.maj_campagne(campagne["id"], "pret", programme)
    log(f"programme prêt : « {campagne['titre']} »")
    try:
        notifications.campagne_prete(campagne["id"])
    except Exception as e:
        log(f"notification impossible : {e}")


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
            notifications.apres_synchro_pronote(b["deposes"] if b else 0)
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
    threading.Thread(target=notifications.boucle, daemon=True, name="notifications").start()
    prochaine_synchro = 0.0
    while True:
        pause = INTERVALLE
        prochaine_synchro = synchro_pronote(prochaine_synchro)
        try:
            # Les révisions passent avant les scans : tu attends le résultat sur le site.
            # Le programme d'une campagne d'abord : il est court et tu vois tout de suite ta semaine.
            for campagne in db.campagnes_en_attente():
                log(f"campagne : « {campagne['titre']} »")
                traiter_campagne(campagne)
            for rev in db.revisions_en_attente():
                log(f"révision : {rev['genre']} « {rev['sujet']} »")
                traiter_revision(kd, rev)
            a_reclasser = db.a_reclasser(LOT * 4)  # peu coûteux : seulement du texte
            matieres = db.matieres() if a_reclasser else []
            for doc in a_reclasser:
                reclasser(doc, matieres)
            ranger_en_attente(kd)
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
