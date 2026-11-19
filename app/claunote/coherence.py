"""Garde-fou hors sujet : une nouvelle fiche s'appuie sur les anciennes et sur les autres cours de la matière.

  demande ──► anciens supports de la matière (Réviser) ─┐
          └─► passages d'autres cours (Cours)   ────────┴─► « repères du périmètre » donnés à Opus
                                                              │
                         nouvelle fiche ◄─────────────────────┘
                              │
                 contrôle (Haiku : plans comparés, peu de jetons)
                     dans le sujet ─► prête
                     hors sujet ────► refaite une fois par Opus avec les écarts ─► re-contrôle ─► prête
                                                                (écarts restants affichés sur la fiche)

Rien ici n'est bloquant : sans anciens supports ni passages, la fiche est faite comme avant ; un contrôle
impossible (limite, panne) laisse la fiche telle quelle.
"""
import json
import re
import tempfile
import unicodedata
from pathlib import Path

from claunote import db
from claunote.agent import ErreurAgent, demander

ANCIENS_MAX = 3            # anciens supports donnés en repère
ANCIEN_CARACTERES = 6_000  # de chacun (le plan et le début suffisent pour le périmètre)
EXTRAITS_MAX = 5           # passages d'autres cours de la matière
EXTRAIT_CARACTERES = 900
REPERES_MAX = 24_000       # en tout : le gros de l'entrée reste les cours choisis
PLAN_CARACTERES = 3_000    # ce que voit le contrôle de chaque support
GENRES_REPERES = ("fiche", "examen", "plan")  # supports en Markdown, lisibles comme repères
MARQUE = "=== REPÈRES DU PÉRIMÈTRE (anciens supports et autres cours, pas des sources à citer) ==="

MOTS_VIDES = set("""avec dans pour sans sous entre leur leurs cette ces cela celle celui comme plus moins tres
fiche fiches quiz cartes flashcards plan revision revisions reviser cours chapitre partie parties sujet faire fais
donne ajoute mettre veux aussi tout tous toute toutes elle elles nous vous ils sont etre avoir peut doit quoi quel
quelle quels quelles notre votre mais donc puis alors encore apres avant bien chaque""".split())


def mots(texte: str) -> set[str]:
    """Mots significatifs, sans accents ni majuscules (« Réseaux » et « reseaux » se rejoignent)."""
    t = unicodedata.normalize("NFKD", (texte or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return {m.rstrip("s") for m in re.findall(r"[a-z0-9]{4,}", t) if m not in MOTS_VIDES}


def plan(contenu: str, genre: str, n: int = PLAN_CARACTERES) -> str:
    """Ce qui dit de quoi parle un support : ses titres (Markdown), ses questions (quiz), ses rectos (cartes)."""
    contenu = contenu or ""
    if genre in ("quiz", "cartes"):
        try:
            elements = json.loads(contenu)
        except ValueError:
            elements = []
        cle = "question" if genre == "quiz" else "recto"
        lignes = [f"- {e.get(cle)}" for e in elements if isinstance(e, dict) and e.get(cle)]
        return "\n".join(lignes)[:n]
    titres = [l.strip() for l in contenu.splitlines() if re.match(r"\s{0,3}#{1,4}\s", l)]
    return ("\n".join(titres) if len(titres) >= 2 else contenu)[:n]


def anciens_supports(rev: dict, limite: int = ANCIENS_MAX) -> list[dict]:
    """Anciens supports prêts de la même matière, les plus proches de la demande d'abord."""
    demande = mots(f"{rev.get('sujet') or ''} {rev.get('consigne') or ''}")
    if not demande:
        return []
    notes = []
    for c in db.supports_de_la_matiere(rev.get("matiere"), rev.get("id"), GENRES_REPERES):
        commun = demande & mots(f"{c['sujet']} {plan(c['contenu'], c['genre'])}")
        if commun:
            notes.append((len(commun), c["cree_le"], c))
    notes.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [c for _, _, c in notes[:limite]]


def autres_cours(rev: dict, limite: int = EXTRAITS_MAX) -> list[dict]:
    """Passages d'autres cours de la matière sur le sujet (pas ceux déjà choisis pour la fiche)."""
    from claunote import index, question  # chargés ici : question importe revision
    requete = question.requete(rev.get("sujet") or "")
    if not requete or not rev.get("matiere"):
        return []
    try:
        vecteur = index.vecteur_question(rev.get("sujet") or "")
    except Exception:
        vecteur = None
    choisis = set(rev.get("doc_ids") or [])
    passages = db.chercher_passages(requete, vecteur, matiere=rev["matiere"], limite=limite * 3)
    return [p for p in passages if p["document_id"] not in choisis][:limite]


def bloc_reperes(anciens: list[dict], extraits: list[dict]) -> str:
    """Texte ajouté à la fin de l'entrée d'Opus ; vide s'il n'y a rien."""
    morceaux = []
    for a in anciens:
        date = f"{a['cree_le']:%d/%m/%Y}" if a.get("cree_le") else ""
        morceaux.append(f"--- Ancien support ({a['genre']}, {date}) : « {a['sujet']} »\n"
                        f"{(a['contenu'] or '')[:ANCIEN_CARACTERES]}")
    for p in extraits:
        nom = p.get("titre") or p.get("nom") or "cours"
        morceaux.append(f"--- Extrait d'un autre cours : « {nom} » ({p.get('niveau') or '?'})\n"
                        f"{(p.get('texte') or '')[:EXTRAIT_CARACTERES]}")
    if not morceaux:
        return ""
    return f"\n\n{MARQUE}\n\n" + "\n\n".join(morceaux)[:REPERES_MAX]


def preparer(rev: dict) -> tuple[str, list[dict]]:
    """(bloc de repères pour Opus, anciens supports utilisés). Une erreur ici ne bloque jamais la fiche."""
    try:
        anciens = anciens_supports(rev)
    except Exception:
        anciens = []
    try:
        extraits = autres_cours(rev)
    except Exception:
        extraits = []
    return bloc_reperes(anciens, extraits), anciens


CONSIGNE_REPERES = f"""- À la fin de l'entrée, sous « {MARQUE} », il y a d'anciens supports faits pour
  cet élève dans la matière et des extraits d'autres de ses cours. Ils servent UNIQUEMENT à rester dans le
  sujet et au bon niveau : traite seulement le sujet demandé (pas les notions voisines qu'ils abordent),
  garde le vocabulaire et les notations de ses cours, et ne contredis pas une ancienne fiche sans raison
  (si elle se trompe, suis les cours et signale-le). Ne les recopie pas et ne les cite pas avec [D…].
"""

RECENTRAGE = """- IMPORTANT : une première version de ce support sortait du sujet demandé. Écarts relevés :
{ecarts}
  Recentre-toi sur le sujet demandé : retire ou réduis ces parties, sans perdre ce qui était juste.
"""


def consigne_recentrage(controle: dict) -> str:
    return RECENTRAGE.format(ecarts="\n".join(f"  > {e}" for e in controle.get("ecarts") or []))


PROMPT = """Tu vérifies qu'un support de révision ({genre}) fait pour un élève reste dans le sujet demandé.
Tu reçois : la demande de l'élève, les titres des cours utilisés, le plan du nouveau support, et
éventuellement le plan d'anciens supports de la même matière (le périmètre déjà travaillé).
Hors sujet = une partie qui traite d'une autre notion, d'une autre matière ou d'un autre niveau que la
demande, ou qui contredit les anciens supports sans raison. N'est PAS hors sujet : un rappel utile pour
comprendre, un exemple, une méthode, un ajout marqué [IA] qui reste sur le sujet. Sois indulgent : ne
signale que ce qui est clairement à côté.
Réponds UNIQUEMENT avec un objet JSON :
{{"dans_le_sujet": true, "ecarts": ["partie « … » : parle de … alors que la demande porte sur …"]}}
- ecarts : vide si tout va bien, 5 au plus, une phrase courte chacun.
"""


def entree_controle(rev: dict, contenu: str, docs: list[dict], anciens: list[dict]) -> str:
    lignes = [f"Demande de l'élève : « {rev.get('sujet') or ''} »"]
    if rev.get("consigne"):
        lignes.append(f"Consigne : {rev['consigne'][:1500]}")
    lignes.append(f"Matière : {rev.get('matiere') or '?'} ; niveau : {rev.get('niveau') or '?'}")
    lignes.append("Cours utilisés :\n" + "\n".join(f"- {d.get('titre') or d.get('nom')}" for d in docs[:40]))
    lignes.append(f"=== Plan du nouveau support ===\n{plan(contenu, rev['genre'])}")
    for a in anciens:
        lignes.append(f"=== Plan d'un ancien support : « {a['sujet']} » ===\n{plan(a['contenu'], a['genre'], 1500)}")
    return "\n\n".join(lignes)


def lire_controle(texte: str) -> dict:
    m = re.search(r"\{.*\}", texte or "", re.S)
    if not m:
        raise ErreurAgent(f"contrôle illisible : {(texte or '')[:200]!r}")
    try:
        d = json.loads(m.group(0))
    except ValueError as e:
        raise ErreurAgent(f"contrôle illisible : {e}") from e
    ecarts = [str(e).strip()[:300] for e in (d.get("ecarts") or []) if str(e).strip()][:5]
    # « hors sujet » sans dire où : rien à corriger, on ne refait pas le support pour si peu
    return {"ok": d.get("dans_le_sujet") is not False or not ecarts, "ecarts": ecarts}


def controler(rev: dict, contenu: str, docs: list[dict], anciens: list[dict]) -> dict | None:
    """{"ok", "ecarts", "anciens"} ; None si le contrôle n'a pas pu se faire (la fiche reste telle quelle)."""
    if not plan(contenu, rev["genre"]).strip():
        return None
    try:
        with tempfile.TemporaryDirectory() as vide:
            resultat, _ = demander("coherence", PROMPT.format(genre=rev["genre"]), Path(vide), lire=lire_controle,
                                   entree=entree_controle(rev, contenu, docs, anciens), timeout=300)
    except Exception:  # limite, panne, réponse illisible : le contrôle est un plus, pas une étape obligatoire
        return None
    resultat["anciens"] = [a["id"] for a in anciens]
    return resultat
