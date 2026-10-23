"""Connexion au site : un seul compte, défini dans le .env.

CLAUNOTE_UTILISATEUR (défaut « admin ») et CLAUNOTE_MOT_DE_PASSE (défaut « claunote »).
Le mot de passe peut être écrit en clair ou haché (`python -m claunote.auth`) ;
il n'est gardé en mémoire que haché (scrypt).

La session est un cookie signé (HMAC-SHA256) : utilisateur, expiration, et une
empreinte du mot de passe, si bien que changer le mot de passe déconnecte partout.
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
        _, sel, attendu = hache.split(":")
        return hmac.compare_digest(hacher(mot_de_passe, bytes.fromhex(sel)), hache)
    except ValueError:
        return False


class Compte:
    def __init__(self, utilisateur: str, mot_de_passe: str, secret: bytes):
        self.utilisateur = utilisateur
        self.par_defaut = (utilisateur, mot_de_passe) == (DEFAUT_UTILISATEUR, DEFAUT_MOT_DE_PASSE)
        self.hash = mot_de_passe if mot_de_passe.startswith("scrypt:") else hacher(mot_de_passe)
        self.secret = secret
        self._echecs: dict[str, list[float]] = {}

    @classmethod
    def from_env(cls, secret: bytes) -> "Compte":
        return cls(os.environ.get("CLAUNOTE_UTILISATEUR") or DEFAUT_UTILISATEUR,
                   os.environ.get("CLAUNOTE_MOT_DE_PASSE") or DEFAUT_MOT_DE_PASSE, secret)

    # --- Mot de passe, avec blocage après trop d'échecs -----------------------

    def bloque(self, ip: str) -> int:
        """Secondes de blocage restantes pour cette adresse (0 si libre)."""
        recents = [t for t in self._echecs.get(ip, []) if time.time() - t < BLOCAGE]
        self._echecs[ip] = recents
        return int(BLOCAGE - (time.time() - recents[0])) + 1 if len(recents) >= ESSAIS_MAX else 0

    def verifier(self, ip: str, utilisateur: str, mot_de_passe: str) -> bool:
        ok_mdp = verifier_hash(mot_de_passe, self.hash)  # toujours calculé : même durée dans tous les cas
        ok = hmac.compare_digest(utilisateur.encode(), self.utilisateur.encode()) and ok_mdp
        if ok:
            self._echecs.pop(ip, None)
        else:
            self._echecs.setdefault(ip, []).append(time.time())
        return ok

    # --- Cookie de session ---------------------------------------------------

    def _signature(self, charge: str) -> str:
        cle = self.secret + self.hash.encode()
        return hmac.new(cle, charge.encode(), hashlib.sha256).hexdigest()

    def creer_session(self, maintenant: float | None = None) -> str:
        expire = int((maintenant or time.time()) + DUREE)
        charge = base64.urlsafe_b64encode(f"{self.utilisateur}|{expire}".encode()).decode()
        return f"{charge}.{self._signature(charge)}"

    def session_valide(self, cookie: str | None, maintenant: float | None = None) -> bool:
        if not cookie or "." not in cookie:
            return False
        charge, signature = cookie.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._signature(charge)):
            return False
        try:
            utilisateur, expire = base64.urlsafe_b64decode(charge).decode().rsplit("|", 1)
            return utilisateur == self.utilisateur and int(expire) > (maintenant or time.time())
        except ValueError:
            return False


if __name__ == "__main__":
    # Génère la valeur à mettre dans CLAUNOTE_MOT_DE_PASSE pour ne pas l'écrire en clair
    mdp = getpass.getpass("Nouveau mot de passe : ")
    if mdp != getpass.getpass("Encore une fois : "):
        raise SystemExit("Les deux mots de passe sont différents.")
    print(hacher(mdp))
