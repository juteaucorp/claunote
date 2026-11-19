"""Connexion au site : mots de passe (scrypt), blocage après trop d'échecs, cookie de session signé.

Les comptes eux-mêmes sont dans la base (comptes.py). Le premier, l'admin, reprend à l'installation
CLAUNOTE_UTILISATEUR (défaut « admin ») et CLAUNOTE_MOT_DE_PASSE (défaut « claunote ») du .env, ou le
mot de passe déjà choisi sur la page Compte. Le mot de passe du .env peut être haché (`python -m claunote.auth`).

La session est un cookie signé (HMAC-SHA256) : compte, génération, expiration, et une empreinte du
mot de passe, si bien que changer le mot de passe déconnecte partout.
"""
import base64
import getpass
import hashlib
import hmac
import os
import secrets
import time

COOKIE = "claunote_session"
DUREE = 30 * 24 * 3600          # une connexion dure 30 jours
ESSAIS_MAX = 5                  # échecs avant blocage temporaire
BLOCAGE = 5 * 60
DEFAUT_UTILISATEUR, DEFAUT_MOT_DE_PASSE = "admin", "claunote"
_N, _R, _P = 2 ** 14, 8, 1


def hacher(mot_de_passe: str, sel: bytes | None = None) -> str:
    sel = sel or secrets.token_bytes(16)
    h = hashlib.scrypt(mot_de_passe.encode(), salt=sel, n=_N, r=_R, p=_P)
    return f"scrypt:{sel.hex()}:{h.hex()}"


def verifier_hash(mot_de_passe: str, hache: str) -> bool:
    try:
        _, sel, _ = hache.split(":")
        return hmac.compare_digest(hacher(mot_de_passe, bytes.fromhex(sel)), hache)
    except ValueError:
        return False


class Limiteur:
    """Blocage d'une adresse après trop d'échecs (mot de passe, code d'appareil, jeton d'invitation)."""

    def __init__(self):
        self._echecs: dict[str, list[float]] = {}

    def bloque(self, ip: str) -> int:
        """Secondes de blocage restantes pour cette adresse (0 si libre)."""
        recents = [t for t in self._echecs.get(ip, []) if time.time() - t < BLOCAGE]
        self._echecs[ip] = recents
        return int(BLOCAGE - (time.time() - recents[0])) + 1 if len(recents) >= ESSAIS_MAX else 0

    def echec(self, ip: str):
        self._echecs.setdefault(ip, []).append(time.time())

    def reussite(self, ip: str):
        self._echecs.pop(ip, None)


# Empreinte calculée une fois : vérifier une connexion coûte toujours un scrypt, même identifiant inconnu
_LEURRE = hacher(secrets.token_hex(8))


def verifier_compte(compte: dict | None, mot_de_passe: str) -> bool:
    """Même durée que le compte existe ou non (pas de devinette des identifiants par le temps de réponse)."""
    ok = verifier_hash(mot_de_passe, (compte or {}).get("hash") or _LEURRE)
    return bool(compte) and bool(compte.get("hash")) and ok


# --- Cookie de session ---------------------------------------------------
# « id|génération|expiration », signé avec la clé du site, le hash du mot de passe et la génération du compte :
# changer le mot de passe, « Déconnecter » le compte ou « Déconnecter partout » coupe ses sessions.

def _signature(secret: bytes, compte: dict, charge: str) -> str:
    cle = secret + (compte.get("hash") or "").encode() + str(compte.get("generation") or 0).encode()
    return hmac.new(cle, charge.encode(), hashlib.sha256).hexdigest()


def creer_session(secret: bytes, compte: dict, maintenant: float | None = None) -> str:
    expire = int((maintenant or time.time()) + DUREE)
    charge = base64.urlsafe_b64encode(f"{compte['id']}|{compte.get('generation') or 0}|{expire}".encode()).decode()
    return f"{charge}.{_signature(secret, compte, charge)}"


def lire_session(secret: bytes, cookie: str | None, trouver, maintenant: float | None = None) -> dict | None:
    """Le compte de la session (dict), ou None. `trouver(id)` lit le compte en base.

    Un ancien cookie (« admin|expiration », d'avant les comptes) reste valable pour le compte 1 :
    personne n'est déconnecté par la mise à jour."""
    if not cookie or "." not in cookie:
        return None
    charge, signature = cookie.rsplit(".", 1)
    try:
        morceaux = base64.urlsafe_b64decode(charge).decode().split("|")
    except (ValueError, UnicodeDecodeError):
        return None
    maintenant = maintenant or time.time()
    try:
        if len(morceaux) == 3:
            compte = trouver(int(morceaux[0]))
            if not compte or int(morceaux[1]) != (compte.get("generation") or 0) or int(morceaux[2]) <= maintenant:
                return None
            attendue = _signature(secret, compte, charge)
        elif len(morceaux) == 2:
            compte = trouver(1)
            if not compte or morceaux[0] != compte["identifiant"] or int(morceaux[1]) <= maintenant \
                    or compte.get("generation"):
                return None
            attendue = hmac.new(secret + (compte.get("hash") or "").encode(), charge.encode(),
                                hashlib.sha256).hexdigest()
        else:
            return None
    except ValueError:
        return None
    return compte if hmac.compare_digest(signature, attendue) else None


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["reinitialiser"]:
        # Mot de passe de l'admin oublié : celui du .env reprend la main (compte 1, réactivé, sessions coupées)
        from claunote import db
        db.init()
        mdp = os.environ.get("CLAUNOTE_MOT_DE_PASSE") or DEFAUT_MOT_DE_PASSE
        db.etat_ecrire("mot_de_passe", None)
        db.maj_compte(1, hash=mdp if mdp.startswith("scrypt:") else hacher(mdp), actif=True, role="admin",
                      mdp_defaut=mdp == DEFAUT_MOT_DE_PASSE, expire_le=None)
        db.deconnecter_compte(1)
        raise SystemExit("Mot de passe de l'admin remis à celui du .env (CLAUNOTE_MOT_DE_PASSE) : reconnecte-toi.")
    # Génère la valeur à mettre dans CLAUNOTE_MOT_DE_PASSE pour ne pas l'écrire en clair
    mdp = getpass.getpass("Nouveau mot de passe : ")
    if mdp != getpass.getpass("Encore une fois : "):
        raise SystemExit("Les deux mots de passe sont différents.")
    print(hacher(mdp))
