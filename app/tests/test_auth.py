import base64
import hashlib
import hmac
import time

from claunote import auth

COMPTE = {"id": 3, "identifiant": "camille", "hash": auth.hacher("claunote"), "generation": 0}


def trouver(compte):
    return lambda i: dict(compte) if i == compte["id"] else None


def test_hash_scrypt():
    h = auth.hacher("claunote")
    assert h.startswith("scrypt:") and "claunote" not in h and "$" not in h  # « $ » casse le .env de compose
    assert auth.verifier_hash("claunote", h) and not auth.verifier_hash("autre", h)
    assert not auth.verifier_hash("x", "pas-un-hash")


def test_verifier_compte():
    assert auth.verifier_compte(COMPTE, "claunote") and not auth.verifier_compte(COMPTE, "autre")
    assert not auth.verifier_compte(None, "claunote") and not auth.verifier_compte({**COMPTE, "hash": None}, "x")


def test_session_signee_et_limitee_dans_le_temps():
    s = auth.creer_session(b"s", COMPTE)
    assert auth.lire_session(b"s", s, trouver(COMPTE))["id"] == 3
    assert not auth.lire_session(b"s", s[:-1] + ("0" if s[-1] != "0" else "1"), trouver(COMPTE))
    assert not auth.lire_session(b"s", s, trouver(COMPTE), maintenant=time.time() + auth.DUREE + 1)
    assert not auth.lire_session(b"s", None, trouver(COMPTE))
    assert not auth.lire_session(b"s", "n'importe quoi", trouver(COMPTE))
    assert not auth.lire_session(b"autre", s, trouver(COMPTE))
    assert not auth.lire_session(b"s", s, lambda i: None)                          # compte supprimé


def test_changer_le_mot_de_passe_ou_deconnecter_le_compte():
    s = auth.creer_session(b"s", COMPTE)
    assert not auth.lire_session(b"s", s, trouver({**COMPTE, "hash": auth.hacher("nouveau")}))
    assert not auth.lire_session(b"s", s, trouver({**COMPTE, "generation": 1}))


def test_un_compte_ne_peut_pas_prendre_la_session_d_un_autre():
    s = auth.creer_session(b"s", COMPTE)
    charge, sig = s.rsplit(".", 1)
    autre = base64.urlsafe_b64encode(base64.urlsafe_b64decode(charge).replace(b"3|", b"1|", 1)).decode()
    admin = {**COMPTE, "id": 1, "identifiant": "admin"}
    assert not auth.lire_session(b"s", f"{autre}.{sig}", trouver(admin))


def test_ancien_cookie_valable_pour_l_admin():
    """Cookie d'avant les comptes (« admin|expiration ») : l'admin n'est pas déconnecté par la mise à jour."""
    admin = {"id": 1, "identifiant": "admin", "hash": auth.hacher("claunote"), "generation": 0}
    charge = base64.urlsafe_b64encode(f"admin|{int(time.time()) + 60}".encode()).decode()
    sig = hmac.new(b"s" + admin["hash"].encode(), charge.encode(), hashlib.sha256).hexdigest()
    assert auth.lire_session(b"s", f"{charge}.{sig}", trouver(admin))["id"] == 1
    assert not auth.lire_session(b"s", f"{charge}.{sig}", trouver({**admin, "generation": 1}))


def test_blocage_apres_trop_d_echecs():
    lim = auth.Limiteur()
    for _ in range(auth.ESSAIS_MAX):
        assert lim.bloque("1.2.3.4") == 0
        lim.echec("1.2.3.4")
    assert lim.bloque("1.2.3.4") > 0 and lim.bloque("5.6.7.8") == 0
    lim.reussite("1.2.3.4")
    assert lim.bloque("1.2.3.4") == 0
