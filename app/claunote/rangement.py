"""Où ranger un document dans kDrive, et sous quel nom."""
import re
import unicodedata
from pathlib import PurePosixPath

from claunote.agent import Analyse

SEUIL_CONFIANCE = 0.7
A_VERIFIER = ["a-verifier"]
HORS_COURS = ["hors-cours"]


def est_classe(a: Analyse) -> bool:
    return a.confiance >= SEUIL_CONFIANCE and bool(a.niveau and a.matiere and a.type)


def dossier(niveau: str, matiere: str, type_: str) -> list[str]:
    """Chemin sous Etudes/ : archives/<niveau>/<matière>/<type>."""
    return ["archives", niveau, matiere, type_]


def dossier_doc(doc: dict) -> list[str]:
    """Dossier kDrive d'un document d'après son statut et son classement."""
    if doc["statut"] == "hors_cours":
        return HORS_COURS
    if doc["statut"] == "classe" and doc["niveau"] and doc["matiere"] and doc["type"]:
        return dossier(doc["niveau"], doc["matiere"], doc["type"])
    return A_VERIFIER


def slug(texte: str) -> str:
    t = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:60]


def nouveau_nom(a: Analyse, nom_actuel: str) -> str:
    """`2025-11-04_suites-geometriques.pdf` ; garde le nom actuel si pas de titre."""
    ext = PurePosixPath(nom_actuel).suffix.lower()
    base = slug(a.titre or "") or slug(PurePosixPath(nom_actuel).stem) or "document"
    return f"{a.date.isoformat()}_{base}{ext}" if a.date else f"{base}{ext}"


# Noms de dossiers reconnus comme année d'étude (comparés sans accents ni majuscules)
_NIVEAUX_DOSSIER = [
    (r"bts\D{0,8}1(?!\d)|\b1\s*(ere|re)?\s*annee\s*(de\s*)?bts|sio\s*1(?!\d)", "BTS1"),
    (r"bts\D{0,8}2(?!\d)|\b2\s*(eme|e)?\s*annee\s*(de\s*)?bts|sio\s*2(?!\d)", "BTS2"),
    (r"terminale|\btle\b|\bterm\b", "Terminale"),
    (r"premiere|\b1\s*(ere|re)\b", "Première"),
    (r"seconde|\b2\s*(nde|de)\b", "Seconde"),
    (r"\b3\s*(e|eme)\b|troisieme", "3e"),
    (r"\b4\s*(e|eme)\b|quatrieme", "4e"),
    (r"\b5\s*(e|eme)\b|cinquieme", "5e"),
    (r"\b6\s*(e|eme)\b|sixieme", "6e"),
]


def niveau_depuis_dossier(dossiers: list[str]) -> str | None:
    """Année d'étude lue dans les noms de dossiers (le plus profond d'abord)."""
    for nom in reversed(dossiers):
        texte = unicodedata.normalize("NFKD", nom).encode("ascii", "ignore").decode().lower()
        texte = re.sub(r"[_\-.]+", " ", texte)
        for motif, niveau in _NIVEAUX_DOSSIER:
            if re.search(motif, texte):
                return niveau
    return None
