"""Accès par Internet (Cloudflare Tunnel), réservé aux appareils approuvés.

Par le VPN ou le réseau local, rien ne change. Une requête arrivée par Cloudflare (en-tête
`cf-connecting-ip` ou `cf-ray`, que Cloudflare ajoute toujours et qu'un visiteur ne peut pas retirer)
n'atteint le site que si l'appareil porte le cookie d'un appareil approuvé. Sinon : page « code
d'appareil ». Le code (8 caractères, 15 min, une seule fois) se crée sur Réglages → Compte depuis un
appareil déjà approuvé ou par le VPN, ou sur la VM : `docker compose exec web python -m claunote.appareils`.

Le cookie (400 jours) contient un identifiant et un secret aléatoire ; la base n'en garde qu'une empreinte.
Retirer un appareil sur la page Compte le coupe aussitôt. La connexion au site (mot de passe ou Face ID)
reste demandée en plus.

`CLAUNOTE_TUNNEL_APPAREILS=0` coupe ce contrôle (si Cloudflare Access fait déjà le tri, par exemple).
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import time

from claunote import db

COOKIE = "claunote_appareil"
DUREE = 400 * 24 * 3600          # maximum accepté par les navigateurs
CODE_DUREE = 15 * 60
CODE_ESSAIS_MAX = 20             # au-delà, le code en cours est annulé (en plus du blocage par adresse)
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # sans 0/O, 1/I/L
VU_TOUTES = 600                  # « vu le » mis à jour au plus toutes les 10 min
CLE, CLE_CODE = "appareils", "appareil_code"


def actif() -> bool:
    return os.environ.get("CLAUNOTE_TUNNEL_APPAREILS", "1").strip() != "0"


def par_tunnel(headers) -> bool:
    return "cf-connecting-ip" in headers or "cf-ray" in headers


def ip_client(request) -> str:
    """Adresse du visiteur : par le tunnel, toutes les requêtes viennent du conteneur cloudflared."""
    if par_tunnel(request.headers) and request.headers.get("cf-connecting-ip"):
        return request.headers["cf-connecting-ip"]
    return request.client.host if request.client else "?"


def _empreinte(texte: str) -> str:
    return hashlib.sha256(texte.encode()).hexdigest()


def _maintenant() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def liste() -> list[dict]:
    try:
        appareils = json.loads(db.etat_valeur(CLE) or "[]")
    except ValueError:
        return []
    for a in appareils:
        for champ in ("cree", "vu"):
            a[champ] = dt.datetime.fromisoformat(a[champ]) if a.get(champ) else None
    return appareils


def _enregistrer(appareils: list[dict]):
    brut = [{**a, **{c: a[c].isoformat(timespec="seconds") if isinstance(a.get(c), dt.datetime) else a.get(c)
                     for c in ("cree", "vu")}} for a in appareils]
    db.etat_ecrire(CLE, json.dumps(brut))


# --- Code d'ajout ---------------------------------------------------------------

def _normaliser(code: str) -> str:
    return "".join(c for c in code.upper() if c in ALPHABET)


def nouveau_code(maintenant: float | None = None) -> str:
    """Crée le code d'ajout (remplace le précédent) ; renvoyé une seule fois, la base n'en garde que l'empreinte."""
    code = "".join(secrets.choice(ALPHABET) for _ in range(8))
    db.etat_ecrire(CLE_CODE, json.dumps({"empreinte": _empreinte(code), "expire": (maintenant or time.time()) + CODE_DUREE,
                                         "echecs": 0}))
    return f"{code[:4]}-{code[4:]}"


def code_en_cours(maintenant: float | None = None) -> float | None:
    """Expiration (timestamp) du code d'ajout encore valable, s'il y en a un."""
    try:
        c = json.loads(db.etat_valeur(CLE_CODE) or "null")
    except ValueError:
        return None
    return c["expire"] if c and c["expire"] > (maintenant or time.time()) else None


def utiliser_code(code: str, nom: str, maintenant: float | None = None) -> str | None:
    """Code bon : crée l'appareil et renvoie la valeur du cookie ; sinon None."""
    try:
        c = json.loads(db.etat_valeur(CLE_CODE) or "null")
    except ValueError:
        c = None
    if not c or c["expire"] <= (maintenant or time.time()):
        return None
    if not hmac.compare_digest(_empreinte(_normaliser(code)), c["empreinte"]):
        c["echecs"] = c.get("echecs", 0) + 1
        db.etat_ecrire(CLE_CODE, None if c["echecs"] >= CODE_ESSAIS_MAX else json.dumps(c))
        return None
    db.etat_ecrire(CLE_CODE, None)   # une seule fois
    ident, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    _enregistrer(liste() + [{"id": ident, "nom": (nom or "Appareil").strip()[:60] or "Appareil",
                             "empreinte": _empreinte(secret), "cree": _maintenant(), "vu": _maintenant()}])
    return f"{ident}.{secret}"


# --- Reconnaissance -----------------------------------------------------------

def reconnu(cookie: str | None) -> dict | None:
    if not cookie or "." not in cookie:
        return None
    ident, secret = cookie.split(".", 1)
    appareils = liste()
    for a in appareils:
        if a["id"] == ident and hmac.compare_digest(a["empreinte"], _empreinte(secret)):
            vu = a.get("vu")
            if not vu or (dt.datetime.now(dt.timezone.utc) - vu).total_seconds() > VU_TOUTES:
                a["vu"] = dt.datetime.now(dt.timezone.utc)
                _enregistrer(appareils)
            return a
    return None


def retirer(ident: str):
    _enregistrer([a for a in liste() if a["id"] != ident])


if __name__ == "__main__":
    # Premier appareil, sans VPN sous la main : code créé depuis la VM
    db.init()
    print(f"Code d'ajout d'appareil : {nouveau_code()} (valable 15 min, une seule fois).\n"
          "Ouvre l'adresse Cloudflare du site sur l'appareil et tape ce code.")
