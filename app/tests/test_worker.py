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


def test_statut_et_note(worker):
    a = Analyse(transcription="", niveau="BTS1", matiere="Réseaux", type="TP", confiance=0.9)
    assert worker.statut_et_note(a, None) == ("classe", None)
    assert worker.statut_et_note(Analyse(transcription="", hors_cours=True, confiance=0.9), None) == ("hors_cours", None)
    statut, note = worker.statut_et_note(Analyse(transcription="", hors_cours=True, confiance=0.5), None)
    assert statut == "a_verifier" and "hors cours" in note
    statut, note = worker.statut_et_note(Analyse(transcription="", niveau="BTS1", confiance=0.4), "conflit")
    assert (statut, note) == ("a_verifier", "conflit")


def test_reclasser_puis_ranger(worker, monkeypatch):
    from claunote import db
    maj, ranges, moves = [], [], []
    doc = {"id": 3, "nom": "a.pdf", "titre": "VLAN", "chapitre": None, "niveau": "BTS2", "transcription": "x"}
    monkeypatch.setattr(worker.agent, "reclasser", lambda d, m: Analyse(
        transcription="", niveau="BTS2", matiere="Réseaux", type="Cours", confiance=0.9))
    monkeypatch.setattr(db, "maj", lambda i, **k: maj.append((i, k)))
    monkeypatch.setattr(db, "marquer_a_ranger", lambda ids, v=True: ranges.append((ids, v)))
    worker.reclasser(doc, ["Réseaux"])
    assert maj[-1][1]["statut"] == "classe" and maj[-1][1]["matiere"] == "Réseaux" and maj[-1][1]["titre"] == "VLAN"
    assert ranges[-1] == ([3], True)

    def echec(d, m):
        raise worker.ErreurAgent("pas de transcription")
    monkeypatch.setattr(worker.agent, "reclasser", echec)
    worker.reclasser(doc, [])
    assert maj[-1][1]["statut"] == "a_verifier" and "pas de transcription" in maj[-1][1]["note"]

    class KD:
        def ensure_path(self, parent, parts):
            moves.append(("path", parts))
            return {"hors-cours": 7}.get(parts[0], 8)

        def move(self, fid, dest):
            if fid == 99:
                raise RuntimeError("introuvable")
            moves.append((fid, dest))

    monkeypatch.setattr(db, "a_ranger", lambda: [
        {"id": 3, "nom": "a.pdf", "statut": "classe", "niveau": "BTS2", "matiere": "Réseaux", "type": "Cours",
         "kdrive_id": 30, "kdrive_md_id": 31},
        {"id": 4, "nom": "b.pdf", "statut": "hors_cours", "niveau": None, "matiere": None, "type": None,
         "kdrive_id": 40, "kdrive_md_id": None},
        {"id": 5, "nom": "c.pdf", "statut": "classe", "niveau": "BTS2", "matiere": "Réseaux", "type": "Cours",
         "kdrive_id": 99, "kdrive_md_id": None}])
    worker.ranger_en_attente(KD())
    assert moves == [("path", ["archives", "BTS2", "Réseaux", "Cours"]), (30, 8), (31, 8), ("path", ["hors-cours"]), (40, 7)]
    assert ranges[-3:] == [([3], False), ([4], False), ([5], False)]
