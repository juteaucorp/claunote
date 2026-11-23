"""Supervision Cloudflare : éteinte sans jeton, relève GraphQL en lecture seule, page Sécurité, alerte sur un pic."""
import datetime as dt
import json
import os
import re

import httpx
import pytest

from claunote import cloudflare, reglages
from tests.test_installation import base_reglages  # noqa: F401 (fixture)
from tests.test_site import anonyme, client  # noqa: F401 (fixtures)

ZONE, COMPTE, JETON = "a" * 32, "b" * 32, "cfut_jeton-de-test"
MAINTENANT = dt.datetime(2026, 11, 23, 20, 0, tzinfo=dt.timezone.utc)


REGLES = [{"count": 40, "dimensions": {"action": "block", "source": "firewallCustom",
                                       "description": "Claunote France seulement", "ruleId": "r1"}},
          {"count": 7, "dimensions": {"action": "block", "source": "ratelimit", "description": "/connexion",
                                      "ruleId": "r2"}},
          {"count": 5, "dimensions": {"action": "managed_challenge", "source": "securitylevel", "description": "",
                                      "ruleId": "r3"}}]
PAYS = [{"count": 30, "dimensions": {"clientCountryName": "US"}}, {"count": 12, "dimensions": {"clientCountryName": "CN"}}]
CHEMINS = [{"count": 20, "dimensions": {"clientRequestPath": "/wp-login.php"}}]
TRAFIC = [{"dimensions": {"datetime": "x"}, "sum": {"requests": 900, "threats": 4}},
          {"dimensions": {"datetime": "y"}, "sum": {"requests": 100, "threats": 1}}]
TURNSTILE = {"data": {"viewer": {"accounts": [{"turnstileAdaptiveGroups": [
    {"count": 9, "dimensions": {"eventType": "challenge"}}, {"count": 8, "dimensions": {"eventType": "solved"}}]}]}}}
FERME = "zone '{}' does not have access to the path. Refer to this page for more details about access controls"


def _bruts(n_vieux=3, n_recent=2):
    """Événements un par un (repli) : n_vieux il y a 2 h, n_recent dans les 15 dernières minutes."""
    return ([{"datetime": "2026-11-23T17:55:00Z", "action": "block", "source": "firewallCustom",
              "description": "Claunote France seulement", "clientCountryName": "US", "clientRequestPath": "/"}] * n_vieux
            + [{"datetime": "2026-11-23T19:50:00Z", "action": "block", "source": "ratelimit", "description": "",
                "clientCountryName": "FR", "clientRequestPath": "/connexion"}] * n_recent)


def _repondre(q: str, r: dict) -> dict:
    """Une requête du module → la réponse d'un faux Cloudflare (jeu de données ou champ fermé = erreur d'offre)."""
    if "accounts(" in q:
        return r["turnstile"]
    fermes = [f for f in r["fermes"] if re.search(rf"\b{f}\b", q)]
    if fermes:
        return {"data": None, "errors": [{"message": FERME.format(ZONE), "path": ["viewer", "zones", 0, fermes[0]]}]}
    if "httpRequests1hGroups" in q:
        donnees = {"t": TRAFIC}
    elif "fenetre:" in q:
        donnees = {"fenetre": [{"count": r["fenetre"]}]}
    elif "firewallEventsAdaptive(" in q:
        donnees = {"r": r["bruts"]}
    elif "clientCountryName" in q:
        donnees = {"r": PAYS}
    elif "clientRequestPath" in q:
        donnees = {"r": CHEMINS}
    else:   # seulement les champs demandés, comme le vrai
        donnees = {"r": [{"count": g["count"], "dimensions": {k: v for k, v in g["dimensions"].items()
                                                               if re.search(rf"\b{k}\b", q)}} for g in REGLES]}
    return {"data": {"viewer": {"zones": [donnees]}}}


@pytest.fixture
def api(monkeypatch):
    """Faux Cloudflare : note chaque requête GraphQL ; `fermes` = jeux de données ou champs hors de l'offre."""
    appels = []
    reponses = {"turnstile": TURNSTILE, "statut": 200, "fenetre": 3, "fermes": [], "bruts": _bruts(), "corps": None}

    def post(url, json=None, timeout=None, headers=None):
        appels.append((url, json["query"], headers["Authorization"]))
        corps = reponses["corps"] or _repondre(json["query"], reponses)
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
    assert all(a[2] == f"Bearer {JETON}" and ZONE in a[1] for a in appels) and len(appels) == 5   # petites requêtes
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
    assert len(api[0]) == 6 and e["turnstile"] == [{"n": 9, "type": "captchas affichés"}, {"n": 8, "type": "réussis"}]
    api[1]["turnstile"] = {"errors": [{"message": "not authorized for that account"}]}
    e = cloudflare.tour(compter=False, maintenant=MAINTENANT)
    assert e["turnstile"] is None and "not authorized" in e["turnstile_erreur"] and not e["erreur"]   # le reste marche


def test_pic_alerte_une_fois_par_vague(actif, api, etat, monkeypatch):
    e, journal = etat
    e["cloudflare"] = json.dumps({"fenetres": [["t", n] for n in (4, 6, 5, 3, 5)]})
    api[1]["fenetre"] = 200
    cloudflare.tour(maintenant=MAINTENANT)
    assert [j[0] for j in journal] == ["cloudflare_pic"]
    assert "200 événements en 15 min (d'habitude 5" in journal[0][1] and "France seulement" in journal[0][1]
    assert journal[0][2]["alerte"] and journal[0][2]["titre"] == "Pic d'attaques arrêté par Cloudflare"
    cloudflare.tour(maintenant=MAINTENANT + dt.timedelta(minutes=15))     # même vague : pas de 2e alerte
    assert len(journal) == 1
    api[1]["fenetre"] = 500                                   # elle double : nouvelle alerte
    cloudflare.tour(maintenant=MAINTENANT + dt.timedelta(minutes=30))
    assert len(journal) == 2


def test_seuil_du_pic():
    assert cloudflare.pic(59, []) is None and cloudflare.pic(60, []) == 60       # au moins 60
    assert cloudflare.pic(100, [30, 40, 50]) is None                              # 5 × la médiane (40) = 201
    assert cloudflare.pic(201, [30, 40, 50]) == 201


def test_jeton_refuse_note_une_fois(actif, api, etat):
    api[1]["statut"], api[1]["corps"] = 403, {"errors": [{"message": "Authentication error"}]}
    e = cloudflare.tour(maintenant=MAINTENANT)
    assert e["erreur"].startswith("Jeton refusé") and "Authentication error" in e["erreur"]
    cloudflare.tour(maintenant=MAINTENANT)
    assert [j[0] for j in etat[1]] == ["cloudflare_erreur"]   # pas une ligne par relève
    api[1]["statut"], api[1]["corps"] = 200, None
    assert not cloudflare.tour(maintenant=MAINTENANT)["erreur"]


def test_champ_description_ferme_rabattu_sur_ruleid(actif, api, etat):
    api[1]["fermes"] = ["description"]
    e = cloudflare.tour(maintenant=MAINTENANT)
    assert not e["erreur"] and e["resume"]["regles"][0]["regle"] == "r1" and not e["resume"]["manques"]


def test_offre_gratuite_groupes_fermes_repli_sur_les_evenements(actif, api, etat):
    """Cas réel : « does not have access to the path » n'est PAS un jeton refusé ; on lit ce qui est ouvert."""
    api[1]["fermes"] = ["firewallEventsAdaptiveGroups"]
    e = cloudflare.tour(maintenant=MAINTENANT)
    r = e["resume"]
    assert not e["erreur"] and r["evenements_24h"] == 5 and r["fenetre"] == 2 and r["requetes_24h"] == 1000
    assert r["regles"][0]["regle"] == "Claunote France seulement" and r["pays"][0] == {"n": 3, "pays": "US"}
    assert etat[1] == []                                      # rien au journal : ce n'est pas une panne


def test_un_jeu_de_donnees_ferme_n_empeche_pas_les_autres(actif, api, etat):
    api[1]["fermes"] = ["firewallEventsAdaptiveGroups", "firewallEventsAdaptive"]
    e = cloudflare.tour(maintenant=MAINTENANT)
    assert not e["erreur"] and e["resume"]["requetes_24h"] == 1000
    assert e["resume"]["manques"] == ["événements de sécurité"] and "fenetres" not in json.loads(etat[0]["cloudflare"])
    api[1]["fermes"] += ["httpRequests1hGroups"]                                # tout fermé
    e = cloudflare.tour(compter=False, maintenant=MAINTENANT)
    assert e["erreur"].startswith("Rien de lisible avec ton offre") and "Jeton" not in e["erreur"]
    assert etat[1] == []
    etat_, message = cloudflare.tester(JETON, ZONE)
    assert etat_ == "indisponible" and "Jeton accepté" in message and "Analytics → Lecture" not in message


def test_tester_signale_ce_qui_manque(api):
    api[1]["fermes"] = ["clientRequestPath", "httpRequests1hGroups"]
    etat_, message = cloudflare.tester(JETON, ZONE, COMPTE)
    assert etat_ == "ok" and "52 événement(s)" in message and "trafic" in message and "Turnstile lisibles" in message


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
    api[1]["statut"], api[1]["corps"] = 403, {"errors": [{"message": "Authentication error"}]}
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
