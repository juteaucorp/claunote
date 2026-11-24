"""Pronote : client du service interne `pronote` (Pawnote) et petites règles métier.

Chaque compte relie SON Pronote (le service garde un jeton par compte) : ses contrôles, ses notes, son emploi du
temps et ses alertes sont rangés chez lui (row-level security), ses réglages d'état sous des clés à lui (cle()).
L'admin d'origine (compte 1) garde les fichiers et les clés d'avant.

Le service Node fait la connexion ; ici on range ce qu'il renvoie et on relie
les matières Pronote (« CULTURE ECONOMIQUE, JURIDIQUE ET MANAGERIALE ») aux
matières des cours classés (« CEJM »).
"""
import base64
import datetime as dt
import difflib
import hashlib
import html
import os
import re
import unicodedata
from zoneinfo import ZoneInfo

import httpx

from claunote import db, emploi_du_temps
from claunote.agent import NIVEAUX

URL = os.environ.get("PRONOTE_URL", "http://pronote:3000")
JOURS_SYNCHRO = 28
PREPARATION_AUTO = os.environ.get("PRONOTE_PREPARATION_AUTO", "0") == "1"
JOURS_AUTO = 10       # préparation auto des évaluations de moins de 10 jours
PARIS = ZoneInfo("Europe/Paris")

# Un devoir qui annonce une évaluation (le contrôle n'est pas toujours marqué dans l'emploi du temps)
MOTS_EVALUATION = re.compile(
    r"\b(controle|ds|dst|devoir surveille|devoir sur table|evaluation|eval|interro\w*|examen|"
    r"partiel|bac blanc|bts blanc|epreuve|ccf|qcm)\b")

# Matières Pronote courantes → nom court utilisé par l'agent de classement
ALIAS = {
    "culture economique juridique et manageriale": "CEJM",
    "culture generale et expression": "Français",
    "expression et culture en langues vivantes etrangeres": "Anglais",
    "anglais lv1": "Anglais",
    "mathematiques pour l informatique": "Mathématiques",
    "mathematiques approfondies": "Mathématiques",
    "histoire geographie": "Histoire-Géographie",
    "sciences de la vie et de la terre": "SVT",
    "physique chimie": "Physique-Chimie",
    "cybersecurite des services informatiques": "Cybersécurité",
    "support et mise a disposition de services informatiques": "Systèmes",
    "administration des systemes et des reseaux": "Réseaux",
    "conception et developpement d applications": "Développement",
}

LIBELLES_NOTE = {1: "Abs", 2: "Disp", 3: "N.Not", 4: "Inapte", 5: "N.Rdu", 6: "Abs 0", 7: "N.Rdu 0", 8: "Félicitations"}


class ErreurPronote(Exception):
    pass


def compte_courant() -> int:
    """Compte de la requête ou du worker (sans compte : l'admin d'origine)."""
    return db.compte_actuel() or 1


def cle(nom: str, compte_id: int | None = None) -> str:
    """Clé d'état Pronote d'un compte : celle d'avant pour l'admin d'origine, « nom:<id> » pour les autres."""
    return db.cle_du_compte(nom, compte_id)


def comptes_relies() -> list[int]:
    """Comptes dont le worker suit le Pronote : l'admin d'origine, et chaque compte actif qui a le droit Agenda
    (un compte qui n'a pas relié le sien est vite passé : le service répond « pas connecté »)."""
    from claunote import comptes
    autres = [k["id"] for k in db.comptes() if k["id"] != 1 and comptes.utilisable(k) and comptes.peut(k, "agenda")]
    return [1, *autres]


class Client:
    def __init__(self, url: str = URL, transport=None, compte_id: int | None = None):
        self.compte = int(compte_id or compte_courant())
        params = {"compte": str(self.compte)} if self.compte != 1 else None
        self.http = httpx.Client(base_url=url, timeout=httpx.Timeout(120, connect=5), transport=transport,
                                 params=params)

    def _appel(self, methode: str, chemin: str, corps=None, timeout=None) -> dict:
        try:
            r = self.http.request(methode, chemin, json=corps,
                                  **({"timeout": timeout} if timeout else {}))
        except httpx.HTTPError as e:
            raise ErreurPronote(f"Le service Pronote ne répond pas ({e.__class__.__name__}).") from e
        donnees = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400:
            raise ErreurPronote(donnees.get("erreur") or f"Erreur {r.status_code} du service Pronote.")
        return donnees

    def etat(self) -> dict:
        # Affiché sur la page Agenda : ne doit jamais la faire attendre
        return self._appel("GET", "/etat", timeout=5)

    def connexion_qr(self, qr: str, pin: str) -> dict:
        return self._appel("POST", "/connexion/qr", {"qr": qr, "pin": pin})

    def connexion_identifiants(self, url: str, identifiant: str, mot_de_passe: str) -> dict:
        return self._appel("POST", "/connexion/identifiants",
                           {"url": url, "username": identifiant, "password": mot_de_passe})

    def deconnexion(self):
        self._appel("POST", "/deconnexion")

    def synchro(self, jours: int = 28) -> dict:
        return self._appel("POST", "/synchro", {"jours": jours})

    def verification(self, jours: int = 8) -> dict:
        """Emploi du temps des prochains jours et notes, sans le reste : la vérification fréquente."""
        return self._appel("POST", "/verification", {"jours": jours})

    def documents(self, deja: list[str]) -> dict:
        """Documents des profs pas encore récupérés (contenu en base64), par lots de 40 Mo."""
        return self._appel("POST", "/documents", {"deja": deja}, timeout=600)


def normaliser(texte: str) -> str:
    t = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def texte(fragment_html: str) -> str:
    """Description Pronote (HTML) → texte simple."""
    t = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", fragment_html or "", flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t\xa0]+", " ", t)).strip()


def annonce_evaluation(description: str) -> bool:
    return MOTS_EVALUATION.search(normaliser(description)) is not None


def date_locale(iso: str | None) -> dt.datetime | None:
    if not iso:
        return None
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(PARIS)


def _sigle(nom_normalise: str) -> str:
    return "".join(m[0] for m in nom_normalise.split() if len(m) > 2)


def trouver_matiere(nom_pronote: str, matieres: list[str]) -> str | None:
    """Matière des cours classés qui correspond au nom Pronote, ou None."""
    if not matieres:
        return None
    n = normaliser(nom_pronote)
    par_norme = {normaliser(m): m for m in matieres}
    # « SISR - RESEAUX » : on essaie aussi chaque morceau séparé par un tiret
    candidats = [n] + [normaliser(p) for p in re.split(r"\s[-/]\s|:", nom_pronote) if p.strip()]
    for c in candidats:
        if c in par_norme:
            return par_norme[c]
        alias = ALIAS.get(c)
        if alias and normaliser(alias) in par_norme:
            return par_norme[normaliser(alias)]
        for norme, m in par_norme.items():
            if norme.replace(" ", "") == _sigle(c) or (len(norme) >= 4 and re.search(rf"\b{norme}\b", c)):
                return m
    proches = difflib.get_close_matches(n, list(par_norme), n=1, cutoff=0.75)
    return par_norme[proches[0]] if proches else None


def niveau_actuel(niveaux_matieres: list[dict], matiere: str, ordre: list[str]) -> str | None:
    """Le niveau le plus avancé qui a des cours dans cette matière."""
    niveaux = [r["niveau"] for r in niveaux_matieres if r["matiere"] == matiere]
    return max(niveaux, key=ordre.index) if niveaux else None


def libelle_note(valeur: dict | None, sur: dict | None = None) -> str:
    if not valeur or not isinstance(valeur, dict):
        return "–"
    if valeur.get("genre", 0) != 0:
        return LIBELLES_NOTE.get(valeur["genre"], "?")
    if not isinstance(valeur.get("points"), (int, float)):  # format inattendu : pas de plantage de la page
        return "?"
    points = f"{valeur['points']:g}".replace(".", ",")
    if sur and sur.get("genre") == 0 and isinstance(sur.get("points"), (int, float)):
        return f"{points}/{sur['points']:g}"
    return points


def id_stable(prefixe: str, matiere: str, le: dt.datetime, description: str = "") -> str:
    """Id tiré du contenu : les ids Pronote changent à chaque connexion. Avec eux, chaque synchro
    voyait des contrôles « nouveaux » (re-notifiés) et perdait le lien vers leur fiche (refaite)."""
    cle = f"{prefixe}{matiere}:{le.isoformat(timespec='minutes')}"
    if description:
        cle += ":" + hashlib.sha1(description.encode()).hexdigest()[:10]
    return cle


def evenements(donnees: dict) -> list[dict]:
    """Synchro brute → lignes de la table pronote_evenements."""
    lignes, vus = [], set()
    for c in donnees.get("controles") or []:
        le = date_locale(c["debut"])
        lignes.append({
            "id": id_stable("cours:", c["matiere"], le), "genre": "evaluation", "matiere": c["matiere"],
            "titre": "Contrôle prévu à l'emploi du temps", "description": texte(c.get("notes", "")),
            "le": le, "fait": False})
    for d in donnees.get("devoirs") or []:
        description = texte(d["description"])
        eval_ = annonce_evaluation(description)
        le = date_locale(d["pour_le"])
        lignes.append({
            "id": id_stable("devoir:", d["matiere"], le, description or str(d.get("themes"))),
            "genre": "evaluation" if eval_ else "devoir", "matiere": d["matiere"],
            "titre": (", ".join(d.get("themes") or []) or description.split("\n")[0])[:120],
            "description": description, "le": le, "fait": d["fait"]})
    uniques = []  # deux cours identiques (même matière, même heure) = un seul contrôle
    for e in lignes:
        if e["id"] not in vus:
            vus.add(e["id"])
            uniques.append(e)
    return uniques


def preparer(ev: dict, aujourd_hui: dt.date | None = None) -> int | None:
    """Crée la révision d'une évaluation Pronote à partir des cours classés ; None si pas de cours."""
    aujourd_hui = aujourd_hui or dt.date.today()
    choix = db.niveaux_matieres()
    matiere = trouver_matiere(ev["matiere"], sorted({r["matiere"] for r in choix}))
    if matiere is None:
        return None
    niveau = niveau_actuel(choix, matiere, NIVEAUX)
    sujet = ev["titre"] if ev["id"].startswith("devoir:") else ""
    ids = db.choisir_documents(niveau, matiere, sujet) or db.choisir_documents(niveau, matiere)
    if not ids:
        return None
    le = ev["le"].astimezone(PARIS).date()
    libelle = f"{matiere} : {sujet}" if sujet else f"{matiere} : évaluation du {le:%d/%m}"
    # Toujours une fiche synthèse (avec ses questions) : un plan jour par jour ne se fait qu'à la demande
    rev_id = db.creer_revision("fiche", libelle, ids, le)
    db.lier_revision(ev["id"], rev_id)
    db.demander_scan()  # réveille le worker
    return rev_id


def preparer_a_venir(aujourd_hui: dt.date | None = None) -> list[int]:
    """Préparation auto : une révision par évaluation proche (même matière, même jour = une seule)."""
    faits, crees = set(), []
    evaluations = db.evenements_a_venir("evaluation", JOURS_AUTO)
    for ev in evaluations:
        if ev["revision_id"]:
            faits.add((ev["matiere"], ev["le"].astimezone(PARIS).date()))
    for ev in evaluations:
        cle = (ev["matiere"], ev["le"].astimezone(PARIS).date())
        if ev["revision_id"] or cle in faits:
            continue
        rev_id = preparer(ev, aujourd_hui)
        if rev_id:
            faits.add(cle)
            crees.append(rev_id)
    return crees


def synchroniser(client: Client | None = None, compte_id: int | None = None) -> dict:
    """Récupère le Pronote d'un compte (celui de la requête ou du worker) et l'enregistre chez lui.
    Renvoie le résumé affiché sur le site."""
    i = int(compte_id or compte_courant())
    with db.pour_compte(i):
        return _synchroniser(client or Client(compte_id=i))


def _synchroniser(client: Client) -> dict:
    etat = client.etat()
    if not etat.get("connecte"):
        return {"connecte": False, "expire": bool(etat.get("expire"))}
    d = client.synchro(JOURS_SYNCHRO)
    sources = [p for p, cle in (("cours:", "controles"), ("devoir:", "devoirs")) if d.get(cle) is not None]
    notes = d.get("notes")
    evs = evenements(d)
    db.enregistrer_pronote(evs, date_locale(d["du"]), sources,
                           notes["notes"] if notes else None, notes["periode"] if notes else None)
    emploi_du_temps.enregistrer(d.get("emploi_du_temps"), d.get("du"), d.get("au"))  # pour caler les notifications
    _alertes(d)
    db.etat_ecrire(cle("pronote_synchro"), dt.datetime.now(PARIS).isoformat(timespec="minutes"))
    db.etat_ecrire(cle("pronote_erreurs"), " ; ".join(d.get("erreurs") or []) or None)
    preparees = preparer_a_venir() if PREPARATION_AUTO else []
    return {"connecte": True, "evenements": len(evs), "preparees": preparees, "erreurs": d.get("erreurs") or []}


def _alertes(d: dict):
    """Cours modifiés et nouvelles notes, comparés au relevé d'avant (alertes.py)."""
    from claunote import alertes
    alertes.suivre_cours(d.get("cours"), d.get("du"), d.get("au"))
    alertes.suivre_notes(d.get("notes"))


JOURS_VERIFICATION = 8   # la semaine qui vient, plus le jour même


def verifier(client: Client | None = None, compte_id: int | None = None) -> dict:
    """Vérification rapide (toutes les 15 min en journée, et vers 7 h 10 pour les notes) : emploi du temps et
    notes seulement. Prévient d'un cours modifié dans la semaine ou d'une nouvelle note."""
    i = int(compte_id or compte_courant())
    with db.pour_compte(i):
        client = client or Client(compte_id=i)
        if not client.etat().get("connecte"):
            return {"connecte": False}
        d = client.verification(JOURS_VERIFICATION)
        emploi_du_temps.enregistrer(d.get("emploi_du_temps"), d.get("du"), d.get("au"))
        notes = d.get("notes")
        if notes:   # notes à jour sur l'Agenda aussi (aucun contrôle ni devoir touché : sources vides)
            db.enregistrer_pronote([], date_locale(d["du"]), [], notes["notes"], notes["periode"])
        from claunote import alertes
        cours = alertes.suivre_cours(d.get("cours"), d.get("du"), d.get("au"))
        nouvelles = alertes.suivre_notes(notes)
        return {"connecte": True, "changements": len(cours), "notes": len(nouvelles), "erreurs": d.get("erreurs") or []}


LOTS_DOCUMENTS = 5  # lots de 40 Mo au plus par passage


def nom_sur(nom: str) -> str:
    """Nom utilisable comme fichier ou dossier kDrive."""
    return re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", nom).strip(" .-")[:120] or "sans-nom"


def importer_documents(kd, inbox_id: int, client: Client | None = None, compte_id: int | None = None) -> dict | None:
    """Documents des profs → inbox kDrive (<classe>/Pronote/<matière>/), analysés ensuite comme un scan.

    Le dossier de classe (« BTS SIO 2 ») donne l'année d'étude au classement. Chaque fichier est noté comme
    déposé par le compte : il le range, et le cours est à lui.
    """
    i = int(compte_id or compte_courant())
    with db.pour_compte(i):
        return _importer_documents(kd, inbox_id, client or Client(compte_id=i), i)


def _importer_documents(kd, inbox_id: int, client: Client, compte_id: int) -> dict | None:
    etat = client.etat()
    if not etat.get("connecte"):
        return None
    classe = nom_sur(etat.get("classe") or "Pronote")
    dossiers: dict[str, int] = {}
    bilan = {"deposes": 0, "liens": 0, "erreurs": [], "restant": 0}
    for _ in range(LOTS_DOCUMENTS):
        r = client.documents(db.cles_documents_pronote())
        bilan["erreurs"] += r.get("erreurs") or []
        for d in r["documents"]:
            if d.get("contenu") is not None:
                matiere = nom_sur(d["matiere"] or "Divers")
                if matiere not in dossiers:
                    dossiers[matiere] = kd.ensure_path(inbox_id, [classe, "Pronote", matiere])
                envoi = kd.upload(dossiers[matiere], nom_sur(d["nom"]), base64.b64decode(d["contenu"]),
                                  conflict="rename")
                db.enregistrer_document_pronote(d, "depose", kdrive_id=envoi.get("id"))
                if compte_id != 1 and envoi.get("id"):
                    db.noter_depot(envoi["id"], compte_id)
                bilan["deposes"] += 1
            elif d.get("url"):
                db.enregistrer_document_pronote(d, "lien")
                bilan["liens"] += 1
            else:
                db.enregistrer_document_pronote(d, "erreur", erreur=d.get("erreur"))
                bilan["erreurs"].append(f"{d['nom']} : {d.get('erreur')}")
        bilan["restant"] = r.get("restant", 0)
        if not bilan["restant"]:
            break
    if bilan["deposes"]:
        db.demander_scan()
    return bilan
