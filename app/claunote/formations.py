"""Formation suivie chaque année (« BTS SIO option SISR », « Terminale générale »…) et son référentiel officiel.

Tu donnes le nom de ta formation pour une année ; Claude cherche son référentiel (programme officiel,
Bulletin officiel, Éduscol) en s'aidant des matières déjà rangées, et en tire la liste des matières
exactes de l'année. Ensuite, chaque nouveau document de cette année est rangé dans l'une d'elles.

    nom de la formation ──► Claude (+ recherche web) ──► référentiel : matières officielles,
    + matières déjà là                                   contenus, correspondances proposées
                                                                  │
    Réorganiser (fusionner, renommer) ──► règles de rangement ◄───┘
                                                  │
    nouveau dépôt ──► analyse ──► ranger() : règle > matière officielle > nom déjà utilisé

Les règles (table matieres_regles) naissent de chaque fusion : « Maths » → « Mathématiques » en BTS1 reste vrai
pour les prochains scans, même si l'IA redit « Maths ».
"""
import contextlib
import contextvars
import json
import tempfile
from pathlib import Path

from claunote import matieres as mat

# Suggestions pour le champ « formation » (on peut écrire autre chose)
SUGGESTIONS = {
    "6e": ["6e (cycle 3)"], "5e": ["5e (cycle 4)"], "4e": ["4e (cycle 4)"], "3e": ["3e (cycle 4)"],
    "Seconde": ["Seconde générale et technologique", "Seconde professionnelle"],
    "Première": ["Première générale", "Première STMG", "Première STI2D", "Première professionnelle"],
    "Terminale": ["Terminale générale", "Terminale STMG", "Terminale STI2D", "Terminale professionnelle"],
    "BTS1": ["BTS SIO option SISR", "BTS SIO option SLAM"],
    "BTS2": ["BTS SIO option SISR", "BTS SIO option SLAM"],
}
ANNEE_LISIBLE = {"BTS1": "1re année de BTS", "BTS2": "2e année de BTS"}

PROMPT = """Un élève a rangé ses cours de l'année « {niveau} » ({annee}). Il suit cette formation : « {formation} ».

Trouve le référentiel ou le programme officiel complet de cette formation pour CETTE année : référentiel du BTS
(arrêté, annexes, blocs de compétences, épreuves), programme du lycée ou du collège (Bulletin officiel, Éduscol).
Cherche sur le web (WebSearch, WebFetch) si tu le peux ; sinon, appuie-toi sur ce que tu sais du texte officiel.
Si le nom donné est ambigu, prends la formation la plus probable d'après ses matières déjà rangées (sur l'entrée).

Sur l'entrée : ses matières déjà rangées pour cette année (nombre de documents, quelques titres de cours),
et les noms utilisés les autres années.

Donne la liste des matières telles qu'elles apparaissent dans un emploi du temps ou sur Pronote pour cette
formation et cette année. Pour une formation découpée en blocs (BTS), garde les enseignements réels : par exemple
en BTS SIO SISR, les cours de réseaux, de systèmes et de cybersécurité sont souvent des matières distinctes.
Nom (`nom`) : court, en français, comme un élève l'écrirait. Si l'une de ses matières déjà rangées correspond,
reprends EXACTEMENT son nom (c'est elle qui est gardée). `officiel` : l'intitulé exact du référentiel.

Puis, pour chaque matière déjà rangée, dis dans quelle matière officielle elle doit aller (`correspondances`) ;
mets `"cible": null` si elle n'en relève d'aucune (option, hors programme) ou si tu hésites.

Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour :
{{"formation": "intitulé officiel complet", "source": "URL du texte officiel ou null",
  "resume": "2 phrases : ce que vise la formation cette année",
  "matieres": [{{"nom": "Mathématiques", "officiel": "Mathématiques pour l'informatique",
    "description": "1 phrase", "contenus": ["grands thèmes ou compétences du programme, 10 au plus"],
    "epreuve": "E2 ou null"}}],
  "correspondances": [{{"actuelle": "Maths", "cible": "Mathématiques", "raison": "même matière"}}]}}
"""

# Pendant l'analyse d'un document : le texte « formations » du compte (posé par le worker, lu par agent et local)
CONTEXTE: contextvars.ContextVar[str] = contextvars.ContextVar("formations", default="")


@contextlib.contextmanager
def contexte_pour(texte: str):
    jeton = CONTEXTE.set(texte or "")
    try:
        yield
    finally:
        CONTEXTE.reset(jeton)


def annee_lisible(niveau: str) -> str:
    return ANNEE_LISIBLE.get(niveau, niveau)


def entree(niveau: str, comptes: list[dict], titres: dict[tuple, list[str]]) -> str:
    """Matières déjà rangées : celles de l'année (avec titres), puis les noms des autres années."""
    lignes = [f"## Matières déjà rangées en {niveau}"]
    ici = [r for r in comptes if r.get("niveau") == niveau and r.get("matiere")]
    for r in ici:
        exemples = "; ".join(titres.get((niveau, r["matiere"]), [])[:5])
        lignes.append(f"- {r['matiere']} ({r['n']} doc.){' : ' + exemples if exemples else ''}")
    if not ici:
        lignes.append("(aucune pour l'instant)")
    autres = sorted({r["matiere"] for r in comptes if r.get("niveau") != niveau and r.get("matiere")})
    if autres:
        lignes += ["", "## Noms utilisés les autres années", ", ".join(autres)]
    return "\n".join(lignes)


def _texte(v, n: int) -> str:
    return " ".join(str(v).split())[:n] if isinstance(v, str) and v.strip() else ""


def lire(texte: str, existantes: list[str]) -> dict:
    """Référentiel renvoyé par l'IA, nettoyé : matières sans doublon, correspondances vers des noms connus."""
    from claunote.agent import ErreurAgent
    debut, fin = texte.find("{"), texte.rfind("}")
    try:
        d = json.loads(texte[debut:fin + 1])
    except (json.JSONDecodeError, ValueError) as e:
        raise ErreurAgent(f"JSON invalide : {e}") from e
    if not isinstance(d, dict) or not isinstance(d.get("matieres"), list):
        raise ErreurAgent("réponse sans liste de matières")
    liste, vues = [], set()
    for m in d["matieres"]:
        if not isinstance(m, dict) or not _texte(m.get("nom"), 80):
            continue
        nom = _texte(m["nom"], 80)
        # Une matière déjà rangée sous ce nom (aux accents près) garde son nom exact
        nom = next((e for e in existantes if mat.forme(e) == mat.forme(nom)), nom)
        if mat.forme(nom) in vues:
            continue
        vues.add(mat.forme(nom))
        contenus = [_texte(c, 160) for c in m.get("contenus") or [] if _texte(c, 160)][:12]
        liste.append({"nom": nom, "officiel": _texte(m.get("officiel"), 160) or nom,
                      "description": _texte(m.get("description"), 300), "contenus": contenus,
                      "epreuve": _texte(m.get("epreuve"), 40)})
    if not liste:
        raise ErreurAgent("aucune matière dans le référentiel")
    noms = {x["nom"] for x in liste}
    par_forme = {mat.forme(n): n for n in noms}
    corr = []
    for c in d.get("correspondances") or []:
        if not isinstance(c, dict) or c.get("actuelle") not in existantes:
            continue
        cible = _texte(c.get("cible"), 80)
        cible = cible if cible in noms else par_forme.get(mat.forme(cible)) if cible else None
        corr.append({"actuelle": c["actuelle"], "cible": cible, "raison": _texte(c.get("raison"), 200)})
    source = _texte(d.get("source"), 400)
    return {"formation": _texte(d.get("formation"), 200), "source": source if source.startswith("http") else "",
            "resume": _texte(d.get("resume"), 600), "matieres": liste, "correspondances": corr}


def chercher(niveau: str, formation: str, comptes: list[dict], titres: dict[tuple, list[str]]) -> tuple[dict, str]:
    """Référentiel de la formation pour cette année (Claude, avec recherche web). Renvoie (référentiel, modèle).

    Lève LimiteAtteinte, ClaudeIndisponible, SansForfait, ErreurAgent comme tout appel à Claude.
    """
    from claunote.agent import demander
    existantes = [r["matiere"] for r in comptes if r.get("niveau") == niveau and r.get("matiere")]
    prompt = PROMPT.format(niveau=niveau, annee=annee_lisible(niveau), formation=formation)
    with tempfile.TemporaryDirectory() as vide:
        return demander("referentiel", prompt, Path(vide), lire=lambda t: lire(t, existantes),
                        outils=["WebSearch", "WebFetch"], entree=entree(niveau, comptes, titres)[:30_000],
                        timeout=1200)


# --- Rangement d'après le référentiel et les règles -----------------------------------

def noms_officiels(ref: dict | None) -> list[str]:
    return [m["nom"] for m in (ref or {}).get("matieres") or []]


def vers_officielle(matiere: str, ref: dict | None) -> str | None:
    """Matière officielle de l'année qui correspond à ce nom : la même, la correspondance validée par l'IA,
    ou une évidence (« Maths » → « Mathématiques », pluriel, accents)."""
    officiels = noms_officiels(ref)
    if not officiels or not matiere:
        return None
    if matiere in officiels:
        return matiere
    for c in ref.get("correspondances") or []:
        if c.get("actuelle") == matiere and c.get("cible") in officiels:
            return c["cible"]
    for o in officiels:
        l = mat.lien(matiere, o)
        if l and l[0]:
            return o
    for m in ref["matieres"]:   # intitulé officiel donné tel quel par l'IA (« Mathématiques pour l'informatique »)
        if mat.forme(m.get("officiel") or "") == mat.forme(matiere):
            return m["nom"]
    return None


def ranger(niveau: str | None, matiere: str | None, existantes: list[str], regles: dict[tuple, str] | None = None,
           refs: dict[str, dict] | None = None, distinctes: set[frozenset] | None = None) -> str | None:
    """Nom de matière à garder pour un nouveau document.

    1. une règle de l'année (tu as fusionné ou renommé ce nom) ;
    2. la matière officielle de l'année (référentiel de ta formation) ;
    3. un nom déjà utilisé pour la même matière (évidence seulement, matieres.harmoniser).
    """
    if not matiere:
        return matiere
    regles, refs = regles or {}, refs or {}
    vu = set()
    while (niveau, matiere) in regles and matiere not in vu:   # règles enchaînées : A → B → C
        vu.add(matiere)
        matiere = regles[(niveau, matiere)]
    if vu:
        return matiere
    officielle = vers_officielle(matiere, refs.get(niveau or ""))
    if officielle:
        return officielle
    return mat.harmoniser(matiere, existantes, distinctes)


def matiere_depuis_dossier(dossiers: list[str], niveau: str | None, connues: list[str]) -> str | None:
    """Matière choisie au dépôt (ou nom de dossier) : un dossier qui porte exactement le nom d'une matière connue
    de l'année (aux accents et au pluriel près). Le plus profond d'abord."""
    if not niveau or not connues:
        return None
    par_forme = {mat.forme(c): c for c in connues}
    for d in reversed(dossiers or []):
        if mat.forme(d) in par_forme:
            return par_forme[mat.forme(d)]
    return None


def texte_pour_classer(formations: list[dict]) -> str:
    """Texte ajouté au prompt d'analyse : formation et matières exactes de chaque année renseignée."""
    lignes = []
    for f in formations:
        ref = f.get("referentiel") or {}
        if not ref.get("matieres"):
            continue
        noms = []
        for m in ref["matieres"]:
            themes = ", ".join(m.get("contenus", [])[:4])
            noms.append(f"« {m['nom']} »" + (f" ({themes})" if themes else ""))
        lignes.append(f"- {f['niveau']} : {ref.get('formation') or f['nom']}. Matières : {' ; '.join(noms)}.")
    if not lignes:
        return ""
    return ("\nFormation suivie par l'élève, année par année (référentiel officiel) :\n" + "\n".join(lignes)
            + "\nSi le document est de l'une de ces années, sa matière doit être EXACTEMENT l'un des noms entre "
              "guillemets de cette année.\n")


def suggestions_reorganisation(niveau: str, actuelles: list[str], ref: dict | None,
                               regles: dict[tuple, str] | None = None) -> dict[str, dict]:
    """Pour chaque matière rangée cette année : {"cible", "raison"} proposé (None : la garder telle quelle)."""
    sortie = {}
    officiels = noms_officiels(ref)
    for m in actuelles:
        cible, raison = None, ""
        if regles and (niveau, m) in regles:
            cible, raison = regles[(niveau, m)], "règle déjà posée"
        elif officiels and m not in officiels:
            corr = next((c for c in (ref or {}).get("correspondances") or [] if c.get("actuelle") == m), None)
            if corr and corr.get("cible") in officiels:
                cible, raison = corr["cible"], corr.get("raison") or "d'après le référentiel"
            elif (o := vers_officielle(m, ref)) and o != m:
                cible, raison = o, "même matière, nom du référentiel"
        sortie[m] = {"cible": cible if cible != m else None, "raison": raison}
    return sortie
