"""Index de tous les documents lus : chaque transcription est coupée en passages, pour que le chat,
les révisions et le classement local retrouvent le bon morceau du bon cours, même dans un long document.

    transcription ──► passages de ~1 200 caractères ──► PostgreSQL : recherche par les mots (toujours)
    (classé, à vérifier,   (coupés aux paragraphes,        + par le sens (pgvector + modèle d'embeddings
     hors cours)            avec un peu de recouvrement)     local d'Ollama, si présents)

Fait par le worker quand il n'a rien de plus urgent ; le sens est calculé sur la VM, sans Claude, et
seulement quand le processeur n'est pas saturé.
"""
import re

from claunote import db, local, ressources

TAILLE = 1200        # caractères par passage
RECOUVREMENT = 150   # repris au début du passage suivant : une idée coupée en deux reste trouvable
LOT_VECTEURS = 32    # passages envoyés d'un coup au modèle d'embeddings


def decouper(texte: str, taille: int = TAILLE, recouvrement: int = RECOUVREMENT) -> list[str]:
    """Passages coupés aux paragraphes (ou aux phrases pour un paragraphe trop long)."""
    blocs = [b.strip() for b in re.split(r"\n\s*\n", texte or "") if b.strip()]
    morceaux: list[str] = []
    for b in blocs:
        if len(b) <= taille:
            morceaux.append(b)
            continue
        phrases, courant = re.split(r"(?<=[.!?;:])\s+|\n", b), ""
        for ph in phrases:
            while len(ph) > taille:  # une « phrase » sans ponctuation (tableau, code) : coupe franche
                morceaux.append(ph[:taille])
                ph = ph[taille - recouvrement:]
            if courant and len(courant) + len(ph) + 1 > taille:
                morceaux.append(courant)
                courant = courant[-recouvrement:] + " " + ph
            else:
                courant = f"{courant} {ph}".strip()
        if courant:
            morceaux.append(courant)
    passages, courant = [], ""
    for m in morceaux:  # regroupe les petits paragraphes
        if courant and len(courant) + len(m) + 2 > taille:
            passages.append(courant)
            courant = m
        else:
            courant = f"{courant}\n\n{m}".strip()
    if courant:
        passages.append(courant)
    return passages


def entete(doc: dict) -> str:
    """Titre ajouté au début de chaque passage pour le sens : « TP VLAN — … » se retrouve par son sujet."""
    return f"{doc.get('titre') or doc.get('nom') or ''}\n"


_refus = {"dimension": False}   # vecteurs refusés par la base (OLLAMA_EMBED changé) : on arrête d'en calculer


def indexer(limite: int = 20, arret=lambda: False) -> int:
    """Indexe les documents nouveaux ou modifiés ; renvoie combien. Le sens seulement si la VM le permet.

    Un document mis à jour sans que son texte change (rangé dans kDrive, reclassé) garde ses vecteurs :
    seuls les passages nouveaux passent par le modèle d'embeddings, le reste n'est pas recalculé.
    """
    n = 0
    for doc in db.a_indexer(limite):
        if arret():
            break
        passages = decouper(doc["transcription"])
        anciens = db.passages_existants(doc["id"])
        connus = {texte: v for texte, v in anciens if v is not None}
        if passages == [texte for texte, _ in anciens] and (len(connus) == len(set(passages)) or _refus["dimension"]):
            db.marquer_indexe(doc["id"])   # rien n'a changé dans le texte
            n += 1
            continue
        nouveaux = [p for p in dict.fromkeys(passages) if p not in connus]
        calcules = {}
        if nouveaux and not _refus["dimension"] and not ressources.sature():
            v = local.vecteurs([entete(doc) + p for p in nouveaux])
            calcules = dict(zip(nouveaux, v)) if v else {}
        vecteurs = [connus.get(p) or calcules.get(p) for p in passages]
        try:
            db.remplacer_passages(doc["id"], passages, vecteurs if any(vecteurs) else None)
        except Exception:
            if not any(vecteurs):
                raise
            # vecteurs d'une autre taille (OLLAMA_EMBED changé) : les mots seulement, plutôt que rien
            _refus["dimension"] = True
            db.remplacer_passages(doc["id"], passages, None)
        n += 1
    return n


def vectoriser(limite: int = 128, arret=lambda: False) -> int:
    """Calcule le sens des passages indexés avant que le modèle d'embeddings soit là ; renvoie combien."""
    n = 0
    while n < limite and not arret() and not _refus["dimension"] and not ressources.sature():
        lot = db.passages_sans_vecteur(LOT_VECTEURS)
        if not lot:
            break
        vecteurs = local.vecteurs([p["texte"] for p in lot])
        if vecteurs is None:
            break
        try:
            db.poser_vecteurs([p["id"] for p in lot], vecteurs)
        except Exception:
            # vecteurs d'une autre taille que la colonne (OLLAMA_EMBED changé) : on garde les mots, et on
            # ne recalcule plus ce lot à chaque tour (jusqu'au prochain démarrage)
            _refus["dimension"] = True
            break
        n += len(lot)
    return n


_questions: dict[str, list[float]] = {}   # sens des dernières questions : une question reposée ne coûte rien
QUESTIONS_GARDEES = 256


def vecteur_question(question: str) -> list[float] | None:
    """Sens d'une question (pour chercher les passages proches) ; None sans modèle d'embeddings."""
    cle = " ".join(question.lower().split())
    if cle in _questions:
        _questions[cle] = _questions.pop(cle)   # la plus récente passe en dernier
        return _questions[cle]
    v = local.vecteurs([question])
    if not v:
        return None
    _questions[cle] = v[0]
    while len(_questions) > QUESTIONS_GARDEES:
        _questions.pop(next(iter(_questions)))
    return v[0]
