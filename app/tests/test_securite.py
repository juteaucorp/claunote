"""Site exposé sur Internet : en-têtes, CSRF, pays, faux en-têtes Cloudflare, alertes, journal, blocages."""
import re

import pytest

from claunote import appareils, db, main, securite
from tests.test_site import anonyme, client  # noqa: F401 (fixtures)

CF = {"cf-connecting-ip": "203.0.113.7", "cf-ray": "abc-CDG", "cf-ipcountry": "FR"}


@pytest.fixture
def site(client):  # noqa: F811
    client.base_url = "https://testserver"
    return client


def _types(c):
    return [j["type"] for j in c.journal]


def _approuver(site, nom="iPhone"):
    code = re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}", site.post("/appareils/code").text).group(0)
    assert site.post("/appareil", headers=CF, data={"code": code, "nom": nom}, follow_redirects=False).status_code == 303


class Requete:
    """Juste ce que securite.py lit d'une requête."""
    def __init__(self, ip="203.0.113.7", pays="FR", tunnel=True):
        self.headers = {**CF, "cf-connecting-ip": ip, "cf-ipcountry": pays} if tunnel else {}
        self.client = type("C", (), {"host": "testclient"})()
        self.url = type("U", (), {"path": "/connexion"})()
        self.cookies = {}


def test_en_tetes_de_securite(site):
    r = site.get("/compte")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "strict-transport-security" not in r.headers          # pas par le VPN (certificat local)
    assert "strict-transport-security" in site.get("/appareil", headers=CF).headers
    assert site.get("/docs").status_code in (303, 404) and site.get("/openapi.json").status_code in (303, 404)


def test_csrf_refuse_un_autre_site(site):
    r = site.post("/scan", headers={"origin": "https://pirate.example"})
    assert r.status_code == 403 and "csrf" in _types(site) and site.alertes
    assert site.post("/scan", headers={"sec-fetch-site": "same-site"}).status_code == 403  # autre sous-domaine
    assert site.post("/scan", headers={"origin": "null", "sec-fetch-site": "cross-site"}).status_code == 403
    # le site lui-même passe
    assert site.post("/scan", headers={"origin": "https://testserver", "sec-fetch-site": "same-origin"}).status_code == 200
    assert site.post("/scan").status_code == 200


def test_pays_non_autorise_refuse(site, monkeypatch):
    r = site.get("/appareil", headers={**CF, "cf-ipcountry": "US"})
    assert r.status_code == 403 and "pays" in _types(site) and site.alertes
    monkeypatch.setenv("CLAUNOTE_PAYS", "FR,BE")
    assert site.get("/appareil", headers={**CF, "cf-ipcountry": "BE"}).status_code == 200
    monkeypatch.setenv("CLAUNOTE_PAYS", "*")
    assert site.get("/appareil", headers={**CF, "cf-ipcountry": "US"}).status_code == 200
    assert site.get("/compte").status_code == 200   # par le VPN, aucun filtre de pays


def test_faux_entete_cloudflare_ignore_et_signale(site, monkeypatch):
    monkeypatch.setattr(appareils, "ips_cloudflared", lambda age_max=60: {"172.18.0.9"})
    for i in range(3):   # change d'adresse à chaque essai : reste bloqué sur sa vraie adresse
        site.post("/connexion", headers={**CF, "cf-connecting-ip": f"198.51.100.{i}"},
                  data={"utilisateur": "admin", "mot_de_passe": "faux"})
    assert "entete_falsifie" in _types(site)
    assert {j["ip"] for j in site.journal if j["type"] == "entete_falsifie"} == {"testclient"}


def test_mot_de_passe_par_defaut_refuse_par_internet(site):
    _approuver(site)
    site.cookies.delete("claunote_session")
    r = site.post("/connexion", headers=CF, data={"utilisateur": "admin", "mot_de_passe": "claunote"},
                  follow_redirects=False)
    assert r.status_code == 403 and "mot de passe par défaut" in r.text and "mot_de_passe_defaut" in _types(site)


def test_appareil_ajoute_et_code_faux_alertent(site):
    site.post("/appareil", headers=CF, data={"code": "AAAA-AAAA", "nom": "x"})
    assert "code_echec" in _types(site) and any("mauvais code" in a[1] for a in site.alertes)
    _approuver(site, "iPad pirate")
    assert "appareil_ajoute" in _types(site)
    assert any("iPad pirate" in a[1] and "203.0.113.7" in a[1] for a in site.alertes)


def test_echecs_de_connexion_alertent_au_troisieme(anonyme):  # noqa: F811
    for _ in range(2):
        anonyme.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "faux"})
    assert not anonyme.alertes and _types(anonyme).count("connexion_echec") == 2
    anonyme.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "faux"})
    assert any("3 échecs" in a[1] for a in anonyme.alertes)


def test_connexion_par_internet_alerte(site, monkeypatch):
    _approuver(site)
    # mot de passe changé (sinon refusé par Internet)
    site.post("/compte", data={"actuel": "claunote", "nouveau": "un-vrai-secret", "confirmation": "un-vrai-secret"})
    assert any("mot de passe" in a[1].lower() for a in site.alertes)
    site.cookies.delete("claunote_session")
    site.alertes.clear()
    r = site.post("/connexion", headers=CF, data={"utilisateur": "admin", "mot_de_passe": "un-vrai-secret"},
                  follow_redirects=False)
    assert r.status_code == 303
    assert any(a[0] == "🔒 Connexion par Internet" and "iPhone" in a[1] for a in site.alertes)


def test_appareil_inconnu_insistant_banni(site):
    for _ in range(securite.INSISTANCE):
        site.get("/bibliotheque", headers=CF, follow_redirects=False)
    assert "banni" in _types(site) and securite.bannie("203.0.113.7")
    assert site.get("/appareil", headers=CF).status_code == 403          # même la page du code
    assert site.get("/appareil", headers={**CF, "cf-connecting-ip": "198.51.100.4"}).status_code == 200
    assert site.get("/compte").status_code == 200                         # le VPN n'est jamais banni
    site.post("/securite/debloquer")
    assert site.get("/appareil", headers=CF).status_code == 200


def test_ban_et_verrou_apres_trop_d_echecs(anonyme):  # noqa: F811
    for _ in range(securite.BAN_ECHECS):
        securite.echec("connexion_echec", Requete("203.0.113.50"))
    assert securite.bannie("203.0.113.50") and not securite.verrouille()
    for i in range(securite.VERROU_ECHECS):
        securite.echec("connexion_echec", Requete(f"198.51.100.{i}"))
    assert securite.verrouille() > 0 and "verrou" in _types(anonyme)
    # les échecs par le VPN ne ferment rien
    securite.reinitialiser()
    for _ in range(40):
        securite.echec("connexion_echec", Requete("10.8.0.2", tunnel=False))
    assert not securite.verrouille() and not securite.bannie("10.8.0.2")


def test_connexion_fermee_par_internet_pendant_le_verrou(site):
    _approuver(site)
    site.post("/compte", data={"actuel": "claunote", "nouveau": "un-vrai-secret", "confirmation": "un-vrai-secret"})
    site.cookies.delete("claunote_session")
    securite._verrou_internet["jusqu_a"] = securite.time.time() + 600
    r = site.post("/connexion", headers=CF, data={"utilisateur": "admin", "mot_de_passe": "un-vrai-secret"})
    assert r.status_code == 401 and "Trop d&#39;essais" in r.text
    # par le VPN, ça passe
    r = site.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "un-vrai-secret"}, follow_redirects=False)
    assert r.status_code == 303


def test_deconnecter_partout(site):
    ancienne = site.cookies.get("claunote_session")
    r = site.post("/securite/deconnecter-partout", follow_redirects=False)
    assert r.status_code == 303 and "deconnexion_partout" in _types(site)
    assert site.get("/compte").status_code == 200                       # cet appareil reste connecté
    from claunote import auth, comptes
    comptes.oublier()
    assert not auth.lire_session(main._cle_sessions(main.app), ancienne, comptes.trouver)   # les autres tombent


def test_deconnecter_partout_retire_les_appareils(site):
    _approuver(site, "Vieux PC")
    _approuver(site, "Cet iPhone")
    site.post("/securite/deconnecter-partout", headers=CF, data={"appareils_aussi": "1"})
    assert [a["nom"] for a in appareils.liste()] == ["Cet iPhone"]    # sauf celui d'où l'on clique


def test_page_securite(site, monkeypatch):
    monkeypatch.setattr(db, "securite_journal", lambda g, limite, decalage: ([{
        "le": main.dt.datetime.now(main.dt.timezone.utc), "type": "code_echec", "gravite": "attention",
        "ip": "203.0.113.7", "pays": "FR", "internet": True, "chemin": "/appareil", "navigateur": "Safari",
        "detail": "<b>x</b>", "alerte": True}], 1))
    monkeypatch.setattr(db, "securite_bilan", lambda: {"alertes_24h": 1, "attention_24h": 1, "alertes_7j": 1,
                                                       "echecs_7j": 4, "ips_internet_7j": 2, "derniere_alerte": None})
    r = site.get("/securite")
    assert r.status_code == 200 and "Code d&#39;appareil refusé" in r.text and "&lt;b&gt;x" in r.text
    assert "Déconnecter partout" in r.text and site.etat.get("securite_vu")


def test_erreurs_serveur_en_rafale(site, monkeypatch):
    def panne():
        raise RuntimeError("boum")
    monkeypatch.setattr(db, "nb_documents", panne)
    for _ in range(securite.ERREURS_SEUIL):
        assert site.get("/").status_code == 500
    assert "erreurs_serveur" in _types(site) and any("erreurs du serveur" in a[1] for a in site.alertes)


def test_liens_dangereux_neutralises():
    rendu = main.rendu_markdown("[clic](javascript:alert(1)) [ok](https://exemple.fr) [doc](/doc/2) "
                                "[x](java%09script:alert(1)) [y](data:text/html,x)")
    assert "javascript:" not in rendu and 'href="https://exemple.fr"' in rendu and 'href="/doc/2"' in rendu
    assert "data:text" not in rendu


def test_redirection_avec_tabulation_refusee():
    assert main._suite_sure("/\t/pirate.example") == "/"
    assert main._suite_sure("/compte?x=1") == "/compte?x=1"


def test_svg_joint_jamais_affiche(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(db, "piece_jointe", lambda i: {"id": 5, "nom": "schema.svg", "contenu": b"<svg/>", "taille": 6})
    assert client.get("/piece/5").headers["content-disposition"].startswith("attachment")
