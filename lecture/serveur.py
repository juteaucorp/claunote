"""Service lecture : les petits modèles d'IA locaux qui ont besoin de PyTorch, à part du site et du worker.

    POST /markdown  PDF numérique ──► Docling (mise en page, tableaux ; pas d'OCR) ──► Markdown
    POST /formule   photo d'une formule ──► pix2tex ──► LaTeX
    POST /classer   question + passages ──► FlashRank (multilingue) ──► un score par passage
    GET  /sante     ce qui est installé et chargé

Chaque modèle est chargé à son premier usage (puis gardé), un seul calcul à la fois par modèle :
la VM n'a pas de carte graphique, on ne la sature pas. Rien ne sort de la VM.
"""
import io
import os
import tempfile
import threading
from importlib.util import find_spec
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

THREADS = int(os.environ.get("LECTURE_THREADS", "2"))
PAGES_MAX = int(os.environ.get("LECTURE_PAGES_MAX", "80"))
MODELE_CLASSEMENT = os.environ.get("LECTURE_CLASSEMENT", "ms-marco-MultiBERT-L-12")   # multilingue (français)
MODELES = Path(os.environ.get("LECTURE_MODELES", "/modeles"))

app = FastAPI(title="Claunote lecture")
_modeles: dict[str, object] = {}
_verrous = {nom: threading.Lock() for nom in ("docling", "formules", "classement")}


def _torch():
    import torch
    torch.set_num_threads(THREADS)


def _charger(nom: str):
    if nom not in _modeles:
        if nom == "docling":
            _torch()
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
            options = PdfPipelineOptions(do_ocr=False, do_table_structure=True)  # les scans passent par l'OCR du worker
            _modeles[nom] = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
        elif nom == "formules":
            _torch()
            from pix2tex.cli import LatexOCR
            _modeles[nom] = LatexOCR()
        elif nom == "classement":
            from flashrank import Ranker
            _modeles[nom] = Ranker(model_name=MODELE_CLASSEMENT, cache_dir=str(MODELES / "flashrank"), max_length=512)
    return _modeles[nom]


@app.get("/sante")
def sante():
    installes = {"docling": find_spec("docling") is not None, "formules": find_spec("pix2tex") is not None,
                 "classement": find_spec("flashrank") is not None}
    return {**installes, "charges": sorted(_modeles)}


@app.post("/markdown")
def markdown(fichier: UploadFile = File(...)):
    suffixe = Path(fichier.filename or "x.pdf").suffix.lower() or ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffixe) as f:
        f.write(fichier.file.read())
        f.flush()
        try:
            with _verrous["docling"]:
                resultat = _charger("docling").convert(f.name, max_num_pages=PAGES_MAX)
        except Exception as e:
            raise HTTPException(422, f"Docling n'a pas lu le fichier : {e}") from e
    document = resultat.document
    return {"markdown": document.export_to_markdown(), "pages": len(getattr(document, "pages", {}) or {})}


@app.post("/formule")
def formule(image: UploadFile = File(...)):
    from PIL import Image, UnidentifiedImageError
    try:
        photo = Image.open(io.BytesIO(image.file.read())).convert("RGB")
    except (UnidentifiedImageError, OSError) as e:
        raise HTTPException(422, "image illisible") from e
    photo.thumbnail((1200, 1200))   # pix2tex attend une formule recadrée, pas une page entière
    with _verrous["formules"]:
        latex = _charger("formules")(photo)
    return {"latex": latex}


class Classement(BaseModel):
    question: str
    passages: list[str]


@app.post("/classer")
def classer(c: Classement):
    from flashrank import RerankRequest
    if not c.passages:
        return {"scores": []}
    with _verrous["classement"]:
        resultats = _charger("classement").rerank(RerankRequest(
            query=c.question, passages=[{"id": i, "text": t[:2000]} for i, t in enumerate(c.passages)]))
    scores = [0.0] * len(c.passages)
    for r in resultats:
        scores[int(r["id"])] = float(r["score"])
    return {"scores": scores}
