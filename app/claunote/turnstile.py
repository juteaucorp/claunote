"""Captcha Cloudflare Turnstile sur les pages ouvertes sans connexion, PAR INTERNET seulement.

    navigateur ──► widget Turnstile (iframe challenges.cloudflare.com) ──► jeton caché cf-turnstile-response
        │
        └─► POST /connexion, /appareil, /invitation ──► siteverify (serveur → Cloudflare) ──► ok ? suite : refus

- Éteint tant que les DEUX clés (TURNSTILE_SITE_KEY + TURNSTILE_SECRET_KEY) ne sont pas saisies
  (Réglages → Installation → Accès par Internet, ou l'assistant). La clé secrète est chiffrée comme les autres jetons.
- Par le VPN (ou le réseau local) : jamais de captcha. Le widget n'est valable que sur le nom de domaine déclaré
  chez Cloudflare (celui du tunnel) : affiché sur l'adresse du VPN, il échouerait et bloquerait la connexion.
- Face ID (clé d'accès) n'en a pas besoin : une clé d'accès ne se devine pas.
"""
import os
import re

import httpx

from claunote import appareils

SCRIPT = "https://challenges.cloudflare.com/turnstile/v0/api.js"
SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
ORIGINE = "https://challenges.cloudflare.com"
CHAMP = "cf-turnstile-response"
DELAI = 10   # secondes pour la réponse de Cloudflare


def cle_site() -> str:
    return (os.environ.get("TURNSTILE_SITE_KEY") or "").strip()


def _secret() -> str:
    return (os.environ.get("TURNSTILE_SECRET_KEY") or "").strip()


def actif() -> bool:
    return bool(cle_site() and _secret())


def requis(request) -> bool:
    """Captcha demandé pour cette requête : clés saisies et visite par Internet (Cloudflare Tunnel)."""
    return actif() and appareils.par_tunnel(request.headers)


def _appel(secret: str, jeton: str, ip: str | None = None) -> dict:
    donnees = {"secret": secret, "response": jeton}
    if ip and ip != "?":
        donnees["remoteip"] = ip
    r = httpx.post(SITEVERIFY, data=donnees, timeout=DELAI)
    r.raise_for_status()
    return r.json()


def verifier(request, jeton: str | None, action: str) -> str | None:
    """None si la personne a passé le captcha (ou s'il n'est pas demandé ici), sinon le message à afficher."""
    if not requis(request):
        return None
    jeton = (jeton or "").strip()
    if not jeton or len(jeton) > 2048:
        return "Vérification anti-robot pas terminée : attends la coche verte, puis réessaie."
    try:
        r = _appel(_secret(), jeton, appareils.ip_client(request))
    except (httpx.HTTPError, ValueError):
        return "Cloudflare ne répond pas pour la vérification anti-robot : réessaie dans un instant (ou passe par le VPN)."
    if not r.get("success"):
        codes = r.get("error-codes") or []
        if "timeout-or-duplicate" in codes:
            return "Vérification anti-robot expirée : recharge la page et réessaie."
        return "Vérification anti-robot refusée : recharge la page et réessaie."
    # Le jeton doit venir de CE formulaire et de CE site (pas d'un jeton obtenu ailleurs puis rejoué ici)
    # Nos widgets ont tous une action : un jeton sans action (ou d'une autre page) n'a pas été obtenu ici
    if (r.get("action") or "") != action:
        return "Vérification anti-robot faite pour une autre page : recharge la page et réessaie."
    hote = request.headers.get("host", "").rsplit(":", 1)[0].lower()
    if (r.get("hostname") or "").lower() != hote:
        return "Vérification anti-robot faite sur une autre adresse : recharge la page et réessaie."
    return None


CLE = re.compile(r"^[0-9]x[A-Za-z0-9_-]{10,100}$")   # 0x4AAAA… (et les clés d'essai 1x000…, 2x000…, 3x000…)


def format_ok(cle: str) -> bool:
    return bool(CLE.match(cle or ""))


def tester(secret: str) -> tuple[str, str]:
    """Clé secrète acceptée par Cloudflare ? (« ok », « refuse » ou « injoignable », message)

    Un faux jeton suffit : Cloudflare dit si c'est le secret ou le jeton qui cloche, sans rien compter."""
    try:
        r = _appel(secret, "essai-claunote")
    except (httpx.HTTPError, ValueError) as e:
        return "injoignable", f"Cloudflare ne répond pas pour vérifier la clé secrète ({type(e).__name__})."
    codes = r.get("error-codes") or []
    if r.get("success") or "invalid-input-response" in codes or "timeout-or-duplicate" in codes:
        return "ok", "Clé secrète Turnstile acceptée par Cloudflare."
    if "invalid-input-secret" in codes:
        return "refuse", ("Clé secrète Turnstile refusée par Cloudflare : recopie-la "
                          "(Turnstile → ton widget → Clé secrète).")
    return "injoignable", f"Réponse inattendue de Cloudflare : {', '.join(codes) or 'aucun code'}."
