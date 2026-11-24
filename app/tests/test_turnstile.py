"""Captcha Cloudflare Turnstile : par Internet seulement, éteint sans clés, vérifié par le serveur (siteverify)."""
import re

import pytest

from claunote import main, turnstile
from tests.test_installation import base_reglages  # noqa: F401 (fixture)
from tests.test_securite import CF, _types
from tests.test_site import anonyme, client  # noqa: F401 (fixtures)

SITE, SECRET = "0x4AAAAAAAtestSiteKey", "0x4AAAAAAAtestSecretKey"


@pytest.fixture
def site(client):  # noqa: F811
    client.base_url = "https://testserver"
    return client


@pytest.fixture
def cloudflare(monkeypatch):
    """Faux siteverify : note les appels, répond ce que le test a choisi."""
    appels, reponse = [], {"success": True, "action": "appareil", "hostname": "testserver"}

    def appel(secret, jeton, ip=None):
        appels.append((secret, jeton, ip))
        return reponse
    monkeypatch.setattr(turnstile, "_appel", appel)
    return appels, reponse


@pytest.fixture
def cles(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SITE_KEY", SITE)
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", SECRET)


def _code(site):
    return re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}", site.post("/appareils/code").text).group(0)


def test_eteint_sans_cles(site, cloudflare, monkeypatch):
    monkeypatch.delenv("TURNSTILE_SITE_KEY", raising=False)
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", SECRET)   # une seule clé : toujours éteint
    r = site.get("/appareil", headers=CF)
    assert "cf-turnstile" not in r.text and "challenges.cloudflare.com" not in r.headers["content-security-policy"]
    r = site.post("/appareil", headers=CF, data={"code": _code(site), "nom": "iPhone"}, follow_redirects=False)
    assert r.status_code == 303 and cloudflare[0] == []


def test_widget_et_csp_par_internet_seulement(site, cles):
    r = site.get("/appareil", headers=CF)
    assert f'data-sitekey="{SITE}"' in r.text and 'data-action="appareil"' in r.text
    assert SECRET not in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self' https://challenges.cloudflare.com" in csp
    assert "frame-src 'self' https://challenges.cloudflare.com" in csp
    assert "connect-src 'self'" in csp and "frame-ancestors 'self'" in csp   # le reste ne bouge pas
    # Par le VPN : pas de widget (il ne marcherait pas sur l'adresse du VPN)
    assert "cf-turnstile" not in site.get("/connexion").text


def test_par_internet_sans_captcha_refuse(site, cles, cloudflare):
    code = _code(site)
    r = site.post("/appareil", headers=CF, data={"code": code, "nom": "iPhone"}, follow_redirects=False)
    assert r.status_code == 403 and "anti-robot" in r.text and "captcha" in _types(site)
    assert cloudflare[0] == []   # pas de jeton : Cloudflare n'est même pas appelé
    # Le code n'a pas été consommé : avec le captcha, il marche
    r = site.post("/appareil", headers=CF, data={"code": code, "nom": "iPhone", turnstile.CHAMP: "jeton-ok"},
                  follow_redirects=False)
    assert r.status_code == 303
    secret, jeton, ip = cloudflare[0][0]
    assert (secret, jeton) == (SECRET, "jeton-ok")


def test_jeton_refuse_ou_d_une_autre_page(site, cles, cloudflare):
    _, reponse = cloudflare
    donnees = {"code": _code(site), "nom": "iPhone", turnstile.CHAMP: "jeton"}
    reponse.update(success=False, **{"error-codes": ["invalid-input-response"]})
    assert site.post("/appareil", headers=CF, data=donnees).status_code == 403
    reponse.update(success=True, action="connexion")   # jeton obtenu sur une autre page
    assert "autre page" in site.post("/appareil", headers=CF, data=donnees).text
    reponse.update(action="appareil", hostname="pirate.example")
    assert "autre adresse" in site.post("/appareil", headers=CF, data=donnees).text
    reponse.update(action="", hostname="testserver")   # jeton sans action : pas un de nos widgets
    assert "autre page" in site.post("/appareil", headers=CF, data=donnees).text
    reponse.update(action="appareil", hostname="")
    assert "autre adresse" in site.post("/appareil", headers=CF, data=donnees).text


def test_cloudflare_injoignable_refuse(site, cles, monkeypatch):
    import httpx

    def panne(*a, **k):
        raise httpx.ConnectError("panne")
    monkeypatch.setattr(turnstile, "_appel", panne)
    r = site.post("/appareil", headers=CF, data={"code": _code(site), "nom": "x", turnstile.CHAMP: "j"})
    assert r.status_code == 403 and "Cloudflare ne répond pas" in r.text


def test_vpn_jamais_de_captcha(anonyme, cles, cloudflare):  # noqa: F811
    r = anonyme.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "claunote"}, follow_redirects=False)
    assert r.status_code == 303 and cloudflare[0] == []


def test_connexion_et_invitation_par_internet(site, cles, cloudflare, monkeypatch):
    monkeypatch.setattr(main.appareils, "actif", lambda: False)   # on teste le captcha, pas l'appareil
    r = site.post("/connexion", headers=CF, data={"utilisateur": "admin", "mot_de_passe": "x"})
    assert r.status_code == 403 and "anti-robot" in r.text
    assert 'data-action="connexion"' in site.get("/connexion", headers=CF).text
    r = site.post("/invitation", headers=CF, data={"jeton": "AAAAA-BBBBB", "identifiant": "lea"})
    assert r.status_code == 403 and "anti-robot" in r.text


def test_cles_dans_les_reglages(site, base_reglages, monkeypatch):  # noqa: F811
    etats = []
    monkeypatch.setattr(turnstile, "tester", lambda s: (etats.pop(0), "réponse de Cloudflare"))
    assert site.post("/installation/cloudflare", data={"pays": "FR", "turnstile_site": "pas-une-cle"}).status_code == 400
    assert site.post("/installation/cloudflare", data={"pays": "FR", "turnstile_site": SITE}).status_code == 400  # sans secret
    etats.append("refuse")
    r = site.post("/installation/cloudflare", data={"pays": "FR", "turnstile_site": SITE, "turnstile_secret": SECRET})
    assert r.status_code == 400 and "TURNSTILE_SECRET_KEY" not in base_reglages and not turnstile.actif()
    etats.append("ok")
    r = site.post("/installation/cloudflare", data={"pays": "FR", "turnstile_site": SITE, "turnstile_secret": SECRET,
                                                    "retour": "reglages"})
    assert r.status_code == 200 and turnstile.actif() and "Captcha allumé" in r.text
    ligne = base_reglages["TURNSTILE_SECRET_KEY"]
    assert ligne["chiffre"] and SECRET not in ligne["valeur"] and SECRET not in r.text
    assert base_reglages["TURNSTILE_SITE_KEY"]["valeur"] == SITE
    # Secret laissé vide : gardé ; clé du site vidée : captcha éteint
    r = site.post("/installation/cloudflare", data={"pays": "FR", "turnstile_site": "", "retour": "reglages"})
    assert r.status_code == 200 and not turnstile.actif() and "TURNSTILE_SECRET_KEY" in base_reglages


def test_tester_la_cle_secrete(monkeypatch):
    monkeypatch.setattr(turnstile, "_appel", lambda s, j, ip=None: {"success": False, "error-codes": ["invalid-input-secret"]})
    assert turnstile.tester(SECRET)[0] == "refuse"
    monkeypatch.setattr(turnstile, "_appel", lambda s, j, ip=None: {"success": False, "error-codes": ["invalid-input-response"]})
    assert turnstile.tester(SECRET)[0] == "ok"
