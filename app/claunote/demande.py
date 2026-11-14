"""Réviser en une phrase : « fiche de CEJM sur le contrat pour jeudi » → ce qu'il faut créer.

    texte libre ──► comprendre() ──► supports (fiche, quiz…) · matières et années · sujet · date
                         │                       │
                         │                       └─► rien de sûr ? une question à l'écran (jamais de refus)
                         └─ sans IA : mots-clés, noms des matières des cours classés, recherche dans les
                            cours quand la matière n'est pas dite, contrôles Pronote pour la date

Tout se passe sans appel à Claude : la réponse est immédiate et ne consomme rien du forfait. La phrase
entière part ensuite comme consigne à l'IA qui rédige, pour que rien de ce qui est demandé ne se perde.
"""
import datetime as dt
import difflib
import re

from claunote import matieres as noms_matieres
from claunote import pronote, revision
from claunote.agent import NIVEAUX

# Mots (sans accents) qui désignent un support
MOTS_GENRES = {
    "fiche": r"fiches?|synthese|resumes?|recap\w*|memo",
    "quiz": r"quiz+|qcm|interroge\w*|questions?|teste\w*",
    "cartes": r"flash ?cards?|cartes?|par coeur|memoris\w*|anki",
    "examen": r"epreuves? blanches?|examens? blancs?|sujets? (?:type|d examen|blanc)|annales?|en conditions(?: reelles)?",
    "plan": r"plans?(?: de revisions?)?|planning|jour par jour|organise\w*",
}
TOUT = r"tous les supports|tout ce qu il faut|la totale|le pack complet"
# Une période ou un examen qui porte sur plusieurs matières : donne son titre à la campagne
CONTEXTES = r"(bts|bac|brevet|partiels?|examens?|ds) blancs?|vacances|semaine de revisions?"
NIVEAUX_MOTS = {"6e": r"6e(?:me)?|sixieme", "5e": r"5e(?:me)?|cinquieme", "4e": r"4e(?:me)?|quatrieme",
                "3e": r"3e(?:me)?|troisieme", "Seconde": r"seconde|2nde", "Première": r"premiere|1ere",
                "Terminale": r"terminale|tle", "BTS1": r"bts ?1|premiere annee(?: de bts)?|1ere annee",
                "BTS2": r"bts ?2|deuxieme annee|2e(?:me)? annee|seconde annee"}
TOUTES_ANNEES = r"toutes? les annees|les deux annees|depuis le debut|tout le bts|sur \d ans"
MOIS = ["janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre",
        "novembre", "decembre"]
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
# Mots qui ne disent rien du sujet (« fais-moi une … sur … pour … »)
CREUX = set("""a ai au aux avec ce ces cette d de des du en et est il j je l la le les m ma me mes moi mon n ne on ou
par pas pour qu que qui s se si sur t te toi ton tu un une y bien tres tout toute tous toutes
fais faire fait prepare preparer genere generer cree creer donne donner sors sortir veux voudrais aimerais peux
pourrais faut besoin dois stp svp merci bonjour salut reviser revision revisions revoir travailler apprendre
comprendre comprends controle controles ds devoir eval evals evaluation evaluations interro interrogation
epreuve examen cours chapitre chapitres lecon lecons theme themes partie notion notions matiere matieres
prochain prochaine semaine jour jours avant apres complet complete vite rapide rapidement rien plus trop encore comme mais celle celui""".split())


def _mots(texte: str) -> tuple[list[str], list[tuple[int, int]], str, list[tuple[int, int]]]:
    """Découpe en mots : (mots d'origine, leur place dans le texte, phrase sans accents, place de chaque mot dedans)."""
    origine, places, normes, places_normes, curseur = [], [], [], [], 0
    for m in re.finditer(r"[^\W_]+", texte):
        norme = pronote.normaliser(m.group(0)).replace(" ", "")
        if not norme:
            continue
        origine.append(m.group(0))
        places.append((m.start(), m.end()))
        places_normes.append((curseur, curseur + len(norme)))
        normes.append(norme)
        curseur += len(norme) + 1
    return origine, places, " ".join(normes), places_normes


class _Lecture:
    """La phrase et les mots déjà expliqués (support, matière, date…) : le reste est le sujet."""

    def __init__(self, texte: str):
        self.texte = texte
        self.origine, self.places, self.phrase, self.places_normes = _mots(texte)
        self.pris: set[int] = set()

    def trouver(self, motif: str, prendre: bool = True) -> re.Match | None:
        m = re.search(rf"(?<![a-z0-9])(?:{motif})(?![a-z0-9])", self.phrase)
        if m and prendre:
            self.prendre(m.start(), m.end())
        return m

    def trouver_tous(self, motif: str) -> bool:
        """Comme trouver, mais explique toutes les occurrences (« fiche synthèse » : les deux mots)."""
        vus = [m for m in re.finditer(rf"(?<![a-z0-9])(?:{motif})(?![a-z0-9])", self.phrase)]
        for m in vus:
            self.prendre(m.start(), m.end())
        return bool(vus)

    def prendre(self, debut: int, fin: int):
        self.pris |= {i for i, (a, b) in enumerate(self.places_normes) if a < fin and b > debut}

    def suites(self, longueur_max: int = 5):
        """Toutes les suites de 1 à 5 mots, les plus longues d'abord : (texte sans accents, début, fin)."""
        mots = self.phrase.split()
        for n in range(min(longueur_max, len(mots)), 0, -1):
            for i in range(len(mots) - n + 1):
                yield " ".join(mots[i:i + n]), self.places_normes[i][0], self.places_normes[i + n - 1][1]

    def sujet(self) -> str:
        """Ce qui reste de la phrase, du premier au dernier mot non expliqué, dans les mots de l'élève."""
        libres = [i for i, mot in enumerate(self.phrase.split()) if i not in self.pris and mot not in CREUX
                  and not mot.isdigit()]
        if not libres:
            return ""
        # Un trou de plus de 3 mots expliqués coupe le sujet : on garde le plus long morceau
        morceaux, courant = [], [libres[0]]
        for i in libres[1:]:
            if i - courant[-1] > 4 or any(j in self.pris for j in range(courant[-1] + 1, i)):
                morceaux.append(courant)
                courant = [i]
            else:
                courant.append(i)
        morceaux.append(courant)
        meilleur = max(morceaux, key=len)
        return self.texte[self.places[meilleur[0]][0]:self.places[meilleur[-1]][1]].strip()


def trouver_genres(lu: _Lecture) -> list[str]:
    if lu.trouver(TOUT):
        return ["fiche", "quiz", "cartes"]
    return [g for g, motif in MOTS_GENRES.items() if lu.trouver_tous(motif)]


def trouver_date(lu: _Lecture, aujourdhui: dt.date) -> tuple[dt.date | None, dt.date | None]:
    """(jour dit dans la phrase, fin de la période si c'est une semaine entière)."""
    if lu.trouver(r"(?:la )?semaine prochaine"):
        lundi = aujourdhui + dt.timedelta(days=7 - aujourdhui.weekday())
        return lundi, lundi + dt.timedelta(days=4)
    if lu.trouver(r"apres demain"):
        return aujourdhui + dt.timedelta(days=2), None
    if lu.trouver(r"demain"):
        return aujourdhui + dt.timedelta(days=1), None
    m = lu.trouver(r"dans (\d{1,2}) (jours?|semaines?)")
    if m:
        return aujourdhui + dt.timedelta(days=int(m.group(1)) * (7 if m.group(2).startswith("sem") else 1)), None
    m = lu.trouver(r"(?:le )?(\d{1,2}) (" + "|".join(MOIS) + r")(?: (20\d\d))?")
    if m:
        return _jour(int(m.group(1)), MOIS.index(m.group(2)) + 1, m.group(3), aujourdhui), None
    m = re.search(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](20\d\d|\d\d))?\b", lu.texte)
    if m:
        lu.trouver(rf"(?:le )?0?{int(m.group(1)):d} 0?{int(m.group(2)):d}(?: (?:20)?\d\d)?")
        annee = m.group(3) and (m.group(3) if len(m.group(3)) == 4 else "20" + m.group(3))
        return _jour(int(m.group(1)), int(m.group(2)), annee, aujourdhui), None
    m = lu.trouver(r"(?:ce |le )?(" + "|".join(JOURS) + r")(?: prochain)?")
    if m:
        ecart = (JOURS.index(m.group(1)) - aujourdhui.weekday()) % 7 or 7
        return aujourdhui + dt.timedelta(days=ecart), None
    return None, None


def _jour(jour: int, mois: int, annee: str | None, aujourdhui: dt.date) -> dt.date | None:
    try:
        d = dt.date(int(annee) if annee else aujourdhui.year, mois, jour)
    except ValueError:
        return None
    if not annee and d < aujourdhui:  # « le 5 janvier » dit en novembre : l'an prochain
        d = d.replace(year=d.year + 1)
    return d


def trouver_niveaux(lu: _Lecture) -> tuple[list[str], bool]:
    """(années dites dans la phrase, « toutes les années » ?)."""
    toutes = bool(lu.trouver(TOUTES_ANNEES))
    return [n for n in NIVEAUX if n in NIVEAUX_MOTS and lu.trouver(NIVEAUX_MOTS[n])], toutes


def trouver_matieres(lu: _Lecture, noms: list[str]) -> list[str]:
    """Matières des cours classés nommées dans la phrase (nom, sigle, autre nom courant, faute de frappe)."""
    par_concept = {}
    for nom in noms:
        par_concept.setdefault(noms_matieres.concept(nom), nom)
    sigles = {"".join(m[0] for m in pronote.normaliser(nom).split() if len(m) > 2): nom for nom in noms
              if len(pronote.normaliser(nom).split()) >= 3}
    alias = {a: cible for a, cible in pronote.ALIAS.items() if cible in noms}
    trouvees: list[str] = []
    for suite, debut, fin in lu.suites():
        deja = any(i in lu.pris for i, (a, b) in enumerate(lu.places_normes) if a < fin and b > debut)
        premier = _indice(lu, debut)
        colle = lu.places[premier][0] > 0 and lu.texte[lu.places[premier][0] - 1] == "-"
        if deja or colle:  # « sous-réseau » parle d'un sujet, pas de la matière Réseaux
            continue
        nom = par_concept.get(noms_matieres.concept(suite)) or alias.get(suite) or sigles.get(suite)
        if nom is None and " " not in suite and len(suite) >= 6:
            proches = difflib.get_close_matches(noms_matieres.concept(suite), list(par_concept), n=1, cutoff=0.86)
            nom = par_concept[proches[0]] if proches else None
        if nom:
            lu.prendre(debut, fin)
            if nom not in trouvees:
                trouvees.append(nom)
    return trouvees


def deviner_matiere(sujet: str, chercher) -> tuple[str | None, list[str]]:
    """Matière la plus probable d'un sujet d'après les cours qui en parlent : (matière sûre ou None, candidates)."""
    mots = [m for m in pronote.normaliser(sujet).split() if len(m) >= 3 and m not in CREUX]
    if not mots or chercher is None:
        return None, []
    voix: dict[str, int] = {}
    for requete in (" ".join(mots), " or ".join(mots)):
        for doc in chercher(requete) or []:
            if doc.get("matiere") and doc.get("statut", "classe") == "classe":
                voix[doc["matiere"]] = voix.get(doc["matiere"], 0) + 1
        if voix:
            break
    if not voix:
        return None, []
    ordre = sorted(voix, key=lambda m: -voix[m])
    total = sum(voix.values())
    sure = ordre[0] if voix[ordre[0]] / total >= 0.6 else None
    return sure, ordre[:4]


def comprendre(texte: str, choix: list[dict], evaluations: list[dict] | None = None,
               aujourdhui: dt.date | None = None, chercher=None) -> dict:
    """Lit la demande. `choix` : [{"niveau", "matiere", "n"}] des cours classés ; `evaluations` : contrôles
    Pronote à venir ; `chercher(q)` : recherche dans les cours (pour deviner la matière d'un sujet).

    Renvoie de quoi afficher « voilà ce que j'ai compris » : genres, matieres [{"niveau", "matiere"}],
    sujet, date, fin, titre (campagne), incertain {"genres": bool, "matieres": bool}, candidates, pourquoi.
    """
    aujourdhui = aujourdhui or dt.date.today()
    texte = re.sub(r"\s+", " ", texte or "").strip()
    lu = _Lecture(texte)
    noms = sorted({c["matiere"] for c in choix})
    contexte = lu.trouver(CONTEXTES, prendre=False)
    genres = trouver_genres(lu)
    date, fin = trouver_date(lu, aujourdhui)
    niveaux_dits, toutes = trouver_niveaux(lu)
    trouvees = trouver_matieres(lu, noms)
    if contexte:
        lu.prendre(contexte.start(), contexte.end())
    sujet = lu.sujet()
    pourquoi, candidates = [], []
    incertain = {"genres": False, "matieres": False}

    if not trouvees:
        sure, candidates = deviner_matiere(sujet, chercher)
        if sure:
            trouvees = [sure]
            pourquoi.append(f"Tes cours qui parlent de « {sujet} » sont en {sure}.")
        else:
            incertain["matieres"] = True
    if not genres:
        genres, incertain["genres"] = ["fiche"], True

    # Année : celle qui est dite, sinon la plus récente où la matière a des cours ; « toutes les années » = toutes
    choisies = []
    for nom in trouvees:
        annees = [c["niveau"] for c in choix if c["matiere"] == nom and c["niveau"] in NIVEAUX]
        if not annees:
            continue
        if toutes:
            gardees = annees
        else:
            gardees = [n for n in annees if n in niveaux_dits] or [max(annees, key=NIVEAUX.index)]
        choisies += [{"niveau": n, "matiere": nom} for n in sorted(gardees, key=NIVEAUX.index, reverse=True)]

    # Pas de date dite : le prochain contrôle Pronote de la matière
    source_date = "texte" if date else ""
    if date is None and len(trouvees) == 1:
        for ev in evaluations or []:
            if pronote.trouver_matiere(ev.get("matiere") or "", noms) == trouvees[0]:
                date, source_date = ev["le"].astimezone(pronote.PARIS).date(), "pronote"
                pourquoi.append(f"Contrôle de {trouvees[0]} trouvé dans Pronote le {date:%d/%m}.")
                break
    if "plan" in genres and date is None:
        pourquoi.append("Un plan jour par jour a besoin de la date de l'évaluation.")

    titre = ""
    if contexte:
        titre = texte[lu.places[_indice(lu, contexte.start())][0]:lu.places[_indice(lu, contexte.end() - 1)][1]]
        titre = re.sub(r"\b(bts|ds|bac)\b", lambda m: m.group(1).upper() if m.group(1) != "bac" else "Bac", titre, flags=re.I)
        titre = titre[:1].upper() + titre[1:]
    return {"texte": texte, "genres": [g for g in revision.GENRES if g in genres], "matieres": choisies,
            "sujet": sujet, "date": date, "fin": fin, "source_date": source_date, "titre": titre,
            "incertain": incertain, "candidates": candidates, "pourquoi": pourquoi}


def _indice(lu: _Lecture, position: int) -> int:
    return next(i for i, (a, b) in enumerate(lu.places_normes) if a <= position < b)


def phrase(compris: dict) -> str:
    """« Fiche de révision en CEJM (BTS2) sur le contrat, pour le jeudi 19/11 » : ce qui va être créé."""
    supports = " + ".join(revision.COURTS[g] for g in compris["genres"]) or "?"
    par_matiere: dict[str, list[str]] = {}
    for m in compris["matieres"]:
        par_matiere.setdefault(m["matiere"], []).append(m["niveau"])
    ou = ", ".join(f"{nom} ({', '.join(annees)})" for nom, annees in par_matiere.items()) or "?"
    t = f"{supports} en {ou}"
    if compris["sujet"]:
        t += f" sur « {compris['sujet']} »"
    if compris["date"]:
        t += f", pour le {revision.JOURS[compris['date'].weekday()]} {compris['date']:%d/%m}"
    return t
