import base64
import contextvars
from contextlib import nullcontext
import hashlib
import datetime as dt
import html
import json
import mimetypes
import re
import os
import random
import tempfile
import unicodedata
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import markdown
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile as FichierRecu

from claunote import (actualites, agent, anki, apercu, apparence, appareils, archivage, auth, carte_mentale, cles, cloudflare, comptes, db, decoupage, demande, demandes_ia,
                      disque, emploi_du_temps, formations, formules, importation, index, installation, lecture, local, maj, matieres, modeles, notes, notifications, pdf, progression,
                      pronote, profil, question, raccourci, rangement, reglages, ressources, revision, schemas, securite, sensible,
                      stockage_local, turnstile, version)
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
                    "/tes-donnees", "/raccourci/envoi"})
# Connecté mais conditions d'utilisation pas encore acceptées (ou changées) : seulement ces pages
AVANT_CONDITIONS = frozenset({"/conditions", "/tes-donnees", "/deconnexion"})
_moi: contextvars.ContextVar[dict | None] = contextvars.ContextVar("moi", default=None)
# « Voir en tant que » en cours (apercu.py) : {"admin": l'admin, "compte": le compte regardé, "fin": instant}
_apercu: contextvars.ContextVar[dict | None] = contextvars.ContextVar("apercu", default=None)


def moi() -> dict | None:
    """Le compte connecté de la requête en cours (None sur une page publique)."""
    return _moi.get()


def _session(request: Request) -> dict | None:
    compte = auth.lire_session(_cle_sessions(request.app), request.cookies.get(auth.COOKIE), comptes.trouver)
    return compte if comptes.utilisable(compte) else None


def apercu_en_cours() -> dict | None:
    """L'aperçu « Voir en tant que » de la requête en cours, ou None."""
    return _apercu.get()


def _apercu_de(request: Request, admin: dict) -> dict | None:
    """Le compte que cet admin regarde (cookie « Voir en tant que » valable), ou None."""
    if not comptes.est_admin(admin):
        return None
    lu = apercu.lire(_cle_sessions(request.app), request.cookies.get(apercu.COOKIE), admin)
    cible = comptes.trouver(lu[0]) if lu else None
    if not cible:
        return None
    return {"admin": admin, "compte": cible, "fin": dt.datetime.fromtimestamp(lu[1], dt.timezone.utc)}


def _refus_apercu(request: Request, apercu_: dict) -> Response:
    """Pendant l'aperçu, rien n'est fait au nom du compte regardé."""
    nom = apercu_["compte"].get("nom") or apercu_["compte"]["identifiant"]
    texte = (f"Aperçu en lecture seule : rien n'est fait au nom de {nom}. "
             "Reviens à ton compte (bandeau en haut) pour agir.")
    if request.headers.get("HX-Request") or request.query_params.get("paquet"):
        return Response(texte, status_code=403)
    # Repli sans JavaScript (le bandeau bloque d'habitude le bouton avant) : une page au style claunote
    return HTMLResponse(_PAGE_REFUS_APERCU.replace("{texte}", html.escape(texte)), status_code=403)


_PAGE_REFUS_APERCU = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Lecture seule · Claunote</title><link rel="icon" href="/static/logo.svg" type="image/svg+xml">
<style>
  :root { --fond: #faf9f5; --carte: #fff; --texte: #1f1e1d; --doux: #6f6b63; --bord: #ebe7de; --accent: #d97757; color-scheme: light dark; }
  @media (prefers-color-scheme: dark) { :root { --fond: #1c1b1a; --carte: #262523; --texte: #eeebe4; --doux: #a7a299;
    --bord: #38362f; --accent: #e08a6d; } }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px; box-sizing: border-box;
    background: var(--fond); color: var(--texte); font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
  .carte { max-width: 440px; background: var(--carte); border: 2px solid var(--accent); border-radius: 18px; padding: 24px; text-align: center; }
  img { width: 56px; height: 56px; border-radius: 14px; border: 1px solid var(--bord); background: #fff; }
  h1 { font-size: 1.3rem; margin: 12px 0 6px; } p { color: var(--doux); margin: 0 0 18px; }
  a { display: inline-block; background: var(--accent); color: #fff; text-decoration: none; font-weight: 600; padding: 10px 18px; border-radius: 12px; }
</style></head><body><div class="carte"><img src="/static/logo.svg" alt="">
<h1>👁 Lecture seule</h1><p>{texte}</p><a href="javascript:history.back()">Retour</a></div></body></html>"""


PUBLIC_PREFIXES = ("/static/",)


def _public(chemin: str) -> bool:
    return chemin in PUBLIC or chemin.startswith(PUBLIC_PREFIXES)


async def _servir_compte(request: Request, call_next, compte: dict, droit: str | None):
    if comptes.peut(compte, droit):
        return await call_next(request)
    texte = f"Ton compte n'a pas le droit « {comptes.DROITS[droit][0]} »." if droit in comptes.DROITS \
        else "Réservé à l'admin."
    if request.method != "GET" and droit == "claude":
        texte = ("Ton compte n'a pas accès au forfait Claude de l'admin : les fiches, quiz et programmes demandent "
                 "Claude. Vois avec l'admin.")
    if request.headers.get("HX-Request") or request.method != "GET":
        return Response(texte, status_code=403)
    return templates.TemplateResponse(request, "refuse.html", {"droit": comptes.DROITS.get(droit)}, status_code=403)


@app.middleware("http")
async def exiger_connexion(request: Request, call_next):
    chemin = request.url.path
    compte = _session(request)
    vu = _apercu_de(request, compte) if compte else None
    if vu:   # « Voir en tant que » : la requête est servie comme pour ce compte, sans rien écrire
        if request.method not in METHODES_SURES and chemin not in apercu.SORTIES:
            return _refus_apercu(request, vu)
        compte = vu["compte"]
    if compte:
        request.state.compte = compte
        if vu:
            pass   # l'admin regarde : pas d'arrêt sur les conditions ni sur l'assistant d'installation
        elif comptes.doit_accepter(compte) and chemin not in AVANT_CONDITIONS and not chemin.startswith("/static/"):
            if request.method != "GET" or request.headers.get("HX-Request"):
                return Response("conditions d'utilisation à accepter", status_code=403,
                                headers={"HX-Redirect": "/conditions"})
            return RedirectResponse("/conditions", status_code=303)
        if chemin == "/" and comptes.est_admin(compte) and installation.a_faire():
            return RedirectResponse("/installation", status_code=303)   # installation neuve : l'assistant d'abord
        droit = comptes.droit_requis(chemin, request.method)
        if comptes.peut(compte, droit) and (comptes.besoin_claude(chemin, request.method) or (
                comptes.creation_ia(chemin, request.method) and comptes.niveau_forfait(compte) == "aucun")):
            droit = "claude"   # refusé : pas de forfait en autonomie (ou « Aucun » pour une fiche, un quiz…)
        jeton, jeton_apercu = _moi.set(compte), _apercu.set(vu)
        # La base ne montre que les données de ce compte (« Tout voir » : l'admin voit tout), refus compris
        try:
            with comptes.pour(compte["id"], comptes.tout_voir(compte, chemin)), \
                    (db.partages() if comptes.lit_les_partages(compte, chemin, request.method) else nullcontext()), \
                    (db.lecture_seule() if vu else nullcontext()):
                return await _servir_compte(request, call_next, compte, droit)
        finally:
            _moi.reset(jeton)
            _apercu.reset(jeton_apercu)
    if _public(chemin):
        return await call_next(request)
    if request.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": "/connexion"})
    if request.method != "GET":
        return Response("connexion requise", status_code=401)
    suite = chemin + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/connexion?{urlencode({'suite': suite})}", status_code=303)


def _appareil_valable(request: Request) -> dict | None:
    """Appareil approuvé, et son compte encore utilisable (désactivé, expiré, supprimé : l'appareil ne passe plus)."""
    a = appareils.reconnu(request.cookies.get(appareils.COOKIE))
    return a if a and comptes.utilisable(comptes.trouver(a["compte_id"])) else None


# Sans appareil approuvé, par Internet : créer son compte avec le lien de l'admin (le lien approuve l'appareil),
# et lire les conditions qu'il faut accepter pour ça. Le lien de nouveau mot de passe, lui, demande un appareil.
SANS_APPAREIL = frozenset({"/conditions", "/tes-donnees"})


def _passe_sans_appareil(request: Request) -> bool:
    chemin = request.url.path
    if chemin in SANS_APPAREIL:
        return request.method == "GET"
    if chemin == "/raccourci/envoi":
        return request.method == "POST"   # raccourci iOS : son jeton tient lieu d'appareil (vérifié par la route)
    if chemin != "/invitation":
        return False
    if request.method == "POST":
        return True   # jeton, captcha et blocage par adresse vérifiés par la page elle-même
    inv = comptes.invitation(request.query_params.get("jeton", ""))
    return bool(inv) and inv["genre"] == "compte"


# Déclaré après exiger_connexion, donc exécuté avant : par Internet, l'appareil passe avant le mot de passe
@app.middleware("http")
async def appareil_approuve(request: Request, call_next):
    """Par Cloudflare Tunnel, seuls les appareils approuvés atteignent le site (voir appareils.py)."""
    chemin = request.url.path
    if (not appareils.par_tunnel(request.headers) or not appareils.actif() or chemin == "/appareil"
            or chemin.startswith("/static/") or _appareil_valable(request) or _passe_sans_appareil(request)):
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
       "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; media-src 'self' blob: https:; "
       "frame-src 'self' blob:; worker-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'self'")
# Captcha Turnstile allumé : son script et son iframe viennent de challenges.cloudflare.com (et rien d'autre)
CSP_TURNSTILE = CSP.replace("script-src 'self'", f"script-src 'self' {turnstile.ORIGINE}").replace(
    "frame-src 'self'", f"frame-src 'self' {turnstile.ORIGINE}")
EN_TETES = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN",
            "Referrer-Policy": "same-origin", "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Permissions-Policy": "camera=(self), microphone=(), geolocation=(), payment=(), usb=(), "
                                  "interest-cohort=()",
            # « Cette réponse vient bien de Claunote » : le Worker Cloudflare de maintenance laisse passer un
            # 503 du site (kDrive en panne…) et ne remplace que les erreurs du tunnel (infra/maintenance)
            "X-Claunote-App": "1"}


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
    if turnstile.actif() and r.headers.get("content-security-policy") == CSP:
        r.headers["content-security-policy"] = CSP_TURNSTILE
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
templates.env.globals["nb_cartes_a_revoir"] = lambda: db.nb_cartes_a_revoir(nouvelles_max=profil.nouvelles_max((moi() or {}).get("id")))
templates.env.globals["installation_a_faire"] = installation.a_faire
templates.env.globals["turnstile_requis"] = turnstile.requis   # captcha par Internet (_turnstile.html)
templates.env.globals["turnstile_cle"] = turnstile.cle_site
templates.env.globals["mdp_par_defaut"] = lambda: bool((moi() or {}).get("mdp_defaut"))
templates.env.globals["moi"] = moi
templates.env.globals["voir_en_tant_que"] = apercu_en_cours   # « Voir en tant que » en cours (bandeau de base.html)
templates.env.globals["apparence"] = lambda: apparence.lire(moi())   # thème et style du compte (base.html)
templates.env.globals["rubriques"] = apparence.RUBRIQUES
templates.env.globals["mon_profil"] = lambda: profil.lire((moi() or {}).get("id"))   # préférences de révision
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
templates.env.globals["nb_demandes_ia"] = lambda: demandes_ia.nb_en_attente() if comptes.est_admin(moi()) else 0
# Page d'une révision ou d'une campagne : la demande d'un compte sans forfait attend-elle l'admin ?
templates.env.globals["attend_admin"] = lambda e: demandes_ia.attend_admin(e, comptes.peut_claude((e or {}).get("compte_id")))
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
templates.env.globals["appareil_par_defaut"] = appareils.par_defaut
templates.env.filters["paris"] = lambda d: d.astimezone(pronote.PARIS).strftime("%d/%m/%Y %H:%M") if d else ""
templates.env.filters["jour_iso"] = dt.date.fromisoformat
templates.env.filters["instant_iso"] = dt.datetime.fromisoformat
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
        if doc_id and int(doc_id) < 0:   # une note de l'élève (notes.comme_passage), pas un cours
            return f'<a href="/notes/{-int(doc_id)}" class="repere-note">[{m.group(1)}]</a>'
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
def connexion(request: Request, utilisateur: str = Form(...), mot_de_passe: str = Form(...), suite: str = Form("/"),
              captcha: str = Form("", alias=turnstile.CHAMP)):
    limiteur = request.app.state.limiteur
    ip = appareils.ip_client(request)
    attente = limiteur.bloque(ip) or _verrou_internet(request)
    refus_robot = None if attente else _captcha(request, captcha, "connexion")
    if refus_robot:
        return _page_connexion(request, suite, refus_robot, 403)
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


def _captcha(request: Request, jeton: str, action: str) -> str | None:
    """Captcha Turnstile (par Internet, clés saisies) : None si passé, sinon le message ; jamais par le VPN."""
    refus = turnstile.verifier(request, jeton, action)
    if refus:
        securite.noter("captcha", request, f"{action} : {refus}")
    return refus


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
            "hote": cles.hote(host), "appareils": appareils.du_compte(moi()["id"]),
            "code_expire": appareils.code_en_cours(moi()["id"]), "perso_max": appareils.PERSO_MAX,
            "par_tunnel": appareils.par_tunnel(request.headers), "droits_noms": {d: v[0] for d, v in comptes.DROITS.items()},
            "appareil_ici": appareils.reconnu(request.cookies.get(appareils.COOKIE)), **plus}


# --- Appareils approuvés pour l'accès par Internet (Cloudflare Tunnel) -------

@app.get("/appareil", response_class=HTMLResponse)
def page_appareil(request: Request, suite: str = "/"):
    return templates.TemplateResponse(request, "appareil.html", {"suite": _suite_sure(suite), "sans_nav": True})


@app.post("/appareil", response_class=HTMLResponse)
def approuver_appareil(request: Request, code: str = Form(...), nom: str = Form("Appareil"), suite: str = Form("/"),
                       captcha: str = Form("", alias=turnstile.CHAMP)):
    limiteur = request.app.state.limiteur
    ip = appareils.ip_client(request)
    attente = limiteur.bloque(ip) or _verrou_internet(request)
    refus_robot = None if attente else _captcha(request, captcha, "appareil")
    if refus_robot:
        return templates.TemplateResponse(request, "appareil.html", {"suite": _suite_sure(suite), "sans_nav": True,
                                                                     "erreur": refus_robot, "nom": nom}, status_code=403)
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
    _noter_appareil_ajoute(request, appareils.reconnu(valeur))
    r = RedirectResponse(_suite_sure(suite), status_code=303)
    r.set_cookie(appareils.COOKIE, valeur, max_age=appareils.DUREE, httponly=True, samesite="lax", secure=True)
    return r


def _noter_appareil_ajoute(request: Request, a: dict | None, comment: str = "code d'appareil"):
    """Nouvel appareil par Internet : journal et alerte aux admins, avec le compte auquel il est relié."""
    if not a:
        return
    k = comptes.trouver(a["compte_id"]) or {}
    qui = f"@{k.get('identifiant', '?')}"
    securite.noter("appareil_ajoute", request, f"« {a['nom']} » relié à {qui} ({comment})", alerte=(
        f"« {a['nom']} » ({qui}) peut maintenant ouvrir le site par Internet. Si ce n'est pas prévu : "
        "Réglages → Comptes → Appareils → Retirer."))


@app.post("/appareils/code", response_class=HTMLResponse)
def creer_code_appareil(request: Request):
    """Chacun ajoute lui-même un appareil à SON compte (déjà connecté, depuis un appareil approuvé ou le VPN).
    Un invité : un seul en plus de son appareil par défaut."""
    admin = comptes.est_admin(moi())
    pris = appareils.perso(moi()["id"])
    if not admin and len(pris) >= appareils.PERSO_MAX:
        return templates.TemplateResponse(request, "compte.html", _contexte_compte(
            request, erreur_appareil=f"Ton emplacement libre est pris par « {pris[0]['nom']} » : retire-le d'abord."),
            status_code=400)
    securite.noter("code_cree", request, f"code valable 15 min pour @{moi()['identifiant']}")
    return templates.TemplateResponse(request, "compte.html", _contexte_compte(
        request, code_appareil=appareils.nouveau_code(moi()["id"], limite=not admin)))


@app.post("/appareils/{ident}/retirer")
def retirer_appareil(request: Request, ident: str, suite: str = Form("")):
    """Son propre appareil ; l'admin, ceux de tout le monde (page Comptes)."""
    a = next((a for a in appareils.liste() if a["id"] == ident), None)
    admin = comptes.est_admin(moi())
    if not a or (a["compte_id"] != moi()["id"] and not admin):
        raise HTTPException(404, "appareil inconnu")
    if not admin and appareils.par_defaut(a):
        raise HTTPException(403, "appareil par défaut : seul l'admin peut le retirer")
    appareils.retirer(ident)
    k = comptes.trouver(a["compte_id"]) or {}
    securite.noter("appareil_retire", request, f"« {a['nom']} » de @{k.get('identifiant', '?')}")
    if suite == "comptes" and comptes.est_admin(moi()):
        return _retour_comptes(f"« {a['nom']} » ne peut plus ouvrir le site par Internet.")
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


# --- Installation : assistant pas à pas, puis Réglages → Configuration du site (installation.py, reglages.py) -------

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
        "turnstile_actif": turnstile.actif(), "supervision_active": cloudflare.actif(),
        "hote_public": cles.hote(request.headers.get("host", "")) if appareils.par_tunnel(request.headers) else None,
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


def _turnstile(request: Request, form: FormData, action: str) -> tuple[str, str | None]:
    """Clés du captcha Turnstile : (message, erreur). Clé du site vide = captcha éteint."""
    cle_site = (form.get("turnstile_site") or "").strip()
    saisi = (form.get("turnstile_secret") or "").strip()
    secret = saisi or os.environ.get("TURNSTILE_SECRET_KEY", "")
    if cle_site and not turnstile.format_ok(cle_site):
        return "", "Clé du site Turnstile : elle ressemble à 0x4AAAA… (Turnstile → ton widget)."
    if saisi and not turnstile.format_ok(saisi):
        return "", "Clé secrète Turnstile : elle ressemble à 0x4AAAA… (Turnstile → ton widget → Clé secrète)."
    if cle_site and not secret:
        return "", "Pour allumer le captcha, colle aussi la clé secrète Turnstile."
    message = ""
    if saisi or (action == "tester" and secret):
        etat, message = turnstile.tester(secret)
        if etat == "refuse":
            return "", message
        message = " " + message
    if action == "tester":
        return (message.strip(), None) if secret else ("", "Colle d'abord la clé secrète Turnstile.")
    avant = turnstile.actif()
    compte_id = moi()["id"]
    if saisi:
        reglages.ecrire("TURNSTILE_SECRET_KEY", saisi, compte_id)
    reglages.ecrire("TURNSTILE_SITE_KEY", cle_site, compte_id)
    if saisi or avant != turnstile.actif():
        _noter_reglage(request, "captcha Turnstile " + ("allumé" if turnstile.actif() else "éteint"))
    return message, None


def _supervision(request: Request, form: FormData, action: str) -> tuple[str, str | None]:
    """Jeton API Cloudflare en lecture seule + identifiants : (message, erreur). Jeton vide = garder l'ancien."""
    zone = (form.get("cf_zone") or "").strip().lower()
    compte = (form.get("cf_compte") or "").strip().lower()
    saisi = (form.get("cf_jeton") or "").strip()
    jeton = saisi or os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if zone and not cloudflare.id_ok(zone):
        return "", "Identifiant de zone Cloudflare : 32 caractères (ton domaine → Vue d'ensemble → colonne « API »)."
    if compte and not cloudflare.id_ok(compte):
        return "", "Identifiant de compte Cloudflare : 32 caractères (même endroit que l'ID de zone)."
    if saisi and (len(saisi) > 200 or not saisi.isascii() or any(c.isspace() for c in saisi)):
        return "", "Jeton API Cloudflare : colle-le tel quel (une seule ligne, sans espace)."
    if action == "tester_cf":
        if not (jeton and zone):
            return "", "Pour tester la supervision : colle le jeton API et l'identifiant de zone."
        etat, message = cloudflare.tester(jeton, zone, compte)
        return (message, None) if etat == "ok" else ("", message)
    if action == "tester":
        return "", None
    message = ""
    if saisi and zone:
        etat, texte = cloudflare.tester(jeton, zone, compte)
        if etat == "refuse":
            return "", texte + " Rien n'a été enregistré."
        message = " " + texte
    avant = cloudflare.actif()
    compte_id = moi()["id"]
    if saisi:
        reglages.ecrire("CLOUDFLARE_API_TOKEN", saisi, compte_id)
    reglages.ecrire("CLOUDFLARE_ZONE_ID", zone, compte_id)
    reglages.ecrire("CLOUDFLARE_ACCOUNT_ID", compte, compte_id)
    if saisi or avant != cloudflare.actif():
        _noter_reglage(request, "supervision Cloudflare " + ("allumée" if cloudflare.actif() else "éteinte"))
    return message, None


def _etape_cloudflare(request: Request, form: FormData, action: str = "enregistrer") -> tuple[str | None, str | None]:
    pays, erreur = installation.pays(form.get("pays"))
    if erreur:
        return None, erreur
    message_supervision, erreur = _supervision(request, form, action)
    if erreur:
        return None, erreur
    if action == "tester_cf":
        return message_supervision, None
    message_captcha, erreur = _turnstile(request, form, action)
    if erreur:
        return None, erreur
    if action == "tester":
        return message_captcha, None
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
    return "Accès par Internet enregistré." + suite + message_captcha + message_supervision, None


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
                message, erreur = await run_in_threadpool(_etape_cloudflare, request, form, action)
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
    if depuis_reglages or action in ("tester", "tester_cf", "creer_dossiers", "sans_claude"):
        r = reponse_page()
    else:
        r = RedirectResponse(f"/installation/{installation.suivante(etape)}", status_code=303)
    if etape == "compte" and action != "passer":
        _cookie_session(request, r, moi())   # nouveau mot de passe : nouvelle signature de session pour cet appareil
    return r


# --- Comptes : invitations, droits, données de chacun (voir comptes.py) --------------------

def _page_comptes(request: Request, statut: int = 200, **plus):
    return templates.TemplateResponse(request, "comptes.html", {
        "comptes": db.comptes(), "invitations": db.invitations_recentes(15), "droits": {d: comptes.DROITS[d] for d in comptes.DROITS_CASES},
        "droits_invite": comptes.DROITS_INVITE, "forfaits": comptes.FORFAIT, "forfait_invite": comptes.FORFAIT_INVITE,
        "niveau_forfait": comptes.niveau_forfait, "roles": comptes.ROLES, "duree_min": comptes.DUREE_JETON // 60,
        "maintenant": dt.datetime.now(dt.timezone.utc), "disque": disque.etat(),
        "appareils": db.nb_appareils_par_compte(), "appareils_internet": appareils.par_compte(),
        "tunnel": appareils.actif(), **plus}, status_code=statut)


def _lien_jeton(request: Request, jeton: str) -> str:
    return f"{_origine(request)}/invitation?{urlencode({'jeton': jeton})}"


@app.get("/comptes", response_class=HTMLResponse)
def page_comptes(request: Request, message: str = ""):
    return _page_comptes(request, message=message)


@app.post("/comptes/inviter", response_class=HTMLResponse)
def inviter(request: Request, role: str = Form("invite"), droits: list[str] = Form([]), note: str = Form(""),
            forfait: str = Form(comptes.FORFAIT_INVITE), appareil: str = Form("")):
    appareil = " ".join(appareil.split())[:60]
    if not appareil:
        return _page_comptes(request, 400, erreur="Donne le nom de l'appareil de la personne (ex. « iPhone de Camille ») : "
                                                  "le lien d'invitation l'autorise à ouvrir le site par Internet.")
    jeton, fin = comptes.inviter(moi()["id"], role, comptes.avec_forfait(droits, forfait), note, appareil)
    securite.noter("invitation", request, f"jeton créé ({comptes.ROLES.get(role, role)}, valable "
                   f"{comptes.DUREE_JETON // 60} min, appareil « {appareil} »)"
                   f"{' : ' + note.strip()[:60] if note.strip() else ''}")
    return _page_comptes(request, jeton={"valeur": jeton, "lien": _lien_jeton(request, jeton), "fin": fin,
                                         "genre": "compte", "appareil": appareil})


TEXTE_TEST = "Notification de test envoyée par l'admin 🔔"


@app.post("/comptes/notification-test", response_class=HTMLResponse)
def notification_test_comptes(request: Request, comptes_: list[int] = Form([], alias="comptes"),
                              texte: str = Form("")):
    """L'admin envoie une notification de test aux comptes choisis (sur tous leurs appareils abonnés)."""
    choisis = [k for k in db.comptes() if k["id"] in set(comptes_)]
    if not choisis:
        return _page_comptes(request, 400, erreur="Coche au moins un compte pour envoyer la notification de test.")
    corps = " ".join(texte.split())[:200] or TEXTE_TEST
    resultats = []
    for k in choisis:
        with db.pour_compte(k["id"]):   # pastille de l'icône calculée pour ce compte
            envoyes, erreurs = notifications.envoyer_a_tous("Claunote", corps, "/notifications", "test-admin",
                                                            compte_id=k["id"])
        nom = k["nom"] or k["identifiant"]
        if envoyes:
            resultats.append(f"{nom} : reçue sur {envoyes} appareil{'s' if envoyes > 1 else ''}")
        elif erreurs:
            resultats.append(f"{nom} : échec ({erreurs[0][:80]})")
        else:
            resultats.append(f"{nom} : aucun appareil abonné")
    securite.noter("notification_test", request, f"vers {', '.join('@' + k['identifiant'] for k in choisis)}")
    return _retour_comptes("Notification de test : " + " ; ".join(resultats) + ".")


def _cible(compte_id: int) -> dict:
    compte = db.compte(compte_id)
    if not compte:
        raise HTTPException(404, "compte inconnu")
    return compte


def _retour_comptes(message: str) -> RedirectResponse:
    return RedirectResponse("/comptes?" + urlencode({"message": message}), status_code=303)


@app.post("/comptes/{compte_id}/modifier", response_class=HTMLResponse)
def modifier_compte(request: Request, compte_id: int, role: str = Form("invite"), droits: list[str] = Form([]),
                    actif: str = Form(""), expire_le: str = Form(""), nom: str = Form(""),
                    forfait: str = Form(comptes.FORFAIT_INVITE)):
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
    droits_ = comptes.avec_forfait(droits, forfait)
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


@app.post("/comptes/{compte_id}/apercu")
def voir_en_tant_que(request: Request, compte_id: int):
    """Admin : regarder le site comme ce compte le voit (lecture seule, 1 h, bandeau pour revenir). Voir apercu.py."""
    cible = _cible(compte_id)
    if compte_id == moi()["id"]:
        return _retour_comptes("C'est ton propre compte : rien à voir de plus.")
    securite.noter("apercu", request, f"début : « {moi()['identifiant']} » voit le site comme "
                                      f"« {cible['identifiant']} » (lecture seule, 1 h)")
    r = RedirectResponse(_premiere_page(cible), status_code=303)
    r.set_cookie(apercu.COOKIE, apercu.creer(_cle_sessions(request.app), moi(), compte_id), max_age=apercu.DUREE,
                 httponly=True, samesite="lax", secure=_https(request))
    return r


@app.post("/apercu/quitter")
def quitter_apercu(request: Request):
    """Fin du « Voir en tant que » : retour à son compte, sur la fiche du compte regardé dans Comptes."""
    vu = apercu_en_cours()
    suite = "/comptes"
    if vu:
        securite.noter("apercu", request, f"fin : « {vu['admin']['identifiant']} » ne voit plus le site comme "
                                          f"« {vu['compte']['identifiant']} »")
        suite = f"/comptes#compte-{vu['compte']['id']}"
    r = RedirectResponse(suite, status_code=303)
    r.delete_cookie(apercu.COOKIE)
    return r


@app.post("/comptes/{compte_id}/appareil-code", response_class=HTMLResponse)
def code_appareil_pour(request: Request, compte_id: int):
    """L'admin crée un code d'appareil pour quelqu'un (nouveau téléphone, appli iPhone installée après coup)."""
    cible = _cible(compte_id)
    code = appareils.nouveau_code(compte_id, par_admin=True)
    securite.noter("code_cree", request, f"code valable 15 min pour @{cible['identifiant']} (par l'admin)")
    return _page_comptes(request, code_appareil={"valeur": code, "pour": cible["identifiant"], "compte_id": compte_id})


@app.post("/comptes/appareils/{ident}/deplacer")
def deplacer_appareil(request: Request, ident: str, compte: int = Form(...)):
    """L'admin donne un de ses appareils (ou celui de n'importe qui) à un autre compte, comme appareil par défaut."""
    cible = _cible(compte)
    avant = next((a for a in appareils.liste() if a["id"] == ident), None)
    if not avant:
        raise HTTPException(404, "appareil inconnu")
    de = comptes.trouver(avant["compte_id"]) or {}
    appareils.deplacer(ident, compte)
    securite.noter("appareil_deplace", request, f"« {avant['nom']} » de @{de.get('identifiant', '?')} "
                   f"vers @{cible['identifiant']}")
    return _retour_comptes(f"« {avant['nom']} » est maintenant l'appareil par défaut de @{cible['identifiant']}.")


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
    appareils.retirer_du_compte(compte_id)
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
        "mdp_min": comptes.MDP_MIN, "version": comptes.CONDITIONS_VERSION,
        "par_internet": appareils.par_tunnel(request.headers) and appareils.actif() and not _appareil_valable(request),
        **plus}, status_code=statut)


@app.get("/invitation", response_class=HTMLResponse)
def page_invitation(request: Request, jeton: str = ""):
    erreur = "Jeton inconnu, déjà utilisé ou expiré : demande-en un nouveau à l'admin." \
        if jeton and not comptes.invitation(jeton) else None
    return _page_invitation(request, jeton, erreur=erreur)


@app.post("/invitation", response_class=HTMLResponse)
def utiliser_invitation(request: Request, jeton: str = Form(...), identifiant: str = Form(""), nom: str = Form(""),
                        mot_de_passe: str = Form(""), confirmation: str = Form(""), accepte: str = Form(""),
                        captcha: str = Form("", alias=turnstile.CHAMP)):
    limiteur = request.app.state.limiteur
    ip = appareils.ip_client(request)
    attente = limiteur.bloque(ip) or _verrou_internet(request)
    if attente:
        return _page_invitation(request, "", 429, erreur=f"Trop d'essais : réessaie dans {attente // 60 + 1} min.")
    refus_robot = _captcha(request, captcha, "invitation")
    if refus_robot:   # le jeton reste dans la page : la personne n'a qu'à réessayer
        jeton_ok = jeton if comptes.invitation(jeton) else ""
        return _page_invitation(request, jeton_ok, 403, erreur=refus_robot, identifiant=identifiant, nom=nom)
    inv = comptes.invitation(jeton)
    if not inv:
        limiteur.echec(ip)
        securite.echec("invitation_echec", request, "jeton inconnu ou expiré")
        return _page_invitation(request, "", 403, erreur="Jeton inconnu, déjà utilisé ou expiré : demande-en un "
                                                         "nouveau à l'admin.")
    sans_appareil = (appareils.par_tunnel(request.headers) and appareils.actif()
                     and not _appareil_valable(request))
    if inv["genre"] == "mot_de_passe" and sans_appareil:
        return _page_invitation(request, "", 403, erreur="Par Internet, ce lien s'ouvre depuis un appareil déjà approuvé. "
                                                         "Nouvel appareil : demande un code d'appareil à l'admin.")
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
    if sans_appareil:   # par Internet : le lien approuve aussi cet appareil, relié au nouveau compte
        valeur = appareils.approuver_par_invitation(inv.get("appareil") or f"Appareil de {identifiant}", compte_id)
        _noter_appareil_ajoute(request, appareils.reconnu(valeur), "lien d'invitation")
        r.set_cookie(appareils.COOKIE, valeur, max_age=appareils.DUREE, httponly=True, samesite="lax", secure=True)
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
    r.delete_cookie(apercu.COOKIE)
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
        "alertes_actives": notifications.preferences(None).get("securite", True), "abonnes": len(db.abonnements()),
        "appareils": appareils.liste(), "par_tunnel": appareils.par_tunnel(request.headers),
        "cf": cloudflare.etat(), "cf_actif": cloudflare.actif(), "cf_seuil": cloudflare.PIC_MIN,
        "lien_page": lambda n: "/securite?" + urlencode({k: v for k, v in (("gravite", gravite), ("page", n)) if v})})


@app.post("/securite/cloudflare")
async def securite_cloudflare():
    """Bouton « Relever maintenant » : une relève sans compter la fenêtre (ni médiane faussée, ni alerte)."""
    e = await run_in_threadpool(cloudflare.tour, False)
    message = e.get("erreur") or "Chiffres Cloudflare relevés."
    return RedirectResponse("/securite?" + urlencode({"message": message}) + "#cloudflare", status_code=303)


@app.post("/securite/debloquer")
def securite_debloquer(request: Request):
    securite.debloquer(request)
    return RedirectResponse("/securite?" + urlencode({"message": "Bans et blocages levés."}), status_code=303)


@app.post("/securite/alertes")
def securite_alertes(actives: str = Form("")):
    notifications.enregistrer_preferences({**notifications.preferences(None), "securite": actives == "1"}, None)
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
    """Page d'arrivée (plus de page « Aujourd'hui ») : celle choisie dans Apparence, sinon Actualités, sinon la
    première rubrique permise."""
    if choisie := _sans_erreur(lambda: apparence.accueil(compte, comptes.peut), None):
        return choisie
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


@app.get("/reglages/apparence", response_class=HTMLResponse)
def page_apparence(request: Request, message: str = ""):
    return templates.TemplateResponse(request, "apparence.html", {
        "ap": apparence.lire(moi()), "modes": apparence.MODES, "styles": apparence.STYLES, "message": message,
        "permises": [k for k, r in apparence.RUBRIQUES.items() if comptes.peut(moi(), r[2])]})


@app.post("/reglages/navigation")
async def enregistrer_navigation(request: Request):
    """Onglets gardés, leur ordre (position 1 à 4) et la page d'accueil du compte."""
    f = await request.form()
    def position(k):
        try:
            return int(f.get(f"ordre_{k}") or 9)
        except ValueError:
            return 9
    gardes = sorted((k for k in apparence.ORDRE if f.get(f"onglet_{k}")),
                    key=lambda k: (position(k), apparence.ORDRE.index(k)))
    if moi():
        apparence.enregistrer_navigation(moi(), gardes, str(f.get("accueil") or ""))
    return RedirectResponse("/reglages/apparence?message=" + quote("Onglets et page d'accueil enregistrés."),
                            status_code=303)


@app.post("/reglages/apparence")
def enregistrer_apparence(mode: str = Form("auto"), style: str = Form("claunote"), suite: str = Form("")):
    if moi():
        apparence.enregistrer(moi(), mode, style)
    if suite == "/actualites/vigie":
        return RedirectResponse(suite, status_code=303)
    return RedirectResponse("/reglages/apparence?message=" + quote("Apparence enregistrée."), status_code=303)


@app.get("/reglages/profil", response_class=HTMLResponse)
def page_profil(request: Request, message: str = ""):
    compte_id = (moi() or {}).get("id") or 1
    p = profil.lire(compte_id)
    return templates.TemplateResponse(request, "profil.html", {
        "p": p, "message": message, "explications": profil.EXPLICATIONS, "detail": profil.DETAIL,
        "alternance": profil.ALTERNANCE, "fiche_forme": profil.FICHE_FORME, "quiz_nb": profil.QUIZ_NB,
        "cartes_nb": profil.CARTES_NB, "nouvelles_jour": profil.NOUVELLES_JOUR, "longueurs": {k: v[0] for k, v in profil.TEXTES.items()},
        "formation_matieres": profil.formation(compte_id, {**p, "formation": ""}),
        "parcours": profil.parcours(compte_id), "bloc": profil.bloc(compte_id)})


@app.post("/reglages/profil")
def enregistrer_profil(formation: str = Form(""), alternance: str = Form(""), entreprise: str = Form(""),
                       objectif: str = Form(""), explications: str = Form("normal"), detail: str = Form("equilibre"),
                       exemples: str = Form(""), difficultes: str = Form(""), libre: str = Form(""),
                       fiche_forme: str = Form("synthese"), quiz_nb: str = Form("10"), cartes_nb: str = Form("20"),
                       nouvelles_jour: str = Form("0")):
    compte_id = (moi() or {}).get("id") or 1
    profil.enregistrer(compte_id, {"formation": formation, "alternance": alternance, "entreprise": entreprise,
                                   "objectif": objectif, "explications": explications, "detail": detail,
                                   "exemples": exemples, "difficultes": difficultes, "libre": libre,
                                   "fiche_forme": fiche_forme, "quiz_nb": quiz_nb, "cartes_nb": cartes_nb,
                                   "nouvelles_jour": nouvelles_jour})
    return RedirectResponse("/reglages/profil?message=" + quote("Profil enregistré : l'IA en tient compte dès maintenant."),
                            status_code=303)


def _resume_serveur() -> dict:
    """Une ligne pour l'entrée « Serveur » des Réglages : le plus urgent d'abord (panne, pause, disque, mise à jour)."""
    panne = _sans_erreur(bandeau_service)
    pause = _sans_erreur(lambda: db.etat_valeur("analyses_pause") == "oui", False)
    dq = _sans_erreur(disque.etat)
    maj_n = _sans_erreur(maj.disponibles, 0) or 0
    if panne:
        return {"phrase": panne[0], "badge": "!", "alerte": True}
    if dq and dq["niveau"] in ("attention", "critique"):
        return {"phrase": f"Peu de place : {dq['libre_go']:.1f} Go libres", "badge": "!", "alerte": True}
    if pause:
        return {"phrase": "Analyses en pause", "badge": "pause", "alerte": True}
    if maj_n:
        return {"phrase": f"{maj_n} nouveauté{'s' if maj_n > 1 else ''} à installer", "badge": maj_n, "alerte": False}
    libre = f" · {dq['libre_go']:.0f} Go libres" if dq else ""
    return {"phrase": f"Tout tourne{libre}", "badge": 0, "alerte": False}


@app.get("/reglages", response_class=HTMLResponse)
def page_reglages(request: Request):
    """Point d'entrée unique : « Moi » pour tout le monde, « Administration » pour l'admin, avec l'état de chaque page."""
    moi_ = getattr(request.state, "compte", None) or {}
    admin = comptes.est_admin(moi_)
    ctx = {"abonnes": len(_sans_erreur(db.abonnements, []) or []), "admin": admin,
           "profil_rempli": _sans_erreur(lambda: profil.rempli(profil.lire(moi_.get("id") or 1)), 0)}
    if admin:
        ctx.update({
            "nb_comptes": len(_sans_erreur(db.comptes, []) or []),
            "alertes": _sans_erreur(securite.nb_nouvelles_alertes, 0),
            "serveur": _resume_serveur(),
            "inst_a_faire": _sans_erreur(installation.a_faire, False),
            "nb_demandes": demandes_ia.nb_en_attente(),
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
    # Invités : les cours publics des autres comptes (ceux de l'admin surtout) directement ici, en lecture seule
    autres = defaultdict(lambda: defaultdict(list))
    if not comptes.est_admin(moi()):
        for r in db.bibliotheque_partagee():
            autres[r["niveau"]][r["matiere"]].append(r)
    niveaux_autres = [n for n in reversed(NIVEAUX) if n in autres] + sorted(n for n in autres if n not in NIVEAUX)
    acces = None if comptes.est_admin(moi()) else ((comptes.trouver(moi()["id"]) or {}).get("acces_cours") or "")
    return templates.TemplateResponse(request, "bibliotheque.html", {
        "arbre": arbre, "niveaux": niveaux, "nb_hors_cours": db.nb_hors_cours(),
        "partage": partage, "publiques": db.matieres_publiques() if partage else set(),
        "autres": autres, "niveaux_autres": niveaux_autres, "acces": acces})


@app.get("/liste", response_class=HTMLResponse)
def liste(request: Request, niveau: str, matiere: str, type: str):
    return templates.TemplateResponse(request, "liste.html", {
        "niveau": niveau, "matiere": matiere, "type": type, "docs": db.liste(niveau, matiere, type)})


@app.get("/doc/{doc_id}", response_class=HTMLResponse)
def document(request: Request, doc_id: int):
    doc = db.document(doc_id)
    if doc is None:
        if not comptes.est_admin(moi()) and db.document_partage(doc_id):   # un cours partagé (cité par le chat, une fiche) : sa page en lecture seule
            return RedirectResponse(f"/partages/doc/{doc_id}", status_code=303)
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
        if doc["statut"] == "classe" and db.plusieurs_comptes() else False,
        "notes_cours": _notes_cours(doc_id)})


def _notes_cours(doc_id: int) -> list[dict]:
    """Ses notes rattachées à ce cours (section « Mes notes » de la page du cours)."""
    return db.notes_du_document(doc_id) if comptes.peut(moi(), "notes") else []


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
def deposer(request: Request, fichiers: list[UploadFile] = File(...), niveau: str = Form(""), matiere: str = Form(""),
            paquet: int = 0):
    """Envoie les fichiers dans l'inbox kDrive, en gardant les dossiers d'un dossier déposé.

    Un niveau choisi devient un sous-dossier de l'inbox : le worker s'en sert comme année d'étude.
    Un gros dossier arrive par paquets (`paquet=1`, réponse JSON) ; le worker attend la fin
    de l'envoi avant de trier le lot.
    """
    if niveau and niveau not in NIVEAUX:
        raise HTTPException(400, "niveau inconnu")
    # Matière choisie : un sous-dossier de l'année, que le worker reconnaît (formations.matiere_depuis_dossier)
    matiere = " ".join(matiere.replace("/", " ").replace("\\", " ").split())[:80] if niveau else ""
    envoyes, ignores = _envoyer_inbox(fichiers, niveau, matiere, moi()["id"])
    if paquet:
        return JSONResponse({"envoyes": envoyes, "ignores": ignores})
    return templates.TemplateResponse(request, "deposer.html", {
        "niveaux": NIVEAUX, "envoyes": envoyes, "ignores": ignores, **_filtres_depot()})


def _envoyer_inbox(fichiers: list[UploadFile], niveau: str, matiere: str, compte_id: int) -> tuple[list, list]:
    """Fichiers dans l'inbox (sous-dossiers gardés), notés au compte qui dépose ; (envoyés, ignorés)."""
    kd = KDrive.from_env()
    dossiers_kdrive: dict[tuple, int] = {}
    envoyes, ignores = [], []
    for f in fichiers:
        parts = [p for p in PurePosixPath((f.filename or "").replace("\\", "/")).parts if p not in ("", ".", "..", "/")]
        if not parts:
            continue
        chemin = ([niveau] if niveau else []) + ([matiere] if matiere else []) + parts
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
            db.noter_depot(envoi["id"], compte_id)
        envoyes.append("/".join(chemin))
    if envoyes:
        db.demander_scan()
    return envoyes, ignores


# --- Scanner avec Apple : raccourci iOS « Envoyer à Claunote » (raccourci.py) ---------------

SCAN_MAX = 200 * 1024 * 1024    # un scan de 200 Mo : déjà des centaines de pages


@app.get("/raccourci", response_class=HTMLResponse)
def page_raccourci(request: Request):
    return templates.TemplateResponse(request, "raccourci.html", _contexte_raccourci(request))


def _contexte_raccourci(request: Request, jeton: str | None = None) -> dict:
    return {"racc": raccourci.etat(moi()["id"]), "jeton": jeton, "nom": raccourci.NOM,
            "adresse": _origine(request) + "/raccourci/envoi", "par_internet": appareils.par_tunnel(request.headers)}


@app.post("/raccourci/jeton", response_class=HTMLResponse)
def raccourci_jeton(request: Request):
    """Nouveau jeton pour le raccourci (l'ancien ne marche plus) : montré une seule fois."""
    jeton = raccourci.creer(moi()["id"])
    securite.noter("raccourci_cree", request, f"compte {moi()['identifiant']}", alerte=(
        f"Jeton du raccourci iPhone « {raccourci.NOM} » créé pour {moi()['identifiant']}. "
        "Si ce n'est pas toi : Déposer → « scanner depuis Notes » → « Retirer »."))
    return templates.TemplateResponse(request, "raccourci.html", _contexte_raccourci(request, jeton))


@app.post("/raccourci/retirer")
def raccourci_retirer(request: Request):
    raccourci.retirer(moi()["id"])
    securite.noter("raccourci_retire", request, f"compte {moi()['identifiant']}")
    return RedirectResponse("/raccourci", status_code=303)


def _refus_raccourci(request: Request, texte: str, statut: int, detail: str = "") -> Response:
    if detail:
        request.app.state.limiteur.echec(appareils.ip_client(request))
        securite.echec("raccourci_echec", request, detail)
    return Response(texte, status_code=statut, media_type="text/plain; charset=utf-8")


@app.post("/raccourci/envoi")
async def raccourci_envoi(request: Request, nom: str = ""):
    """Reçoit un scan du raccourci iOS : corps brut (« Fichier ») ou formulaire (un ou plusieurs fichiers).

    Réponse en texte court : le raccourci l'affiche tel quel (« Afficher le résultat »)."""
    ip = appareils.ip_client(request)
    if request.app.state.limiteur.bloque(ip) or _verrou_internet(request):
        return _refus_raccourci(request, "Trop d'essais refusés : réessaie plus tard.", 429)
    jeton = raccourci.lire_entete(request.headers.get("authorization"))
    compte_id = await run_in_threadpool(raccourci.verifier, jeton) if jeton else None
    compte = comptes.trouver(compte_id) if compte_id else None
    if not compte or not comptes.utilisable(compte):
        return _refus_raccourci(request, "Jeton du raccourci refusé : recrée-le dans Claunote (Déposer → "
                                         "scanner depuis Notes).", 401,
                                "jeton absent" if not jeton else f"jeton refusé ({jeton[:4]}…)")
    if not comptes.peut(compte, "deposer"):
        return _refus_raccourci(request, "Ton compte n'a pas le droit « Déposer ».", 403)
    if int(request.headers.get("content-length") or 0) > SCAN_MAX:
        return _refus_raccourci(request, "Fichier trop gros (200 Mo au plus).", 413)
    fichiers: list[FichierRecu] = []
    if (request.headers.get("content-type") or "").lower().startswith("multipart/form-data"):
        formulaire = await request.form(max_files=100)
        fichiers = [f for _, f in formulaire.multi_items() if isinstance(f, FichierRecu)]
    else:   # « Corps de la requête : Fichier » : le fichier lui-même
        tampon = tempfile.SpooledTemporaryFile(max_size=GROS_FICHIER)
        taille = 0
        async for morceau in request.stream():
            taille += len(morceau)
            if taille > SCAN_MAX:
                return _refus_raccourci(request, "Fichier trop gros (200 Mo au plus).", 413)
            tampon.write(morceau)
        tampon.seek(0)
        if taille:
            fichiers = [UploadFile(tampon, size=taille, filename=nom)]
    maintenant = dt.datetime.now()
    for f in fichiers:   # nom absent (corps brut) ou sans extension : d'après le type du fichier
        f.filename = raccourci.nom_du_scan(PurePosixPath((f.filename or nom).replace("\\", "/")).name,
                                          f.content_type if f.headers else request.headers.get("content-type"),
                                          maintenant)
    if not fichiers:
        return _refus_raccourci(request, "Rien reçu : partage un scan ou une photo vers le raccourci.", 400)

    def deposer_pour_le_compte():
        jeton_moi = _moi.set(compte)
        try:
            with comptes.pour(compte["id"]):
                envoyes, ignores = _envoyer_inbox(fichiers, "", "", compte["id"])
                raccourci.noter_usage(compte["id"], len(envoyes))
                return envoyes, ignores
        finally:
            _moi.reset(jeton_moi)

    request.app.state.limiteur.reussite(ip)
    envoyes, ignores = await run_in_threadpool(deposer_pour_le_compte)
    if not envoyes:
        return _refus_raccourci(request, "Format non lu par Claunote : envoie un PDF ou une photo.", 415)
    n = len(envoyes)
    return Response(f"✅ {n} scan{'s' if n > 1 else ''} envoyé{'s' if n > 1 else ''} à Claunote : analyse en cours.",
                    media_type="text/plain; charset=utf-8")


def _filtres_depot() -> dict:
    """Listes partagées avec la page Déposer, qui écarte le code et les fichiers système avant l'envoi,
    et les matières proposées pour chaque année (celles du référentiel, puis celles déjà rangées)."""
    return {"extensions": sorted(EXTENSIONS), "dossiers_inutiles": sorted(importation.DOSSIERS_INUTILES),
            "matieres_depot": _matieres_depot()}


def _matieres_depot() -> dict[str, list[str]]:
    par_niveau: dict[str, list[str]] = defaultdict(list)
    try:
        for f in db.formations():
            if f["statut"] == "pret":
                par_niveau[f["niveau"]] += formations.noms_officiels(f["referentiel"])
        for r in db.matieres_par_annee():
            par_niveau[r["niveau"]].append(r["matiere"])
    except Exception:
        return {}
    return {n: list(dict.fromkeys(noms)) for n, noms in par_niveau.items()}


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
    dans_notes = db.chercher_notes(q) if q.strip() and comptes.peut(moi(), "notes") else []
    return templates.TemplateResponse(request, "recherche.html", {"q": q, "resultats": resultats,
                                                                  "proches": proches[:8], "dans_notes": dans_notes})


@app.get("/a-verifier", response_class=HTMLResponse)
def a_verifier(request: Request):
    docs = db.a_verifier()   # les siens seulement, même avec « Tout voir » : chacun range ses dépôts
    return templates.TemplateResponse(request, "a_verifier.html", {
        "doutes": [d for d in docs if d["statut"] != "erreur"], "erreurs": [d for d in docs if d["statut"] == "erreur"],
        "niveaux": NIVEAUX, "types": TYPES, "matieres": db.matieres(),
        # « Tout voir » : ce qui attend chez les autres, pour info (c'est à eux de ranger leurs dépôts)
        "ailleurs": db.a_verifier_ailleurs() if comptes.tout_voir(moi(), request.url.path) else []})


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
        "a_choisir": bool(db.niveaux_matieres()) or _partages_utilisables(), "a_archiver": archivage.nb_propositions()})


def _partages_utilisables() -> bool:
    """Un invité sans cours à lui peut réviser ceux qu'on lui partage."""
    return not comptes.est_admin(moi()) and bool(db.bibliotheque_partagee())


@app.get("/reviser/nouvelle", response_class=HTMLResponse)
def nouvelle_revision(request: Request, sujet: str = "", matiere: str = "", date_eval: str = "", genre: str = "",
                      doc: int | None = None, note: int | None = None):
    """Formulaire d'une révision ; les paramètres le préremplissent (depuis l'agenda Pronote, un cours, une note)."""
    document = db.document(doc) if doc else None
    jointes = db.notes_choisies([note]) if note and comptes.peut(moi(), "notes") else []
    if document and comptes.peut(moi(), "notes"):
        jointes += [n for n in db.notes_du_document(document["id"]) if n["id"] != note]
    return templates.TemplateResponse(request, "reviser_nouvelle.html", {
        "choix": db.niveaux_matieres_comptes(), "genres": revision.GENRES, "document": document,
        "notes_jointes": jointes,
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
    pieces = await _pieces(f) + _notes_jointes(f)
    rev_id = db.creer_revision(genre, sujet, ids, eval_le, consigne=consigne, strict=bool(f.get("strict")))
    if pieces:
        db.ajouter_pieces(pieces, revision_id=rev_id)
    db.demander_scan()
    return RedirectResponse(f"/revision/{rev_id}", status_code=303)


def _notes_jointes(f) -> list[tuple[str, bytes]]:
    """Notes cochées pour une fiche, un quiz… : jointes au support comme le contexte de l'élève (ses mots à lui)."""
    if not comptes.peut(moi(), "notes"):
        return []
    ids = list(dict.fromkeys(int(i) for i in f.getlist("note_ids") if str(i).isdigit()))
    return [notes.piece_jointe(n) for n in db.notes_choisies(ids)]


def descriptions_genres() -> dict:
    """Selon Mon profil : forme des fiches, taille des quiz et des paquets."""
    p = profil.lire((moi() or {}).get("id"))
    fiche = "explicative + questions" if p["fiche_forme"] == "explicative" else "synthèse dense + questions"
    return {"fiche": fiche, "quiz": f"{p['quiz_nb']} questions à choix", "cartes": f"{p['cartes_nb']} cartes à mémoriser",
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
        "descriptions": descriptions_genres(), "cochees": cochees, "devant": devant,
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
    if rev["statut"] == "pret":
        db.noter_ouverture(rev_id)   # « pas ouverte depuis… » : base des propositions d'archivage
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
    # Sources : ses cours, et ceux qu'on lui partage (un invité fait aussi ses fiches avec les cours de l'admin)
    with (nullcontext() if comptes.est_admin(moi()) else db.partages()):
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
        "sans_texte": revision.sans_texte(sources), "attente_opus": _attente_opus(rev)})


def _attente_opus(rev: dict) -> str | None:
    """« 19 h 05 » si cette révision attend la reprise d'Opus 5.5 (limite d'usage), sinon None."""
    if rev["statut"] != "en_attente" or not modeles.obligatoire(rev["genre"]):
        return None
    jusqu_a = _instant(((etat_service().get("modeles") or {}).get("opus") or {}).get("jusqu_a"))
    return _heure_fr(jusqu_a) if jusqu_a and jusqu_a > dt.datetime.now(pronote.PARIS) else None


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


@app.post("/revision/{rev_id}/archiver")
def archiver_revision(rev_id: int):
    rev = db.revision(rev_id)
    if rev is None:
        raise HTTPException(404)
    db.archiver(revisions=[rev_id])
    return RedirectResponse(f"/revision/{rev_id}", status_code=303)


# --- Archives : ménage proposé au fil du temps (archivage.py) --------------------

@app.get("/archives", response_class=HTMLResponse)
def page_archives(request: Request):
    campagnes, revisions = db.archives()
    return templates.TemplateResponse(request, "archives.html", {
        "propositions": archivage.propositions(), "campagnes": campagnes, "revisions": revisions,
        "genres": revision.GENRES, "courts": revision.COURTS, "garder_jours": archivage.GARDER_JOURS})


def _retour(retour: str) -> str:
    return retour if retour.startswith("/") and not retour.startswith("//") else "/archives"


@app.post("/archives/{action}")
async def action_archives(request: Request, action: str):
    """Cases cochées (« revision:12 », « campagne:3 ») : archiver, garder, restaurer ou supprimer pour de bon."""
    f = await request.form()
    revs, camps = archivage.separer(f.getlist("cle"))
    if action == "archiver":
        db.archiver(revs, camps)
    elif action == "garder":
        db.garder(revs, camps, archivage.GARDER_JOURS)
    elif action == "restaurer":
        db.restaurer(revs, camps, archivage.GARDER_JOURS)
    elif action == "supprimer":
        db.supprimer_archives(revs, camps)
    else:
        raise HTTPException(404)
    return RedirectResponse(_retour(str(f.get("retour") or "")), status_code=303)


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


@app.post("/campagne/{campagne_id}/archiver")
def archiver_campagne(campagne_id: int):
    if db.campagne(campagne_id) is None:
        raise HTTPException(404)
    db.archiver(campagnes=[campagne_id])
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
        passages = question.reordonner(q, passages, limite)
    else:
        passages = [{**d, "document_id": d["id"], "texte": (d.get("transcription") or "")[:3000]}
                    for d in db.cours_pour_question(question.requete(q), conv["niveau"], conv["matiere"], 5,
                                                    avec_texte=True, statuts=db.STATUTS_UTILES)]
    return _notes_pour_question(q, conv) + passages


def _notes_pour_question(q: str, conv: dict) -> list[dict]:
    """Ses propres notes qui en parlent, en tête des passages : ce qu'il a compris et noté lui-même."""
    if not comptes.peut(moi(), "notes"):
        return []
    try:
        return [notes.comme_passage(n) for n in db.notes_pour_question(q, conv["niveau"], conv["matiere"],
                                                                        notes.NB_POUR_QUESTION)]
    except Exception as e:   # la recherche dans les cours suffit
        print(f"notes pour la question impossibles : {e}", flush=True)
        return []


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

    Selon le réglage (page Analyses) : le modèle local d'abord, Claude seulement s'il doute ou demande de
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


GARDER, AUTRE = "__garder__", "__autre__"


def _annees_matieres(comptes: list[dict]) -> list[dict]:
    """Une carte par année : formation, référentiel, matières rangées et où les ranger (page Matières)."""
    par_niveau = defaultdict(list)
    for r in comptes:
        par_niveau[r["niveau"]].append(r)
    forms = {f["niveau"]: f for f in db.formations()}
    regles = db.matieres_regles()
    annees = []
    for niveau in reversed(NIVEAUX):
        if niveau not in par_niveau and niveau not in forms:
            continue
        f = forms.get(niveau)
        ref = f["referentiel"] if f and f["statut"] == "pret" else None
        rangees = par_niveau.get(niveau, [])
        noms = [r["matiere"] for r in rangees]
        n_par_nom = {r["matiere"]: r["n"] for r in rangees}
        officielles = [{**m, "n": n_par_nom.get(m["nom"], 0)} for m in (ref or {}).get("matieres", [])]
        annees.append({
            "niveau": niveau, "formation": f, "ref": ref, "rangees": rangees, "officielles": officielles,
            "noms_officiels": [m["nom"] for m in officielles],
            "suggestions": formations.suggestions_reorganisation(niveau, noms, ref, regles),
            "regles": sorted(((src, cible) for (n, src), cible in regles.items() if n == niveau), key=lambda x: x[1]),
            "propositions": formations.SUGGESTIONS.get(niveau, [])})
    return annees


@app.get("/organiser/matieres", response_class=HTMLResponse)
def page_matieres(request: Request, message: str = ""):
    comptes, groupes = _propositions_matieres()
    annees = _annees_matieres(comptes)
    montrees = {a["niveau"] for a in annees}
    officiels = {a["niveau"]: a["noms_officiels"] for a in annees}
    for g in groupes:   # le nom du référentiel de l'année, s'il est dans le groupe
        nom = next((n["nom"] for n in g["noms"] if n["nom"] in officiels.get(g["niveau"], [])), None)
        g["nom"] = nom or g["nom"]
    return templates.TemplateResponse(request, "matieres.html", {
        "groupes": groupes, "annees": annees, "message": message, "pause": _claude_en_pause(),
        "GARDER": GARDER, "AUTRE": AUTRE, "autres_niveaux": [n for n in reversed(NIVEAUX) if n not in montrees],
        "suggestions_formation": formations.SUGGESTIONS,
        "en_attente": any(a["formation"] and a["formation"]["statut"] in ("a_chercher", "en_cours") for a in annees),
        "nb_distinctes": sum(map(len, db.matieres_distinctes().values()))})


def _vers_matieres(message: str, niveau: str = "") -> RedirectResponse:
    ancre = f"#annee-{niveau}" if niveau else ""
    return RedirectResponse(f"/organiser/matieres?{urlencode({'message': message})}{ancre}", status_code=303)


@app.post("/organiser/formation")
def definir_formation(niveau: str = Form(...), nom: str = Form("")):
    """Nom de la formation d'une année : Claude cherchera son référentiel officiel (worker)."""
    if niveau not in NIVEAUX:
        raise HTTPException(400, "année inconnue")
    nom = " ".join(nom.split())[:200]
    db.definir_formation(niveau, nom)
    if not nom:
        return _vers_matieres(f"{niveau} : formation oubliée. Les règles de rangement restent.", niveau)
    db.demander_scan()   # réveille le worker
    return _vers_matieres(f"{niveau} : « {nom} » enregistrée. Claude cherche le référentiel officiel "
                          "(quelques minutes) ; la page se met à jour toute seule.", niveau)


@app.post("/organiser/reorganiser")
async def reorganiser_matieres(request: Request):
    """Ranger chaque matière d'une année dans une autre (officielle, existante ou nouveau nom).
    Chaque changement devient une règle : les prochains dépôts suivent le même rangement."""
    f = await request.form()
    niveau = f.get("niveau", "")
    if niveau not in NIVEAUX:
        raise HTTPException(400, "année inconnue")
    anciennes, cibles, autres = f.getlist("ancienne"), f.getlist("cible"), f.getlist("autre")
    if not (len(anciennes) == len(cibles) == len(autres)):
        raise HTTPException(400, "formulaire incomplet")
    vers: dict[str, list[str]] = defaultdict(list)
    for ancienne, cible, autre in zip(anciennes, cibles, autres):
        cible = " ".join(autre.split())[:80] if cible == AUTRE else cible
        if ancienne and cible and cible not in (GARDER, AUTRE) and cible != ancienne:
            vers[cible].append(ancienne)
    if not vers:
        return _vers_matieres(f"{niveau} : rien à changer.", niveau)
    n = sum(db.fusionner_matieres(niveau, olds, cible) for cible, olds in vers.items())
    if n:
        db.demander_scan()   # le worker déplace les fichiers
    resume = " ; ".join(f"{', '.join(olds)} → {cible}" for cible, olds in vers.items())
    return _vers_matieres(f"{niveau} : {resume}. {n} document{'s' if n > 1 else ''} rangé{'s' if n > 1 else ''} "
                          "autrement ; les prochains dépôts suivront ce rangement.", niveau)


@app.post("/organiser/regles/supprimer")
def supprimer_regle(niveau: str = Form(...), source: str = Form(...)):
    db.supprimer_regle(niveau, source)
    return _vers_matieres(f"{niveau} : règle « {source} » retirée. Les documents déjà rangés ne bougent pas.", niveau)


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
               "les fichiers sont déplacés dans kDrive en arrière-plan, et les prochains dépôts suivront ce nom.")
    return RedirectResponse(f"/organiser/matieres?{urlencode({'message': message})}", status_code=303)


# --- Flashcards --------------------------------------------------------------

@app.get("/cartes", response_class=HTMLResponse)
def page_cartes(request: Request):
    try:
        delaisses = [p for p in archivage.propositions() if p["genre"] == "cartes"]
    except Exception:
        delaisses = []
    return templates.TemplateResponse(request, "cartes.html", {"paquets": db.paquets(), "delaisses": delaisses})


@app.get("/cartes/session", response_class=HTMLResponse)
def session_cartes(request: Request, paquet: int | None = None):
    """Une carte à la fois : recto, on retourne, on note de « À revoir » à « Facile »."""
    carte = db.prochaine_carte(paquet, nouvelles_max=profil.nouvelles_max((moi() or {}).get("id")))
    reperes = (carte["reperes"] or {}) if carte else {}
    return templates.TemplateResponse(request, "session_cartes.html", {
        "carte": carte, "paquet": paquet, "notes": revision.NOTES, "reperes": reperes,
        "reste": db.nb_cartes_a_revoir(paquet, nouvelles_max=profil.nouvelles_max((moi() or {}).get("id"))),
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
    pronote_a_moi = comptes.peut(moi(), "agenda")   # son Pronote à lui (Agenda)
    return templates.TemplateResponse(request, "notifications.html", {
        "p": notifications.preferences(), "appareils": db.abonnements(), "pronote_a_moi": pronote_a_moi,
        "edt": emploi_du_temps.resume(dt.datetime.now(pronote.PARIS),
                                      notifications._heure(notifications.preferences()["heure"])),
        "recentes": db.notifications_recentes(8), "cle": notifications.cle_publique(),
        "attente": notifications.raison_attente(dt.datetime.now(pronote.PARIS)),
        "worker_a_jour": version.worker_a_jour(etat_service()),
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


@app.post("/notifications/test-verrouille", response_class=HTMLResponse)
def tester_ecran_verrouille():
    """Test envoyé par le worker dans 1 min, comme une alerte : on verrouille l'iPhone et on regarde s'il arrive.
    « Envoyer un test » part du site pendant que le téléphone est réveillé : il arrive toujours, ça ne prouve rien
    pour une alerte qui part quand le téléphone dort."""
    if not db.abonnements():
        return "Aucun appareil abonné : active d'abord les notifications sur ton iPhone."
    maintenant = dt.datetime.now(pronote.PARIS)
    db.notifier(f"test-verrouille:{maintenant:%Y%m%d%H%M%S}", "🔒 Test écran verrouillé",
                f"Programmé à {maintenant:%H:%M:%S}, parti une minute plus tard comme une alerte.", "/notifications",
                dans=60)
    return ("Programmé : <strong>verrouille ton iPhone maintenant</strong>. Il part dans 1 min par le même chemin "
            "que les alertes. S'il n'arrive pas avant que tu rallumes l'écran, c'est l'iPhone qui le retient.")


@app.post("/notifications/reglages")
def regler_notifications(heure: str = Form("18:00"), silence_debut: str = Form("22:00"),
                         silence_fin: str = Form("07:30"), rappel: bool = Form(False), pronote_: bool = Form(False,
                         alias="pronote"), revisions: bool = Form(False),
                         actualites_: bool = Form(False, alias="actualites"), edt: bool = Form(False),
                         alertes_nuit: bool = Form(False)):
    for h in (heure, silence_debut, silence_fin):
        try:
            dt.time.fromisoformat(h)
        except ValueError:
            raise HTTPException(400, "heure invalide")
    notifications.enregistrer_preferences({**notifications.preferences(), "heure": heure, "silence_debut": silence_debut,
                                           "silence_fin": silence_fin, "rappel": rappel,
                                           "pronote": pronote_, "revisions": revisions,
                                           "actualites": actualites_, "edt": edt,
                                           "alertes_nuit": alertes_nuit})
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


def _quand_actu(d: dt.datetime) -> str:
    """« à 18:40 », « hier à 18:40 », « lundi à 18:40 » ou « il y a 9 jours »."""
    d = d.astimezone(pronote.PARIS)
    ecart = (dt.datetime.now(pronote.PARIS).date() - d.date()).days
    heure = d.strftime("%H:%M")
    if ecart <= 0:
        return f"de {heure}"
    if ecart == 1:
        return f"d'hier à {heure}"
    if ecart < 7:
        return f"de {['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche'][d.weekday()]} à {heure}"
    return f"d'il y a {ecart} jours"


def _categories_actu(flux_l: list) -> list[dict]:
    """Les catégories (dossiers) des sources, avec leurs articles non lus, comme dans un lecteur RSS."""
    par: dict[str, dict] = {}
    for f in flux_l:
        if f.get("categorie") and not f.get("sujet_id"):
            c = par.setdefault(f["categorie"], {"nom": f["categorie"], "non_lus": 0})
            c["non_lus"] += f.get("non_lus") or 0
    return sorted(par.values(), key=lambda c: c["nom"].lower())


TYPES_ACTU = {"article": ("", "Articles"), "audio": ("audio", "Podcasts"), "video": ("video", "Vidéos")}
PERIODES_ACTU = {"24h": (1, "24 heures"), "7j": (7, "7 jours"), "30j": (30, "30 jours")}


def marquer(texte: str | None, q: str = "") -> Markup:
    """Échappe le texte et surligne (<mark>) les mots cherchés, sans tenir compte des accents ni de la casse."""
    texte = html.escape(texte or "")
    mots = [m.strip('"') for m in re.findall(r'-?"[^"]+"|-?\S+', q or "")
            if not m.startswith("-") and m.lower() not in {"or", "ou"}]
    motifs = []
    for m in sorted({m for m in mots if len(m) >= 2}, key=len, reverse=True):
        base = "".join(c for c in unicodedata.normalize("NFD", html.escape(m).lower()) if unicodedata.category(c) != "Mn")
        motifs.append("".join(VARIANTES.get(c, re.escape(c)) for c in base))
    if not motifs:
        return Markup(texte)
    return Markup(re.sub("(" + "|".join(motifs) + ")", r"<mark>\1</mark>", texte, flags=re.I))


VARIANTES = {"a": "[aàâäáã]", "c": "[cç]", "e": "[eéèêë]", "i": "[iîïíì]", "o": "[oôöóò]", "u": "[uùûüú]", "y": "[yÿý]",
             "n": "[nñ]"}
templates.env.filters["marquer"] = marquer


def _duree_txt(secondes: int | None) -> str:
    if not secondes:
        return ""
    h, m = divmod(round(secondes / 60), 60)
    return f"{h} h {m:02d}" if h else f"{max(m, 1)} min"


templates.env.filters["duree"] = _duree_txt


@app.get("/actualites", response_class=HTMLResponse)
def page_actualites(request: Request, vue: str = "pour_toi", sujet: int | None = None, flux: int | None = None,
                    page: int = 1, article: int | None = None, q: str = "", categorie: str | None = None,
                    type: str = "", periode: str = "", non_lus: bool = False, tri: str = ""):
    vue = vue if vue in VUES_ACTU else "pour_toi"
    q = q.strip()[:200]
    categorie = (categorie or "").strip() or None
    type = type if type in TYPES_ACTU else ""
    periode = periode if periode in PERIODES_ACTU else ""
    filtres = bool(q or categorie is not None or type or periode or non_lus)
    if sujet or flux or (filtres and vue != "favoris"):
        vue = "tout"   # une recherche ou un filtre porte sur tous les articles (ou sur les favoris)
    if q:
        tri = "pertinence" if tri == "pertinence" else "recents"
    elif vue == "pour_toi" and not sujet:
        tri = "recents" if tri == "recents" else "importance"   # « Pour toi » : le plus important d'abord, jour par jour
    else:
        tri = "recents"
    page = max(page, 1)
    vraie_visite = not request.headers.get("x-rafraichir")   # pas le rafraîchissement automatique de la page
    if vraie_visite:   # ton heure de lecture
        try:
            actualites.noter_lecture()
        except Exception as e:
            print(f"actualités : heure de lecture non notée : {e}", flush=True)
    precedente = actualites.visite(noter=vraie_visite and not db.en_lecture_seule())
    liste, total = db.articles(vue, sujet, flux, PAR_PAGE_ACTU, (page - 1) * PAR_PAGE_ACTU, q=q, categorie=categorie,
                               media=TYPES_ACTU[type][0] if type else None,
                               jours=PERIODES_ACTU[periode][0] if periode else None, non_lus=non_lus, tri=tri)
    if not q:   # une recherche montre chaque article trouvé (pas de regroupement « aussi chez »)
        liste = actualites.regrouper(liste, precedente)
    else:
        liste = [{**a, "aussi": [], "image_id": a["id"], "ids": [a["id"]], "sources": 1,
                  "nouveau": bool(precedente and a["cree_le"] > precedente)} for a in liste]
    accueil = page == 1 and not sujet and not flux and not filtres and vue != "favoris"
    # « Depuis ta visite » (Inoreader, Feedly) et ce qui monte (tendances), en haut de la première page
    depuis = None
    if accueil and vue == "pour_toi" and precedente:
        depuis = {**db.actualites_depuis(precedente), "le": precedente,
                  "le_txt": _quand_actu(precedente), "absent": time.time() - precedente.timestamp() > 20 * 3600}
    tendances = db.tendances() if accueil else []
    cours = actualites.cours_lies([a.get("notion") for a in liste])
    aujourdhui = dt.datetime.now(pronote.PARIS).date()
    # « L'essentiel » : en haut de « Pour toi », première page seulement
    essentiel = None
    if vue == "pour_toi" and page == 1 and not sujet and not flux and not filtres:
        p = actualites.point()
        if p and p.get("points") and time.time() - float(p.get("le") or 0) < actualites.POINT_AFFICHE:
            sources = {a["id"]: a for a in db.articles_par_ids(sorted({i for x in p["points"] for i in x["ids"]}))}
            essentiel = {**p, "le_txt": dt.datetime.fromtimestamp(float(p["le"]), pronote.PARIS).strftime("%H:%M"),
                         "points": [{**x, "articles": [sources[i] for i in x["ids"] if i in sources]}
                                    for x in p["points"]]}
            essentiel["points"] = [{**x, "lu": all(a["lu"] for a in x["articles"]),
                                    "sources": len({a["source"] for a in x["articles"]}),
                                    "nouveau": bool(precedente and any(a["cree_le"] > precedente for a in x["articles"]))}
                                   for x in essentiel["points"] if x["articles"]]
            essentiel["nouveau"] = bool(precedente and float(p["le"]) > precedente.timestamp())
            r = p.get("a_retenir")
            if isinstance(r, dict) and r.get("notion") and r.get("texte"):
                essentiel["a_retenir"] = {**r, "cours": actualites.cours_lies([r["notion"]]).get(r["notion"])}
    if depuis and essentiel:   # « Pendant ton absence » : seulement ce que « L'essentiel » ne dit pas déjà
        vus = {a["id"] for x in essentiel["points"] for a in x["articles"]}
        vus_ev = {a.get("evenement") for x in essentiel["points"] for a in x["articles"]} - {None}
        depuis["a_la_une"] = [a for a in depuis["a_la_une"] if a["id"] not in vus and a.get("evenement") not in vus_ev]
    groupes: list[tuple[str, list]] = []
    for a in liste:
        jour = _jour_actu(a["publie_le"], aujourdhui)
        if not groupes or groupes[-1][0] != jour:
            groupes.append((jour, []))
        groupes[-1][1].append(a)
    sujets = db.sujets()
    flux_l = db.flux_liste()

    recherche = {"q": q, "categorie": categorie, "type": type, "periode": periode, "non_lus": "1" if non_lus else "",
                 "tri": "pertinence" if tri == "pertinence" else ""}
    # Dans le lien : la pertinence d'une recherche, ou « par heure » choisi à la place de l'importance de « Pour toi »
    tri_lien = recherche["tri"] or ("recents" if tri == "recents" and vue == "pour_toi" and not sujet else "")

    def lien_page(n: int) -> str:
        return "/actualites?" + urlencode({k: v for k, v in {"vue": vue, "sujet": sujet, "flux": flux, **recherche,
                                                              "tri": tri_lien, "page": n}.items() if v})
    return templates.TemplateResponse(request, "actualites.html", {
        "recherche": recherche, "filtres": filtres, "types": TYPES_ACTU, "periodes": PERIODES_ACTU,
        "categories": _categories_actu(flux_l),
        "vue": vue, "vues": VUES_ACTU, "sujet": sujet, "flux": flux, "groupes": groupes, "total": total,
        "page": page, "pages": -(-total // PAR_PAGE_ACTU), "par": PAR_PAGE_ACTU, "lien_page": lien_page,
        "sujets": sujets, "flux_liste": flux_l, "article": article, "paris": pronote.PARIS,
        "sujet_nom": next((s["nom"] for s in sujets if s["id"] == sujet), None),
        "flux_nom": next((f["titre"] or f["url"] for f in flux_l if f["id"] == flux), None),
        "en_attente": sum(1 for f in flux_l if f["releve_le"] is None),
        "reglages": actualites.reglages(), "essentiel": essentiel, "cours": cours, "tri": tri,
        "depuis": depuis, "tendances": tendances, "accueil": accueil})


@app.get("/actualites/{article_id}/image")
def actualite_image(article_id: int):
    """L'illustration de l'article, réduite et passée par le site (la page ne charge rien d'un site tiers)."""
    a = db.article(article_id)
    image = actualites.vignette(a["image"]) if a and a.get("image") else None
    if not image:
        return Response(status_code=404, headers={"Cache-Control": "private, max-age=86400"})
    return Response(image, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


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
    actualites.noter_lecture()
    return {"ok": True}


@app.get("/actualites/vigie", response_class=HTMLResponse)
def page_vigie(request: Request):
    """Page cachée (easter egg : 10 appuis rapides sur l'onglet Actualités, voir base.html) : Vigie, l'outil de veille d'un ami.
    Pas d'iframe : Vigie demande une connexion et la CSP (frame-src 'self') reste fermée ; une carte et un lien.
    La trouver débloque le style Vigie dans Réglages → Apparence."""
    nouveau = apparence.debloquer_vigie(moi())
    ap = apparence.lire(moi())
    return templates.TemplateResponse(request, "vigie.html", {   # la page cachée est toujours en Vigie sombre (aperçu)
        "url": VIGIE_URL, "nouveau": nouveau, "style": ap["style"],
        "apparence_forcee": {**ap, "style": "vigie", "theme": "sombre"}})


VIGIE_URL = "https://vigie.dix-neuf.ovh"


@app.post("/actualites/{article_id}/histoire-lue")
def actualite_histoire_lue(article_id: int):
    """« Lu » sur une carte : l'article et ses reprises chez les autres sites (la même histoire)."""
    return {"ok": True, "ids": db.histoire_lue(article_id)}


@app.post("/actualites/tout-lu")
def actualites_tout_lu(vue: str = Form("pour_toi")):
    db.articles_tout_lu(vue)
    return RedirectResponse(f"/actualites?vue={quote(vue)}", status_code=303)


@app.post("/actualites/relever", response_class=HTMLResponse)
def actualites_relever():
    db.etat_ecrire("actualites_relever", "oui")
    return "C'est demandé : les nouveaux articles arrivent d'ici une minute."


def _page_sources(request: Request, message: str = "", erreur: str = "", saisie: dict | None = None):
    flux_l = db.flux_liste()
    dossiers: dict[str, list] = {}
    for f in sorted(flux_l, key=lambda f: (f["sujet_id"] is not None, not f.get("categorie"), (f.get("categorie") or "").lower())):
        nom = "Recherches Google Actualités" if f["sujet_id"] else (f.get("categorie") or "Sans catégorie")
        dossiers.setdefault(nom, []).append(f)
    return templates.TemplateResponse(request, "actualites_sources.html", {
        "dossiers": dossiers, "noms_categories": sorted({f["categorie"] for f in flux_l if f.get("categorie")}, key=str.lower)
                                                 or [c for c in dict.fromkeys(c for _, _, c, _ in actualites.FLUX_DEFAUT)],
        "sujets": db.sujets(), "flux_liste": flux_l, "message": message, "erreur": erreur,
        "saisie": saisie or {}, "reglages": actualites.reglages(), "paris": pronote.PARIS,
        "notifs": notifications.preferences().get("actualites", True),
        "quotidien": actualites.heure_quotidienne(), "personnalise": actualites.personnalise(),
        "defaut": (defaut := actualites.profil_defaut()), "permanent": actualites.defaut_en_permanence(),
        "defaut_le": dt.datetime.fromtimestamp(float(defaut["le"]), pronote.PARIS).strftime("%d/%m à %H:%M")
                     if defaut.get("le") else "",
        "suivi": actualites.comptes_suivant_defaut() if comptes.est_admin(moi()) else None})


@app.get("/actualites/sources", response_class=HTMLResponse)
def page_actualites_page_sources(request: Request, message: str = ""):
    return _page_sources(request, message)


def _categorie(texte: str) -> str:
    return re.sub(r"\s+", " ", texte or "").strip()[:40]


@app.post("/actualites/flux", response_class=HTMLResponse)
def ajouter_flux(request: Request, url: str = Form(...), notifier: bool = Form(False), categorie: str = Form("")):
    try:
        adresse, infos, items = actualites.decouvrir(url)
    except actualites.FluxIllisible as e:
        return _page_sources(request, erreur=f"Flux non ajouté : {e}.", saisie={"url": url, "categorie": categorie})
    actualites.marquer_personnalise()
    categorie = _categorie(categorie) or actualites.categorie_connue(adresse, infos["site"] or "") \
        or ("Podcast" if items and all(i["media"] == "audio" for i in items) else "") \
        or ("YouTube" if items and all(i["media"] == "video" for i in items) else "")
    flux_id = db.flux_ajouter(adresse, infos["titre"], infos["site"] or urlsplit(adresse).hostname or "", notifier=notifier,
                              categorie=categorie)
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
    actualites.marquer_personnalise()
    db.flux_maj(flux_id, notifier=notifier)
    return RedirectResponse("/actualites/sources#flux", status_code=303)


@app.post("/actualites/flux/{flux_id}/categorie")
def flux_categorie(flux_id: int, categorie: str = Form("")):
    """Ranger une source dans une catégorie (dossier), comme dans un lecteur RSS."""
    actualites.marquer_personnalise()
    db.flux_maj(flux_id, categorie=_categorie(categorie))
    return RedirectResponse(f"/actualites/sources#f{flux_id}", status_code=303)


@app.post("/actualites/flux/{flux_id}/supprimer")
def supprimer_flux(flux_id: int):
    actualites.marquer_personnalise()
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
    actualites.marquer_personnalise()
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
    actualites.marquer_personnalise()
    db.sujet_maj(sujet_id, **champs)
    _recherche_sujet(sujet_id, recherche)
    if (champs["description"], champs["mots_cles"], champs["nom"]) != (avant["description"], avant["mots_cles"],
                                                                     avant["nom"]):
        db.articles_retrier()
    return RedirectResponse("/actualites/sources?message=Sujet+enregistré#sujets", status_code=303)


@app.post("/actualites/sujets/{sujet_id}/supprimer")
def supprimer_sujet(sujet_id: int):
    actualites.marquer_personnalise()
    db.sujet_supprimer(sujet_id)
    return RedirectResponse("/actualites/sources?message=Sujet+retiré#sujets", status_code=303)


@app.post("/actualites/suivre-defaut")
def actualites_suivre_defaut():
    """Revenir au fil par défaut (ses changements sont remplacés)."""
    actualites.suivre_defaut()
    db.etat_ecrire("actualites_relever", "oui")
    return RedirectResponse("/actualites/sources?" + urlencode(
        {"message": "Ton fil suit de nouveau le fil par défaut."}), status_code=303)


@app.post("/actualites/defaut")
def actualites_defaut(request: Request, depuis: str = Form("mon-fil")):
    """Admin : son fil (ou la sélection de départ) devient le fil par défaut de tous les comptes non personnalisés."""
    if depuis == "depart":   # la sélection de départ : son fil ne la remplace plus tout seul
        db.etat_ecrire("actualites_defaut_permanent", "non")
    p = actualites.definir_defaut(depuis_mon_fil=depuis != "depart")
    securite.noter("actualites_defaut", request, f"{'sélection de départ' if p['depart'] else 'fil de l’admin'} : "
                   f"{len(p['sujets'])} sujets, {len(p['flux'])} flux")
    db.etat_ecrire("actualites_relever", "oui")
    return RedirectResponse("/actualites/sources?" + urlencode(
        {"message": ("La sélection de départ" if p["depart"] else "Ton fil") + " est maintenant le fil par défaut : "
                    "les comptes qui n'ont rien changé le reçoivent d'ici une minute."}) + "#defaut", status_code=303)


@app.post("/actualites/defaut/permanent")
def actualites_defaut_permanent(request: Request, permanent: bool = Form(False)):
    """Admin : son fil est-il le fil par défaut en permanence (chaque changement suivi par les autres comptes) ?"""
    db.etat_ecrire("actualites_defaut_permanent", "oui" if permanent else "non")
    if permanent:
        actualites.definir_defaut()
        db.etat_ecrire("actualites_relever", "oui")
    securite.noter("actualites_defaut", request, f"fil de l’admin par défaut en permanence : {'oui' if permanent else 'non'}")
    return RedirectResponse("/actualites/sources#defaut", status_code=303)


@app.post("/actualites/reglages")
def regler_actualites(ia: bool = Form(False), notifs: bool = Form(False), quotidien: str = Form("auto"),
                      heure: str = Form("")):
    if quotidien == "heure":
        try:
            quotidien = dt.time.fromisoformat(heure).strftime("%H:%M")
        except ValueError:
            quotidien = "auto"
    actualites.enregistrer_reglages({"ia": ia, "quotidien": quotidien if quotidien in ("auto", "non") or ":" in quotidien
                                     else "auto"})
    notifications.enregistrer_preferences({**notifications.preferences(), "actualites": notifs})
    return RedirectResponse("/actualites/sources?message=Réglages+enregistrés#reglages", status_code=303)


# --- Agenda (Pronote) -------------------------------------------------------

def _agenda(request: Request, erreur: str | None = None, message: str | None = None):
    try:
        etat = pronote.Client().etat()
    except pronote.ErreurPronote:
        # Service arrêté (mise à jour, redémarrage) : l'agenda de la dernière synchro reste lisible
        etat = {"connecte": bool(db.etat_valeur(pronote.cle("pronote_synchro"))), "injoignable": True}
    ctx = {"etat": etat, "erreur": erreur, "message": message}
    if etat.get("connecte"):
        matieres = sorted({r["matiere"] for r in db.niveaux_matieres()})
        evaluations = [{**e, "cours": pronote.trouver_matiere(e["matiere"], matieres)}
                       for e in db.evenements_a_venir("evaluation", 60)]
        ctx |= {"evaluations": evaluations, "devoirs": db.evenements_a_venir("devoir", 14),
                "documents": db.documents_pronote(30),
                "notes": db.notes_recentes(15), "synchro": db.etat_valeur(pronote.cle("pronote_synchro")),
                "erreurs_synchro": db.etat_valeur(pronote.cle("pronote_erreurs")), "paris": pronote.PARIS}
        ctx |= _emploi_du_temps(evaluations)
    return templates.TemplateResponse(request, "agenda.html", ctx)


def _emploi_du_temps(evaluations: list[dict]) -> dict:
    """Emploi du temps des 2 semaines (contrôles repérés) et quoi revoir avant les prochains cours."""
    maintenant = dt.datetime.now(pronote.PARIS)
    try:
        jours = emploi_du_temps.jours(maintenant.date(), 14)
    except Exception:
        return {"edt_jours": []}
    controles = {(e["matiere"], e["le"].astimezone(pronote.PARIS).replace(second=0, microsecond=0))
                 for e in evaluations if e["id"].startswith("cours:")}
    for j in jours:
        for c in j["creneaux"]:
            c["controle"] = (c.get("matiere"), c["debut"].replace(second=0, microsecond=0)) in controles
            c["maintenant"] = c["debut"] <= maintenant < c["fin"]
    return {"edt_jours": jours, "maintenant": maintenant,
            "avance": _sans_erreur(lambda: emploi_du_temps.avant_les_cours(maintenant.date()))}


def _demander_documents():
    """Le worker récupère les documents des profs (trop long pour une page web)."""
    db.etat_ecrire(pronote.cle("pronote_demande"), "oui")   # le sien : chaque compte a son Pronote
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
        "sonde": {**(s.get("sonde") or {}), "le": _instant((s.get("sonde") or {}).get("le"))},
        "disque": _sans_erreur(disque.etat)})


@app.post("/etat/nettoyer", response_class=HTMLResponse)
def nettoyer_disque(request: Request):
    """« Libérer de la place » sous la jauge du disque : même action que le service maj (images Docker, cache)."""
    if not maj.present():
        return HTMLResponse("Le service maj ne tourne pas sur la VM : voir l'onglet Mise à jour.")
    try:
        maj.demander("nettoyer")
        securite.noter("mise_a_jour", request, "nettoyer")
    except RuntimeError as e:
        return HTMLResponse(html.escape(str(e)))
    return HTMLResponse("Nettoyage lancé : le suivi est dans l'onglet <a href=\"/mise-a-jour\">Mise à jour</a>.")


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


# --- Notes : prise de notes par matière, par cours, ou libres (notes.py) ------------------------------
# Privées par défaut ; seul l'auteur les modifie (même l'admin en « Tout voir » ne fait que les lire).

templates.env.filters["titre_note"] = notes.titre_affiche
templates.env.filters["extrait_note"] = notes.extrait
templates.env.globals["nb_notes_archivees"] = lambda: (db.nb_notes() or {}).get("archivees", 0)


def date_note(d: dt.datetime | None) -> str:
    """« à 14:05 » aujourd'hui, « hier », sinon la date."""
    if not d:
        return ""
    d = d.astimezone(pronote.PARIS)
    jours = (dt.datetime.now(pronote.PARIS).date() - d.date()).days
    if jours == 0:
        return f"à {d:%H:%M}"
    if jours == 1:
        return "hier"
    return f"{d:%d/%m}" if d.year == dt.datetime.now(pronote.PARIS).year else f"{d:%d/%m/%Y}"


templates.env.filters["date_note"] = date_note


def rendu_note(texte: str | None) -> Markup:
    return Markup(notes.cases_a_cocher(rendu_markdown(texte)))


def _matieres_note() -> list[dict]:
    """Matières proposées pour une note : celles de ses cours, des cours qu'il lit en partage, et de ses notes.
    L'année la plus récente d'abord."""
    vues: dict[tuple, dict] = {}
    sources = [db.niveaux_matieres_comptes(), db.matieres_notes()]
    if not comptes.est_admin(moi()):
        sources.append(db.bibliotheque_partagee())
    for liste in sources:
        for r in liste:
            if r.get("matiere"):
                vues.setdefault((r.get("niveau"), r["matiere"]), {"niveau": r.get("niveau"), "matiere": r["matiere"]})
    return sorted(vues.values(), key=lambda m: (-_rang_niveau(m["niveau"] or ""), m["niveau"] or "",
                                                 m["matiere"].lower()))


def _a_l_auteur(n: dict) -> bool:
    return bool(moi()) and n.get("compte_id") == moi()["id"]


def _lire_champs_note(f) -> dict:
    """Champs envoyés par l'éditeur (seulement ceux présents : l'enregistrement automatique peut n'envoyer que le texte)."""
    champs = {}
    if "titre" in f:
        champs["titre"] = (f.get("titre") or "").strip()[:notes.TITRE_MAX]
    if "contenu" in f:
        contenu = (f.get("contenu") or "").replace("\r\n", "\n")
        if len(contenu) > notes.CONTENU_MAX:
            raise HTTPException(413, "note trop longue : coupe-la en plusieurs notes")
        champs["contenu"] = contenu
    if "matiere" in f:
        valeur = f.get("matiere") or ""
        if valeur == "__autre__":
            valeur = f"{f.get('niveau_autre') or ''}|{(f.get('matiere_autre') or '').strip()}"
        champs["niveau"], champs["matiere"] = notes.matiere_choisie(valeur)
    if "document_id" in f:
        brut = str(f.get("document_id") or "")
        doc = db.cours_lisible(int(brut)) if brut.isdigit() else None
        champs["document_id"] = doc["id"] if doc else None
        if doc and not champs.get("matiere") and "matiere" not in f:
            champs["niveau"], champs["matiere"] = doc["niveau"], doc["matiere"]
    return champs


def _groupes_notes(liste: list[dict]) -> list[dict]:
    """Épinglées d'abord, puis une section par matière (l'année la plus récente d'abord), les libres à la fin."""
    epinglees = [n for n in liste if n["epinglee"]]
    par_matiere: dict[tuple, list] = defaultdict(list)
    for n in liste:
        if not n["epinglee"]:
            par_matiere[(n["niveau"], n["matiere"])].append(n)
    cles = sorted((k for k in par_matiere if k[1]), key=lambda k: (-_rang_niveau(k[0] or ""), k[1].lower()))
    groupes = [{"titre": "📌 Épinglées", "notes": epinglees, "cle": "epinglees"}] if epinglees else []
    groupes += [{"titre": k[1], "niveau": k[0], "notes": par_matiere[k], "cle": f"{k[0] or ''}|{k[1]}"} for k in cles]
    if par_matiere.get((None, None)):
        groupes.append({"titre": "Sans matière", "notes": par_matiere[(None, None)], "cle": "libres"})
    return groupes


@app.get("/notes", response_class=HTMLResponse)
def page_notes(request: Request, q: str = "", m: str = "", vue: str = ""):
    archivees = vue == "archives"
    libres = m == "libres"
    niveau, matiere = (None, None) if libres else notes.matiere_choisie(m)
    liste = db.notes(q[:200], niveau, matiere, libres, archivees)
    contexte = {"liste": liste, "groupes": [] if q.strip() or archivees else _groupes_notes(liste),
                "q": q, "m": m, "vue": "archives" if archivees else "", "nb": db.nb_notes(),
                "matieres": db.matieres_notes(), "moi_id": (moi() or {}).get("id")}
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "resultats-notes":
        return templates.TemplateResponse(request, "_notes_liste.html", contexte)
    return templates.TemplateResponse(request, "notes.html", contexte)


def _page_note(request: Request, n: dict | None, pre: dict | None = None, statut: int = 200) -> HTMLResponse:
    pre = pre or {}
    doc_id = (n or pre).get("document_id")
    cours = db.cours_lisible(doc_id) if doc_id else None
    niveau, matiere = (n or pre).get("niveau"), (n or pre).get("matiere")
    choix = _matieres_note()
    if matiere and not any(c["matiere"] == matiere and c["niveau"] == niveau for c in choix):
        choix.insert(0, {"niveau": niveau, "matiere": matiere})
    lecture = bool(n) and (not _a_l_auteur(n) or bool(apercu_en_cours()))
    return templates.TemplateResponse(request, "note.html", {
        "n": n, "pre": pre, "cours": cours, "choix": choix, "niveaux": NIVEAUX, "lecture": lecture,
        "rendu": rendu_note(n["contenu"]) if n else "",
        "cours_matiere": db.cours_pour_note(niveau, matiere) if matiere and niveau and not lecture else [],
        "a_moi_note": bool(n) and _a_l_auteur(n), "moi_id": (moi() or {}).get("id"),
        "choisi": cours["id"] if cours else None}, status_code=statut)


@app.get("/notes/nouvelle", response_class=HTMLResponse)
def nouvelle_note(request: Request, matiere: str = "", doc: int | None = None, pronote_: str = Query("", alias="pronote"),
                  jour: str = "", titre: str = ""):
    """Éditeur vide, prérempli depuis une matière, un cours (doc) ou un créneau de l'Agenda (pronote)."""
    pre = {"titre": titre.strip()[:notes.TITRE_MAX], "contenu": ""}
    pre["niveau"], pre["matiere"] = notes.matiere_choisie(matiere)
    if doc:
        cours = db.cours_lisible(doc)
        if cours:
            pre.update(document_id=cours["id"], niveau=cours["niveau"], matiere=cours["matiere"])
    elif pronote_:
        rangees = db.niveaux_matieres()
        trouvee = pronote.trouver_matiere(pronote_, sorted({r["matiere"] for r in rangees}))
        if trouvee:
            niveaux = [r["niveau"] for r in rangees if r["matiere"] == trouvee]
            pre["niveau"], pre["matiere"] = max(niveaux, key=_rang_niveau), trouvee
        try:
            quand = dt.date.fromisoformat(jour) if jour else dt.datetime.now(pronote.PARIS).date()
        except ValueError:
            quand = dt.datetime.now(pronote.PARIS).date()
        pre["titre"] = pre["titre"] or f"Cours de {matiere_lisible(pronote_)} du {quand:%d/%m}"
    return _page_note(request, None, pre)


def _reponse_auto(request: Request) -> bool:
    return request.headers.get("X-Claunote-Auto") == "1"


@app.post("/notes")
async def creer_note(request: Request):
    f = await request.form()
    champs = _lire_champs_note(f)
    cree = db.creer_note(**{k: champs.get(k) for k in ("titre", "contenu", "niveau", "matiere", "document_id")
                            if champs.get(k) is not None})
    if _reponse_auto(request):
        return JSONResponse({"id": cree["id"], "maj_le": cree["maj_le"].isoformat(), "url": f"/notes/{cree['id']}"})
    return RedirectResponse(f"/notes/{cree['id']}", status_code=303)


@app.post("/notes/apercu", response_class=HTMLResponse)
def apercu_note(contenu: str = Form("")):
    """Aperçu dans l'éditeur : le même rendu que la note enregistrée (formules, schémas, cases à cocher)."""
    rendu = rendu_note(contenu[:notes.CONTENU_MAX]) if contenu.strip() else Markup('<p class="doux">Rien à afficher pour l’instant.</p>')
    return HTMLResponse(str(rendu))


@app.get("/notes/pour-reviser", response_class=HTMLResponse)
def notes_pour_reviser(request: Request, niveau_matiere: str = "", deja_: str = Query("", alias="deja")):
    """Formulaire d'une fiche ou d'un quiz : ses notes de la matière choisie, à cocher pour les joindre."""
    niveau, matiere = notes.matiere_choisie(niveau_matiere)
    deja = {int(i) for i in deja_.split(",") if i.strip().isdigit()}
    liste = [n for n in (db.notes_matiere(niveau, matiere) if matiere else []) if n["id"] not in deja]
    return templates.TemplateResponse(request, "_notes_reviser.html", {"liste": liste, "matiere": matiere})


@app.get("/notes/cours", response_class=HTMLResponse)
def cours_de_la_matiere(request: Request, matiere: str = "", choisi: int | None = None):
    """Les cours de la matière choisie, pour y rattacher la note (chargés quand la matière change)."""
    niveau, nom = notes.matiere_choisie(matiere)
    return templates.TemplateResponse(request, "_notes_cours.html", {
        "cours_matiere": db.cours_pour_note(niveau, nom) if nom and niveau else [], "choisi": choisi,
        "moi_id": (moi() or {}).get("id")})


def _note_a_moi(note_id: int) -> dict:
    n = db.note(note_id)
    if n is None:
        raise HTTPException(404)
    if not _a_l_auteur(n):
        raise HTTPException(403, "seul l'auteur modifie sa note")
    return n


@app.get("/notes/{note_id}", response_class=HTMLResponse)
def voir_note(request: Request, note_id: int):
    n = db.note(note_id)
    if n is None:
        if db.note_partagee(note_id):   # la note publique d'un autre compte : sa page en lecture seule
            return RedirectResponse(f"/partages/note/{note_id}", status_code=303)
        raise HTTPException(404)
    return _page_note(request, n)


@app.post("/notes/{note_id}")
async def enregistrer_note(request: Request, note_id: int):
    """Enregistrement (automatique pendant la frappe, ou bouton). `base` : la version sur laquelle l'éditeur
    travaillait ; si la note a changé ailleurs entre-temps, rien n'est écrasé (409), sauf `forcer`."""
    _note_a_moi(note_id)
    f = await request.form()
    champs = _lire_champs_note(f)
    if not champs:
        raise HTTPException(400, "rien à enregistrer")
    base = None if f.get("forcer") else (_instant(str(f.get("base") or "")) or None)
    statut, valeur = db.maj_note(note_id, base, **champs)
    if statut == "absente":
        raise HTTPException(404)
    if statut == "conflit":
        if _reponse_auto(request):
            return JSONResponse({"conflit": True, "maj_le": valeur["maj_le"].isoformat(), "titre": valeur["titre"],
                                 "contenu": valeur["contenu"]}, status_code=409)
        raise HTTPException(409, "cette note a été modifiée ailleurs entre-temps : recharge la page")
    if _reponse_auto(request):
        return JSONResponse({"maj_le": valeur.isoformat(), "heure": _heure_fr(valeur)})
    return RedirectResponse(f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/visibilite")
def visibilite_note(note_id: int, valeur: str = Form(...)):
    return _visibilite("notes", note_id, valeur, f"/notes/{note_id}")


@app.post("/notes/{note_id}/{action}")
def action_note(note_id: int, action: str, suite: str = Form("")):
    _note_a_moi(note_id)
    if action == "supprimer":
        db.supprimer_note(note_id)
        return RedirectResponse(_suite_sure(suite) if suite else "/notes", status_code=303)
    actions = {"epingler": {"epinglee": True}, "detacher": {"epinglee": False},
               "archiver": {"archive_le": dt.datetime.now(dt.timezone.utc), "epinglee": False},
               "restaurer": {"archive_le": None}}
    if action not in actions:
        raise HTTPException(404)
    db.changer_note(note_id, **actions[action])
    return RedirectResponse(_suite_sure(suite) if suite else f"/notes/{note_id}", status_code=303)


@app.get("/notes/{note_id}/telecharger")
def telecharger_note(note_id: int):
    n = db.note(note_id) or db.note_partagee(note_id)
    if n is None:
        raise HTTPException(404)
    return Response(notes.markdown_complet(n), media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(notes.nom_fichier(n))}"})


@app.get("/partages/note/{note_id}", response_class=HTMLResponse)
def partage_note(request: Request, note_id: int):
    n = db.note_partagee(note_id)
    if n is None:
        raise HTTPException(404)
    cours = db.cours_lisible(n["document_id"]) if n.get("document_id") else None
    return templates.TemplateResponse(request, "partage_note.html", {"n": n, "rendu": rendu_note(n["contenu"]),
                                                                     "cours": cours})


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
        "campagnes": db.partages_campagnes(), "genres": revision.GENRES,
        "notes_partagees": db.partages_notes() if comptes.peut(moi(), "notes") else []})


@app.get("/partages/liste", response_class=HTMLResponse)
def partage_liste(request: Request, niveau: str, matiere: str, type: str):
    return templates.TemplateResponse(request, "liste.html", {
        "niveau": niveau, "matiere": matiere, "type": type, "docs": db.liste_partagee(niveau, matiere, type),
        "lecture_seule": True})


@app.get("/partages/doc/{doc_id}", response_class=HTMLResponse)
def partage_document(request: Request, doc_id: int):
    doc = db.document_partage(doc_id)
    if doc is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "partage_doc.html", {
        "doc": doc, "transcription": rendu_markdown(doc["transcription"]), "notes_cours": _notes_cours(doc_id)})


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


# --- Demandes IA des comptes sans forfait Claude : l'admin dit oui ou non (demandes_ia.py) ---

@app.get("/demandes", response_class=HTMLResponse)
def page_demandes(request: Request):
    demandes = db.demandes_ia()
    cours = db.titres_documents_tous([i for d in demandes for i in (d.get("doc_ids") or [])])
    return templates.TemplateResponse(request, "demandes.html", {
        "demandes": demandes, "cours": cours, "genres": demandes_ia.GENRES,
        "acces_attente": db.acces_cours("attente"), "acces_accordes": db.acces_cours("accorde")})


@app.post("/demandes/acces/{compte_id}")
def decider_acces(compte_id: int, reponse: str = Form(...)):
    if reponse == "retirer":
        demandes_ia.retirer_acces(compte_id)
    elif reponse in ("oui", "non"):
        demandes_ia.decider_acces(compte_id, reponse == "oui")
    else:
        raise HTTPException(400)
    comptes.oublier(compte_id)
    return RedirectResponse("/demandes", status_code=303)


@app.post("/acces-cours/demander")
def demander_acces_cours(request: Request):
    """Un invité demande à lire tous les cours de l'environnement, privés compris : l'admin dit oui ou non."""
    compte = moi()
    if not compte or comptes.est_admin(compte):
        raise HTTPException(400)
    demandes_ia.demander_acces(compte)
    comptes.oublier(compte["id"])
    return RedirectResponse("/bibliotheque#acces-cours", status_code=303)


@app.post("/demandes/{quoi}/{item_id}")
def decider_demande(quoi: str, item_id: int, reponse: str = Form(...), raison: str = Form("")):
    if quoi not in ("revision", "campagne") or reponse not in ("oui", "non"):
        raise HTTPException(400)
    demandes_ia.decider(quoi, item_id, reponse == "oui", raison)
    if reponse == "oui":
        db.demander_scan()   # le worker la prend tout de suite
    return RedirectResponse("/demandes", status_code=303)
