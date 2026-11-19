"""Quel modèle Claude pour quelle tâche, et quand monter (ou descendre) d'un cran.

Le forfait Pro donne accès à plusieurs modèles via `claude -p --model` :
- haiku : rapide et sobre, pour les tâches de texte simples (noms de dossiers, reclassement) ;
- sonnet : lecture des scans manuscrits et supports de révision ;
- opus : le plus fort, pour les épreuves blanches et les seconds avis quand l'agent doute.

Escalade : un classement peu sûr (confiance < 0,7) ou une réponse illisible est refait avec
le modèle au-dessus, avant de te demander de vérifier. Repli : un modèle refusé par le forfait,
ou dont la limite propre est atteinte (Opus), est remplacé par celui du dessous jusqu'à la reprise.

Création de contenu (fiches, quiz, flashcards, plans, programmes, épreuves, et leurs modifications) :
toujours Opus 5.5, désigné par son identifiant exact (CLAUDE_ID_OPUS, par défaut claude-opus-5-5). Si
Opus atteint sa limite, ces tâches ATTENDENT sa reprise au lieu d'être faites par Sonnet (le reste,
classement et lecture, continue). CLAUDE_REPLI_CREATION=1 rétablit le repli sur Sonnet.

Réglage possible dans le .env : CLAUDE_MODELES=examen=sonnet,scan=opus (tâche=modèle, séparés
par des virgules) et CLAUDE_MODELE_MAX=sonnet pour ne jamais utiliser Opus.
"""
import datetime as dt
import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ECHELLE = ["haiku", "sonnet", "opus"]  # du plus léger au plus fort

TACHES = {
    "scan": "sonnet",        # lire un scan manuscrit : la qualité de lecture compte
    "texte": "haiku",        # texte déjà extrait (Word, PowerPoint, PDF numérique, long scan transcrit) : classement
    "classement": "haiku",   # reclasser une transcription (Organiser) : monte si l'agent doute
    "dossiers": "haiku",     # comprendre les noms de dossiers d'un import
    "condense": "haiku",     # condenser un groupe de cours quand il y en a trop pour une seule lecture
    "decoupage": "sonnet",   # où commence chaque document dans un gros scan (pages en petites images)
    # Création de contenu : Opus 5.5 (voir CREATION)
    "fiche": "opus",
    "quiz": "opus",
    "cartes": "opus",
    "plan": "opus",
    "programme": "opus",     # programme jour par jour d'une campagne
    "examen": "opus",        # sujet + corrigé d'épreuve blanche : long raisonnement
    "question": "sonnet",    # question posée dans « Demander » (Réviser) : réponse courte appuyée sur les cours
    "matieres": "haiku",     # repérer les matières en double (liste de noms + quelques titres)
    "controle": "haiku",     # vérifier un classement fait par le modèle local (texte seul)
    "actualites": "haiku",   # trier les articles des flux RSS selon les sujets suivis (par lots de 40)
    "coherence": "haiku",    # une nouvelle fiche reste-t-elle dans le sujet ? (plans comparés, texte court)
}
# Tâches qui créent un support : Opus obligatoire (pas de repli silencieux sur Sonnet)
CREATION = {"fiche", "quiz", "cartes", "plan", "programme", "examen"}
LIBELLES = {
    "scan": "Lecture d'un scan", "texte": "Classement d'un texte (Word, PDF numérique…)", "classement": "Reclassement",
    "dossiers": "Noms de dossiers d'un import", "decoupage": "Découpage d'un gros scan",
    "fiche": "Fiche de révision", "quiz": "Quiz",
    "cartes": "Flashcards", "plan": "Plan de révision", "programme": "Programme de campagne",
    "condense": "Cours condensés",
    "examen": "Épreuve blanche",
    "question": "Question posée (Demander)",
    "matieres": "Matières en double",
    "controle": "Vérification du modèle local",
    "actualites": "Tri des actualités",
    "coherence": "Vérification hors sujet d'un support",
    "sonde": "Test de Claude (bouton Réessayer)",
}
NOMS = {"haiku": "Haiku", "sonnet": "Sonnet", "opus": "Opus 5.5",
        "local": "Local, sans Claude", "ollama": "Ollama (local)"}  # analyse sur la VM (local.py)

INDISPONIBLE_MODELE = dt.timedelta(hours=24)  # modèle refusé par le forfait : on réessaie le lendemain

# modèle → (jusqu'à quand, pourquoi) ; tenu par le worker, publié sur la page État
_indisponibles: dict[str, tuple[dt.datetime, str]] = {}


ID_OPUS = "claude-opus-5-5"
_alias_seul: set[str] = set()  # identifiant exact refusé par Claude Code (trop ancien) : alias « opus » à la place


def identifiant(modele: str) -> str:
    """Ce qui est passé à `claude -p --model` : l'identifiant exact d'Opus 5.5, l'alias pour les autres."""
    if modele == "opus" and modele not in _alias_seul:
        return (os.environ.get("CLAUDE_ID_OPUS") or ID_OPUS).strip()
    return modele


def identifiant_refuse(modele: str) -> bool:
    """L'identifiant exact a été refusé : on retente avec l'alias. Faux s'il n'y a pas d'autre nom à essayer."""
    if identifiant(modele) == modele:
        return False
    _alias_seul.add(modele)
    return True


def obligatoire(tache: str) -> bool:
    """Tâche de création qui doit rester sur Opus : elle attend la reprise d'Opus plutôt que de passer à Sonnet."""
    if tache not in CREATION or (os.environ.get("CLAUDE_REPLI_CREATION") or "").strip().lower() in ("1", "oui", "true"):
        return False
    voulu = _reglages().get(tache) or TACHES[tache]
    return voulu == "opus" and maximum() == "opus"


def attente_opus() -> dt.datetime | None:
    """Heure de reprise d'Opus s'il est en pause (limite), sinon None."""
    jusqu_a = _indisponibles.get("opus")
    return jusqu_a[0] if jusqu_a and jusqu_a[0] > _maintenant() else None


def _maintenant() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _reglages() -> dict[str, str]:
    choix = {}
    for morceau in (os.environ.get("CLAUDE_MODELES") or "").split(","):
        tache, _, modele = morceau.partition("=")
        if tache.strip() in TACHES and modele.strip().lower() in ECHELLE:
            choix[tache.strip()] = modele.strip().lower()
    return choix


def maximum() -> str:
    m = (os.environ.get("CLAUDE_MODELE_MAX") or "opus").strip().lower()
    return m if m in ECHELLE else "opus"


def disponible(modele: str) -> bool:
    jusqu_a = _indisponibles.get(modele)
    if jusqu_a and jusqu_a[0] > _maintenant():
        return False
    _indisponibles.pop(modele, None)
    return ECHELLE.index(modele) <= ECHELLE.index(maximum())


def marquer_indisponible(modele: str, jusqu_a: dt.datetime | None, raison: str):
    _indisponibles[modele] = (jusqu_a or _maintenant() + INDISPONIBLE_MODELE, raison)


def indisponibles() -> dict[str, dict]:
    return {m: {"jusqu_a": j.isoformat(), "raison": r} for m, (j, r) in _indisponibles.items()
            if j > _maintenant()}


def choisir(tache: str) -> str:
    """Le modèle prévu pour la tâche, ou le plus proche en dessous s'il est indisponible."""
    voulu = _reglages().get(tache) or TACHES.get(tache, "sonnet")
    if obligatoire(tache):
        return "opus"  # en pause, la tâche attend (OpusEnPause) au lieu de descendre
    for m in reversed(ECHELLE[:ECHELLE.index(voulu) + 1]):
        if disponible(m):
            return m
    return ECHELLE[0]


def plus_fort(modele: str) -> str | None:
    """Le modèle au-dessus disponible, pour un second avis ; None si on est déjà en haut."""
    for m in ECHELLE[ECHELLE.index(modele) + 1:]:
        if disponible(m):
            return m
    return None


def plus_faible(modele: str) -> str | None:
    for m in reversed(ECHELLE[:ECHELLE.index(modele)]):
        if disponible(m):
            return m
    return None


def tableau() -> list[dict]:
    """Pour la page État : chaque tâche, son modèle prévu et celui utilisé en ce moment."""
    reglages = _reglages()
    return [{"tache": t, "libelle": LIBELLES[t], "prevu": reglages.get(t) or m, "actuel": choisir(t),
             "second_avis": plus_fort(choisir(t))} for t, m in TACHES.items()]


# --- Heure de reprise après une limite d'usage ------------------------------------

MOIS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
                                   start=1)}
_EPOCH = re.compile(r"\|(\d{10})\b")
_RESET = re.compile(
    r"resets?\s+(?:at\s+|on\s+)?(?:(?P<mois>[A-Za-z]{3})[a-z]*\.?\s+(?P<jour>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>am|pm)?(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+-]+)*)\))?", re.I)


def reprise(message: str, maintenant: dt.datetime | None = None) -> dt.datetime | None:
    """Heure à laquelle la limite d'usage se lève, lue dans le message de Claude Code.

    Formats connus : « Claude AI usage limit reached|1761235200 » (horodatage) et
    « 5-hour limit reached ∙ resets 7pm (Europe/Paris) », « resets Oct 27, 9am ».
    Sans fuseau indiqué, l'heure est celle du conteneur, comme l'affiche Claude Code.
    """
    m = _EPOCH.search(message or "")
    if m:
        return dt.datetime.fromtimestamp(int(m.group(1)), dt.timezone.utc)
    m = _RESET.search(message or "")
    if not m or (not m.group("ampm") and not m.group("min")):
        return None  # « resets 7 » seul : trop ambigu
    try:
        fuseau = ZoneInfo(m.group("tz")) if m.group("tz") else None
    except (ZoneInfoNotFoundError, ValueError):
        fuseau = None
    maintenant = (maintenant or _maintenant()).astimezone(fuseau)  # None : fuseau local
    heure = int(m.group("h")) % 12 + (12 if (m.group("ampm") or "").lower() == "pm" else 0) \
        if m.group("ampm") else int(m.group("h"))
    if heure > 23:
        return None
    jour = maintenant.date()
    if m.group("mois") and m.group("mois")[:3].lower() in MOIS:
        try:
            jour = dt.date(jour.year, MOIS[m.group("mois")[:3].lower()], int(m.group("jour")))
        except ValueError:
            return None
        if jour < maintenant.date() - dt.timedelta(days=1):
            jour = jour.replace(year=jour.year + 1)
    quand = dt.datetime.combine(jour, dt.time(heure, int(m.group("min") or 0)), maintenant.tzinfo)
    if quand <= maintenant and not m.group("mois"):
        quand += dt.timedelta(days=1)
    return quand.astimezone(dt.timezone.utc)
