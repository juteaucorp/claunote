"""« Une question » : un chat sur tous tes documents, depuis Réviser.

    question ──► passages de tous tes documents (par les mots, et par le sens si l'index le permet)
                 + ton agenda Pronote (contrôles, devoirs)
                     │
       ┌─────────────┴─────────────── mode « Rapide, sans IA » : les passages surlignés, tout de suite
       ▼ mode « IA »
    modèle local (sur la VM) ──► sûr de lui ──────────────────────► sa réponse (1 fois sur 10 contrôlée)
          │                  └─► doute, ou demande de l'aide ───► Claude vérifie et complète son brouillon
          └─ pas installé, VM saturée, ou réglage « Claude d'abord » ─► Claude répond directement

La réponse est faite par le site lui-même, dans un fil à part (pas par le worker, qui peut être occupé
par un gros scan) : le chat se met à jour tout seul quand elle arrive.
"""
import json
import re
import tempfile
from pathlib import Path

from claunote import revision
from claunote.agent import ErreurAgent, demander

MODES = {"ia": "IA", "local": "Rapide, sans IA"}
DOCS_MAX = 6             # documents cités dans une réponse
TEXTE_CLAUDE = 24_000    # caractères de passages donnés à Claude
TEXTE_LOCAL = 7_000      # … au modèle local (un processeur lit lentement)
HISTORIQUE = 6           # derniers messages de la discussion rappelés à l'IA
CONFIANCE_LOCALE = 0.7   # en dessous, le modèle local demande l'aide de Claude
MOT = re.compile(r"\w{2,}")
STATUTS = {"a_verifier": "classement à vérifier", "hors_cours": "rangé hors cours"}

REGLES = """- Ses documents d'abord : cite le repère [Dn] des points qui en viennent. N'invente rien.
- Son agenda (contrôles, devoirs) est fourni : sers-t'en pour les questions d'organisation.
- Une question sans rapport avec ses cours (méthode, organisation, culture générale) : réponds quand même.
- Français, Markdown simple, sans HTML ni LaTeX (formules en texte : u_n = u_0 × q^n)."""

PROMPT = """Tu es l'assistant de révision d'un élève (du collège au BTS SIO). Il te pose une question
dans un chat. Sur l'entrée : la discussion jusqu'ici, son agenda, puis des passages de ses documents
(cours, TP, documents administratifs…) qui parlent du sujet, chacun avec un repère [D1], [D2]…{portee}
Règles :
- Réponds directement à la question, simplement et brièvement (quelques lignes, une liste ou un petit
  exemple si ça aide). Développe seulement si l'élève le demande.
""" + REGLES + """
- Si ses documents ne suffisent pas ou si c'est une info pratique ou récente, tu peux chercher sur le web
  (WebSearch, WebFetch), sur des sources fiables ; marque chaque apport du web [W1], [W2]…
{brouillon}Réponds uniquement avec ta réponse{web_md}.
"""

BROUILLON = """- Un assistant local, plus petit que toi, a préparé un brouillon de réponse (sur l'entrée, avant
  les documents){demande}. Vérifie-le avec les documents : garde ce qui est juste, corrige les erreurs,
  complète ce qui manque. L'élève ne voit pas le brouillon : réponds-lui directement.
  Ta toute dernière ligne doit être exactement « BROUILLON: juste » si le brouillon était juste pour
  l'essentiel, sinon « BROUILLON: corrigé ».
"""

PROMPT_LOCAL = """Tu es l'assistant de révision d'un élève. Réponds à sa dernière question à partir de ses documents
fournis (repères [D1], [D2]…), simplement et brièvement. Cite le repère des points qui en viennent.
""" + REGLES + """
Si la réponse n'est pas dans ses documents, ou si tu n'es pas sûr, dis-le avec une confiance basse et
pose ta question à un assistant plus fort (il a accès au web et à tous les documents).{portee}
Réponds UNIQUEMENT avec un objet JSON :
{{"reponse": "ta réponse en Markdown", "confiance": 0.0, "besoin_aide": false, "question_pour_aide": ""}}
- confiance : de 0 à 1, ta certitude que ta réponse est juste et complète.
- besoin_aide : true si tu as besoin de l'assistant plus fort (info absente, doute, calcul délicat…).
"""


def requete(question: str) -> str:
    """Requête plein texte « mot1 | mot2 | … » : un passage qui contient une partie des mots compte aussi.

    Les mots vides (« quoi », « est »…) sont ignorés par la configuration « french » de PostgreSQL.
    """
    mots = list(dict.fromkeys(m.lower() for m in MOT.findall(question or "")))[:30]
    return " | ".join(mots)


def titre(question: str) -> str:
    t = " ".join((question or "").split())
    return t if len(t) <= 70 else t[:67].rstrip() + "…"


def _extrait(texte: str) -> str:
    """Extrait sur une ligne, sans la mise en forme Markdown du document (titres, listes, code)."""
    t = re.sub(r"\s+", " ", (texte or "").replace("```", " ")).strip()
    return re.sub(r"(^|\s)(#{1,6}|[-*+>]|\d+\.)\s", " ", t).strip()


def par_document(passages: list[dict]) -> list[dict]:
    """Passages regroupés par document, dans l'ordre de pertinence ; repères D1, D2…"""
    docs: dict[int, dict] = {}
    for p in passages:
        d = docs.get(p["document_id"])
        if d is None:
            if len(docs) >= DOCS_MAX:
                continue
            d = docs[p["document_id"]] = {"id": p["document_id"], "repere": f"D{len(docs) + 1}", "passages": [],
                                          **{k: p.get(k) for k in ("nom", "titre", "niveau", "matiere", "type",
                                                                   "statut")}}
        d["passages"].append(p)
    return list(docs.values())


def _entete(d: dict) -> str:
    quoi = ", ".join(x for x in (d.get("niveau"), d.get("matiere"), d.get("type"), STATUTS.get(d.get("statut")))
                     if x)
    return f"[{d['repere']}] {d.get('titre') or d.get('nom')}" + (f" ({quoi})" if quoi else "")


def reponse_locale(question: str, passages: list[dict], portee: str = "") -> tuple[str, dict]:
    """Réponse sans IA : les passages des documents qui en parlent, mots surlignés. (Markdown, repères)."""
    docs = par_document(passages)
    if not docs:
        return (f"Je n'ai rien trouvé dans tes documents{portee} pour « {titre(question)} ».\n\n"
                "Essaie avec d'autres mots (le nom de la notion, une commande, un mot-clé du chapitre), "
                "ou demande à l'IA : elle peut aussi chercher sur le web.", {})
    lignes = [f"Voici ce que disent tes documents{portee} :"]
    for d in docs:
        lignes.append(f"**{_entete(d)}**")
        for p in d["passages"][:2]:
            extrait = _extrait(p.get("extrait") or p["texte"][:300])
            lignes.append(f"> … {extrait} …")
    return "\n\n".join(lignes), {d["repere"]: d["id"] for d in docs}


def entree(question: str, historique: list[dict], passages: list[dict], agenda: str = "",
           limite: int = TEXTE_CLAUDE, brouillon: str = "") -> tuple[str, dict]:
    """Texte donné à l'IA : discussion, agenda, brouillon éventuel, passages numérotés ; repères [Dn] → id."""
    fil = []
    for m in historique[-HISTORIQUE:]:
        qui = "Élève" if m["role"] == "eleve" else "Toi"
        fil.append(f"{qui} : {m['contenu'].strip()[:3000]}")
    fil.append(f"Élève (question à laquelle répondre) : {question.strip()}")
    blocs, reste = [], limite
    docs = par_document(passages)
    for d in docs:
        if reste <= 0:
            break
        corps = "\n\n…\n\n".join(p["texte"] for p in d["passages"])[:reste]
        blocs.append(f"{_entete(d)}\n{corps}")
        reste -= len(corps)
    texte = "Discussion :\n" + "\n\n".join(fil)
    if agenda:
        texte += f"\n\n=====\n\nAgenda de l'élève :\n{agenda}"
    if brouillon:
        texte += f"\n\n=====\n\nBrouillon de l'assistant local :\n{brouillon}"
    texte += "\n\n=====\n\nDocuments :\n\n" + (revision.SEPARATEUR.join(blocs) if blocs
                                                 else "(aucun document trouvé sur ce sujet)")
    return texte, {d["repere"]: d["id"] for d in docs[:len(blocs)]}


def repondre_local(question: str, historique: list[dict], passages: list[dict], agenda: str = "",
                   portee: str = "") -> dict:
    """Le modèle local répond : {"reponse", "confiance", "aide", "demande", "reperes"} ; ErreurAgent s'il échoue."""
    from claunote import local
    texte, reperes = entree(question, historique, passages, agenda, TEXTE_LOCAL)
    brut = local.ollama(PROMPT_LOCAL.format(portee=f"\nSujet choisi par l'élève : {portee}." if portee else ""),
                        texte, json_seul=True, timeout=600)
    try:
        d = json.loads(brut)
    except ValueError as e:
        raise ErreurAgent(f"réponse du modèle local illisible : {e}") from e
    reponse = str(d.get("reponse") or "").strip() if isinstance(d, dict) else ""
    if not reponse:
        raise ErreurAgent("réponse vide du modèle local")
    try:
        confiance = min(max(float(d.get("confiance") or 0), 0.0), 1.0)
    except (TypeError, ValueError):
        confiance = 0.0
    return {"reponse": reponse, "confiance": confiance, "reperes": reperes,
            "aide": d.get("besoin_aide") is True or confiance < CONFIANCE_LOCALE,
            "demande": str(d.get("question_pour_aide") or "").strip()[:500]}


VERDICT = re.compile(r"\n*\s*BROUILLON\s*:\s*(juste|corrigé|corrige)\s*\.?\s*$", re.I)


def repondre_ia(question: str, historique: list[dict], passages: list[dict], portee: str = "", agenda: str = "",
                brouillon: dict | None = None) -> tuple[str, dict, list, str, bool | None]:
    """Réponse de Claude : (Markdown, repères, sources web, modèle, brouillon local jugé juste ou None).

    `brouillon` : la réponse du modèle local (repondre_local), que Claude vérifie et complète.
    Lève LimiteAtteinte, ClaudeIndisponible… comme tout appel à Claude.
    """
    texte, reperes = entree(question, historique, passages, agenda,
                            brouillon=brouillon["reponse"] if brouillon else "")

    def lire(reponse: str):
        reponse = reponse.strip()
        verdict = None
        m = VERDICT.search(reponse)
        if m:
            verdict, reponse = m.group(1).lower() == "juste", reponse[:m.start()].rstrip()
        if not reponse:
            raise ErreurAgent("réponse vide")
        contenu, web = revision.separer_web(reponse)
        return contenu, web, verdict

    consigne = ""
    if brouillon:
        demande = f" et te demande : « {brouillon['demande']} »" if brouillon.get("demande") else ""
        consigne = BROUILLON.format(demande=demande)
    prompt = PROMPT.format(portee=f"\nL'élève a choisi de parler de : {portee}." if portee else "",
                           brouillon=consigne, web_md=revision.WEB_MD)
    with tempfile.TemporaryDirectory() as vide:
        (contenu, web, verdict), modele = demander("question", prompt, Path(vide), lire=lire,
                                                   outils=revision.OUTILS_WEB, entree=texte, timeout=300)
    return contenu, reperes, web, modele, verdict
