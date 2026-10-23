"""Génération de supports de révision à partir des transcriptions.

Trois genres : fiche de révision, quiz (QCM) et plan de révision jusqu'à une évaluation.
Les documents sont numérotés [D1], [D2]… dans le prompt ; Claude doit citer ces
repères pour que le site puisse renvoyer vers le cours d'origine.
"""
import datetime as dt
import json
import tempfile
from pathlib import Path

from claunote.agent import ErreurAgent, appeler_claude
from claunote.rangement import slug

GENRES = {"fiche": "Fiche de révision", "quiz": "Quiz", "plan": "Plan de révision"}
BUDGET = 120_000  # caractères de cours envoyés au maximum
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

COMMUN = """Tu aides un élève (du collège au BTS SIO) à réviser à partir de SES cours, fournis
ci-dessous. Chaque document commence par un repère [D1], [D2]…
Règles :
- N'utilise QUE ces documents. Si une notion manque, ne l'invente pas.
- Cite la source de chaque point important avec son repère, par exemple [D2].
- Écris en français, simplement, sans HTML.
"""

PROMPTS = {
    "fiche": COMMUN + """
Fais une fiche de révision sur « {sujet} », en Markdown, avec ces sections :
## L'essentiel (5 lignes maximum)
## Définitions
## Formules, commandes ou règles à connaître
## Méthodes (étapes numérotées)
## Pièges fréquents
## Pour s'entraîner (3 questions, puis les réponses dans une section « Réponses »)
Réponds uniquement avec la fiche.
""",
    "quiz": COMMUN + """
Fais un quiz de {nb} questions à choix multiples sur « {sujet} ».
Réponds UNIQUEMENT avec un objet JSON, sans texte autour :
{{"questions": [{{"question": "...", "choix": ["...", "...", "...", "..."], "reponse": 0,
  "explication": "...", "source": "D1"}}]}}
`reponse` est l'indice (à partir de 0) de la bonne réponse dans `choix`.
""",
    "plan": COMMUN + """
L'évaluation sur « {sujet} » a lieu le {date_eval}. Aujourd'hui nous sommes le {aujourdhui}.
Fais un plan de révision en Markdown, jour par jour jusqu'à la veille de l'évaluation :
pour chaque jour, un titre « ### {{jour}} {{date}} », 20 à 45 minutes de travail, les notions
à revoir avec leur repère [Dn] et un petit exercice. Garde le dernier jour pour une révision
globale. Termine par une liste des 5 notions les plus probables à l'évaluation.
""",
}


def contexte(docs: list[dict]) -> tuple[str, dict[str, int]]:
    """Texte des cours numérotés [D1]… et correspondance repère → id du document."""
    parties, reperes, reste = [], {}, BUDGET
    for i, d in enumerate(docs, start=1):
        if reste <= 0:
            break
        repere = f"D{i}"
        reperes[repere] = d["id"]
        entete = f"[{repere}] {d.get('titre') or d['nom']} ({d.get('niveau')}, {d.get('matiere')}, {d.get('type')})"
        corps = (d.get("transcription") or "")[:reste]
        reste -= len(corps)
        parties.append(f"{entete}\n{corps}")
    return "\n\n---\n\n".join(parties), reperes


def date_fr(d: dt.date) -> str:
    return f"{JOURS[d.weekday()]} {d.strftime('%d/%m/%Y')}"


def prompt(genre: str, sujet: str, date_eval: dt.date | None = None, aujourdhui: dt.date | None = None) -> str:
    if genre not in PROMPTS:
        raise ValueError(f"genre inconnu : {genre}")
    if genre == "plan" and date_eval is None:
        raise ValueError("un plan de révision demande une date d'évaluation")
    aujourdhui = aujourdhui or dt.date.today()
    return PROMPTS[genre].format(sujet=sujet, nb=10, aujourdhui=date_fr(aujourdhui),
                                 date_eval=date_fr(date_eval) if date_eval else "")


def parse_quiz(texte: str) -> list[dict]:
    debut, fin = texte.find("{"), texte.rfind("}")
    try:
        questions = json.loads(texte[debut:fin + 1])["questions"]
    except (ValueError, KeyError, TypeError) as e:
        raise ErreurAgent(f"quiz illisible : {texte[:200]!r}") from e
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


def generer(genre: str, sujet: str, docs: list[dict], date_eval: dt.date | None = None) -> tuple[str, dict]:
    """Renvoie (contenu, repères). Contenu : Markdown, ou JSON pour un quiz."""
    if not docs:
        raise ErreurAgent("aucun document à utiliser")
    texte_cours, reperes = contexte(docs)
    with tempfile.TemporaryDirectory() as vide:  # aucun fichier accessible : tout est dans l'entrée
        texte = appeler_claude(prompt(genre, sujet, date_eval), Path(vide), entree=texte_cours, timeout=900)
    if genre == "quiz":
        return json.dumps(parse_quiz(texte), ensure_ascii=False), reperes
    return texte.strip(), reperes


def nom_fichier(genre: str, sujet: str, quand: dt.date) -> str:
    base = slug(sujet)[:50] or "revision"
    return f"{quand.isoformat()}_{genre}_{base}.md"


def quiz_en_markdown(questions: list[dict]) -> str:
    lignes = []
    for i, q in enumerate(questions, start=1):
        lignes.append(f"### {i}. {q['question']}")
        lignes += [f"- {chr(65 + j)}. {c}" for j, c in enumerate(q["choix"])]
        lignes.append(f"\n**Réponse : {chr(65 + q['reponse'])}.** {q['explication']}\n")
    return "\n".join(lignes)
