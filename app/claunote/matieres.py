"""Matières en double sur une même année (« Maths » et « Mathématiques », « Réseau » et « Réseaux »…).

L'agent de classement choisit un nom court, mais pas toujours le même d'un document à l'autre.
Ici, sans IA : on rapproche les noms d'une même année et on propose de les fusionner.

    noms d'une année ──► forme simple ──► même concept (alias), pluriel, sigle,
                         (accents,        nom presque identique, nom contenu dans l'autre
                          mots vides)            │
                                                 ▼
                              groupes à fusionner + nom proposé (le plus utilisé)

Un groupe « sûr » (alias, pluriel, accents) est une évidence ; les autres sont des propositions.
Un passage IA (bouton sur la page) peut en trouver d'autres, à partir de quelques titres de cours.
La fusion elle-même (db.fusionner_matieres) change la matière des documents ; le worker les déplace dans kDrive.
"""
import difflib
import json
import re
import tempfile
import unicodedata
from pathlib import Path

MOTS_VIDES = {"et", "de", "la", "le", "les", "des", "du", "en", "l", "d", "a", "au", "aux", "pour"}

# Même matière, autre nom (formes simples, voir forme()) → concept
ALIAS = {
    "math": "mathematique", "maths": "mathematique", "mathematique": "mathematique", "mathematiques": "mathematique",
    "mathematique informatique": "mathematique",
    "francai": "francai", "lettre": "francai", "culture generale expression": "francai", "cge": "francai",
    "hg": "histoire geographie", "histoire geo": "histoire geographie", "hist geo": "histoire geographie",
    "histoire geographie": "histoire geographie", "histoire geographie emc": "histoire geographie",
    "svt": "svt", "science vie terre": "svt",
    "pc": "physique chimie", "physique chimie": "physique chimie", "science physique": "physique chimie",
    "eps": "eps", "sport": "eps", "education physique sportive": "eps",
    "emc": "emc", "education morale civique": "emc",
    "ses": "ses", "science economique sociale": "ses",
    "anglai": "anglai", "english": "anglai", "lv1 anglai": "anglai", "anglai lv1": "anglai",
    "espagnol": "espagnol", "lv2 espagnol": "espagnol", "espagnol lv2": "espagnol",
    "allemand": "allemand", "lv2 allemand": "allemand", "allemand lv2": "allemand",
    "nsi": "nsi", "numerique science informatique": "nsi",
    "snt": "snt", "science numerique technologie": "snt",
    "techno": "technologie", "technologie": "technologie",
    "philo": "philosophie", "philosophie": "philosophie",
    "cejm": "cejm", "culture economique juridique manageriale": "cejm",
    "reseau": "reseau", "administration systeme reseau": "reseau",
    "systeme": "systeme", "systeme exploitation": "systeme",
    "dev": "developpement", "developpement": "developpement", "programmation": "developpement",
    "developpement application": "developpement", "conception developpement application": "developpement",
    "cyber": "cybersecurite", "cybersecurite": "cybersecurite", "securite informatique": "cybersecurite",
    "cybersecurite service informatique": "cybersecurite",
}
PROCHE = 0.86  # ressemblance minimale de deux noms (fautes de frappe, tirets…)


def forme(nom: str) -> str:
    """Forme simple d'un nom : sans accents, ponctuation, mots vides ni pluriel (« Réseaux » → « reseau »)."""
    t = unicodedata.normalize("NFKD", nom or "").encode("ascii", "ignore").decode().lower()
    mots = []
    for m in re.sub(r"[^a-z0-9]+", " ", t).split():
        if m in MOTS_VIDES:
            continue
        if m.endswith("eaux"):        # réseaux → reseau
            m = m[:-1]
        elif len(m) > 3 and m.endswith("aux"):  # généraux → general
            m = m[:-3] + "al"
        elif len(m) > 3 and m[-1] in "sx":
            m = m[:-1]
        mots.append(m)
    return " ".join(mots)


def concept(nom: str) -> str:
    f = forme(nom)
    return ALIAS.get(f, f)


def _sigle(f: str) -> str:
    return "".join(m[0] for m in f.split() if len(m) > 2)


def lien(a: str, b: str) -> tuple[bool, str] | None:
    """Pourquoi deux noms désignent sans doute la même matière : (sûr, raison), ou None."""
    fa, fb = forme(a), forme(b)
    if fa == fb:
        return True, "même nom, à l'accent, au pluriel ou à la ponctuation près"
    if concept(a) == concept(b):
        return True, "même matière sous un autre nom"
    courte, longue = sorted((fa, fb), key=len)
    if " " not in courte and len(courte) >= 2 and courte == _sigle(longue):
        return False, f"« {a if fa == courte else b} » est le sigle de l'autre"
    if difflib.SequenceMatcher(None, fa, fb).ratio() >= PROCHE:
        return False, "noms presque identiques"
    if len(courte) >= 4 and set(courte.split()) < set(longue.split()):
        return False, "un nom contient l'autre"
    return None


def paires(noms) -> set[frozenset]:
    """Toutes les paires de noms d'un groupe."""
    noms = sorted(set(noms))
    return {frozenset((a, b)) for i, a in enumerate(noms) for b in noms[i + 1:]}


def deja_tranche(groupe: dict, distinctes: dict[str, set[frozenset]]) -> bool:
    """Toutes les paires du groupe ont été laissées telles quelles pour cette année."""
    p = paires(n["nom"] for n in groupe["noms"])
    return bool(p) and p <= distinctes.get(groupe["niveau"], set())


def proposer(comptes: list[dict], autres_annees: dict[str, int] | None = None,
             distinctes: dict[str, set[frozenset]] | None = None) -> list[dict]:
    """Groupes de matières à fusionner, année par année.

    `comptes` : [{"niveau", "matiere", "n"}] (documents par année et matière).
    `autres_annees` : {matière: nombre de documents toutes années} pour départager le nom proposé.
    `distinctes` : {année: paires de noms laissées telles quelles} : jamais reproposées ensemble.
    Renvoie [{"niveau", "noms": [{"nom", "n"}], "nom", "sur", "raisons"}].
    """
    autres_annees, distinctes = autres_annees or {}, distinctes or {}
    par_niveau: dict[str, dict[str, int]] = {}
    for r in comptes:
        if r.get("niveau") and r.get("matiere"):
            par_niveau.setdefault(r["niveau"], {})[r["matiere"]] = r["n"]
    groupes = []
    for niveau, noms in par_niveau.items():
        liste = sorted(noms)
        parent = {n: n for n in liste}

        def racine(n):
            while parent[n] != n:
                n = parent[n]
            return n

        liens = {}
        ecartees = distinctes.get(niveau, set())
        for i, a in enumerate(liste):
            for b in liste[i + 1:]:
                l = None if frozenset((a, b)) in ecartees else lien(a, b)
                if l:
                    parent[racine(b)] = racine(a)
                    liens[(a, b)] = l
        paquets: dict[str, list[str]] = {}
        for n in liste:
            paquets.setdefault(racine(n), []).append(n)
        for membres in paquets.values():
            if len(membres) < 2:
                continue
            internes = [l for (a, b), l in liens.items() if a in membres and b in membres]
            # Le plus utilisé ; à égalité, celui des autres années, puis le nom complet (« Mathématiques »)
            nom = max(membres, key=lambda m: (noms[m], autres_annees.get(m, 0), m[:1].isupper(), len(m)))
            groupes.append({"niveau": niveau, "noms": [{"nom": m, "n": noms[m]} for m in membres], "nom": nom,
                            "sur": all(s for s, _ in internes), "raisons": sorted({r for _, r in internes}),
                            "source": "auto"})
    return groupes


def harmoniser(matiere: str | None, existantes: list[str], distinctes: set[frozenset] | None = None) -> str | None:
    """Nom déjà utilisé pour la même matière (évidence seulement), sinon le nom donné.

    Évite que l'agent crée « Maths » à côté de « Mathématiques » au prochain scan ; sauf si tu as choisi
    de garder ces deux noms séparés (`distinctes`, toutes années confondues).
    """
    if not matiere:
        return matiere
    if matiere in existantes:
        return matiere
    for e in existantes:
        if distinctes and frozenset((matiere, e)) in distinctes:
            continue
        l = lien(matiere, e)
        if l and l[0]:
            return e
    return matiere


# --- Second avis de l'IA ------------------------------------------------------------

PROMPT_IA = """Voici, sur l'entrée, les matières des cours d'un élève (du collège au BTS SIO), année par année,
avec le nombre de documents et quelques titres de cours. L'IA qui a classé ces cours a parfois donné
plusieurs noms à la même matière (« Maths » et « Mathématiques », « SLAM » et « Développement »…).

Trouve, dans une même année, les noms qui désignent la même matière et doivent être fusionnés.
Ne fusionne pas deux vraies matières différentes (Physique-Chimie et SVT, Réseaux et Cybersécurité…) :
en cas de doute, ne propose rien. Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour :
{"fusions": [{"niveau": "BTS1", "noms": ["Maths", "Mathématiques"], "nom": "Mathématiques",
  "raison": "même matière"}]}
`nom` : le nom à garder, de préférence l'un des noms existants. Aucune fusion : {"fusions": []}.
"""


def entree_ia(comptes: list[dict], titres: dict[tuple, list[str]]) -> str:
    lignes = []
    for niveau in sorted({r["niveau"] for r in comptes if r.get("niveau")}):
        lignes.append(f"## {niveau}")
        for r in comptes:
            if r["niveau"] == niveau and r.get("matiere"):
                exemples = "; ".join(titres.get((niveau, r["matiere"]), [])[:4])
                lignes.append(f"- {r['matiere']} ({r['n']} doc.){' : ' + exemples if exemples else ''}")
    return "\n".join(lignes)


def lire_ia(texte: str, comptes: list[dict]) -> list[dict]:
    """Fusions proposées par l'IA, gardées seulement si elles portent sur des matières qui existent."""
    from claunote.agent import ErreurAgent
    debut, fin = texte.find("{"), texte.rfind("}")
    try:
        d = json.loads(texte[debut:fin + 1])
    except (json.JSONDecodeError, ValueError) as e:
        raise ErreurAgent(f"JSON invalide : {e}") from e
    if not isinstance(d, dict) or not isinstance(d.get("fusions"), list):
        raise ErreurAgent("réponse sans liste de fusions")
    existe = {(r["niveau"], r["matiere"]): r["n"] for r in comptes}
    groupes = []
    for f in d["fusions"]:
        if not isinstance(f, dict) or not isinstance(f.get("noms"), list):
            continue
        niveau = f.get("niveau")
        noms = [n for n in dict.fromkeys(f["noms"]) if isinstance(n, str) and (niveau, n) in existe]
        if len(noms) < 2:
            continue
        nom = f.get("nom") if isinstance(f.get("nom"), str) and f["nom"].strip() else noms[0]
        groupes.append({"niveau": niveau, "noms": [{"nom": n, "n": existe[(niveau, n)]} for n in noms],
                        "nom": nom.strip(), "sur": False, "raisons": [str(f.get("raison") or "avis de l'IA")[:200]],
                        "source": "ia"})
    return groupes


def demander_ia(comptes: list[dict], titres: dict[tuple, list[str]], essai_local=None) -> list[dict]:
    """Le modèle local d'abord (`essai_local(prompt, entree)`), sinon un appel court à Claude (Haiku).

    Lève LimiteAtteinte, ClaudeIndisponible… si Claude est appelé et ne répond pas.
    """
    from claunote.agent import ErreurAgent, demander
    if essai_local:
        d = essai_local(PROMPT_IA, entree_ia(comptes, titres)[:12_000])
        if d:
            try:
                groupes = lire_ia(json.dumps(d), comptes)
                if groupes:
                    return [{**g, "raisons": g["raisons"] + ["vu par le modèle local"]} for g in groupes]
            except ErreurAgent:
                pass
    with tempfile.TemporaryDirectory() as vide:
        groupes, _ = demander("matieres", PROMPT_IA, Path(vide), lire=lambda t: lire_ia(t, comptes),
                              entree=entree_ia(comptes, titres)[:40_000], timeout=300)
    return groupes


def fusionner_propositions(auto: list[dict], ia: list[dict]) -> list[dict]:
    """Propositions locales puis celles de l'IA qui n'y sont pas déjà."""
    vus = {(g["niveau"], frozenset(n["nom"] for n in g["noms"])) for g in auto}
    return auto + [g for g in ia if (g["niveau"], frozenset(n["nom"] for n in g["noms"])) not in vus]
