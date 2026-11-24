"""« Voir en tant que » : l'admin regarde le site exactement comme un autre compte le voit, sans rien faire à sa place.

    Comptes ──« Voir en tant que »──► cookie signé (admin, compte, 1 h) ──► chaque requête de l'admin :
        moi() = le compte choisi : ses droits, son forfait, ses cours, les cours publics, son fil d'Actualités
        base en LECTURE SEULE (default_transaction_read_only) : aucune écriture en son nom, même par erreur
        POST, PUT, DELETE refusés (sauf « Revenir à mon compte » et la déconnexion)
        bandeau en haut de chaque page, début et fin notés dans Réglages › Sécurité

Le cookie ne vaut que pour la session de l'admin qui l'a ouvert : signé avec la clé du site, son mot de passe et sa
génération (changer le mot de passe ou « Déconnecter partout » le coupe aussi). Un compte qui n'est plus admin ne
peut plus s'en servir.
"""
import base64
import hashlib
import hmac
import time

COOKIE = "claunote_apercu"
DUREE = 3600                     # une heure, puis retour automatique à son compte
# Seules requêtes qui changent quelque chose et passent pendant l'aperçu : en sortir, ou se déconnecter
SORTIES = frozenset({"/apercu/quitter", "/deconnexion"})


def _signature(secret: bytes, admin: dict, charge: str) -> str:
    cle = secret + b"apercu" + (admin.get("hash") or "").encode() + str(admin.get("generation") or 0).encode()
    return hmac.new(cle, charge.encode(), hashlib.sha256).hexdigest()


def creer(secret: bytes, admin: dict, cible_id: int, maintenant: float | None = None) -> str:
    expire = int((maintenant or time.time()) + DUREE)
    charge = base64.urlsafe_b64encode(f"{admin['id']}|{int(cible_id)}|{expire}".encode()).decode()
    return f"{charge}.{_signature(secret, admin, charge)}"


def lire(secret: bytes, cookie: str | None, admin: dict | None, maintenant: float | None = None) -> tuple[int, int] | None:
    """(compte regardé, expiration) si le cookie est valable pour cet admin, sinon None."""
    if not cookie or not admin or "." not in cookie:
        return None
    charge, signature = cookie.rsplit(".", 1)
    if not hmac.compare_digest(signature, _signature(secret, admin, charge)):
        return None
    try:
        admin_id, cible, expire = (int(x) for x in base64.urlsafe_b64decode(charge).decode().split("|"))
    except (ValueError, UnicodeDecodeError):
        return None
    if admin_id != admin["id"] or cible == admin["id"] or expire <= (maintenant or time.time()):
        return None
    return cible, expire
