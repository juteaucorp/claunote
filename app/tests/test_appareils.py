"""Accès par Cloudflare Tunnel : seuls les appareils approuvés passent, le VPN ne change pas."""
import re
import time

from claunote import appareils
from tests.test_site import anonyme, client  # noqa: F401 (fixtures)

CF = {"cf-connecting-ip": "203.0.113.7", "cf-ray": "abc-CDG", "cf-ipcountry": "FR"}


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
    assert "Mes appareils par Internet" in site.get("/compte").text


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


def test_appareil_retire_ne_revient_pas(site):
    """« vu le » mis à jour juste après « Retirer » : l'appareil retiré n'est pas réécrit dans la liste."""
    valeur = appareils.utiliser_code(_code(site), "iPhone volé")
    ident = valeur.split(".")[0]
    appareils.retirer(ident)
    appareils._marquer_vu(ident)   # requête de cet appareil commencée avant le retrait
    assert ident not in [a["id"] for a in appareils.liste()]
    assert appareils.reconnu(valeur) is None


# --- Appareils reliés aux comptes (les invités n'ont pas le VPN) ------------------------

from tests.test_comptes import MDP, _creer, _inviter, _jeton  # noqa: E402


def _invite_par_internet(site, identifiant="camille"):
    """L'admin invite (par le VPN) ; la personne ouvre le lien par Internet, sans appareil approuvé."""
    jeton = _inviter(site)
    c = TestClient(main.app, base_url="https://testserver")
    r = c.get("/invitation", params={"jeton": jeton}, headers=CF, follow_redirects=False)
    assert r.status_code == 200 and "iPhone de Camille" in r.text and "par Internet" in r.text
    r = c.post("/invitation", headers=CF, follow_redirects=False, data={
        "jeton": jeton, "identifiant": identifiant, "mot_de_passe": MDP, "confirmation": MDP, "accepte": "1"})
    assert r.status_code == 303
    return c


from fastapi.testclient import TestClient  # noqa: E402

from claunote import main  # noqa: E402


def test_le_lien_d_invitation_approuve_l_appareil(site, faux_comptes):
    # sans lien valable, rien ne passe par Internet
    c = TestClient(main.app, base_url="https://testserver")
    assert c.get("/invitation", headers=CF, follow_redirects=False).status_code == 303
    assert c.get("/invitation", params={"jeton": "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"}, headers=CF,
                 follow_redirects=False).status_code == 303
    invite = _invite_par_internet(site)
    a, = appareils.liste()
    assert a["nom"] == "iPhone de Camille" and a["compte_id"] == 2 and a["origine"] == "invitation"
    assert invite.get("/compte", headers=CF, follow_redirects=False).status_code == 200   # appareil + session
    assert any("iPhone de Camille" in corps and "@camille" in corps for _, corps in site.alertes)
    # l'admin voit l'appareil sous le compte, avec « Retirer »
    page = site.get("/comptes").text
    assert "iPhone de Camille" in page and "1 appareil par Internet" in page


def test_invitation_par_le_vpn_ne_cree_pas_d_appareil(site, faux_comptes):
    _creer(_inviter(site))
    assert appareils.liste() == []


def test_nom_de_l_appareil_obligatoire(site):
    r = site.post("/comptes/inviter", data={"role": "invite", "droits": ["bibliotheque"], "appareil": "  "})
    assert r.status_code == 400 and "nom de l&#39;appareil" in r.text


def test_chacun_ajoute_un_appareil_a_son_compte(site, faux_comptes):
    invite = _invite_par_internet(site)
    r = invite.post("/appareils/code", headers=CF)
    assert r.status_code == 200 and "Mes appareils par Internet" in r.text
    code = re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}", r.text).group(0)
    # l'admin aussi a un code en cours : chacun le sien, aucun ne remplace l'autre
    code_admin = _code(site)
    nouveau = TestClient(main.app, base_url="https://testserver")
    assert nouveau.post("/appareil", headers=CF, data={"code": code, "nom": "iPad"},
                        follow_redirects=False).status_code == 303
    assert {a["nom"]: a["compte_id"] for a in appareils.liste()} == {"iPhone de Camille": 2, "iPad": 2}
    assert appareils.code_en_cours(1) and not appareils.code_en_cours(2)
    assert appareils.utiliser_code(code_admin, "PC")
    # sa page ne montre que ses appareils
    page = invite.get("/compte", headers=CF).text
    assert "iPad" in page and "PC" not in page.split("Mes appareils par Internet")[1].split("</div>")[0]


def test_un_invite_ne_retire_que_ses_appareils(site, faux_comptes):
    invite = _invite_par_internet(site)
    valeur = appareils.utiliser_code(_code(site), "PC de l'admin")
    admin_pc = valeur.split(".")[0]
    assert invite.post(f"/appareils/{admin_pc}/retirer", headers=CF).status_code == 404
    assert admin_pc in [a["id"] for a in appareils.liste()]
    sien = next(a["id"] for a in appareils.liste() if a["compte_id"] == 2)
    # l'admin, lui, retire l'appareil de n'importe qui depuis la page Comptes
    r = site.post(f"/appareils/{sien}/retirer", data={"suite": "comptes"}, follow_redirects=False)
    assert r.headers["location"].startswith("/comptes?")
    assert invite.get("/compte", headers=CF, follow_redirects=False).headers["location"].startswith("/appareil")


def test_admin_cree_un_code_pour_quelqu_un(site, faux_comptes):
    _creer(_inviter(site))
    r = site.post("/comptes/2/appareil-code")
    assert r.status_code == 200 and "Code d'appareil pour @camille" in r.text
    code = re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}", r.text.split("id=\"code-appareil\"")[1]).group(0)
    appareils.utiliser_code(code, "Nouveau téléphone")
    assert appareils.liste()[0]["compte_id"] == 2
    assert faux_comptes and site.post("/comptes/99/appareil-code").status_code == 404


def test_compte_desactive_ou_supprime_coupe_ses_appareils(site, faux_comptes):
    invite = _invite_par_internet(site)
    cookie = invite.cookies.get(appareils.COOKIE)
    faux_comptes.comptes[2]["actif"] = False
    main.comptes.oublier(2)
    c = TestClient(main.app, base_url="https://testserver", cookies={appareils.COOKIE: cookie})
    assert c.get("/connexion", headers=CF, follow_redirects=False).headers["location"].startswith("/appareil")
    faux_comptes.comptes[2]["actif"] = True
    main.comptes.oublier(2)
    site.post("/comptes/2/supprimer", data={"confirmation": "camille"})
    assert appareils.liste() == []


def test_lien_mot_de_passe_demande_un_appareil_par_internet(site, faux_comptes):
    _creer(_inviter(site))
    jeton = _jeton(site.post("/comptes/2/mot-de-passe").text)
    c = TestClient(main.app, base_url="https://testserver")
    assert c.get("/invitation", params={"jeton": jeton}, headers=CF, follow_redirects=False).status_code == 303
    r = c.post("/invitation", headers=CF, data={"jeton": jeton, "mot_de_passe": MDP + "x", "confirmation": MDP + "x"})
    assert r.status_code == 403 and "appareil déjà approuvé" in r.text


def test_ancien_code_et_anciens_appareils_restent_a_l_admin(anonyme):
    import json
    anonyme.etat[appareils.CLE_CODE] = json.dumps({"empreinte": appareils._empreinte("ABCDEFGH"),
                                                   "expire": time.time() + 60, "echecs": 0})
    anonyme.etat[appareils.CLE] = json.dumps([{"id": "x1", "nom": "iPhone", "empreinte": "e", "cree": None, "vu": None}])
    assert appareils.liste()[0]["compte_id"] == 1
    valeur = appareils.utiliser_code("abcd-efgh", "Mac")
    assert appareils.reconnu(valeur)["compte_id"] == 1


# --- Appareil par défaut (verrouillé) + un emplacement libre ; l'admin déplace ses appareils -------

def test_admin_deplace_un_appareil_vers_un_compte(site, faux_comptes):
    _creer(_inviter(site))
    valeur = appareils.utiliser_code(_code(site), "iPad de la maison")
    ident = valeur.split(".")[0]
    assert appareils.reconnu(valeur)["compte_id"] == 1
    r = site.post(f"/comptes/appareils/{ident}/deplacer", data={"compte": "2"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/comptes?")
    a = appareils.reconnu(valeur)
    assert a["compte_id"] == 2 and appareils.par_defaut(a)
    assert site.post(f"/comptes/appareils/{ident}/deplacer", data={"compte": "99"}).status_code == 404
    assert "Déplacer vers" in site.get("/comptes").text


def test_invite_ne_retire_pas_son_appareil_par_defaut_et_n_a_qu_un_emplacement(site, faux_comptes):
    invite = _invite_par_internet(site)
    defaut = appareils.liste()[0]
    assert appareils.par_defaut(defaut)
    assert invite.post(f"/appareils/{defaut['id']}/retirer", headers=CF).status_code == 403
    assert "géré par l'admin" in invite.get("/compte", headers=CF).text
    # son emplacement libre
    code = re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}", invite.post("/appareils/code", headers=CF).text).group(0)
    second = appareils.utiliser_code(code, "PC")
    assert second and not appareils.par_defaut(appareils.reconnu(second))
    r = invite.post("/appareils/code", headers=CF)
    assert r.status_code == 400 and "emplacement libre est pris" in r.text
    # il le retire lui-même, puis peut en ajouter un autre
    assert invite.post(f"/appareils/{second.split('.')[0]}/retirer", headers=CF).status_code == 200
    assert invite.post("/appareils/code", headers=CF).status_code == 200
    # l'admin, lui, n'a pas de limite et retire n'importe quel appareil
    _code(site)
    _code(site)
    assert site.post(f"/appareils/{defaut['id']}/retirer", data={"suite": "comptes"},
                     follow_redirects=False).status_code == 303


def test_deux_codes_ne_donnent_pas_deux_emplacements(anonyme):
    c1 = appareils.nouveau_code(2, limite=True)
    assert appareils.utiliser_code(c1, "A")
    c2 = appareils.nouveau_code(2, limite=True)   # créé avant le contrôle de la page : refusé à l'usage
    assert appareils.utiliser_code(c2, "B") is None
    c3 = appareils.nouveau_code(2, par_admin=True)  # l'admin, lui, peut toujours en donner un
    assert appareils.par_defaut(appareils.reconnu(appareils.utiliser_code(c3, "C")))
