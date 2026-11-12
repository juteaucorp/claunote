"""Notifications push sur le téléphone, sans compte développeur payant (Web Push).

Le serveur signe chaque envoi avec sa clé VAPID (créée au premier démarrage, gardée en base)
et chiffre le message pour l'appareil ; Apple (ou Google) ne fait que le transmettre, sans
pouvoir le lire. Le téléphone reçoit donc les notifications même hors VPN : seule la VM a
besoin d'Internet pour les envoyer.

Ce qui part (une file en base, une clé par notification tirée du contenu, jamais d'un id Pronote ;
en plus, même titre + même texte en moins de 20 h = pas renvoyé, et plus de 3 d'un coup = un résumé) :
- une fois par jour à l'heure choisie, le rappel le plus utile : évaluation proche sans fiche,
  flashcards à revoir, fiche à relire, ou nouveaux cours sans révision ;
- le matin d'une évaluation, un dernier rappel ;
- tout de suite : révision prête, nouveau contrôle annoncé sur Pronote, documents des profs,
  actualités importantes des sujets suivis (actualites.py).
Rien ne part pendant les heures de silence : la file attend le matin.
"""
import datetime as dt
import json
import os
import time
from urllib.parse import urlencode

from py_vapid import Vapid02 as Vapid
from py_vapid.utils import b64urlencode
from cryptography.hazmat.primitives import serialization
from pywebpush import WebPushException, webpush

from claunote import db, pronote, revision

PARIS = pronote.PARIS
CONTACT = os.environ.get("CLAUNOTE_PUSH_CONTACT") or "mailto:claunote@example.com"
DUREE_VIE = 12 * 3600   # Apple/Google gardent le message 12 h si le téléphone est éteint
INTERVALLE = 30         # secondes entre deux tours de la boucle
MAX_PAR_TOUR = 3        # au-delà, les notifications du tour partent en un seul résumé
DEFAUT = {"heure": "18:00", "silence_debut": "22:00", "silence_fin": "07:30",
          "rappel": True, "pronote": True, "revisions": True, "actualites": True, "securite": True}
PRETE = {"fiche": "Fiche prête", "quiz": "Quiz prêt", "cartes": "Flashcards prêtes", "plan": "Plan de révision prêt",
         "examen": "Épreuve blanche prête"}
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]


def log(msg: str):
    print(f"notifications : {msg}", flush=True)


# --- Clé VAPID et réglages ----------------------------------------------------

def vapid() -> Vapid:
    pem = db.etat_valeur("vapid_prive")
    if not pem:
        v = Vapid()
        v.generate_keys()
        pem = db.etat_initialiser("vapid_prive", v.private_pem().decode())
    return Vapid.from_pem(pem.encode())


def cle_publique() -> str:
    """applicationServerKey donnée au navigateur pour s'abonner."""
    brut = vapid().public_key.public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    return b64urlencode(brut)


def preferences() -> dict:
    try:
        return {**DEFAUT, **json.loads(db.etat_valeur("notifications") or "{}")}
    except ValueError:
        return dict(DEFAUT)


def enregistrer_preferences(p: dict):
    db.etat_ecrire("notifications", json.dumps({k: p[k] for k in DEFAUT if k in p}))


def _heure(texte: str) -> dt.time:
    try:
        return dt.time.fromisoformat(texte)
    except ValueError:
        return dt.time(18)


def en_silence(maintenant: dt.datetime, p: dict) -> bool:
    debut, fin, t = _heure(p["silence_debut"]), _heure(p["silence_fin"]), maintenant.time()
    if debut == fin:
        return False
    return debut <= t or t < fin if debut > fin else debut <= t < fin


# --- Envoi ------------------------------------------------------------------

def badge() -> int:
    """Pastille sur l'icône de l'appli : flashcards du jour + évaluations de la semaine."""
    return db.nb_cartes_a_revoir() + db.nb_evaluations_proches()


def envoyer(abo: dict, charge: dict) -> str | None:
    """None si c'est parti, « expire » si l'appareil s'est désabonné, sinon l'erreur."""
    try:
        webpush({"endpoint": abo["endpoint"], "keys": {"p256dh": abo["p256dh"], "auth": abo["auth"]}},
                json.dumps(charge), vapid_private_key=vapid(), vapid_claims={"sub": CONTACT},
                ttl=DUREE_VIE, timeout=15, headers={"Urgency": "normal"})
        return None
    except WebPushException as e:
        code = e.response.status_code if e.response is not None else None
        return "expire" if code in (404, 410) else f"{code or ''} {e.message}".strip()[:300]
    except Exception as e:
        return str(e)[:300]


def envoyer_a_tous(titre: str, corps: str, url: str, tag: str = "") -> tuple[int, list[str]]:
    """Envoie sur chaque appareil abonné ; oublie ceux qui ne répondent plus."""
    charge = {"titre": titre, "corps": corps, "url": url, "tag": tag, "badge": badge()}
    envoyes, erreurs = 0, []
    for abo in db.abonnements():
        erreur = envoyer(abo, charge)
        if erreur == "expire":
            db.retirer_abonnement(abo["endpoint"])
            log(f"appareil désabonné retiré : {abo['appareil'] or abo['endpoint'][:40]}")
        elif erreur:
            erreurs.append(erreur)
        else:
            envoyes += 1
    return envoyes, erreurs


def vider_file(maintenant: dt.datetime):
    p = preferences()
    if en_silence(maintenant, p):
        return
    file = db.reserver_notifications()
    if len(file) > MAX_PAR_TOUR:  # une rafale (synchro, import…) = une seule notification résumée
        envoyes, erreurs = envoyer_a_tous(
            f"{len(file)} nouveautés sur Claunote", " · ".join(n["titre"] for n in file)[:300], "/notifications",
            "resume")
        statut, erreur = ("envoye", None) if envoyes or not erreurs else ("erreur", " ; ".join(erreurs))
        for n in file:
            db.maj_notification(n["id"], statut, erreur)
        log(f"{len(file)} notifications regroupées en une : {statut}")
        return
    for n in file:
        envoyes, erreurs = envoyer_a_tous(n["titre"], n["corps"], n["url"], n["cle"])
        if envoyes or not erreurs:
            db.maj_notification(n["id"], "envoye")
            log(f"envoyée : {n['titre']}")
        else:
            db.maj_notification(n["id"], "erreur", " ; ".join(erreurs))
            log(f"échec : {n['titre']} : {erreurs[0]}")


# --- Ce qu'on propose ---------------------------------------------------------

def _quand(jours: int) -> str:
    return "aujourd'hui" if jours <= 0 else "demain" if jours == 1 else f"dans {jours} jours"


def _cartes(n: int) -> str:
    return f"{n} flashcard{'s' if n > 1 else ''} à revoir (~{max(1, round(n * 12 / 60))} min)"


def rappel_du_jour(aujourd_hui: dt.date) -> tuple[str, str, str] | None:
    """Le rappel quotidien le plus utile : (titre, texte, page à ouvrir), ou None s'il n'y a rien."""
    evals = [(e, (e["le"].astimezone(PARIS).date() - aujourd_hui).days)
             for e in db.evenements_a_venir("evaluation", 8)]
    dues = db.nb_cartes_a_revoir()
    sans_fiche = [(e, j) for e, j in evals if not e["revision_id"]]
    if sans_fiche:
        e, j = sans_fiche[0]
        suite = f" Et {_cartes(dues)}." if dues else ""
        return (f"Contrôle de {e['matiere']} {_quand(j)}",
                f"Pas encore de fiche : touche pour que je la prépare avec tes cours.{suite}", "/")
    campagne = db.campagne_du_jour(aujourd_hui)
    if campagne:
        jour = next((j for j in revision.jours_programme(campagne["programme"], campagne["faits"])
                     if j["date"] == aujourd_hui), None)
        reste = [t for t in jour["taches"] if not t["fait"]] if jour else []
        if reste:
            quoi = " · ".join(f"{t['matiere']} ({t['duree']} min)" for t in reste[:3])
            suite = f" Et {_cartes(dues)}." if dues else ""
            return f"{campagne['titre']} : programme du jour", quoi + "." + suite, f"/campagne/{campagne['id']}"
    proches = [(e, j) for e, j in evals if j <= 3 and e["revision_statut"] == "pret"]
    if dues:
        suite = f" · {proches[0][0]['matiere']} {_quand(proches[0][1])} : ta fiche est prête." if proches else ""
        return "Réviser maintenant", _cartes(dues) + suite, "/cartes/session"
    if proches:
        e, j = proches[0]
        return (f"Contrôle de {e['matiere']} {_quand(j)}", "Relis ta fiche, 5 minutes suffisent.",
                f"/revision/{e['revision_id']}")
    nouveaux = db.nouveaux_cours_sans_revision()
    if nouveaux:
        m, n = nouveaux["matiere"], nouveaux["n"]
        return (f"Nouveaux cours en {m}",
                f"{n} cours cette semaine : je t'en fais des flashcards pour les retenir ?",
                "/reviser?" + urlencode({"matiere": m, "genre": "cartes"}))
    return None


def planifier(maintenant: dt.datetime):
    """Ajoute à la file les rappels du jour quand c'est l'heure (une seule fois par jour)."""
    p = preferences()
    jour = maintenant.date()
    if p["rappel"] and maintenant.time() >= _heure(p["heure"]) and not db.notification_connue(f"rappel:{jour}"):
        r = rappel_du_jour(jour)
        if r:
            db.notifier(f"rappel:{jour}", *r)
        else:
            db.notifier(f"rappel:{jour}", "Rien à proposer aujourd'hui", statut="ignore")
    if p["rappel"] and maintenant.time() >= _heure(p["silence_fin"]):
        for e in db.evenements_a_venir("evaluation", 1):
            if e["le"].astimezone(PARIS).date() == jour and e["le"] > maintenant:
                url = f"/revision/{e['revision_id']}" if e["revision_id"] else "/agenda"
                heure = e["le"].astimezone(PARIS)
                db.notifier(f"jour:{e['matiere']}:{heure:%Y-%m-%dT%H:%M}", f"{e['matiere']} aujourd'hui à {heure:%H:%M}",
                            "Un dernier coup d'œil à ta fiche ?" if e["revision_id"] else e["titre"], url)


def revision_prete(rev: dict):
    if preferences()["revisions"] and db.abonnements():
        db.notifier(f"revision:{rev['id']}", PRETE.get(rev["genre"], "Révision prête"), rev["sujet"],
                    f"/revision/{rev['id']}")


def campagne_prete(campagne_id: int):
    """Une seule notification, quand le programme et tous les supports de la campagne sont générés."""
    campagne = db.campagne(campagne_id)
    revs = db.revisions_campagne(campagne_id)
    if not campagne or campagne["statut"] == "en_attente" or any(r["statut"] == "en_attente" for r in revs):
        return
    if preferences()["revisions"] and db.abonnements():
        erreurs = sum(r["statut"] == "erreur" for r in revs)
        db.notifier(f"campagne:{campagne_id}", f"{campagne['titre']} est prête",
                    f"{len(revs) - erreurs} supports et ton programme jour par jour"
                    + (f" ({erreurs} en erreur, à relancer)" if erreurs else ""), f"/campagne/{campagne_id}")


def apres_synchro_pronote(documents_deposes: int = 0):
    """Nouveaux contrôles et documents des profs vus pendant la synchro Pronote."""
    actif = preferences()["pronote"] and bool(db.abonnements())
    # Clés d'avant les ids stables : au premier passage, les contrôles déjà connus sont notés sans être ré-annoncés
    if not db.etat_valeur("notifications_cles_stables"):
        actif = False
        db.etat_ecrire("notifications_cles_stables", "1")
    for e in db.evenements_a_venir("evaluation", 60):
        le = e["le"].astimezone(PARIS)
        # Sans appareil abonné, on note seulement les contrôles déjà connus pour ne pas tout annoncer d'un coup.
        # Clé tirée du contenu (matière + date), jamais d'un id Pronote : ceux-là changent à chaque connexion.
        db.notifier(f"nouvelle:{e['matiere']}:{le:%Y-%m-%dT%H:%M}", f"Nouveau contrôle : {e['matiere']}",
                    f"{JOURS[le.weekday()]} {le:%d/%m} · {e['titre']}", "/agenda",
                    statut="attente" if actif else "ignore")
    if actif and documents_deposes:
        db.notifier(f"documents:{int(time.time())}", "Nouveaux documents des profs",
                    f"{documents_deposes} document{'s' if documents_deposes > 1 else ''} récupéré"
                    f"{'s' if documents_deposes > 1 else ''} sur Pronote, rangé{'s' if documents_deposes > 1 else ''} "
                    "dans tes cours d'ici quelques minutes.", "/agenda")


def import_a_confirmer(lot: str, inutiles: int):
    """Un dossier importé attend ton feu vert : fichiers à supprimer ou scans à découper."""
    if db.abonnements():
        corps = (f"{inutiles} fichier{'s' if inutiles > 1 else ''} inutile{'s' if inutiles > 1 else ''} "
                 "(code, config…) à supprimer ?" if inutiles else "Des scans à regrouper : dis-moi où couper.")
        db.notifier(f"import:{lot}:{int(time.time())}", f"Import « {lot or 'inbox'} » trié", corps, "/import")


def document_personnel(nom: str, libelle: str, supprime: bool, pages: str | None = None):
    """Garde-fou : un document personnel a été retiré avant l'IA (corbeille) ou attend ton avis."""
    if db.abonnements():
        quoi = f"Pages {pages} de « {nom} »" if pages else f"« {nom} »"
        corps = (f"{quoi} ({libelle}) : mis à la corbeille kDrive sans être lu par l'IA (récupérable 30 jours)."
                 if supprime else f"{quoi} ressemble à un document personnel ({libelle}) : à supprimer ou à analyser ?")
        db.notifier(f"sensible:{nom}:{pages or ''}:{int(time.time())}",
                    "Document personnel supprimé" if supprime else "Document personnel ?", corps, "/import")


def decoupage_fini(nom: str, documents: int):
    if db.abonnements():
        db.notifier(f"decoupage:{nom}:{int(time.time())}", "Gros scan découpé",
                    f"« {nom} » → {documents} document{'s' if documents > 1 else ''}, analysés un par un.", "/import")


def tour(maintenant: dt.datetime | None = None):
    maintenant = maintenant or dt.datetime.now(PARIS)
    if not db.abonnements():
        return
    planifier(maintenant)
    vider_file(maintenant)


def boucle():
    """Tourne à côté du worker : les analyses longues ne retardent pas les rappels."""
    while True:
        try:
            tour()
        except Exception as e:
            log(f"erreur : {e}")
        time.sleep(INTERVALLE)
