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
    monkeypatch.setattr(db, "niveaux_matieres", lambda: [{"niveau": "BTS1", "matiere": "Réseaux"}])
    monkeypatch.setattr(db, "revisions", lambda: [])
    monkeypatch.setattr(db, "choisir_documents", lambda n, m, q="": [1] if q == "" or q == "vlan" else [])
    monkeypatch.setattr(db, "creer_revision", lambda *a: appels.append(("rev",) + a) or 5)
    monkeypatch.setattr(db, "documents", lambda ids: [DOC] if 1 in ids else [])
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


def test_page_reviser(client):
    r = client.get("/reviser")
    assert r.status_code == 200 and 'value="BTS1|Réseaux"' in r.text and "Plan de révision" in r.text


def test_demander_une_fiche_depuis_un_document(client):
    r = client.post("/reviser", data={"genre": "fiche", "doc_id": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/revision/5"
    assert ("rev", "fiche", "Les VLAN", [1], None) in client.appels and "scan" in client.appels


def test_demander_par_matiere_avec_repli(client):
    r = client.post("/reviser", data={"genre": "quiz", "niveau_matiere": "BTS1|Réseaux", "sujet": "ospf"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert ("rev", "quiz", "ospf", [1], None) in client.appels


def test_plan_sans_date_refuse(client):
    r = client.post("/reviser", data={"genre": "plan", "niveau_matiere": "BTS1|Réseaux"})
    assert r.status_code == 400


def test_voir_revision(client, monkeypatch):
    base = {"id": 5, "genre": "fiche", "sujet": "VLAN", "date_eval": None, "erreur": None,
            "reperes": {"D1": 1}, "statut": "pret", "contenu": "## L'essentiel\nUn VLAN [D1] <b>x</b>"}
    revs = {5: base, 6: {**base, "id": 6, "statut": "en_attente", "contenu": None},
            7: {**base, "id": 7, "genre": "quiz", "contenu": '[{"question": "Q?", "choix": ["a", "b"], '
                '"reponse": 1, "explication": "car", "source": "D1"}]'}}
    monkeypatch.setattr(db, "revision", lambda i: revs.get(i))
    r = client.get("/revision/5")
    assert '<a href="/doc/1">[D1]</a>' in r.text and "<b>x</b>" not in r.text and "Les VLAN" in r.text
    assert 'http-equiv="refresh"' in client.get("/revision/6").text
    r = client.get("/revision/7")
    assert "Voir la réponse" in r.text and "B. b" in r.text
    assert client.get("/revision/99").status_code == 404
