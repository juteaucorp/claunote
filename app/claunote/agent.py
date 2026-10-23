"""Analyse d'un scan avec Claude Code en mode non interactif (`claude -p`).

Utilise le forfait Claude Pro via CLAUDE_CODE_OAUTH_TOKEN : pas de clé API.
Une seule passe par document : transcription Markdown + classement en JSON.
"""
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
import datetime as dt
from pathlib import Path

MODELE = os.environ.get("CLAUDE_MODEL", "sonnet")
NIVEAUX = ["6e", "5e", "4e", "3e", "Seconde", "Première", "Terminale", "BTS1", "BTS2"]
TYPES = ["Cours", "TP", "Exercices", "Évaluation", "Notes perso"]

PROMPT = """Tu analyses un document scanné d'un élève (du collège au BTS SIO) : le fichier `{nom}`.
Lis-le en entier avec l'outil Read (s'il a plus de 10 pages, lis-le par tranches de pages).

Réponds UNIQUEMENT avec un objet JSON valide, sans aucun texte autour :
{{"transcription": "...", "titre": "...", "niveau": "...", "matiere": "...", "type": "...",
  "chapitre": "...", "date": "AAAA-MM-JJ ou null", "mots_cles": ["..."], "resume": "...", "confiance": 0.0}}

Règles :
- transcription : tout le texte, fidèlement, en Markdown. Formules en LaTeX ($...$), schémas
  décrits entre [crochets], passages illisibles notés [illisible]. N'invente rien.
- titre : court et parlant (ex. « Suites géométriques »).
- niveau : exactement un parmi {niveaux}.
- matiere : nom court en français (Mathématiques, Français, Anglais, Histoire-Géographie, SVT,
  Physique-Chimie, Réseaux, Systèmes, Développement, Cybersécurité, CEJM, …).
- type : exactement un parmi {types}.
- date : la date écrite sur le document, sinon null.
- resume : 2 ou 3 phrases sur ce que le document enseigne.
- confiance : de 0 à 1, ta certitude sur niveau + matière + type. Mets moins de 0.7 si tu hésites.
"""

LIMITE = re.compile(r"usage limit|rate limit|limit reached|too many requests|\b429\b", re.I)


class LimiteAtteinte(Exception):
    """Limite d'usage du forfait atteinte : réessayer plus tard."""


class ErreurAgent(Exception):
    pass


@dataclass
class Analyse:
    transcription: str
    titre: str | None = None
    niveau: str | None = None
    matiere: str | None = None
    type: str | None = None
    chapitre: str | None = None
    date: dt.date | None = None
    mots_cles: list[str] = field(default_factory=list)
    resume: str | None = None
    confiance: float = 0.0


def _choix(valeur, options):
    if not isinstance(valeur, str):
        return None
    for o in options:
        if valeur.strip().lower() == o.lower():
            return o
    return None


def _date(valeur):
    try:
        return dt.date.fromisoformat(valeur) if valeur else None
    except (TypeError, ValueError):
        return None


def parse_reponse(texte: str) -> Analyse:
    """Extrait le JSON de la réponse (tolère des balises ```json autour)."""
    debut, fin = texte.find("{"), texte.rfind("}")
    if debut < 0 or fin < debut:
        raise ErreurAgent(f"pas de JSON dans la réponse : {texte[:200]!r}")
    try:
        d = json.loads(texte[debut:fin + 1])
    except json.JSONDecodeError as e:
        raise ErreurAgent(f"JSON invalide : {e}") from e
    niveau, type_ = _choix(d.get("niveau"), NIVEAUX), _choix(d.get("type"), TYPES)
    try:
        confiance = min(max(float(d.get("confiance") or 0), 0.0), 1.0)
    except (TypeError, ValueError):
        confiance = 0.0
    if niveau is None or type_ is None or not d.get("matiere"):
        confiance = min(confiance, 0.5)  # classement incomplet : à vérifier
    return Analyse(
        transcription=str(d.get("transcription") or ""),
        titre=d.get("titre") or None,
        niveau=niveau,
        matiere=(d.get("matiere") or "").strip() or None,
        type=type_,
        chapitre=d.get("chapitre") or None,
        date=_date(d.get("date")),
        mots_cles=[str(m) for m in d.get("mots_cles") or []],
        resume=d.get("resume") or None,
        confiance=confiance,
    )


def appeler_claude(prompt: str, cwd: Path, outils: list[str] | None = None,
                   entree: str | None = None, timeout: int = 1800) -> str:
    """Lance `claude -p` et renvoie le texte de la réponse.

    `entree` est passé sur l'entrée standard (Claude Code l'ajoute au prompt).
    Lève LimiteAtteinte si la limite d'usage du forfait est atteinte.
    """
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", MODELE]
    if outils:
        cmd += ["--allowedTools", ",".join(outils)]
    try:
        p = subprocess.run(cmd, cwd=cwd, input=entree, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ErreurAgent(f"Claude Code trop long ({timeout // 60} min)") from e
    try:
        sortie = json.loads(p.stdout)
    except json.JSONDecodeError:
        sortie = {"is_error": True, "result": p.stdout}
    texte = str(sortie.get("result") or "")
    if p.returncode != 0 or sortie.get("is_error"):
        message = f"{texte} {p.stderr}".strip()
        if LIMITE.search(message):
            raise LimiteAtteinte(message[:300])
        raise ErreurAgent(message[:500] or f"code de sortie {p.returncode}")
    return texte


def analyser(chemin: Path) -> Analyse:
    prompt = PROMPT.format(nom=chemin.name, niveaux=", ".join(NIVEAUX), types=", ".join(TYPES))
    return parse_reponse(appeler_claude(prompt, chemin.parent, outils=["Read"]))
