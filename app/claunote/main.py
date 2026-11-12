import base64
import hashlib
import datetime as dt
import html
import json
import mimetypes
import re
import os
import random
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, urlencode

import markdown
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.background import BackgroundTask

from claunote import (agent, anki, appareils, auth, carte_mentale, cles, db, decoupage, formules, importation, index,
                      lecture, local, maj, matieres, modeles, notifications, pdf, progression, pronote, question,
                      rangement, ressources, revision, schemas, sensible)
from claunote.agent import NIVEAUX, TYPES, ClaudeIndisponible, ErreurAgent, LimiteAtteinte
from claunote.kdrive import KDrive, KDriveIndisponible

INBOX_ID = int(os.environ.get("KDRIVE_INBOX_ID", "0"))
ETUDES_ID = int(os.environ.get("KDRIVE_ETUDES_ID", "0"))



@asynccontextmanager
async def lifespan(app):
    db.init()
    secret = os.environ.get("CLAUNOTE_SECRET") or db.secret_session()
    app.state.compte = auth.Compte.from_env(secret.encode(), db.etat_valeur("mot_de_passe"))
    agent.journal = _noter_appel  # les appels faits par le site (Demander, matières) vont sur la page État
    if app.state.compte.par_defaut:
        print("ATTENTION : mot de passe par défaut (admin / claunote), "
              "change-le sur la page Compte du site", flush=True)
    yield


def _noter_appel(**appel):
    try:
        db.journal_ia(**appel)
    except Exception as e:
        print(f"appel à Claude non noté : {e}", flush=True)


app = FastAPI(title="Claunote", lifespan=lifespan)
PUBLIC = ("/appareil", "/connexion", "/cles/connexion", "/static/", "/manifest.webmanifest", "/sw.js", "/hors-ligne", "/installer", "/certificat")


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


# Déclaré après exiger_connexion, donc exécuté avant : par Internet, l'appareil passe avant le mot de passe
@app.middleware("http")
async def appareil_approuve(request: Request, call_next):
    """Par Cloudflare Tunnel, seuls les appareils approuvés atteignent le site (voir appareils.py)."""
    chemin = request.url.path
    if (not appareils.par_tunnel(request.headers) or not appareils.actif() or chemin == "/appareil"
            or chemin.startswith("/static/") or appareils.reconnu(request.cookies.get(appareils.COOKIE))):
        return await call_next(request)
    if request.headers.get("HX-Request"):
        return Response(status_code=403, headers={"HX-Redirect": "/appareil"})
    if request.method != "GET":
        return Response("appareil non approuvé", status_code=403)
    suite = chemin + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/appareil?{urlencode({'suite': suite})}", status_code=303)
@app.exception_handler(KDriveIndisponible)
def kdrive_indisponible(request: Request, e: KDriveIndisponible):
    """kDrive en panne pendant une page qui en a besoin (ouvrir un scan, déposer) : explication claire."""
    texte = f"kDrive ne répond pas pour l'instant ({e}). Réessaie dans quelques minutes : rien n'est perdu."
    if request.headers.get("HX-Request") or request.query_params.get("paquet"):
        return JSONResponse({"erreur": texte}, status_code=503)
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                        f"<body style='font-family:system-ui;margin:24px'><p>{html.escape(texte)}</p>"
                        f"<p><a href='javascript:history.back()'>Retour</a> · <a href='/etat'>État du service</a></p>",
                        status_code=503)


def _instant(iso: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(iso).astimezone(pronote.PARIS) if iso else None
    except ValueError:
        return None


def _heure_fr(d: dt.datetime) -> str:
    """« 19 h 05 », ou « lun. 27/10 à 9 h 00 » si ce n'est pas aujourd'hui."""
    heure = f"{d:%H} h {d:%M}"
    return heure if d.date() == dt.datetime.now(pronote.PARIS).date() else \
        f"{notifications.JOURS[d.weekday()]} {d:%d/%m} à {heure}"


WORKER_MUET = dt.timedelta(minutes=3)  # le worker publie son état toutes les 30 s


def etat_service() -> dict:
    """État du worker, de Claude et de kDrive, publié par le worker (voir worker.publier)."""
    try:
        s = json.loads(db.etat_valeur("service") or "{}")
    except ValueError:
        s = {}
    vu = _instant(s.get("vu"))
    for nom in ("claude", "kdrive"):
        service = s.get(nom) or {"etat": "ok"}
        service["jusqu_a"] = _instant(service.get("jusqu_a"))
        service["depuis"] = _instant(service.get("depuis"))
        s[nom] = service
    s["vu"], s["demarre"] = vu, _instant(s.get("demarre"))
    s.setdefault("en_cours", [])
    s.setdefault("local", {})
    s["vivant"] = bool(vu) and dt.datetime.now(pronote.PARIS) - vu < WORKER_MUET
    s["connu"] = vu is not None
    return s


def bandeau_service() -> tuple[str, str, bool] | None:
    """Une phrase en haut de chaque page quand quelque chose bloque : (texte, détail, bouton Réessayer Claude)."""
    s = etat_service()
    if not s["connu"]:
        return None
    if not s["vivant"]:
        if maj.action_en_cours():   # il redémarre pendant la mise à jour : surtout pas de commande à lancer
            return ("Mise à jour en cours : le worker redémarre.",
                    "N'exécute aucune commande sur la VM pendant ce temps : la page se remettra à jour seule.", False)
        return (f"Le worker ne répond plus depuis {_heure_fr(s['vu'])} : scans et révisions attendent.",
                "Sur la VM, si aucune mise à jour n'est en cours : docker compose up -d", False)
    if s.get("arret"):
        return "Le worker redémarre : il termine le travail en cours.", "", False
    c, k = s["claude"], s["kdrive"]
    sans_claude = local.reglages()["mode"] != "jamais"
    if c["etat"] == "limite":
        quand = f" vers {_heure_fr(c['jusqu_a'])}" if c["jusqu_a"] else " dans un moment"
        return (f"Limite de ton forfait Claude atteinte : l'IA reprend{quand}.",
                "Les scans continuent d'être lus sur la VM, sans Claude (plus lentement)." if sans_claude
                else "Rien n'est perdu : la file attend et repart toute seule.", True)
    if c["etat"] == "connexion":
        return ("Claude refuse le jeton du forfait : l'IA est en pause.",
                "Sur la VM : claude setup-token, puis mets le jeton dans .env (CLAUDE_CODE_OAUTH_TOKEN).", False)
    if c["etat"] == "panne":
        return (f"Claude ne répond pas, nouvel essai à {_heure_fr(c['jusqu_a'])}." if c["jusqu_a"]
                else "Claude ne répond pas.", "Rien n'est perdu : la file attend.", True)
    if k["etat"] != "ok":
        return (f"kDrive ne répond pas, nouvel essai à {_heure_fr(k['jusqu_a'])}." if k["jusqu_a"]
                else "kDrive ne répond pas.", "Les analyses et les rangements reprendront tout seuls.", False)
    return None


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["bandeau_service"] = bandeau_service
# Empreinte du contenu d'une page : le rafraîchissement invisible ne touche à la page que si elle a changé
templates.env.filters["empreinte"] = lambda html_: hashlib.sha1(str(html_).encode()).hexdigest()[:16]
templates.env.globals["nb_a_verifier"] = lambda: db.nb_a_verifier()
templates.env.globals["nb_import_a_confirmer"] = lambda: db.nb_import_a_confirmer()
templates.env.globals["nb_evaluations_proches"] = lambda: db.nb_evaluations_proches()
templates.env.globals["nb_cartes_a_revoir"] = lambda: db.nb_cartes_a_revoir()
templates.env.globals["mdp_par_defaut"] = lambda: app.state.compte.par_defaut
templates.env.globals["nb_mises_a_jour"] = maj.disponibles
templates.env.filters["note"] = pronote.libelle_note
_matieres_cours = {"le": 0.0, "noms": []}


def matiere_lisible(nom_pronote: str | None) -> str:
    """« ADMINISTRATION DES SYSTEMES ET DES RESEAUX » → « Réseaux » (nom de tes cours), sinon en minuscules."""
    if not nom_pronote:
        return ""
    if time.monotonic() - _matieres_cours["le"] > 60:
        _matieres_cours.update(le=time.monotonic(), noms=db.matieres())
    trouve = pronote.trouver_matiere(nom_pronote, _matieres_cours["noms"])
    if trouve:
        return trouve
    return nom_pronote[:1] + nom_pronote[1:].lower() if nom_pronote.isupper() else nom_pronote


templates.env.filters["matiere"] = matiere_lisible


def quand(iso: str | None) -> str:
    """« 2026-10-26T11:25+01:00 » → « aujourd'hui à 11 h 25 » ou « lun. 26/10 à 11 h 25 »."""
    try:
        d = dt.datetime.fromisoformat(iso) if iso else None
    except ValueError:
        d = None
    if d is None:
        return iso or ""
    d = d.replace(tzinfo=pronote.PARIS) if d.tzinfo is None else d.astimezone(pronote.PARIS)  # sans fuseau : Paris
    heure = _heure_fr(d)
    return f"aujourd'hui à {heure}" if " à " not in heure else heure


templates.env.filters["quand"] = quand
templates.env.filters["paris"] = lambda d: d.astimezone(pronote.PARIS).strftime("%d/%m/%Y %H:%M") if d else ""
templates.env.filters["jour_iso"] = dt.date.fromisoformat
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
EXTENSIONS = importation.DOCUMENTS


def surligner(extrait: str | None) -> Markup:
    """Échappe l'extrait de recherche puis rétablit seulement les <b> de ts_headline."""
    # Sans la mise en forme Markdown du cours (« ## Titre », puces) : un extrait se lit d'une traite
    texte = re.sub(r"(^|\s)(#{1,6}|[-*+>]|\d+\.)\s", " ", re.sub(r"\s+", " ", extrait or "")).strip()
    texte = html.escape(texte)
    return Markup(texte.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>"))


templates.env.filters["surligner"] = surligner
# Extraits des passages : les mots trouvés sont entre ** (ts_headline), rendus en gras
templates.env.filters["surligner_md"] = lambda extrait: surligner(re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", extrait or ""))


def rendu_markdown(texte: str | None) -> str:
    # Formules mises à l'abri du Markdown, puis rendues par KaTeX dans le navigateur (voir formules.py)
    # Schémas (blocs ```dot / ```svg) dessinés ici en SVG, les mêmes que dans le PDF (voir schemas.py)
    texte, figures = schemas.proteger(texte or "")
    texte, maths = formules.proteger(texte)
    # Le HTML brut éventuel de la transcription est neutralisé avant le rendu
    texte = texte.replace("&", "&amp;").replace("<", "&lt;")
    rendu = formules.restaurer(markdown.markdown(texte, extensions=["tables", "fenced_code"]), maths)
    return schemas.restaurer(rendu, figures)


# Texte simple (questions de quiz, flashcards) avec des formules $…$
templates.env.filters["maths"] = formules.en_ligne
templates.env.filters["schema"] = schemas.figure


def avec_sources(rendu: str, reperes: dict, web: list[dict] | None = None) -> Markup:
    """Transforme les repères [D1] du texte en liens vers le cours d'origine, et [W1] vers la page web."""
    pages = {w["repere"]: w["url"] for w in web or []}

    def lien(m):
        if m.group(1).startswith("W"):
            url = pages.get(m.group(1))
            return (f'<a class="web" href="{html.escape(url)}" target="_blank" rel="noopener noreferrer">'
                    f'[{m.group(1)}]</a>') if url else m.group(0)
        doc_id = reperes.get(m.group(1))
        return f'<a href="/doc/{doc_id}">[{m.group(1)}]</a>' if doc_id else m.group(0)
    return Markup(re.sub(r"\[([DW]\d+)\]", lien, rendu))


# --- Connexion --------------------------------------------------------------

def _suite_sure(suite: str) -> str:
    # Uniquement un chemin du site : pas de redirection vers un autre domaine
    return suite if suite.startswith("/") and not suite.startswith("//") and "\\" not in suite else "/"


def _face_id(request: Request) -> bool:
    """Bouton Face ID sur la page de connexion : adresse compatible et au moins un appareil ajouté."""
    try:
        return not cles.possible(request.headers.get("host", ""), _https(request)) and bool(db.cles_acces())
    except Exception:
        return False


@app.get("/connexion", response_class=HTMLResponse)
def page_connexion(request: Request, suite: str = "/"):
    return templates.TemplateResponse(request, "connexion.html", {"suite": _suite_sure(suite), "sans_nav": True,
                                                                  "face_id": _face_id(request)})


@app.post("/connexion", response_class=HTMLResponse)
def connexion(request: Request, utilisateur: str = Form(...), mot_de_passe: str = Form(...), suite: str = Form("/")):
    compte = request.app.state.compte
    ip = appareils.ip_client(request)
    attente = compte.bloque(ip)
    if attente or not compte.verifier(ip, utilisateur.strip(), mot_de_passe):
        attente = attente or compte.bloque(ip)
        erreur = (f"Trop d'essais : réessaie dans {attente // 60 + 1} min." if attente
                  else "Identifiant ou mot de passe incorrect.")
        return templates.TemplateResponse(request, "connexion.html", {"suite": _suite_sure(suite), "erreur": erreur,
                                                                       "sans_nav": True, "face_id": _face_id(request)},
                                          status_code=401)
    r = RedirectResponse(_suite_sure(suite), status_code=303)
    _cookie_session(request, r, compte)
    return r


def _cookie_session(request: Request, reponse: Response, compte: auth.Compte):
    reponse.set_cookie(auth.COOKIE, compte.creer_session(), max_age=auth.DUREE, httponly=True, samesite="lax",
                       secure=_https(request))


def _https(request: Request) -> bool:
    return (request.url.scheme == "https" or request.headers.get("X-Forwarded-Proto") == "https"
            or appareils.par_tunnel(request.headers))   # Cloudflare sert toujours le site en HTTPS


def _origine(request: Request) -> str:
    return f"{'https' if _https(request) else request.url.scheme}://{request.headers.get('host', '')}"


def _contexte_compte(request: Request, **plus) -> dict:
    host = request.headers.get("host", "")
    return {"utilisateur": request.app.state.compte.utilisateur, "cles": db.cles_acces(),
            "cles_impossible": cles.possible(host, _https(request)), "nom_sslip": cles.nom_sslip(host),
            "hote": cles.hote(host), "appareils": appareils.liste(), "code_expire": appareils.code_en_cours(),
            "par_tunnel": appareils.par_tunnel(request.headers),
            "appareil_ici": appareils.reconnu(request.cookies.get(appareils.COOKIE)), **plus}


# --- Appareils approuvés pour l'accès par Internet (Cloudflare Tunnel) -------

@app.get("/appareil", response_class=HTMLResponse)
def page_appareil(request: Request, suite: str = "/"):
    return templates.TemplateResponse(request, "appareil.html", {"suite": _suite_sure(suite), "sans_nav": True})


@app.post("/appareil", response_class=HTMLResponse)
def approuver_appareil(request: Request, code: str = Form(...), nom: str = Form("Appareil"), suite: str = Form("/")):
    compte = request.app.state.compte
    ip = appareils.ip_client(request)
    attente = compte.bloque(ip)
    valeur = None if attente else appareils.utiliser_code(code, nom)
    if not valeur:
        if not attente:
            compte._echecs.setdefault(ip, []).append(time.time())
        attente = attente or compte.bloque(ip)
        erreur = (f"Trop d'essais : réessaie dans {attente // 60 + 1} min." if attente
                  else "Code inconnu ou expiré : crée-en un nouveau (Réglages → Compte).")
        return templates.TemplateResponse(request, "appareil.html", {"suite": _suite_sure(suite), "sans_nav": True,
                                                                     "erreur": erreur, "nom": nom}, status_code=403)
    r = RedirectResponse(_suite_sure(suite), status_code=303)
    r.set_cookie(appareils.COOKIE, valeur, max_age=appareils.DUREE, httponly=True, samesite="lax", secure=True)
    return r


@app.post("/appareils/code", response_class=HTMLResponse)
def creer_code_appareil(request: Request):
    return templates.TemplateResponse(request, "compte.html",
                                      _contexte_compte(request, code_appareil=appareils.nouveau_code()))


@app.post("/appareils/{ident}/retirer")
def retirer_appareil(ident: str):
    appareils.retirer(ident)
    return RedirectResponse("/compte#appareils", status_code=303)


@app.get("/compte", response_class=HTMLResponse)
def page_compte(request: Request):
    return templates.TemplateResponse(request, "compte.html", _contexte_compte(request))


# --- Face ID / Touch ID (clés d'accès, voir cles.py) ---------------------------

COOKIE_DEFI = "claunote_defi"


def _reponse_options(jeton: str, options: str) -> Response:
    r = Response(options, media_type="application/json")
    r.set_cookie(COOKIE_DEFI, jeton, max_age=cles.DUREE_DEFI, httponly=True, samesite="strict")
    return r


@app.post("/cles/inscription/options")
def cles_inscription_options(request: Request):
    host = request.headers.get("host", "")
    if cles.possible(host, _https(request)):
        raise HTTPException(400, "Face ID demande un nom de site en HTTPS (voir Réglages → Compte)")
    return _reponse_options(*cles.options_inscription(host, request.app.state.compte.utilisateur,
                                                      [k["id"] for k in db.cles_acces()]))


@app.post("/cles/inscription")
async def cles_inscription(request: Request):
    corps = cles.lire(await request.body())
    try:
        cle = cles.verifier_inscription(request.cookies.get(COOKIE_DEFI), corps.get("cle") or {},
                                        request.headers.get("host", ""), _origine(request))
    except cles.CleRefusee as e:
        return JSONResponse({"erreur": str(e)}, status_code=400)
    db.ajouter_cle(cle, (corps.get("nom") or "Appareil").strip()[:60])
    return {"ok": True}


@app.post("/cles/{cle_id}/supprimer")
def cles_supprimer(cle_id: str):
    db.supprimer_cle(cle_id)
    return RedirectResponse("/compte#cles", status_code=303)


@app.post("/cles/connexion/options")
def cles_connexion_options(request: Request):
    host = request.headers.get("host", "")
    if cles.possible(host, _https(request)):
        raise HTTPException(400, "Face ID indisponible à cette adresse")
    return _reponse_options(*cles.options_connexion(host))


@app.post("/cles/connexion")
async def cles_connexion(request: Request):
    compte = request.app.state.compte
    ip = appareils.ip_client(request)
    if compte.bloque(ip):
        return JSONResponse({"erreur": "Trop d'essais : réessaie dans quelques minutes."}, status_code=429)
    corps = cles.lire(await request.body())
    reponse = corps.get("cle") or {}
    cle = db.cle_acces(cles.identifiant(reponse))
    try:
        if cle is None:
            raise cles.CleRefusee("clé inconnue : ajoute cet appareil depuis Réglages → Compte")
        compteur = cles.verifier_connexion(request.cookies.get(COOKIE_DEFI), reponse, cle,
                                           request.headers.get("host", ""), _origine(request))
    except cles.CleRefusee as e:
        compte._echecs.setdefault(ip, []).append(time.time())
        return JSONResponse({"erreur": str(e)}, status_code=400)
    db.cle_utilisee(cle["id"], compteur)
    r = JSONResponse({"suite": _suite_sure(str(corps.get("suite") or "/"))})
    _cookie_session(request, r, compte)
    r.delete_cookie(COOKIE_DEFI)
    return r


@app.post("/compte", response_class=HTMLResponse)
def changer_mot_de_passe(request: Request, actuel: str = Form(...), nouveau: str = Form(...),
                         confirmation: str = Form(...)):
    compte = request.app.state.compte
    ip = appareils.ip_client(request)
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
        return templates.TemplateResponse(request, "compte.html", _contexte_compte(request, erreur=erreur),
                                          status_code=400)
    hache = auth.hacher(nouveau)
    db.etat_ecrire("mot_de_passe", hache)
    # Nouveau compte = nouvelles signatures : les autres appareils sont déconnectés, pas celui-ci
    request.app.state.compte = auth.Compte(compte.utilisateur, hache, compte.secret)
    r = templates.TemplateResponse(request, "compte.html", _contexte_compte(
        request, message="Mot de passe changé. Les autres appareils devront se reconnecter."))
    _cookie_session(request, r, request.app.state.compte)
    return r


def _contexte_maj(request: Request, **plus) -> dict:
    return {"present": maj.present(), "e": maj.etat(), "journal": maj.journal(),
            "heure": lambda d: _heure_fr(d.astimezone(pronote.PARIS)), **plus}


@app.get("/mise-a-jour", response_class=HTMLResponse)
def page_mise_a_jour(request: Request):
    """Version installée, nouveautés sur GitHub, et bouton pour tout mettre à jour (service maj)."""
    return templates.TemplateResponse(request, "mise_a_jour.html", _contexte_maj(request))


@app.get("/mise-a-jour/etat", response_class=HTMLResponse)
def etat_mise_a_jour(request: Request):
    """Bloc rafraîchi pendant une action (le site lui-même redémarre au milieu d'une mise à jour)."""
    return templates.TemplateResponse(request, "_mise_a_jour.html", _contexte_maj(request))


@app.post("/mise-a-jour/{action}", response_class=HTMLResponse)
def demander_mise_a_jour(request: Request, action: str):
    """Réservé à l'admin, c'est-à-dire au compte connecté (le seul du site)."""
    if action not in maj.ACTIONS:
        raise HTTPException(404)
    erreur = None
    if not maj.present():
        erreur = "Le service maj ne tourne pas sur la VM (voir l'installation ci-dessous)."
    else:
        try:
            maj.demander(action)
        except RuntimeError as e:
            erreur = str(e)
    return templates.TemplateResponse(request, "_mise_a_jour.html", _contexte_maj(request, erreur=erreur))


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
    aujourdhui = dt.datetime.now(pronote.PARIS).date()
    campagne = db.campagne_du_jour(aujourdhui)
    jour = next((j for j in revision.jours_programme(campagne["programme"], campagne["faits"])
                 if j["date"] >= aujourdhui), None) if campagne else None
    return templates.TemplateResponse(request, "accueil.html", {
        "aujourdhui": aujourdhui, "paris": pronote.PARIS, "campagne": campagne, "jour": jour,
        "courts": revision.COURTS,
        "evaluations": evaluations[:6], "devoirs": [d for d in db.evenements_a_venir("devoir", 7) if not d["fait"]],
        "documents": db.documents_pronote(5), "revisions": db.revisions(4),
        "synchro": db.etat_valeur("pronote_synchro"), "nb_documents": db.nb_documents()})


@app.get("/bibliotheque", response_class=HTMLResponse)
def bibliotheque(request: Request):
    arbre = defaultdict(lambda: defaultdict(list))
    for r in db.bibliotheque():
        arbre[r["niveau"]][r["matiere"]].append(r)
    niveaux = [n for n in reversed(NIVEAUX) if n in arbre]  # le plus récent (BTS2) en haut, le collège en bas
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
    # Aperçu : le fichier lui-même s'il s'affiche dans le navigateur, sinon son autre format (PDF d'un
    # PowerPoint), sinon rien (un .pptx dans une iframe fait planter Safari sur iPhone).
    apercu, variante = genre_apercu(doc["nom"]), 0
    if apercu is None and doc.get("variante_nom") and genre_apercu(doc["variante_nom"]):
        apercu, variante = genre_apercu(doc["variante_nom"]), 1
    return templates.TemplateResponse(request, "document.html", {
        "doc": doc, "transcription": rendu_markdown(doc["transcription"]), "apercu": apercu, "variante": variante,
        "format": PurePosixPath(doc["nom"]).suffix.lstrip(".").upper(),
        "niveaux": NIVEAUX, "types": TYPES, "matieres": db.matieres()})


def _page_carte_mentale(request: Request, texte: str, titre: str, retour: str) -> HTMLResponse:
    racine, branches = carte_mentale.arbre(texte, titre)
    return templates.TemplateResponse(request, "carte_mentale.html", {
        "racine": racine, "branches": branches, "titre": titre, "retour": retour})


@app.get("/doc/{doc_id}/carte-mentale", response_class=HTMLResponse)
def carte_mentale_document(request: Request, doc_id: int):
    doc = db.document(doc_id)
    if doc is None:
        raise HTTPException(404)
    return _page_carte_mentale(request, doc["transcription"] or "", doc["titre"] or doc["nom"], f"/doc/{doc_id}")


@app.get("/doc/{doc_id}/fichier")
def fichier(doc_id: int, variante: int = 0):
    """Sert le scan original (ou son autre format), récupéré à la volée depuis kDrive."""
    doc = db.document(doc_id)
    if doc is None or (variante and not doc.get("variante_kdrive_id")):
        raise HTTPException(404)
    if variante:
        return _servir(doc["variante_kdrive_id"], doc["variante_nom"])
    return _servir(doc["kdrive_id"], doc["nom"])


APERCUS = {".pdf": "pdf", ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image", ".gif": "image",
           ".txt": "texte", ".md": "texte"}


def genre_apercu(nom: str | None) -> str | None:
    """pdf, image ou texte si le navigateur sait l'afficher ; None pour Word, PowerPoint, Excel, HEIC…"""
    return APERCUS.get(PurePosixPath(nom or "").suffix.lower())


def _servir(kdrive_id: int, nom: str) -> FileResponse:
    """Affiché dans le navigateur s'il sait le lire ; sinon téléchargé (jamais ouvert de force dans la page)."""
    tmp = Path(tempfile.mkdtemp())
    chemin = KDrive.from_env().download(kdrive_id, tmp / PurePosixPath(nom).name)
    media = mimetypes.guess_type(nom)[0] or "application/octet-stream"
    if genre_apercu(nom) == "texte":
        media = "text/plain; charset=utf-8"
    return FileResponse(chemin, media_type=media,
                        content_disposition_type="inline" if genre_apercu(nom) else "attachment",
                        filename=PurePosixPath(nom).name, background=BackgroundTask(lambda: _nettoyer(tmp)))


def _nettoyer(dossier: Path):
    for f in dossier.iterdir():
        f.unlink()
    dossier.rmdir()


GROS_FICHIER = 32 * 1024 * 1024


@app.get("/deposer", response_class=HTMLResponse)
def page_deposer(request: Request):
    return templates.TemplateResponse(request, "deposer.html", {"niveaux": NIVEAUX, **_filtres_depot()})


@app.post("/deposer", response_class=HTMLResponse)
def deposer(request: Request, fichiers: list[UploadFile] = File(...), niveau: str = Form(""), paquet: int = 0):
    """Envoie les fichiers dans l'inbox kDrive, en gardant les dossiers d'un dossier déposé.

    Un niveau choisi devient un sous-dossier de l'inbox : le worker s'en sert comme année d'étude.
    Un gros dossier arrive par paquets (`paquet=1`, réponse JSON) ; le worker attend la fin
    de l'envoi avant de trier le lot.
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
        if (PurePosixPath(parts[-1]).suffix.lower() not in EXTENSIONS
                or any(p.lower() in importation.DOSSIERS_INUTILES for p in parts[:-1])
                or parts[-1].lower() in importation.SYSTEME_NOMS or parts[-1].startswith(("~$", "._"))):
            ignores.append("/".join(parts))
            continue
        dossiers = tuple(chemin[:-1])
        if dossiers not in dossiers_kdrive:
            dossiers_kdrive[dossiers] = kd.ensure_path(INBOX_ID, list(dossiers))
        f.file.seek(0, 2)
        taille = f.file.tell()
        f.file.seek(0)
        if taille > GROS_FICHIER:  # scan d'une année entière : envoyé par morceaux, pas en mémoire
            kd.upload_flux(dossiers_kdrive[dossiers], chemin[-1], f.file, taille)
        else:
            kd.upload(dossiers_kdrive[dossiers], chemin[-1], f.file.read(), conflict="rename")
        envoyes.append("/".join(chemin))
    if envoyes:
        db.demander_scan()
    if paquet:
        return JSONResponse({"envoyes": envoyes, "ignores": ignores})
    return templates.TemplateResponse(request, "deposer.html", {
        "niveaux": NIVEAUX, "envoyes": envoyes, "ignores": ignores, **_filtres_depot()})


def _filtres_depot() -> dict:
    """Listes partagées avec la page Déposer, qui écarte le code et les fichiers système avant l'envoi."""
    return {"extensions": sorted(EXTENSIONS), "dossiers_inutiles": sorted(importation.DOSSIERS_INUTILES)}


@app.get("/recherche", response_class=HTMLResponse)
def recherche(request: Request, q: str = ""):
    """Résultats exacts (tous les mots), puis « aussi en rapport » : passages proches par les mots ou le sens."""
    resultats = db.recherche(q) if q.strip() else []
    proches = []
    if q.strip():
        try:
            vecteur = index.vecteur_question(q) if not ressources.sature() else None
            vus = {r["id"] for r in resultats}
            for p in question.reordonner(q, db.chercher_passages(question.requete(q), vecteur, limite=20), 20):
                if p["document_id"] not in vus:
                    vus.add(p["document_id"])
                    proches.append({**p, "id": p["document_id"]})
        except Exception as e:  # index pas encore créé : la recherche exacte suffit
            print(f"recherche par passages impossible : {e}", flush=True)
    return templates.TemplateResponse(request, "recherche.html", {"q": q, "resultats": resultats,
                                                                  "proches": proches[:8]})


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


@app.post("/doc/{doc_id}/classer")
def classer(doc_id: int, niveau: str = Form(...), matiere: str = Form(...), type: str = Form(...),
            retour: str = Form("/a-verifier")):
    """Classement manuel. Le worker déplace ensuite le scan et sa transcription dans le bon dossier kDrive
    (comme pour Organiser) : la page répond tout de suite, même si kDrive est lent ou en panne."""
    doc = db.document(doc_id)
    if doc is None or niveau not in NIVEAUX or type not in TYPES or not matiere.strip():
        raise HTTPException(400, "classement invalide")
    db.maj(doc_id, statut="classe", niveau=niveau, matiere=matiere.strip(), type=type, confiance=1.0, note=None,
           erreur=None, relire=False, a_ranger=True)
    db.demander_scan()  # réveille le worker : il déplace les fichiers dans kDrive
    return RedirectResponse(_suite_sure(retour), status_code=303)


@app.post("/doc/{doc_id}/hors-cours")
def mettre_hors_cours(doc_id: int, retour: str = Form("/hors-cours")):
    """Document administratif ou sans intérêt pour réviser : rangé à part, hors de la bibliothèque."""
    doc = db.document(doc_id)
    if doc is None or doc["statut"] == "nouveau":
        raise HTTPException(400, "document en cours d'analyse")
    db.maj(doc_id, statut="hors_cours", note=None, erreur=None, a_ranger=True)
    db.demander_scan()  # le worker le range dans Etudes/hors-cours
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
           "cours": "Remettre dans les cours", "relire": "Relire avec Claude (relit le scan, garde l'année)",
           "supprimer": "Supprimer (corbeille kDrive)"}


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
        db.demander_scan()  # le worker envoie les fichiers à la corbeille kDrive
        message = f"{n} document{'s' if n > 1 else ''} supprimé{'s' if n > 1 else ''} (fichiers dans la corbeille kDrive)."
        return RedirectResponse(f"/organiser?{requete}&{urlencode({'message': message})}", status_code=303)
    if action in ("niveau", "matiere", "type"):
        n = db.maj_masse(ids, **{action: valeur})
        db.completer_classement(ids)
    elif action == "ia":
        n = db.maj_masse(ids, statut="reclasser")
    elif action == "relire":
        n = db.marquer_relire(ids)
    else:
        n = db.maj_masse(ids, statut="hors_cours" if action == "hors_cours" else "a_verifier")
        db.completer_classement(ids)
    if n:
        db.demander_scan()  # réveille le worker : il déplace les fichiers dans kDrive
    message = f"{n} document{'s' if n > 1 else ''} modifié{'s' if n > 1 else ''}."
    if action == "ia" and n:
        message += " L'IA les range dans les minutes qui viennent (recharge la page)."
    elif action == "relire" and n:
        message += " Claude les relit quand la file d'analyse est vide (page Analyses)."
    elif n:
        message += " Les fichiers sont déplacés dans kDrive en arrière-plan."
    if len(ids) > n:
        message += f" {len(ids) - n} ignoré{'s' if len(ids) - n > 1 else ''} (en analyse, ou en erreur pour l'IA)."
    return RedirectResponse(f"/organiser?{requete}&{urlencode({'message': message})}", status_code=303)


def _supprimer(doc_id: int) -> bool:
    """Oubli en base tout de suite ; le worker envoie scan et transcription à la corbeille kDrive
    (récupérables 30 jours), et réessaie si kDrive est en panne : aucun fichier n'est oublié."""
    doc = db.document(doc_id)
    if doc is None:
        return False
    db.mettre_a_la_corbeille([f for f in (doc["kdrive_id"], doc["kdrive_md_id"], doc.get("variante_kdrive_id")) if f])
    db.supprimer(doc_id)
    return True


@app.post("/doc/{doc_id}/supprimer")
def supprimer_document(doc_id: int, retour: str = Form("/a-verifier")):
    if not _supprimer(doc_id):
        raise HTTPException(404)
    db.demander_scan()
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


# --- Import de dossiers entiers : ce que le tri attend de toi -----------------------------

@app.get("/import", response_class=HTMLResponse)
def page_import(request: Request, message: str = ""):
    lots = []
    for b in db.import_bilan():
        compris = b["compris"] or {}
        arbre = []
        for dossier, n in db.import_dossiers(b["lot"]).items():
            i = importation.indices(dossier.split("/") if dossier else [])
            i.update({k: v for k, v in (compris.get(dossier) or {}).items() if v})
            arbre.append({"chemin": dossier, "nom": dossier.rsplit("/", 1)[-1] or "(racine)",
                          "profondeur": dossier.count("/"), "n": n, **i})
        lots.append({**b, "arbre": arbre[:300]})
    par_lot = defaultdict(lambda: defaultdict(list))
    for f in db.import_fichiers("inutile"):
        par_lot[f["lot"]][f["categorie"]].append(f)
    ordre = list(importation.LIBELLES)  # code d'abord, ce qui est le plus sûrement inutile
    inutiles = {lot: {"n": sum(map(len, cats.values())),
                      "categories": sorted(cats.items(), key=lambda c: ordre.index(c[0]) if c[0] in ordre else 99)}
                for lot, cats in par_lot.items()}
    groupes = [{"cle": g, "pages": db.import_groupe(g)} for g in db.import_groupes(sur=False)]
    return templates.TemplateResponse(request, "import.html", {
        "lots": lots, "inutiles": inutiles, "groupes": [g for g in groupes if g["pages"]],
        "libelles": importation.LIBELLES, "message": message,
        "doutes": db.sensibles("doute"), "corbeille": db.sensibles(), "categories": sensible.LIBELLES,
        "decoupages": db.decoupages(), "seuil_pages": decoupage.SEUIL_PAGES})


@app.post("/import/personnels")
def choix_personnels(ids: list[int] = Form([]), action: str = Form(...)):
    """Documents peut-être personnels : à la corbeille, ou analysés (ce sont des cours)."""
    decision = {"supprimer": "supprime", "analyser": "analyser"}.get(action)
    if decision is None:
        raise HTTPException(400, "action inconnue")
    lignes = db.sensible_choisir(ids, decision)
    # Le worker applique : « supprimer » met à la corbeille kDrive, « analyser » saute le garde-fou
    db.import_marquer([s["kdrive_id"] for s in lignes], "supprimer" if decision == "supprime" else "analyser")
    if lignes:
        db.demander_scan()
    n, pluriel = len(lignes), "s" if len(lignes) > 1 else ""
    message = (f"{n} document{pluriel} part{'ent' if n > 1 else ''} à la corbeille kDrive (récupérable{pluriel} 30 jours)."
               if decision == "supprime" else f"{n} document{pluriel} ajouté{pluriel} à l'analyse.")
    return RedirectResponse(f"/import?{urlencode({'message': message})}", status_code=303)


@app.post("/import/choix")
def choix_import(ids: list[int] = Form([]), action: str = Form(...)):
    """Fichiers inutiles : supprimer (corbeille kDrive), garder à part, ou analyser quand même."""
    decision = {"supprimer": "supprimer", "garder": "garder", "analyser": "analyser"}.get(action)
    if decision is None:
        raise HTTPException(400, "action inconnue")
    fichiers = db.import_fichiers("inutile", ids)
    if decision == "analyser":  # seuls les formats lisibles par l'agent
        fichiers = [f for f in fichiers if not f["dossier"] and PurePosixPath(f["nom"]).suffix.lower() in EXTENSIONS]
    n = db.import_marquer([f["kdrive_id"] for f in fichiers], decision)
    if n:
        db.demander_scan()
    pluriel = "s" if n > 1 else ""
    message = {"supprimer": f"{n} fichier{pluriel} part{'ent' if n > 1 else ''} à la corbeille kDrive (récupérable{pluriel} 30 jours).",
               "garder": f"{n} fichier{pluriel} rangé{pluriel} à part dans Etudes/autres.",
               "analyser": f"{n} fichier{pluriel} ajouté{pluriel} à l'analyse."}[decision]
    if decision == "analyser" and n < len(ids):
        message += f" {len(ids) - n} ignoré{'s' if len(ids) - n > 1 else ''} : format que l'agent ne sait pas lire."
    return RedirectResponse(f"/import?{urlencode({'message': message})}", status_code=303)


@app.post("/import/decouper")
def decouper_import(groupe: str = Form(...), coupes: list[int] = Form([]), action: str = Form("decouper")):
    """Scans au regroupement incertain : un document commence à chaque page cochée."""
    pages = db.import_groupe(groupe)
    if not pages:
        return RedirectResponse("/import", status_code=303)
    ordres = [p["ordre"] for p in pages]
    morceaux = [[o] for o in ordres] if action == "separer" else importation.decouper(ordres, set(coupes))
    par_ordre = {p["ordre"]: p["kdrive_id"] for p in pages}
    for k, morceau in enumerate(morceaux):
        ids = [par_ordre[o] for o in morceau]
        if len(ids) == 1:
            db.import_marquer(ids, "analyser", groupe=None)
        else:
            for ordre, i in enumerate(ids):
                db.import_marquer([i], "page", groupe=f"{groupe}#{k + 1}", ordre=ordre, sur=True)
    db.demander_scan()
    docs = len(morceaux)
    return RedirectResponse(f"/import?{urlencode({'message': f'{len(pages)} pages → {docs} document' + ('s' if docs > 1 else '') + '.'})}",
                            status_code=303)


@app.get("/import/fichier/{kdrive_id}")
def fichier_import(kdrive_id: int):
    """Aperçu d'un fichier en attente de tri (uniquement ceux du tri, pas tout kDrive)."""
    f = db.import_connus([kdrive_id])
    if kdrive_id not in f:
        raise HTTPException(404)
    lignes = db.import_fichiers(f[kdrive_id], [kdrive_id])
    if not lignes:
        raise HTTPException(404)
    nom = lignes[0]["nom"]
    if PurePosixPath(nom).suffix.lower() not in importation.IMAGES:
        return _servir(kdrive_id, nom)
    # Miniature légère : une page de scan pèse souvent plusieurs Mo
    with tempfile.TemporaryDirectory() as tmp:
        chemin = KDrive.from_env().download(kdrive_id, Path(tmp) / PurePosixPath(nom).name)
        return Response(importation.miniature(chemin), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


HISTORIQUE_PAR_PAGE = 20


MAX_PIECES = 8                      # fichiers joints à une révision ou une campagne
MAX_PIECE = 20 * 1024 * 1024        # par fichier
MAX_PIECES_TOTAL = 40 * 1024 * 1024
templates.env.globals.update(max_pieces=MAX_PIECES, max_mo=MAX_PIECE // 1024 // 1024)


async def _pieces(f) -> list[tuple[str, bytes]]:
    """Fichiers joints au formulaire (emploi du temps, dates d'épreuves…), vérifiés : du contexte, pas des cours."""
    fichiers = [p for p in f.getlist("pieces") if getattr(p, "filename", "")]
    if len(fichiers) > MAX_PIECES:
        raise HTTPException(400, f"{MAX_PIECES} fichiers joints au maximum")
    pieces, total = [], 0
    for p in fichiers:
        nom = PurePosixPath(p.filename.replace("\\", "/")).name[:120]
        if PurePosixPath(nom).suffix.lower() not in revision.EXTENSIONS_PJ:
            raise HTTPException(400, f"« {nom} » : joins une image, un PDF, un Word, un Excel ou un texte")
        contenu = await p.read()
        if len(contenu) > MAX_PIECE:
            raise HTTPException(400, f"« {nom} » dépasse {MAX_PIECE // 1024 // 1024} Mo")
        total += len(contenu)
        if total > MAX_PIECES_TOTAL:
            raise HTTPException(400, f"{MAX_PIECES_TOTAL // 1024 // 1024} Mo de fichiers joints au maximum")
        if contenu:
            pieces.append((nom, contenu))
    return pieces


@app.get("/piece/{piece_id}")
def voir_piece(piece_id: int):
    piece = db.piece_jointe(piece_id)
    if piece is None:
        raise HTTPException(404)
    nom = piece["nom"]
    media = mimetypes.guess_type(nom)[0] or "application/octet-stream"
    lisible = media == "application/pdf" or (media.startswith("image/") and not nom.lower().endswith((".heic", ".heif")))
    return Response(bytes(piece["contenu"]), media_type=media, headers={
        "Content-Disposition": f"{'inline' if lisible else 'attachment'}; filename*=UTF-8''{quote(nom)}",
        "Cache-Control": "private, max-age=3600"})


@app.get("/reviser", response_class=HTMLResponse)
def reviser(request: Request, sujet: str = "", matiere: str = "", date_eval: str = "", genre: str = "",
            filtre: str = "", m: str = "", q: str = "", page: int = 1):
    """Accueil des révisions : à faire maintenant, campagnes, historique filtrable."""
    if sujet or matiere or date_eval or genre:  # ancien lien prérempli (agenda, notifications)
        params = {k: v for k, v in {"sujet": sujet, "matiere": matiere, "date_eval": date_eval,
                                     "genre": genre}.items() if v}
        return RedirectResponse(f"/reviser/nouvelle?{urlencode(params)}", status_code=303)
    page = max(page, 1)
    lignes, total = db.historique(filtre if filtre in revision.GENRES else "", m, q, HISTORIQUE_PAR_PAGE,
                                  (page - 1) * HISTORIQUE_PAR_PAGE)
    aujourdhui = dt.datetime.now(pronote.PARIS).date()
    contexte = {"genres": revision.GENRES, "courts": revision.COURTS, "lignes": lignes, "total": total,
                "page": page, "pages": max(1, -(-total // HISTORIQUE_PAR_PAGE)), "filtre": filtre, "m": m, "q": q,
                "matieres": db.matieres_revisions(), "aujourdhui": aujourdhui}
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "historique":
        return templates.TemplateResponse(request, "_historique.html", contexte)
    campagnes = db.campagnes(6)
    du_jour = db.campagne_du_jour(aujourdhui)
    jour = None
    if du_jour:
        jour = next((j for j in revision.jours_programme(du_jour["programme"], du_jour["faits"],
                                                          db.revisions_campagne(du_jour["id"]))
                     if j["date"] >= aujourdhui), None)
    # Les contrôles des 14 prochains jours : ce qu'il est le plus utile de réviser maintenant
    noms = sorted({r["matiere"] for r in db.niveaux_matieres()})
    evaluations = [{**e, "cours": pronote.trouver_matiere(e["matiere"], noms)}
                   for e in db.evenements_a_venir("evaluation", 14)][:4]
    return templates.TemplateResponse(request, "reviser.html", {
        **contexte, "campagnes": campagnes, "du_jour": du_jour, "jour": jour, "compte": db.compte_revisions(),
        "evaluations": evaluations, "paris": pronote.PARIS,
        "a_choisir": bool(db.niveaux_matieres())})


@app.get("/reviser/nouvelle", response_class=HTMLResponse)
def nouvelle_revision(request: Request, sujet: str = "", matiere: str = "", date_eval: str = "", genre: str = "",
                      doc: int | None = None):
    """Formulaire d'une révision ; les paramètres le préremplissent (depuis l'agenda Pronote ou un cours)."""
    document = db.document(doc) if doc else None
    return templates.TemplateResponse(request, "reviser_nouvelle.html", {
        "choix": db.niveaux_matieres_comptes(), "genres": revision.GENRES, "document": document,
        "demain": dt.date.today() + dt.timedelta(days=1),
        "pre": {"sujet": sujet, "matiere": matiere, "date_eval": date_eval, "genre": genre or "fiche"}})


@app.get("/reviser/documents", response_class=HTMLResponse)
def choisir_documents(request: Request, niveau_matiere: str = "", nom: str = "doc_ids"):
    """Liste à cocher des cours d'une matière (chargée à la demande)."""
    niveau, _, matiere = niveau_matiere.partition("|")
    return templates.TemplateResponse(request, "_documents.html", {
        "docs": db.documents_matiere(niveau, matiere) if matiere else [], "nom": nom})


@app.post("/reviser")
async def demander_revision(request: Request):
    """Crée une demande de révision ; le worker la traite en priorité."""
    f = await request.form()
    genre, sujet, consigne = f.get("genre", ""), (f.get("sujet") or "").strip(), (f.get("consigne") or "").strip()
    date_eval, doc_id = f.get("date_eval") or "", f.get("doc_id")
    if genre not in revision.GENRES:
        raise HTTPException(400, "genre inconnu")
    eval_le = dt.date.fromisoformat(date_eval) if date_eval else None
    if genre == "plan" and (eval_le is None or eval_le <= dt.date.today()):
        raise HTTPException(400, "un plan de révision demande une date d'évaluation future")
    choisis = [int(i) for i in f.getlist("doc_ids") if str(i).isdigit()]
    if doc_id:
        doc = db.document(int(doc_id))
        if doc is None:
            raise HTTPException(404)
        ids, sujet = [int(doc_id)], sujet or doc["titre"] or doc["nom"]
    else:
        niveau, _, matiere = (f.get("niveau_matiere") or "").partition("|")
        if not matiere:
            raise HTTPException(400, "choisis un niveau et une matière")
        # Les cours cochés, sinon le sujet sert de recherche ; s'il ne trouve rien, toute la matière
        ids = choisis or db.choisir_documents(niveau, matiere, sujet) or db.choisir_documents(niveau, matiere)
        sujet = sujet or f"{matiere} ({niveau})"
    if not ids:
        raise HTTPException(400, "aucun document classé pour ce choix")
    pieces = await _pieces(f)
    rev_id = db.creer_revision(genre, sujet, ids, eval_le, consigne=consigne)
    if pieces:
        db.ajouter_pieces(pieces, revision_id=rev_id)
    db.demander_scan()
    return RedirectResponse(f"/revision/{rev_id}", status_code=303)


def _sources(reperes: dict) -> dict[int, str]:
    return {d["id"]: d["titre"] or d["nom"] for d in db.documents(sorted(set((reperes or {}).values())))}


@app.get("/revision/{rev_id}", response_class=HTMLResponse)
def voir_revision(request: Request, rev_id: int):
    rev = db.revision(rev_id)
    if rev is None:
        raise HTTPException(404)
    reperes = rev["reperes"] or {}
    web = rev.get("sources_web") or []
    contenu, corrige, questions, cartes = None, None, [], []
    if rev["statut"] == "pret":
        if rev["genre"] == "quiz":
            questions = json.loads(rev["contenu"])
        elif rev["genre"] == "cartes":
            cartes = db.cartes(rev_id)
        elif rev["genre"] == "examen":
            sujet, corr = revision.separer_corrige(rev["contenu"])
            contenu = avec_sources(rendu_markdown(sujet), reperes, web)
            corrige = avec_sources(rendu_markdown(corr), reperes, web) if corr else None
        else:
            contenu = avec_sources(rendu_markdown(rev["contenu"]), reperes, web)
    sources = db.documents(list(reperes.values()))
    par_id = {d["id"]: d for d in sources}
    return templates.TemplateResponse(request, "revision.html", {
        "rev": rev, "genres": revision.GENRES, "contenu": contenu, "corrige": corrige, "questions": questions,
        "cartes": cartes, "reperes": reperes, "sources": sources,
        "cours": [(r, par_id[i]) for r, i in reperes.items() if i in par_id],
        "pieces": db.pieces_jointes(rev_id, rev.get("campagne_id")),
        "web": {w["repere"]: w for w in web}})


def _pdf(contenu: bytes, nom: str, telecharger: bool) -> Response:
    mode = "attachment" if telecharger else "inline"
    return Response(contenu, media_type="application/pdf", headers={
        "Content-Disposition": f"{mode}; filename*=UTF-8''{quote(nom)}", "Cache-Control": "private, max-age=60"})


@app.get("/revision/{rev_id}/carte-mentale", response_class=HTMLResponse)
def carte_mentale_revision(request: Request, rev_id: int):
    rev = db.revision(rev_id)
    if rev is None or rev["statut"] != "pret" or rev["genre"] not in revision.GENRES_TEXTE:
        raise HTTPException(404)
    texte = revision.separer_corrige(rev["contenu"])[0] if rev["genre"] == "examen" else rev["contenu"]
    texte = revision.separer_web(texte)[0]
    return _page_carte_mentale(request, texte, rev["sujet"], f"/revision/{rev_id}")


@app.get("/revision/{rev_id}/pdf")
def revision_pdf(rev_id: int, corrige: int = 1, telecharger: int = 0):
    rev = db.revision(rev_id)
    if rev is None or rev["statut"] != "pret":
        raise HTTPException(404, "révision pas encore prête")
    cartes = db.cartes(rev_id) if rev["genre"] == "cartes" else None
    contenu = pdf.revision_pdf(rev, _sources(rev["reperes"]), cartes, corrige=bool(corrige))
    suffixe = "" if corrige or rev["genre"] not in ("quiz", "examen") else " (sujet)"
    return _pdf(contenu, pdf.nom(f"{revision.COURTS[rev['genre']]} {rev['sujet']}{suffixe}"), bool(telecharger))


@app.post("/revision/{rev_id}/fait")
async def revision_faite(request: Request, rev_id: int):
    """Marque une révision comme faite (et garde le score d'un quiz)."""
    if db.revision(rev_id) is None:
        raise HTTPException(404)
    f = await request.form()
    fait = f.get("fait", "1") == "1"
    score = None
    if f.get("bonnes") is not None and f.get("total"):
        try:
            score = {"bonnes": int(f["bonnes"]), "total": int(f["total"])}
        except ValueError:
            raise HTTPException(400, "score invalide")
    db.marquer_fait(rev_id, fait, score)
    if request.headers.get("HX-Request"):
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return RedirectResponse(f"/revision/{rev_id}", status_code=303)


@app.post("/revision/{rev_id}/relancer")
def relancer_revision(rev_id: int):
    if db.revision(rev_id) is None:
        raise HTTPException(404)
    db.relancer_revision(rev_id)
    db.demander_scan()
    return RedirectResponse(f"/revision/{rev_id}", status_code=303)


@app.post("/revision/{rev_id}/supprimer")
def supprimer_revision(rev_id: int):
    rev = db.revision(rev_id)
    if rev is None:
        raise HTTPException(404)
    db.supprimer_revision(rev_id)
    suite = f"/campagne/{rev['campagne_id']}" if rev["campagne_id"] else "/reviser"
    return RedirectResponse(suite, status_code=303)


# --- Campagnes ----------------------------------------------------------------

def _rang_niveau(niveau: str) -> int:
    return NIVEAUX.index(niveau) if niveau in NIVEAUX else -1


def _epreuves(f, groupes: dict[str, dict]) -> list[dict]:
    """Tableau « date et heure de chaque épreuve » : lignes remplies et matières choisies seulement."""
    epreuves = []
    for nom, jour, heure in zip(f.getlist("epreuve_matiere"), f.getlist("epreuve_date"), f.getlist("epreuve_heure")):
        cle = matieres.concept(nom)
        try:
            jour = dt.date.fromisoformat(jour).isoformat()
        except ValueError:
            continue
        if cle not in groupes or any(e["cle"] == cle for e in epreuves):
            continue
        heure = heure if re.fullmatch(r"\d{2}:\d{2}", heure or "") else ""
        epreuves.append({"cle": cle, "matiere": groupes[cle]["matiere"], "date": jour, "heure": heure})
    return sorted(epreuves, key=lambda e: (e["date"], e["heure"]))


@app.get("/campagne/nouvelle", response_class=HTMLResponse)
def nouvelle_campagne(request: Request):
    choix = db.niveaux_matieres_comptes()
    par_niveau = defaultdict(list)
    for c in choix:
        par_niveau[c["niveau"]].append({**c, "concept": matieres.concept(c["matiere"])})
    niveaux = [n for n in reversed(NIVEAUX) if n in par_niveau]  # l'année en cours d'abord
    aujourdhui = dt.datetime.now(pronote.PARIS).date()
    lundi = aujourdhui + dt.timedelta(days=7 - aujourdhui.weekday())
    return templates.TemplateResponse(request, "campagne_nouvelle.html", {
        "par_niveau": par_niveau, "niveaux": niveaux, "genres": revision.GENRES,
        "debut": aujourdhui, "fin": lundi + dt.timedelta(days=4)})


@app.post("/campagne")
async def creer_campagne(request: Request):
    """Une révision par matière et par support coché, plus un programme jour par jour."""
    f = await request.form()
    titre = (f.get("titre") or "").strip() or "Campagne de révision"
    consigne = (f.get("consigne") or "").strip()
    try:
        debut, fin = dt.date.fromisoformat(f.get("debut") or ""), dt.date.fromisoformat(f.get("fin") or "")
    except ValueError:
        raise HTTPException(400, "dates invalides")
    if fin < debut or (fin - debut).days > 60:
        raise HTTPException(400, "la période doit aller de 1 à 60 jours")
    genres = [g for g in f.getlist("genres") if g in revision.GENRES and g != "plan"]
    choisies = [m for m in f.getlist("matieres") if "|" in m]
    if not genres or not choisies:
        raise HTTPException(400, "choisis au moins une matière et un type de support")
    # Même matière sur plusieurs années (ex. Maths en BTS1 et BTS2) : un seul jeu de supports
    # qui couvre les cours de toutes ces années, l'année la plus récente d'abord.
    groupes: dict[str, dict] = {}
    for nm in sorted(choisies, key=lambda nm: -_rang_niveau(nm.partition("|")[0])):
        niveau, _, matiere = nm.partition("|")
        sujet = (f.get(f"sujet:{nm}") or "").strip()
        choisis = [int(i) for i in f.getlist(f"docs:{nm}") if str(i).isdigit()]
        ids = choisis or db.choisir_documents(niveau, matiere, sujet) or db.choisir_documents(niveau, matiere)
        if not ids:
            raise HTTPException(400, f"aucun cours classé en {matiere} ({niveau})")
        g = groupes.setdefault(matieres.concept(matiere), {"matiere": matiere, "niveaux": [], "sujets": [], "ids": []})
        g["niveaux"].append(niveau)
        g["ids"] += [i for i in ids if i not in g["ids"]]
        if sujet and sujet not in g["sujets"]:
            g["sujets"].append(sujet)
    epreuves = _epreuves(f, groupes)
    pieces = await _pieces(f)
    campagne_id = db.creer_campagne(titre, consigne, debut, fin, epreuves)
    if pieces:
        db.ajouter_pieces(pieces, campagne_id=campagne_id)
    for cle, g in groupes.items():
        sujet = ", ".join(g["sujets"])
        consigne_rev = consigne
        if len(g["niveaux"]) > 1:
            annees = ", ".join(reversed(g["niveaux"]))
            consigne_rev = (f"{consigne}\n\n" if consigne else "") + (
                f"Ce support couvre {g['matiere']} sur plusieurs années ({annees}) : reprends les notions "
                "de chacune de ces années, pas seulement la plus récente.")
        date_epreuve = next((dt.date.fromisoformat(e["date"]) for e in epreuves if e["cle"] == cle), fin)
        for genre in genres:
            db.creer_revision(genre, f"{g['matiere']} : {sujet}" if sujet else g["matiere"], g["ids"],
                              date_epreuve if genre == "examen" else None, consigne=consigne_rev, campagne_id=campagne_id)
    db.demander_scan()
    return RedirectResponse(f"/campagne/{campagne_id}", status_code=303)


@app.get("/campagne/{campagne_id}", response_class=HTMLResponse)
def voir_campagne(request: Request, campagne_id: int):
    campagne = db.campagne(campagne_id)
    if campagne is None:
        raise HTTPException(404)
    revs = db.revisions_campagne(campagne_id)
    par_matiere = defaultdict(list)
    for r in revs:
        par_matiere[r["matiere"] or "Autres"].append(r)
    jours = revision.jours_programme(campagne["programme"], campagne["faits"], revs)
    taches = [t for j in jours for t in j["taches"]]
    aujourdhui = dt.datetime.now(pronote.PARIS).date()
    return templates.TemplateResponse(request, "campagne.html", {
        "campagne": campagne, "par_matiere": dict(par_matiere), "revs": revs, "jours": jours,
        "genres": revision.GENRES, "courts": revision.COURTS, "aujourdhui": aujourdhui,
        "faites": sum(t["fait"] for t in taches), "nb_taches": len(taches),
        "en_attente": campagne["statut"] == "en_attente" or any(r["statut"] == "en_attente" for r in revs),
        "erreurs": [r for r in revs if r["statut"] == "erreur"], "pieces": db.pieces_jointes(campagne_id=campagne_id)})


@app.post("/campagne/{campagne_id}/tache", response_class=HTMLResponse)
def cocher_tache(request: Request, campagne_id: int, cle: str = Form(...)):
    if db.campagne(campagne_id) is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}:\d{1,2}", cle):
        raise HTTPException(400)
    fait = db.basculer_tache(campagne_id, cle)
    if request.headers.get("HX-Request"):
        return HTMLResponse("", headers={"HX-Trigger": json.dumps({"tache": {"cle": cle, "fait": fait}})})
    return RedirectResponse(f"/campagne/{campagne_id}", status_code=303)


@app.post("/campagne/{campagne_id}/relancer")
def relancer_campagne(campagne_id: int):
    if db.campagne(campagne_id) is None:
        raise HTTPException(404)
    db.relancer_campagne(campagne_id)
    db.demander_scan()
    return RedirectResponse(f"/campagne/{campagne_id}", status_code=303)


@app.post("/campagne/{campagne_id}/supprimer")
def supprimer_campagne(campagne_id: int, garder: bool = Form(False)):
    if db.campagne(campagne_id) is None:
        raise HTTPException(404)
    db.supprimer_campagne(campagne_id, avec_revisions=not garder)
    return RedirectResponse("/reviser", status_code=303)


@app.get("/campagne/{campagne_id}/pdf")
def campagne_pdf(campagne_id: int, corrige: int = 1, telecharger: int = 0):
    """Livret : programme jour par jour puis tous les supports prêts."""
    campagne = db.campagne(campagne_id)
    if campagne is None:
        raise HTTPException(404)
    revs = [db.revision(r["id"]) for r in db.revisions_campagne(campagne_id) if r["statut"] == "pret"]
    reperes = {}
    for r in revs:
        reperes.update({f"{r['id']}:{k}": v for k, v in (r["reperes"] or {}).items()})
    contenu = pdf.campagne_pdf(campagne, revs, _sources(reperes), corrige=bool(corrige))
    return _pdf(contenu, pdf.nom(campagne["titre"]), bool(telecharger))


# --- Demander : questions dans un chat (question.py) ---------------------------

def _portee(niveau: str | None, matiere: str | None) -> str:
    return f" de {matiere} ({niveau})" if matiere else ""


def _claude_en_pause() -> str | None:
    """Pourquoi l'IA ne peut pas répondre en ce moment (limite du forfait, jeton refusé), ou None."""
    c = etat_service()["claude"]
    if c["etat"] == "limite":
        quand = f" (reprise vers {_heure_fr(c['jusqu_a'])})" if c["jusqu_a"] else ""
        return f"limite de ton forfait Claude atteinte{quand}"
    if c["etat"] == "connexion":
        return "Claude refuse le jeton du forfait"
    return None


def _passages(q: str, conv: dict, limite: int = 10) -> list[dict]:
    """Passages de tous les documents lus (par les mots et par le sens), ou documents entiers si l'index
    n'est pas encore fait."""
    vecteur = index.vecteur_question(q) if not ressources.sature() else None
    # Deux fois plus de candidats, puis les meilleurs gardés après relecture (FlashRank, si le service tourne)
    passages = db.chercher_passages(question.requete(q), vecteur, conv["niveau"], conv["matiere"], limite * 2)
    if passages:
        return question.reordonner(q, passages, limite)
    return [{**d, "document_id": d["id"], "texte": (d.get("transcription") or "")[:3000]}
            for d in db.cours_pour_question(question.requete(q), conv["niveau"], conv["matiere"], 5,
                                            avec_texte=True, statuts=db.STATUTS_UTILES)]


def _agenda_texte(jours: int = 30) -> str:
    """Contrôles et devoirs à venir, pour les questions d'organisation (« qu'est-ce que je révise ce soir ? »)."""
    try:
        lignes = []
        for e in db.evenements_a_venir(None, jours)[:25]:
            le = e["le"].astimezone(pronote.PARIS)
            quoi = "évaluation" if e["genre"] == "evaluation" else "devoir"
            lignes.append(f"- {notifications.JOURS[le.weekday()]} {le:%d/%m} : {quoi} de {matiere_lisible(e['matiere'])}"
                          f" — {e['titre']}{' (fait)' if e.get('fait') else ''}")
        return "\n".join(lignes)
    except Exception:
        return ""


def _repondre_localement(msg_id: int, q: str, conv: dict, pourquoi: str = ""):
    contenu, reperes = question.reponse_locale(q, _passages(q, conv, 8), _portee(conv["niveau"], conv["matiere"]))
    if pourquoi:
        contenu = f"*L'IA ne peut pas répondre maintenant ({pourquoi}) : voici ce que j'ai trouvé sans elle.*\n\n{contenu}"
    db.maj_message(msg_id, "pret", contenu, reperes, role="local")


def _local_libre() -> str | None:
    """Pourquoi le modèle local ne doit pas répondre au chat maintenant (pas installé, saturé, occupé), ou None."""
    pourquoi = local.disponible()
    if pourquoi:
        return pourquoi
    if any(e.startswith("Analyse locale") for e in etat_service().get("en_cours") or []):
        return "modèle local occupé par une analyse"
    return None


def _repondre_ia(msg_id: int, q: str, conv: dict, historique: list[dict]):
    """Lancé dans un fil à part : la page interroge /demander/message/<id> jusqu'à la réponse.

    Selon le réglage (page Activité) : le modèle local d'abord, Claude seulement s'il doute ou demande de
    l'aide (et 1 fois sur 10 pour contrôler) ; ou Claude d'abord, le local en secours.
    """
    portee = _portee(conv["niveau"], conv["matiere"]).strip()
    brouillon = None
    try:
        passages, agenda = _passages(q, conv), _agenda_texte()
        mode, pause = local.reglages()["mode"], _claude_en_pause()
        if mode in ("local", "toujours") or pause:
            if not _local_libre():
                try:
                    brouillon = question.repondre_local(q, historique, passages, agenda, portee)
                except ErreurAgent as e:
                    print(f"modèle local indisponible pour Demander : {e}", flush=True)
            if brouillon and (mode == "toujours" or pause or (not brouillon["aide"]
                                                              and random.random() >= local.CONTROLE)):
                avertissement = ""
                if brouillon["aide"]:
                    raison = f"Claude n'est pas disponible ({pause})" if pause else "le réglage « local uniquement »"
                    avertissement = (f"*Réponse du modèle local, qui n'est pas sûr de lui ; {raison}.*\n\n")
                db.maj_message(msg_id, "pret", avertissement + brouillon["reponse"], brouillon["reperes"],
                               modele="local", detail=f"Modèle local · confiance {brouillon['confiance']:.0%}")
                return
            if pause:
                _repondre_localement(msg_id, q, conv, pause)
                return
        contenu, reperes, web, modele, verdict = question.repondre_ia(q, historique, passages, portee, agenda,
                                                                      brouillon)
        detail = None
        if brouillon:
            detail = ("Brouillon du modèle local, confirmé par Claude" if verdict
                      else "Brouillon du modèle local, corrigé et complété par Claude")
            if verdict is not None:
                db.noter_controle("question", verdict, q[:200])
        db.maj_message(msg_id, "pret", contenu, reperes, web, modele, detail=detail)
    except (LimiteAtteinte, ClaudeIndisponible) as e:
        pourquoi = ("limite de ton forfait Claude atteinte" if isinstance(e, LimiteAtteinte)
                    else f"Claude ne répond pas : {str(e)[:120]}")
        if brouillon:   # le modèle local avait répondu : sa réponse plutôt que des passages bruts
            doute = " Il n'était pas sûr de lui." if brouillon["aide"] else ""
            db.maj_message(msg_id, "pret", f"*Réponse du modèle local, non vérifiée par Claude ({pourquoi}).{doute}*\n\n"
                           + brouillon["reponse"], brouillon["reperes"], modele="local",
                           detail=f"Modèle local · confiance {brouillon['confiance']:.0%} · non vérifié")
        else:
            _repondre_localement(msg_id, q, conv, pourquoi)
    except Exception as e:  # ErreurAgent, base momentanément injoignable…
        try:
            db.maj_message(msg_id, "erreur", erreur=str(e)[:300])
        except Exception:
            pass


def _lancer_reponse(conv: dict, q: str, mode: str) -> int:
    """Crée la réponse : tout de suite sans IA, sinon en attente pendant que l'IA travaille."""
    pause = _claude_en_pause() if mode == "ia" else None
    if mode == "ia" and (not pause or not _local_libre()):
        historique = [m for m in db.messages(conv["id"]) if m["statut"] == "pret"][:-1]  # sans la question
        msg_id = db.ajouter_message(conv["id"], "ia", statut="en_attente")
        threading.Thread(target=_repondre_ia, args=(msg_id, q, conv, historique), daemon=True,
                         name=f"demander-{msg_id}").start()
        return msg_id
    msg_id = db.ajouter_message(conv["id"], "local", statut="en_attente")
    _repondre_localement(msg_id, q, conv, pause or "")
    return msg_id


def _contexte_message(m: dict) -> dict:
    rendu = avec_sources(rendu_markdown(m["contenu"]), m["reperes"], m["sources_web"]) if m["contenu"] else ""
    return {"m": m, "rendu": rendu, "noms": modeles.NOMS}


templates.env.globals["contexte_message"] = _contexte_message


@app.get("/demander", response_class=HTMLResponse)
@app.get("/demander/{conv_id}", response_class=HTMLResponse)
def page_demander(request: Request, conv_id: int | None = None, matiere: str = "", niveau: str = ""):
    """Chat : une conversation, ou une nouvelle (préremplie avec une matière depuis un lien)."""
    conv = None
    if conv_id is not None:
        conv = db.conversation(conv_id)
        if conv is None:
            raise HTTPException(404)
    return templates.TemplateResponse(request, "demander.html", {
        "conv": conv, "messages": db.messages(conv_id) if conv else [], "conversations": db.conversations(15),
        "choix": db.niveaux_matieres_comptes(), "modes": question.MODES, "pause": _claude_en_pause(),
        "pre": f"{niveau}|{matiere}" if matiere else "", "formules_photo": lecture.etat().get("formules", False)})


@app.post("/formule")
async def lire_formule(image: UploadFile = File(...)):
    """Photo recadrée d'une formule → LaTeX (pix2tex, service lecture), à glisser dans une question."""
    contenu = await image.read(8_000_001)
    if len(contenu) > 8_000_000:
        return JSONResponse({"erreur": "Photo trop lourde (8 Mo au plus)."}, status_code=413)
    try:
        return {"latex": lecture.formule(contenu, image.filename or "formule.png")}
    except lecture.LectureIndisponible as e:
        return JSONResponse({"erreur": f"Formule non lue : {e}"}, status_code=503)


@app.post("/demander")
def poser_question(request: Request, question_: str = Form("", alias="question"), q: str = Form(""),
                   mode: str = Form("ia"), portee: str = Form(""), conversation: int | None = Form(None)):
    """`q` : la même question tapée dans une barre de recherche (« Demander à l'IA » de l'accueil)."""
    q = (question_ or q).strip()[:4000]
    if not q:
        raise HTTPException(400, "pose une question")
    mode = mode if mode in question.MODES else "ia"
    conv = db.conversation(conversation) if conversation else None
    nouvelle = conv is None
    if nouvelle:
        niveau, _, matiere = portee.partition("|")
        conv = db.conversation(db.creer_conversation(question.titre(q), niveau, matiere))
    eleve_id = db.ajouter_message(conv["id"], "eleve", q)
    reponse_id = _lancer_reponse(conv, q, mode)
    if nouvelle or not request.headers.get("HX-Request"):
        url = f"/demander/{conv['id']}"
        if request.headers.get("HX-Request"):
            return Response(status_code=204, headers={"HX-Redirect": url})
        return RedirectResponse(url, status_code=303)
    return templates.TemplateResponse(request, "_messages.html", {
        "liste": [db.message(eleve_id), db.message(reponse_id)]})


@app.get("/demander/message/{msg_id}", response_class=HTMLResponse)
def voir_message(request: Request, msg_id: int):
    m = db.message(msg_id)
    if m is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "_messages.html", {"liste": [m]})


@app.post("/demander/message/{msg_id}/ia", response_class=HTMLResponse)
def demander_a_l_ia(request: Request, msg_id: int):
    """Sous une réponse sans IA (ou en erreur) : la même question, cette fois à Claude."""
    m = db.message(msg_id)
    if m is None:
        raise HTTPException(404)
    conv = db.conversation(m["conversation_id"])
    precedentes = [x for x in db.messages(conv["id"]) if x["role"] == "eleve" and x["id"] < msg_id]
    if not precedentes:
        raise HTTPException(400, "pas de question à reposer")
    q = precedentes[-1]["contenu"]
    eleve_id = db.ajouter_message(conv["id"], "eleve", q)
    reponse_id = _lancer_reponse(conv, q, "ia")
    return templates.TemplateResponse(request, "_messages.html", {
        "liste": [db.message(eleve_id), db.message(reponse_id)]})


@app.post("/demander/{conv_id}/supprimer")
def supprimer_conversation(conv_id: int):
    db.supprimer_conversation(conv_id)
    return RedirectResponse("/demander", status_code=303)


# --- Analyses : file, pause, arrêt, analyse sans Claude (local.py) --------------------

ACTIONS_FILE = {"suspendre": "Ne pas analyser pour l'instant", "reprendre": "Remettre dans la file",
                "local": "Modèle local (sur la VM)", "claude": "Claude directement",
                "auto": "Selon le réglage", "supprimer": "Supprimer (corbeille kDrive)"}


def reglages_analyse() -> dict:
    return local.reglages()


templates.env.globals["analyses_en_pause"] = lambda: db.etat_valeur("analyses_pause") == "oui"


@app.get("/analyses", response_class=HTMLResponse)
def page_analyses(request: Request, message: str = ""):
    s = etat_service()
    return templates.TemplateResponse(request, "analyses.html", {
        "s": s, "r": reglages_analyse(), "modes": local.MODES, "file": db.file_analyse(),
        "attente": db.file_attente(), "nb_relire": db.nb_a_relire(), "actions": ACTIONS_FILE, "message": message,
        "modele_local": local.OLLAMA_MODELE, "heure": _heure_fr, "vm": ressources.etat(),
        "fiabilite": db.fiabilite_locale(), "index": db.bilan_index(), "conso": _conso(), "maj_present": maj.present()})


def _conso() -> tuple[list[dict], dt.datetime | None]:
    lignes, releve = maj.conso()
    return lignes, releve.astimezone(pronote.PARIS) if releve else None


@app.post("/analyses/locaux")
def modeles_locaux(action: str = Form(...)):
    """Éteint complètement les modèles locaux (conteneurs arrêtés, plus aucun appel) ou les rallume."""
    if action not in ("eteindre", "allumer"):
        raise HTTPException(400, "action inconnue")
    eteindre = action == "eteindre"
    db.etat_ecrire("modeles_locaux", "eteints" if eteindre else None)
    db.demander_scan()
    try:
        suite = maj.locaux(eteindre)
    except OSError as e:
        suite = f"le service maj n'a pas pu être prévenu ({e})"
    message = ("Modèles locaux éteints : tout passe par Claude. " if eteindre
               else "Modèles locaux rallumés. ") + suite
    return RedirectResponse(f"/analyses?{urlencode({'message': message})}", status_code=303)


@app.post("/analyses/controle")
def controle_analyses(action: str = Form(...)):
    """Pause (le travail en cours finit), reprise, ou arrêt immédiat (les appels en cours sont coupés)."""
    if action not in ("pause", "reprendre", "arreter"):
        raise HTTPException(400, "action inconnue")
    db.etat_ecrire("analyses_pause", None if action == "reprendre" else "oui")
    if action == "arreter":
        db.etat_ecrire("analyses_arreter", "oui")
    db.demander_scan()
    message = {"pause": "Pause demandée : les analyses en cours se terminent, aucune autre ne démarre.",
               "arreter": "Arrêt demandé : les analyses en cours s'arrêtent dans les secondes qui viennent "
                          "(les fichiers restent dans la file).",
               "reprendre": "Les analyses reprennent."}[action]
    return RedirectResponse(f"/analyses?{urlencode({'message': message})}", status_code=303)


@app.post("/analyses/reglages")
def reglages_analyses(mode: str = Form(...), relire: str = Form("")):
    if mode not in local.MODES:
        raise HTTPException(400, "mode inconnu")
    db.etat_ecrire("analyse_locale", mode)
    db.etat_ecrire("relire_avec_claude", "oui" if relire else "non")
    db.demander_scan()
    return RedirectResponse(f"/analyses?{urlencode({'message': 'Réglages enregistrés.'})}", status_code=303)


@app.post("/analyses/file")
def actions_file(ids: list[int] = Form([]), action: str = Form(...)):
    if action not in ACTIONS_FILE:
        raise HTTPException(400, "action inconnue")
    if not ids:
        raise HTTPException(400, "coche au moins un fichier")
    n = db.file_choisir(ids, action)
    if n:
        db.demander_scan()
    pluriel = "s" if n > 1 else ""
    message = f"{n} fichier{pluriel} : {ACTIONS_FILE[action].lower()}."
    return RedirectResponse(f"/analyses?{urlencode({'message': message})}", status_code=303)


@app.post("/analyses/relire")
def relectures(action: str = Form(...)):
    """Documents analysés sans Claude : annuler leur relecture par Claude."""
    if action != "annuler":
        raise HTTPException(400, "action inconnue")
    n = db.annuler_relectures()
    return RedirectResponse(f"/analyses?{urlencode({'message': f'{n} relecture(s) annulée(s).'})}", status_code=303)


# --- Matières en double (matieres.py) -----------------------------------------

def _propositions_matieres() -> tuple[list[dict], list[dict]]:
    comptes = db.matieres_par_annee()
    toutes: dict[str, int] = defaultdict(int)
    for r in comptes:
        toutes[r["matiere"]] += r["n"]
    return comptes, matieres.proposer(comptes, toutes, db.matieres_distinctes())


templates.env.globals["nb_matieres_en_double"] = lambda: len(_propositions_matieres()[1])


@app.get("/organiser/matieres", response_class=HTMLResponse)
def page_matieres(request: Request, message: str = ""):
    comptes, groupes = _propositions_matieres()
    par_niveau = defaultdict(list)
    for r in comptes:
        par_niveau[r["niveau"]].append(r)
    niveaux = [n for n in reversed(NIVEAUX) if n in par_niveau]
    return templates.TemplateResponse(request, "matieres.html", {
        "groupes": groupes, "par_niveau": par_niveau, "niveaux": niveaux, "message": message,
        "pause": _claude_en_pause(), "nb_distinctes": sum(map(len, db.matieres_distinctes().values()))})


@app.post("/organiser/matieres/ia", response_class=HTMLResponse)
def matieres_ia(request: Request):
    """Second avis de l'IA (court appel Haiku) ; les propositions locales restent affichées."""
    comptes, groupes = _propositions_matieres()
    erreur = None
    if not comptes:
        ia = []
    else:
        try:
            ia = matieres.demander_ia(comptes, db.titres_par_matiere(),
                                      local.essayer_json if local.reglages()["mode"] != "jamais" else None)
        except LimiteAtteinte:
            ia, erreur = [], "Limite de ton forfait Claude atteinte : réessaie plus tard."
        except (ClaudeIndisponible, ErreurAgent) as e:
            ia, erreur = [], f"L'IA n'a pas pu répondre : {str(e)[:200]}"
    distinctes = db.matieres_distinctes()
    nouvelles = [g for g in matieres.fusionner_propositions(groupes, ia)[len(groupes):]
                 if not matieres.deja_tranche(g, distinctes)]
    return templates.TemplateResponse(request, "_matieres_ia.html", {"groupes": nouvelles, "erreur": erreur})


@app.post("/organiser/matieres/garder")
async def garder_matieres(request: Request):
    """« Laisser tel quel » : ces matières restent séparées, la proposition ne revient plus."""
    f = await request.form()
    niveau, noms = f.get("niveau", ""), [n for n in f.getlist("tous") if n]
    if niveau not in NIVEAUX or len(set(noms)) < 2:
        raise HTTPException(400, "proposition inconnue")
    db.garder_distinctes(niveau, noms)
    message = (f"{niveau} : {', '.join(sorted(set(noms)))} restent séparées. "
               "Elles ne te seront plus proposées à la fusion, et les prochains scans garderont ces noms.")
    return RedirectResponse(f"/organiser/matieres?{urlencode({'message': message})}", status_code=303)


@app.post("/organiser/matieres/reproposer")
def reproposer_matieres():
    n = db.oublier_distinctes()
    message = f"{n} choix « laisser tel quel » oublié{'s' if n > 1 else ''} : les propositions reviennent."
    return RedirectResponse(f"/organiser/matieres?{urlencode({'message': message})}", status_code=303)


@app.post("/organiser/matieres/fusionner")
async def fusionner_matieres(request: Request):
    """Fusionne des matières d'une même année sous un seul nom (choisi ou tapé)."""
    f = await request.form()
    niveau, nom = f.get("niveau", ""), " ".join((f.get("nom") or "").split())
    noms = [n for n in f.getlist("noms") if n]
    if niveau not in NIVEAUX or not nom or len(set(noms) | {nom}) < 2:
        raise HTTPException(400, "choisis au moins deux matières de la même année et le nom à garder")
    n = db.fusionner_matieres(niveau, noms, nom)
    if n:
        db.demander_scan()  # réveille le worker : il déplace les fichiers dans kDrive
    message = (f"{niveau} : {', '.join(m for m in noms if m != nom)} → {nom}. "
               f"{n} document{'s' if n > 1 else ''} renommé{'s' if n > 1 else ''}, "
               "les fichiers sont déplacés dans kDrive en arrière-plan.")
    return RedirectResponse(f"/organiser/matieres?{urlencode({'message': message})}", status_code=303)


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
        # Quand la carte reviendra, pour chaque bouton (FSRS)
        "apercu": {n: revision.delai(carte, n) for n in revision.NOTES} if carte else {}})


@app.post("/cartes/{carte_id}/noter")
def noter_carte(carte_id: int, note: int = Form(...), paquet: int | None = Form(None)):
    carte = db.carte(carte_id)
    if carte is None or note not in revision.NOTES:
        raise HTTPException(400, "carte ou note invalide")
    db.maj_carte(carte_id, note, revision.repetition(carte, note))
    return RedirectResponse("/cartes/session" + (f"?paquet={paquet}" if paquet else ""), status_code=303)


@app.get("/progression", response_class=HTMLResponse)
def page_progression(request: Request):
    """Régularité, mémoire des flashcards, réussite par matière et notes Pronote."""
    maintenant = dt.datetime.now(pronote.PARIS)
    p = progression.calculer(db.progression_donnees(), maintenant.date(), maintenant)
    return templates.TemplateResponse(request, "progression.html", {"p": p})


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


# --- Installation sur le téléphone et notifications --------------------------

CA = Path(os.environ.get("CLAUNOTE_CA", "/caddy/caddy/pki/authorities/local/root.crt"))


def adresse_https() -> str | None:
    hote = os.environ.get("CLAUNOTE_HOTE", "").strip()
    port = os.environ.get("HTTPS_PORT", "443").strip()
    return f"https://{hote}{'' if port == '443' else ':' + port}" if hote else None


@app.get("/installer", response_class=HTMLResponse)
def installer(request: Request):
    """Pas à pas pour l'iPhone ; ouvert sans connexion (on y arrive avant d'avoir le certificat)."""
    https = request.url.scheme == "https" or request.headers.get("X-Forwarded-Proto") == "https"
    return templates.TemplateResponse(request, "installer.html", {
        "sans_nav": True, "https": https, "adresse": adresse_https(), "certificat": CA.exists()})


@app.get("/certificat.crt")
def certificat():
    """Certificat racine de l'autorité locale de Caddy, au format que l'iPhone sait installer."""
    if not CA.exists():
        raise HTTPException(404, "certificat introuvable : le service caddy a-t-il démarré ?")
    pem = CA.read_text()
    der = base64.b64decode("".join(l for l in pem.splitlines() if l and not l.startswith("-----")))
    return Response(der, media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="claunote.crt"'})


@app.get("/notifications", response_class=HTMLResponse)
def page_notifications(request: Request, message: str = ""):
    return templates.TemplateResponse(request, "notifications.html", {
        "p": notifications.preferences(), "appareils": db.abonnements(),
        "recentes": db.notifications_recentes(8), "cle": notifications.cle_publique(),
        "message": message, "adresse": adresse_https()})


@app.post("/notifications/abonnement")
async def abonner(request: Request):
    d = await request.json()
    try:
        db.enregistrer_abonnement(d["endpoint"], d["keys"]["p256dh"], d["keys"]["auth"],
                                  str(d.get("appareil") or "")[:80])
    except (KeyError, TypeError):
        raise HTTPException(400, "abonnement incomplet")
    return {"ok": True}


@app.post("/notifications/desabonnement")
async def desabonner(request: Request):
    d = await request.json()
    db.retirer_abonnement(str(d.get("endpoint") or ""))
    return {"ok": True}


@app.post("/notifications/retirer")
def retirer_appareil(endpoint: str = Form(...)):
    db.retirer_abonnement(endpoint)
    return RedirectResponse("/notifications", status_code=303)


@app.post("/notifications/test", response_class=HTMLResponse)
def tester_notification():
    if not db.abonnements():
        return "Aucun appareil abonné : active d'abord les notifications sur ton iPhone."
    envoyes, erreurs = notifications.envoyer_a_tous("Claunote", "Les notifications marchent 🎉", "/notifications",
                                                     "test")
    if erreurs and not envoyes:
        return f"Échec de l'envoi : {html.escape(erreurs[0])}. La VM a-t-elle accès à Internet ?"
    return f"Envoyée sur {envoyes} appareil{'s' if envoyes > 1 else ''}."


@app.post("/notifications/reglages")
def regler_notifications(heure: str = Form("18:00"), silence_debut: str = Form("22:00"),
                         silence_fin: str = Form("07:30"), rappel: bool = Form(False), pronote_: bool = Form(False,
                         alias="pronote"), revisions: bool = Form(False)):
    for h in (heure, silence_debut, silence_fin):
        try:
            dt.time.fromisoformat(h)
        except ValueError:
            raise HTTPException(400, "heure invalide")
    notifications.enregistrer_preferences({"heure": heure, "silence_debut": silence_debut,
                                           "silence_fin": silence_fin, "rappel": rappel,
                                           "pronote": pronote_, "revisions": revisions})
    return RedirectResponse("/notifications?message=Réglages+enregistrés", status_code=303)


# --- Agenda (Pronote) -------------------------------------------------------

def _agenda(request: Request, erreur: str | None = None, message: str | None = None):
    try:
        etat = pronote.Client().etat()
    except pronote.ErreurPronote:
        # Service arrêté (mise à jour, redémarrage) : l'agenda de la dernière synchro reste lisible
        etat = {"connecte": bool(db.etat_valeur("pronote_synchro")), "injoignable": True}
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
    return RedirectResponse(f"/reviser/nouvelle?{urlencode(params)}", status_code=303)


@app.post("/scan", response_class=HTMLResponse)
def scan():
    db.demander_scan()
    return "Scan demandé : le worker vérifie l'inbox dans les 5 secondes."


@app.post("/claude/reessayer", response_class=HTMLResponse)
def claude_reessayer():
    """Pendant une pause de Claude : le worker fait un tout petit appel et repart si Claude répond.

    Aucun crédit n'est acheté : le worker n'utilise que le jeton du forfait (agent.ENV_FORFAIT) ;
    si la limite est toujours là, la pause continue simplement.
    """
    db.etat_ecrire("claude_sonde", "oui")
    db.demander_scan()  # réveille le worker
    return "Test demandé : le worker essaie Claude dans les secondes qui viennent (résultat sur la page État)."


@app.get("/etat", response_class=HTMLResponse)
def page_etat(request: Request):
    """Santé du service : worker, Claude (limite du forfait), kDrive, file d'attente, modèles utilisés."""
    s = etat_service()
    indispo = {m: {**v, "jusqu_a": _instant(v.get("jusqu_a"))} for m, v in (s.get("modeles") or {}).items()}
    for m in indispo:  # le site ne voit pas l'état mémoire du worker : il recopie ses indisponibilités
        modeles.marquer_indisponible(m, indispo[m]["jusqu_a"], indispo[m]["raison"])
    return templates.TemplateResponse(request, "etat.html", {
        "s": s, "heure": _heure_fr, "file": db.file_attente(), "taches": modeles.tableau(), "noms": modeles.NOMS,
        "indispo": indispo, "bilan": db.bilan_ia(24), "appels": db.appels_ia(25), "libelles": modeles.LIBELLES,
        "maximum": modeles.maximum(), "sonde_demandee": db.etat_valeur("claude_sonde") is not None,
        "sonde": {**(s.get("sonde") or {}), "le": _instant((s.get("sonde") or {}).get("le"))}})


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
