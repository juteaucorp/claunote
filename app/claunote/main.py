import html
import mimetypes
import os
import tempfile
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

import markdown
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.background import BackgroundTask

from claunote import db, rangement
from claunote.agent import NIVEAUX, TYPES
from claunote.kdrive import KDrive

INBOX_ID = int(os.environ.get("KDRIVE_INBOX_ID", "0"))
ETUDES_ID = int(os.environ.get("KDRIVE_ETUDES_ID", "0"))



@asynccontextmanager
async def lifespan(app):
    db.init()
    yield


app = FastAPI(title="Claunote", lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def surligner(extrait: str | None) -> Markup:
    """Échappe l'extrait de recherche puis rétablit seulement les <b> de ts_headline."""
    texte = html.escape(extrait or "")
    return Markup(texte.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>"))


templates.env.filters["surligner"] = surligner


def rendu_markdown(texte: str | None) -> str:
    # Le HTML brut éventuel de la transcription est neutralisé avant le rendu
    texte = (texte or "").replace("&", "&amp;").replace("<", "&lt;")
    return markdown.markdown(texte, extensions=["tables", "fenced_code"])


@app.get("/", response_class=HTMLResponse)
def bibliotheque(request: Request):
    arbre = defaultdict(lambda: defaultdict(list))
    for r in db.bibliotheque():
        arbre[r["niveau"]][r["matiere"]].append(r)
    niveaux = [n for n in NIVEAUX if n in arbre]
    return templates.TemplateResponse(request, "bibliotheque.html", {
        "arbre": arbre, "niveaux": niveaux, "nb_a_verifier": len(db.a_verifier())})


@app.get("/liste", response_class=HTMLResponse)
def liste(request: Request, niveau: str, matiere: str, type: str):
    return templates.TemplateResponse(request, "liste.html", {
        "niveau": niveau, "matiere": matiere, "type": type, "docs": db.liste(niveau, matiere, type)})


@app.get("/doc/{doc_id}", response_class=HTMLResponse)
def document(request: Request, doc_id: int):
    doc = db.document(doc_id)
    if doc is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "document.html", {
        "doc": doc, "transcription": rendu_markdown(doc["transcription"])})


@app.get("/doc/{doc_id}/fichier")
def fichier(doc_id: int):
    """Sert le scan original, récupéré à la volée depuis kDrive."""
    doc = db.document(doc_id)
    if doc is None:
        raise HTTPException(404)
    tmp = Path(tempfile.mkdtemp())
    chemin = KDrive.from_env().download(doc["kdrive_id"], tmp / doc["nom"])
    media = mimetypes.guess_type(doc["nom"])[0] or "application/octet-stream"
    return FileResponse(chemin, media_type=media, content_disposition_type="inline",
                        filename=doc["nom"], background=BackgroundTask(lambda: _nettoyer(tmp)))


def _nettoyer(dossier: Path):
    for f in dossier.iterdir():
        f.unlink()
    dossier.rmdir()


@app.get("/recherche", response_class=HTMLResponse)
def recherche(request: Request, q: str = ""):
    resultats = db.recherche(q) if q.strip() else []
    return templates.TemplateResponse(request, "recherche.html", {"q": q, "resultats": resultats})


@app.get("/a-verifier", response_class=HTMLResponse)
def a_verifier(request: Request):
    return templates.TemplateResponse(request, "a_verifier.html", {
        "docs": db.a_verifier(), "niveaux": NIVEAUX, "types": TYPES, "matieres": db.matieres()})


@app.post("/doc/{doc_id}/classer")
def classer(doc_id: int, niveau: str = Form(...), matiere: str = Form(...), type: str = Form(...)):
    """Classement manuel : déplace le scan et sa transcription dans le bon dossier."""
    doc = db.document(doc_id)
    if doc is None or niveau not in NIVEAUX or type not in TYPES or not matiere.strip():
        raise HTTPException(400, "classement invalide")
    matiere = matiere.strip()
    kd = KDrive.from_env()
    dest_id = kd.ensure_path(ETUDES_ID, rangement.dossier(niveau, matiere, type))
    kd.move(doc["kdrive_id"], dest_id)
    if doc["kdrive_md_id"]:
        kd.move(doc["kdrive_md_id"], dest_id)
    db.maj(doc_id, statut="classe", niveau=niveau, matiere=matiere, type=type, confiance=1.0)
    return RedirectResponse("/a-verifier", status_code=303)


@app.post("/doc/{doc_id}/reessayer")
def reessayer(doc_id: int):
    """Remet un document en erreur dans la file : le worker le réanalysera."""
    db.maj(doc_id, statut="nouveau", erreur=None)
    db.demander_scan()
    return RedirectResponse("/a-verifier", status_code=303)


@app.post("/scan", response_class=HTMLResponse)
def scan():
    db.demander_scan()
    return "Scan demandé : le worker vérifie l'inbox dans les 5 secondes."


@app.get("/health")
def health():
    try:
        KDrive.from_env().stat(INBOX_ID)
        return {"ok": True, "kdrive": True}
    except Exception as e:
        return {"ok": False, "kdrive": False, "erreur": str(e)}


@app.get("/inbox")
def inbox():
    items = KDrive.from_env().list_dir(INBOX_ID)
    return {"fichiers": sorted(i["name"] for i in items if i["type"] == "file")}
