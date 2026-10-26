"""Génération de supports de révision à partir des transcriptions.

Cinq genres : fiche de révision, quiz (QCM), flashcards, épreuve blanche (sujet + corrigé)
et plan de révision jusqu'à une évaluation. Une campagne regroupe plusieurs révisions
(ex. une semaine d'épreuves blanches) et un programme jour par jour. Les flashcards se révisent ensuite en répétition espacée (FSRS) ou dans Anki.
Les documents sont numérotés [D1], [D2]… dans le prompt ; Claude doit citer ces
repères pour que le site puisse renvoyer vers le cours d'origine.

Pas de limite de cours : au-delà de BUDGET caractères, les cours sont condensés par groupes
(une passe IA par groupe, mémorisée), puis le support est fait à partir de tous les résumés :

    cours D1…D80 ──► groupe 1 ─► condensé 1 ─┐
                 ──► groupe 2 ─► condensé 2 ─┼─► fiche / quiz / épreuve…
                 ──► groupe 3 ─► condensé 3 ─┘

Des pièces jointes (emploi du temps, dates d'épreuves…) peuvent accompagner la consigne :
c'est du contexte, pas des cours.
"""
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

import fsrs

from claunote import agent
from claunote.agent import ErreurAgent, demander
from claunote.rangement import slug

GENRES = {"fiche": "Fiche de révision", "quiz": "Quiz", "cartes": "Flashcards", "examen": "Épreuve blanche",
          "plan": "Plan de révision"}
COURTS = {"fiche": "Fiche", "quiz": "Quiz", "cartes": "Flashcards", "examen": "Épreuve blanche", "plan": "Plan"}
GENRES_TEXTE = ("fiche", "examen", "plan")  # supports en Markdown : carte mentale possible
CORRIGE = "## Corrigé"  # sépare le sujet d'une épreuve blanche de son corrigé
BUDGET = 120_000  # caractères de cours lus d'un coup ; au-delà, les cours sont condensés par groupes
GROUPE = 90_000   # caractères de cours par passe de condensation
NIVEAUX_CONDENSATION = 3  # condensés de condensés au plus, puis on coupe
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

SOURCES_WEB = "## Sources web"  # fin d'un support Markdown : les pages web consultées
OUTILS_WEB = ["WebSearch", "WebFetch"]

COMMUN = """Tu aides un élève (du collège au BTS SIO) à réviser à partir de SES cours, fournis
ci-dessous. Chaque document commence par un repère [D1], [D2]…
Règles :
- Ses cours d'abord : ils sont la base de tout, cite la source de chaque point important avec
  son repère, par exemple [D2]. N'invente rien.
- Le web seulement si c'est utile, à toi d'en juger (WebSearch, WebFetch) : une notion des cours
  incomplète ou illisible, un point à confirmer (définition, chiffre, article de loi, commande,
  date), une erreur probable dans les cours, ou le format officiel d'une épreuve. Si les cours
  suffisent, ne cherche pas. Le web complète ou corrige, il ne remplace jamais les cours : en cas
  de désaccord, garde les deux et signale-le (« Attention : le cours dit…, la source officielle dit… »).
- Sources fiables seulement (sites officiels, éducation nationale, documentation officielle,
  Légifrance, Wikipédia en dernier recours). Marque chaque apport du web par [W1], [W2]…
- Écris en français, simplement, sans HTML. Formules de maths, de physique ou de chimie en LaTeX entre
  $…$ (ex. $u_n = u_0 \\times q^n$, $\\ce{{H2O}}$) : le site les affiche proprement.
{consigne}"""

PROMPTS = {
    "fiche": COMMUN + """
Fais une fiche de révision sur « {sujet} », en Markdown, avec ces sections :
## L'essentiel (5 lignes maximum)
## Définitions
## Formules, commandes ou règles à connaître
## Méthodes (étapes numérotées)
## Pièges fréquents
## Pour s'entraîner (3 questions, puis les réponses dans une section « Réponses »)
Réponds uniquement avec la fiche{web_md}.
""",
    "quiz": COMMUN + """
Fais un quiz de {nb} questions à choix multiples sur « {sujet} ».
Réponds UNIQUEMENT avec un objet JSON, sans texte autour :
{{"questions": [{{"question": "...", "choix": ["...", "...", "...", "..."], "reponse": 0,
  "explication": "...", "source": "D1"}}]{web_json}}}
`reponse` est l'indice (à partir de 0) de la bonne réponse dans `choix`.
{note_web}""",
    "cartes": COMMUN + """
Fais {nb_cartes} flashcards sur « {sujet} » : une notion par carte (définition, commande,
formule, date, schéma à expliquer…). Recto : une question courte et précise. Verso : la réponse
en 1 à 3 lignes, sans recopier la question.
Réponds UNIQUEMENT avec un objet JSON, sans texte autour :
{{"cartes": [{{"recto": "...", "verso": "...", "source": "D1"}}]{web_json}}}
{note_web}""",
    "examen": COMMUN + """
Fais un sujet d'épreuve blanche sur « {sujet} », comme un vrai sujet d'examen de ce niveau
(contrôle, bac ou BTS selon les cours), faisable en {duree}. En Markdown :
# Titre de l'épreuve
Une ligne avec la durée, le barème total sur 20 et le matériel autorisé.
## Exercice 1 (n points) … et ainsi de suite : des questions progressives, avec un barème par question,
appuyées sur les notions et exercices des cours (cite les repères [Dn] seulement dans le corrigé).
Puis une section qui commence exactement par la ligne « """ + CORRIGE + """ » : la réponse rédigée de chaque
question, le barème détaillé et les erreurs qui coûtent des points.
Réponds uniquement avec le sujet puis le corrigé{web_md}.
""",
    "plan": COMMUN + """
L'évaluation sur « {sujet} » a lieu le {date_eval}. Aujourd'hui nous sommes le {aujourdhui}.
Fais un plan de révision en Markdown, jour par jour jusqu'à la veille de l'évaluation :
pour chaque jour, un titre « ### {{jour}} {{date}} », 20 à 45 minutes de travail, les notions
à revoir avec leur repère [Dn] et un petit exercice. Garde le dernier jour pour une révision
globale. Termine par une liste des 5 notions les plus probables à l'évaluation{web_md}.
""",
}


def blocs_cours(docs: list[dict]) -> tuple[list[str], dict[str, int]]:
    """Un bloc par cours, numéroté [D1]…, et correspondance repère → id du document."""
    blocs, reperes = [], {}
    for i, d in enumerate(docs, start=1):
        repere = f"D{i}"
        reperes[repere] = d["id"]
        entete = f"[{repere}] {d.get('titre') or d['nom']} ({d.get('niveau')}, {d.get('matiere')}, {d.get('type')})"
        blocs.append(f"{entete}\n{d.get('transcription') or ''}")
    return blocs, reperes


SEPARATEUR = "\n\n---\n\n"


def grouper(blocs: list[str], taille: int | None = None) -> list[str]:
    """Regroupe les blocs en paquets d'au plus `taille` caractères ; un bloc trop long est coupé en suites."""
    taille, morceaux = taille or GROUPE, []
    for bloc in blocs:
        entete = bloc.split("\n", 1)[0]
        if len(bloc) <= taille:
            morceaux.append(bloc)
            continue
        corps, pas = bloc[len(entete) + 1:], taille - len(entete) - 20
        for n, debut in enumerate(range(0, len(corps), pas), start=1):
            morceaux.append(f"{entete} (partie {n})\n{corps[debut:debut + pas]}")
    groupes, courant = [], []
    for m in morceaux:
        if courant and len(SEPARATEUR.join(courant + [m])) > taille:
            groupes.append(SEPARATEUR.join(courant))
            courant = []
        courant.append(m)
    if courant:
        groupes.append(SEPARATEUR.join(courant))
    return groupes


PROMPT_CONDENSE = """Tu condenses une partie des cours d'un élève (du collège au BTS SIO), fournis ci-dessous,
pour qu'un autre agent en fasse ensuite un support de révision sur « {sujet} ». C'est la partie
{n} sur {total} : d'autres parties sont condensées à côté.
Règles :
- Pour chaque document, une section qui commence par sa ligne d'en-tête exacte, ex. « ### [D3] Titre (…) ».
  Un document sans rien d'utile : seulement son en-tête et « (rien d'utile) ».
- Garde tout ce qui sert à réviser : définitions, formules, commandes, règles, méthodes, exemples
  et exercices types (énoncé court + résultat), pièges signalés. Priorité à ce qui touche au sujet.
- Supprime les répétitions, les bavardages, la mise en page et ce qui est hors sujet.
- Vise environ un quart de la longueur d'origine. N'invente rien, n'ajoute rien.
{consigne}Réponds uniquement avec le texte condensé, en Markdown simple, sans HTML ; garde les formules en LaTeX ($…$).
"""


class Memoire:
    """Condensés déjà faits (en mémoire) ; le worker passe db.Condenses() pour les garder entre deux essais."""

    def __init__(self):
        self._d = {}

    def get(self, cle: str) -> str | None:
        return self._d.get(cle)

    def set(self, cle: str, texte: str):
        self._d[cle] = texte


def condenser(groupe: str, sujet: str, n: int, total: int, consigne: str = "", memoire=None) -> str:
    memoire = memoire if memoire is not None else Memoire()
    cle = hashlib.sha256(f"{sujet}\n{consigne}\n{groupe}".encode()).hexdigest()
    deja = memoire.get(cle)
    if deja:
        return deja

    def lire(texte: str) -> str:
        texte = texte.strip()
        if "[D" not in texte:  # sans repère, le support ne pourrait plus citer ses sources
            raise ErreurAgent(f"condensé inutilisable : {texte[:200]!r}")
        return texte

    with tempfile.TemporaryDirectory() as vide:
        texte, _ = demander("condense", PROMPT_CONDENSE.format(sujet=sujet, n=n, total=total,
                                                               consigne=bloc_consigne(consigne)),
                            Path(vide), lire=lire, entree=groupe, timeout=900)
    memoire.set(cle, texte)
    return texte


def contexte(docs: list[dict], sujet: str = "", consigne: str = "", memoire=None,
             progres=lambda texte: None) -> tuple[str, dict[str, int], int]:
    """Texte des cours numérotés [D1]…, repères, et nombre de groupes condensés (0 si tout tient d'un coup)."""
    blocs, reperes = blocs_cours(docs)
    texte, condenses = SEPARATEUR.join(blocs), 0
    for niveau in range(NIVEAUX_CONDENSATION):
        if len(texte) <= BUDGET:
            break
        groupes = grouper(blocs)
        blocs = []
        for n, g in enumerate(groupes, start=1):
            progres(f"cours condensés {n}/{len(groupes)}" + (f" (passe {niveau + 1})" if niveau else ""))
            blocs.append(condenser(g, sujet, n, len(groupes), consigne, memoire))
        condenses += len(groupes)
        texte = SEPARATEUR.join(blocs)
    return texte[:BUDGET], reperes, condenses


# --- Pièces jointes (contexte donné par l'élève) ----------------------------------

IMAGES_PJ = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
HEIC_PJ = {".heic", ".heif"}
TEXTES_PJ = {".txt", ".md", ".csv"}
EXTENSIONS_PJ = IMAGES_PJ | HEIC_PJ | TEXTES_PJ | {".pdf"} | set(agent.OFFICE) | set(agent.VIEUX_OFFICE)


def preparer_pieces(pieces: list[dict], dossier: Path) -> tuple[list[str], str]:
    """Écrit les pièces que Claude lit lui-même (images, PDF) dans `dossier` et extrait le texte des autres.

    Renvoie (noms des fichiers à lire avec Read, texte des pièces bureautiques).
    """
    a_lire, textes = [], []
    for i, p in enumerate(pieces, start=1):
        extension = Path(p["nom"]).suffix.lower()
        if extension not in EXTENSIONS_PJ:
            continue
        chemin = dossier / f"piece{i}_{slug(Path(p['nom']).stem)[:40] or 'fichier'}{extension}"
        chemin.write_bytes(bytes(p["contenu"]))
        try:
            if extension in TEXTES_PJ:
                textes.append(f"Pièce jointe « {p['nom']} » :\n{chemin.read_text(errors='replace')[:30_000]}")
            elif extension in agent.OFFICE:
                textes.append(f"Pièce jointe « {p['nom']} » :\n{agent.texte_office(chemin)[:30_000]}")
            elif extension in agent.VIEUX_OFFICE:
                textes.append(f"Pièce jointe « {p['nom']} » :\n{agent.texte_vieux_office(chemin)[:30_000]}")
            else:
                a_lire.append((agent.heic_en_jpeg(chemin) if extension in HEIC_PJ else chemin).name)
        except Exception as e:  # une pièce illisible ne bloque pas le support
            textes.append(f"Pièce jointe « {p['nom']} » : illisible ({e})")
    return a_lire, "\n\n".join(textes)


def consigne_pieces(a_lire: list[str]) -> str:
    if not a_lire:
        return ""
    return ("- L'élève a joint des fichiers de contexte dans le dossier courant (emploi du temps, dates d'épreuves, "
            "consignes…). Lis-les avec l'outil Read avant tout : " + ", ".join(f"`{n}`" for n in a_lire) +
            ". Ce ne sont pas des cours : ne les cite pas avec un repère [Dn].\n")


def entree_avec_pieces(texte: str, texte_pieces: str) -> str:
    if not texte_pieces:
        return texte
    return f"Fichiers joints par l'élève (contexte, pas des cours) :\n{texte_pieces}\n\n=====\n\nCours :\n\n{texte}"


def date_fr(d: dt.date) -> str:
    return f"{JOURS[d.weekday()]} {d.strftime('%d/%m/%Y')}"


def bloc_consigne(consigne: str) -> str:
    consigne = (consigne or "").strip()
    return ("- Consigne de l'élève, à respecter en priorité (sans inventer de contenu absent des cours) :\n"
            + "\n".join(f"  > {ligne}" for ligne in consigne.splitlines()) + "\n") if consigne else ""


def prompt(genre: str, sujet: str, date_eval: dt.date | None = None, aujourdhui: dt.date | None = None,
           consigne: str = "", fichiers: list[str] | None = None) -> str:
    if genre not in PROMPTS:
        raise ValueError(f"genre inconnu : {genre}")
    if genre == "plan" and date_eval is None:
        raise ValueError("un plan de révision demande une date d'évaluation")
    aujourdhui = aujourdhui or dt.date.today()
    return PROMPTS[genre].format(sujet=sujet, nb=10, nb_cartes=20, aujourdhui=date_fr(aujourdhui),
                                 date_eval=date_fr(date_eval) if date_eval else "", duree="2 heures",
                                 consigne=bloc_consigne(consigne) + consigne_pieces(fichiers or []),
                                 web_md=WEB_MD, web_json=WEB_JSON, note_web=NOTE_WEB_JSON)


WEB_MD = (f", puis, seulement si tu as utilisé le web, une dernière section « {SOURCES_WEB} » avec une ligne "
          "par page : « - [W1] Titre de la page — https://… »")
WEB_JSON = ', "web": [{"repere": "W1", "titre": "...", "url": "https://..."}]'
NOTE_WEB_JSON = 'Mets la clé "web" seulement si tu as utilisé le web ; "source" peut alors valoir "W1".\n'


def _web_propre(sources) -> list[dict]:
    propres = []
    for w in sources if isinstance(sources, list) else []:
        if isinstance(w, dict) and re.fullmatch(r"W\d+", str(w.get("repere") or "")) \
                and str(w.get("url") or "").startswith(("https://", "http://")):
            propres.append({"repere": w["repere"], "titre": str(w.get("titre") or w["url"]).strip()[:200],
                            "url": str(w["url"]).strip()})
    return propres


def separer_web(texte: str) -> tuple[str, list[dict]]:
    """Retire la section « Sources web » d'un support Markdown et la renvoie en liste."""
    i = texte.rfind(SOURCES_WEB)
    if i < 0:
        return texte, []
    sources = [{"repere": m.group(1), "titre": m.group(2).strip(" -—–:"), "url": m.group(3)}
               for m in re.finditer(r"\[(W\d+)\]\s*(.*?)\s*(https?://\S+?)[)>.,]*$", texte[i:], re.M)]
    return texte[:i].rstrip(), _web_propre(sources)


def web_json(texte: str) -> list[dict]:
    debut, fin = texte.find("{"), texte.rfind("}")
    try:
        return _web_propre(json.loads(texte[debut:fin + 1]).get("web"))
    except (ValueError, AttributeError):
        return []


def separer_corrige(texte: str) -> tuple[str, str]:
    """Sujet et corrigé d'une épreuve blanche (corrigé vide si l'IA a oublié la section)."""
    i = texte.find(CORRIGE)
    return (texte, "") if i < 0 else (texte[:i].rstrip(), texte[i:])


def _latex_dans_json(texte: str) -> str:
    """Double les \\ des formules $…$ oubliés dans le JSON : « \\frac » y serait lu comme un saut de page + « rac »."""
    return re.sub(r"\$[^$\n]*\$", lambda m: re.sub(r"(?<!\\)\\(?=[A-Za-z])", r"\\\\", m.group(0)), texte)


def _liste_json(texte: str, cle: str, quoi: str) -> list:
    debut, fin = texte.find("{"), texte.rfind("}")
    try:
        return list(json.loads(_latex_dans_json(texte[debut:fin + 1]))[cle])
    except (ValueError, KeyError, TypeError) as e:
        raise ErreurAgent(f"{quoi} illisible : {texte[:200]!r}") from e


def parse_quiz(texte: str) -> list[dict]:
    questions = _liste_json(texte, "questions", "quiz")
    propres = []
    for q in questions:
        choix = [str(c) for c in q.get("choix") or []]
        try:
            rep = int(q.get("reponse"))
        except (TypeError, ValueError):
            continue
        if q.get("question") and len(choix) >= 2 and 0 <= rep < len(choix):
            propres.append({"question": str(q["question"]), "choix": choix, "reponse": rep,
                            "explication": str(q.get("explication") or ""),
                            "source": str(q.get("source") or "")})
    if not propres:
        raise ErreurAgent("le quiz ne contient aucune question valide")
    return propres


def parse_cartes(texte: str) -> list[dict]:
    propres = []
    for k in _liste_json(texte, "cartes", "flashcards"):
        if isinstance(k, dict) and str(k.get("recto") or "").strip() and str(k.get("verso") or "").strip():
            propres.append({"recto": str(k["recto"]).strip(), "verso": str(k["verso"]).strip(),
                            "source": str(k.get("source") or "")})
    if not propres:
        raise ErreurAgent("aucune flashcard valide")
    return propres


def generer(genre: str, sujet: str, docs: list[dict], date_eval: dt.date | None = None,
            consigne: str = "", pieces: list[dict] | None = None, memoire=None,
            progres=lambda texte: None) -> tuple[str, dict, list[dict]]:
    """Renvoie (contenu, repères des cours, sources web). Contenu : Markdown, ou JSON pour un quiz et des flashcards.

    L'agent peut chercher sur le web s'il le juge utile ; les cours restent la base.
    Beaucoup de cours : condensés d'abord par groupes (voir contexte). `pieces` : [{"nom", "contenu"}].
    """
    if not docs:
        raise ErreurAgent("aucun document à utiliser")
    texte_cours, reperes, _ = contexte(docs, sujet, consigne, memoire, progres)

    def lire(texte: str):
        # Réponse inutilisable : demander() réessaie avec le modèle au-dessus
        if genre == "quiz":
            return json.dumps(parse_quiz(texte), ensure_ascii=False), web_json(texte)
        if genre == "cartes":
            return json.dumps(parse_cartes(texte), ensure_ascii=False), web_json(texte)
        if len(texte.strip()) < 80:
            raise ErreurAgent(f"réponse trop courte : {texte[:200]!r}")
        return separer_web(texte.strip())

    with tempfile.TemporaryDirectory() as dossier:  # les cours sont dans l'entrée, les pièces jointes ici
        a_lire, texte_pieces = preparer_pieces(pieces or [], Path(dossier))
        progres("rédaction")
        (contenu, web), _ = demander(genre, prompt(genre, sujet, date_eval, consigne=consigne, fichiers=a_lire),
                                     Path(dossier), lire=lire, outils=OUTILS_WEB + (["Read"] if a_lire else []),
                                     entree=entree_avec_pieces(texte_cours, texte_pieces), timeout=1200)
    return contenu, reperes, web


def nom_fichier(genre: str, sujet: str, quand: dt.date, extension: str = "md") -> str:
    base = slug(sujet)[:50] or "revision"
    return f"{quand.isoformat()}_{genre}_{base}.{extension}"


# --- Programme d'une campagne --------------------------------------------------

PROMPT_PROGRAMME = """Tu organises les révisions d'un élève (du collège au BTS SIO) pour « {titre} ».
Le programme couvre chaque jour du {debut} au {fin} (aujourd'hui : {aujourdhui}).
Les informations (contexte de l'élève, matières, supports déjà préparés, évaluations Pronote) sont
fournies ci-dessous. Règles :
- Un jour par date, sans en sauter ; 1 à 4 tâches par jour, 30 min à 3 h au total en semaine
  (plus le week-end si le contexte le demande). Une épreuve blanche se fait en conditions réelles,
  à la date que donne le contexte s'il en donne une.
- Chaque tâche dit concrètement quoi faire, et `support` indique le support préparé à utiliser
  parmi : {supports} (ou "" si aucun).
- Alterne les matières, garde de la place pour relire les corrigés et refaire les erreurs.
- Respecte en priorité le contexte de l'élève (emploi du temps, dates d'épreuves, disponibilités).
{pieces}Réponds UNIQUEMENT avec un objet JSON, sans texte autour :
{{"conseil": "une phrase d'encouragement et de méthode", "jours": [{{"date": "AAAA-MM-JJ", "titre": "...",
  "taches": [{{"matiere": "...", "quoi": "...", "duree": 45, "support": "fiche"}}]}}]}}
`duree` est en minutes.
"""


def prompt_programme(titre: str, debut: dt.date, fin: dt.date, aujourdhui: dt.date | None = None,
                     fichiers: list[str] | None = None) -> str:
    return PROMPT_PROGRAMME.format(titre=titre, debut=date_fr(debut), fin=date_fr(fin),
                                   pieces=consigne_pieces(fichiers or []),
                                   aujourdhui=date_fr(aujourdhui or dt.date.today()),
                                   supports=", ".join(f'"{g}"' for g in GENRES if g != "plan"))


def entree_programme(consigne: str, matieres: list[dict], evenements: list[dict]) -> str:
    """Texte fourni au programme : contexte, puis pour chaque matière ses supports et ses cours.

    `matieres` : [{"matiere", "niveau", "sujet", "supports": [genres], "cours": [titres]}].
    """
    parties = [f"Contexte donné par l'élève :\n{consigne.strip() or '(aucun)'}", "Matières :"]
    for m in matieres:
        supports = ", ".join(GENRES[g] for g in m["supports"]) or "aucun"
        cours = "; ".join(m["cours"]) or "?"
        parties.append(f"- {m['matiere']} ({m['niveau']}){' sur « ' + m['sujet'] + ' »' if m.get('sujet') else ''}"
                       f" : supports préparés : {supports}. Cours : {cours}")
    if evenements:
        parties.append("Évaluations et devoirs Pronote sur la période :")
        parties += [f"- {date_fr(e['le'].date())} : {e['matiere']} ({e['genre']}) {e['titre']}" for e in evenements]
    return "\n".join(parties)


def parse_programme(texte: str, debut: dt.date, fin: dt.date) -> dict:
    """Garde les jours de la période, dans l'ordre, et les tâches complètes."""
    debut_json, fin_json = texte.find("{"), texte.rfind("}")
    try:
        donnees = json.loads(texte[debut_json:fin_json + 1])
        jours_bruts = list(donnees["jours"])
    except (ValueError, KeyError, TypeError) as e:
        raise ErreurAgent(f"programme illisible : {texte[:200]!r}") from e
    jours = {}
    for j in jours_bruts:
        try:
            date = dt.date.fromisoformat(str(j.get("date")))
        except (ValueError, AttributeError):
            continue
        if not debut <= date <= fin or date in jours:
            continue
        taches = []
        for t in j.get("taches") or []:
            if not isinstance(t, dict) or not str(t.get("quoi") or "").strip():
                continue
            try:
                duree = min(max(int(t.get("duree") or 30), 5), 300)
            except (TypeError, ValueError):
                duree = 30
            support = str(t.get("support") or "")
            taches.append({"matiere": str(t.get("matiere") or "").strip(), "quoi": str(t["quoi"]).strip(),
                           "duree": duree, "support": support if support in GENRES else ""})
        jours[date] = {"date": date.isoformat(), "titre": str(j.get("titre") or "").strip(), "taches": taches}
    if not jours:
        raise ErreurAgent("le programme ne contient aucun jour de la période")
    return {"conseil": str(donnees.get("conseil") or "").strip(), "jours": [jours[d] for d in sorted(jours)]}


def jours_programme(programme: dict | None, faits: list[str], revisions: list[dict] | None = None) -> list[dict]:
    """Jours du programme prêts à afficher : libellé, tâches avec leur clé, état et support lié."""
    supports = {}
    for r in revisions or []:
        supports.setdefault((r["matiere"] or "", r["genre"]), r)
        supports.setdefault(("", r["genre"]), r)
    jours = []
    for j in (programme or {}).get("jours", []):
        date = dt.date.fromisoformat(j["date"])
        taches = []
        for i, t in enumerate(j.get("taches") or []):
            # Un programme d'une ancienne version n'a pas toujours tous les champs
            t = {"matiere": "", "quoi": "", "duree": 0, **t, "support": t.get("support") or ""}
            cle = f"{j['date']}:{i}"
            lien = None
            if t["support"]:
                lien = supports.get((t["matiere"], t["support"])) or next(
                    (r for (m, g), r in supports.items() if g == t["support"] and m and
                     (m.lower() in t["matiere"].lower() or t["matiere"].lower() in m.lower())), None)
            taches.append({**t, "cle": cle, "fait": cle in faits, "revision": lien})
        jours.append({"date": date, "libelle": f"{JOURS[date.weekday()]} {date:%d/%m}", "titre": j.get("titre") or "",
                      "taches": taches, "minutes": sum(t["duree"] for t in taches)})
    return jours


def generer_programme(campagne: dict, matieres: list[dict], evenements: list[dict],
                      pieces: list[dict] | None = None) -> dict:
    with tempfile.TemporaryDirectory() as dossier:
        a_lire, texte_pieces = preparer_pieces(pieces or [], Path(dossier))
        entree = entree_programme(campagne["consigne"], matieres, evenements)
        if texte_pieces:
            entree += f"\n\nFichiers joints par l'élève :\n{texte_pieces}"
        programme, _ = demander("programme", prompt_programme(campagne["titre"], campagne["debut"], campagne["fin"],
                                                              fichiers=a_lire),
                                Path(dossier), lire=lambda t: parse_programme(t, campagne["debut"], campagne["fin"]),
                                outils=["Read"] if a_lire else None, entree=entree, timeout=600)
    return programme


def quiz_en_markdown(questions: list[dict]) -> str:
    lignes = []
    for i, q in enumerate(questions, start=1):
        lignes.append(f"### {i}. {q['question']}")
        lignes += [f"- {chr(65 + j)}. {c}" for j, c in enumerate(q["choix"])]
        lignes.append(f"\n**Réponse : {chr(65 + q['reponse'])}.** {q['explication']}\n")
    return "\n".join(lignes)


def cartes_en_markdown(cartes: list[dict]) -> str:
    return "\n".join(f"### {i}. {k['recto']}\n\n{k['verso']}\n" for i, k in enumerate(cartes, start=1))


# --- Répétition espacée (SM-2, comme Anki) -----------------------------------

NOTES = {0: "À revoir", 3: "Difficile", 4: "Bien", 5: "Facile"}
NOTE_FSRS = {0: fsrs.Rating.Again, 3: fsrs.Rating.Hard, 4: fsrs.Rating.Good, 5: fsrs.Rating.Easy}
# Part des cartes qu'on veut encore savoir le jour où elles reviennent : plus haut = plus de répétitions
RETENTION = float(os.environ.get("FSRS_RETENTION", "0.9"))
PARIS = ZoneInfo("Europe/Paris")


def planificateur(hasard: bool = True) -> fsrs.Scheduler:
    """FSRS (l'algorithme d'Anki depuis 2023) : moins de répétitions que SM-2 pour la même mémoire."""
    return fsrs.Scheduler(desired_retention=RETENTION, enable_fuzzing=hasard)


def carte_fsrs(k: dict) -> fsrs.Card:
    """État FSRS d'une carte ; une carte suivie avec l'ancien SM-2 est reprise là où elle en était."""
    if k.get("fsrs"):
        return fsrs.Card.from_dict(k["fsrs"])
    if not k.get("repetitions"):
        return fsrs.Card(card_id=k["id"], due=dt.datetime.now(dt.timezone.utc))
    echeance = dt.datetime.combine(k["prochaine"], dt.time(), PARIS).astimezone(dt.timezone.utc)
    vue = k.get("vue_le") or echeance - dt.timedelta(days=max(k["intervalle"], 1))
    # SM-2 : facilité 2,5 = carte moyenne (difficulté FSRS 5), 1,3 = très dure (10)
    difficulte = min(10.0, max(1.0, 5 + (2.5 - k["facilite"]) * 5))
    return fsrs.Card(card_id=k["id"], state=fsrs.State.Review, stability=float(max(k["intervalle"], 1)),
                     difficulty=difficulte, due=echeance, last_review=vue)


def repetition(k: dict, note: int, maintenant: dt.datetime | None = None, hasard: bool = True) -> dict:
    """Nouvel état d'une carte après une réponse « À revoir » (0), « Difficile » (3), « Bien » (4), « Facile » (5)."""
    if note not in NOTES:
        raise ValueError(f"note inconnue : {note}")
    maintenant = maintenant or dt.datetime.now(dt.timezone.utc)
    p = planificateur(hasard)
    avant = carte_fsrs(k)
    retenue = p.get_card_retrievability(avant, maintenant) if avant.last_review else None
    apres, _ = p.review_card(avant, NOTE_FSRS[note], maintenant)
    return {"fsrs": apres.to_dict(), "echeance": apres.due, "prochaine": apres.due.astimezone(PARIS).date(),
            "intervalle": (apres.due - maintenant).days, "repetitions": (k.get("repetitions") or 0) + (note > 0),
            "stabilite": apres.stability, "difficulte": apres.difficulty, "retenue": retenue}


def delai(k: dict, note: int, maintenant: dt.datetime | None = None) -> str:
    """Quand la carte reviendra avec cette réponse : « 10 min », « demain », « 4 j », « 3 mois »."""
    maintenant = maintenant or dt.datetime.now(dt.timezone.utc)
    ecart = repetition(k, note, maintenant, hasard=False)["echeance"] - maintenant
    minutes = ecart.total_seconds() / 60
    if minutes < 60:
        return f"{max(1, round(minutes))} min"
    if minutes < 20 * 60:
        return f"{round(minutes / 60)} h"
    jours = round(minutes / 1440)
    return "demain" if jours <= 1 else f"{jours} j" if jours < 45 else f"{round(jours / 30)} mois" if jours < 365 \
        else f"{jours / 365:.1f} an".replace(".", ",")
