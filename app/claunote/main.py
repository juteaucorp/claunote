import datetime as dt
import html
import json
import mimetypes
import re
import os
import tempfile
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

import markdown
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.background import BackgroundTask

from claunote import db, pronote, rangement, revision
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
templates.env.globals["nb_a_verifier"] = lambda: db.nb_a_verifier()
templates.env.globals["nb_evaluations_proches"] = lambda: db.nb_evaluations_proches()
templates.env.filters["note"] = pronote.libelle_note
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp"}


def surligner(extrait: str | None) -> Markup:
    """Échappe l'extrait de recherche puis rétablit seulement les <b> de ts_headline."""
    texte = html.escape(extrait or "")
    return Markup(texte.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>"))


templates.env.filters["surligner"] = surligner


def rendu_markdown(texte: str | None) -> str:
    # Le HTML brut éventuel de la transcription est neutralisé avant le rendu
    texte = (texte or "").replace("&", "&amp;").replace("<", "&lt;")
    return markdown.markdown(texte, extensions=["tables", "fenced_code"])


def avec_sources(rendu: str, reperes: dict) -> Markup:
    """Transforme les repères [D1] du texte en liens vers le cours d'origine."""
    def lien(m):
        doc_id = reperes.get(m.group(1))
        return f'<a href="/doc/{doc_id}">[{m.group(1)}]</a>' if doc_id else m.group(0)
    return Markup(re.sub(r"\[(D\d+)\]", lien, rendu))


@app.get("/", response_class=HTMLResponse)
def bibliotheque(request: Request):
    arbre = defaultdict(lambda: defaultdict(list))
    for r in db.bibliotheque():
        arbre[r["niveau"]][r["matiere"]].append(r)
    niveaux = [n for n in NIVEAUX if n in arbre]
    return templates.TemplateResponse(request, "bibliotheque.html", {
        "arbre": arbre, "niveaux": niveaux})


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


@app.get("/deposer", response_class=HTMLResponse)
def page_deposer(request: Request):
    return templates.TemplateResponse(request, "deposer.html", {"niveaux": NIVEAUX})


@app.post("/deposer", response_class=HTMLResponse)
def deposer(request: Request, fichiers: list[UploadFile] = File(...), niveau: str = Form("")):
    """Envoie les fichiers dans l'inbox kDrive, en gardant les dossiers d'un dossier déposé.

    Un niveau choisi devient un sous-dossier de l'inbox : le worker s'en sert comme année d'étude.
    """
    if niveau and niveau not in NIVEAUX:
        raise HTTPException(400, "niveau inconnu")
    kd = KDrive.from_env()
    dossiers_kdrive: dict[tuple, int] = {}
    envoyes, ignores = [], []
    for f in fichiers:
        parts = [p for p in PurePosixPath((f.filename or "").replace("\\", "/")).parts if p not in ("", ".", "..", "/")]
        if not parts:
            continue
        chemin = ([niveau] if niveau else []) + parts
        if PurePosixPath(parts[-1]).suffix.lower() not in EXTENSIONS:
            ignores.append("/".join(parts))
            continue
        dossiers = tuple(chemin[:-1])
        if dossiers not in dossiers_kdrive:
            dossiers_kdrive[dossiers] = kd.ensure_path(INBOX_ID, list(dossiers))
        kd.upload(dossiers_kdrive[dossiers], chemin[-1], f.file.read(), conflict="rename")
        envoyes.append("/".join(chemin))
    if envoyes:
        db.demander_scan()
    return templates.TemplateResponse(request, "deposer.html", {
        "niveaux": NIVEAUX, "envoyes": envoyes, "ignores": ignores})


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
    db.maj(doc_id, statut="classe", niveau=niveau, matiere=matiere, type=type, confiance=1.0, note=None)
    return RedirectResponse("/a-verifier", status_code=303)


@app.post("/doc/{doc_id}/reessayer")
def reessayer(doc_id: int):
    """Remet un document en erreur dans la file : le worker le réanalysera."""
    db.maj(doc_id, statut="nouveau", erreur=None)
    db.demander_scan()
    return RedirectResponse("/a-verifier", status_code=303)


@app.get("/reviser", response_class=HTMLResponse)
def reviser(request: Request, sujet: str = "", matiere: str = "", date_eval: str = "", genre: str = ""):
    """Les paramètres préremplissent le formulaire (depuis l'agenda Pronote)."""
    return templates.TemplateResponse(request, "reviser.html", {
        "choix": db.niveaux_matieres(), "genres": revision.GENRES, "revisions": db.revisions(),
        "demain": dt.date.today() + dt.timedelta(days=1),
        "pre": {"sujet": sujet, "matiere": matiere, "date_eval": date_eval, "genre": genre}})


@app.post("/reviser")
def demander_revision(genre: str = Form(...), sujet: str = Form(""), niveau_matiere: str = Form(""),
                      date_eval: str = Form(""), doc_id: int | None = Form(None)):
    """Crée une demande de révision ; le worker la traite en priorité."""
    if genre not in revision.GENRES:
        raise HTTPException(400, "genre inconnu")
    eval_le = dt.date.fromisoformat(date_eval) if date_eval else None
    if genre == "plan" and (eval_le is None or eval_le <= dt.date.today()):
        raise HTTPException(400, "un plan de révision demande une date d'évaluation future")
    sujet = sujet.strip()
    if doc_id is not None:
        doc = db.document(doc_id)
        if doc is None:
            raise HTTPException(404)
        ids, sujet = [doc_id], sujet or doc["titre"] or doc["nom"]
    else:
        niveau, _, matiere = niveau_matiere.partition("|")
        if not matiere:
            raise HTTPException(400, "choisis un niveau et une matière")
        # Le sujet sert de recherche ; s'il ne trouve rien, on prend toute la matière
        ids = db.choisir_documents(niveau, matiere, sujet) or db.choisir_documents(niveau, matiere)
        sujet = sujet or f"{matiere} ({niveau})"
    if not ids:
        raise HTTPException(400, "aucun document classé pour ce choix")
    rev_id = db.creer_revision(genre, sujet, ids, eval_le)
    db.demander_scan()
    return RedirectResponse(f"/revision/{rev_id}", status_code=303)


@app.get("/revision/{rev_id}", response_class=HTMLResponse)
def voir_revision(request: Request, rev_id: int):
    rev = db.revision(rev_id)
    if rev is None:
        raise HTTPException(404)
    reperes = rev["reperes"] or {}
    contenu, questions = None, []
    if rev["statut"] == "pret":
        if rev["genre"] == "quiz":
            questions = json.loads(rev["contenu"])
        else:
            contenu = avec_sources(rendu_markdown(rev["contenu"]), reperes)
    return templates.TemplateResponse(request, "revision.html", {
        "rev": rev, "genres": revision.GENRES, "contenu": contenu, "questions": questions,
        "reperes": reperes, "sources": db.documents(list(reperes.values()))})


# --- Agenda (Pronote) -------------------------------------------------------

def _agenda(request: Request, erreur: str | None = None, message: str | None = None):
    try:
        etat = pronote.Client().etat()
    except pronote.ErreurPronote as e:
        etat, erreur = {"connecte": False, "injoignable": True}, erreur or str(e)
    ctx = {"etat": etat, "erreur": erreur, "message": message}
    if etat.get("connecte"):
        matieres = sorted({r["matiere"] for r in db.niveaux_matieres()})
        evaluations = [{**e, "cours": pronote.trouver_matiere(e["matiere"], matieres)}
                       for e in db.evenements_a_venir("evaluation", 60)]
        ctx |= {"evaluations": evaluations, "devoirs": db.evenements_a_venir("devoir", 14),
                "notes": db.notes_recentes(15), "synchro": db.etat_valeur("pronote_synchro"),
                "erreurs_synchro": db.etat_valeur("pronote_erreurs"), "paris": pronote.PARIS}
    return templates.TemplateResponse(request, "agenda.html", ctx)


def _connecter(request: Request, connexion) -> HTMLResponse:
    try:
        connexion()
        r = pronote.synchroniser()
    except pronote.ErreurPronote as e:
        return _agenda(request, erreur=str(e))
    return _agenda(request, message=f"Connecté à Pronote : {r.get('evenements', 0)} évaluation(s) et devoir(s) récupérés.")


@app.get("/agenda", response_class=HTMLResponse)
def agenda(request: Request):
    return _agenda(request)


@app.post("/agenda/connexion-qr", response_class=HTMLResponse)
def agenda_connexion_qr(request: Request, qr: str = Form(...), pin: str = Form(...)):
    return _connecter(request, lambda: pronote.Client().connexion_qr(qr.strip(), pin.strip()))


@app.post("/agenda/connexion", response_class=HTMLResponse)
def agenda_connexion(request: Request, url: str = Form(...), identifiant: str = Form(...),
                     mot_de_passe: str = Form(...)):
    return _connecter(request, lambda: pronote.Client().connexion_identifiants(url.strip(), identifiant.strip(),
                                                                               mot_de_passe))


@app.post("/agenda/synchroniser", response_class=HTMLResponse)
def agenda_synchroniser(request: Request):
    try:
        r = pronote.synchroniser()
    except pronote.ErreurPronote as e:
        return _agenda(request, erreur=str(e))
    msg = f"Synchronisé : {r.get('evenements', 0)} évaluation(s) et devoir(s)."
    if r.get("preparees"):
        msg += f" {len(r['preparees'])} révision(s) lancée(s)."
    return _agenda(request, message=msg)


@app.post("/agenda/deconnexion")
def agenda_deconnexion():
    try:
        pronote.Client().deconnexion()
    except pronote.ErreurPronote:
        pass
    return RedirectResponse("/agenda", status_code=303)


@app.post("/agenda/preparer")
def agenda_preparer(ev_id: str = Form(...)):
    """Évaluation Pronote → révision sur les cours de la matière (ou formulaire prérempli)."""
    ev = db.evenement(ev_id)
    if ev is None:
        raise HTTPException(404)
    if ev["revision_id"]:
        return RedirectResponse(f"/revision/{ev['revision_id']}", status_code=303)
    rev_id = pronote.preparer(ev)
    if rev_id:
        return RedirectResponse(f"/revision/{rev_id}", status_code=303)
    le = ev["le"].astimezone(pronote.PARIS).date()
    plan = (le - dt.date.today()).days >= pronote.JOURS_PLAN
    params = {"sujet": ev["titre"] if ev["id"].startswith("devoir:") else "",
              "genre": "plan" if plan else "fiche", "date_eval": le.isoformat() if plan else ""}
    return RedirectResponse(f"/reviser?{urlencode(params)}", status_code=303)


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
