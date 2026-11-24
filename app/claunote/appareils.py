"""Accès par Internet (Cloudflare Tunnel), réservé aux appareils approuvés.

Par le VPN ou le réseau local, rien ne change. Une requête arrivée par Cloudflare (en-tête
`cf-connecting-ip` ou `cf-ray`, que Cloudflare ajoute toujours et qu'un visiteur ne peut pas retirer)
n'atteint le site que si l'appareil porte le cookie d'un appareil approuvé. Sinon : page « code
d'appareil ».

Chaque appareil appartient à un compte (les invités n'ont pas le VPN) :

    invitation de l'admin (nom de l'appareil) ──► le lien ouvre la création du compte par Internet,
                                                 et approuve l'appareil qui l'utilise (une seule fois)
    Réglages → Compte → « Ajouter un appareil » ──► code de 8 caractères, 15 min, une fois : à taper
                                                 sur le nouvel appareil, qui rejoint CE compte
    Comptes (admin) ──► appareils de chacun, « Retirer », code d'appareil pour quelqu'un

Un code par compte à la fois (un nouveau remplace le précédent). Sur la VM aussi :
`docker compose exec web python -m claunote.appareils [identifiant]`.

Le cookie (400 jours) contient un identifiant et un secret aléatoire ; la base n'en garde qu'une empreinte.
Retirer un appareil le coupe aussitôt ; un compte désactivé, expiré ou supprimé coupe aussi ses appareils.
La connexion au site (mot de passe ou Face ID) reste demandée en plus.

`CLAUNOTE_TUNNEL_APPAREILS=0` coupe ce contrôle (si Cloudflare Access fait déjà le tri, par exemple).
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import socket
import threading
import time

from claunote import db

COOKIE = "claunote_appareil"
DUREE = 400 * 24 * 3600          # maximum accepté par les navigateurs
CODE_DUREE = 15 * 60
CODE_ESSAIS_MAX = 20             # au-delà, les codes en cours sont annulés (en plus du blocage par adresse)
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # sans 0/O, 1/I/L
VU_TOUTES = 600                  # « vu le » mis à jour au plus toutes les 10 min
CLE, CLE_CODE = "appareils", "appareil_code"
ADMIN_ORIGINE = 1                # appareils d'avant les comptes : ceux de l'admin d'origine
# Un invité gère lui-même UN appareil en plus de son appareil par défaut (lien d'invitation, ou donné par
# l'admin) : il ne peut ni retirer celui par défaut ni en ajouter un troisième. Les admins, sans limite.
PERSO_MAX = 1
PERSO = "code"                   # origine d'un appareil ajouté par le compte lui-même (son emplacement libre)
_cloudflared = {"le": float("-inf"), "ips": set()}
# Lire, modifier, réécrire la liste d'un coup : sans ça, une requête d'un appareil (mise à jour de « vu le »)
# pouvait réécrire l'ancienne liste juste après « Retirer » et faire revenir l'appareil retiré.
_verrou = threading.Lock()


def actif() -> bool:
    return os.environ.get("CLAUNOTE_TUNNEL_APPAREILS", "1").strip() != "0"


def par_tunnel(headers) -> bool:
    return "cf-connecting-ip" in headers or "cf-ray" in headers


def ips_cloudflared(age_max: float = 60) -> set[str]:
    """Adresses du conteneur cloudflared sur le réseau Docker (relues toutes les minutes).

    CLAUNOTE_CLOUDFLARED : son nom ou son adresse s'il ne tourne pas dans ce docker-compose."""
    if time.monotonic() - _cloudflared["le"] > age_max:
        try:
            ips = {a[4][0] for a in socket.getaddrinfo(os.environ.get("CLAUNOTE_CLOUDFLARED") or "cloudflared", None)}
        except OSError:
            ips = set()
        _cloudflared.update(le=time.monotonic(), ips=ips)
    return _cloudflared["ips"]


def source_cloudflare(request) -> bool:
    """La requête vient-elle vraiment de cloudflared ? Sinon un en-tête cf-connecting-ip est inventé."""
    if not request.client:
        return False
    # cloudflared redémarré avec une nouvelle adresse : relue tout de suite plutôt qu'une fausse alerte
    return request.client.host in ips_cloudflared() or request.client.host in ips_cloudflared(age_max=5)


def ip_client(request) -> str:
    """Adresse du visiteur : par le tunnel, toutes les requêtes viennent du conteneur cloudflared.

    cf-connecting-ip n'est cru que s'il vient de cloudflared : écrit à la main depuis le réseau local,
    il permettrait de changer d'adresse à chaque essai et d'échapper au blocage."""
    if par_tunnel(request.headers) and request.headers.get("cf-connecting-ip") and source_cloudflare(request):
        return request.headers["cf-connecting-ip"]
    return request.client.host if request.client else "?"


def _empreinte(texte: str) -> str:
    return hashlib.sha256(texte.encode()).hexdigest()


def _maintenant() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def liste(frais: bool = False) -> list[dict]:
    try:
        appareils = json.loads((db.etat_frais(CLE) if frais else db.etat_valeur(CLE)) or "[]")
    except ValueError:
        return []
    for a in appareils:
        a["compte_id"] = a.get("compte_id") or ADMIN_ORIGINE
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


def _codes(maintenant: float | None = None) -> dict:
    """{"codes": [{empreinte, expire, compte_id}], "echecs": n}, sans les codes expirés.
    Ancien format (un seul code, celui de l'admin) relu tel quel."""
    try:
        c = json.loads(db.etat_frais(CLE_CODE) or "null")
    except ValueError:
        c = None
    if not c:
        return {"codes": [], "echecs": 0}
    if "codes" not in c:
        c = {"codes": [{"empreinte": c["empreinte"], "expire": c["expire"], "compte_id": ADMIN_ORIGINE}],
             "echecs": c.get("echecs", 0)}
    c["codes"] = [k for k in c["codes"] if k["expire"] > (maintenant or time.time())]
    return c


def _ecrire_codes(c: dict):
    db.etat_ecrire(CLE_CODE, json.dumps(c) if c["codes"] else None)


def nouveau_code(compte_id: int = ADMIN_ORIGINE, maintenant: float | None = None, par_admin: bool = False,
                 limite: bool = False) -> str:
    """Code d'ajout d'un appareil pour ce compte (remplace son code précédent) ; renvoyé une seule fois,
    la base n'en garde que l'empreinte. `par_admin` : l'appareil sera « par défaut » (verrouillé pour l'invité) ;
    `limite` : refusé à l'usage si l'emplacement libre du compte a été pris entre-temps."""
    code = "".join(secrets.choice(ALPHABET) for _ in range(8))
    with _verrou:
        c = _codes(maintenant)
        c["codes"] = [k for k in c["codes"] if k["compte_id"] != compte_id] + [
            {"empreinte": _empreinte(code), "expire": (maintenant or time.time()) + CODE_DUREE, "compte_id": compte_id,
             "par_admin": par_admin, "limite": limite}]
        _ecrire_codes(c)
    return f"{code[:4]}-{code[4:]}"


def code_en_cours(compte_id: int = ADMIN_ORIGINE, maintenant: float | None = None) -> float | None:
    """Expiration (timestamp) du code d'ajout encore valable de ce compte, s'il y en a un."""
    return next((k["expire"] for k in _codes(maintenant)["codes"] if k["compte_id"] == compte_id), None)


def utiliser_code(code: str, nom: str, maintenant: float | None = None) -> str | None:
    """Code bon : crée l'appareil (au compte du code) et renvoie la valeur du cookie ; sinon None."""
    with _verrou:   # deux essais en même temps : le compteur d'échecs et l'usage unique tiennent
        c = _codes(maintenant)
        bon = next((k for k in c["codes"] if hmac.compare_digest(_empreinte(_normaliser(code)), k["empreinte"])), None)
        if not bon:
            if c["codes"]:
                c["echecs"] = c.get("echecs", 0) + 1
                if c["echecs"] >= CODE_ESSAIS_MAX:
                    c = {"codes": [], "echecs": 0}   # trop d'essais : tous les codes en cours tombent
                _ecrire_codes(c)
            return None
        c["codes"].remove(bon)   # une seule fois
        _ecrire_codes(c)
        if bon.get("limite") and len(perso(bon["compte_id"], frais=True)) >= PERSO_MAX:
            return None          # emplacement libre déjà pris (autre code utilisé entre-temps)
        return _creer(nom, bon["compte_id"], "admin" if bon.get("par_admin") else PERSO)


def _creer(nom: str, compte_id: int, origine: str = PERSO) -> str:
    ident, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    _enregistrer(liste(frais=True) + [{"id": ident, "nom": (nom or "Appareil").strip()[:60] or "Appareil",
                                       "compte_id": compte_id, "origine": origine,
                                       "empreinte": _empreinte(secret), "cree": _maintenant(), "vu": _maintenant()}])
    return f"{ident}.{secret}"


def approuver_par_invitation(nom: str, compte_id: int) -> str:
    """Le lien d'invitation (une fois, 15 min) vaut approbation de l'appareil qui crée le compte."""
    with _verrou:
        return _creer(nom, compte_id, "invitation")


def du_compte(compte_id: int) -> list[dict]:
    return [a for a in liste() if a["compte_id"] == compte_id]


def par_defaut(a: dict) -> bool:
    """Appareil par défaut du compte (lien d'invitation, donné ou déplacé par l'admin) : l'invité ne le retire pas."""
    return a.get("origine") != PERSO


def perso(compte_id: int, frais: bool = False) -> list[dict]:
    """Appareils que le compte a ajoutés lui-même (son emplacement libre)."""
    return [a for a in liste(frais) if a["compte_id"] == compte_id and not par_defaut(a)]


def deplacer(ident: str, compte_id: int) -> dict | None:
    """L'admin donne un appareil à un autre compte : il y devient un appareil par défaut. Renvoie l'appareil."""
    with _verrou:
        tous = liste(frais=True)
        a = next((a for a in tous if a["id"] == ident), None)
        if a:
            a.update(compte_id=compte_id, origine="admin")
            _enregistrer(tous)
        return a


def par_compte() -> dict[int, list[dict]]:
    """Appareils de chaque compte (page Comptes de l'admin), les plus récents d'abord."""
    groupes: dict[int, list[dict]] = {}
    for a in sorted(liste(), key=lambda a: a.get("vu") or a.get("cree") or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                    reverse=True):
        groupes.setdefault(a["compte_id"], []).append(a)
    return groupes


def retirer_du_compte(compte_id: int) -> int:
    """Compte supprimé : ses appareils ne passent plus. Renvoie combien sont retirés."""
    with _verrou:
        tous = liste(frais=True)
        restent = [a for a in tous if a["compte_id"] != compte_id]
        _enregistrer(restent)
        c = _codes()
        c["codes"] = [k for k in c["codes"] if k["compte_id"] != compte_id]
        _ecrire_codes(c)
    return len(tous) - len(restent)


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
                _marquer_vu(ident)
            return a
    return None


def _marquer_vu(ident: str):
    with _verrou:
        appareils = liste(frais=True)
        for a in appareils:
            if a["id"] == ident:   # encore là (pas retiré entre-temps) : seulement sa date change
                a["vu"] = dt.datetime.now(dt.timezone.utc)
                _enregistrer(appareils)
                return


def retirer(ident: str):
    with _verrou:
        _enregistrer([a for a in liste(frais=True) if a["id"] != ident])


if __name__ == "__main__":
    # Premier appareil, sans VPN sous la main : code créé depuis la VM (pour l'admin, ou le compte nommé)
    import sys
    db.init()
    compte = db.compte_par_identifiant(sys.argv[1]) if len(sys.argv) > 1 else db.compte(ADMIN_ORIGINE)
    if not compte:
        sys.exit(f"Compte « {sys.argv[1]} » inconnu.")
    print(f"Code d'ajout d'appareil pour @{compte['identifiant']} : {nouveau_code(compte['id'])} "
          "(valable 15 min, une seule fois).\nOuvre l'adresse Cloudflare du site sur l'appareil et tape ce code.")
