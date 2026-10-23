"""Surveille l'inbox kDrive, trie puis analyse chaque nouveau fichier et le range.

Un dossier copié en entier dans l'inbox forme un « lot ». Une fois le lot complet (plus de
nouveau fichier pendant 90 s), il est trié sans IA (importation.py) : fichiers inutiles proposés
à la suppression sur le site, doublons PDF/Word, pages de scans à fusionner ; puis une seule
passe IA lit les noms de dossiers pour comprendre l'ancienne organisation.
Chaque document gardé : téléchargement → analyse Claude Code → base de données → déplacement
dans archives/<niveau>/<matière>/<type> (ou a-verifier/) + transcription .md à côté.
Un sous-dossier nommé avec l'année d'étude (« BTS1 », « Terminale »…) fixe le niveau.

Fiabilité (voir la page État du site) :
- Claude et kDrive ont chacun un disjoncteur (Garde) : après une panne ou la limite du forfait,
  les étapes qui en ont besoin attendent (jusqu'à l'heure de reprise annoncée par Claude Code),
  les autres continuent (Pronote, tri, rangement, notifications).
- Chaque travail est reprenable : l'analyse est enregistrée avant le déplacement dans kDrive,
  une révision reste « en attente » tant qu'elle n'est pas finie, un PDF non copié est recopié.
- À l'arrêt (mise à jour, redémarrage), le document en cours est terminé avant de quitter.
"""
import datetime as dt
import json
import os
import shutil
import signal
import threading
import time
from pathlib import Path, PurePosixPath

import httpx

from claunote import agent, db, importation, modeles, notifications, pdf, pronote, rangement, revision
from claunote.agent import ClaudeDeconnecte, ClaudeIndisponible, ErreurAgent, LimiteAtteinte, analyser
from claunote.kdrive import KDrive, KDriveIndisponible

INBOX_ID = int(os.environ["KDRIVE_INBOX_ID"])
ETUDES_ID = int(os.environ["KDRIVE_ETUDES_ID"])
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/cache"))
INTERVALLE = 60          # secondes entre deux vérifications de l'inbox
LOT = int(os.environ.get("WORKER_LOT", "5"))  # documents analysés par passage
SYNCHRO_PRONOTE = int(os.environ.get("PRONOTE_INTERVALLE_H", "3")) * 3600
REESSAI_PRONOTE = 15 * 60  # synchro Pronote ratée : nouvel essai dans 15 min, pas dans 3 h
NETTOYAGE = 10 * 60        # dossiers vides de l'inbox : vérifiés toutes les 10 min
ESSAIS_MAX = 3             # Claude plante 3 fois sur le même document : il passe en erreur
ARRET = threading.Event()  # SIGTERM reçu : finir le travail en cours puis quitter


def log(msg: str):
    print(msg, flush=True)


def _iso(t: float | None) -> str | None:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="seconds") if t else None


class Garde:
    """Disjoncteur d'un service externe : après un échec, ses étapes attendent avant de réessayer.

    Pause croissante (1, 2, 4… min) pour une panne, jusqu'à l'heure annoncée pour une limite.
    """

    def __init__(self, nom: str, pause_max: int = 30 * 60):
        self.nom, self.pause_max = nom, pause_max
        self.etat, self.message, self.jusqu_a, self.echecs, self.depuis = "ok", "", 0.0, 0, None

    def disponible(self) -> bool:
        return time.time() >= self.jusqu_a

    def ok(self):
        if self.etat != "ok":
            log(f"{self.nom} : de nouveau disponible")
        self.etat, self.message, self.jusqu_a, self.echecs, self.depuis = "ok", "", 0.0, 0, None

    def echec(self, etat: str, message: str, jusqu_a: float | None = None):
        self.echecs += 1
        if jusqu_a is None:
            jusqu_a = time.time() + min(60 * 2 ** (self.echecs - 1), self.pause_max)
        self.etat, self.message, self.jusqu_a = etat, message[:300], jusqu_a
        self.depuis = self.depuis or time.time()
        log(f"{self.nom} : {etat}, nouvel essai à {dt.datetime.fromtimestamp(jusqu_a):%H:%M} : {message[:200]}")

    def limite(self, e: LimiteAtteinte):
        """Limite du forfait : jusqu'à l'heure annoncée (+1 min), sinon 20, 40 puis 60 min."""
        if e.reprise:
            jusqu_a = min(e.reprise.timestamp() + 60, time.time() + 7 * 86400)
        else:
            jusqu_a = time.time() + min(20 * 60 * 2 ** self.echecs, 3600)
        self.echec("limite", str(e), jusqu_a)

    def publier(self) -> dict:
        return {"etat": self.etat, "message": self.message, "jusqu_a": _iso(self.jusqu_a if self.etat != "ok" else None),
                "depuis": _iso(self.depuis)}

    def restaurer(self, d: dict | None):
        """Après un redémarrage pendant une limite : ne pas relancer Claude avant l'heure de reprise."""
        if d and d.get("etat") == "limite" and d.get("jusqu_a"):
            jusqu_a = dt.datetime.fromisoformat(d["jusqu_a"]).timestamp()
            if jusqu_a > time.time():
                self.etat, self.message, self.jusqu_a, self.echecs = "limite", d.get("message") or "", jusqu_a, 1
                self.depuis = dt.datetime.fromisoformat(d["depuis"]).timestamp() if d.get("depuis") else time.time()


CLAUDE = Garde("Claude", pause_max=30 * 60)
KDRIVE = Garde("kDrive", pause_max=15 * 60)
ETAT = {"demarre": None, "activite": None, "reste": False}  # publié toutes les 30 s par le thread battement


def publier():
    """État du worker pour le site (bandeau, page État)."""
    db.etat_ecrire("service", json.dumps({
        "vu": _iso(time.time()), "demarre": ETAT["demarre"], "arret": ARRET.is_set(), "activite": ETAT["activite"],
        "claude": CLAUDE.publier(), "kdrive": KDRIVE.publier(), "modeles": modeles.indisponibles()}))


def activite(texte: str | None):
    ETAT["activite"] = texte
    try:
        publier()
    except Exception:
        pass


def battement():
    """Publie l'état même pendant une longue analyse : le site sait que le worker vit."""
    while True:
        try:
            publier()
        except Exception as e:
            log(f"état non publié : {e}")
        time.sleep(30)


def noter_appel(**appel):
    """Chaque appel à Claude : gardé pour la page État ; une réponse prouve que Claude est revenu."""
    if appel.get("resultat") == "ok":
        CLAUDE.ok()
    try:
        db.journal_ia(**appel)
    except Exception as e:
        log(f"appel non noté : {e}")


def panne_sur(table: str, ligne_id: int, e: ClaudeIndisponible) -> bool:
    """Vrai si ce document (ou cette révision) fait planter Claude à chaque fois alors que Claude marche.

    Une vraie panne de Claude ne doit pas mettre tout en erreur : 3 échecs comptent seulement si
    Claude répondait juste avant (le reste passe), et les autres travaux sont essayés d'abord
    (tri par nombre d'essais). Au-delà de 6 échecs, le fichier est mis de côté quoi qu'il arrive,
    pour ne pas bloquer l'IA indéfiniment.
    """
    if isinstance(e, ClaudeDeconnecte):
        return False
    n = db.compter_essai(table, ligne_id)
    return (n >= ESSAIS_MAX and CLAUDE.echecs == 0) or n >= 2 * ESSAIS_MAX


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


def indice(dossiers: list[str], nom: str) -> str:
    """Ce que l'ancienne organisation des dossiers laisse deviner, pour le prompt d'analyse."""
    lot = db.import_lot(dossiers[0] if dossiers else "")
    compris = (lot["compris"] or {}).get("/".join(dossiers)) if lot else None
    return importation.texte_indices(dossiers, nom, compris)


def traiter(kd: KDrive, item: dict, matieres: list[str] | None = None):
    dossiers = item.get("dossiers", [])
    doc_id = db.creer(item["id"], item["name"])
    dossier_local = CACHE_DIR / str(item["id"])
    try:
        chemin = kd.download(item["id"], dossier_local / item["name"])
        a = analyser(chemin, dossiers, indice(dossiers, item["name"]), matieres)
    except ErreurAgent as e:
        variantes = db.variantes_de(item["id"])
        if variantes:  # le même document existe dans un autre format : on essaie celui-là
            db.import_basculer(item["id"], variantes[0]["kdrive_id"])
            db.supprimer(doc_id)
            log(f"{item['name']} illisible ({e}), analyse de « {variantes[0]['nom']} » à la place")
            return
        db.maj(doc_id, statut="erreur", erreur=str(e))
        log(f"erreur d'analyse : {item['name']} : {e}")
        return
    except ClaudeIndisponible as e:
        if panne_sur("documents", doc_id, e):
            db.maj(doc_id, statut="erreur", erreur=f"Claude Code s'arrête à chaque essai sur ce fichier : {e}")
            log(f"erreur d'analyse : {item['name']} : Claude plante à chaque essai")
            return
        raise
    finally:
        shutil.rmtree(dossier_local, ignore_errors=True)

    note = appliquer_dossier(a, dossiers)
    statut, note = statut_et_note(a, note)
    if statut == "a_verifier" and a.avis:
        note = f"{note} {a.avis}"
    # L'analyse est enregistrée avant de toucher à kDrive : si le worker s'arrête ou si kDrive
    # tombe maintenant, le rangement est repris au tour suivant (a_ranger) sans rappeler Claude.
    db.maj(doc_id, statut=statut, titre=a.titre, niveau=a.niveau, matiere=a.matiere, type=a.type,
           chapitre=a.chapitre, date_doc=a.date, mots_cles=a.mots_cles, resume=a.resume,
           transcription=a.transcription, confiance=a.confiance, erreur=None, origine="/".join(dossiers) or None,
           note=note, essais=0, modele=a.modele or None, avis=a.avis or None)
    db.marquer_a_ranger([doc_id])
    db.import_marquer([item["id"]], "fait")
    nom = rangement.nouveau_nom(a, item["name"])
    if nom != item["name"]:
        try:
            kd.rename(item["id"], nom)
            db.maj(doc_id, nom=nom)
        except Exception as e:
            log(f"renommage impossible ({e}), nom conservé : {item['name']}")
            nom = item["name"]
    doc = db.document(doc_id)
    ranger_doc(kd, doc, {})
    log(f"{statut.replace('_', ' ')} ({modeles.NOMS.get(a.modele, a.modele)}{', second avis' if a.avis else ''}) : "
        f"{item['name']} → {'/'.join(rangement.dossier_doc(doc))}/{nom}")


def ranger_doc(kd: KDrive, doc: dict, dossiers: dict[tuple, int]):
    """Range un document à sa place dans kDrive, avec sa transcription .md et son autre format.

    Rejouable sans risque : sert après une analyse, après une action sur le site (Organiser,
    reclassement) et pour reprendre un rangement interrompu par une panne ou un redémarrage.
    """
    parts = tuple(rangement.dossier_doc(doc))
    if parts not in dossiers:
        dossiers[parts] = kd.ensure_path(ETUDES_ID, list(parts))
    dest = dossiers[parts]
    try:
        kd.move(doc["kdrive_id"], dest)
    except httpx.HTTPStatusError as e:  # supprimé ou déplacé à la main dans kDrive
        log(f"déplacement kDrive impossible : {doc['nom']} : {e.response.status_code}")
        db.marquer_a_ranger([doc["id"]], False)
        return
    md_id = doc.get("kdrive_md_id")
    if md_id:
        try:
            kd.move(md_id, dest)
        except httpx.HTTPStatusError:
            md_id = None  # transcription .md supprimée : on la redépose
    if not md_id and (doc.get("transcription") or "").strip():
        try:
            md = kd.upload(dest, f"{PurePosixPath(doc['nom']).stem}.md", doc["transcription"].encode())
            db.maj(doc["id"], kdrive_md_id=md.get("id"))
        except httpx.HTTPStatusError as e:
            log(f"envoi de la transcription impossible : {e}")
    if doc.get("variante_kdrive_id"):
        try:
            kd.move(doc["variante_kdrive_id"], dest)
        except httpx.HTTPStatusError:
            pass
    ranger_variantes(kd, doc["kdrive_id"], doc["id"], dest, doc["nom"])
    db.marquer_a_ranger([doc["id"]], False)


def ranger_variantes(kd: KDrive, kdrive_id: int, doc_id: int, dest_id: int, nom: str):
    """L'autre format du document (PDF ↔ Word) suit le document analysé, sous le même nom."""
    for i, v in enumerate(db.variantes_de(kdrive_id)):
        nom_v = PurePosixPath(nom).stem + PurePosixPath(v["nom"]).suffix.lower()
        try:
            kd.move(v["kdrive_id"], dest_id)
            try:
                kd.rename(v["kdrive_id"], nom_v)
            except httpx.HTTPStatusError:
                nom_v = v["nom"]
            if i == 0:
                db.maj(doc_id, variante_kdrive_id=v["kdrive_id"], variante_nom=nom_v)
            db.import_marquer([v["kdrive_id"]], "fait")
        except httpx.HTTPStatusError as e:
            log(f"autre format non rangé : {v['nom']} : {e}")


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
    activite(f"Reclassement de « {doc['titre'] or doc['nom']} »")
    try:
        a = agent.reclasser(doc, matieres)
    except ErreurAgent as e:
        db.maj(doc["id"], statut="a_verifier", note=f"Reclassement impossible : {e}")
        db.marquer_a_ranger([doc["id"]])
        log(f"reclassement impossible : {doc['nom']} : {e}")
        return
    statut, note = statut_et_note(a, None)
    if statut == "a_verifier" and a.avis:
        note = f"{note} {a.avis}"
    db.maj(doc["id"], statut=statut, niveau=a.niveau, matiere=a.matiere, type=a.type,
           chapitre=a.chapitre or doc["chapitre"], titre=a.titre or doc["titre"], confiance=a.confiance, note=note,
           modele=a.modele or None, avis=a.avis or None)
    db.marquer_a_ranger([doc["id"]])
    log(f"reclassé : {doc['nom']} → {statut} {a.niveau} / {a.matiere} / {a.type}")


def ranger_en_attente(kd: KDrive):
    """Déplace dans kDrive les documents modifiés sur le site, ou dont le rangement a été interrompu.

    kDrive en panne : on s'arrête, les documents restent « à ranger » pour le tour suivant.
    """
    dossiers: dict[tuple, int] = {}
    for doc in db.a_ranger():
        if ARRET.is_set():
            return
        try:
            ranger_doc(kd, doc, dossiers)
        except KDriveIndisponible:
            raise
        except Exception as e:
            log(f"rangement impossible : {doc['nom']} : {e}")
            db.marquer_a_ranger([doc["id"]], False)


def traiter_revision(kd: KDrive, rev: dict):
    """Génère une fiche, un quiz, des flashcards, une épreuve ou un plan, puis en dépose le PDF dans Etudes/fiches/.

    Rejouable : la révision reste « en attente » jusqu'au bout (limite, panne, redémarrage).
    """
    activite(f"{revision.GENRES.get(rev['genre'], rev['genre'])} « {rev['sujet']} » "
             f"({modeles.NOMS[modeles.choisir(rev['genre'])]})")
    try:
        contenu, reperes, web = revision.generer(rev["genre"], rev["sujet"], db.documents(rev["doc_ids"]),
                                                 rev["date_eval"], rev.get("consigne") or "")
    except ErreurAgent as e:
        db.maj_revision(rev["id"], "erreur", erreur=str(e))
        log(f"erreur de révision {rev['id']} : {e}")
        return
    except ClaudeIndisponible as e:
        if panne_sur("revisions", rev["id"], e):
            db.maj_revision(rev["id"], "erreur", erreur=f"Claude Code s'arrête à chaque essai : {e}")
            return
        raise
    if rev["genre"] == "cartes":
        db.creer_cartes(rev["id"], json.loads(contenu))
    db.maj_revision(rev["id"], "pret", contenu=contenu, reperes=reperes, web=web)
    copier_pdf(kd, db.revision(rev["id"]))
    log(f"révision prête : {rev['genre']} « {rev['sujet']} »" + (f" ({len(web)} source(s) web)" if web else ""))
    try:
        if not rev.get("campagne_id"):  # une campagne prévient une seule fois, quand tout est prêt
            notifications.revision_prete(rev)
        else:
            notifications.campagne_prete(rev["campagne_id"])
    except Exception as e:
        log(f"notification impossible : {e}")


def copier_pdf(kd: KDrive, rev: dict) -> int | None:
    """PDF de la révision dans Etudes/fiches/<campagne ou matière>/, pour l'ouvrir ou le partager depuis kDrive.

    En cas d'échec (kDrive en panne), la copie est retentée aux tours suivants (5 fois au plus).
    """
    kdrive_id = None
    try:
        sources = {d["id"]: d["titre"] or d["nom"] for d in db.documents(list((rev["reperes"] or {}).values()))}
        dossier = kd.ensure_path(ETUDES_ID, ["fiches", rev.get("campagne_titre") or rev.get("matiere") or "divers"])
        nom = revision.nom_fichier(rev["genre"], rev["sujet"], rev["cree_le"].date(), "pdf")
        kdrive_id = kd.upload(dossier, nom, pdf.revision_pdf(rev, sources)).get("id")
    except Exception as e:
        log(f"copie kDrive de la révision impossible : {e}")
    db.maj_pdf_revision(rev["id"], kdrive_id)
    return kdrive_id


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
    activite(f"Programme de « {campagne['titre']} »")
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
    """Fichiers d'un dossier et de ses sous-dossiers, avec le chemin des dossiers traversés.

    Un dossier d'outil (.vscode, node_modules…) n'est pas parcouru : il est rendu tel quel,
    pour être proposé à la suppression en un bloc.
    """
    for item in kd.list_dir(dossier_id):
        if item["type"] == "dir" and item["name"].lower() in importation.DOSSIERS_INUTILES:
            yield {**item, "dossiers": dossiers}
        elif item["type"] == "dir" and profondeur < 12:
            yield from parcourir(kd, item["id"], dossiers + [item["name"]], profondeur + 1)
        elif item["type"] == "file":
            yield {**item, "dossiers": dossiers}


def a_traiter(kd: KDrive, items: list[dict] | None = None) -> list[dict]:
    """Fichiers à analyser : gardés par le tri, restés 'nouveau' (limite, panne), ou perdus hors de l'inbox.

    Les documents des profs passent en premier ; un fichier qui a déjà fait planter Claude, en dernier.
    """
    if items is None:
        items = list(parcourir(kd, INBOX_ID, []))
    ids = [i["id"] for i in items]
    decisions, docs = db.import_connus(ids), db.statuts_kdrive(ids)
    en_attente = set(db.lots_a_comprendre())  # l'IA doit d'abord lire les noms de dossiers du lot
    retenus = []
    for item in items:
        doc = docs.get(item["id"])
        if doc is not None:
            if doc["statut"] == "nouveau":
                retenus.append((doc["essais"], item))
        elif decisions.get(item["id"]) == "analyser" and item["type"] == "file" and lot_de(item) not in en_attente:
            retenus.append((0, item))
    if len(retenus) < LOT:
        retenus += [(doc["essais"], item) for doc, item in orphelins(kd, ids)]
    retenus.sort(key=lambda r: (r[0], "Pronote" not in r[1]["dossiers"]))
    return [item for _, item in retenus]


def orphelins(kd: KDrive, dans_inbox: list[int]):
    """Documents jamais finis dont le fichier a quitté l'inbox (ancien arrêt au mauvais moment)."""
    for doc in db.documents_orphelins(dans_inbox, LOT):
        try:
            info = kd.stat(doc["kdrive_id"])
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                db.supprimer(doc["id"])
                log(f"document oublié : {doc['nom']} n'existe plus dans kDrive")
            continue
        yield doc, {"id": doc["kdrive_id"], "name": info.get("name") or doc["nom"], "type": "file",
                    "dossiers": (doc["origine"] or "").split("/") if doc.get("origine") else []}


# --- Tri des dossiers importés --------------------------------------------------------

STABLE = 90          # secondes sans nouveau fichier avant de trier un lot (copie terminée)
_vus: dict[str, tuple[int, float]] = {}  # lot → (nombre de fichiers nouveaux, depuis quand)


def lot_de(item: dict) -> str:
    return item["dossiers"][0] if item["dossiers"] else ""


def trier_inbox(kd: KDrive, items: list[dict]) -> int:
    """Trie les lots complets de fichiers jamais vus. Renvoie le nombre de fichiers triés."""
    ids = [i["id"] for i in items]
    connus, docs = db.import_connus(ids), db.statuts_kdrive(ids)
    nouveaux: dict[str, list[dict]] = {}
    for item in items:
        if item["id"] not in connus and item["id"] not in docs:
            nouveaux.setdefault(lot_de(item), []).append(item)
    maintenant, tries = time.monotonic(), 0
    for lot in list(_vus):
        if lot not in nouveaux:
            del _vus[lot]
    for lot, lot_items in nouveaux.items():
        n, depuis = _vus.get(lot, (None, maintenant))
        if n != len(lot_items):
            _vus[lot] = (len(lot_items), maintenant)  # nouveau ou encore en cours de copie : on attend
            continue
        if maintenant - depuis < STABLE:
            continue
        fichiers = importation.trier([importation.Fichier(
            i["id"], i["name"], i["dossiers"], int(i.get("size") or 0), dossier=i["type"] == "dir") for i in lot_items])
        if besoin_organisation(fichiers):
            db.import_a_comprendre(lot)  # passe IA faite par comprendre_en_attente, même après une panne
        db.import_enregistrer(lot, fichiers)
        _vus.pop(lot, None)
        tries += len(fichiers)
        compte = {d: sum(f.decision == d for f in fichiers) for d in ("analyser", "inutile", "variante", "page")}
        log(f"import « {lot or 'racine'} » trié : {compte['analyser']} à analyser, {compte['page']} pages de scans, "
            f"{compte['variante']} autres formats, {compte['inutile']} inutiles à confirmer")
        if compte["inutile"] or any(f.decision == "page" and not f.sur for f in fichiers):
            try:
                notifications.import_a_confirmer(lot, compte["inutile"])
            except Exception as e:
                log(f"notification impossible : {e}")
    return tries


def besoin_organisation(fichiers: list[importation.Fichier]) -> bool:
    """Vrai si les heuristiques ne suffisent pas : assez de documents, dossiers sans année ou matière."""
    dossiers: dict[str, int] = {}
    for f in fichiers:
        if f.categorie == "document":
            cle = "/".join(f.dossiers)
            dossiers[cle] = dossiers.get(cle, 0) + 1
    incomplets = [d for d in dossiers if not all(
        importation.indices(d.split("/") if d else []).get(k) for k in ("niveau", "matiere"))]
    return sum(dossiers.values()) >= 5 and bool(incomplets) and not any("Pronote" in d.split("/") for d in dossiers)


def comprendre_en_attente():
    """Une passe IA (modèle léger) sur les seuls noms de dossiers de chaque lot qui en a besoin.

    Limite ou panne de Claude : le lot attend (ses fichiers aussi), et la passe se fait au retour.
    """
    for lot in db.lots_a_comprendre():
        activite(f"Organisation des dossiers de « {lot or 'inbox'} »")
        try:
            resume, compris = agent.interpreter_arborescence(db.import_dossiers(lot), db.matieres())
        except ErreurAgent as e:
            log(f"organisation de « {lot} » non interprétée par l'IA ({e}) : indices des noms de dossiers seulement")
        else:
            db.import_lot_maj(lot, resume, compris)
            log(f"organisation de « {lot} » comprise : {resume}")
        db.lot_compris(lot)


def executer_choix(kd: KDrive) -> bool:
    """Applique dans kDrive les choix faits sur la page Import, et fusionne les scans. Vrai si l'inbox a changé."""
    change = False
    for r in db.import_a_executer():
        try:
            if r["decision"] == "supprimer":
                kd.delete(r["kdrive_id"])
                db.import_oublier([r["kdrive_id"]])
                log(f"supprimé (corbeille kDrive) : {r['chemin']}")
            else:
                dest = kd.ensure_path(ETUDES_ID, ["autres"] + r["chemin"].split("/")[:-1])
                kd.move(r["kdrive_id"], dest)
                db.import_marquer([r["kdrive_id"]], "fait")
                log(f"gardé à part : {r['chemin']} → autres/")
        except Exception as e:  # déjà supprimé ou déplacé à la main dans kDrive
            log(f"{r['chemin']} : {e}")
            db.import_oublier([r["kdrive_id"]])
        change = True
    for groupe in db.import_groupes(sur=True):
        fusionner(kd, groupe)
        change = True
    return change


def fusionner(kd: KDrive, groupe: str):
    """Pages d'un même scan réunies en un PDF, déposé dans l'inbox pour être analysé.

    Les pages d'origine sont gardées dans Etudes/import/pages-fusionnees/.
    """
    pages = db.import_groupe(groupe)
    if not pages:
        return
    dossiers = pages[0]["chemin"].split("/")[:-1]
    local = CACHE_DIR / f"fusion-{pages[0]['kdrive_id']}"
    try:
        chemins = [kd.download(p["kdrive_id"], local / f"{i:03d}{PurePosixPath(p['nom']).suffix.lower()}")
                   for i, p in enumerate(pages)]
        contenu = importation.fusionner_pdf(chemins)
    except Exception as e:
        log(f"fusion impossible de « {groupe} » ({e}) : pages analysées une par une")
        db.import_marquer([p["kdrive_id"] for p in pages], "analyser")
        return
    finally:
        shutil.rmtree(local, ignore_errors=True)
    nom = importation.nom_fusion(groupe.split("#")[0], len(pages))
    nouveau = kd.upload(kd.ensure_path(INBOX_ID, dossiers), nom, contenu, conflict="rename")
    fichier = importation.Fichier(nouveau["id"], nouveau.get("name") or nom, dossiers, len(contenu),
                                  raison=f"{len(pages)} pages fusionnées", groupe=groupe)
    db.import_enregistrer(pages[0]["lot"], [fichier])
    db.import_marquer([p["kdrive_id"] for p in pages], "fusionne")
    originaux = kd.ensure_path(ETUDES_ID, ["import", "pages-fusionnees"] + dossiers)
    for p in pages:
        try:
            kd.move(p["kdrive_id"], originaux)
        except Exception as e:
            log(f"page d'origine non déplacée : {p['chemin']} : {e}")
    log(f"{len(pages)} pages fusionnées → {'/'.join(dossiers + [fichier.nom])}")


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
    while time.monotonic() < fin and not ARRET.is_set():
        try:
            if db.scan_demande():
                log("scan demandé depuis le site")
                return
        except Exception:
            pass  # base momentanément injoignable : on attend la fin du délai
        ARRET.wait(5)


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
        log(f"pronote : synchro impossible, nouvel essai dans {REESSAI_PRONOTE // 60} min : {e}")
        return time.monotonic() + REESSAI_PRONOTE
    return time.monotonic() + SYNCHRO_PRONOTE


def etape(nom: str, faire, *args):
    """Exécute une étape du tour ; une panne de Claude ou de kDrive ouvre son disjoncteur."""
    try:
        return faire(*args)
    except LimiteAtteinte as e:
        CLAUDE.limite(e)
    except ClaudeDeconnecte as e:
        CLAUDE.echec("connexion", f"jeton Claude refusé ou expiré : {e}", time.time() + 30 * 60)
    except ClaudeIndisponible as e:
        CLAUDE.echec("panne", str(e))
    except KDriveIndisponible as e:
        KDRIVE.echec("panne", str(e))
    except Exception as e:
        log(f"erreur ({nom}) : {e.__class__.__name__} : {e}")
    finally:
        activite(None)
    return None


def analyser_inbox(kd: KDrive, items: list[dict]):
    matieres = db.matieres()
    for item in items[:LOT]:
        if ARRET.is_set() or not CLAUDE.disponible():
            return
        activite(f"Analyse de « {item['name']} »")
        log(f"analyse : {'/'.join(item['dossiers'] + [item['name']])}")
        traiter(kd, item, matieres)
    ETAT["reste"] = len(items) > LOT  # encore du travail : pas d'attente d'une minute avant le lot suivant


def revisions_en_attente(kd: KDrive):
    # Le programme d'une campagne d'abord : il est court et tu vois tout de suite ta semaine.
    for campagne in db.campagnes_en_attente():
        if ARRET.is_set():
            return
        log(f"campagne : « {campagne['titre']} »")
        traiter_campagne(campagne)
    for rev in db.revisions_en_attente():
        if ARRET.is_set() or not CLAUDE.disponible():
            return
        log(f"révision : {rev['genre']} « {rev['sujet']} »")
        traiter_revision(kd, rev)


def reclasser_en_attente():
    a_reclasser = db.a_reclasser(LOT * 4)  # peu coûteux : seulement du texte
    matieres = db.matieres() if a_reclasser else []
    for doc in a_reclasser:
        if ARRET.is_set() or not CLAUDE.disponible():
            return
        reclasser(doc, matieres)


def pdf_manquants(kd: KDrive):
    for rev in db.revisions_sans_pdf():
        copier_pdf(kd, rev)


def lire_inbox(kd: KDrive) -> list[dict]:
    inbox = list(parcourir(kd, INBOX_ID, []))
    KDRIVE.ok()
    trier_inbox(kd, inbox)
    if executer_choix(kd):
        inbox = list(parcourir(kd, INBOX_ID, []))
    return inbox


def tour(kd: KDrive, dernier_nettoyage: float) -> float:
    """Un passage du worker. Renvoie l'heure du dernier nettoyage de l'inbox."""
    # Ce que tu attends sur le site passe d'abord : révisions, puis reclassements, puis les scans.
    if CLAUDE.disponible():
        etape("révisions", revisions_en_attente, kd)
    if CLAUDE.disponible() and not ARRET.is_set():
        etape("reclassements", reclasser_en_attente)
    if not KDRIVE.disponible() or ARRET.is_set():
        return dernier_nettoyage
    etape("rangement", ranger_en_attente, kd)
    etape("PDF des révisions", pdf_manquants, kd)
    inbox = etape("inbox", lire_inbox, kd)
    if CLAUDE.disponible() and not ARRET.is_set():
        etape("organisation des imports", comprendre_en_attente)
    ETAT["reste"] = False
    if inbox is not None and CLAUDE.disponible() and KDRIVE.disponible() and not ARRET.is_set():
        etape("analyses", lambda: analyser_inbox(kd, a_traiter(kd, inbox)))
    if time.monotonic() - dernier_nettoyage > NETTOYAGE and KDRIVE.disponible() and not ARRET.is_set():
        etape("nettoyage", nettoyer_inbox, kd)
        return time.monotonic()
    return dernier_nettoyage


def arreter(signum, _frame):
    if not ARRET.is_set():
        log("arrêt demandé : je termine le travail en cours, puis je m'arrête")
    ARRET.set()


def main():
    signal.signal(signal.SIGTERM, arreter)
    signal.signal(signal.SIGINT, arreter)
    while True:  # au démarrage de la VM, la base peut mettre quelques secondes à accepter les connexions
        try:
            db.init()
            break
        except Exception as e:
            log(f"base de données pas encore prête ({e.__class__.__name__}), nouvel essai dans 5 s")
            if ARRET.wait(5):
                return
    agent.journal = noter_appel
    try:
        CLAUDE.restaurer(json.loads(db.etat_valeur("service") or "{}").get("claude"))
    except ValueError:
        pass
    ETAT["demarre"] = _iso(time.time())
    kd = KDrive.from_env()
    log(f"worker : surveillance du dossier kDrive {INBOX_ID}, modèles : "
        + ", ".join(f"{t} → {m['actuel']}" for t, m in ((r["tache"], r) for r in modeles.tableau())))
    if os.environ.get("CLAUDE_MODEL"):
        log("CLAUDE_MODEL n'est plus utilisé : chaque tâche a son modèle (voir CLAUDE_MODELES dans .env.example)")
    threading.Thread(target=notifications.boucle, daemon=True, name="notifications").start()
    threading.Thread(target=battement, daemon=True, name="battement").start()
    prochaine_synchro, dernier_nettoyage = 0.0, float("-inf")
    while not ARRET.is_set():
        prochaine_synchro = synchro_pronote(prochaine_synchro)
        dernier_nettoyage = tour(kd, dernier_nettoyage)
        attendre(5 if ETAT["reste"] and CLAUDE.disponible() else INTERVALLE)
    publier()
    log("worker arrêté proprement")


if __name__ == "__main__":
    main()
