"""« Demander » : poser une question dans un chat, depuis Réviser.

Deux façons de répondre :

    question ──► recherche dans les cours (PostgreSQL, sans IA) ──► cours qui en parlent [D1]…
                                                                      │
                     ┌────────────── mode « Rapide, sans IA » ────────┤
                     ▼                                                ▼ mode « IA »
       extraits surlignés des cours (instantané,         Claude répond en citant [D1]…, avec le fil
       gratuit, marche même pendant une pause de Claude)  de la discussion ; le web si utile [W1]…

La réponse IA est faite par le site lui-même, dans un fil à part (pas par le worker, qui peut être
occupé plusieurs minutes par un gros scan) : le chat se met à jour tout seul quand elle arrive.
"""
import re
import tempfile
from pathlib import Path

from claunote import revision
from claunote.agent import ErreurAgent, demander

MODES = {"ia": "IA (Claude)", "local": "Rapide, sans IA"}
COURS_MAX = 6            # cours donnés à l'IA
TEXTE_PAR_COURS = 12_000  # caractères de chaque cours
HISTORIQUE = 6           # derniers messages de la discussion rappelés à l'IA
MOT = re.compile(r"\w{2,}")

PROMPT = """Tu es l'assistant de révision d'un élève (du collège au BTS SIO). Il te pose une question
dans un chat. Sur l'entrée : la discussion jusqu'ici, puis ses cours qui parlent du sujet, chacun avec
un repère [D1], [D2]… (il peut n'y en avoir aucun).{portee}
Règles :
- Réponds directement à la question, simplement et brièvement (quelques lignes, une liste ou un petit
  exemple si ça aide). Développe seulement si l'élève le demande.
- Ses cours d'abord : cite le repère [Dn] des points qui en viennent. N'invente rien.
- Si ses cours ne suffisent pas ou si c'est une info pratique ou récente, tu peux chercher sur le web
  (WebSearch, WebFetch), sur des sources fiables ; marque chaque apport du web [W1], [W2]…
- Une question sans rapport avec ses cours (méthode, organisation, culture générale) : réponds quand même.
- Français, Markdown simple, sans HTML ni LaTeX (formules en texte : u_n = u_0 × q^n).
Réponds uniquement avec ta réponse{web_md}.
"""


def requete(question: str) -> str:
    """Requête plein texte « mot1 | mot2 | … » : un cours qui contient une partie des mots compte aussi.

    Les mots vides (« quoi », « est »…) sont ignorés par la configuration « french » de PostgreSQL.
    """
    mots = list(dict.fromkeys(m.lower() for m in MOT.findall(question or "")))[:30]
    return " | ".join(mots)


def titre(question: str) -> str:
    t = " ".join((question or "").split())
    return t if len(t) <= 70 else t[:67].rstrip() + "…"


def _extrait(texte: str) -> str:
    """Extrait de ts_headline sur une ligne, sans la mise en forme Markdown du cours (titres, listes)."""
    t = re.sub(r"\s+", " ", texte or "").strip()
    return re.sub(r"(^|\s)(#{1,6}|[-*+>]|\d+\.)\s", " ", t).strip()


def reponse_locale(question: str, cours: list[dict], portee: str = "") -> tuple[str, dict]:
    """Réponse sans IA : les passages des cours qui contiennent les mots de la question. (Markdown, repères)."""
    if not cours:
        return (f"Je n'ai rien trouvé dans tes cours{portee} pour « {titre(question)} ».\n\n"
                "Essaie avec d'autres mots (le nom de la notion, une commande, un mot-clé du chapitre), "
                "ou demande à l'IA : elle peut aussi chercher sur le web.", {})
    lignes, reperes = [f"Voici ce que disent tes cours{portee} :"], {}
    for i, c in enumerate(cours, start=1):
        repere = f"D{i}"
        reperes[repere] = c["id"]
        quoi = " · ".join(x for x in (c.get("niveau"), c.get("matiere"), c.get("type")) if x)
        lignes.append(f"**[{repere}] {c.get('titre') or c['nom']}** — {quoi}")
        extrait = _extrait(c.get("extrait") or "")
        if extrait:
            lignes.append(f"> … {extrait} …")
        elif c.get("resume"):
            lignes.append(f"> {c['resume']}")
    return "\n\n".join(lignes), reperes


def entree(question: str, historique: list[dict], cours: list[dict]) -> tuple[str, dict]:
    """Texte donné à l'IA : la discussion, puis les cours numérotés ; et les repères [Dn] → id."""
    fil = []
    for m in historique[-HISTORIQUE:]:
        qui = "Élève" if m["role"] == "eleve" else "Toi"
        fil.append(f"{qui} : {m['contenu'].strip()[:3000]}")
    fil.append(f"Élève (question à laquelle répondre) : {question.strip()}")
    docs = [{**c, "transcription": (c.get("transcription") or "")[:TEXTE_PAR_COURS]} for c in cours[:COURS_MAX]]
    blocs, reperes = revision.blocs_cours(docs)
    texte = "Discussion :\n" + "\n\n".join(fil) + "\n\n=====\n\nCours :\n\n" + (
        revision.SEPARATEUR.join(blocs) if blocs else "(aucun cours trouvé sur ce sujet)")
    return texte, reperes


PROMPT_LOCAL = """Tu es l'assistant de révision d'un élève. Réponds à sa dernière question, en français, simplement et
brièvement, à partir de ses cours fournis (repères [D1], [D2]…) : cite le repère des points qui en viennent.
Si les cours ne contiennent pas la réponse, dis-le en une phrase, puis donne ce que tu sais en restant prudent.
Markdown simple, sans HTML.{portee}"""


def repondre_ollama(question: str, historique: list[dict], cours: list[dict], portee: str = "") -> tuple[str, dict]:
    """Claude indisponible : même question au modèle local (Ollama, sur le processeur), sans web."""
    from claunote import local
    texte, reperes = entree(question, historique, [{**c, "transcription": (c.get("transcription") or "")[:2500]}
                                                    for c in cours[:4]])
    reponse = local.ollama(PROMPT_LOCAL.format(portee=f" Sujet choisi : {portee}." if portee else ""), texte,
                           json_seul=False, timeout=600).strip()
    if not reponse:
        raise ErreurAgent("réponse vide du modèle local")
    return reponse, reperes


def repondre_ia(question: str, historique: list[dict], cours: list[dict], portee: str = "") -> tuple[str, dict, list, str]:
    """Réponse de Claude : (Markdown, repères des cours, sources web, modèle).

    Lève LimiteAtteinte, ClaudeIndisponible… comme tout appel à Claude.
    """
    texte, reperes = entree(question, historique, cours)

    def lire(reponse: str):
        reponse = reponse.strip()
        if not reponse:
            raise ErreurAgent("réponse vide")
        return revision.separer_web(reponse)

    prompt = PROMPT.format(portee=f"\nL'élève a choisi de parler de : {portee}." if portee else "",
                           web_md=revision.WEB_MD)
    with tempfile.TemporaryDirectory() as vide:
        (contenu, web), modele = demander("question", prompt, Path(vide), lire=lire, outils=revision.OUTILS_WEB,
                                          entree=texte, timeout=300)
    return contenu, reperes, web, modele
