import base64
import contextvars
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
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import markdown
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from claunote import (actualites, agent, anki, appareils, auth, carte_mentale, cles, comptes, db, decoupage, demande,
                      disque, formules, importation, index, installation, lecture, local, maj, matieres, modeles, notifications, pdf, progression,
                      pronote, question, rangement, reglages, ressources, revision, schemas, securite, sensible,
                      stockage_local)
from claunote.agent import NIVEAUX, TYPES, ClaudeIndisponible, ErreurAgent, LimiteAtteinte
from claunote.kdrive import KDrive, KDriveIndisponible

INBOX_ID = int(os.environ.get("KDRIVE_INBOX_ID", "0"))
ETUDES_ID = int(os.environ.get("KDRIVE_ETUDES_ID", "0"))



@asynccontextmanager
async def lifespan(app):
    db.init()
    app.state.secret = os.environ.get("CLAUNOTE_SECRET") or db.secret_session()
    # « Déconnecter partout » change la génération : toutes les signatures de session changent
    app.state.limiteur = auth.Limiteur()
    comptes.migrer()   # installation d'avant les comptes : son identifiant et son mot de passe font l'admin
    installation.detecter()   # installation neuve : l'assistant s'ouvre ; installation existante : rien d'imposé
    if reglages.stockage() != "kdrive":
        _appliquer_reglages()   # fichiers sur la VM : numéros des dossiers Inbox et Etudes
    agent.journal = _noter_appel  # les appels faits par le site (Demander, matières) vont sur la page État
    if (db.compte(1) or {}).get("mdp_defaut"):
        print("ATTENTION : mot de passe par défaut (admin / claunote), "
              "change-le sur la page Compte du site", flush=True)
    yield


def _cle_sessions(app) -> bytes:
    return (app.state.secret + (db.etat_valeur("session_generation") or "")).encode()


def _noter_appel(**appel):
    try:
        db.journal_ia(**appel)
    except Exception as e:
        print(f"appel à Claude non noté : {e}", flush=True)


# Pas de documentation automatique de l'API (/docs, /openapi.json) : rien à montrer à un curieux
app = FastAPI(title="Claunote", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Pages ouvertes sans connexion : chemins EXACTS (un préfixe « /appareil » ouvrait aussi « /appareils/code »
# et « /appareils/…/retirer », qui doivent rester réservés au compte), plus les fichiers statiques.
PUBLIC = frozenset({"/appareil", "/connexion", "/cles/connexion", "/cles/connexion/options", "/manifest.webmanifest",
                    "/sw.js", "/hors-ligne", "/installer", "/certificat.crt", "/invitation", "/conditions",
                    "/tes-donnees"})
# Connecté mais conditions d'utilisation pas encore acceptées (ou changées) : seulement ces pages
AVANT_CONDITIONS = frozenset({"/conditions", "/tes-donnees", "/deconnexion"})
_moi: contextvars.ContextVar[dict | None] = contextvars.ContextVar("moi", default=None)


def moi() -> dict | None:
    """Le compte connecté de la requête en cours (None sur une page publique)."""
    return _moi.get()


def _session(request: Request) -> dict | None:
    compte = auth.lire_session(_cle_sessions(request.app), request.cookies.get(auth.COOKIE), comptes.trouver)
    return compte if comptes.utilisable(compte) else None
PUBLIC_PREFIXES = ("/static/",)


def _public(chemin: str) -> bool:
    return chemin in PUBLIC or chemin.startswith(PUBLIC_PREFIXES)


async def _servir_compte(request: Request, call_next, compte: dict, droit: str | None):
    if comptes.peut(compte, droit):
        return await call_next(request)
    texte = f"Ton compte n'a pas le droit « {comptes.DROITS[droit][0]} »." if droit in comptes.DROITS \
        else "Réservé à l'admin."
    if request.method != "GET" and droit == "claude":
        texte += " Les fiches, quiz et programmes demandent Claude : vois avec l'admin."
    if request.headers.get("HX-Request") or request.method != "GET":
        return Response(texte, status_code=403)
    return templates.TemplateResponse(request, "refuse.html", {"droit": comptes.DROITS.get(droit)}, status_code=403)


@app.middleware("http")
async def exiger_connexion(request: Request, call_next):
    chemin = request.url.path
    compte = _session(request)
    if compte:
        request.state.compte = compte
        if comptes.doit_accepter(compte) and chemin not in AVANT_CONDITIONS and not chemin.startswith("/static/"):
            if request.method != "GET" or request.headers.get("HX-Request"):
                return Response("conditions d'utilisation à accepter", status_code=403,
                                headers={"HX-Redirect": "/conditions"})
            return RedirectResponse("/conditions", status_code=303)
        if chemin == "/" and comptes.est_admin(compte) and installation.a_faire():
            return RedirectResponse("/installation", status_code=303)   # installation neuve : l'assistant d'abord
        droit = comptes.droit_requis(chemin, request.method)
        if comptes.peut(compte, droit) and comptes.besoin_claude(chemin, request.method):
            droit = "claude"
        jeton = _moi.set(compte)
        # La base ne montre que les données de ce compte (« Tout voir » : l'admin voit tout), refus compris
        try:
            with comptes.pour(compte["id"], comptes.est_admin(compte) and compte.get("tout_voir")):
                return await _servir_compte(request, call_next, compte, droit)
        finally:
            _moi.reset(jeton)
    if _public(chemin):
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
    securite.appareil_inconnu(request)
    if securite.bannie(appareils.ip_client(request)):
        return Response("accès refusé", status_code=403)
    if request.headers.get("HX-Request"):
        return Response(status_code=403, headers={"HX-Redirect": "/appareil"})
    if request.method != "GET":
        return Response("appareil non approuvé", status_code=403)
    suite = chemin + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/appareil?{urlencode({'suite': suite})}", status_code=303)


# Avant les appareils : par Internet, un pays non autorisé ou une adresse bannie ne voit rien du tout
@app.middleware("http")
async def garde_internet(request: Request, call_next):
    if appareils.par_tunnel(request.headers):
        if request.headers.get("cf-connecting-ip") and not appareils.source_cloudflare(request):
            _entete_falsifie(request)
        raison = securite.refus_internet(request)
        if raison:
            return Response(f"accès refusé ({raison})", status_code=403)
    return await call_next(request)


_falsifies: dict[str, float] = {}


def _entete_falsifie(request: Request):
    """Quelqu'un du réseau local (ou du VPN) se fait passer pour Cloudflare : noté une fois par heure."""
    ip = request.client.host if request.client else "?"
    if time.time() - _falsifies.get(ip, 0) < 3600:
        return
    _falsifies[ip] = time.time()
    securite.noter("entete_falsifie", request, f"cf-connecting-ip={request.headers.get('cf-connecting-ip')!r} "
                   "envoyé par une machine qui n'est pas cloudflared (ou CLAUNOTE_CLOUDFLARED à régler)", ip=ip,
                   alerte=f"La machine {ip} (réseau local ou VPN) envoie de faux en-têtes Cloudflare.")


METHODES_SURES = {"GET", "HEAD", "OPTIONS"}


def _meme_origine(request: Request) -> bool:
    """Un formulaire ou un script d'un autre site (même un autre sous-domaine) ne peut rien changer ici."""
    site = request.headers.get("sec-fetch-site")
    if site in ("cross-site", "same-site"):
        return False
    origine = request.headers.get("origin")
    if origine == "null":
        return site == "same-origin"
    if origine:
        host = request.headers.get("host", "")
        return urlsplit(origine).netloc.lower() == host.lower()
    return True   # ni Origin ni Sec-Fetch-Site : pas un navigateur (curl…), donc pas une attaque CSRF


@app.middleware("http")
async def anti_csrf(request: Request, call_next):
    if request.method not in METHODES_SURES and not _meme_origine(request):
        securite.noter("csrf", request, f"{request.method} {request.url.path} depuis "
                       f"{request.headers.get('origin') or request.headers.get('sec-fetch-site')}",
                       alerte=f"Une page d'un autre site a essayé d'agir sur Claunote ({request.url.path}) : refusé.")
        return Response("requête refusée : elle ne vient pas de ce site", status_code=403)
    return await call_next(request)


# Pas de script ni d'image venus d'ailleurs, pas d'envoi de données ailleurs, pas d'affichage dans un autre site
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; media-src 'self' blob:; "
       "frame-src 'self' blob:; worker-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'self'")
EN_TETES = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN",
            "Referrer-Policy": "same-origin", "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Permissions-Policy": "camera=(self), microphone=(), geolocation=(), payment=(), usb=(), "
                                  "interest-cohort=()"}


# Déclaré en dernier, donc exécuté en premier : en-têtes sur toutes les réponses, même les refus
@app.middleware("http")
async def en_tetes_securite(request: Request, call_next):
    try:
        r = await call_next(request)
    except Exception as e:
        securite.erreur_serveur(request, f"{type(e).__name__}: {e}"[:300])
        r = Response("Erreur du serveur : réessaie dans un instant.", status_code=500)
    else:
        if r.status_code == 500:
            securite.erreur_serveur(request, f"réponse 500 sur {request.url.path}")
    for nom, valeur in EN_TETES.items():
        r.headers.setdefault(nom, valeur)
    if appareils.par_tunnel(request.headers):   # nom de domaine Cloudflare : HTTPS obligatoire pendant 1 an
        r.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return r


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
templates.env.globals["installation_a_faire"] = installation.a_faire
templates.env.globals["mdp_par_defaut"] = lambda: bool((moi() or {}).get("mdp_defaut"))
templates.env.globals["moi"] = moi
templates.env.globals["peut"] = lambda droit: comptes.peut(moi(), droit)
# Boutons Public / Privé : seulement s'il y a d'autres comptes, et sur ses propres éléments
templates.env.globals["partage_possible"] = lambda: bool(moi()) and db.plusieurs_comptes()
templates.env.globals["a_moi"] = lambda element: bool(moi()) and element.get("compte_id", moi()["id"]) == moi()["id"]
templates.env.globals["est_admin"] = lambda: comptes.est_admin(moi())
templates.env.globals["paris_tz"] = pronote.PARIS
templates.env.globals["contact"] = (os.environ.get("CLAUNOTE_CONTACT") or "").strip()   # mentions légales
templates.env.globals["disque_admin"] = lambda: disque.etat() if comptes.est_admin(moi()) else None
templates.env.globals["nb_mises_a_jour"] = maj.disponibles
templates.env.globals["nb_actualites"] = lambda: db.nb_actualites_nouvelles()
templates.env.globals["nb_alertes_securite"] = securite.nb_nouvelles_alertes
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
    # Listes et tableaux collés au texte décollés (sinon une ligne qui suit un tableau en devient une rangée)
    texte, figures = schemas.proteger(revision.aerer_listes(texte or ""))
    texte, maths = formules.proteger(texte)
    # Le HTML brut éventuel de la transcription est neutralisé avant le rendu
    texte = texte.replace("&", "&amp;").replace("<", "&lt;")
    rendu = formules.restaurer(markdown.markdown(texte, extensions=["tables", "fenced_code"]), maths)
    return schemas.restaurer(liens_surs(rendu), figures)


SCHEMA_URL = re.compile(r"^\s*([a-z][a-z0-9+.\-]*)\s*:", re.I)
ATTRIBUT_URL = re.compile(r'\b(href|src)="([^"]*)"', re.I)


def liens_surs(rendu: str) -> str:
    """Liens du Markdown (écrit par l'IA à partir des scans) : http(s), mailto ou relatifs, jamais javascript:."""
    def sur(m):
        brut = html.unescape(m.group(2))
        brut = "".join(c for c in brut if ord(c) > 32)   # « java\tscript: » est lu « javascript: » par le navigateur
        schema = SCHEMA_URL.match(brut)
        if schema and schema.group(1).lower() not in ("http", "https", "mailto"):
            return f'{m.group(1)}="#"'
        return m.group(0)
    return ATTRIBUT_URL.sub(sur, rendu)


# Texte simple (questions de quiz, flashcards) avec des formules $…$
templates.env.filters["maths"] = formules.en_ligne
templates.env.filters["schema"] = schemas.figure


def avec_sources(rendu: str, reperes: dict, web: list[dict] | None = None) -> Markup:
    """Transforme les repères [D1] du texte en liens vers le cours d'origine, [W1] vers la page web,
    et [IA] (ajouté par l'IA, pas dans les cours) en étiquette visible."""
    pages = {w["repere"]: w["url"] for w in web or []}

    def lien(m):
        if m.group(1).startswith("W"):
            url = pages.get(m.group(1))
            return (f'<a class="web" href="{html.escape(url)}" target="_blank" rel="noopener noreferrer">'
                    f'[{m.group(1)}]</a>') if url else m.group(0)
        doc_id = reperes.get(m.group(1))
        return f'<a href="/doc/{doc_id}">[{m.group(1)}]</a>' if doc_id else m.group(0)
    rendu = re.sub(r"\[([DW]\d+)\]", lien, rendu)
    return Markup(re.sub(r"\[IA\]", HORS_COURS, rendu))


HORS_COURS = '<span class="ajout-ia" title="Ajouté par l\'IA : ce n\'est pas dans tes cours">pas dans tes cours</span>'
templates.env.globals["HORS_COURS"] = Markup(HORS_COURS)


# --- Connexion --------------------------------------------------------------

def _suite_sure(suite: str) -> str:
    # Uniquement un chemin du site : pas de redirection vers un autre domaine
    # (ni caractère de contrôle : le navigateur retire tabulations et retours, « /\t/x.fr » deviendrait « //x.fr »)
    return suite if (suite.startswith("/") and not suite.startswith("//") and "\\" not in suite
                     and not any(ord(c) < 32 or ord(c) == 127 for c in suite)) else "/"


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
    limiteur = request.app.state.limiteur
    ip = appareils.ip_client(request)
    attente = limiteur.bloque(ip) or _verrou_internet(request)
    compte = db.compte_par_identifiant(utilisateur) if not attente else None
    ok = not attente and auth.verifier_compte(compte, mot_de_passe)   # toujours un scrypt : même durée
    if ok and not comptes.utilisable(compte):
        securite.echec("connexion_echec", request, f"compte désactivé « {utilisateur.strip()[:40]} »")
        return _page_connexion(request, suite, "Ce compte est désactivé : vois avec l'admin.", 403)
    refus = _refus_internet(request, compte if ok else None)
    if refus:
        return _page_connexion(request, suite, refus, 403)
    if not ok:
        if not attente:
            limiteur.echec(ip)
            securite.echec("connexion_echec", request, f"identifiant « {utilisateur.strip()[:40]} »")
        attente = attente or limiteur.bloque(ip)
        erreur = (f"Trop d'essais : réessaie dans {attente // 60 + 1} min." if attente
                  else "Identifiant ou mot de passe incorrect.")
        return _page_connexion(request, suite, erreur, 401)
    limiteur.reussite(ip)
    securite.reussite(request, f"mot de passe ({compte['identifiant']})")
    r = RedirectResponse(_suite_sure(suite), status_code=303)
    _cookie_session(request, r, compte)
    return r


def _page_connexion(request: Request, suite: str, erreur: str, statut: int) -> HTMLResponse:
    return templates.TemplateResponse(request, "connexion.html", {"suite": _suite_sure(suite), "erreur": erreur,
                                                                   "sans_nav": True, "face_id": _face_id(request)},
                                      status_code=statut)


def _verrou_internet(request: Request) -> int:
    """Secondes de fermeture des connexions par Internet (trop d'échecs) ; jamais par le VPN."""
    return securite.verrouille() if appareils.par_tunnel(request.headers) else 0


def _refus_internet(request: Request, compte: dict | None = None) -> str | None:
    """Par Internet, le mot de passe par défaut ne suffit pas : n'importe qui le connaît."""
    if appareils.par_tunnel(request.headers) and (compte or {}).get("mdp_defaut"):
        securite.noter("mot_de_passe_defaut", request, "connexion refusée", alerte=(
            "Connexion par Internet refusée : le mot de passe est encore admin / claunote. "
            "Change-le par le VPN (Réglages → Compte)."))
        return ("Par Internet, le mot de passe par défaut est refusé. Change-le d'abord par le VPN "
                "(Réglages → Compte).")
    return None


def _cookie_session(request: Request, reponse: Response, compte: dict):
    comptes.oublier(compte["id"])
    compte = db.compte(compte["id"]) or compte   # génération et mot de passe à jour
    reponse.set_cookie(auth.COOKIE, auth.creer_session(_cle_sessions(request.app), compte), max_age=auth.DUREE,
                       httponly=True, samesite="lax", secure=_https(request))
    try:
        db.maj_compte(compte["id"], connecte_le=dt.datetime.now(dt.timezone.utc))
    except Exception:
        pass


def _https(request: Request) -> bool:
    return (request.url.scheme == "https" or request.headers.get("X-Forwarded-Proto") == "https"
            or appareils.par_tunnel(request.headers))   # Cloudflare sert toujours le site en HTTPS


def _origine(request: Request) -> str:
    return f"{'https' if _https(request) else request.url.scheme}://{request.headers.get('host', '')}"


def _contexte_compte(request: Request, **plus) -> dict:
    host = request.headers.get("host", "")
    return {"utilisateur": (moi() or {}).get("identifiant", ""), "cles": db.cles_acces(),
            "cles_impossible": cles.possible(host, _https(request)), "nom_sslip": cles.nom_sslip(host),
            "hote": cles.hote(host), "appareils": appareils.liste(), "code_expire": appareils.code_en_cours(),
            "par_tunnel": appareils.par_tunnel(request.headers), "droits_noms": {d: v[0] for d, v in comptes.DROITS.items()},
            "appareil_ici": appareils.reconnu(request.cookies.get(appareils.COOKIE)), **plus}


# --- Appareils approuvés pour l'accès par Internet (Cloudflare Tunnel) -------

@app.get("/appareil", response_class=HTMLResponse)
def page_appareil(request: Request, suite: str = "/"):
    return templates.TemplateResponse(request, "appareil.html", {"suite": _suite_sure(suite), "sans_nav": True})


@app.post("/appareil", response_class=HTMLResponse)
def approuver_appareil(request: Request, code: str = Form(...), nom: str = Form("Appareil"), suite: str = Form("/")):
    limiteur = request.app.state.limiteur
    ip = appareils.ip_client(request)
    attente = limiteur.bloque(ip) or _verrou_internet(request)
    valeur = None if attente else appareils.utiliser_code(code, nom)
    if not valeur:
        if not attente:
            limiteur.echec(ip)
            securite.echec("code_echec", request, f"nom donné « {nom.strip()[:60]} »")
        attente = attente or limiteur.bloque(ip)
        erreur = (f"Trop d'essais : réessaie dans {attente // 60 + 1} min." if attente
                  else "Code inconnu ou expiré : crée-en un nouveau (Réglages → Compte).")
        return templates.TemplateResponse(request, "appareil.html", {"suite": _suite_sure(suite), "sans_nav": True,
                                                                     "erreur": erreur, "nom": nom}, status_code=403)
    nom_appareil = (nom or "Appareil").strip()[:60] or "Appareil"
    securite.noter("appareil_ajoute", request, f"« {nom_appareil} »", alerte=(
        f"« {nom_appareil} » peut maintenant ouvrir le site par Internet. Si ce n'est pas toi : "
        "Réglages → Compte → Retirer, puis Sécurité → Déconnecter partout."))
    r = RedirectResponse(_suite_sure(suite), status_code=303)
    r.set_cookie(appareils.COOKIE, valeur, max_age=appareils.DUREE, httponly=True, samesite="lax", secure=True)
    return r


@app.post("/appareils/code", response_class=HTMLResponse)
def creer_code_appareil(request: Request):
    securite.noter("code_cree", request, "code valable 15 min")
    return templates.TemplateResponse(request, "compte.html",
                                      _contexte_compte(request, code_appareil=appareils.nouveau_code()))


@app.post("/appareils/{ident}/retirer")
def retirer_appareil(request: Request, ident: str):
    nom = next((a["nom"] for a in appareils.liste() if a["id"] == ident), ident)
    appareils.retirer(ident)
    securite.noter("appareil_retire", request, f"« {nom} »")
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
    return _reponse_options(*cles.options_inscription(host, moi()["identifiant"],
                                                      [k["id"] for k in db.cles_acces()]))


@app.post("/cles/inscription")
async def cles_inscription(request: Request):
    corps = cles.lire(await request.body())
    try:
        cle = cles.verifier_inscription(request.cookies.get(COOKIE_DEFI), corps.get("cle") or {},
                                        request.headers.get("host", ""), _origine(request))
    except cles.CleRefusee as e:
        return JSONResponse({"erreur": str(e)}, status_code=400)
    nom = (corps.get("nom") or "Appareil").strip()[:60]
    db.ajouter_cle(cle, nom)
    securite.noter("cle_ajoutee", request, f"« {nom} »", alerte=(
        f"Clé Face ID « {nom} » ajoutée : elle permet de se connecter sans mot de passe. "
        "Si ce n'est pas toi : Réglages → Compte → Supprimer."))
    return {"ok": True}


@app.post("/cles/{cle_id}/supprimer")
def cles_supprimer(request: Request, cle_id: str):
    db.supprimer_cle(cle_id)
    securite.noter("cle_supprimee", request, f"clé {cle_id[:12]}…")
    return RedirectResponse("/compte#cles", status_code=303)


@app.post("/cles/connexion/options")
def cles_connexion_options(request: Request):
    host = request.headers.get("host", "")
    if cles.possible(host, _https(request)):
        raise HTTPException(400, "Face ID indisponible à cette adresse")
    return _reponse_options(*cles.options_connexion(host))


@app.post("/cles/connexion")
async def cles_connexion(request: Request):
    limiteur = request.app.state.limiteur
    ip = appareils.ip_client(request)
    if limiteur.bloque(ip) or _verrou_internet(request):
        return JSONResponse({"erreur": "Trop d'essais : réessaie dans quelques minutes."}, status_code=429)
    corps = cles.lire(await request.body())
    reponse = corps.get("cle") or {}
    cle = db.cle_acces(cles.identifiant(reponse))
    try:
        if cle is None:
            raise cles.CleRefusee("clé inconnue : ajoute cet appareil depuis Réglages → Compte")
        compteur = cles.verifier_connexion(request.cookies.get(COOKIE_DEFI), reponse, cle,
                                           request.headers.get("host", ""), _origine(request))
        compte = db.compte(cle.get("compte_id") or 1)
        if not comptes.utilisable(compte):
            raise cles.CleRefusee("ce compte est désactivé : vois avec l'admin")
    except cles.CleRefusee as e:
        limiteur.echec(ip)
        securite.echec("faceid_echec", request, str(e)[:200])
        return JSONResponse({"erreur": str(e)}, status_code=400)
    db.cle_utilisee(cle["id"], compteur)
    securite.reussite(request, f"Face ID « {cle.get('nom') or 'clé'} »")
    r = JSONResponse({"suite": _suite_sure(str(corps.get("suite") or "/"))})
    _cookie_session(request, r, compte)
    r.delete_cookie(COOKIE_DEFI)
    return r


@app.post("/compte", response_class=HTMLResponse)
def changer_mot_de_passe(request: Request, actuel: str = Form(...), nouveau: str = Form(...),
                         confirmation: str = Form(...)):
    compte = db.compte(moi()["id"])
    limiteur = request.app.state.limiteur
    ip = appareils.ip_client(request)
    erreur = None
    if limiteur.bloque(ip) or not auth.verifier_compte(compte, actuel):
        limiteur.echec(ip)
        erreur = "Mot de passe actuel incorrect."
        securite.echec("connexion_echec", request, "mot de passe actuel faux (page Compte)")
    else:
        erreur = comptes.verifier_mot_de_passe(nouveau, confirmation, compte["identifiant"])
    if erreur:
        return templates.TemplateResponse(request, "compte.html", _contexte_compte(request, erreur=erreur),
                                          status_code=400)
    # Nouveau mot de passe = nouvelles signatures : les autres appareils sont déconnectés, pas celui-ci
    comptes.changer_mot_de_passe(compte["id"], nouveau)
    securite.noter("mot_de_passe", request, f"changé sur la page Compte ({compte['identifiant']})", alerte=(
        "Le mot de passe du site vient d'être changé. Si ce n'est pas toi : sur la VM, "
        "docker compose exec web python -m claunote.auth reinitialiser"))
    r = templates.TemplateResponse(request, "compte.html", _contexte_compte(
        request, message="Mot de passe changé. Les autres appareils devront se reconnecter."))
    _cookie_session(request, r, compte)
    return r


# --- Installation : assistant pas à pas, puis Réglages → Installation (installation.py, reglages.py) -------

def _appliquer_reglages():
    """Le site prend les nouveaux réglages tout de suite ; le worker voit « reglages_version » et redémarre."""
    global INBOX_ID, ETUDES_ID
    try:
        ETUDES_ID, INBOX_ID = reglages.dossiers()
    except OSError as e:   # dossier des fichiers de la VM pas monté
        print(f"dossiers du stockage illisibles : {e}", flush=True)
    agent.rafraichir_env()


def _json_ou_rien(texte):
    try:
        return json.loads(texte) if texte else None
    except ValueError:
        return None


def _page_installation(request: Request, etape: str | None, statut: int = 200, **plus):
    return templates.TemplateResponse(request, "installation.html", {
        "etape": etape, "etapes": installation.ETAPES, "noms": installation.NOMS,
        "precedente": installation.precedente(etape) if etape else None,
        "r": reglages.etat(), "stockages": reglages.STOCKAGES, "stockage": reglages.stockage(),
        "kdrive_ok": reglages.stockage_pret(), "fichiers_dir": str(stockage_local.dossier()),
        "copie_kdrive": _json_ou_rien(db.etat_valeur("copie_kdrive")), "disque": disque.etat(), "besoins": disque.BESOINS,
        "besoin_total": disque.BESOIN_INSTALLATION, "tunnel_actif": maj.tunnel_actif(), "maj_present": maj.present(),
        "analyse": local.reglages(), "compte_admin": db.compte(moi()["id"]), "a_faire": installation.a_faire(),
        **plus}, status_code=statut)


@app.get("/installation")
def installation_debut():
    return RedirectResponse("/installation/disque", status_code=303)


@app.get("/installation/{etape}", response_class=HTMLResponse)
def installation_etape(request: Request, etape: str):
    if etape not in installation.NOMS:
        raise HTTPException(404)
    return _page_installation(request, etape)


@app.get("/reglages/installation", response_class=HTMLResponse)
def reglages_installation(request: Request, message: str = ""):
    return _page_installation(request, None, message=message)


def _noter_reglage(request: Request, quoi: str):
    securite.noter("reglage", request, quoi, alerte=f"Réglage de l'installation changé sur le site : {quoi}.")


def _etape_compte(request: Request, form: FormData) -> tuple[str | None, str | None]:
    """(message, erreur). Nouveau mot de passe obligatoire tant que c'est encore admin / claunote."""
    compte = db.compte(moi()["id"])
    identifiant = (form.get("identifiant") or compte["identifiant"]).strip().lower()
    mdp, confirmation = form.get("mot_de_passe") or "", form.get("confirmation") or ""
    if identifiant != compte["identifiant"]:
        erreur = comptes.verifier_identifiant(identifiant)
        if erreur:
            return None, erreur
    if mdp or compte.get("mdp_defaut"):
        erreur = comptes.verifier_mot_de_passe(mdp, confirmation, identifiant)
        if erreur:
            return None, erreur
    if identifiant != compte["identifiant"] and not db.renommer_compte(compte["id"], identifiant):
        return None, "Cet identifiant est déjà pris."
    db.maj_compte(compte["id"], nom=(form.get("nom") or "").strip()[:60] or None)
    if mdp:
        comptes.changer_mot_de_passe(compte["id"], mdp)
        securite.noter("mot_de_passe", request, f"choisi dans l'assistant d'installation ({identifiant})", alerte=True)
    comptes.oublier(compte["id"])
    reglages.ecrire("CLAUNOTE_CONTACT", (form.get("contact") or "").strip()[:200], compte["id"])
    return "Compte enregistré.", None


def _etape_stockage(request: Request, form: FormData, action: str) -> tuple[str | None, str | None, dict]:
    """kdrive : tout sur kDrive ; local : tout sur la VM ; les_deux : sur la VM + copie de Etudes/ sur kDrive."""
    choix = form.get("stockage") or "kdrive"
    if choix not in reglages.STOCKAGES:
        return None, "Choix de stockage inconnu.", {}
    if choix != reglages.stockage():
        with db.sans_compte():
            deja = db.nb_documents()
        if deja:   # leurs fichiers sont là où ils ont été rangés : changer les rendrait introuvables
            return None, (f"Il y a déjà {deja} document(s) rangé(s) avec « {reglages.STOCKAGES[reglages.stockage()]} » : "
                          "changer de stockage les rendrait introuvables. Ce choix se fait à l'installation."), {}
    compte_id = moi()["id"]
    if choix == "local":
        if action == "tester":
            d = disque.etat(str(stockage_local.dossier()))
            return (d["phrase"] if d else "Espace du dossier des fichiers inconnu."), None, {}
        try:
            stockage_local.Local.from_env()   # crée Inbox, Etudes et la corbeille
        except OSError as e:
            return None, f"Dossier des fichiers inutilisable ({stockage_local.dossier()}) : {e}", {}
        reglages.ecrire("STOCKAGE", choix, compte_id)
        _noter_reglage(request, "fichiers sur la VM")
        return f"Fichiers sur la VM, dans {stockage_local.dossier()}.", None, {}
    jeton = installation.secret_ou_garde("KDRIVE_TOKEN", form.get("kdrive_token"))
    ids = {}
    for cle, nom in (("KDRIVE_ID", "Numéro du kDrive"), ("KDRIVE_ETUDES_ID", "Dossier des cours"),
                     ("KDRIVE_INBOX_ID", "Dossier inbox")):
        ids[cle], erreur = installation.nombre(form.get(cle.lower()), nom)
        if erreur:
            return None, erreur, {}
    if choix == "les_deux":
        ids.pop("KDRIVE_INBOX_ID")   # les scans arrivent sur la VM : kDrive ne garde que la copie
    if not jeton or not ids["KDRIVE_ID"]:
        return None, "Il faut le jeton API et le numéro du kDrive.", {}
    if action == "creer_dossiers":
        try:
            etudes, inbox = reglages.creer_dossiers_kdrive(jeton, ids["KDRIVE_ID"])
        except Exception as e:
            return None, f"kDrive refuse : {str(e)[:200]}", {}
        ids["KDRIVE_ETUDES_ID"] = str(etudes)
        if "KDRIVE_INBOX_ID" in ids:
            ids["KDRIVE_INBOX_ID"] = str(inbox)
    elif action == "tester":
        ok, texte, racine = reglages.tester_kdrive(jeton, ids["KDRIVE_ID"], *(int(v) for k, v in ids.items()
                                                                               if k != "KDRIVE_ID" and v))
        return (texte, None, {"racine": racine, "saisi": ids}) if ok else (None, texte, {"saisi": ids})
    if not all(ids.values()):
        return None, ("Choisis le dossier de la copie (ou « Créer les dossiers »)." if choix == "les_deux"
                      else "Choisis les deux dossiers (ou « Créer les dossiers »)."), {"saisi": ids}
    ok, texte, _ = reglages.tester_kdrive(jeton, ids["KDRIVE_ID"], *(int(v) for k, v in ids.items() if k != "KDRIVE_ID"))
    if not ok:
        return None, texte + " Rien n'a été enregistré.", {"saisi": ids}
    if choix == "les_deux":
        try:
            stockage_local.Local.from_env()
        except OSError as e:
            return None, f"Dossier des fichiers inutilisable ({stockage_local.dossier()}) : {e}", {"saisi": ids}
    reglages.ecrire("STOCKAGE", choix, compte_id)
    if (form.get("kdrive_token") or "").strip():
        reglages.ecrire("KDRIVE_TOKEN", jeton, compte_id)
    for cle, valeur in ids.items():
        reglages.ecrire(cle, valeur, compte_id)
    _noter_reglage(request, "stockage " + reglages.STOCKAGES[choix])
    cree = " Dossiers Claunote/Etudes et Claunote/Inbox prêts." if action == "creer_dossiers" else ""
    quoi = "Fichiers sur la VM, copie de sauvegarde sur kDrive toutes les heures." if choix == "les_deux" else "kDrive enregistré."
    return f"{quoi}{cree} {texte}", None, {}


def _etape_claude(request: Request, form: FormData, action: str) -> tuple[str | None, str | None]:
    saisi = (form.get("claude_token") or "").strip()
    if action == "sans_claude":
        db.etat_ecrire("analyse_locale", "toujours")
        return ("Sans Claude : les scans sont lus par les modèles locaux de la VM. Les fiches, quiz et questions "
                "demandent Claude : tu pourras ajouter le jeton plus tard ici."), None
    jeton = saisi or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if action == "tester":
        if not jeton:
            return None, "Colle d'abord le jeton."
        ok, texte = reglages.tester_claude(jeton)
        return (texte, None) if ok else (None, texte)
    if not saisi:
        return ("Jeton gardé tel quel." if jeton else None), (None if jeton else "Colle le jeton, ou choisis « Sans Claude ».")
    if not saisi.startswith("sk-ant-"):
        return None, "Ce n'est pas un jeton Claude (il commence par sk-ant-) : refais claude setup-token."
    reglages.ecrire("CLAUDE_CODE_OAUTH_TOKEN", saisi, moi()["id"])
    _noter_reglage(request, "jeton Claude")
    return "Jeton Claude enregistré (chiffré). « Tester » vérifie qu'il marche.", None


def _etape_cloudflare(request: Request, form: FormData) -> tuple[str | None, str | None]:
    pays, erreur = installation.pays(form.get("pays"))
    if erreur:
        return None, erreur
    compte_id = moi()["id"]
    saisi = (form.get("tunnel_token") or "").strip()
    actif = form.get("tunnel") == "1"
    if actif and not (saisi or os.environ.get("CLOUDFLARE_TUNNEL_TOKEN")):
        return None, "Pour allumer le tunnel, colle son jeton (Zero Trust → Réseaux → Tunnels)."
    if saisi:
        reglages.ecrire("CLOUDFLARE_TUNNEL_TOKEN", saisi, compte_id)
    reglages.ecrire("CLAUNOTE_PAYS", pays, compte_id)
    suite = ""
    if saisi or actif != maj.tunnel_actif():
        suite = " " + maj.tunnel(actif)
        _noter_reglage(request, "tunnel Cloudflare " + ("allumé" if actif else "éteint"))
    return "Accès par Internet enregistré." + suite, None


@app.post("/installation/{etape}", response_class=HTMLResponse)
async def installation_enregistrer(request: Request, etape: str):
    if etape not in installation.NOMS:
        raise HTTPException(404)
    form = await request.form()
    action, depuis_reglages = form.get("action") or "enregistrer", form.get("retour") == "reglages"
    if action == "quitter":
        installation.terminer()
        return RedirectResponse("/", status_code=303)
    message = erreur = None
    plus: dict = {}
    if action != "passer":
        try:
            if etape == "compte":
                message, erreur = await run_in_threadpool(_etape_compte, request, form)
            elif etape == "stockage":
                message, erreur, plus = await run_in_threadpool(_etape_stockage, request, form, action)
            elif etape == "claude":
                message, erreur = await run_in_threadpool(_etape_claude, request, form, action)
            elif etape == "cloudflare":
                message, erreur = await run_in_threadpool(_etape_cloudflare, request, form)
            elif etape == "fin":
                installation.terminer()
                return RedirectResponse("/", status_code=303)
        except reglages.CleIndisponible as e:
            erreur = str(e)
        _appliquer_reglages()
    reponse_page = (lambda statut=200: _page_installation(request, None if depuis_reglages else etape, statut,
                                                          message=message, erreur=erreur, section=etape, **plus))
    if erreur:
        return reponse_page(400)
    if depuis_reglages or action in ("tester", "creer_dossiers", "sans_claude"):
        r = reponse_page()
    else:
        r = RedirectResponse(f"/installation/{installation.suivante(etape)}", status_code=303)
    if etape == "compte" and action != "passer":
        _cookie_session(request, r, moi())   # nouveau mot de passe : nouvelle signature de session pour cet appareil
    return r


# --- Comptes : invitations, droits, données de chacun (voir comptes.py) --------------------

def _page_comptes(request: Request, statut: int = 200, **plus):
    return templates.TemplateResponse(request, "comptes.html", {
        "comptes": db.comptes(), "invitations": db.invitations_recentes(15), "droits": comptes.DROITS,
        "droits_invite": comptes.DROITS_INVITE, "roles": comptes.ROLES, "duree_min": comptes.DUREE_JETON // 60,
        "maintenant": dt.datetime.now(dt.timezone.utc), "disque": disque.etat(), **plus}, status_code=statut)


def _lien_jeton(request: Request, jeton: str) -> str:
    return f"{_origine(request)}/invitation?{urlencode({'jeton': jeton})}"


@app.get("/comptes", response_class=HTMLResponse)
def page_comptes(request: Request, message: str = ""):
    return _page_comptes(request, message=message)


@app.post("/comptes/inviter", response_class=HTMLResponse)
def inviter(request: Request, role: str = Form("invite"), droits: list[str] = Form([]), note: str = Form("")):
    jeton, fin = comptes.inviter(moi()["id"], role, droits, note)
    securite.noter("invitation", request, f"jeton créé ({comptes.ROLES.get(role, role)}, valable "
                   f"{comptes.DUREE_JETON // 60} min){' : ' + note.strip()[:60] if note.strip() else ''}")
    return _page_comptes(request, jeton={"valeur": jeton, "lien": _lien_jeton(request, jeton), "fin": fin,
                                         "genre": "compte"})


def _cible(compte_id: int) -> dict:
    compte = db.compte(compte_id)
    if not compte:
        raise HTTPException(404, "compte inconnu")
    return compte


def _retour_comptes(message: str) -> RedirectResponse:
    return RedirectResponse("/comptes?" + urlencode({"message": message}), status_code=303)


@app.post("/comptes/{compte_id}/modifier", response_class=HTMLResponse)
def modifier_compte(request: Request, compte_id: int, role: str = Form("invite"), droits: list[str] = Form([]),
                    actif: str = Form(""), expire_le: str = Form(""), nom: str = Form("")):
    cible = _cible(compte_id)
    role = role if role in comptes.ROLES else "invite"
    actif_ = actif == "1"
    perd_admin = cible["role"] == "admin" and (role != "admin" or not actif_)
    if perd_admin and (compte_id == moi()["id"] or db.nb_admins_actifs(sauf=compte_id) == 0):
        return _page_comptes(request, 400, erreur="Il faut toujours au moins un admin actif, et tu ne peux pas "
                                                  "te retirer tes propres droits d'admin.")
    if compte_id == 1 and role != "admin":
        return _page_comptes(request, 400, erreur="Le compte d'origine reste admin.")
    try:
        fin = dt.datetime.combine(dt.date.fromisoformat(expire_le), dt.time(23, 59), pronote.PARIS) if expire_le else None
    except ValueError:
        return _page_comptes(request, 400, erreur="Date de fin d'accès illisible.")
    droits_ = [d for d in droits if d in comptes.DROITS]
    db.maj_compte(compte_id, role=role, droits=droits_, actif=actif_, expire_le=fin, nom=nom.strip()[:60] or None,
                  **({"tout_voir": False} if role != "admin" else {}))
    if not actif_:
        db.deconnecter_compte(compte_id)
    comptes.oublier(compte_id)
    securite.noter("compte_modifie", request, f"« {cible['identifiant']} » : {comptes.ROLES[role]}, "
                   f"{'actif' if actif_ else 'désactivé'}, droits {', '.join(droits_) or 'aucun'}"
                   + (f", jusqu'au {expire_le}" if expire_le else ""))
    return _retour_comptes(f"Compte « {cible['identifiant']} » enregistré.")


@app.post("/comptes/{compte_id}/deconnecter")
def deconnecter_compte(request: Request, compte_id: int):
    cible = _cible(compte_id)
    db.deconnecter_compte(compte_id)
    comptes.oublier(compte_id)
    securite.noter("compte_deconnecte", request, f"« {cible['identifiant']} » déconnecté partout")
    if compte_id == moi()["id"]:
        return RedirectResponse("/connexion", status_code=303)
    return _retour_comptes(f"« {cible['identifiant']} » est déconnecté de tous ses appareils.")


@app.post("/comptes/{compte_id}/mot-de-passe", response_class=HTMLResponse)
def lien_mot_de_passe(request: Request, compte_id: int):
    cible = _cible(compte_id)
    jeton, fin = comptes.lien_mot_de_passe(moi()["id"], compte_id)
    securite.noter("lien_mot_de_passe", request, f"pour « {cible['identifiant']} », valable {comptes.DUREE_JETON // 60} min")
    return _page_comptes(request, jeton={"valeur": jeton, "lien": _lien_jeton(request, jeton), "fin": fin,
                                         "genre": "mot_de_passe", "pour": cible["identifiant"]})


@app.post("/comptes/{compte_id}/supprimer", response_class=HTMLResponse)
def supprimer_compte(request: Request, compte_id: int, confirmation: str = Form("")):
    cible = _cible(compte_id)
    if compte_id in (1, moi()["id"]):
        return _page_comptes(request, 400, erreur="Ni ton propre compte ni le compte d'origine ne peuvent être supprimés.")
    if confirmation.strip().lower() != cible["identifiant"]:
        return _page_comptes(request, 400, erreur=f"Pour supprimer, tape exactement « {cible['identifiant']} ».")
    if cible["role"] == "admin" and db.nb_admins_actifs(sauf=compte_id) == 0:
        return _page_comptes(request, 400, erreur="Il faut toujours au moins un admin actif.")
    db.supprimer_compte(compte_id)
    comptes.oublier(compte_id)
    securite.noter("compte_supprime", request, f"« {cible['identifiant']} » et ses données", alerte=(
        f"Le compte « {cible['identifiant']} » et toutes ses données ont été supprimés."))
    return _retour_comptes(f"Compte « {cible['identifiant']} » supprimé, avec ses données sur le site.")


@app.post("/comptes/invitations/{invitation_id}/annuler")
def annuler_invitation(request: Request, invitation_id: int):
    db.annuler_invitation(invitation_id)
    securite.noter("invitation_annulee", request, f"jeton n° {invitation_id}")
    return _retour_comptes("Jeton annulé : il ne marche plus.")


@app.post("/comptes/tout-voir")
def tout_voir(request: Request, actif: str = Form(""), suite: str = Form("/")):
    """Admin : voir et utiliser les données de tous les comptes (bandeau visible tant que c'est actif)."""
    actif_ = actif == "1"
    db.maj_compte(moi()["id"], tout_voir=actif_)
    comptes.oublier(moi()["id"])
    securite.noter("tout_voir", request, "activé : données de tous les comptes" if actif_ else "désactivé")
    return RedirectResponse(_suite_sure(suite), status_code=303)


# --- Invitation : créer son compte avec le jeton de l'admin (page publique) ---------------

def _page_invitation(request: Request, jeton: str = "", statut: int = 200, **plus):
    inv = comptes.invitation(jeton) if jeton else None
    cible = db.compte(inv["compte_id"]) if inv and inv.get("compte_id") else None
    return templates.TemplateResponse(request, "invitation.html", {
        "jeton": comptes.normaliser_jeton(jeton) if inv else "", "inv": inv, "cible": cible, "sans_nav": True,
        "mdp_min": comptes.MDP_MIN, "version": comptes.CONDITIONS_VERSION, **plus}, status_code=statut)


@app.get("/invitation", response_class=HTMLResponse)
def page_invitation(request: Request, jeton: str = ""):
    erreur = "Jeton inconnu, déjà utilisé ou expiré : demande-en un nouveau à l'admin." \
        if jeton and not comptes.invitation(jeton) else None
    return _page_invitation(request, jeton, erreur=erreur)


@app.post("/invitation", response_class=HTMLResponse)
def utiliser_invitation(request: Request, jeton: str = Form(...), identifiant: str = Form(""), nom: str = Form(""),
                        mot_de_passe: str = Form(""), confirmation: str = Form(""), accepte: str = Form("")):
    limiteur = request.app.state.limiteur
    ip = appareils.ip_client(request)
    attente = limiteur.bloque(ip) or _verrou_internet(request)
    if attente:
        return _page_invitation(request, "", 429, erreur=f"Trop d'essais : réessaie dans {attente // 60 + 1} min.")
    inv = comptes.invitation(jeton)
    if not inv:
        limiteur.echec(ip)
        securite.echec("invitation_echec", request, "jeton inconnu ou expiré")
        return _page_invitation(request, "", 403, erreur="Jeton inconnu, déjà utilisé ou expiré : demande-en un "
                                                         "nouveau à l'admin.")
    if inv["genre"] == "mot_de_passe":
        cible = db.compte(inv["compte_id"])
        erreur = comptes.verifier_mot_de_passe(mot_de_passe, confirmation, (cible or {}).get("identifiant", ""))
        if erreur:
            return _page_invitation(request, jeton, 400, erreur=erreur)
        if not comptes.utiliser_lien_mot_de_passe(inv, mot_de_passe):
            return _page_invitation(request, "", 403, erreur="Ce lien vient d'être utilisé ou a expiré.")
        securite.noter("mot_de_passe", request, f"nouveau mot de passe de « {cible['identifiant']} » (lien de l'admin)")
        r = RedirectResponse("/", status_code=303)
        _cookie_session(request, r, cible)
        return r
    identifiant = identifiant.strip().lower()
    erreur = (comptes.verifier_identifiant(identifiant)
              or comptes.verifier_mot_de_passe(mot_de_passe, confirmation, identifiant)
              or (None if accepte == "1" else "Il faut lire et accepter les conditions d'utilisation pour créer ton compte."))
    if erreur:
        return _page_invitation(request, jeton, 400, erreur=erreur, identifiant=identifiant, nom=nom)
    compte_id = comptes.utiliser_invitation(inv, identifiant, mot_de_passe, nom)
    if not compte_id:
        return _page_invitation(request, "", 409, erreur="Ce jeton vient d'être utilisé, ou l'identifiant est pris.")
    limiteur.reussite(ip)
    securite.noter("compte_cree", request, f"« {identifiant} » ({comptes.ROLES.get(inv['role'])}), conditions "
                   f"{comptes.CONDITIONS_VERSION} acceptées", alerte=f"Nouveau compte « {identifiant} » créé avec "
                   "une invitation. Si ce n'est pas prévu : Réglages → Comptes → Supprimer.")
    r = RedirectResponse("/", status_code=303)
    _cookie_session(request, r, db.compte(compte_id))
    return r


# --- Conditions d'utilisation, mentions légales, « Tes données » (pages publiques) -------

@app.get("/conditions", response_class=HTMLResponse)
def page_conditions(request: Request):
    compte = getattr(request.state, "compte", None)
    return templates.TemplateResponse(request, "conditions.html", {
        "version": comptes.CONDITIONS_VERSION, "a_accepter": comptes.doit_accepter(compte), "compte": compte,
        "sans_nav": comptes.doit_accepter(compte) or not compte})


@app.post("/conditions")
def accepter_conditions(request: Request, accepte: str = Form("")):
    compte = getattr(request.state, "compte", None)
    if not compte:
        return RedirectResponse("/connexion", status_code=303)
    if accepte != "1":
        return RedirectResponse("/conditions", status_code=303)
    db.maj_compte(compte["id"], conditions_version=comptes.CONDITIONS_VERSION,
                  conditions_le=dt.datetime.now(dt.timezone.utc))
    comptes.oublier(compte["id"])
    securite.noter("conditions", request, f"« {compte['identifiant']} » accepte la version {comptes.CONDITIONS_VERSION}")
    return RedirectResponse("/", status_code=303)


@app.get("/tes-donnees", response_class=HTMLResponse)
def page_tes_donnees(request: Request):
    compte = getattr(request.state, "compte", None)
    return templates.TemplateResponse(request, "tes_donnees.html", {
        "compte": compte, "sans_nav": not compte or comptes.doit_accepter(compte), "droits": comptes.DROITS,
        "version": comptes.CONDITIONS_VERSION})


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
            securite.noter("mise_a_jour", request, action)
        except RuntimeError as e:
            erreur = str(e)
    return templates.TemplateResponse(request, "_mise_a_jour.html", _contexte_maj(request, erreur=erreur))


@app.post("/deconnexion")
def deconnexion():
    r = RedirectResponse("/connexion", status_code=303)
    r.delete_cookie(auth.COOKIE)
    return r


# --- Sécurité : journal, blocages, déconnecter partout (voir securite.py) ---------

GRAVITES = {"alerte": "Alertes", "attention": "À surveiller", "info": "Informations"}
PAR_PAGE_SECURITE = 50


@app.get("/securite", response_class=HTMLResponse)
def page_securite(request: Request, gravite: str = "", page: int = 1, message: str = ""):
    gravite = gravite if gravite in GRAVITES else ""
    page = max(page, 1)
    lignes, total = db.securite_journal(gravite or None, PAR_PAGE_SECURITE, (page - 1) * PAR_PAGE_SECURITE)
    securite.marquer_vu()
    return templates.TemplateResponse(request, "securite.html", {
        "lignes": lignes, "total": total, "page": page, "pages": max(1, -(-total // PAR_PAGE_SECURITE)),
        "gravite": gravite, "gravites": GRAVITES, "libelles": {k: v[1] for k, v in securite.TYPES.items()},
        "bilan": db.securite_bilan(), "e": securite.etat(), "message": message,
        "alertes_actives": notifications.preferences().get("securite", True), "abonnes": len(db.abonnements()),
        "appareils": appareils.liste(), "par_tunnel": appareils.par_tunnel(request.headers),
        "lien_page": lambda n: "/securite?" + urlencode({k: v for k, v in (("gravite", gravite), ("page", n)) if v})})


@app.post("/securite/debloquer")
def securite_debloquer(request: Request):
    securite.debloquer(request)
    return RedirectResponse("/securite?" + urlencode({"message": "Bans et blocages levés."}), status_code=303)


@app.post("/securite/alertes")
def securite_alertes(actives: str = Form("")):
    notifications.enregistrer_preferences({**notifications.preferences(), "securite": actives == "1"})
    return RedirectResponse("/securite", status_code=303)


@app.post("/securite/tester")
def securite_tester(request: Request):
    securite.alerter("test", "Test d'alerte", "Les alertes de sécurité arrivent bien sur cet appareil.")
    return RedirectResponse("/securite?" + urlencode({"message": "Alerte de test envoyée."}), status_code=303)


@app.post("/securite/deconnecter-partout")
def deconnecter_partout(request: Request, appareils_aussi: str = Form("")):
    """Nouvelle clé de signature : toutes les sessions tombent, sauf celle de cet appareil (recréée)."""
    db.etat_ecrire("session_generation", base64.urlsafe_b64encode(os.urandom(12)).decode())
    compte = moi()
    retires = 0
    if appareils_aussi == "1":
        ici = appareils.reconnu(request.cookies.get(appareils.COOKIE))
        for a in appareils.liste():
            if not ici or a["id"] != ici["id"]:
                appareils.retirer(a["id"])
                retires += 1
    detail = "toutes les sessions coupées" + (f", {retires} appareil(s) Internet retiré(s)" if retires else "")
    securite.noter("deconnexion_partout", request, detail, alerte=detail.capitalize() + ".")
    r = RedirectResponse("/securite?" + urlencode({"message": detail.capitalize() + " (sauf cet appareil)."}),
                         status_code=303)
    _cookie_session(request, r, compte)
    return r


# --- Bibliothèque ----------------------------------------------------------

def _premiere_page(compte: dict | None) -> str:
    """Page d'arrivée (plus de page « Aujourd'hui ») : Actualités, sinon la première rubrique permise."""
    for url, droit in (("/actualites", "actualites"), ("/bibliotheque", "bibliotheque"), ("/reviser", "reviser"),
                       ("/agenda", "agenda")):
        if comptes.peut(compte, droit):
            return url
    return "/reglages"


@app.get("/")
def accueil(request: Request):
    return RedirectResponse(_premiere_page(getattr(request.state, "compte", None)), status_code=303)


def _nb_cours_total() -> int:
    try:
        return db.nb_documents()
    except Exception:
        return 0


templates.env.globals["nb_cours_total"] = _nb_cours_total


# --- Réglages : une seule page qui mène à tout -----------------------------------

def _sans_erreur(f, defaut=None):
    try:
        return f()
    except Exception:
        return defaut


@app.get("/reglages", response_class=HTMLResponse)
def page_reglages(request: Request):
    """Point d'entrée unique : « Moi » pour tout le monde, « Administration » pour l'admin, avec l'état de chaque page."""
    moi_ = getattr(request.state, "compte", None) or {}
    admin = comptes.est_admin(moi_)
    ctx = {"abonnes": len(_sans_erreur(db.abonnements, []) or []), "admin": admin}
    if admin:
        ctx.update({
            "nb_comptes": len(_sans_erreur(db.comptes, []) or []),
            "alertes": _sans_erreur(securite.nb_nouvelles_alertes, 0),
            "maj_n": _sans_erreur(maj.disponibles, 0),
            "pause": _sans_erreur(lambda: db.etat_valeur("analyses_pause") == "oui", False),
            "disque": _sans_erreur(disque.etat),
            "inst_a_faire": _sans_erreur(installation.a_faire, False),
        })
    return templates.TemplateResponse(request, "reglages.html", ctx)


@app.get("/bibliotheque", response_class=HTMLResponse)
def bibliotheque(request: Request):
    arbre = defaultdict(lambda: defaultdict(list))
    for r in db.bibliotheque():
        arbre[r["niveau"]][r["matiere"]].append(r)
    niveaux = [n for n in reversed(NIVEAUX) if n in arbre]  # le plus récent (BTS2) en haut, le collège en bas
    # « Tout voir » mélange les comptes : les boutons Public / Privé des matières restent sur ses propres données
    partage = db.plusieurs_comptes() and not (moi() or {}).get("tout_voir")
    return templates.TemplateResponse(request, "bibliotheque.html", {
        "arbre": arbre, "niveaux": niveaux, "nb_hors_cours": db.nb_hors_cours(),
        "partage": partage, "publiques": db.matieres_publiques() if partage else set()})


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
        "niveaux": NIVEAUX, "types": TYPES, "matieres": db.matieres(),
        "matiere_publique": (doc["niveau"], doc["matiere"]) in db.matieres_publiques()
        if doc["statut"] == "classe" and db.plusieurs_comptes() else False})


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
            envoi = kd.upload_flux(dossiers_kdrive[dossiers], chemin[-1], f.file, taille)
        else:
            envoi = kd.upload(dossiers_kdrive[dossiers], chemin[-1], f.file.read(), conflict="rename")
        if isinstance(envoi, dict) and envoi.get("id"):   # le worker donnera le document à ce compte
            db.noter_depot(envoi["id"], moi()["id"])
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
    # Un SVG ouvert dans le navigateur peut contenir du JavaScript : téléchargé, jamais affiché
    lisible = media == "application/pdf" or (media.startswith("image/") and media != "image/svg+xml"
                                             and not nom.lower().endswith((".heic", ".heif")))
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
    rev_id = db.creer_revision(genre, sujet, ids, eval_le, consigne=consigne, strict=bool(f.get("strict")))
    if pieces:
        db.ajouter_pieces(pieces, revision_id=rev_id)
    db.demander_scan()
    return RedirectResponse(f"/revision/{rev_id}", status_code=303)


DESCRIPTIONS_GENRES = {"fiche": "synthèse dense + questions", "quiz": "10 questions à choix", "cartes": "20 cartes à mémoriser",
                       "examen": "sujet type examen + corrigé", "plan": "jour par jour jusqu'au contrôle"}


@app.post("/reviser/demande", response_class=HTMLResponse)
def lire_demande(request: Request, texte: str = Form("")):
    """Réviser en une phrase : on montre ce qu'on a compris, avec une question là où ce n'est pas sûr."""
    if not texte.strip():
        return RedirectResponse("/reviser", status_code=303)
    choix = db.niveaux_matieres_comptes()
    aujourdhui = dt.datetime.now(pronote.PARIS).date()
    compris = demande.comprendre(texte[:1000], choix, db.evenements_a_venir("evaluation", 30), aujourdhui,
                                 chercher=db.recherche)
    cochees = {f"{m['niveau']}|{m['matiere']}" for m in compris["matieres"]}
    niveaux = [n for n in reversed(NIVEAUX) if any(c["niveau"] == n for c in choix)]  # l'année en cours d'abord
    # Devant : ce qui est coché, les matières possibles d'après les cours, et sans piste toute l'année en cours
    devant = [c for c in choix if f"{c['niveau']}|{c['matiere']}" in cochees]
    if compris["incertain"]["matieres"]:
        for nom in compris["candidates"]:
            annees = [c for c in choix if c["matiere"] == nom and c["niveau"] in NIVEAUX]
            if annees:
                devant.append(max(annees, key=lambda c: NIVEAUX.index(c["niveau"])))
        if not devant and niveaux:
            devant = [c for c in choix if c["niveau"] == niveaux[0]]
    cles_devant = {f"{c['niveau']}|{c['matiere']}" for c in devant}
    autres = [(n, [c for c in choix if c["niveau"] == n and f"{n}|{c['matiere']}" not in cles_devant]) for n in niveaux]
    gabarit = "_demande.html" if request.headers.get("HX-Request") else "reviser_demande.html"
    return templates.TemplateResponse(request, gabarit, {
        "compris": compris, "phrase": demande.phrase(compris), "genres": revision.COURTS,
        "descriptions": DESCRIPTIONS_GENRES, "cochees": cochees, "devant": devant,
        "autres": [(n, liste) for n, liste in autres if liste], "aucun_cours": not choix,
        "demain": aujourdhui + dt.timedelta(days=1)})


@app.post("/reviser/lancer")
async def lancer_demande(request: Request):
    """Crée ce qui a été compris (et corrigé à l'écran) : une révision par support, ou une campagne
    dès qu'il y a plusieurs matières."""
    f = await request.form()
    texte, sujet = (f.get("texte") or "").strip()[:1000], (f.get("sujet") or "").strip()
    genres = [g for g in revision.GENRES if g in f.getlist("genres")]
    choisies = [m for m in f.getlist("matieres") if "|" in m]
    if not genres or not choisies:
        raise HTTPException(400, "choisis au moins une matière et un support")
    try:
        eval_le = dt.date.fromisoformat(f.get("date_eval") or "") if f.get("date_eval") else None
    except ValueError:
        raise HTTPException(400, "date invalide")
    aujourdhui = dt.datetime.now(pronote.PARIS).date()
    if "plan" in genres and (eval_le is None or eval_le <= aujourdhui):
        raise HTTPException(400, "un plan de révision demande une date d'évaluation future")
    consigne = f"Demande de l'élève, dans ses mots : « {texte} »" if texte else ""
    choisies.sort(key=lambda nm: -_rang_niveau(nm.partition("|")[0]))  # l'année la plus récente d'abord
    concepts = {matieres.concept(nm.partition("|")[2]) for nm in choisies}
    if len(concepts) > 1:
        # Plusieurs matières : une campagne (un jeu de supports par matière + un programme jour par jour)
        fin = eval_le if eval_le and eval_le > aujourdhui else aujourdhui + dt.timedelta(days=7)
        try:
            fin = max(fin, dt.date.fromisoformat(f.get("fin") or ""))
        except ValueError:
            pass
        champs = [("titre", (f.get("titre") or "").strip() or f"Révisions du {aujourdhui:%d/%m}"), ("consigne", consigne),
                  ("debut", aujourdhui.isoformat()), ("fin", min(fin, aujourdhui + dt.timedelta(days=60)).isoformat())]
        champs += [("genres", g) for g in genres if g != "plan"] or [("genres", "fiche")]
        champs += [("matieres", nm) for nm in choisies] + [(f"sujet:{nm}", sujet) for nm in choisies]
        return RedirectResponse(f"/campagne/{_creer_campagne(FormData(champs), [])}", status_code=303)
    ids, niveaux = [], []
    for nm in choisies:
        niveau, _, matiere = nm.partition("|")
        trouves = db.choisir_documents(niveau, matiere, sujet) or db.choisir_documents(niveau, matiere)
        ids += [i for i in trouves if i not in ids]
        niveaux.append(niveau)
    if not ids:
        raise HTTPException(400, "aucun cours classé pour ce choix")
    matiere = choisies[0].partition("|")[2]
    if len(niveaux) > 1:
        consigne += (f"\n\nCe support couvre {matiere} sur plusieurs années ({', '.join(reversed(niveaux))}) : "
                     "reprends les notions de chacune de ces années, pas seulement la plus récente.")
    libelle = f"{matiere} : {sujet}" if sujet else f"{matiere} ({', '.join(niveaux)})"
    crees = [db.creer_revision(g, libelle, ids, eval_le, consigne=consigne.strip(), strict=bool(f.get("strict")))
             for g in genres]
    db.demander_scan()
    return RedirectResponse(f"/revision/{crees[0]}" if len(crees) == 1 else "/reviser#deja", status_code=303)


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
    if rev["statut"] == "pret" or (rev.get("retouche") and rev.get("contenu")):  # modification en cours : l'actuel reste lisible
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
        "web": {w["repere"]: w for w in web},
        # Ajouts de l'IA hors des cours ([IA] dans le texte, "source": "IA" dans un quiz ou des flashcards)
        "ajout_ia": rev["statut"] == "pret" and bool(re.search(r'\[IA\]|"source": "IA"', rev["contenu"] or "")),
        "sans_texte": revision.sans_texte(sources)})


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


@app.post("/revision/{rev_id}/renommer")
async def renommer_revision(request: Request, rev_id: int):
    if db.revision(rev_id) is None:
        raise HTTPException(404)
    sujet = re.sub(r"\s+", " ", str((await request.form()).get("sujet") or "")).strip()[:120]
    if sujet:
        db.renommer_revision(rev_id, sujet)
    return RedirectResponse(f"/revision/{rev_id}", status_code=303)


@app.post("/revision/{rev_id}/modifier")
async def modifier_revision(request: Request, rev_id: int):
    """« Ajoute un exemple sur… », « plus court », « corrige la partie 3 » : Claunote refait le support en tenant
    compte de la demande (même cours, même format) ; l'actuel reste affiché pendant ce temps."""
    rev = db.revision(rev_id)
    if rev is None:
        raise HTTPException(404)
    if rev["genre"] not in revision.GENRES_TEXTE or rev["statut"] != "pret":
        raise HTTPException(400, "ce support ne peut pas être modifié maintenant")
    demande = str((await request.form()).get("demande") or "").strip()[:2000]
    if demande:
        db.demander_retouche(rev_id, demande)
        db.demander_scan()
    return RedirectResponse(f"/revision/{rev_id}", status_code=303)


@app.post("/revision/{rev_id}/annuler-modification")
def annuler_modification(rev_id: int):
    rev = db.revision(rev_id)
    if rev is None:
        raise HTTPException(404)
    if rev.get("retouche") and rev["statut"] == "en_attente":
        db.finir_retouche(rev_id)
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
    return RedirectResponse(f"/campagne/{_creer_campagne(f, await _pieces(f))}", status_code=303)


def _creer_campagne(f, pieces: list[dict]) -> int:
    """Crée la campagne décrite par le formulaire `f` (page Campagne, ou demande en une phrase)."""
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
    return campagne_id


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
def campagne_pdf(campagne_id: int, corrige: int = 1, telecharger: int = 0, programme: int = 0):
    """Livret : tous les supports prêts (fiches, questions…) ; le programme jour par jour seulement sur demande."""
    campagne = db.campagne(campagne_id)
    if campagne is None:
        raise HTTPException(404)
    revs = [db.revision(r["id"]) for r in db.revisions_campagne(campagne_id) if r["statut"] == "pret"]
    reperes = {}
    for r in revs:
        reperes.update({f"{r['id']}:{k}": v for k, v in (r["reperes"] or {}).items()})
    contenu = pdf.campagne_pdf(campagne, revs, _sources(reperes), corrige=bool(corrige), programme=bool(programme))
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
        # Le fil garde le compte de la requête : sa recherche ne voit que ses cours, et son droit au forfait
        threading.Thread(target=contextvars.copy_context().run, args=(_repondre_ia, msg_id, q, conv, historique),
                         daemon=True, name=f"demander-{msg_id}").start()
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
                         alias="pronote"), revisions: bool = Form(False),
                         actualites_: bool = Form(False, alias="actualites")):
    for h in (heure, silence_debut, silence_fin):
        try:
            dt.time.fromisoformat(h)
        except ValueError:
            raise HTTPException(400, "heure invalide")
    notifications.enregistrer_preferences({**notifications.preferences(), "heure": heure, "silence_debut": silence_debut,
                                           "silence_fin": silence_fin, "rappel": rappel,
                                           "pronote": pronote_, "revisions": revisions,
                                           "actualites": actualites_})
    return RedirectResponse("/notifications?message=Réglages+enregistrés", status_code=303)


# --- Actualités (flux RSS, sujets suivis) ----------------------------------------

VUES_ACTU = {"pour_toi": "Pour toi", "tout": "Tout", "favoris": "Favoris"}
PAR_PAGE_ACTU = 30


def _jour_actu(d: dt.datetime, aujourdhui: dt.date) -> str:
    jour = d.astimezone(pronote.PARIS).date()
    ecart = (aujourdhui - jour).days
    if ecart <= 0:
        return "Aujourd'hui"
    if ecart == 1:
        return "Hier"
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
            "novembre", "décembre"]
    jours = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    return f"{jours[jour.weekday()]} {jour.day} {mois[jour.month - 1]}"


@app.get("/actualites", response_class=HTMLResponse)
def page_actualites(request: Request, vue: str = "pour_toi", sujet: int | None = None, flux: int | None = None,
                    page: int = 1, article: int | None = None):
    vue = vue if vue in VUES_ACTU else "pour_toi"
    if sujet or flux:
        vue = "tout"
    page = max(page, 1)
    liste, total = db.articles(vue, sujet, flux, PAR_PAGE_ACTU, (page - 1) * PAR_PAGE_ACTU)
    aujourdhui = dt.datetime.now(pronote.PARIS).date()
    groupes: list[tuple[str, list]] = []
    for a in liste:
        jour = _jour_actu(a["publie_le"], aujourdhui)
        if not groupes or groupes[-1][0] != jour:
            groupes.append((jour, []))
        groupes[-1][1].append(a)
    sujets = db.sujets()
    flux_l = db.flux_liste()

    def lien_page(n: int) -> str:
        return "/actualites?" + urlencode({k: v for k, v in {"vue": vue, "sujet": sujet, "flux": flux,
                                                              "page": n}.items() if v})
    return templates.TemplateResponse(request, "actualites.html", {
        "vue": vue, "vues": VUES_ACTU, "sujet": sujet, "flux": flux, "groupes": groupes, "total": total,
        "page": page, "pages": -(-total // PAR_PAGE_ACTU), "par": PAR_PAGE_ACTU, "lien_page": lien_page,
        "sujets": sujets, "flux_liste": flux_l, "article": article, "paris": pronote.PARIS,
        "sujet_nom": next((s["nom"] for s in sujets if s["id"] == sujet), None),
        "flux_nom": next((f["titre"] or f["url"] for f in flux_l if f["id"] == flux), None),
        "en_attente": sum(1 for f in flux_l if f["releve_le"] is None),
        "reglages": actualites.reglages()})


@app.post("/actualites/{article_id}/favori", response_class=HTMLResponse)
def actualite_favori(article_id: int):
    a = db.article(article_id)
    if not a:
        raise HTTPException(404)
    db.article_maj(article_id, favori=not a["favori"])
    return templates.get_template("_actualite_favori.html").render(a={**a, "favori": not a["favori"]})


@app.post("/actualites/{article_id}/lu")
def actualite_lue(article_id: int):
    db.article_maj(article_id, lu=True)
    return {"ok": True}


@app.post("/actualites/tout-lu")
def actualites_tout_lu(vue: str = Form("pour_toi")):
    db.articles_tout_lu(vue)
    return RedirectResponse(f"/actualites?vue={quote(vue)}", status_code=303)


@app.post("/actualites/relever", response_class=HTMLResponse)
def actualites_relever():
    db.etat_ecrire("actualites_relever", "oui")
    return "C'est demandé : les nouveaux articles arrivent d'ici une minute."


def _page_sources(request: Request, message: str = "", erreur: str = "", saisie: dict | None = None):
    return templates.TemplateResponse(request, "actualites_sources.html", {
        "sujets": db.sujets(), "flux_liste": db.flux_liste(), "message": message, "erreur": erreur,
        "saisie": saisie or {}, "reglages": actualites.reglages(), "paris": pronote.PARIS,
        "notifs": notifications.preferences().get("actualites", True)})


@app.get("/actualites/sources", response_class=HTMLResponse)
def page_actualites_page_sources(request: Request, message: str = ""):
    return _page_sources(request, message)


@app.post("/actualites/flux", response_class=HTMLResponse)
def ajouter_flux(request: Request, url: str = Form(...), notifier: bool = Form(False)):
    try:
        adresse, infos, items = actualites.decouvrir(url)
    except actualites.FluxIllisible as e:
        return _page_sources(request, erreur=f"Flux non ajouté : {e}.", saisie={"url": url})
    flux_id = db.flux_ajouter(adresse, infos["titre"], infos["site"] or urlsplit(adresse).hostname or "", notifier=notifier)
    sujets = db.sujets()
    for i in items:  # déjà en ligne : rangés sans notification, triés par l'IA au prochain passage
        i["cle"] = actualites.cle_article(i["lien"], i["titre"], flux_id)
        i["sujets"] = actualites.par_mots_cles(sujets, i)
        if not sujets:
            i["tri"] = "mots_cles"
    n = db.articles_ajouter(flux_id, items, annonce=True)
    db.flux_maj(flux_id, releve_le=dt.datetime.now(dt.timezone.utc), erreur=None, echecs=0)
    return RedirectResponse("/actualites/sources?" + urlencode(
        {"message": f"« {infos['titre'] or adresse} » ajouté : {n} article{'s' if n > 1 else ''}."}), status_code=303)


@app.post("/actualites/flux/{flux_id}/notifier")
def flux_notifier(flux_id: int, notifier: bool = Form(False)):
    db.flux_maj(flux_id, notifier=notifier)
    return RedirectResponse("/actualites/sources#flux", status_code=303)


@app.post("/actualites/flux/{flux_id}/supprimer")
def supprimer_flux(flux_id: int):
    db.flux_supprimer(flux_id)
    return RedirectResponse("/actualites/sources?message=Flux+retiré#flux", status_code=303)


def _mots(texte: str) -> list[str]:
    vus, mots = set(), []
    for m in re.split(r"[,;\n]", texte or ""):
        m = m.strip()
        if m and m.lower() not in vus:
            vus.add(m.lower())
            mots.append(m[:60])
    return mots


def _recherche_sujet(sujet_id: int, recherche: bool):
    """Case « chercher aussi sur Google Actualités » : un flux de recherche lié au sujet."""
    sujet = next((s for s in db.sujets() if s["id"] == sujet_id), None)
    if not sujet:
        return
    if recherche and not sujet["recherche_id"]:
        db.flux_ajouter(actualites.recherche_google(sujet), f"Google Actualités : {sujet['nom']}",
                        "news.google.com", sujet_id=sujet_id)
    elif not recherche and sujet["recherche_id"]:
        db.flux_supprimer(sujet["recherche_id"])
    db.etat_ecrire("actualites_relever", "oui")


@app.post("/actualites/sujets", response_class=HTMLResponse)
def ajouter_sujet(request: Request, nom: str = Form(...), description: str = Form(""), mots_cles: str = Form(""),
                  notifier: bool = Form(False), recherche: bool = Form(False)):
    nom = nom.strip()[:80]
    if not nom:
        return _page_sources(request, erreur="Donne un nom au sujet.")
    sujet_id = db.sujet_ajouter(nom, description.strip()[:500], _mots(mots_cles), notifier)
    if sujet_id is None:
        return _page_sources(request, erreur=f"Le sujet « {nom} » existe déjà.",
                        saisie={"nom": nom, "description": description, "mots_cles": mots_cles})
    _recherche_sujet(sujet_id, recherche)
    db.articles_retrier()   # les articles de la semaine sont relus avec ce nouveau sujet (sans renotifier)
    return RedirectResponse("/actualites/sources?" + urlencode(
        {"message": f"Sujet « {nom} » ajouté : les articles de la semaine sont relus avec lui."}), status_code=303)


@app.post("/actualites/sujets/{sujet_id}")
def modifier_sujet(sujet_id: int, nom: str = Form(...), description: str = Form(""), mots_cles: str = Form(""),
                   notifier: bool = Form(False), recherche: bool = Form(False)):
    avant = next((s for s in db.sujets() if s["id"] == sujet_id), None)
    if not avant:
        raise HTTPException(404)
    champs = {"nom": nom.strip()[:80] or avant["nom"], "description": description.strip()[:500],
              "mots_cles": _mots(mots_cles), "notifier": notifier}
    db.sujet_maj(sujet_id, **champs)
    _recherche_sujet(sujet_id, recherche)
    if (champs["description"], champs["mots_cles"], champs["nom"]) != (avant["description"], avant["mots_cles"],
                                                                     avant["nom"]):
        db.articles_retrier()
    return RedirectResponse("/actualites/sources?message=Sujet+enregistré#sujets", status_code=303)


@app.post("/actualites/sujets/{sujet_id}/supprimer")
def supprimer_sujet(sujet_id: int):
    db.sujet_supprimer(sujet_id)
    return RedirectResponse("/actualites/sources?message=Sujet+retiré#sujets", status_code=303)


@app.post("/actualites/reglages")
def regler_actualites(ia: bool = Form(False), notifs: bool = Form(False)):
    actualites.enregistrer_reglages({"ia": ia})
    notifications.enregistrer_preferences({**notifications.preferences(), "actualites": notifs})
    return RedirectResponse("/actualites/sources?message=Réglages+enregistrés#reglages", status_code=303)


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
    params = {"sujet": ev["titre"] if ev["id"].startswith("devoir:") else "", "genre": "fiche",
              "date_eval": le.isoformat()}
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


# --- Public ou privé : chacun choisit pour ses cours, matières, révisions, campagnes -----------
# Privé par défaut. Public : les autres comptes le lisent sur « Partagés », en lecture seule. « Tout voir » de
# l'admin ne change pas (il voit tout, privé compris) ; seul le propriétaire choisit, même pour l'admin.

VALEURS_VISIBILITE = {"public": True, "prive": False, "matiere": None}


def _visibilite(table: str, element_id: int, valeur: str, suite: str) -> RedirectResponse:
    if valeur not in VALEURS_VISIBILITE or (valeur == "matiere" and table != "documents"):
        raise HTTPException(400)
    if not db.changer_visibilite(table, element_id, VALEURS_VISIBILITE[valeur]):
        raise HTTPException(404)   # pas à ce compte (ou n'existe pas)
    return RedirectResponse(_suite_sure(suite), status_code=303)


@app.post("/doc/{doc_id}/visibilite")
def visibilite_document(doc_id: int, valeur: str = Form(...)):
    return _visibilite("documents", doc_id, valeur, f"/doc/{doc_id}")


@app.post("/revision/{rev_id}/visibilite")
def visibilite_revision(rev_id: int, valeur: str = Form(...)):
    return _visibilite("revisions", rev_id, valeur, f"/revision/{rev_id}")


@app.post("/campagne/{campagne_id}/visibilite")
def visibilite_campagne(campagne_id: int, valeur: str = Form(...)):
    return _visibilite("campagnes", campagne_id, valeur, f"/campagne/{campagne_id}")


@app.post("/bibliotheque/visibilite")
def visibilite_matiere(niveau: str = Form(...), matiere: str = Form(...), valeur: str = Form(...)):
    if valeur not in ("public", "prive"):
        raise HTTPException(400)
    db.rendre_matiere(niveau, matiere, valeur == "public")
    return RedirectResponse("/bibliotheque", status_code=303)


@app.get("/partages", response_class=HTMLResponse)
def page_partages(request: Request):
    docs = db.partages_documents()
    par_auteur = defaultdict(lambda: defaultdict(list))
    for d in docs:
        par_auteur[d["auteur"]][(d["niveau"], d["matiere"])].append(d)
    return templates.TemplateResponse(request, "partages.html", {
        "par_auteur": par_auteur, "nb_docs": len(docs), "revisions": db.partages_revisions(),
        "campagnes": db.partages_campagnes(), "genres": revision.GENRES})


@app.get("/partages/doc/{doc_id}", response_class=HTMLResponse)
def partage_document(request: Request, doc_id: int):
    doc = db.document_partage(doc_id)
    if doc is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "partage_doc.html", {
        "doc": doc, "transcription": rendu_markdown(doc["transcription"])})


@app.get("/partages/doc/{doc_id}/fichier")
def partage_fichier(doc_id: int):
    doc = db.document_partage(doc_id)
    if doc is None:
        raise HTTPException(404)
    return _servir(doc["kdrive_id"], doc["nom"])


@app.get("/partages/revision/{rev_id}", response_class=HTMLResponse)
def partage_revision(request: Request, rev_id: int):
    rev = db.revision_partagee(rev_id)
    if rev is None:
        raise HTTPException(404)
    reperes = rev["reperes"] or {}
    sources = db.documents_partages(list(reperes.values()))   # seulement les cours publics eux aussi
    visibles = {r: i for r, i in reperes.items() if i in {d["id"] for d in sources}}
    web = rev.get("sources_web") or []
    contenu, questions, cartes = None, [], []
    if rev["genre"] == "quiz":
        questions = json.loads(rev["contenu"] or "[]")
    elif rev["genre"] == "cartes":
        cartes = db.cartes_partagees(rev_id)
    else:
        contenu = Markup(str(avec_sources(rendu_markdown(rev["contenu"]), visibles, web))
                         .replace('href="/doc/', 'href="/partages/doc/'))
    return templates.TemplateResponse(request, "partage_revision.html", {
        "rev": rev, "genres": revision.GENRES, "contenu": contenu, "questions": questions, "cartes": cartes,
        "sources": sources, "reperes": visibles})


@app.get("/partages/campagne/{campagne_id}", response_class=HTMLResponse)
def partage_campagne(request: Request, campagne_id: int):
    campagne, revs = db.campagne_partagee(campagne_id)
    if campagne is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "partage_campagne.html", {
        "campagne": campagne, "revs": revs, "genres": revision.GENRES})
