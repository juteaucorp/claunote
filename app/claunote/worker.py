"""Surveille l'inbox kDrive, trie puis analyse chaque nouveau fichier et le range.

Un dossier copié en entier dans l'inbox forme un « lot ». Une fois le lot complet (plus de
nouveau fichier pendant 90 s), il est trié sans IA (importation.py) : fichiers inutiles proposés
à la suppression sur le site, doublons PDF/Word, pages de scans à fusionner ; puis une seule
passe IA lit les noms de dossiers pour comprendre l'ancienne organisation.
Chaque document gardé : téléchargement → garde-fou des documents personnels (sans IA, sensible.py)
→ analyse Claude Code → base de données → déplacement dans archives/<niveau>/<matière>/<type>
(ou a-verifier/) + transcription .md à côté. Un très gros scan (une année entière) est d'abord
découpé en documents (decoupage.py), qui reviennent dans l'inbox et sont analysés un par un.
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

import httpx

from claunote import (actualites, agent, db, decoupage, importation, index, lecture, local, modeles, notifications,
                      ocr, pdf, pronote, rangement, ressources, revision, sensible)
from claunote.agent import NIVEAUX, ClaudeDeconnecte, ClaudeIndisponible, ErreurAgent, Interrompu, LimiteAtteinte, analyser
from claunote.kdrive import KDrive, KDriveIndisponible
from claunote.matieres import harmoniser

INBOX_ID = int(os.environ["KDRIVE_INBOX_ID"])
ETUDES_ID = int(os.environ["KDRIVE_ETUDES_ID"])
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/cache"))
INTERVALLE = 60          # secondes entre deux vérifications de l'inbox
# Inbox inchangée depuis CALME secondes : on la regarde toutes les INTERVALLE_CALME secondes seulement.
# Le site réveille le worker tout de suite (dépôt, scan, révision…) : seul un fichier posé directement
# dans kDrive attend un peu plus.
CALME, INTERVALLE_CALME = 1800, 300
PARALLELE = agent.PARALLELE  # analyses menées en même temps (CLAUDE_PARALLELE, 3 par défaut)
# documents analysés par passage ; au moins 2 par fil (un ancien WORKER_LOT=5 briderait le parallélisme)
LOT = max(int(os.environ.get("WORKER_LOT") or 4 * PARALLELE), 2 * PARALLELE)
SYNCHRO_PRONOTE = int(os.environ.get("PRONOTE_INTERVALLE_H", "3")) * 3600
REESSAI_PRONOTE = 15 * 60  # synchro Pronote ratée : nouvel essai dans 15 min, pas dans 3 h
NETTOYAGE = 10 * 60        # dossiers vides de l'inbox : vérifiés toutes les 10 min
ESSAIS_MAX = 3             # Claude plante 3 fois sur le même document : il passe en erreur
ARRET = threading.Event()  # SIGTERM reçu : finir le travail en cours puis quitter
KDRIVE_VERROU = threading.Lock()  # rangements des analyses parallèles : un dossier n'est pas créé deux fois
PAUSE = threading.Event()  # analyses en pause (page Analyses du site) : révisions et rangements continuent
STOP = threading.Event()   # « Arrêter maintenant » : les analyses en cours abandonnent (elles restent dans la file)
EN_COURS: list[str] = []   # ce que font les fils d'analyse (local et Claude), publié sur la page Activité
EN_COURS_VERROU = threading.Lock()


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
ETAT = {"demarre": None, "activite": None, "reste": False, "en_cours": [], "local": {}}  # publié toutes les 30 s
SONDE = {"le": None, "resultat": None, "message": ""}  # dernier test de Claude demandé depuis le site
SONDE_ECART = 60  # boutons répétés : au plus un test par minute


def reglages_analyse() -> dict:
    return local.reglages()


def surveiller():
    """Toutes les 3 s : pause des analyses et bouton « Arrêter maintenant » du site."""
    while not ARRET.is_set():
        try:
            arreter = db.etat_consommer("analyses_arreter")
            pause = db.etat_valeur("analyses_pause") == "oui"
            if pause and not PAUSE.is_set():
                log("analyses en pause (demandé depuis le site)")
            elif not pause and PAUSE.is_set():
                log("analyses reprises")
            (PAUSE.set if pause else PAUSE.clear)()
            if arreter and pause:  # « Reprendre » cliqué juste après : rien à arrêter
                STOP.set()
                n = agent.interrompre()
                log(f"arrêt des analyses demandé : {n} appel(s) à Claude arrêté(s), le reste s'arrête entre deux pages")
                publier()
            elif not pause:
                STOP.clear()
        except Exception as e:
            log(f"surveillance des demandes du site impossible : {e}")
        ARRET.wait(3)


def publier():
    """État du worker pour le site (bandeau, page État)."""
    db.etat_ecrire("service", json.dumps({
        "vu": _iso(time.time()), "demarre": ETAT["demarre"], "arret": ARRET.is_set(), "activite": ETAT["activite"],
        "en_cours": ETAT["en_cours"], "pause": PAUSE.is_set(), "local": ETAT["local"],
        "claude": CLAUDE.publier(), "kdrive": KDRIVE.publier(), "modeles": modeles.indisponibles(), "sonde": SONDE,
        "processeur": ressources.publier()}))


def activite(texte: str | None):
    ETAT["activite"] = texte
    try:
        publier()
    except Exception:
        pass


_liberation = {"le": float("-inf")}


def liberer_si_besoin(maintenant: float | None = None) -> bool:
    """Mémoire de la VM basse : les modèles au repos (Ollama, service lecture) sont déchargés tout de suite
    au lieu d'attendre leur délai ; ils se rechargent seuls au prochain usage. Au plus une fois par 10 min."""
    maintenant = time.monotonic() if maintenant is None else maintenant
    libre = ressources.memoire_libre_mo()
    if libre is None or libre >= 2 * ressources.MEMOIRE_MIN_MO or ressources.local_actif():
        return False
    if maintenant - _liberation["le"] < 600:
        return False
    _liberation["le"] = maintenant
    ollama, lecture_ = local.liberer(), lecture.liberer()
    log(f"mémoire basse ({libre} Mo libres) : modèles au repos déchargés"
        f"{' (Ollama)' if ollama else ''}{' (lecture : ' + ', '.join(lecture_) + ')' if lecture_ else ''}")
    return True


def battement():
    """Publie l'état même pendant une longue analyse : le site sait que le worker vit."""
    while True:
        try:
            freinait = ressources.BUDGET.freine
            ressources.BUDGET.echantillon()   # occupation du processeur, pour le budget sur la durée
            if ressources.BUDGET.freine != freinait:
                log(ressources.BUDGET.raison() or f"processeur redescendu à {round(ressources.BUDGET.moyenne * 100)} % "
                                                  "en moyenne : le travail de fond reprend à pleine vitesse")
            liberer_si_besoin()
            ETAT["local"] = {"ocr": ocr.disponible(), "eteints": ressources.locaux_eteints(),
                             **{k: v for k, v in local.etat_ollama().items() if k in ("pret", "message")}}
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


def memoire(item: dict) -> Path:
    """Morceaux déjà transcrits d'un long scan : gardés jusqu'à la fin de son analyse."""
    return CACHE_DIR / "morceaux" / str(item["id"])


def distinctes() -> set[frozenset]:
    """Paires de matières que tu as choisi de garder séparées (toutes années) : jamais harmonisées."""
    try:
        return set().union(*db.matieres_distinctes().values())
    except Exception:
        return set()


def arrete() -> bool:
    return STOP.is_set() or ARRET.is_set()


def claude_peut_aider() -> bool:
    """Claude est là et le réglage permet de lui demander de l'aide pour une analyse."""
    return CLAUDE.disponible() and reglages_analyse()["mode"] != "toujours"


def etape_echec(e: Exception):
    """Claude est tombé pendant qu'il aidait le modèle local : on ouvre son disjoncteur, le local continue."""
    if isinstance(e, LimiteAtteinte):
        CLAUDE.limite(e)
    elif isinstance(e, ClaudeDeconnecte):
        CLAUDE.echec("connexion", f"jeton Claude refusé ou expiré : {e}", time.time() + 30 * 60)
    else:
        CLAUDE.echec("panne", str(e))


def a_relire(a) -> bool:
    """Un document lu sur la VM sans l'aide de Claude sera relu par Claude à son retour, si c'est utile."""
    if a.modele != "local" or "vérifié par Claude" in (a.avis or ""):
        return False
    r = reglages_analyse()
    return r["relire"] and (a.a_relire or r["mode"] == "auto")


def traiter(kd: KDrive, item: dict, matieres: list[str] | None = None, cache_dossiers: dict | None = None):
    """Analyse un fichier et le range. `item["local"]` : sans Claude ; `item["relire"]` : relecture par Claude
    d'un document déjà analysé sans lui (en cas d'échec, il garde son classement)."""
    dossiers = item.get("dossiers", [])
    relecture = item.get("relire")
    doc_id = db.creer(item["id"], item["name"])
    dossier_local = CACHE_DIR / str(item["id"])
    debut = time.monotonic()
    try:
        chemin = kd.download(item["id"], dossier_local / item["name"])
        telecharge = time.monotonic()
        if not relecture and avant_analyse(kd, item, chemin, doc_id):
            return
        a = None
        if item.get("local"):
            try:
                a = local.analyser(chemin, dossiers, indice(dossiers, item["name"]), matieres, arret=arrete,
                                   claude=claude_peut_aider(), claude_echec=etape_echec)
            except local.BesoinClaude as e:  # manuscrit illisible par l'OCR : Claude lit le scan lui-même
                log(f"le modèle local demande l'aide de Claude : {item['name']} : {e}")
                item = {**item, "local": False}
        if a is None:
            a = analyser(chemin, dossiers, indice(dossiers, item["name"]), matieres, memoire(item))
    except ErreurAgent as e:
        shutil.rmtree(memoire(item), ignore_errors=True)
        if item.get("local"):  # illisible sans Claude (manuscrit…) : il attend Claude dans la file, sans erreur
            db.maj(doc_id, relire=True, note=f"Pas lisible sans Claude ({e}) : en attente de Claude.")
            log(f"illisible sans Claude, attend Claude : {item['name']} : {e}")
            return
        if relecture:
            db.maj(doc_id, relire=False, note=f"Relecture par Claude impossible ({e}) : classement local gardé.")
            log(f"relecture impossible : {item['name']} : {e}")
            return
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
        if not relecture and panne_sur("documents", doc_id, e):
            db.maj(doc_id, statut="erreur", erreur=f"Claude Code s'arrête à chaque essai sur ce fichier : {e}")
            log(f"erreur d'analyse : {item['name']} : Claude plante à chaque essai")
            return
        raise
    finally:
        shutil.rmtree(dossier_local, ignore_errors=True)

    note = appliquer_dossier(a, dossiers)
    a.matiere = harmoniser(a.matiere, matieres or [], distinctes())  # « Maths » → « Mathématiques »
    statut, note = statut_et_note(a, note)
    if statut == "a_verifier" and a.avis:
        note = f"{note} {a.avis}"
    # L'analyse est enregistrée avant de toucher à kDrive : si le worker s'arrête ou si kDrive
    # tombe maintenant, le rangement est repris au tour suivant (a_ranger) sans rappeler Claude.
    db.maj(doc_id, statut=statut, titre=a.titre, niveau=a.niveau, matiere=a.matiere, type=a.type,
           chapitre=a.chapitre, date_doc=a.date, mots_cles=a.mots_cles, resume=a.resume,
           transcription=a.transcription, confiance=a.confiance, erreur=None, origine="/".join(dossiers) or None,
           note=note, essais=0, modele=a.modele or None, avis=a.avis or None,
           relire=a_relire(a))
    db.marquer_a_ranger([doc_id])
    db.import_marquer([item["id"]], "fait")
    shutil.rmtree(memoire(item), ignore_errors=True)
    analyse = time.monotonic()
    nom = rangement.nouveau_nom(a, item["name"])
    if nom != item["name"]:
        try:
            kd.rename(item["id"], nom)
            db.maj(doc_id, nom=nom)
        except Exception as e:
            log(f"renommage impossible ({e}), nom conservé : {item['name']}")
            nom = item["name"]
    doc = db.document(doc_id)
    with KDRIVE_VERROU:
        ranger_doc(kd, doc, {} if cache_dossiers is None else cache_dossiers)
    fin = time.monotonic()
    log(f"{statut.replace('_', ' ')} ({modeles.NOMS.get(a.modele, a.modele)}{', second avis' if a.avis else ''}) : "
        f"{item['name']} → {'/'.join(rangement.dossier_doc(doc))}/{nom} "
        f"[téléchargement {telecharge - debut:.0f} s, IA {analyse - telecharge:.0f} s, rangement {fin - analyse:.0f} s]")


# --- Avant l'IA : documents personnels et très gros scans -------------------------------

def avant_analyse(kd: KDrive, item: dict, chemin: Path, doc_id: int) -> bool:
    """Vrai si le fichier ne doit pas être analysé tel quel (personnel, ou gros scan découpé)."""
    dossiers = item.get("dossiers", [])
    # Documents des profs, ou fichier que tu as dit « pas personnel » : pas de garde-fou
    controle = "Pronote" not in dossiers and db.sensible_decision(item["id"]) != "analyser"
    if controle:
        v = sensible.examiner_nom(item["name"], dossiers)
        if v.niveau == "sur":
            mettre_de_cote(kd, item, doc_id, v)
            return True
    if decoupage.a_decouper(chemin):
        decouper(kd, item, chemin, doc_id)
        return True
    if controle:
        activite(f"Vérification de « {item['name']} » (documents personnels, sans IA)")
        v = sensible.examiner(chemin, item["name"], dossiers)
        if v:
            mettre_de_cote(kd, item, doc_id, v)
            return True
    return False


def mettre_de_cote(kd: KDrive, item: dict, doc_id: int, v: sensible.Verdict):
    """Document personnel : corbeille kDrive si c'est sûr, sinon il attend ton avis (page Import)."""
    dossiers = item.get("dossiers", [])
    chemin = "/".join(dossiers + [item["name"]])
    raisons = "; ".join(v.raisons)
    if v.niveau == "sur":
        try:
            kd.delete(item["id"])
        except httpx.HTTPStatusError as e:  # déjà supprimé à la main
            log(f"{chemin} : corbeille impossible ({e.response.status_code})")
        db.import_oublier([item["id"]])
        db.sensible_noter(item["id"], item["name"], chemin, v.categorie, v.raisons, "corbeille")
        log(f"document personnel ({v.libelle}) mis à la corbeille kDrive sans analyse : {chemin} ({raisons})")
    else:
        db.import_enregistrer(lot_de(item), [importation.Fichier(
            item["id"], item["name"], dossiers, int(item.get("size") or 0), decision="sensible", raison=raisons)])
        db.import_marquer([item["id"]], "sensible")
        db.sensible_noter(item["id"], item["name"], chemin, v.categorie, v.raisons, "doute")
        log(f"peut-être personnel ({v.libelle}), pas analysé, en attente de ton avis : {chemin} ({raisons})")
    db.supprimer(doc_id)
    try:
        notifications.document_personnel(item["name"], v.libelle, v.niveau == "sur")
    except Exception as e:
        log(f"notification impossible : {e}")


def decouper(kd: KDrive, item: dict, chemin: Path, doc_id: int):
    """Très gros scan → documents déposés dans l'inbox à côté de lui. Reprenable à chaque étape.

    L'état (lecture locale, réponses de Claude par paquet, documents envoyés) est gardé en base :
    une limite du forfait ou un redémarrage reprend où on en était, sans rappeler Claude.
    """
    dossiers, nom = item.get("dossiers", []), item["name"]
    etat = db.decoupage(item["id"]) or {}
    if etat.get("statut") == "fait":  # fini avant un arrêt : il ne restait qu'à oublier la fiche
        db.supprimer(doc_id)
        return
    if not etat:
        db.decoupage_maj(item["id"], nom=nom, chemin="/".join(dossiers + [nom]), pages=ocr.nb_pages(chemin))
    plan = etat.get("plan")
    if plan is None:
        plan = planifier_decoupage(item, chemin, etat)
        if plan is None:
            return  # arrêt demandé : la suite au prochain démarrage
    envoyer_decoupage(kd, item, chemin, plan)
    # L'original : gardé à part (les documents produits en viennent), ou à la corbeille s'il contient
    # des pages personnelles sûres, pour qu'elles ne restent pas dans tes cours.
    if (plan["sensibles"].get("sur") or {}).get("pages"):
        kd.delete(item["id"])
        devenu = "corbeille kDrive"
    else:
        with KDRIVE_VERROU:
            dest = kd.ensure_path(ETUDES_ID, ["import", "scans-decoupes"] + dossiers)
        kd.move(item["id"], dest)
        devenu = "Etudes/import/scans-decoupes"
    db.import_enregistrer(lot_de(item), [importation.Fichier(item["id"], nom, dossiers, int(item.get("size") or 0))])
    db.import_marquer([item["id"]], "decoupe")
    db.decoupage_maj(item["id"], statut="fait")
    db.supprimer(doc_id)
    n = len(plan["documents"])
    log(f"gros scan découpé : {nom} → {n} documents, {len(plan['blanches'])} pages blanches écartées, original → {devenu}")
    try:
        notifications.decoupage_fini(nom, n)
    except Exception as e:
        log(f"notification impossible : {e}")


def planifier_decoupage(item: dict, chemin: Path, etat: dict) -> dict | None:
    nom = item["name"]
    miniatures = CACHE_DIR / f"decoupage-{item['id']}"
    pages = etat.get("lecture")
    if pages is None or not miniatures.exists():
        log(f"gros scan : {nom} ({ocr.nb_pages(chemin)} pages), lecture locale de chaque page")
        pages = decoupage.lire_local(chemin, miniatures, progression=lambda n, t: activite(
            f"Découpage de « {nom} » : lecture des pages ({n}/{t}), sans IA"))
        db.decoupage_maj(item["id"], lecture=pages, statut="decoupage")
    coupes, toutes = dict(etat.get("coupes") or {}), {}
    precedente = None
    for i in range(0, len(pages), decoupage.PAQUET):
        paquet = pages[i:i + decoupage.PAQUET]
        cle = str(paquet[0]["page"])
        if cle not in coupes:
            if ARRET.is_set() or not CLAUDE.disponible():
                return None
            activite(f"Découpage de « {nom} » : pages {paquet[0]['page']} à {paquet[-1]['page']} sur {len(pages)}")
            try:
                reponse = decoupage.demander_coupes(paquet, miniatures, precedente)
            except ErreurAgent as e:
                log(f"découpage de {nom}, pages {cle}+ : réponse de Claude inutilisable ({e}), indices locaux seulement")
                reponse = decoupage.coupes_locales(paquet)
            coupes[cle] = {str(k): v for k, v in reponse.items()}
            db.decoupage_maj(item["id"], coupes=coupes)
        toutes.update({int(k): v for k, v in coupes[cle].items()})
        montrees = [p["page"] for p in paquet if not p["blanche"] and not p["sensible"]]
        if montrees:
            debut = max((n for n, c in toutes.items() if c.get("nouveau") and n <= montrees[-1]), default=None)
            precedente = {"page": montrees[-1], **({k: toutes[debut].get(k) for k in ("titre", "matiere")}
                                                     if debut else {})}
    plan = decoupage.plan(pages, toutes)
    db.decoupage_maj(item["id"], plan=plan, statut="envoi")
    shutil.rmtree(miniatures, ignore_errors=True)
    return plan


def envoyer_decoupage(kd: KDrive, item: dict, chemin: Path, plan: dict):
    """Chaque document dans l'inbox (même dossier que le gros scan) ; pages personnelles à part."""
    dossiers, nom = item.get("dossiers", []), item["name"]
    with KDRIVE_VERROU:  # analyses en parallèle : un dossier n'est pas créé deux fois
        dest = kd.ensure_path(INBOX_ID, dossiers)
        perso = kd.ensure_path(ETUDES_ID, ["import", "pages-personnelles"]) if "sur" in plan["sensibles"] else None
    total = len(plan["documents"])
    for k, d in enumerate(plan["documents"], 1):
        if d.get("kdrive_id"):
            continue
        activite(f"Découpage de « {nom} » : envoi du document {k}/{total}")
        contenu = decoupage.extraire(chemin, d["pages"])
        nom_d = decoupage.nom_partie(nom, k, total, d)
        envoi = kd.upload(dest, nom_d, contenu, conflict="rename")
        d["kdrive_id"] = envoi["id"]
        db.import_enregistrer(lot_de(item), [importation.Fichier(
            envoi["id"], envoi.get("name") or nom_d, dossiers, len(contenu),
            raison=f"document {k}/{total} de « {nom} » (pages {decoupage.plages(d['pages'])})")])
        db.decoupage_maj(item["id"], plan=plan)
    stem = rangement.slug(PurePosixPath(nom).stem)[:40] or "scan"
    for niveau, g in plan["sensibles"].items():
        if g.get("fait"):
            continue
        pages = decoupage.plages(g["pages"])
        nom_s = f"{stem}-pages-{pages.replace(', ', '_')}-personnel.pdf"
        contenu = decoupage.extraire(chemin, g["pages"])
        v = sensible.Verdict(niveau, g["categorie"], g["raisons"])
        if niveau == "sur":
            envoi = kd.upload(perso, nom_s, contenu, conflict="rename")
            kd.delete(envoi["id"])
            db.sensible_noter(envoi["id"], nom, "/".join(dossiers + [nom]), g["categorie"], g["raisons"],
                              "corbeille", pages)
            log(f"pages {pages} de {nom} ({v.libelle}) mises à la corbeille kDrive sans analyse")
        else:
            envoi = kd.upload(dest, nom_s, contenu, conflict="rename")
            nom_s = envoi.get("name") or nom_s
            db.import_enregistrer(lot_de(item), [importation.Fichier(
                envoi["id"], nom_s, dossiers, len(contenu), decision="sensible", raison="; ".join(g["raisons"]))])
            db.sensible_noter(envoi["id"], nom_s, "/".join(dossiers + [nom_s]), g["categorie"], g["raisons"],
                              "doute", pages)
            log(f"pages {pages} de {nom} peut-être personnelles ({v.libelle}) : en attente de ton avis")
        g["fait"] = True
        db.decoupage_maj(item["id"], plan=plan)
        try:
            notifications.document_personnel(nom, v.libelle, niveau == "sur", pages)
        except Exception as e:
            log(f"notification impossible : {e}")


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
    try:
        a = None
        if reglages_analyse()["mode"] != "jamais":  # le modèle local d'abord
            a = local.reclasser(doc, matieres, claude=claude_peut_aider(), claude_echec=etape_echec)
        if a is None:
            if not CLAUDE.disponible():
                return  # ni local ni Claude : il attend
            a = agent.reclasser(doc, matieres)
        a.matiere = harmoniser(a.matiere, matieres, distinctes())
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
    quoi = (f"{revision.GENRES.get(rev['genre'], rev['genre'])} « {rev['sujet']} » "
            f"({modeles.NOMS[modeles.choisir(rev['genre'])]})")
    activite(quoi)
    try:
        # Beaucoup de cours : condensés par groupes, gardés en base pour reprendre après une limite
        contenu, reperes, web = revision.generer(
            rev["genre"], rev["sujet"], db.documents(rev["doc_ids"]), rev["date_eval"], rev.get("consigne") or "",
            pieces=db.pieces_jointes(rev["id"], rev.get("campagne_id"), contenu=True), memoire=db.Condenses(),
            progres=lambda etape: activite(f"{quoi} : {etape}"), strict=bool(rev.get("strict")))
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
    try:
        copier_pdf(kd, db.revision(rev["id"]))
    except KDriveIndisponible as e:   # la révision est prête sur le site ; son PDF ira dans kDrive plus tard
        log(f"PDF de la révision {rev['id']} : kDrive indisponible ({e}), copie au prochain tour")
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
    except KDriveIndisponible:
        raise   # panne : le disjoncteur s'ouvre, la copie est refaite plus tard sans compter d'essai
    except Exception as e:
        log(f"copie kDrive de la révision impossible : {e}")
    db.maj_pdf_revision(rev["id"], kdrive_id)
    return kdrive_id


def traiter_campagne(campagne: dict):
    """Programme jour par jour d'une campagne, à partir des matières et des supports demandés."""
    revs = db.revisions_campagne(campagne["id"])
    matieres = {}
    for r in revs:
        m = matieres.setdefault(r["matiere"] or "?", {
            "niveau": r["niveau"] or "?", "matiere": r["matiere"] or "?", "sujet": "", "supports": [], "cours": []})
        m["supports"].append(r["genre"])
        m.setdefault("ids", []).extend(i for i in r["doc_ids"] if i not in m["ids"])
        sujet = r["sujet"].split(" : ", 1)[-1]
        if sujet != r["matiere"]:
            m["sujet"] = sujet
    for m in matieres.values():
        docs = db.documents(m.pop("ids"))
        niveaux = sorted({d.get("niveau") for d in docs if d.get("niveau")},
                         key=lambda n: NIVEAUX.index(n) if n in NIVEAUX else -1)
        if len(niveaux) > 1:  # même matière sur plusieurs années : le programme voit l'année de chaque cours
            m["niveau"] = ", ".join(niveaux)
            m["cours"] = [f"[{d.get('niveau')}] {d['titre'] or d['nom']}" for d in docs]
        else:
            m["cours"] = [d["titre"] or d["nom"] for d in docs]
    activite(f"Programme de « {campagne['titre']} »")
    try:
        programme = revision.generer_programme(campagne, list(matieres.values()),
                                               db.evenements_entre(campagne["debut"], campagne["fin"]),
                                               db.pieces_jointes(campagne_id=campagne["id"], contenu=True))
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


def a_traiter(kd: KDrive, items: list[dict] | None = None, sans_claude: bool = False) -> list[dict]:
    """Fichiers à analyser : gardés par le tri, restés 'nouveau' (limite, panne), ou perdus hors de l'inbox.

    Les documents des profs passent en premier ; un fichier qui a déjà fait planter Claude, en dernier.
    Un fichier suspendu sur la page Activité est sauté ; `moteur` : local ou claude, si choisi à la main.
    `sans_claude` : Claude est indisponible, rien n'attend la passe IA sur les noms de dossiers, et un fichier
    déjà illisible pour le modèle local attend Claude.
    """
    if items is None:
        items = list(parcourir(kd, INBOX_ID, []))
    ids = [i["id"] for i in items]
    decisions, docs = db.import_connus(ids), db.statuts_kdrive(ids)
    suspendus, moteurs = db.import_choix(ids)
    en_attente = set() if sans_claude else set(db.lots_a_comprendre())  # l'IA lit d'abord les noms de dossiers
    retenus = []
    for item in items:
        if item["id"] in suspendus:
            continue
        item = {**item, "moteur": moteurs.get(item["id"])}
        doc = docs.get(item["id"])
        if doc is not None:
            # déjà illisible sans Claude : on n'essaie pas de nouveau en local, il attend Claude
            if doc["statut"] == "nouveau" and not (sans_claude and doc.get("relire")):
                retenus.append((doc["essais"], item))
        elif decisions.get(item["id"]) == "analyser" and item["type"] == "file" and lot_de(item) not in en_attente:
            retenus.append((0, item))
    if len(retenus) < LOT:
        retenus += [(doc["essais"], {**item, "moteur": None}) for doc, item in orphelins(kd, ids)
                    if not (sans_claude and doc.get("relire"))]
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
            essai_local = local.essayer_json if reglages_analyse()["mode"] != "jamais" else None
            resume, compris = agent.interpreter_arborescence(db.import_dossiers(lot), db.matieres(), essai_local)
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
        except KDriveIndisponible:
            raise   # panne : ton choix est gardé, il sera appliqué quand kDrive répondra
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
    except KDriveIndisponible:
        raise   # panne passagère : la fusion est retentée au tour suivant, pas abandonnée
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


def delai(encore_du_travail: bool, calme_depuis: float) -> int:
    """Secondes avant le prochain tour : tout de suite s'il reste du travail, plus espacé quand rien ne bouge."""
    if encore_du_travail:
        return 5
    return INTERVALLE_CALME if calme_depuis >= CALME else INTERVALLE


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
    except Interrompu:
        log(f"{nom} : arrêté depuis le site, les fichiers restent dans la file")
    except Exception as e:
        log(f"erreur ({nom}) : {e.__class__.__name__} : {e}")
    finally:
        activite(None)
    return None


def sonder_claude():
    """Bouton « Réessayer maintenant » du site, pendant une pause de Claude (limite, panne).

    Un appel minuscule (agent.sonder) : s'il répond, la pause est levée et la file repart tout de
    suite ; sinon la pause continue (l'heure de reprise annoncée est mise à jour). Rien n'est acheté :
    `claude -p` n'utilise que le jeton du forfait (voir agent.ENV_FORFAIT).
    """
    if not db.etat_consommer("claude_sonde"):
        return
    maintenant = time.time()
    if SONDE["le"] and maintenant - dt.datetime.fromisoformat(SONDE["le"]).timestamp() < SONDE_ECART:
        return
    if CLAUDE.etat == "ok":
        SONDE.update(le=_iso(maintenant), resultat="ok", message="Claude était déjà disponible.")
        return
    SONDE.update(le=_iso(maintenant), resultat="en_cours", message="")
    activite("Test de Claude demandé depuis le site")
    try:
        agent.sonder()
    except LimiteAtteinte as e:
        SONDE.update(resultat="limite", message=str(e)[:300])
        if not e.reprise and CLAUDE.etat == "limite" and CLAUDE.jusqu_a > maintenant:
            CLAUDE.echec("limite", str(e), CLAUDE.jusqu_a)  # pas d'heure annoncée : on garde la précédente
            return
        raise  # etape : la pause continue jusqu'à la nouvelle heure de reprise
    except ClaudeIndisponible as e:
        SONDE.update(resultat="panne", message=str(e)[:300])
        raise
    except Exception as e:
        SONDE.update(resultat="erreur", message=str(e)[:300])
        raise
    SONDE.update(resultat="ok", message="")
    CLAUDE.ok()
    log("Claude répond de nouveau (test demandé depuis le site) : la file repart")


def en_parallele(items: list, faire, libelle, fils: int = PARALLELE, continuer=None, freiner: bool = False):
    """Applique `faire` à chaque élément, `fils` à la fois.

    `freiner` : travail sur le processeur de la VM ; avant chaque élément, on attend que l'occupation
    moyenne repasse sous la limite (ressources.respirer) : des pics, pas une VM à fond en permanence.

    Le premier qui tombe sur la limite du forfait, une panne de Claude ou de kDrive arrête la
    distribution : ceux déjà lancés finissent, les autres restent pour le tour suivant, puis
    l'erreur est relevée pour ouvrir le disjoncteur (etape).
    """
    echec = []
    continuer = continuer or CLAUDE.disponible

    def un(item):
        if ARRET.is_set() or echec or not continuer():
            return
        if freiner and ressources.BUDGET.freine:
            activite(f"Pause : {ressources.BUDGET.raison()}")
            ressources.respirer(lambda: ARRET.is_set() or STOP.is_set())
            if ARRET.is_set() or not continuer():
                return
        with EN_COURS_VERROU:  # partagé : le modèle local et Claude travaillent en même temps
            EN_COURS.append(libelle(item))
            ETAT["en_cours"] = list(EN_COURS)
            activite(EN_COURS[0] + (f" (et {len(EN_COURS) - 1} autre(s) en même temps)" if len(EN_COURS) > 1 else ""))
        try:
            faire(item)
        except (LimiteAtteinte, ClaudeIndisponible, KDriveIndisponible, Interrompu) as e:
            echec.append(e)
        except Exception as e:  # un fichier qui plante ne doit pas arrêter les autres
            log(f"erreur ({libelle(item)}) : {e.__class__.__name__} : {e}")
        finally:
            with EN_COURS_VERROU:
                EN_COURS.remove(libelle(item))
                ETAT["en_cours"] = list(EN_COURS)

    with ThreadPoolExecutor(fils, thread_name_prefix="ia") as ex:
        list(ex.map(un, items))
    if echec:
        raise echec[0]


LOT_LOCAL = 2 * local.PARALLELE


def repartir(items: list[dict], mode: str, claude_ok: bool) -> tuple[list[dict], list[dict]]:
    """Qui lit quoi ce tour-ci : (pour le modèle local, pour Claude). Le reste attend le tour suivant.

    « local d'abord » : le modèle local prend tout ce qu'il peut ; Claude ne prend que le surplus, quand la
    VM est saturée ou quand la file ferait attendre un document plus de LOCAL_ATTENTE_MAX_MIN minutes.
    """
    choisis_local = [i for i in items if i.get("moteur") == "local"]
    choisis_claude = [i for i in items if i.get("moteur") == "claude"] if claude_ok else []
    libres = [i for i in items if not i.get("moteur")]
    if ressources.locaux_eteints():   # même ceux confiés au modèle local partent chez Claude
        return [], (choisis_local + choisis_claude + libres)[:LOT] if claude_ok else []
    if mode == "jamais":
        return choisis_local[:LOT_LOCAL], (choisis_claude + libres)[:LOT] if claude_ok else []
    if mode == "toujours" or (mode == "auto" and not claude_ok) or (mode == "local" and not claude_ok):
        return (choisis_local + libres)[:LOT_LOCAL], choisis_claude[:LOT]
    if mode == "auto":
        return choisis_local[:LOT_LOCAL], (choisis_claude + libres)[:LOT]
    # local d'abord, Claude disponible
    sature = ressources.sature()
    place = 0 if sature else max(LOT_LOCAL - len(choisis_local), 0)
    pour_local = choisis_local[:LOT_LOCAL] + libres[:place]
    reste = libres[place:]
    attente = ressources.attente_min(len(choisis_local) + len(libres), local.PARALLELE)
    debordement = reste if (sature or attente > ressources.ATTENTE_MAX_MIN) else []
    if debordement:
        log(f"modèle local : {sature or f'environ {attente:.0f} min de file'} → {len(debordement[:LOT])} "
            f"document(s) confié(s) à Claude")
    return pour_local, (choisis_claude + debordement)[:LOT]


def analyser_inbox(kd: KDrive, items: list[dict], mode: str | None = None, claude_ok: bool | None = None):
    """Analyse un lot : le modèle local et Claude travaillent en même temps, chacun sur sa part (repartir)."""
    mode = mode or reglages_analyse()["mode"]
    claude_ok = CLAUDE.disponible() if claude_ok is None else claude_ok
    pour_local, pour_claude = repartir(items, mode, claude_ok)
    matieres, dossiers = db.matieres(), {}

    def faire(item):
        log(f"analyse : {'/'.join(item['dossiers'] + [item['name']])}")
        traiter(kd, {**item, "local": False}, matieres, dossiers)

    def avec_claude():
        en_parallele(pour_claude, faire, lambda item: f"Analyse de « {item['name']} »", PARALLELE + 1,
                     continuer=lambda: CLAUDE.disponible() and not PAUSE.is_set() and not STOP.is_set())

    erreurs = []
    fil = None
    if pour_claude:
        def fil_claude():
            try:
                avec_claude()
            except Exception as e:
                erreurs.append(e)
        fil = threading.Thread(target=fil_claude, name="analyses-claude")
        fil.start()
    if pour_local:
        analyser_sans_claude(kd, pour_local, matieres, dossiers)
    if fil:
        fil.join()
    ETAT["reste"] = len(items) > len(pour_local) + len(pour_claude)  # encore du travail : pas d'attente d'1 min
    if erreurs:
        raise erreurs[0]


def analyser_sans_claude(kd: KDrive, items: list[dict], matieres: list[str] | None = None,
                         dossiers: dict | None = None):
    """Lecture sur la VM (local.py) : OCR + modèle local, Claude n'aidant que si besoin. Peu à la fois."""
    matieres = db.matieres() if matieres is None else matieres
    dossiers = {} if dossiers is None else dossiers

    def faire(item):
        log(f"analyse locale : {'/'.join(item['dossiers'] + [item['name']])}")
        traiter(kd, {**item, "local": True}, matieres, dossiers)

    en_parallele(items, faire, lambda item: f"Analyse locale de « {item['name']} »", local.PARALLELE,
                 continuer=lambda: not PAUSE.is_set() and not STOP.is_set(), freiner=True)


def relire_avec_claude(kd: KDrive):
    """Documents analysés sans Claude : relus par Claude quand il est là et que la file est vide."""
    items = []
    for doc in db.a_relire(PARALLELE * 2):
        try:
            info = kd.stat(doc["kdrive_id"])
        except httpx.HTTPStatusError:
            db.marquer_relire([doc["id"]], False)
            continue
        items.append({"id": doc["kdrive_id"], "name": info.get("name") or doc["nom"], "type": "file", "relire": True,
                      "dossiers": (doc["origine"] or "").split("/") if doc.get("origine") else []})
    if items:
        matieres, dossiers = db.matieres(), {}
        en_parallele(items, lambda item: traiter(kd, item, matieres, dossiers),
                     lambda item: f"Relecture par Claude de « {item['name']} »", PARALLELE,
                     continuer=lambda: CLAUDE.disponible() and not PAUSE.is_set() and not STOP.is_set())


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
    en_parallele(a_reclasser, lambda doc: reclasser(doc, matieres),
                 lambda doc: f"Reclassement de « {doc['titre'] or doc['nom']} »")


def vider_corbeille(kd: KDrive):
    """Fichiers des documents supprimés sur le site : à la corbeille kDrive (déjà partis : on oublie)."""
    for kdrive_id in db.corbeille():
        if ARRET.is_set():
            return
        try:
            kd.delete(kdrive_id)
        except httpx.HTTPStatusError as e:   # panne (5xx) : KDriveIndisponible, réessayé au tour suivant
            if e.response.status_code != 404:   # refus durable (droits…) : inutile de réessayer chaque minute
                log(f"corbeille kDrive : fichier {kdrive_id} non supprimé ({e.response.status_code}), abandonné")
        db.corbeille_faite(kdrive_id)


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


def indexer_si_calme():
    """Rien d'urgent : on indexe les passages (recherche, chat) ; sur la VM, sans Claude ni kDrive."""
    if not ETAT["reste"] and not ARRET.is_set():
        n = etape("index des documents", lambda: (index.indexer(20, arrete), index.vectoriser(128, arrete)))
        # Lots pleins : il reste de l'index à faire, le worker ne s'endort pas 5 min entre deux tours
        ETAT["index_en_retard"] = bool(n) and (n[0] >= 20 or n[1] >= 128)


def tour(kd: KDrive, dernier_nettoyage: float) -> float:
    """Un passage du worker. Renvoie l'heure du dernier nettoyage de l'inbox."""
    # Ce que tu attends sur le site passe d'abord : révisions, puis reclassements, puis les scans.
    if CLAUDE.disponible():
        etape("révisions", revisions_en_attente, kd)
    if (CLAUDE.disponible() or not local.disponible()) and not ARRET.is_set() and not PAUSE.is_set():
        etape("reclassements", reclasser_en_attente)
    if not KDRIVE.disponible() or ARRET.is_set():
        indexer_si_calme()   # l'index ne dépend pas de kDrive : il avance même pendant une panne
        return dernier_nettoyage
    etape("rangement", ranger_en_attente, kd)
    etape("PDF des révisions", pdf_manquants, kd)
    etape("corbeille kDrive", vider_corbeille, kd)
    inbox = etape("inbox", lire_inbox, kd)
    if inbox is not None:
        ETAT["inbox"] = frozenset(i["id"] for i in inbox)
    if (CLAUDE.disponible() or not local.disponible()) and not ARRET.is_set():
        etape("organisation des imports", comprendre_en_attente)
    ETAT["reste"] = False
    r = reglages_analyse()
    claude_ok = CLAUDE.disponible() and r["mode"] != "toujours"
    if inbox is not None and KDRIVE.disponible() and not ARRET.is_set() and not PAUSE.is_set():
        if claude_ok or r["mode"] != "jamais":  # sans Claude : OCR et modèle local sur la VM
            etape("analyses", lambda: analyser_inbox(kd, a_traiter(kd, inbox, sans_claude=not claude_ok),
                                                     r["mode"], claude_ok))
        if claude_ok and not ETAT["reste"] and not ARRET.is_set():
            etape("relectures par Claude", relire_avec_claude, kd)
    indexer_si_calme()
    if time.monotonic() - dernier_nettoyage > NETTOYAGE and KDRIVE.disponible() and not ARRET.is_set():
        etape("nettoyage", nettoyer_inbox, kd)
        return time.monotonic()
    return dernier_nettoyage


def actualites_boucle():
    """Flux RSS relevés toutes les 30 min et triés par l'IA, à côté du worker (ne retarde pas les analyses)."""
    try:
        actualites.initialiser()
    except Exception as e:
        log(f"actualités : sélection de départ impossible : {e}")
    while not ARRET.is_set():
        etape("actualités", actualites.tour, CLAUDE.disponible)
        ARRET.wait(30)


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
    threading.Thread(target=actualites_boucle, daemon=True, name="actualites").start()
    threading.Thread(target=battement, daemon=True, name="battement").start()
    threading.Thread(target=surveiller, daemon=True, name="surveillance").start()
    prochaine_synchro, dernier_nettoyage = 0.0, float("-inf")
    dernier_changement, inbox_vue = time.monotonic(), None
    while not ARRET.is_set():
        etape("test de Claude", sonder_claude)
        prochaine_synchro = synchro_pronote(prochaine_synchro)
        dernier_nettoyage = tour(kd, dernier_nettoyage)
        if ETAT["reste"] or ETAT.get("index_en_retard") or ETAT.get("inbox") != inbox_vue:
            dernier_changement, inbox_vue = time.monotonic(), ETAT.get("inbox")
        attendre(delai(ETAT["reste"] and CLAUDE.disponible(), time.monotonic() - dernier_changement))
    publier()
    log("worker arrêté proprement")


if __name__ == "__main__":
    main()
