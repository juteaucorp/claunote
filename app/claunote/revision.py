"""Génération de supports de révision à partir des transcriptions.

Cinq genres : fiche de révision, quiz (QCM), flashcards, épreuve blanche (sujet + corrigé)
et plan de révision jusqu'à une évaluation. Une campagne regroupe plusieurs révisions
(ex. une semaine d'épreuves blanches) et un programme jour par jour. Les flashcards se révisent ensuite en répétition espacée (SM-2) ou dans Anki.
Les documents sont numérotés [D1], [D2]… dans le prompt ; Claude doit citer ces
repères pour que le site puisse renvoyer vers le cours d'origine.
"""
import datetime as dt
import json
import re
import tempfile
from pathlib import Path

from claunote.agent import ErreurAgent, demander
from claunote.rangement import slug

GENRES = {"fiche": "Fiche de révision", "quiz": "Quiz", "cartes": "Flashcards", "examen": "Épreuve blanche",
          "plan": "Plan de révision"}
COURTS = {"fiche": "Fiche", "quiz": "Quiz", "cartes": "Flashcards", "examen": "Épreuve blanche", "plan": "Plan"}
CORRIGE = "## Corrigé"  # sépare le sujet d'une épreuve blanche de son corrigé
BUDGET = 120_000  # caractères de cours envoyés au maximum
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
                                 consigne=bloc_consigne(consigne), web_md=WEB_MD, web_json=WEB_JSON,
                                 note_web=NOTE_WEB_JSON)


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
            consigne: str = "") -> tuple[str, dict, list[dict]]:
    """Renvoie (contenu, repères des cours, sources web). Contenu : Markdown, ou JSON pour un quiz et des flashcards.

    L'agent peut chercher sur le web s'il le juge utile ; les cours restent la base.
    """
    if not docs:
        raise ErreurAgent("aucun document à utiliser")
    texte_cours, reperes = contexte(docs)

    def lire(texte: str):
        # Réponse inutilisable : demander() réessaie avec le modèle au-dessus
        if genre == "quiz":
            return json.dumps(parse_quiz(texte), ensure_ascii=False), web_json(texte)
        if genre == "cartes":
            return json.dumps(parse_cartes(texte), ensure_ascii=False), web_json(texte)
        if len(texte.strip()) < 80:
            raise ErreurAgent(f"réponse trop courte : {texte[:200]!r}")
        return separer_web(texte.strip())

    with tempfile.TemporaryDirectory() as vide:  # aucun fichier local : les cours sont dans l'entrée
        (contenu, web), _ = demander(genre, prompt(genre, sujet, date_eval, consigne=consigne), Path(vide), lire=lire,
                                     outils=OUTILS_WEB, entree=texte_cours, timeout=1200)
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
        programme, _ = demander("programme", prompt_programme(campagne["titre"], campagne["debut"], campagne["fin"]),
                                Path(vide), lire=lambda t: parse_programme(t, campagne["debut"], campagne["fin"]),
                                entree=entree_programme(campagne["consigne"], matieres, evenements), timeout=600)
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
