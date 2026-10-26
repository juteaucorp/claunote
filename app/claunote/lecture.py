"""Lecture structurée des documents (Docling) et petits modèles locaux du service `lecture`.

    Word, PowerPoint, Excel ──► Docling (ici, sans modèle) ──► Markdown : titres, listes, tableaux,
                                                                équations Word en LaTeX
    PDF numérique ───────────► service lecture (Docling + modèles de mise en page) ──► Markdown + tableaux
    photo d'une formule ─────► service lecture (pix2tex) ──► LaTeX
    passages d'une question ─► service lecture (FlashRank) ──► les plus utiles d'abord

Tout est facultatif : si Docling ou le service manque (ou tombe), on garde l'extraction d'avant.
Le service n'est pas rappelé pendant PAUSE secondes après une panne : pas d'attente à chaque document.
"""
import logging
import os
import threading
import time
from pathlib import Path

import httpx

URL = os.environ.get("LECTURE_URL", "http://lecture:8000").rstrip("/")
PAUSE = 300
OFFICE = {".docx", ".pptx", ".xlsx"}
logging.getLogger("docling").setLevel(logging.WARNING)

_verrou = threading.Lock()
_convertisseur = None
_panne = {"jusqu_a": 0.0}


class LectureIndisponible(Exception):
    pass


# --- Bureautique : Docling dans ce conteneur -----------------------------------------------

def office(chemin: Path) -> str | None:
    """Markdown structuré d'un .docx/.pptx/.xlsx, ou None (Docling absent, fichier qu'il ne sait pas lire)."""
    global _convertisseur
    if chemin.suffix.lower() not in OFFICE:
        return None
    try:
        from docling.document_converter import DocumentConverter
    except ImportError:
        return None
    try:
        with _verrou:   # un seul convertisseur, partagé entre les fils du worker
            _convertisseur = _convertisseur or DocumentConverter()
            texte = _convertisseur.convert(chemin).document.export_to_markdown()
    except Exception as e:
        print(f"Docling n'a pas lu {chemin.name} : {e}", flush=True)
        return None
    return texte if texte.strip() else None


# --- Service lecture (PDF, formules, tri des passages) ----------------------------------

def _appel(methode: str, chemin: str, timeout: float, **k) -> dict:
    if time.time() < _panne["jusqu_a"]:
        raise LectureIndisponible("service lecture en pause après une panne")
    try:
        r = httpx.request(methode, f"{URL}{chemin}", timeout=timeout, **k)
    except httpx.HTTPError as e:
        _panne["jusqu_a"] = time.time() + PAUSE
        raise LectureIndisponible(f"service lecture injoignable : {e}") from e
    if r.status_code >= 500 or r.status_code == 404:
        raise LectureIndisponible(f"service lecture : erreur {r.status_code} {r.text[:200]}")
    if r.status_code >= 400:
        raise LectureIndisponible(r.json().get("detail", r.text[:200]) if r.headers.get("content-type", "").startswith(
            "application/json") else r.text[:200])
    return r.json()


def etat() -> dict:
    """Ce que le service sait faire : {"docling": bool, "formules": bool, "classement": bool} ({} s'il est absent)."""
    try:
        return _appel("GET", "/sante", 5)
    except LectureIndisponible:
        return {}


def pdf(chemin: Path, timeout: float = 600) -> str | None:
    """Markdown d'un PDF numérique (tableaux, titres, colonnes), ou None si le service ne répond pas."""
    try:
        with open(chemin, "rb") as f:
            d = _appel("POST", "/markdown", timeout, files={"fichier": (chemin.name, f, "application/pdf")})
    except (LectureIndisponible, OSError, ValueError) as e:
        print(f"lecture PDF par Docling impossible ({chemin.name}) : {e}", flush=True)
        return None
    texte = d.get("markdown") or ""
    return texte if texte.strip() else None


def meilleur_pdf(chemin: Path, texte_brut: str) -> str:
    """Le Markdown de Docling s'il ne perd rien par rapport au texte brut (pypdf), sinon le texte brut."""
    structure = pdf(chemin)
    if structure and len(structure) >= 0.6 * len(texte_brut):
        return structure
    return texte_brut


def formule(image: bytes, nom: str = "formule.png") -> str:
    """LaTeX d'une photo de formule (pix2tex). LectureIndisponible si le service ne sait pas le faire."""
    d = _appel("POST", "/formule", 120, files={"image": (nom, image, "application/octet-stream")})
    latex = (d.get("latex") or "").strip()
    if not latex:
        raise LectureIndisponible("aucune formule reconnue sur la photo")
    return latex


def classer(question: str, textes: list[str], timeout: float = 20) -> list[float] | None:
    """Score de chaque passage pour la question (FlashRank), ou None si le service ne répond pas."""
    if len(textes) < 2:
        return None
    try:
        scores = _appel("POST", "/classer", timeout, json={"question": question, "passages": textes}).get("scores")
    except LectureIndisponible:
        return None
    return scores if isinstance(scores, list) and len(scores) == len(textes) else None
