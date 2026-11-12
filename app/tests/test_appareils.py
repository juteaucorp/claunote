"""Accès par Cloudflare Tunnel : seuls les appareils approuvés passent, le VPN ne change pas."""
import re
import time

from claunote import appareils
from tests.test_site import anonyme, client  # noqa: F401 (fixtures)

CF = {"cf-connecting-ip": "203.0.113.7", "cf-ray": "abc-CDG"}


import pytest


@pytest.fixture
def site(client):  # noqa: F811
    client.base_url = "https://testserver"   # le cookie d'appareil est « Secure » (Cloudflare sert en HTTPS)
    return client


def _code(client):
    r = client.post("/appareils/code")
    assert r.status_code == 200
    return re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}", r.text).group(0)


def test_vpn_inchange(site):
    assert site.get("/compte").status_code == 200
    assert "Appareils par Internet" in site.get("/compte").text


def test_tunnel_sans_appareil_ne_montre_que_le_code(site):
    # même avec une session valide
    r = site.get("/compte", headers=CF, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/appareil?suite=%2Fcompte")
    assert site.get("/sw.js", headers=CF, follow_redirects=False).status_code == 303
    assert site.post("/appareils/code", headers=CF).status_code == 403
    assert site.get("/", headers={**CF, "HX-Request": "true"}).headers["HX-Redirect"] == "/appareil"
    assert "Code d'appareil" in site.get("/appareil", headers=CF).text
    assert site.get("/static/logo.svg", headers=CF).status_code == 200


def test_code_approuve_un_appareil_une_seule_fois(site):
    code = _code(site)
    site.cookies.delete("claunote_session")
    r = site.post("/appareil", headers=CF, follow_redirects=False,
                    data={"code": code.lower().replace("-", " "), "nom": "iPhone", "suite": "/compte"})
    assert r.status_code == 303 and r.headers["location"] == "/compte"
    assert "secure" in r.headers["set-cookie"].lower() and "httponly" in r.headers["set-cookie"].lower()
    # l'appareil passe ; la connexion au site reste demandée
    assert site.get("/compte", headers=CF, follow_redirects=False).headers["location"].startswith("/connexion")
    # le code ne sert qu'une fois
    autre = site.cookies.get(appareils.COOKIE)
    site.cookies.delete(appareils.COOKIE)
    r = site.post("/appareil", headers=CF, data={"code": code, "nom": "Pirate"})
    assert r.status_code == 403 and "expiré" in r.text
    site.cookies.set(appareils.COOKIE, autre)
    assert [a["nom"] for a in appareils.liste()] == ["iPhone"]


def test_retirer_coupe_l_appareil(site):
    code = _code(site)
    site.post("/appareil", headers=CF, data={"code": code, "nom": "PC"})
    assert site.get("/compte", headers=CF, follow_redirects=False).status_code == 200   # session + appareil
    ident = appareils.liste()[0]["id"]
    site.post(f"/appareils/{ident}/retirer", headers=CF)
    assert site.get("/compte", headers=CF, follow_redirects=False).headers["location"].startswith("/appareil")


def test_mauvais_codes_bloques(site):
    _code(site)
    for _ in range(5):
        r = site.post("/appareil", headers=CF, data={"code": "AAAA-AAAA", "nom": "x"})
    assert "Trop d&#39;essais" in r.text
    # bloqué par l'adresse réelle du visiteur, pas celle de cloudflared : une autre adresse peut essayer
    r = site.post("/appareil", headers={**CF, "cf-connecting-ip": "198.51.100.1"}, data={"code": "AAAA-AAAA", "nom": "x"})
    assert "Trop d&#39;essais" not in r.text


def test_code_expire_et_annule_apres_trop_d_echecs(anonyme):
    code = appareils.nouveau_code(maintenant=time.time() - appareils.CODE_DUREE - 1)
    assert appareils.utiliser_code(code, "x") is None
    code = appareils.nouveau_code()
    for _ in range(appareils.CODE_ESSAIS_MAX):
        assert appareils.utiliser_code("ZZZZ-ZZZZ", "x") is None
    assert appareils.utiliser_code(code, "x") is None   # le bon code ne marche plus


def test_controle_desactivable(site, monkeypatch):
    monkeypatch.setenv("CLAUNOTE_TUNNEL_APPAREILS", "0")
    assert site.get("/compte", headers=CF).status_code == 200


def test_cookie_falsifie_refuse(anonyme):
    code = appareils.nouveau_code()
    valeur = appareils.utiliser_code(code, "iPhone")
    assert appareils.reconnu(valeur)["nom"] == "iPhone"
    ident = valeur.split(".")[0]
    assert appareils.reconnu(f"{ident}.faux") is None and appareils.reconnu("rien") is None
