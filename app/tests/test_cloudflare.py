"""Supervision Cloudflare : éteinte sans jeton, relève GraphQL en lecture seule, page Sécurité, alerte sur un pic."""
import datetime as dt
import json
import os

import httpx
import pytest

from claunote import cloudflare, reglages
from tests.test_installation import base_reglages  # noqa: F401 (fixture)
from tests.test_site import anonyme, client  # noqa: F401 (fixtures)

ZONE, COMPTE, JETON = "a" * 32, "b" * 32, "cfut_jeton-de-test"
MAINTENANT = dt.datetime(2026, 11, 23, 20, 0, tzinfo=dt.timezone.utc)


def _zone(fenetre=3):
    return {"data": {"viewer": {"zones": [{
        "regles": [{"count": 40, "dimensions": {"action": "block", "source": "firewallCustom",
                                                "description": "Claunote France seulement"}},
                   {"count": 7, "dimensions": {"action": "block", "source": "ratelimit", "description": "/connexion"}},
                   {"count": 5, "dimensions": {"action": "managed_challenge", "source": "securitylevel",
                                               "description": ""}}],
        "pays": [{"count": 30, "dimensions": {"clientCountryName": "US"}},
                 {"count": 12, "dimensions": {"clientCountryName": "CN"}}],
        "chemins": [{"count": 20, "dimensions": {"clientRequestPath": "/wp-login.php"}}],
        "fenetre": [{"count": fenetre}],
        "trafic": [{"dimensions": {"datetime": "x"}, "sum": {"requests": 900, "threats": 4}},
                   {"dimensions": {"datetime": "y"}, "sum": {"requests": 100, "threats": 1}}]}]}}}


TURNSTILE = {"data": {"viewer": {"accounts": [{"turnstileAdaptiveGroups": [
    {"count": 9, "dimensions": {"eventType": "challenge"}}, {"count": 8, "dimensions": {"eventType": "solved"}}]}]}}}


@pytest.fixture
def api(monkeypatch):
    """Faux Cloudflare : note chaque requête GraphQL, répond la réponse choisie (zone ou comptes)."""
    appels = []
    reponses = {"zone": _zone(), "turnstile": TURNSTILE, "statut": 200}

    def post(url, json=None, timeout=None, headers=None):
        appels.append((url, json["query"], headers["Authorization"]))
        corps = reponses["turnstile"] if "accounts(" in json["query"] else reponses["zone"]
        return httpx.Response(reponses["statut"], json=corps, request=httpx.Request("POST", url))
    monkeypatch.setattr(cloudflare.httpx, "post", post)
    return appels, reponses


@pytest.fixture
def etat(monkeypatch):
    e, journal = {}, []
    monkeypatch.setattr(cloudflare.db, "etat_valeur", lambda k: e.get(k))
    monkeypatch.setattr(cloudflare.db, "etat_ecrire", lambda k, v: e.update({k: v}))
    from claunote import securite
    monkeypatch.setattr(securite, "noter", lambda type, request, detail="", **k: journal.append((type, detail, k)))
    return e, journal


@pytest.fixture
def actif(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", JETON)
    monkeypatch.setenv("CLOUDFLARE_ZONE_ID", ZONE)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)


def test_eteint_sans_jeton_ni_zone(monkeypatch, api, etat):
    for cle in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID"):
        monkeypatch.delenv(cle, raising=False)
    assert not cloudflare.actif() and not cloudflare.due({})
    assert cloudflare.tour() == {} and api[0] == []          # aucun appel sans jeton
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", JETON)
    monkeypatch.setenv("CLOUDFLARE_ZONE_ID", "pas-un-id")
    assert not cloudflare.actif()


def test_releve_et_resume(actif, api, etat):
    e = cloudflare.tour(maintenant=MAINTENANT)
    appels, _ = api
    assert len(appels) == 1 and appels[0][2] == f"Bearer {JETON}" and ZONE in appels[0][1]
    assert '"2026-11-23T19:55:00Z"' in appels[0][1]           # 5 min de retard : Cloudflare compte encore
    r = e["resume"]
    assert (r["evenements_24h"], r["bloques_24h"], r["defis_24h"], r["requetes_24h"]) == (52, 47, 5, 1000)
    assert r["regles"][0] == {"n": 40, "source": "Règle personnalisée", "regle": "Claunote France seulement",
                              "action": "bloqué", "defi": False}
    assert r["regles"][1]["source"] == "Limite de débit" and r["pays"][0] == {"n": 30, "pays": "US"}
    assert json.loads(etat[0]["cloudflare"])["fenetres"] == [["2026-11-23T19:55:00+00:00", 3]]
    assert not cloudflare.due(e) and etat[1] == []            # prochaine relève dans 15 min, rien d'anormal


def test_turnstile_seulement_avec_l_identifiant_de_compte(actif, api, etat, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", COMPTE)
    e = cloudflare.tour(maintenant=MAINTENANT)
    assert len(api[0]) == 2 and e["turnstile"] == [{"n": 9, "type": "captchas affichés"}, {"n": 8, "type": "réussis"}]
    api[1]["turnstile"] = {"errors": [{"message": "not authorized for that account"}]}
    e = cloudflare.tour(compter=False, maintenant=MAINTENANT)
    assert e["turnstile"] is None and "not authorized" in e["turnstile_erreur"] and not e["erreur"]   # le reste marche


def test_pic_alerte_une_fois_par_vague(actif, api, etat, monkeypatch):
    e, journal = etat
    e["cloudflare"] = json.dumps({"fenetres": [["t", n] for n in (4, 6, 5, 3, 5)]})
    api[1]["zone"] = _zone(fenetre=200)
    cloudflare.tour(maintenant=MAINTENANT)
    assert [j[0] for j in journal] == ["cloudflare_pic"]
    assert "200 événements en 15 min (d'habitude 5" in journal[0][1] and "France seulement" in journal[0][1]
    assert journal[0][2]["alerte"] and journal[0][2]["titre"] == "Pic d'attaques arrêté par Cloudflare"
    cloudflare.tour(maintenant=MAINTENANT + dt.timedelta(minutes=15))     # même vague : pas de 2e alerte
    assert len(journal) == 1
    api[1]["zone"] = _zone(fenetre=500)                                   # elle double : nouvelle alerte
    cloudflare.tour(maintenant=MAINTENANT + dt.timedelta(minutes=30))
    assert len(journal) == 2


def test_seuil_du_pic():
    assert cloudflare.pic(59, []) is None and cloudflare.pic(60, []) == 60       # au moins 60
    assert cloudflare.pic(100, [30, 40, 50]) is None                              # 5 × la médiane (40) = 201
    assert cloudflare.pic(201, [30, 40, 50]) == 201


def test_jeton_refuse_note_une_fois(actif, api, etat):
    api[1]["statut"], api[1]["zone"] = 403, {"errors": [{"message": "Authentication error"}]}
    e = cloudflare.tour(maintenant=MAINTENANT)
    assert e["erreur"].startswith("Jeton ou permissions refusés") and "Authentication error" in e["erreur"]
    cloudflare.tour(maintenant=MAINTENANT)
    assert [j[0] for j in etat[1]] == ["cloudflare_erreur"]   # pas une ligne par relève
    api[1]["statut"], api[1]["zone"] = 200, _zone()
    assert not cloudflare.tour(maintenant=MAINTENANT)["erreur"]


def test_champ_description_absent_rabattu_sur_ruleid(actif, api, etat):
    appels, reponses = api
    zone = _zone()

    def selon(url, json=None, timeout=None, headers=None):
        appels.append(json["query"])
        corps = ({"errors": [{"message": "unknown field \"description\""}]} if "description" in json["query"] else zone)
        return httpx.Response(200, json=corps, request=httpx.Request("POST", url))
    cloudflare.httpx.post = selon   # remis par monkeypatch (fixture api)
    e = cloudflare.tour(maintenant=MAINTENANT)
    assert len(appels) == 2 and "ruleId" in appels[1] and not e["erreur"]


def test_reglages_tester_et_enregistrer(client, base_reglages, api, monkeypatch):  # noqa: F811
    r = client.post("/installation/cloudflare", data={"pays": "FR", "cf_zone": "123", "retour": "reglages"})
    assert r.status_code == 400 and "32 caractères" in r.text
    r = client.post("/installation/cloudflare", data={"pays": "FR", "cf_jeton": JETON, "cf_zone": ZONE,
                                                      "action": "tester_cf", "retour": "reglages"})
    assert r.status_code == 200 and "Jeton accepté" in r.text and "CLOUDFLARE_API_TOKEN" not in base_reglages
    r = client.post("/installation/cloudflare", data={"pays": "FR", "cf_jeton": JETON, "cf_zone": ZONE.upper(),
                                                      "retour": "reglages"})
    assert r.status_code == 200 and "Supervision allumée" in r.text
    ligne = base_reglages["CLOUDFLARE_API_TOKEN"]
    assert ligne["chiffre"] and JETON not in ligne["valeur"] and JETON not in r.text   # chiffré, jamais réaffiché
    assert base_reglages["CLOUDFLARE_ZONE_ID"]["valeur"] == ZONE and os.environ["CLOUDFLARE_API_TOKEN"] == JETON
    assert any(j["type"] == "reglage" and "supervision" in j["detail"] for j in client.journal)
    # Jeton refusé : rien n'est enregistré
    api[1]["statut"], api[1]["zone"] = 403, {"errors": [{"message": "Authentication error"}]}
    r = client.post("/installation/cloudflare", data={"pays": "FR", "cf_jeton": "autre", "cf_zone": ZONE,
                                                      "retour": "reglages"})
    assert r.status_code == 400 and "Zone → Analytics → Lecture" in r.text
    assert reglages.valeurs()["CLOUDFLARE_API_TOKEN"] == JETON


def test_page_securite(client, api, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cloudflare.db, "securite_journal", lambda g, limite, decalage: ([], 0))
    monkeypatch.setattr(cloudflare.db, "securite_bilan", lambda: {"alertes_24h": 0, "attention_24h": 0, "alertes_7j": 0,
                                                                  "echecs_7j": 0, "ips_internet_7j": 0,
                                                                  "derniere_alerte": None})
    for cle in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID"):
        monkeypatch.delenv(cle, raising=False)
    r = client.get("/securite")
    assert r.status_code == 200 and "Éteint" in r.text and 'id="cloudflare"' in r.text
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", JETON)
    monkeypatch.setenv("CLOUDFLARE_ZONE_ID", ZONE)
    r = client.post("/securite/cloudflare", follow_redirects=True)
    assert r.status_code == 200 and "Chiffres Cloudflare relevés" in r.text
    assert "Claunote France seulement" in r.text and "Limite de débit" in r.text and "/wp-login.php" in r.text
    assert "fenetres" not in json.loads(client.etat["cloudflare"])   # « Relever maintenant » ne compte pas la fenêtre
