"""Génération de supports de révision à partir des transcriptions.

Cinq genres : fiche de révision, quiz (QCM), flashcards, épreuve blanche (sujet + corrigé)
et plan de révision jusqu'à une évaluation. Une campagne regroupe plusieurs révisions
(ex. une semaine d'épreuves blanches) et un programme jour par jour. Les flashcards se révisent ensuite en répétition espacée (SM-2) ou dans Anki.
Les documents sont numérotés [D1], [D2]… dans le prompt ; Claude doit citer ces
repères pour que le site puisse renvoyer vers le cours d'origine.
"""
import datetime as dt
import json
import tempfile
from pathlib import Path

from claunote.agent import ErreurAgent, appeler_claude
from claunote.rangement import slug

GENRES = {"fiche": "Fiche de révision", "quiz": "Quiz", "cartes": "Flashcards", "examen": "Épreuve blanche",
          "plan": "Plan de révision"}
COURTS = {"fiche": "Fiche", "quiz": "Quiz", "cartes": "Flashcards", "examen": "Épreuve blanche", "plan": "Plan"}
CORRIGE = "## Corrigé"  # sépare le sujet d'une épreuve blanche de son corrigé
BUDGET = 120_000  # caractères de cours envoyés au maximum
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

COMMUN = """Tu aides un élève (du collège au BTS SIO) à réviser à partir de SES cours, fournis
ci-dessous. Chaque document commence par un repère [D1], [D2]…
Règles :
- N'utilise QUE ces documents. Si une notion manque, ne l'invente pas.
- Cite la source de chaque point important avec son repère, par exemple [D2].
- Écris en français, simplement, sans HTML ni LaTeX (formules en texte : u_n = u_0 × q^n).
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
Réponds uniquement avec la fiche.
""",
    "quiz": COMMUN + """
Fais un quiz de {nb} questions à choix multiples sur « {sujet} ».
Réponds UNIQUEMENT avec un objet JSON, sans texte autour :
{{"questions": [{{"question": "...", "choix": ["...", "...", "...", "..."], "reponse": 0,
  "explication": "...", "source": "D1"}}]}}
`reponse` est l'indice (à partir de 0) de la bonne réponse dans `choix`.
""",
    "cartes": COMMUN + """
Fais {nb_cartes} flashcards sur « {sujet} » : une notion par carte (définition, commande,
formule, date, schéma à expliquer…). Recto : une question courte et précise. Verso : la réponse
en 1 à 3 lignes, sans recopier la question.
Réponds UNIQUEMENT avec un objet JSON, sans texte autour :
{{"cartes": [{{"recto": "...", "verso": "...", "source": "D1"}}]}}
""",
    "examen": COMMUN + """
Fais un sujet d'épreuve blanche sur « {sujet} », comme un vrai sujet d'examen de ce niveau
(contrôle, bac ou BTS selon les cours), faisable en {duree}. En Markdown :
# Titre de l'épreuve
Une ligne avec la durée, le barème total sur 20 et le matériel autorisé.
## Exercice 1 (n points) … et ainsi de suite : des questions progressives, avec un barème par question,
appuyées sur les notions et exercices des cours (cite les repères [Dn] seulement dans le corrigé).
Puis une section qui commence exactement par la ligne « """ + CORRIGE + """ » : la réponse rédigée de chaque
question, le barème détaillé et les erreurs qui coûtent des points.
Réponds uniquement avec le sujet puis le corrigé.
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


def bloc_consigne(consigne: str) -> str:
    consigne = (consigne or "").strip()
    return ("- Consigne de l'élève, à respecter en priorité (sans inventer de contenu absent des cours) :\n"
            + "\n".join(f"  > {ligne}" for ligne in consigne.splitlines()) + "\n") if consigne else ""


def prompt(genre: str, sujet: str, date_eval: dt.date | None = None, aujourdhui: dt.date | None = None,
           consigne: str = "") -> str:
    if genre not in PROMPTS:
        raise ValueError(f"genre inconnu : {genre}")
    if genre == "plan" and date_eval is None:
        raise ValueError("un plan de révision demande une date d'évaluation")
    aujourdhui = aujourdhui or dt.date.today()
    return PROMPTS[genre].format(sujet=sujet, nb=10, nb_cartes=20, aujourdhui=date_fr(aujourdhui),
                                 date_eval=date_fr(date_eval) if date_eval else "", duree="2 heures",
                                 consigne=bloc_consigne(consigne))


def separer_corrige(texte: str) -> tuple[str, str]:
    """Sujet et corrigé d'une épreuve blanche (corrigé vide si l'IA a oublié la section)."""
    i = texte.find(CORRIGE)
    return (texte, "") if i < 0 else (texte[:i].rstrip(), texte[i:])


def _liste_json(texte: str, cle: str, quoi: str) -> list:
    debut, fin = texte.find("{"), texte.rfind("}")
    try:
        return list(json.loads(texte[debut:fin + 1])[cle])
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
            consigne: str = "") -> tuple[str, dict]:
    """Renvoie (contenu, repères). Contenu : Markdown, ou JSON pour un quiz et des flashcards."""
    if not docs:
        raise ErreurAgent("aucun document à utiliser")
    texte_cours, reperes = contexte(docs)
    with tempfile.TemporaryDirectory() as vide:  # aucun fichier accessible : tout est dans l'entrée
        texte = appeler_claude(prompt(genre, sujet, date_eval, consigne=consigne), Path(vide),
                               entree=texte_cours, timeout=900)
    if genre == "quiz":
        return json.dumps(parse_quiz(texte), ensure_ascii=False), reperes
    if genre == "cartes":
        return json.dumps(parse_cartes(texte), ensure_ascii=False), reperes
    return texte.strip(), reperes


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
- Respecte en priorité le contexte de l'élève.
Réponds UNIQUEMENT avec un objet JSON, sans texte autour :
{{"conseil": "une phrase d'encouragement et de méthode", "jours": [{{"date": "AAAA-MM-JJ", "titre": "...",
  "taches": [{{"matiere": "...", "quoi": "...", "duree": 45, "support": "fiche"}}]}}]}}
`duree` est en minutes.
"""


def prompt_programme(titre: str, debut: dt.date, fin: dt.date, aujourdhui: dt.date | None = None) -> str:
    return PROMPT_PROGRAMME.format(titre=titre, debut=date_fr(debut), fin=date_fr(fin),
                                   aujourdhui=date_fr(aujourdhui or dt.date.today()),
                                   supports=", ".join(f'"{g}"' for g in GENRES if g != "plan"))


def entree_programme(consigne: str, matieres: list[dict], evenements: list[dict]) -> str:
    """Texte fourni au programme : contexte, puis pour chaque matière ses supports et ses cours.

    `matieres` : [{"matiere", "niveau", "sujet", "supports": [genres], "cours": [titres]}].
    """
    parties = [f"Contexte donné par l'élève :\n{consigne.strip() or '(aucun)'}", "Matières :"]
    for m in matieres:
        supports = ", ".join(GENRES[g] for g in m["supports"]) or "aucun"
        cours = "; ".join(m["cours"][:25]) or "?"
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
        for i, t in enumerate(j["taches"]):
            cle = f"{j['date']}:{i}"
            lien = None
            if t["support"]:
                lien = supports.get((t["matiere"], t["support"])) or next(
                    (r for (m, g), r in supports.items() if g == t["support"] and m and
                     (m.lower() in t["matiere"].lower() or t["matiere"].lower() in m.lower())), None)
            taches.append({**t, "cle": cle, "fait": cle in faits, "revision": lien})
        jours.append({"date": date, "libelle": f"{JOURS[date.weekday()]} {date:%d/%m}", "titre": j["titre"],
                      "taches": taches, "minutes": sum(t["duree"] for t in taches)})
    return jours


def generer_programme(campagne: dict, matieres: list[dict], evenements: list[dict]) -> dict:
    with tempfile.TemporaryDirectory() as vide:
        texte = appeler_claude(prompt_programme(campagne["titre"], campagne["debut"], campagne["fin"]), Path(vide),
                               entree=entree_programme(campagne["consigne"], matieres, evenements), timeout=600)
    return parse_programme(texte, campagne["debut"], campagne["fin"])


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


def repetition(facilite: float, intervalle: int, repetitions: int, note: int,
               aujourdhui: dt.date | None = None) -> tuple[float, int, int, dt.date]:
    """Nouvel état d'une carte après une réponse notée 0 (raté) à 5 (facile).

    Renvoie (facilité, intervalle en jours, répétitions réussies, prochaine date).
    """
    if note not in NOTES:
        raise ValueError(f"note inconnue : {note}")
    aujourdhui = aujourdhui or dt.date.today()
    if note < 3:
        repetitions, intervalle = 0, 0  # ratée : on la revoit aujourd'hui même
    else:
        intervalle = 1 if repetitions == 0 else 3 if repetitions == 1 else round(intervalle * facilite)
        if note == 5:
            intervalle = round(intervalle * 1.3) + 1
        repetitions += 1
    facilite = max(1.3, facilite + 0.1 - (5 - note) * (0.08 + (5 - note) * 0.02))
    return round(facilite, 2), intervalle, repetitions, aujourdhui + dt.timedelta(days=intervalle)
