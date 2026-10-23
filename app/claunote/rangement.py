"""Où ranger un document dans kDrive, et sous quel nom."""
import re
import unicodedata
from pathlib import PurePosixPath

from claunote.agent import Analyse

SEUIL_CONFIANCE = 0.7
A_VERIFIER = ["a-verifier"]


def est_classe(a: Analyse) -> bool:
    return a.confiance >= SEUIL_CONFIANCE and bool(a.niveau and a.matiere and a.type)


def dossier(niveau: str, matiere: str, type_: str) -> list[str]:
    """Chemin sous Etudes/ : archives/<niveau>/<matière>/<type>."""
    return ["archives", niveau, matiere, type_]


def slug(texte: str) -> str:
    t = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:60]


def nouveau_nom(a: Analyse, nom_actuel: str) -> str:
    """`2025-11-04_suites-geometriques.pdf` ; garde le nom actuel si pas de titre."""
    ext = PurePosixPath(nom_actuel).suffix.lower()
    base = slug(a.titre or "") or slug(PurePosixPath(nom_actuel).stem) or "document"
    return f"{a.date.isoformat()}_{base}{ext}" if a.date else f"{base}{ext}"
