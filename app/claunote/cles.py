"""Connexion par Face ID, Touch ID ou Windows Hello : clés d'accès (passkeys, WebAuthn).

    Compte → « Ajouter cet appareil » ──► l'iPhone crée une clé (la partie secrète ne le quitte jamais)
                                            └─► le site garde la clé publique (table cles_acces)
    Connexion → « Face ID » ──► l'iPhone signe un défi ──► le site vérifie avec la clé publique ─► session

WebAuthn exige un nom de domaine (pas une adresse IP) et HTTPS (ou localhost). Sans nom :
CLAUNOTE_HOTE=claunote.10.8.0.5.sslip.io (sslip.io renvoie l'IP écrite dans le nom, rien à configurer).
"""
import ipaddress
import json
import secrets
import time

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

DUREE_DEFI = 300        # secondes pour répondre au défi
_defis: dict[str, tuple[bytes, float]] = {}   # jeton du cookie → (défi, expiration) ; un seul processus web


class CleRefusee(Exception):
    pass


def hote(host: str) -> str:
    """« claunote.lan:8443 » → « claunote.lan » (le domaine de la clé ne contient pas le port)."""
    return host.rsplit(":", 1)[0].strip("[]") if host.count(":") <= 1 or host.startswith("[") else host


def possible(host: str, https: bool) -> str | None:
    """None si les clés d'accès marchent ici, sinon la raison (adresse IP, pas de HTTPS)."""
    nom = hote(host)
    try:
        ipaddress.ip_address(nom)
        return "ip"
    except ValueError:
        pass
    if not https and nom != "localhost":
        return "http"
    return None


def nom_sslip(host: str) -> str | None:
    """Nom à mettre dans CLAUNOTE_HOTE quand le site est ouvert par une adresse IPv4."""
    try:
        ip = ipaddress.IPv4Address(hote(host))
    except ValueError:
        return None
    return f"claunote.{ip}.sslip.io"


def _nouveau_defi() -> tuple[str, bytes]:
    maintenant = time.time()
    for jeton in [j for j, (_, fin) in _defis.items() if fin < maintenant]:
        _defis.pop(jeton, None)
    jeton, defi = secrets.token_urlsafe(24), secrets.token_bytes(32)
    _defis[jeton] = (defi, maintenant + DUREE_DEFI)
    return jeton, defi


def _defi(jeton: str | None) -> bytes:
    defi, fin = _defis.pop(jeton or "", (None, 0))
    if defi is None or fin < time.time():
        raise CleRefusee("défi expiré : recommence")
    return defi


def options_inscription(host: str, utilisateur: str, existantes: list[str]) -> tuple[str, str]:
    """(jeton du défi, options JSON pour navigator.credentials.create)."""
    jeton, defi = _nouveau_defi()
    options = webauthn.generate_registration_options(
        rp_id=hote(host), rp_name="Claunote", user_name=utilisateur, user_display_name=utilisateur,
        user_id=utilisateur.encode(), challenge=defi,
        # Clé « découvrable » : à la connexion, pas besoin de taper l'identifiant
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(i)) for i in existantes])
    return jeton, webauthn.options_to_json(options)


def verifier_inscription(jeton: str | None, reponse: dict, host: str, origine: str) -> dict:
    """Vérifie la clé créée par l'appareil ; renvoie ce qu'il faut garder en base."""
    try:
        v = webauthn.verify_registration_response(
            credential=reponse, expected_challenge=_defi(jeton), expected_rp_id=hote(host),
            expected_origin=origine, require_user_verification=True)
    except CleRefusee:
        raise
    except Exception as e:
        raise CleRefusee(f"clé refusée : {e}") from e
    return {"id": bytes_to_base64url(v.credential_id), "cle_publique": v.credential_public_key,
            "compteur": v.sign_count, "rp_id": hote(host)}


def options_connexion(host: str) -> tuple[str, str]:
    jeton, defi = _nouveau_defi()
    options = webauthn.generate_authentication_options(
        rp_id=hote(host), challenge=defi, user_verification=UserVerificationRequirement.REQUIRED)
    return jeton, webauthn.options_to_json(options)


def identifiant(reponse: dict) -> str:
    return str(reponse.get("id") or "")


def verifier_connexion(jeton: str | None, reponse: dict, cle: dict, host: str, origine: str) -> int:
    """Vérifie la signature de l'appareil ; renvoie le nouveau compteur de la clé."""
    try:
        v = webauthn.verify_authentication_response(
            credential=reponse, expected_challenge=_defi(jeton), expected_rp_id=hote(host),
            expected_origin=origine, credential_public_key=bytes(cle["cle_publique"]),
            credential_current_sign_count=cle["compteur"], require_user_verification=True)
    except CleRefusee:
        raise
    except Exception as e:
        raise CleRefusee(f"connexion refusée : {e}") from e
    return v.new_sign_count


def lire(corps: bytes) -> dict:
    try:
        d = json.loads(corps)
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}
