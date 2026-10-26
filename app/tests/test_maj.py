import datetime as dt
import json

import pytest

from claunote import maj
from tests.test_site import anonyme, client  # noqa: F401  (fixtures du site)


@pytest.fixture
def dossier(tmp_path, monkeypatch):
    monkeypatch.setattr(maj, "DOSSIER", tmp_path)
    (tmp_path / "vu").write_text(dt.datetime.now(dt.timezone.utc).isoformat())
    return tmp_path


def test_absent_sans_service(tmp_path, monkeypatch):
    monkeypatch.setattr(maj, "DOSSIER", tmp_path)
    assert not maj.present()
    (tmp_path / "vu").write_text((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat())
    assert not maj.present()   # service arrêté depuis 5 min
    assert maj.disponibles() == 0 and maj.etat()["en_cours"] is False


def test_demander_n_accepte_que_les_actions_prevues(dossier):
    with pytest.raises(ValueError):
        maj.demander("rm -rf /")
    maj.demander("mettre_a_jour")
    assert (dossier / "demande").read_text() == "mettre_a_jour"
    with pytest.raises(RuntimeError):   # une seule action à la fois
        maj.demander("verifier")


def test_etat_lu(dossier):
    (dossier / "etat.json").write_text(json.dumps({
        "statut": "ok", "message": "Vérifié.", "retard": 2, "fin": "2026-10-26T12:00:00+00:00",
        "version": {"id": "abc1234", "le": "2026-10-20T10:00:00+02:00", "titre": "Ancienne"},
        "nouveautes": [{"id": "d1", "le": "2026-10-25T10:00:00+02:00", "titre": "Nouvelle page"}]}))
    e = maj.etat()
    assert e["version"]["id"] == "abc1234" and e["nouveautes"][0]["le"].day == 25 and not e["en_cours"]
    assert maj.disponibles() == 2


def test_page_mise_a_jour(client, dossier):
    (dossier / "etat.json").write_text(json.dumps({
        "statut": "ok", "message": "Vérifié.", "retard": 1,
        "version": {"id": "abc1234", "le": "2026-10-20T10:00:00+02:00", "titre": "Ancienne"},
        "nouveautes": [{"id": "d1", "le": "2026-10-25T10:00:00+02:00", "titre": "Nouvelle page"}]}))
    r = client.get("/mise-a-jour")
    assert r.status_code == 200
    assert "abc1234" in r.text and "1 nouveauté(s) disponible(s)" in r.text and "Nouvelle page" in r.text
    assert "Tout mettre à jour" in r.text and "ne tourne pas encore" not in r.text
    assert "Mise à jour<span class=\"badge\">1</span>" in r.text   # onglet Réglages


def test_mise_a_jour_sans_mot_de_passe(client, dossier):
    r = client.post("/mise-a-jour/mettre_a_jour")
    assert (dossier / "demande").read_text() == "mettre_a_jour"
    assert 'hx-trigger="every 2s"' in r.text and "En attente du service maj" in r.text
    assert 'type="password"' not in client.get("/mise-a-jour").text
    assert client.post("/mise-a-jour/supprimer").status_code == 404


def test_verifier(client, dossier):
    client.post("/mise-a-jour/verifier")
    assert (dossier / "demande").read_text() == "verifier"


def test_sans_service_maj(client, tmp_path, monkeypatch):
    monkeypatch.setattr(maj, "DOSSIER", tmp_path)
    r = client.get("/mise-a-jour")
    assert "ne tourne pas encore" in r.text
    r = client.post("/mise-a-jour/mettre_a_jour")
    assert "ne tourne pas sur la VM" in r.text and not (tmp_path / "demande").exists()


def test_reservee_aux_connectes(anonyme, dossier):
    assert anonyme.post("/mise-a-jour/verifier").status_code == 401
    assert not (dossier / "demande").exists()


def test_explique_le_jeton_github(client, dossier):
    (dossier / "etat.json").write_text(json.dumps({
        "action": "verifier", "statut": "erreur",
        "message": "Vérification impossible : GitHub refuse l'accès (dépôt privé) : ajoute GITHUB_TOKEN dans le .env de la VM."}))
    r = client.get("/mise-a-jour")
    assert "Dépôt privé : donne un jeton GitHub" in r.text and "Contents → Read-only" in r.text
