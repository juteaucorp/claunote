"""Service lecture : les petits modèles d'IA locaux qui ont besoin de PyTorch, à part du site et du worker.

    POST /markdown  PDF numérique ──► Docling (mise en page, tableaux ; pas d'OCR) ──► Markdown
    POST /formule   photo d'une formule ──► pix2tex ──► LaTeX
    POST /classer   question + passages ──► FlashRank (multilingue) ──► un score par passage
    GET  /sante     ce qui est installé et chargé

Chaque modèle est chargé à son premier usage, un seul calcul à la fois par modèle : la VM n'a pas de
carte graphique, on ne la sature pas. Un modèle inutilisé depuis LECTURE_GARDER_MIN minutes (10) est
déchargé et sa mémoire rendue au système ; POST /liberer décharge tout de suite (mémoire de la VM basse).
Rien ne sort de la VM.
"""
import ctypes
import gc
import io
import os
import tempfile
import threading
import time
from importlib.util import find_spec
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

THREADS = int(os.environ.get("LECTURE_THREADS", "2"))
# Avant tout import de bibliothèque de calcul : sinon chacune prend tous les cœurs de la VM
for _variable in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_variable, str(THREADS))
PAGES_MAX = int(os.environ.get("LECTURE_PAGES_MAX", "80"))
MODELE_CLASSEMENT = os.environ.get("LECTURE_CLASSEMENT", "ms-marco-MultiBERT-L-12")   # multilingue (français)
MODELES = Path(os.environ.get("LECTURE_MODELES", "/modeles"))
GARDER = float(os.environ.get("LECTURE_GARDER_MIN", "10")) * 60

app = FastAPI(title="Claunote lecture")
_modeles: dict[str, object] = {}
_utilise: dict[str, float] = {}   # dernier usage de chaque modèle chargé
_verrous = {nom: threading.Lock() for nom in ("docling", "formules", "classement")}


def _rendre_memoire():
    """Après un déchargement : ramasse-miettes, puis la mémoire libre est vraiment rendue au système."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass


def decharger(noms=None) -> list[str]:
    """Décharge les modèles donnés (tous par défaut) qui ne calculent pas en ce moment."""
    partis = []
    for nom in list(noms or _modeles):
        verrou = _verrous[nom]
        if nom in _modeles and verrou.acquire(blocking=False):   # en plein calcul : on ne touche à rien
            try:
                _modeles.pop(nom, None)
                _utilise.pop(nom, None)
                partis.append(nom)
            finally:
                verrou.release()
    if partis:
        _rendre_memoire()
        print(f"modèles déchargés : {', '.join(partis)}", flush=True)
    return partis


def _surveiller():
    while True:
        time.sleep(60)
        maintenant = time.monotonic()
        decharger([n for n, t in list(_utilise.items()) if maintenant - t > GARDER])


threading.Thread(target=_surveiller, daemon=True, name="dechargement").start()


def _torch():
    import torch
    torch.set_num_threads(THREADS)


def _charger(nom: str):
    _utilise[nom] = time.monotonic()
    if nom not in _modeles:
        if nom == "docling":
            _torch()
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
            options = PdfPipelineOptions(do_ocr=False, do_table_structure=True)  # les scans passent par l'OCR du worker
            try:   # Docling a son propre réglage de cœurs (4 par défaut)
                try:
                    from docling.datamodel.accelerator_options import AcceleratorOptions
                except ImportError:
                    from docling.datamodel.pipeline_options import AcceleratorOptions
                options.accelerator_options = AcceleratorOptions(num_threads=THREADS, device="cpu")
            except Exception as e:
                print(f"Docling : cœurs non réglés ({e})", flush=True)
            _modeles[nom] = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
        elif nom == "formules":
            _torch()
            from pix2tex.cli import LatexOCR
            _modeles[nom] = LatexOCR()
        elif nom == "classement":
            from flashrank import Ranker
            classeur = Ranker(model_name=MODELE_CLASSEMENT, cache_dir=str(MODELES / "flashrank"), max_length=512)
            _limiter_onnx(classeur)
            _modeles[nom] = classeur
    return _modeles[nom]


def _limiter_onnx(classeur):
    """FlashRank ouvre sa session ONNX Runtime sur tous les cœurs : on la rouvre sur THREADS."""
    session = getattr(classeur, "session", None)
    chemin = getattr(session, "_model_path", None)
    if not chemin:
        return
    try:
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads, options.inter_op_num_threads = THREADS, 1
        classeur.session = ort.InferenceSession(chemin, sess_options=options, providers=["CPUExecutionProvider"])
    except Exception as e:   # la session d'origine reste : plus de cœurs, mais le tri marche
        print(f"FlashRank : cœurs non réglés ({e})", flush=True)


@app.get("/sante")
def sante():
    installes = {"docling": find_spec("docling") is not None, "formules": find_spec("pix2tex") is not None,
                 "classement": find_spec("flashrank") is not None}
    return {**installes, "charges": sorted(_modeles)}


@app.post("/liberer")
def liberer():
    """La VM manque de mémoire (le worker le voit) : tout ce qui ne calcule pas est déchargé."""
    return {"decharges": decharger()}


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
