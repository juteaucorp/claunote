"""Profil de chaque compte : lu par l'IA (parcours dans toutes les consignes, profil dans les créations)."""
import datetime as dt
import json

import pytest

from claunote import agent, db, notifications as nt, profil, revision
from tests.test_site import anonyme, client  # noqa: F401  (fixtures du site)


@pytest.fixture
def etat(monkeypatch):
    e = {}
    monkeypatch.setattr(db, "etat_valeur", lambda k: e.get(k))
    monkeypatch.setattr(db, "etat_ecrire", lambda k, v: e.update({k: v}))
    monkeypatch.setattr(db, "formation_du_compte", lambda i: {"niveau": "BTS2", "nom": "BTS SIO option SISR"}
                        if i == 1 else None)
    return e


def test_profil_vide_garde_les_consignes(etat):
    assert profil.bloc(1) == ""
    assert profil.parcours(1) == "(aujourd'hui en BTS SIO option SISR)"   # tiré de ses formations
    assert profil.parcours(2) == profil.PAR_DEFAUT
    consigne = revision.prompt("fiche", "VLAN")
    assert "(du collège au BTS SIO)" in consigne   # la consigne d'origine, adaptée seulement à l'appel
    adaptee = profil.adapter(consigne, "fiche", 2)
    assert "BTS SIO)" not in adaptee.split("\n")[0] and profil.PAR_DEFAUT in adaptee
    assert adaptee.startswith("Tu aides un élève")   # profil vide : rien en tête


def test_profil_rempli_lu_dans_les_creations(etat):
    p = profil.enregistrer(2, {"formation": "  Terminale\ngénérale ", "alternance": "oui", "entreprise": "garage",
                               "explications": "pas_a_pas", "detail": "nimporte", "exemples": "le foot",
                               "libre": "Je suis dyslexique.\n\n\n\nJe révise dans le train.\x07", "inconnu": "x"})
    assert p["formation"] == "Terminale générale" and p["detail"] == "equilibre" and "inconnu" not in p
    assert "\x07" not in p["libre"] and "\n\n\n" not in p["libre"]
    assert profil.parcours(2) == "(aujourd'hui en Terminale générale, en alternance (garage))"
    b = profil.bloc(2)
    assert "pas des règles" in b and "le foot" in b and "    > Je suis dyslexique." in b and "garage" in b
    fiche = profil.adapter(revision.prompt("fiche", "VLAN"), "fiche", 2)
    assert fiche.startswith("Ce que l'élève a écrit") and "(aujourd'hui en Terminale générale" in fiche
    # Classement d'un scan : le parcours seulement, pas le profil
    scan = profil.adapter(agent.PROMPT, "scan", 2)
    assert "Terminale générale" in scan and "le foot" not in scan
    # Le profil d'un compte ne se voit pas chez un autre
    assert profil.bloc(3) == ""
    # Pas d'entreprise sans alternance
    assert profil.enregistrer(2, {"alternance": "non", "entreprise": "garage"})["entreprise"] == ""


def test_profil_illisible_ne_bloque_rien(etat):
    etat["profil:2"] = "{pas du json"
    assert profil.lire(2) == profil.DEFAUT
    assert profil.adapter("x (du collège au BTS SIO)", "fiche", 2) == "x " + profil.PAR_DEFAUT


def test_appel_claude_adapte_la_consigne(etat, monkeypatch):
    profil.enregistrer(1, {"objectif": "le BTS avec mention"})
    vus = []

    class Fini:
        returncode, stdout, stderr = 0, json.dumps({"result": "ok", "is_error": False}), ""

    monkeypatch.setattr(agent, "_lancer", lambda cmd, *a, **k: vus.append(cmd[2]) or Fini())
    monkeypatch.setattr(agent, "journal", lambda **k: None)
    agent.appeler_claude(revision.prompt("quiz", "VLAN"), None, tache="quiz")
    assert vus[0].startswith("Ce que l'élève a écrit") and "le BTS avec mention" in vus[0]
    assert "(aujourd'hui en BTS SIO option SISR)" in vus[0]


def test_page_profil(client, monkeypatch):
    monkeypatch.setattr(db, "formation_du_compte", lambda i: {"niveau": "BTS2", "nom": "BTS SIO option SISR"})
    r = client.get("/reglages")
    assert 'href="/reglages/profil"' in r.text and "Mon profil" in r.text
    r = client.get("/reglages/profil")
    assert r.status_code == 200 and "BTS SIO option SISR" in r.text and "Rien d'autre pour l'instant" in r.text
    r = client.post("/reglages/profil", data={"alternance": "oui", "entreprise": "PME", "difficultes": "les maths",
                                              "explications": "direct", "detail": "court"}, follow_redirects=True)
    assert "Profil enregistré" in r.text and "les maths" in r.text and "en alternance (PME)" in r.text
    assert "1 info" not in client.get("/reglages").text and "5 infos lues par l&#39;IA" in client.get("/reglages").text


def test_rappel_du_jour_pour_chaque_invite(monkeypatch):
    lundi = dt.datetime(2026, 10, 26, 18, 30, tzinfo=nt.PARIS)
    etat = {"notifications:2": json.dumps({"heure": "18:00"}), "notifications:3": json.dumps({"rappel": False}),
            "pronote_edt": json.dumps({"creneaux": [{"debut": "2026-10-26T17:00:00+01:00",
                                                     "fin": "2026-10-26T20:00:00+01:00", "dispense": False}]})}
    monkeypatch.setattr(db, "etat_valeur", lambda k: etat.get(k))
    monkeypatch.setattr(db, "comptes", lambda: [{"id": 1, "role": "admin", "actif": True},
                                                {"id": 2, "role": "invite", "actif": True},
                                                {"id": 3, "role": "invite", "actif": True},
                                                {"id": 4, "role": "invite", "actif": False}])
    monkeypatch.setattr(db, "abonnements_de", lambda i: [{"endpoint": "x"}])
    envoyes = {}
    monkeypatch.setattr(db, "notification_connue", lambda cle: cle in envoyes)
    monkeypatch.setattr(db, "notifier_compte", lambda i, cle, *a, **k: envoyes.update({cle: (i, a, k)}))
    vus = []
    monkeypatch.setattr(nt, "rappel_du_jour", lambda jour: vus.append(db.compte_actuel()) or ("Réviser", "3 cartes", "/"))
    nt.planifier_invites(lundi)
    nt.planifier_invites(lundi)   # une seule fois par jour
    # Le compte 2 reçoit le sien à son heure, même si l'admin est en cours (son emploi du temps à lui)
    assert envoyes == {"rappel:2026-10-26@2": (2, ("Réviser", "3 cartes", "/"), {})} and vus == [2]
