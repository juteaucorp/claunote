"""Notifications push sur le téléphone, sans compte développeur payant (Web Push).

Le serveur signe chaque envoi avec sa clé VAPID (créée au premier démarrage, gardée en base)
et chiffre le message pour l'appareil ; Apple (ou Google) ne fait que le transmettre, sans
pouvoir le lire. Le téléphone reçoit donc les notifications même hors VPN : seule la VM a
besoin d'Internet pour les envoyer.

Ce qui part (une file en base, une clé par notification tirée du contenu, jamais d'un id Pronote ;
en plus, même titre + même texte en moins de 20 h = pas renvoyé, et plus de 3 d'un coup = un résumé) :
- une fois par jour à l'heure choisie, le rappel le plus utile : évaluation proche sans fiche,
  programme de campagne, de temps en temps la veille d'un cours (emploi du temps Pronote) ses flashcards
  ou sa fiche, flashcards à revoir, fiche à relire, ou nouveaux cours sans révision ;
- le matin d'une évaluation, un dernier rappel ;
- le dimanche, s'il y a du ménage à faire : révisions, flashcards et campagnes qui ne servent plus,
  à archiver (archivage.py ; jamais archivé sans toi) ;
- tout de suite : révision prête, nouveau contrôle annoncé sur Pronote, documents des profs,
  cours de la semaine modifié (salle, prof, annulé…) et nouvelle note (alertes.py, vérifié toutes les 15 min
  en journée), actualités importantes des sujets suivis (actualites.py).
Rien ne part pendant les heures de silence : la file attend le matin.
Calé sur l'emploi du temps Pronote (emploi_du_temps.py) : rien ne sonne pendant un cours ni au travail
(heures « Dispensé » en alternance), la file attend la pause, midi ou la fin de journée ; le rappel du jour
part à la sortie des cours. Seules les urgentes passent quand même : le matin d'un contrôle, un cours modifié,
les alertes graves des actualités, le disque plein.
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

from claunote import archivage, db, emploi_du_temps, profil, pronote, revision

PARIS = pronote.PARIS
CONTACT = os.environ.get("CLAUNOTE_PUSH_CONTACT") or "mailto:claunote@example.com"
DUREE_VIE = 12 * 3600   # Apple/Google gardent le message 12 h si le téléphone est éteint
# « high » : Apple livre tout de suite. En « normal », il passe en priorité basse (5) : l'iPhone en veille
# garde les messages et les sort d'un coup au prochain réveil réseau (un « Envoyer un test », par ex.).
# Claunote n'envoie déjà que l'important, donc tout part en « high ».
URGENCE = "high"
INTERVALLE = 30         # secondes entre deux tours de la boucle
MAX_PAR_TOUR = 3        # au-delà, les notifications du tour partent en un seul résumé
DEFAUT = {"heure": "18:00", "silence_debut": "22:00", "silence_fin": "07:30",
          "rappel": True, "pronote": True, "revisions": True, "actualites": True, "securite": True,
          "edt": True}
# Partent même en cours ou au travail : elles n'ont plus de sens plus tard
URGENTES = ("jour:", "edt:", "actu-alerte:", "disque:")   # edt: un cours de la semaine modifié (alertes.py)
PRETE = {"fiche": "Fiche prête", "quiz": "Quiz prêt", "cartes": "Flashcards prêtes", "plan": "Plan de révision prêt",
         "examen": "Épreuve blanche prête"}
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
JOURS_LONGS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


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


_COMPTE = object()


def _cle_preferences(compte_id) -> str:
    """Réglages de chaque compte. Ceux de l'admin d'origine (et du worker, qui ne travaille pour personne)
    gardent leur ancienne clé ; un invité ne peut plus changer les heures de silence de l'admin."""
    if compte_id is _COMPTE:
        compte_id = db.compte_actuel()
    return "notifications" if compte_id in (None, 1) else f"notifications:{int(compte_id)}"


def preferences(compte_id=_COMPTE) -> dict:
    """Réglages du compte de la requête (ou de `compte_id` ; None : l'admin d'origine)."""
    try:
        return {**DEFAUT, **json.loads(db.etat_valeur(_cle_preferences(compte_id)) or "{}")}
    except ValueError:
        return dict(DEFAUT)


def enregistrer_preferences(p: dict, compte_id=_COMPTE):
    db.etat_ecrire(_cle_preferences(compte_id), json.dumps({k: p[k] for k in DEFAUT if k in p}))


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
    return db.nb_cartes_a_revoir(nouvelles_max=profil.nouvelles_max()) + db.nb_evaluations_proches()


def envoyer(abo: dict, charge: dict) -> str | None:
    """None si c'est parti, « expire » si l'appareil s'est désabonné, sinon l'erreur."""
    try:
        webpush({"endpoint": abo["endpoint"], "keys": {"p256dh": abo["p256dh"], "auth": abo["auth"]}},
                json.dumps(charge), vapid_private_key=vapid(), vapid_claims={"sub": CONTACT},
                ttl=DUREE_VIE, timeout=15, headers={"Urgency": URGENCE})
        return None
    except WebPushException as e:
        code = e.response.status_code if e.response is not None else None
        return "expire" if code in (404, 410) else f"{code or ''} {e.message}".strip()[:300]
    except Exception as e:
        return str(e)[:300]


ICI = object()


def envoyer_a_tous(titre: str, corps: str, url: str, tag: str = "", compte_id=ICI) -> tuple[int, list[str]]:
    """Envoie sur chaque appareil abonné du compte ; oublie ceux qui ne répondent plus.

    `compte_id` : None pour les admins (alertes de sécurité, Pronote) ; par défaut, le compte de la requête
    (ou les admins pour le worker, qui ne travaille pour personne en particulier)."""
    if compte_id is ICI:
        compte_id = db.compte_actuel()
    try:
        with db.pour_compte(compte_id or 1):   # sa pastille à lui : ses cartes et ses contrôles
            pastille = badge()
    except Exception:
        pastille = 0
    charge = {"titre": titre, "corps": corps, "url": url, "tag": tag, "badge": pastille}
    envoyes, erreurs = 0, []
    for abo in db.abonnements_de(compte_id):
        erreur = envoyer(abo, charge)
        if erreur == "expire":
            db.retirer_abonnement(abo["endpoint"])
            log(f"appareil désabonné retiré : {abo['appareil'] or abo['endpoint'][:40]}")
        elif erreur:
            erreurs.append(erreur)
        else:
            envoyes += 1
    return envoyes, erreurs


def _occupe(maintenant: dt.datetime, compte_id, p: dict) -> bool:
    """En cours ou au travail d'après SON emploi du temps Pronote (s'il a relié Pronote et gardé le réglage)."""
    if not p.get("edt", True):
        return False
    occupe, _ = emploi_du_temps.occupation(maintenant, emploi_du_temps._charger(compte_id))
    return bool(occupe)


def vider_file(maintenant: dt.datetime):
    """Chaque compte a ses heures de silence et son emploi du temps (son Pronote à lui).

    Avant, le silence et les cours de l'admin retenaient aussi les notifications des invités."""
    par_compte: dict = {}
    for n in db.reserver_notifications():   # chaque compte reçoit les siennes (None : les admins)
        par_compte.setdefault(n.get("compte_id"), []).append(n)
    for compte_id, file in par_compte.items():
        p = preferences(None if compte_id in (None, 1) else compte_id)
        if en_silence(maintenant, p):
            db.remettre_en_attente([n["id"] for n in file])   # ses heures de silence à lui : elles attendent
            continue
        if _occupe(maintenant, compte_id, p):   # en cours ou au travail : seules les urgentes partent
            db.remettre_en_attente([n["id"] for n in file if not urgente(n["cle"])])
            file = [n for n in file if urgente(n["cle"])]
        if file:
            _vider(file, compte_id)


def urgente(cle: str) -> bool:
    return any(cle.startswith(p) for p in URGENTES)


def _vider(file: list[dict], compte_id: int | None):
    # Une urgente (alerte d'actualité, cours modifié…) part toujours seule, jamais noyée dans un résumé
    urgentes = [n for n in file if urgente(n["cle"])]
    file = [n for n in file if not urgente(n["cle"])]
    _envoyer(urgentes, compte_id)
    if len(file) > MAX_PAR_TOUR:  # une rafale (synchro, import…) = une seule notification résumée
        envoyes, erreurs = envoyer_a_tous(
            f"{len(file)} nouveautés sur Claunote", " · ".join(n["titre"] for n in file)[:300], "/notifications",
            "resume", compte_id=compte_id)
        statut, erreur = ("envoye", None) if envoyes or not erreurs else ("erreur", " ; ".join(erreurs))
        for n in file:
            db.maj_notification(n["id"], statut, erreur)
        log(f"{len(file)} notifications regroupées en une : {statut}")
        return
    _envoyer(file, compte_id)


def _envoyer(file: list[dict], compte_id: int | None):
    for n in file:
        envoyes, erreurs = envoyer_a_tous(n["titre"], n["corps"], n["url"], n["cle"], compte_id=compte_id)
        if envoyes or not erreurs:
            db.maj_notification(n["id"], "envoye")
            log(f"envoyée : {n['titre']}" if envoyes else f"aucun appareil abonné pour : {n['titre']}")
        else:
            db.maj_notification(n["id"], "erreur", " ; ".join(erreurs))
            log(f"échec : {n['titre']} : {erreurs[0]}")


# --- Ce qu'on propose ---------------------------------------------------------

def _quand(jours: int) -> str:
    return "aujourd'hui" if jours <= 0 else "demain" if jours == 1 else f"dans {jours} jours"


def _cartes(n: int) -> str:
    return f"{n} flashcard{'s' if n > 1 else ''} à revoir (~{max(1, round(n * 12 / 60))} min)"


AVANCE_JOURS = 7   # une même matière n'est proposée « avant le cours » qu'une fois par semaine


def avant_les_cours(aujourd_hui: dt.date) -> tuple[str, str, str] | None:
    """Parfois, la veille d'un cours : revoir les flashcards ou la fiche de cette matière.

    Une matière à la fois, au plus une fois par semaine chacune ; seulement si l'emploi du temps est connu."""
    if not preferences().get("edt", True):
        return None
    try:
        a = emploi_du_temps.avant_les_cours(aujourd_hui)
        deja = json.loads(db.etat_valeur(db.cle_du_compte("avance_proposees")) or "{}")
    except Exception as e:
        log(f"révisions avant les cours impossibles : {e}")
        return None
    if not a or not a["suggestions"]:
        return None
    limite = (aujourd_hui - dt.timedelta(days=AVANCE_JOURS)).isoformat()
    for s in a["suggestions"]:
        if deja.get(s["matiere"], "") > limite:
            continue
        deja = {m: j for m, j in deja.items() if j > limite} | {s["matiere"]: aujourd_hui.isoformat()}
        db.etat_ecrire(db.cle_du_compte("avance_proposees"), json.dumps(deja))
        ecart = (a["date"] - aujourd_hui).days
        quand = "Demain" if ecart == 1 else JOURS_LONGS[a["date"].weekday()].capitalize()
        bouts = []
        if s["cartes"]:
            bouts.append(_cartes(s["cartes"]))
        if s["fiche"]:
            bouts.append(f"ta fiche « {s['fiche']['sujet']} » à relire")
        url = f"/cartes/session?paquet={s['paquet']}" if s["cartes"] and s["paquet"] else f"/revision/{s['fiche']['id']}"
        return (f"{quand} : {s['matiere']} à {s['debut']:%H:%M}", "Avant le cours : " + " et ".join(bouts) + ".", url)
    return None


def rappel_du_jour(aujourd_hui: dt.date) -> tuple[str, str, str] | None:
    """Le rappel quotidien le plus utile : (titre, texte, page à ouvrir), ou None s'il n'y a rien."""
    evals = [(e, (e["le"].astimezone(PARIS).date() - aujourd_hui).days)
             for e in db.evenements_a_venir("evaluation", 8)]
    dues = db.nb_cartes_a_revoir(nouvelles_max=profil.nouvelles_max())
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
    avance = avant_les_cours(aujourd_hui)
    if avance:
        return avance
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
    if p["rappel"] and maintenant >= moment_rappel(jour, p) and not db.notification_connue(f"rappel:{jour}"):
        with db.pour_compte(1):   # ses cartes, ses campagnes et ses cours : pas ceux des autres comptes
            r = rappel_du_jour(jour)
        if r:
            db.notifier(f"rappel:{jour}", *r)
        else:
            db.notifier(f"rappel:{jour}", "Rien à proposer aujourd'hui", statut="ignore")
    planifier_invites(maintenant)
    if p["rappel"] and maintenant.weekday() == DIMANCHE and maintenant.time() >= _heure(p["heure"]):
        proposer_archivage(maintenant)
    if p["rappel"] and maintenant.time() >= _heure(p["silence_fin"]):
        with db.pour_compte(1):   # ses contrôles à lui : chaque invité a les siens (planifier_invites)
            du_jour = db.evenements_a_venir("evaluation", 1)
        for e in du_jour:
            if e["le"].astimezone(PARIS).date() == jour and e["le"] > maintenant:
                url = f"/revision/{e['revision_id']}" if e["revision_id"] else "/agenda"
                heure = e["le"].astimezone(PARIS)
                db.notifier(f"jour:{e['matiere']}:{heure:%Y-%m-%dT%H:%M}", f"{e['matiere']} aujourd'hui à {heure:%H:%M}",
                            "Un dernier coup d'œil à ta fiche ?" if e["revision_id"] else e["titre"], url)


def planifier_invites(maintenant: dt.datetime):
    """Le rappel du jour de chaque invité abonné, à son heure et calé sur SON emploi du temps : ses flashcards,
    ses campagnes, ses contrôles Pronote s'il a relié le sien. Plus un rappel le matin d'un contrôle.

    Avant, seul l'admin d'origine en recevait. Les autres admins reçoivent déjà ceux de l'admin d'origine."""
    jour = maintenant.date()
    for compte in db.comptes():
        i = compte["id"]
        if i == 1 or compte.get("role") == "admin" or not compte.get("actif"):
            continue
        p = preferences(i)
        if not p["rappel"] or not db.abonnements_de(i):
            continue
        try:
            with db.pour_compte(i):   # ses cartes, ses cours et son Pronote à lui
                _planifier_compte(maintenant, jour, i, p)
        except Exception as e:
            log(f"rappel du compte {i} impossible : {e}")


def _planifier_compte(maintenant: dt.datetime, jour: dt.date, i: int, p: dict):
    cle = f"rappel:{jour}@{i}"
    if maintenant >= moment_rappel(jour, p) and not db.notification_connue(cle):
        r = rappel_du_jour(jour)
        if r:
            db.notifier_compte(i, cle, *r)
        else:
            db.notifier_compte(i, cle, "Rien à proposer aujourd'hui", statut="ignore")
    if maintenant.time() >= _heure(p["silence_fin"]):
        for e in db.evenements_a_venir("evaluation", 1):
            if e["le"].astimezone(PARIS).date() == jour and e["le"] > maintenant:
                url = f"/revision/{e['revision_id']}" if e["revision_id"] else "/agenda"
                heure = e["le"].astimezone(PARIS)
                db.notifier_compte(i, f"jour:{e['matiere']}:{heure:%Y-%m-%dT%H:%M}@{i}",
                                   f"{e['matiere']} aujourd'hui à {heure:%H:%M}",
                                   "Un dernier coup d'œil à ta fiche ?" if e["revision_id"] else e["titre"], url)


DIMANCHE = 6


def moment_rappel(jour: dt.date, p: dict | None = None) -> dt.datetime:
    """L'heure choisie ; calée sur l'emploi du temps, plus tôt si les cours finissent avant."""
    p = p or preferences()
    if p.get("edt", True):
        return emploi_du_temps.heure_rappel(jour, _heure(p["heure"]))[0]
    return dt.datetime.combine(jour, _heure(p["heure"]), PARIS)


def proposer_archivage(maintenant: dt.datetime):
    """Une fois par semaine et par compte : le ménage proposé, s'il y a assez à ranger pour déranger."""
    annee, semaine, _ = maintenant.isocalendar()
    for compte in db.comptes():
        if not compte.get("actif", True):
            continue
        cle = f"archivage:{compte['id']}:{annee}-S{semaine:02d}"
        if db.notification_connue(cle):
            continue
        try:
            with db.pour_compte(compte["id"]):   # ses révisions seulement
                props = archivage.propositions(maintenant)
        except Exception as e:
            log(f"propositions d'archivage impossibles : {e}")
            continue
        # L'admin d'origine : appareils d'avant les comptes (sans compte), comme le rappel du jour
        with db.pour_compte(None if compte["id"] == 1 else compte["id"]):
            if len(props) >= archivage.MIN_NOTIFICATION:
                db.notifier(cle, "Un peu de ménage ?", archivage.resume(props), "/archives")
            else:
                db.notifier(cle, "Rien à archiver cette semaine", statut="ignore")


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


def apres_synchro_pronote(documents_deposes: int = 0, compte_id: int | None = None):
    """Nouveaux contrôles et documents des profs vus pendant la synchro Pronote du compte (sous ses droits)."""
    i = int(compte_id or db.compte_actuel() or 1)
    suffixe = "" if i == 1 else f"@{i}"
    actif = preferences(None if i == 1 else i)["pronote"] and bool(db.abonnements_de(None if i == 1 else i))
    # Premier passage (et, pour l'admin d'origine, clés d'avant les ids stables) : les contrôles déjà connus sont
    # notés sans être annoncés d'un coup
    premier = db.cle_du_compte("notifications_cles_stables", i)
    if not db.etat_valeur(premier):
        actif = False
        db.etat_ecrire(premier, "1")
    for e in db.evenements_a_venir("evaluation", 60):
        le = e["le"].astimezone(PARIS)
        # Clé tirée du contenu (matière + date), jamais d'un id Pronote : ceux-là changent à chaque connexion.
        db.notifier(f"nouvelle:{e['matiere']}:{le:%Y-%m-%dT%H:%M}{suffixe}", f"Nouveau contrôle : {e['matiere']}",
                    f"{JOURS[le.weekday()]} {le:%d/%m} · {e['titre']}", "/agenda",
                    statut="attente" if actif else "ignore")
    if actif and documents_deposes:
        db.notifier(f"documents:{int(time.time())}{suffixe}", "Nouveaux documents des profs",
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
