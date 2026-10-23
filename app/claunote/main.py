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
from urllib.parse import parse_qs, urlencode

import markdown
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.background import BackgroundTask

from claunote import anki, auth, db, pronote, rangement, revision
from claunote.agent import NIVEAUX, TYPES
from claunote.kdrive import KDrive

INBOX_ID = int(os.environ.get("KDRIVE_INBOX_ID", "0"))
ETUDES_ID = int(os.environ.get("KDRIVE_ETUDES_ID", "0"))



@asynccontextmanager
async def lifespan(app):
    db.init()
    secret = os.environ.get("CLAUNOTE_SECRET") or db.secret_session()
    app.state.compte = auth.Compte.from_env(secret.encode(), db.etat_valeur("mot_de_passe"))
    if app.state.compte.par_defaut:
        print("ATTENTION : mot de passe par défaut (admin / claunote), "
              "change-le sur la page Compte du site", flush=True)
    yield


app = FastAPI(title="Claunote", lifespan=lifespan)
PUBLIC = ("/connexion", "/static/", "/manifest.webmanifest", "/sw.js", "/hors-ligne")


@app.middleware("http")
async def exiger_connexion(request: Request, call_next):
    chemin = request.url.path
    if chemin.startswith(PUBLIC) or request.app.state.compte.session_valide(request.cookies.get(auth.COOKIE)):
        return await call_next(request)
    if request.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": "/connexion"})
    if request.method != "GET":
        return Response("connexion requise", status_code=401)
    suite = chemin + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/connexion?{urlencode({'suite': suite})}", status_code=303)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["nb_a_verifier"] = lambda: db.nb_a_verifier()
templates.env.globals["nb_evaluations_proches"] = lambda: db.nb_evaluations_proches()
templates.env.globals["nb_cartes_a_revoir"] = lambda: db.nb_cartes_a_revoir()
templates.env.globals["mdp_par_defaut"] = lambda: app.state.compte.par_defaut
templates.env.filters["note"] = pronote.libelle_note
templates.env.filters["paris"] = lambda d: d.astimezone(pronote.PARIS).strftime("%d/%m/%Y %H:%M") if d else ""
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


# --- Connexion --------------------------------------------------------------

def _suite_sure(suite: str) -> str:
    # Uniquement un chemin du site : pas de redirection vers un autre domaine
    return suite if suite.startswith("/") and not suite.startswith("//") and "\\" not in suite else "/"


@app.get("/connexion", response_class=HTMLResponse)
def page_connexion(request: Request, suite: str = "/"):
    return templates.TemplateResponse(request, "connexion.html", {"suite": _suite_sure(suite), "sans_nav": True})


@app.post("/connexion", response_class=HTMLResponse)
def connexion(request: Request, utilisateur: str = Form(...), mot_de_passe: str = Form(...), suite: str = Form("/")):
    compte = request.app.state.compte
    ip = request.client.host if request.client else "?"
    attente = compte.bloque(ip)
    if attente or not compte.verifier(ip, utilisateur.strip(), mot_de_passe):
        attente = attente or compte.bloque(ip)
        erreur = (f"Trop d'essais : réessaie dans {attente // 60 + 1} min." if attente
                  else "Identifiant ou mot de passe incorrect.")
        return templates.TemplateResponse(request, "connexion.html", {"suite": _suite_sure(suite), "erreur": erreur,
                                                                       "sans_nav": True},
                                          status_code=401)
    r = RedirectResponse(_suite_sure(suite), status_code=303)
    _cookie_session(request, r, compte)
    return r


def _cookie_session(request: Request, reponse: Response, compte: auth.Compte):
    https = request.url.scheme == "https" or request.headers.get("X-Forwarded-Proto") == "https"
    reponse.set_cookie(auth.COOKIE, compte.creer_session(), max_age=auth.DUREE, httponly=True, samesite="lax",
                       secure=https)


@app.get("/compte", response_class=HTMLResponse)
def page_compte(request: Request):
    return templates.TemplateResponse(request, "compte.html", {"utilisateur": request.app.state.compte.utilisateur})


@app.post("/compte", response_class=HTMLResponse)
def changer_mot_de_passe(request: Request, actuel: str = Form(...), nouveau: str = Form(...),
                         confirmation: str = Form(...)):
    compte = request.app.state.compte
    ip = request.client.host if request.client else "?"
    erreur = None
    if compte.bloque(ip) or not compte.verifier(ip, compte.utilisateur, actuel):
        erreur = "Mot de passe actuel incorrect."
    elif len(nouveau) < 8:
        erreur = "Le nouveau mot de passe doit faire au moins 8 caractères."
    elif nouveau != confirmation:
        erreur = "Les deux nouveaux mots de passe sont différents."
    elif nouveau == auth.DEFAUT_MOT_DE_PASSE:
        erreur = "Choisis un autre mot de passe que celui par défaut."
    if erreur:
        return templates.TemplateResponse(request, "compte.html", {"utilisateur": compte.utilisateur, "erreur": erreur},
                                          status_code=400)
    hache = auth.hacher(nouveau)
    db.etat_ecrire("mot_de_passe", hache)
    # Nouveau compte = nouvelles signatures : les autres appareils sont déconnectés, pas celui-ci
    request.app.state.compte = auth.Compte(compte.utilisateur, hache, compte.secret)
    r = templates.TemplateResponse(request, "compte.html", {
        "utilisateur": compte.utilisateur,
        "message": "Mot de passe changé. Les autres appareils devront se reconnecter."})
    _cookie_session(request, r, request.app.state.compte)
    return r


@app.post("/deconnexion")
def deconnexion():
    r = RedirectResponse("/connexion", status_code=303)
    r.delete_cookie(auth.COOKIE)
    return r


# --- Bibliothèque ----------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def accueil(request: Request):
    """Aujourd'hui : ce qui arrive (évals, devoirs), ce qui attend (cartes, docs des profs)."""
    matieres = sorted({r["matiere"] for r in db.niveaux_matieres()})
    evaluations = [{**e, "cours": pronote.trouver_matiere(e["matiere"], matieres)}
                   for e in db.evenements_a_venir("evaluation", 21)]
    return templates.TemplateResponse(request, "accueil.html", {
        "aujourdhui": dt.datetime.now(pronote.PARIS).date(), "paris": pronote.PARIS,
        "evaluations": evaluations[:6], "devoirs": [d for d in db.evenements_a_venir("devoir", 7) if not d["fait"]],
        "documents": db.documents_pronote(5), "revisions": db.revisions(4),
        "synchro": db.etat_valeur("pronote_synchro"), "nb_documents": db.nb_documents()})


@app.get("/bibliotheque", response_class=HTMLResponse)
def bibliotheque(request: Request):
    arbre = defaultdict(lambda: defaultdict(list))
    for r in db.bibliotheque():
        arbre[r["niveau"]][r["matiere"]].append(r)
    niveaux = [n for n in NIVEAUX if n in arbre]
    return templates.TemplateResponse(request, "bibliotheque.html", {
        "arbre": arbre, "niveaux": niveaux, "nb_hors_cours": db.nb_hors_cours()})


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
        "doc": doc, "transcription": rendu_markdown(doc["transcription"]),
        "niveaux": NIVEAUX, "types": TYPES, "matieres": db.matieres()})


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
    docs = db.a_verifier()
    return templates.TemplateResponse(request, "a_verifier.html", {
        "doutes": [d for d in docs if d["statut"] != "erreur"], "erreurs": [d for d in docs if d["statut"] == "erreur"],
        "niveaux": NIVEAUX, "types": TYPES, "matieres": db.matieres()})


ERREURS = [  # motif dans le message technique → explication et conseil
    (r"limit|429", "La limite de ton forfait Claude était atteinte.", "Réessaie plus tard : ça repartira tout seul."),
    (r"trop long", "L'analyse a pris plus de 30 minutes.",
     "Document très long ? Découpe le PDF en plusieurs fichiers, ou classe-le à la main."),
    (r"illisible|zip|aucun texte", "Le fichier est abîmé, protégé ou vide.",
     "Ouvre-le pour vérifier ; s'il est inutile, supprime-le, sinon classe-le à la main."),
    (r"JSON|pas de JSON", "Claude n'a pas réussi à lire le document.",
     "Scan flou, page blanche ou photo de travers ? Réessaie, ou classe-le à la main."),
]


def explication_erreur(message: str | None) -> tuple[str, str]:
    for motif, explication, conseil in ERREURS:
        if re.search(motif, message or "", re.I):
            return explication, conseil
    return "L'analyse a échoué.", "Réessaie ; si ça recommence, classe-le à la main ou mets-le hors cours."


templates.env.filters["explication_erreur"] = explication_erreur


def _deplacer(doc, parts: list[str]):
    kd = KDrive.from_env()
    dest_id = kd.ensure_path(ETUDES_ID, parts)
    kd.move(doc["kdrive_id"], dest_id)
    if doc["kdrive_md_id"]:
        kd.move(doc["kdrive_md_id"], dest_id)


@app.post("/doc/{doc_id}/classer")
def classer(doc_id: int, niveau: str = Form(...), matiere: str = Form(...), type: str = Form(...),
            retour: str = Form("/a-verifier")):
    """Classement manuel : déplace le scan et sa transcription dans le bon dossier."""
    doc = db.document(doc_id)
    if doc is None or niveau not in NIVEAUX or type not in TYPES or not matiere.strip():
        raise HTTPException(400, "classement invalide")
    matiere = matiere.strip()
    _deplacer(doc, rangement.dossier(niveau, matiere, type))
    db.maj(doc_id, statut="classe", niveau=niveau, matiere=matiere, type=type, confiance=1.0, note=None, erreur=None)
    return RedirectResponse(_suite_sure(retour), status_code=303)


@app.post("/doc/{doc_id}/hors-cours")
def mettre_hors_cours(doc_id: int, retour: str = Form("/hors-cours")):
    """Document administratif ou sans intérêt pour réviser : rangé à part, hors de la bibliothèque."""
    doc = db.document(doc_id)
    if doc is None or doc["statut"] == "nouveau":
        raise HTTPException(400, "document en cours d'analyse")
    _deplacer(doc, rangement.HORS_COURS)
    db.maj(doc_id, statut="hors_cours", note=None, erreur=None)
    return RedirectResponse(_suite_sure(retour), status_code=303)


@app.get("/hors-cours", response_class=HTMLResponse)
def page_hors_cours(request: Request):
    return templates.TemplateResponse(request, "hors_cours.html", {
        "docs": db.hors_cours(), "niveaux": NIVEAUX, "types": TYPES, "matieres": db.matieres()})


# --- Organiser : actions de masse -------------------------------------------

def _date_heure(valeur: str):
    """« 2026-10-23T14:00 » saisi sur le site, heure de Paris."""
    try:
        return dt.datetime.fromisoformat(valeur).replace(tzinfo=pronote.PARIS) if valeur else None
    except ValueError:
        raise HTTPException(400, f"date invalide : {valeur}")


ACTIONS = {"niveau": "Changer l'année", "matiere": "Changer la matière", "type": "Changer le type",
           "ia": "Ranger par l'IA (matière et type, garde l'année)", "hors_cours": "Mettre hors cours",
           "cours": "Remettre dans les cours", "supprimer": "Supprimer (corbeille kDrive)"}


PAR_PAGE = (25, 50, 100)
SUPPRESSION_MAX = 200


def _filtres_organiser(du="", au="", niveau="", matiere="", type="", statut="", texte="") -> dict:
    return {"du": _date_heure(du), "au": _date_heure(au), "niveau": niveau, "matiere": matiere, "type_": type,
            "statut": statut, "texte": texte}


@app.get("/organiser", response_class=HTMLResponse)
def organiser(request: Request, du: str = "", au: str = "", niveau: str = "", matiere: str = "", type: str = "",
              statut: str = "", texte: str = "", page: int = 1, par: int = 25, message: str = ""):
    filtres = {"du": du, "au": au, "niveau": niveau, "matiere": matiere, "type": type, "statut": statut, "texte": texte}
    par = par if par in PAR_PAGE else PAR_PAGE[0]
    page = max(page, 1)
    docs, total = db.organiser(_filtres_organiser(**filtres), par, (page - 1) * par)
    pages = max(1, -(-total // par))
    if page > pages:  # page demandée au-delà de la fin (après une action de masse)
        page = pages
        docs, total = db.organiser(_filtres_organiser(**filtres), par, (page - 1) * par)
    actifs = {k: v for k, v in filtres.items() if v}
    return templates.TemplateResponse(request, "organiser.html", {
        "docs": docs, "total": total, "page": page, "pages": pages, "par": par, "par_page": PAR_PAGE,
        "f": filtres, "nb_filtres": len(actifs), "requete": urlencode({**filtres, "par": par}),
        "lien_page": lambda n, p=par: "/organiser?" + urlencode({**filtres, "par": p, "page": n}),
        "message": message, "niveaux": NIVEAUX, "types": TYPES, "matieres": db.matieres(), "statuts": db.STATUTS,
        "actions": ACTIONS, "sans_annee": db.SANS_ANNEE})


@app.post("/organiser")
def action_de_masse(ids: list[int] = Form([]), action: str = Form(...), nouveau_niveau: str = Form(""),
                    nouvelle_matiere: str = Form(""), nouveau_type: str = Form(""), requete: str = Form(""),
                    tous: str = Form("")):
    """`tous` : l'action porte sur tous les résultats du filtre, pas seulement la page affichée."""
    valeur = {"niveau": nouveau_niveau, "matiere": nouvelle_matiere, "type": nouveau_type}.get(action, "").strip()
    if action not in ACTIONS:
        raise HTTPException(400, "action inconnue")
    if (action == "niveau" and valeur not in NIVEAUX) or (action == "type" and valeur not in TYPES) \
            or (action == "matiere" and not valeur):
        raise HTTPException(400, "choisis la nouvelle valeur")
    if tous == "1":
        criteres = {k: v[0] for k, v in parse_qs(requete).items()
                    if k in ("du", "au", "niveau", "matiere", "type", "statut", "texte")}
        ids = db.ids_filtres(_filtres_organiser(**criteres))
    if action == "supprimer":
        if len(ids) > SUPPRESSION_MAX:
            raise HTTPException(400, f"pas plus de {SUPPRESSION_MAX} suppressions à la fois")
        n = sum(_supprimer(i) for i in ids)
        message = f"{n} document{'s' if n > 1 else ''} supprimé{'s' if n > 1 else ''} (fichiers dans la corbeille kDrive)."
        return RedirectResponse(f"/organiser?{requete}&{urlencode({'message': message})}", status_code=303)
    if action in ("niveau", "matiere", "type"):
        n = db.maj_masse(ids, **{action: valeur})
        db.completer_classement(ids)
    elif action == "ia":
        n = db.maj_masse(ids, statut="reclasser")
    else:
        n = db.maj_masse(ids, statut="hors_cours" if action == "hors_cours" else "a_verifier")
        db.completer_classement(ids)
    if n:
        db.demander_scan()  # réveille le worker : il déplace les fichiers dans kDrive
    message = f"{n} document{'s' if n > 1 else ''} modifié{'s' if n > 1 else ''}."
    if action == "ia" and n:
        message += " L'IA les range dans les minutes qui viennent (recharge la page)."
    elif n:
        message += " Les fichiers sont déplacés dans kDrive en arrière-plan."
    if len(ids) > n:
        message += f" {len(ids) - n} ignoré{'s' if len(ids) - n > 1 else ''} (en analyse, ou en erreur pour l'IA)."
    return RedirectResponse(f"/organiser?{requete}&{urlencode({'message': message})}", status_code=303)


def _supprimer(doc_id: int) -> bool:
    """Scan et transcription à la corbeille kDrive (récupérables 30 jours), puis oubli en base."""
    doc = db.document(doc_id)
    if doc is None:
        return False
    kd = KDrive.from_env()
    for fid in (doc["kdrive_id"], doc["kdrive_md_id"]):
        if fid:
            try:
                kd.delete(fid)
            except Exception as e:  # déjà supprimé à la main dans kDrive : on oublie quand même
                print(f"suppression kDrive impossible ({fid}) : {e}", flush=True)
    db.supprimer(doc_id)
    return True


@app.post("/doc/{doc_id}/supprimer")
def supprimer_document(doc_id: int, retour: str = Form("/a-verifier")):
    if not _supprimer(doc_id):
        raise HTTPException(404)
    return RedirectResponse(_suite_sure(retour), status_code=303)


@app.post("/doc/{doc_id}/reessayer")
def reessayer(doc_id: int, retour: str = Form("/a-verifier")):
    """Relance l'analyse : le worker reprend le fichier dans l'inbox kDrive."""
    doc = db.document(doc_id)
    if doc is None:
        raise HTTPException(404)
    if doc["statut"] != "erreur":  # déjà rangé ailleurs : on le remet dans l'inbox
        KDrive.from_env().move(doc["kdrive_id"], INBOX_ID)
    db.maj(doc_id, statut="nouveau", erreur=None)
    db.demander_scan()
    return RedirectResponse(_suite_sure(retour), status_code=303)


@app.post("/a-verifier/reessayer-tout")
def reessayer_tout():
    if db.reessayer_erreurs():
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
    contenu, questions, cartes = None, [], []
    if rev["statut"] == "pret":
        if rev["genre"] == "quiz":
            questions = json.loads(rev["contenu"])
        elif rev["genre"] == "cartes":
            cartes = db.cartes(rev_id)
        else:
            contenu = avec_sources(rendu_markdown(rev["contenu"]), reperes)
    return templates.TemplateResponse(request, "revision.html", {
        "rev": rev, "genres": revision.GENRES, "contenu": contenu, "questions": questions, "cartes": cartes,
        "reperes": reperes, "sources": db.documents(list(reperes.values()))})


# --- Flashcards --------------------------------------------------------------

@app.get("/cartes", response_class=HTMLResponse)
def page_cartes(request: Request):
    return templates.TemplateResponse(request, "cartes.html", {"paquets": db.paquets()})


@app.get("/cartes/session", response_class=HTMLResponse)
def session_cartes(request: Request, paquet: int | None = None):
    """Une carte à la fois : recto, on retourne, on note de « À revoir » à « Facile »."""
    carte = db.prochaine_carte(paquet)
    reperes = (carte["reperes"] or {}) if carte else {}
    return templates.TemplateResponse(request, "session_cartes.html", {
        "carte": carte, "paquet": paquet, "notes": revision.NOTES, "reperes": reperes,
        "reste": db.nb_cartes_a_revoir(paquet),
        # Dans combien de jours la carte reviendra, pour chaque bouton
        "apercu": {n: revision.repetition(carte["facilite"], carte["intervalle"], carte["repetitions"], n)[1]
                   for n in revision.NOTES} if carte else {}})


@app.post("/cartes/{carte_id}/noter")
def noter_carte(carte_id: int, note: int = Form(...), paquet: int | None = Form(None)):
    carte = db.carte(carte_id)
    if carte is None or note not in revision.NOTES:
        raise HTTPException(400, "carte ou note invalide")
    db.maj_carte(carte_id, *revision.repetition(carte["facilite"], carte["intervalle"], carte["repetitions"], note))
    return RedirectResponse("/cartes/session" + (f"?paquet={paquet}" if paquet else ""), status_code=303)


@app.get("/cartes/anki")
def export_anki(paquet: int | None = None):
    """Paquet .apkg à ouvrir dans Anki / AnkiDroid ; sans `paquet`, toutes les flashcards."""
    par_rev = defaultdict(list)
    for k in db.cartes(paquet):
        par_rev[(k["revision_id"], k["sujet"])].append(k)
    if not par_rev:
        raise HTTPException(404, "aucune flashcard")
    reperes = {rev_id: ks[0]["reperes"] or {} for (rev_id, _), ks in par_rev.items()}
    titres = {d["id"]: d["titre"] or d["nom"]
              for d in db.documents(sorted({i for r in reperes.values() for i in r.values()}))}
    decks = [anki.paquet(rev_id, sujet, ks, {r: titres.get(i, "") for r, i in reperes[rev_id].items()})
             for (rev_id, sujet), ks in par_rev.items()]
    nom = (f"claunote-{rangement.slug(next(iter(par_rev))[1])[:40]}" if paquet else "claunote") + ".apkg"
    return Response(anki.exporter(decks), media_type="application/apkg",
                    headers={"Content-Disposition": f'attachment; filename="{nom}"'})


# --- PWA (appli installable) -------------------------------------------------

STATIC = Path(__file__).parent / "static"


@app.get("/manifest.webmanifest")
def manifeste():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # Servi à la racine pour contrôler tout le site
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/hors-ligne", response_class=HTMLResponse)
def hors_ligne(request: Request):
    return templates.TemplateResponse(request, "hors_ligne.html", {"sans_nav": True})


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
                "documents": db.documents_pronote(30),
                "notes": db.notes_recentes(15), "synchro": db.etat_valeur("pronote_synchro"),
                "erreurs_synchro": db.etat_valeur("pronote_erreurs"), "paris": pronote.PARIS}
    return templates.TemplateResponse(request, "agenda.html", ctx)


def _demander_documents():
    """Le worker récupère les documents des profs (trop long pour une page web)."""
    db.etat_ecrire("pronote_demande", "oui")
    db.demander_scan()  # réveille le worker


def _connecter(request: Request, connexion) -> HTMLResponse:
    try:
        connexion()
        r = pronote.synchroniser()
    except pronote.ErreurPronote as e:
        return _agenda(request, erreur=str(e))
    _demander_documents()
    return _agenda(request, message=f"Connecté à Pronote : {r.get('evenements', 0)} évaluation(s) et devoir(s) récupérés. "
                                    "Les documents des profs arrivent dans la bibliothèque dans quelques minutes.")


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
    _demander_documents()
    msg = (f"Synchronisé : {r.get('evenements', 0)} évaluation(s) et devoir(s). "
           "Les nouveaux documents des profs sont récupérés en arrière-plan.")
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
