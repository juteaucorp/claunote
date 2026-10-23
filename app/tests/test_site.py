from datetime import date

import pytest
from fastapi.testclient import TestClient

from claunote import db, main

DOC = {"id": 1, "kdrive_id": 77, "nom": "2025-03-12_vlan.pdf", "statut": "classe", "titre": "Les VLAN",
       "niveau": "BTS1", "matiere": "Réseaux", "type": "TP", "chapitre": "Commutation",
       "date_doc": date(2025, 3, 12), "mots_cles": ["vlan"], "resume": "Configurer des VLAN.",
       "transcription": "# TP VLAN\n<script>alert(1)</script>", "confiance": 0.9, "erreur": None,
       "kdrive_md_id": 78}


@pytest.fixture
def client(monkeypatch):
    appels = []
    monkeypatch.setattr(db, "init", lambda: None)
    monkeypatch.setattr(db, "bibliotheque", lambda: [{"niveau": "BTS1", "matiere": "Réseaux", "type": "TP", "n": 3}])
    monkeypatch.setattr(db, "a_verifier", lambda: [{**DOC, "id": 2, "statut": "a_verifier", "confiance": 0.4}])
    monkeypatch.setattr(db, "document", lambda i: DOC if i == 1 else None)
    monkeypatch.setattr(db, "liste", lambda *a: [DOC])
    monkeypatch.setattr(db, "matieres", lambda: ["Réseaux"])
    monkeypatch.setattr(db, "recherche", lambda q: [{**DOC, "extrait": "les <b>VLAN</b> <i>x</i>"}])
    monkeypatch.setattr(db, "demander_scan", lambda: appels.append("scan"))
    monkeypatch.setattr(db, "maj", lambda i, **k: appels.append(("maj", i, k)))
    with TestClient(main.app) as c:
        c.appels = appels
        yield c


def test_bibliotheque(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "BTS1" in r.text and "TP · 3" in r.text and "1 document à vérifier" in r.text


def test_document_neutralise_le_html(client):
    r = client.get("/doc/1")
    assert r.status_code == 200 and "Configurer des VLAN." in r.text
    assert "<script>alert" not in r.text
    assert client.get("/doc/99").status_code == 404


def test_recherche_garde_seulement_le_surlignage(client):
    r = client.get("/recherche", params={"q": "vlan"})
    assert "<b>VLAN</b>" in r.text and "<i>x</i>" not in r.text


def test_scan_et_a_verifier(client):
    assert "Scan demandé" in client.post("/scan").text
    assert client.appels == ["scan"]
    r = client.get("/a-verifier")
    assert "confiance 40 %" in r.text


def test_classer_deplace_dans_kdrive(client, monkeypatch):
    moves = []

    class FauxKD:
        def ensure_path(self, parent, parts):
            moves.append(("path", parts))
            return 500

        def move(self, fid, dest):
            moves.append(("move", fid, dest))

    monkeypatch.setattr(main.KDrive, "from_env", classmethod(lambda cls: FauxKD()))
    r = client.post("/doc/1/classer", data={"niveau": "BTS2", "matiere": " Réseaux ", "type": "Cours"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert moves == [("path", ["archives", "BTS2", "Réseaux", "Cours"]), ("move", 77, 500), ("move", 78, 500)]
    assert client.appels[-1] == ("maj", 1, {"statut": "classe", "niveau": "BTS2", "matiere": "Réseaux",
                                            "type": "Cours", "confiance": 1.0})
    assert client.post("/doc/1/classer", data={"niveau": "Master", "matiere": "x", "type": "TP"}).status_code == 400
