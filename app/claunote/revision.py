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
import random
import re
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

import fsrs

from claunote import agent, schemas
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
ci-dessous. Chaque document commence par un repère [D1], [D2]… et indique son niveau, sa matière et son type.
Règles :
{regle_cours}- Pertinence avant tout. Avant d'écrire, repère dans les cours : le niveau réel (collège, lycée, BTS),
  le vocabulaire et les notations du professeur (garde-les), et ce sur quoi il insiste (répété, encadré,
  donné en exercice, en TP ou en contrôle) : c'est la priorité. Laisse l'anecdotique et le hors-sujet.
  Couvre tous les cours fournis qui touchent au sujet, pas seulement les premiers.
- Le web seulement si c'est utile, à toi d'en juger (WebSearch, WebFetch) : une notion des cours
  incomplète ou illisible, un point à confirmer (définition, chiffre, article de loi, commande,
  date), une erreur probable dans les cours, ou le format officiel d'une épreuve. Si les cours
  suffisent, ne cherche pas. Le web complète ou corrige, il ne remplace jamais les cours : en cas
  de désaccord, garde les deux et signale-le (« Attention : le cours dit…, la source officielle dit… »).
- Sources fiables seulement (sites officiels, éducation nationale, documentation officielle,
  Légifrance, Wikipédia en dernier recours). Marque chaque apport du web par [W1], [W2]…
- Écris en français, simplement. Formules de maths, de physique ou de chimie en LaTeX entre
  $…$ (ex. $u_n = u_0 \\times q^n$, $\\ce{{H2O}}$) : le site les affiche proprement. Toute formule,
  même un seul symbole, est entre $…$ (jamais de \\frac ou \\sigma hors des $) ; un seul antislash par
  commande ; des accolades autour de chaque argument ($\\frac{{1}}{{2}}$, $\\overline{{a}}$, pas \\frac12) ;
  seulement des commandes courantes (\\frac, \\sqrt, \\overline, \\times, \\leq, \\in, lettres grecques…).
- Un tableau : une ligne vide avant et après, des cellules courtes (quelques mots), 4 colonnes au plus.
{consigne}"""

# Ce que dit le prompt sur les cours, selon le mode choisi dans le formulaire
REGLE_STRICTE = """- Ses cours d'abord : ils sont la base de tout, cite la source de chaque point important avec
  son repère, par exemple [D2]. N'invente rien : reste strictement dans ses cours.
"""
REGLE_COMPLEMENT = """- Ses cours d'abord : ils sont la base de tout, cite la source de chaque point important avec
  son repère, par exemple [D2]. Ne déforme jamais ce qu'ils disent.
- Si ses cours ne couvrent pas tout le sujet demandé (ou pas du tout, ou si certains sont vides ou
  illisibles), ne refuse JAMAIS : complète avec tes propres connaissances, au niveau de l'élève et
  conforme au programme officiel (référentiel du diplôme, ex. BTS SIO). Ce que tu ajoutes ainsi
  doit être reconnaissable au premier coup d'œil :
  - une section entière hors de ses cours : son titre finit par « [IA] » et elle commence par
    l'encadré « > **Hors de tes cours** : … » qui dit en une phrase ce qui manque dans ses cours ;
  - un point isolé : le repère [IA] à la place d'un [Dn] (dans un quiz ou des flashcards, "source": "IA").
  Ne mets jamais [IA] sur ce qui vient de ses cours, ni un [Dn] sur ce que tu as ajouté.
  Rien de ses cours ne touche au sujet : fais tout le support quand même, en commençant par l'encadré
  « > **Hors de tes cours** : … » qui le dit clairement.
"""
NOTIONS_STRICTES = "ni notion absente des cours"
NOTIONS_COMPLEMENT = "et les notions du sujet absentes des cours sont marquées [IA] dans le corrigé"


# Ce que l'IA peut dessiner (rendu par schemas.py, sur le site comme dans le PDF)
VISUEL = """
Mise en page : pas un mur de texte. Tu disposes de vrais outils de dessin, rendus en image sur le site
et dans le PDF ; sers-t'en chaque fois qu'un dessin explique mieux qu'une phrase :
- Schéma automatique (Graphviz), dans un bloc de code « ```dot Légende du schéma » … « ``` » :
  organigramme, étapes d'une méthode, architecture réseau, arbre, cycle, états, MCD, causes et
  conséquences, carte des notions. Écris seulement les nœuds et les liens, par exemple
  `digraph {{ rankdir=LR; pc [label="PC client"]; pc -> srv [label="1. DHCP Discover"] }}` :
  libellés courts entre guillemets ("\\n" pour aller à la ligne), sans couleurs ni polices (le site
  les met), 4 à 14 nœuds. La mise en place est automatique : rien ne se chevauche.
- Dessin libre, dans un bloc « ```svg Légende » … « ``` » : courbe dans un repère, figure de géométrie,
  circuit, frise chronologique, échange de messages entre deux machines, pile de couches, croquis.
  `<svg viewBox="0 0 720 H">` avec des formes simples (rect, circle, line, path, polygon, text),
  texte de taille 14 au moins, traits et textes foncés sur fond clair, deux ou trois couleurs sobres,
  des marges, aucun texte qui en chevauche un autre ou sort du cadre. Pas de <style>, d'image, de lien.
- Tableau Markdown pour comparer (2 à 5 colonnes, cellules courtes).
- Encadrés : une citation qui commence par un mot-clé en gras, « > **À retenir** … », « > **Attention** … »,
  « > **Exemple** … », « > **Méthode** … », « > **Définition** … ».
Un schéma doit être juste, tiré des cours (cite son repère [Dn] dans la phrase qui l'introduit, ou [IA]
s'il vient d'un complément hors de ses cours) et utile :
jamais décoratif. Dans un schéma : texte simple, ni LaTeX ni Markdown. Hors des blocs ```svg, pas de HTML.
"""

PROMPTS = {
    "fiche": COMMUN + VISUEL + """
Fais une FICHE SYNTHÈSE sur « {sujet} », en Markdown : la fiche qu'on relit la veille et le matin de
l'épreuve, très dense, où chaque ligne peut rapporter un point. Ce n'est NI un plan de révision, NI un
sommaire, NI un résumé rédigé : aucune liste de « choses à revoir », aucune introduction, aucune phrase
de liaison. Seulement le contenu à savoir, déjà formulé comme il faudrait l'écrire sur la copie.
Structure :
# SUJET OU MATIÈRE — FICHE SYNTHÈSE (niveau, ex. BTS SIO)
Puis 4 à 8 grandes parties numérotées « ## 1. TITRE EN MAJUSCULES », choisies pour CE sujet et qui le
couvrent en entier (toute la matière si c'est toute la matière qui est demandée) :
- d'abord les réflexes ou les méthodes de l'épreuve, quand les cours ou le format officiel les donnent :
  durée, matériel, comment rédiger chaque type de réponse (ex. « formule → calcul → résultat arrondi →
  phrase », le syllogisme juridique), le sens des verbes de consigne ;
- ensuite une partie par thème ou chapitre : les mots à savoir par cœur, les formules, règles, commandes,
  mécanismes, avec les exemples types à savoir refaire ;
- si la matière s'y prête, une partie récapitulative en tableau (auteurs et dates, articles de loi,
  formulaire, commandes) ;
- avant-dernière partie, toujours : « ## n. PIÈGES », les confusions classiques, une ligne chacune
  (« +5 % → q = 1,05, pas 0,05 », « Nullité (formation) ≠ résolution (exécution) ») ;
- dernière partie, toujours : « ## n. QUESTIONS » : 10 à 15 questions numérotées comme à l'épreuve, de
  difficulté croissante (restitution, puis application avec des données, puis justification), sans
  indice ; puis une section « ## RÉPONSES » : la réponse attendue de chaque question, en une à trois lignes.
Dans chaque partie : des sous-titres « ### » courts, et sous chacun des puces d'une à trois lignes qui
commencent par le mot-clé en gras : « - **Externalité** : effet de l'activité d'un agent sur un autre
**sans compensation par le prix**. »
- Style télégraphique : « → » pour une conséquence ou une étape, « = » pour une définition, « ≠ » pour
  une confusion, « · » pour enchaîner des notions voisines sur la même puce.
- Gras sur les mots-clés et sur ce qui doit apparaître tel quel sur la copie, rien d'autre.
- Un tableau dès qu'on compare (deux notions, deux lois, plusieurs droits) ou pour un formulaire.
- Une méthode : ses étapes numérotées dans une puce (« 1. … 2. … 3. … »), puis un exemple type traité
  avec de vrais nombres. Chaque formule avec un exemple chiffré.
- Les phrases toutes faites à recopier (justification d'une loi, conclusion type) entre « ».
- Un schéma seulement s'il remplace un paragraphe (courbe, arbre, architecture), compact, deux au plus :
  pas de « vue d'ensemble » décorative. Pas d'encadré « > » sauf « Hors de tes cours ».
- Repères : un seul [Dn] à la fin du sous-titre « ### » ou de la puce dont il est la source, pas à
  chaque phrase.
Longueur : dense et complète, l'équivalent d'une à deux pages A4 sur trois colonnes en petits caractères
(900 à 2 000 mots avant les questions, selon l'ampleur du sujet), sans remplissage.
Réponds uniquement avec la fiche{web_md}.
""",
    "quiz": COMMUN + """
Fais un quiz de {nb} questions à choix multiples sur « {sujet} », qui couvre l'ensemble des cours.
- Un tiers de connaissance (définition, formule, date), un tiers de compréhension (pourquoi, différence
  entre deux notions), un tiers d'application (petit cas concret, calcul, lecture d'une commande, d'une
  configuration ou d'un schéma). Difficulté croissante.
- 4 choix par question, une seule bonne réponse, indiscutable d'après les cours. Les mauvaises réponses
  sont des erreurs plausibles (les confusions classiques), de même longueur et de même style que la bonne.
  Jamais « toutes les réponses », « aucune des réponses » ni de double négation.
- Les choix seront mélangés : ne parle jamais de leur lettre ou de leur position.
- `explication` : en 1 à 3 phrases, pourquoi c'est juste et quelle erreur se cache derrière le piège principal.
- `schema`, facultatif : quand la question porte sur un schéma (topologie réseau, courbe, figure, MCD…),
  son code Graphviz (`digraph {{ … }}`) ou SVG (`<svg viewBox="0 0 720 H">…</svg>`, texte de taille 14 au
  moins) dans une chaîne ; le site le dessine au-dessus de la question. Sinon, omets cette clé.
Réponds UNIQUEMENT avec un objet JSON, sans texte autour :
{{"questions": [{{"question": "...", "choix": ["...", "...", "...", "..."], "reponse": 0,
  "explication": "...", "source": "D1"}}]{web_json}}}
`reponse` est l'indice (à partir de 0) de la bonne réponse dans `choix`.
{note_web}""",
    "cartes": COMMUN + """
Fais {nb_cartes} flashcards sur « {sujet} », de l'indispensable au détail, en couvrant tous les cours.
- Une seule information par carte (définition, commande, formule, date, étape, rôle d'un élément). Une
  liste à apprendre se découpe en plusieurs cartes : pas de « citer les 7 couches ».
- Recto : une question courte, précise et compréhensible seule (rappelle le contexte : « En SQL, … »).
  Pas de question à réponse oui/non. Varie les formes : du terme vers sa définition et l'inverse,
  « à quoi sert… », « quelle commande pour… », « que se passe-t-il si… », un exemple à reconnaître.
- Verso : la réponse en 1 à 3 lignes, sans recopier la question, avec un exemple très court si ça aide.
Réponds UNIQUEMENT avec un objet JSON, sans texte autour :
{{"cartes": [{{"recto": "...", "verso": "...", "source": "D1"}}]{web_json}}}
{note_web}""",
    "examen": COMMUN + VISUEL + """
Fais un sujet d'épreuve blanche sur « {sujet} », comme un vrai sujet d'examen de ce niveau, faisable
en {duree}. Repère d'abord l'épreuve visée d'après les cours (contrôle de chapitre, brevet, bac, épreuve
de BTS SIO…) et reprends sa forme ; vérifie le format officiel sur le web si tu as un doute.
En Markdown :
# Titre de l'épreuve
Une ligne avec la durée, le barème total sur 20 et le matériel autorisé.
Pour un BTS, un bac technologique ou une étude de cas : d'abord un contexte réaliste (une organisation,
une situation, une mission), puis des documents ou annexes sur lesquels portent les questions (tableau de
données, extrait de configuration ou de code, schéma réseau ou MCD dessiné en dot, courbe en svg…).
## Exercice 1 (n points) … et ainsi de suite : des questions progressives, avec un barème par question.
- Environ 30 % de restitution, 50 % d'application, 20 % d'analyse ou de justification, avec les verbes
  de consigne d'un vrai sujet (Définir, Calculer, Justifier, Proposer, Comparer…).
- Reprends les types d'exercices des cours avec des données nouvelles : ni copie d'un exercice déjà
  corrigé, {notions_examen}.
- Le total des barèmes fait exactement 20 : vérifie la somme. Le tout tient dans la durée.
- Dans le sujet : aucun indice, aucune réponse, aucun repère [Dn].
Puis une section qui commence exactement par la ligne « """ + CORRIGE + """ » : pour chaque question, la
réponse rédigée attendue (le schéma corrigé si un schéma était demandé), les points accordés à chaque
élément de réponse, les erreurs qui coûtent des points, et le repère [Dn] du cours à revoir ([IA] si la
notion n'est pas dans ses cours). Termine le
corrigé par un tableau « Question ratée | Notion à revoir | Cours [Dn] ».
Réponds uniquement avec le sujet puis le corrigé{web_md}.
""",
    "plan": COMMUN + VISUEL + """
L'évaluation sur « {sujet} » a lieu le {date_eval}. Aujourd'hui nous sommes le {aujourdhui}.
Fais un plan de révision en Markdown, jour par jour jusqu'à la veille de l'évaluation.
Commence par un tableau récapitulatif « Jour | Notions | Durée », puis pour chaque jour un titre
« ### {{jour}} {{date}} », 20 à 45 minutes de travail, les notions à revoir avec leur repère [Dn] et un
petit exercice précis (tiré des cours quand ils en ont).
- Les notions les plus lourdes ou les plus probables d'abord ; chaque notion importante revient au moins
  deux fois, à quelques jours d'écart (répétition espacée).
- Rappel actif : chaque séance commence par se tester de mémoire sur la séance précédente (2 ou 3
  questions que tu écris), avant de relire.
- Garde le dernier jour pour une révision globale légère, sans notion nouvelle.
Termine par une liste des 5 notions les plus probables à l'évaluation{web_md}.
""",
}


def blocs_cours(docs: list[dict]) -> tuple[list[str], dict[str, int]]:
    """Un bloc par cours, numéroté [D1]…, et correspondance repère → id du document."""
    blocs, reperes = [], {}
    for i, d in enumerate(docs, start=1):
        repere = f"D{i}"
        reperes[repere] = d["id"]
        entete = f"[{repere}] {d.get('titre') or d['nom']} ({d.get('niveau')}, {d.get('matiere')}, {d.get('type')})"
        texte = (d.get("transcription") or "").strip() or SANS_TEXTE
        blocs.append(f"{entete}\n{texte}")
    return blocs, reperes


SEPARATEUR = "\n\n---\n\n"
# Document classé à la main sans avoir été lu (analyse en erreur) : le dire plutôt qu'un bloc vide
SANS_TEXTE = "(pas de transcription : ce document n'a pas encore été lu par l'IA)"


def sans_texte(docs: list[dict]) -> list[dict]:
    """Cours sans transcription : l'IA ne peut pas s'en servir (à relancer depuis la page du document)."""
    return [d for d in docs if not (d.get("transcription") or "").strip()]


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
  et exercices types (énoncé court + résultat), pièges signalés, schémas et figures (décris-les
  en une ou deux lignes : éléments, liens, sens des flèches). Priorité à ce qui touche au sujet.
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
                textes.append(f"Pièce jointe « {p['nom']} » :\n{agent.texte_bureautique(chemin)[:30_000]}")
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


def bloc_consigne(consigne: str, strict: bool = True) -> str:
    consigne = (consigne or "").strip()
    reserve = " (sans inventer de contenu absent des cours)" if strict else ""
    return (f"- Consigne de l'élève, à respecter en priorité{reserve} :\n"
            + "\n".join(f"  > {ligne}" for ligne in consigne.splitlines()) + "\n") if consigne else ""


def prompt(genre: str, sujet: str, date_eval: dt.date | None = None, aujourdhui: dt.date | None = None,
           consigne: str = "", fichiers: list[str] | None = None, strict: bool = False) -> str:
    """`strict` : rien hors des cours. Sinon (par défaut), l'IA complète ce qui manque et le marque [IA]."""
    if genre not in PROMPTS:
        raise ValueError(f"genre inconnu : {genre}")
    if genre == "plan" and date_eval is None:
        raise ValueError("un plan de révision demande une date d'évaluation")
    aujourdhui = aujourdhui or dt.date.today()
    return PROMPTS[genre].format(sujet=sujet, nb=10, nb_cartes=20, aujourdhui=date_fr(aujourdhui),
                                 date_eval=date_fr(date_eval) if date_eval else "", duree="2 heures",
                                 consigne=bloc_consigne(consigne, strict) + consigne_pieces(fichiers or []),
                                 regle_cours=REGLE_STRICTE if strict else REGLE_COMPLEMENT,
                                 notions_examen=NOTIONS_STRICTES if strict else NOTIONS_COMPLEMENT,
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


# Choix qui parlent des autres (« les deux », « A et B ») : leur ordre ne doit pas bouger
FIGE = re.compile(r"réponses|ci-dessus|précédent|les deux\b|\b[A-D] et [A-D]\b", re.I)


def parse_quiz(texte: str, melanger: bool = False) -> list[dict]:
    questions = _liste_json(texte, "questions", "quiz")
    propres = []
    for q in questions:
        choix = [str(c) for c in q.get("choix") or []]
        try:
            rep = int(q.get("reponse"))
        except (TypeError, ValueError):
            continue
        if q.get("question") and len(choix) >= 2 and 0 <= rep < len(choix):
            if melanger and not any(FIGE.search(c) for c in choix):
                # L'IA met trop souvent la bonne réponse en premier : l'ordre des choix est tiré au sort
                ordre = random.sample(range(len(choix)), len(choix))
                choix, rep = [choix[i] for i in ordre], ordre.index(rep)
            propre = {"question": str(q["question"]), "choix": choix, "reponse": rep,
                      "explication": str(q.get("explication") or ""), "source": str(q.get("source") or "")}
            schema = str(q.get("schema") or "").strip()
            if schema and schemas.nettoyer(schema if "```" in schema or schema.startswith("<svg")
                                           else f"```dot\n{schema}\n```")[1]:
                propre["schema"] = schema
            propres.append(propre)
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


RE_PUCE = re.compile(r"^ {0,3}(?:[-*+]|\d{1,2}[.)])\s+\S")
RE_SEPARATEUR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$|^\s*\|\s*:?-{2,}:?\s*\|\s*$")


def aerer_listes(texte: str) -> str:
    """Lignes vides là où Markdown en a besoin (hors des blocs de code) :
    - avant une liste collée au paragraphe qui la précède, sinon les puces sortent en un seul bloc ;
    - avant un tableau collé à un paragraphe, sinon il n'est pas reconnu ;
    - après un tableau suivi directement d'une ligne sans « | », sinon cette ligne devient une rangée
      du tableau (puces rangées dans la première colonne, tableau qui déborde de sa colonne) ;
    - après une liste suivie d'un paragraphe, sinon il se colle à la dernière puce."""
    lignes_in = texte.split("\n")
    lignes, dans_code, dans_tableau = [], False, False
    for i, ligne in enumerate(lignes_in):
        cloture = ligne.lstrip().startswith("```")
        if cloture:
            dans_code = not dans_code
            dans_tableau = False
        avant = lignes[-1] if lignes else ""
        suivante = lignes_in[i + 1] if i + 1 < len(lignes_in) else ""
        if not dans_code and not cloture:
            if dans_tableau and ligne.strip() and "|" not in ligne:
                lignes.append("")
                dans_tableau = False
            elif not ligne.strip():
                dans_tableau = False
            if "|" in ligne and RE_SEPARATEUR.match(suivante) and not RE_SEPARATEUR.match(ligne):
                if avant.strip() and "|" not in avant:
                    lignes.append("")
                dans_tableau = True
            elif (RE_PUCE.match(ligne) and avant.strip() and not RE_PUCE.match(avant)
                    and not avant.startswith((" ", "\t", ">", "|", "#"))):
                lignes.append("")
            elif (RE_PUCE.match(avant) and ligne.strip() and not RE_PUCE.match(ligne)
                    and not ligne.startswith((" ", "\t", "|"))):
                lignes.append("")   # paragraphe après une liste : pas la suite de la dernière puce
        lignes.append(ligne)
    return "\n".join(lignes)


def _lecteur(genre: str):
    """Lit la réponse de l'IA ; réponse inutilisable : ErreurAgent, et demander() réessaie avec le modèle au-dessus."""
    def lire(texte: str):
        if genre == "quiz":
            return json.dumps(parse_quiz(texte, melanger=True), ensure_ascii=False), web_json(texte)
        if genre == "cartes":
            return json.dumps(parse_cartes(texte), ensure_ascii=False), web_json(texte)
        if len(texte.strip()) < 80:
            raise ErreurAgent(f"réponse trop courte : {texte[:200]!r}")
        texte, web = separer_web(texte.strip())
        return schemas.nettoyer(aerer_listes(texte))[0], web  # un schéma au code faux est retiré plutôt qu'affiché en brut
    return lire


def generer(genre: str, sujet: str, docs: list[dict], date_eval: dt.date | None = None,
            consigne: str = "", pieces: list[dict] | None = None, memoire=None,
            progres=lambda texte: None, strict: bool = False) -> tuple[str, dict, list[dict]]:
    """Renvoie (contenu, repères des cours, sources web). Contenu : Markdown, ou JSON pour un quiz et des flashcards.

    L'agent peut chercher sur le web s'il le juge utile ; les cours restent la base. Sauf `strict`, ce que
    les cours ne couvrent pas est complété par l'IA et marqué [IA] (pas de refus faute de cours).
    Beaucoup de cours : condensés d'abord par groupes (voir contexte). `pieces` : [{"nom", "contenu"}].
    """
    if not docs:
        raise ErreurAgent("aucun document à utiliser")
    texte_cours, reperes, _ = contexte(docs, sujet, consigne, memoire, progres)
    lire = _lecteur(genre)
    with tempfile.TemporaryDirectory() as dossier:  # les cours sont dans l'entrée, les pièces jointes ici
        a_lire, texte_pieces = preparer_pieces(pieces or [], Path(dossier))
        progres("rédaction")
        (contenu, web), _ = demander(genre, prompt(genre, sujet, date_eval, consigne=consigne, fichiers=a_lire,
                                                    strict=strict),
                                     Path(dossier), lire=lire, outils=OUTILS_WEB + (["Read"] if a_lire else []),
                                     entree=entree_avec_pieces(texte_cours, texte_pieces), timeout=1200)
    return contenu, reperes, web


RETOUCHE = """

=== MODIFICATION D'UN SUPPORT EXISTANT ===
Ce support existe déjà : il est à la fin de l'entrée, après les cours, sous « {marque} ». L'élève
demande de le modifier, dans ses mots :
{demande}
- Applique cette demande, et seulement elle : garde tel quel tout ce qu'elle ne touche pas (structure,
  parties, repères [D1]…, marques [IA], schémas), sauf une erreur évidente que tu corriges au passage.
- Si elle demande d'ajouter ou de développer, appuie-toi sur les cours fournis (même règles qu'au-dessus).
- Respecte toujours le format décrit plus haut pour ce genre de support.
- Réponds avec le support COMPLET modifié (pas seulement le morceau changé), dans le même format, sans
  phrase avant ni après pour expliquer ce que tu as fait.
"""
MARQUE_ACTUEL = "=== SUPPORT ACTUEL À MODIFIER ==="


def retoucher(rev: dict, demande: str, docs: list[dict], pieces: list[dict] | None = None, memoire=None,
              progres=lambda texte: None) -> tuple[str, list[dict]]:
    """Modifie un support prêt selon une demande libre de l'élève (« ajoute un exemple sur… », « plus court »).

    L'IA reçoit les cours (pour pouvoir ajouter du contenu), le support actuel et la demande ; elle renvoie
    le support complet. Renvoie (contenu, sources web) ; les repères des cours ne changent pas."""
    demande = (demande or "").strip()
    if not demande:
        raise ErreurAgent("demande de modification vide")
    if not (rev.get("contenu") or "").strip():
        raise ErreurAgent("rien à modifier : le support n'a pas encore de contenu")
    genre = rev["genre"]
    texte_cours, _, _ = contexte(docs, rev["sujet"], demande, memoire, progres) if docs else ("", {}, 0)
    consigne = rev.get("consigne") or ""
    with tempfile.TemporaryDirectory() as dossier:
        a_lire, texte_pieces = preparer_pieces(pieces or [], Path(dossier))
        progres("modification")
        texte_prompt = prompt(genre, rev["sujet"], rev.get("date_eval") if genre == "plan" else None,
                              consigne=consigne, fichiers=a_lire, strict=bool(rev.get("strict")))
        texte_prompt += RETOUCHE.format(marque=MARQUE_ACTUEL,
                                        demande="\n".join(f"  > {ligne}" for ligne in demande.splitlines()))
        entree = entree_avec_pieces(texte_cours, texte_pieces) + f"\n\n{MARQUE_ACTUEL}\n\n{rev['contenu']}"
        (contenu, web), _ = demander(genre, texte_prompt, Path(dossier), lire=_lecteur(genre),
                                     outils=OUTILS_WEB + (["Read"] if a_lire else []), entree=entree, timeout=1200)
    return contenu, web or (rev.get("sources_web") or [])


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
- Les épreuves datées par l'élève sont fixes : l'épreuve blanche de la matière se fait à ce jour et
  cette heure, la matière est revue plus souvent à l'approche de sa date (rappel court la veille),
  et le jour J rien de lourd avant l'épreuve.
- Une matière qui couvre plusieurs années (ex. « BTS1, BTS2 ») : répartis des rappels sur les cours
  de chacune de ces années (repérées par [année]), pas seulement la plus récente.
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


def entree_programme(consigne: str, matieres: list[dict], evenements: list[dict],
                     epreuves: list[dict] | None = None) -> str:
    """Texte fourni au programme : contexte, épreuves datées, puis pour chaque matière ses supports et ses cours.

    `matieres` : [{"matiere", "niveau", "sujet", "supports": [genres], "cours": [titres]}].
    `epreuves` : [{"matiere", "date": "AAAA-MM-JJ", "heure": "HH:MM" ou ""}], saisies par l'élève.
    """
    parties = [f"Contexte donné par l'élève :\n{consigne.strip() or '(aucun)'}", "Matières :"]
    for m in matieres:
        supports = ", ".join(GENRES[g] for g in m["supports"]) or "aucun"
        cours = "; ".join(m["cours"]) or "?"
        parties.append(f"- {m['matiere']} ({m['niveau']}){' sur « ' + m['sujet'] + ' »' if m.get('sujet') else ''}"
                       f" : supports préparés : {supports}. Cours : {cours}")
    if epreuves:
        parties.append("Épreuves (dates fixées par l'élève) :")
        parties += [f"- {e['matiere']} : {date_fr(dt.date.fromisoformat(e['date']))}"
                    + (f" à {e['heure'].replace(':', ' h ')}" if e.get("heure") else "") for e in epreuves]
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
        entree = entree_programme(campagne["consigne"], matieres, evenements, campagne.get("epreuves"))
        if texte_pieces:
            entree += f"\n\nFichiers joints par l'élève :\n{texte_pieces}"
        programme, _ = demander("programme", prompt_programme(campagne["titre"], campagne["debut"], campagne["fin"],
                                                              fichiers=a_lire),
                                Path(dossier), lire=lambda t: parse_programme(t, campagne["debut"], campagne["fin"]),
                                outils=["Read"] if a_lire else None, entree=entree, timeout=600)
    return programme


# --- Répétition espacée (FSRS, comme Anki) -----------------------------------

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
