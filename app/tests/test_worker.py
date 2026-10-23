import time

import pytest

from claunote.agent import Analyse
from claunote.rangement import niveau_depuis_dossier


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("KDRIVE_INBOX_ID", "10")
    monkeypatch.setenv("KDRIVE_ETUDES_ID", "1")
    import importlib

    from claunote import worker as w
    return importlib.reload(w)


@pytest.mark.parametrize("dossier,niveau", [
    ("BTS1", "BTS1"), ("BTS SIO 2", "BTS2"), ("2023-2024 BTS1", "BTS1"), ("1ère année BTS", "BTS1"),
    ("BTS SIO 2024", None), ("Terminale", "Terminale"), ("Tle S", "Terminale"), ("1ère", "Première"),
    ("2nde", "Seconde"), ("3ème", "3e"), ("6e", "6e"), ("Maths", None), ("2021-2022", None)])
def test_niveau_depuis_dossier(dossier, niveau):
    assert niveau_depuis_dossier([dossier]) == niveau


def test_niveau_le_plus_profond_gagne():
    assert niveau_depuis_dossier(["Lycée", "Terminale", "Maths"]) == "Terminale"


def test_appliquer_dossier(worker):
    a = Analyse("t", niveau=None, matiere="Réseaux", type="TP", confiance=0.9)
    assert worker.appliquer_dossier(a, ["BTS1"]) is None and a.niveau == "BTS1" and a.confiance == 0.9
    b = Analyse("t", niveau="3e", matiere="Maths", type="Cours", confiance=0.95)
    note = worker.appliquer_dossier(b, ["BTS1"])
    assert "BTS1" in note and "3e" in note and b.niveau == "BTS1" and b.confiance < 0.7
    c = Analyse("t", niveau="3e", confiance=0.9)
    assert worker.appliquer_dossier(c, ["Divers"]) is None and c.niveau == "3e"


class FauxKD:
    def __init__(self, arbre):
        self.arbre, self.supprimes = arbre, []

    def list_dir(self, fid):
        return self.arbre.get(fid, [])

    def delete(self, fid):
        self.supprimes.append(fid)
        for enfants in self.arbre.values():
            enfants[:] = [e for e in enfants if e["id"] != fid]


def test_parcourir_les_sous_dossiers(worker, monkeypatch):
    kd = FauxKD({10: [{"id": 1, "name": "a.pdf", "type": "file"}, {"id": 20, "name": "BTS1", "type": "dir"}],
                 20: [{"id": 21, "name": "Réseaux", "type": "dir"}, {"id": 2, "name": "b.pdf", "type": "file"}],
                 21: [{"id": 3, "name": "c.pdf", "type": "file"}]})
    monkeypatch.setattr(worker.db, "par_kdrive", lambda i: {"statut": "classe"} if i == 2 else None)
    items = worker.a_traiter(kd)
    assert [(i["name"], i["dossiers"]) for i in items] == [("a.pdf", []), ("c.pdf", ["BTS1", "Réseaux"])]


def test_nettoyer_inbox_supprime_les_dossiers_vides_et_anciens(worker):
    vieux = time.time() - 3600
    kd = FauxKD({10: [{"id": 20, "name": "BTS1", "type": "dir", "last_modified_at": vieux},
                      {"id": 30, "name": "En cours", "type": "dir", "last_modified_at": time.time()},
                      {"id": 40, "name": "Terminale", "type": "dir", "last_modified_at": vieux},
                      {"id": 1, "name": "a.pdf", "type": "file"}],
                 20: [{"id": 21, "name": "Réseaux", "type": "dir", "last_modified_at": vieux}],
                 40: [{"id": 4, "name": "reste.pdf", "type": "file"}]})
    worker.nettoyer_inbox(kd)
    assert kd.supprimes == [21, 20]


def test_analyser_transmet_le_dossier(monkeypatch, tmp_path):
    from claunote import agent
    vu = {}

    def faux(prompt, cwd, outils=None, **k):
        vu["prompt"] = prompt
        return '{"transcription": "x", "niveau": "BTS1", "matiere": "Réseaux", "type": "TP", "confiance": 0.9}'

    monkeypatch.setattr(agent, "appeler_claude", faux)
    agent.analyser(tmp_path / "a.pdf", ["BTS1", "Réseaux"])
    assert "« BTS1/Réseaux »" in vu["prompt"]


def test_flashcards_enregistrees(worker, monkeypatch):
    from claunote import db
    vu = {}
    cartes = [{"recto": "VLAN ?", "verso": "Réseau virtuel", "source": "D1"}]
    monkeypatch.setattr(worker.revision, "generer", lambda *a: (__import__("json").dumps(cartes), {"D1": 3}))
    monkeypatch.setattr(db, "documents", lambda ids: [])
    monkeypatch.setattr(db, "creer_cartes", lambda rev_id, k: vu.update(cartes=(rev_id, k)))
    monkeypatch.setattr(db, "maj_revision", lambda rev_id, statut, **k: vu.update(statut=statut))

    class FauxKD:
        def ensure_path(self, parent, parts):
            return 9

        def upload(self, dossier, nom, contenu):
            vu["md"] = contenu.decode()
            return {"id": 5}

    worker.traiter_revision(FauxKD(), {"id": 4, "genre": "cartes", "sujet": "VLAN", "doc_ids": [3], "date_eval": None})
    assert vu["cartes"] == (4, cartes) and vu["statut"] == "pret" and "### 1. VLAN ?" in vu["md"]
