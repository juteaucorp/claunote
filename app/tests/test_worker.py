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
    monkeypatch.setattr(worker.db, "import_connus", lambda ids: {1: "analyser", 3: "analyser"})
    items = worker.a_traiter(kd)
    assert [(i["name"], i["dossiers"]) for i in items] == [("a.pdf", []), ("c.pdf", ["BTS1", "Réseaux"])]
    monkeypatch.setattr(worker.db, "import_connus", lambda ids: {1: "inutile", 3: "page"})
    assert worker.a_traiter(kd) == []  # en attente de ton choix ou de la fusion


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
    from claunote import db, notifications
    import json
    vu = {}
    cartes = [{"recto": "VLAN ?", "verso": "Réseau virtuel", "source": "D1"}]
    monkeypatch.setattr(worker.revision, "generer", lambda *a: (json.dumps(cartes), {"D1": 3}, []))
    monkeypatch.setattr(db, "documents", lambda ids: [{"id": 3, "titre": "TP VLAN", "nom": "x.pdf"}])
    monkeypatch.setattr(db, "creer_cartes", lambda rev_id, k: vu.update(cartes=(rev_id, k)))
    monkeypatch.setattr(db, "maj_revision", lambda rev_id, statut, **k: vu.update(statut=statut, kdrive=k.get("kdrive_id")))
    monkeypatch.setattr(db, "revision", lambda i: {"id": i, "genre": "cartes", "sujet": "VLAN", "statut": "pret",
                                                   "contenu": json.dumps(cartes), "reperes": {"D1": 3},
                                                   "matiere": "Réseaux", "niveau": "BTS1", "campagne_titre": None})
    monkeypatch.setattr(notifications, "revision_prete", lambda rev: vu.update(notif=rev["id"]))

    class FauxKD:
        def ensure_path(self, parent, parts):
            vu["dossier"] = parts
            return 9

        def upload(self, dossier, nom, contenu):
            vu["fichier"] = (nom, contenu[:5])
            return {"id": 5}

    worker.traiter_revision(FauxKD(), {"id": 4, "genre": "cartes", "sujet": "VLAN", "doc_ids": [3], "date_eval": None})
    assert vu["cartes"] == (4, cartes) and vu["statut"] == "pret" and vu["kdrive"] == 5 and vu["notif"] == 4
    # Le PDF part dans Etudes/fiches/<matière>/
    assert vu["dossier"] == ["fiches", "Réseaux"] and vu["fichier"][0].endswith("_cartes_vlan.pdf")
    assert vu["fichier"][1] == b"%PDF-"


def test_programme_de_campagne(worker, monkeypatch):
    from claunote import db, notifications
    import datetime as dt
    vu = {}
    revs = [{"id": 1, "genre": "examen", "sujet": "Maths : suites", "niveau": "BTS2", "matiere": "Maths", "doc_ids": [3],
             "statut": "pret"},
            {"id": 2, "genre": "fiche", "sujet": "Maths : suites", "niveau": "BTS2", "matiere": "Maths", "doc_ids": [4],
             "statut": "pret"},
            {"id": 3, "genre": "fiche", "sujet": "CEJM", "niveau": "BTS2", "matiere": "CEJM", "doc_ids": [5],
             "statut": "pret"}]
    monkeypatch.setattr(db, "revisions_campagne", lambda i: revs)
    monkeypatch.setattr(db, "documents", lambda ids: [{"id": i, "titre": f"Cours {i}", "nom": ""} for i in ids])
    monkeypatch.setattr(db, "evenements_entre", lambda d, f: [])
    monkeypatch.setattr(db, "maj_campagne", lambda i, statut, programme=None, erreur=None: vu.update(statut=statut,
                                                                                                    programme=programme))
    monkeypatch.setattr(notifications, "campagne_prete", lambda i: vu.update(notif=i))

    def faux(campagne, matieres, evenements):
        vu["matieres"] = matieres
        return {"conseil": "", "jours": []}
    monkeypatch.setattr(worker.revision, "generer_programme", faux)
    worker.traiter_campagne({"id": 8, "titre": "BTS blanc", "consigne": "", "debut": dt.date(2026, 11, 2),
                             "fin": dt.date(2026, 11, 6)})
    assert vu["statut"] == "pret" and vu["notif"] == 8
    assert vu["matieres"] == [
        {"niveau": "BTS2", "matiere": "Maths", "sujet": "suites", "supports": ["examen", "fiche"],
         "cours": ["Cours 3", "Cours 4"]},
        {"niveau": "BTS2", "matiere": "CEJM", "sujet": "", "supports": ["fiche"], "cours": ["Cours 5"]}]


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


class KDImport(FauxKD):
    """Faux kDrive qui garde la trace des déplacements, envois et téléchargements."""

    def __init__(self, arbre):
        super().__init__(arbre)
        self.deplaces, self.envois, self.chemins = [], [], []

    def move(self, fid, dest):
        self.deplaces.append((fid, dest))

    def rename(self, fid, nom):
        self.deplaces.append((fid, nom))

    def ensure_path(self, parent, parts):
        self.chemins.append((parent, parts))
        return 500 + len(parts)

    def download(self, fid, dest):
        from PIL import Image
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.suffix == ".jpg":
            Image.new("RGB", (100, 140), "white").save(dest)
        else:
            dest.write_bytes(b"x")
        return dest

    def upload(self, dir_id, nom, contenu, conflict="version"):
        self.envois.append((dir_id, nom, contenu[:4]))
        return {"id": 999, "name": nom}


def test_trier_attend_la_fin_de_la_copie(worker, monkeypatch):
    enregistres, notes = [], []
    monkeypatch.setattr(worker, "STABLE", 0)
    monkeypatch.setattr(worker.db, "import_connus", lambda ids: {})
    monkeypatch.setattr(worker.db, "par_kdrive", lambda i: None)
    monkeypatch.setattr(worker.db, "import_enregistrer", lambda lot, f: enregistres.append((lot, f)))
    monkeypatch.setattr(worker.notifications, "import_a_confirmer", lambda lot, n: notes.append((lot, n)))
    monkeypatch.setattr(worker.agent, "interpreter_arborescence", lambda *a: pytest.fail("trop peu de documents"))
    items = [{"id": 1, "name": "cours.docx", "type": "file", "size": 9000, "dossiers": ["Vieux", "BTS1", "Maths"]},
             {"id": 2, "name": "cours.pdf", "type": "file", "size": 9000, "dossiers": ["Vieux", "BTS1", "Maths"]},
             {"id": 3, "name": ".vscode", "type": "dir", "dossiers": ["Vieux", "Dev"]}]
    assert worker.trier_inbox(None, items[:2]) == 0          # premier passage : on note le lot
    assert worker.trier_inbox(None, items) == 0              # il grossit encore : on attend
    assert worker.trier_inbox(None, items) == 3              # stable : trié
    (lot, fichiers), = enregistres
    assert lot == "Vieux" and [f.decision for f in fichiers] == ["analyser", "variante", "inutile"]
    assert notes == [("Vieux", 1)]


def test_comprendre_organisation_une_passe_ia(worker, monkeypatch):
    sauves = []
    monkeypatch.setattr(worker.db, "matieres", lambda: [])
    monkeypatch.setattr(worker.db, "import_lot_maj", lambda lot, r, c: sauves.append((lot, r, c)))
    monkeypatch.setattr(worker.agent, "interpreter_arborescence",
                        lambda dossiers, m: ("Par année scolaire.", {"V/2023-2024": {"niveau": "BTS1"}}))
    fichiers = [importation_fichier(i, f"d{i}.pdf", ["V", "2023-2024"]) for i in range(6)]
    worker.comprendre_organisation("V", fichiers)
    assert sauves == [("V", "Par année scolaire.", {"V/2023-2024": {"niveau": "BTS1"}})]


def importation_fichier(i, nom, dossiers):
    from claunote.importation import Fichier
    return Fichier(i, nom, dossiers)


def test_fusionner_les_pages(worker, monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "CACHE_DIR", tmp_path)
    pages = [{"kdrive_id": 70 + i, "nom": f"suites {i + 1}.jpg", "chemin": f"V/Maths/suites {i + 1}.jpg", "lot": "V"}
             for i in range(3)]
    enregistres, marques = [], []
    monkeypatch.setattr(worker.db, "import_groupe", lambda g: pages)
    monkeypatch.setattr(worker.db, "import_enregistrer", lambda lot, f: enregistres.append((lot, f)))
    monkeypatch.setattr(worker.db, "import_marquer", lambda ids, d, **k: marques.append((ids, d)))
    kd = KDImport({})
    worker.fusionner(kd, "V/Maths/suites")
    assert kd.envois == [(502, "suites-3p.pdf", b"%PDF")]
    (lot, (f,)), = enregistres
    assert lot == "V" and f.id == 999 and f.decision == "analyser" and f.dossiers == ["V", "Maths"]
    assert marques == [([70, 71, 72], "fusionne")]
    assert (1, ["import", "pages-fusionnees", "V", "Maths"]) in kd.chemins
    assert {fid for fid, _ in kd.deplaces} == {70, 71, 72}


def test_executer_les_choix(worker, monkeypatch):
    oublies, marques = [], []
    monkeypatch.setattr(worker.db, "import_a_executer", lambda: [
        {"kdrive_id": 5, "decision": "supprimer", "chemin": "V/Dev/.vscode"},
        {"kdrive_id": 6, "decision": "garder", "chemin": "V/Dev/archive.zip"}])
    monkeypatch.setattr(worker.db, "import_groupes", lambda sur: [])
    monkeypatch.setattr(worker.db, "import_oublier", lambda ids: oublies.extend(ids))
    monkeypatch.setattr(worker.db, "import_marquer", lambda ids, d, **k: marques.append((ids, d)))
    kd = KDImport({})
    assert worker.executer_choix(kd)
    assert kd.supprimes == [5] and oublies == [5]
    assert (1, ["autres", "V", "Dev"]) in kd.chemins and (6, 503) in kd.deplaces and marques == [([6], "fait")]


def test_la_variante_suit_le_document(worker, monkeypatch):
    maj = []
    monkeypatch.setattr(worker.db, "variantes_de", lambda kid: [{"kdrive_id": 8, "nom": "TP1.pdf"}])
    monkeypatch.setattr(worker.db, "maj", lambda i, **k: maj.append(k))
    monkeypatch.setattr(worker.db, "import_marquer", lambda ids, d, **k: None)
    kd = KDImport({})
    worker.ranger_variantes(kd, 7, 1, 42, "2025-01-10_vlan.docx")
    assert kd.deplaces == [(8, 42), (8, "2025-01-10_vlan.pdf")]
    assert maj == [{"variante_kdrive_id": 8, "variante_nom": "2025-01-10_vlan.pdf"}]


def test_analyse_impossible_essaie_l_autre_format(worker, monkeypatch, tmp_path):
    appels = []
    monkeypatch.setattr(worker, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(worker.db, "creer", lambda kid, nom: 1)
    monkeypatch.setattr(worker.db, "import_lot", lambda lot: None)
    monkeypatch.setattr(worker.db, "variantes_de", lambda kid: [{"kdrive_id": 8, "nom": "TP1.pdf"}])
    monkeypatch.setattr(worker.db, "import_basculer", lambda a, n: appels.append(("bascule", a, n)))
    monkeypatch.setattr(worker.db, "supprimer", lambda i: appels.append(("oubli", i)))

    def echec(*a):
        raise worker.ErreurAgent("fichier .doc illisible")
    monkeypatch.setattr(worker, "analyser", echec)
    worker.traiter(KDImport({}), {"id": 7, "name": "TP1.doc", "dossiers": ["V"]})
    assert appels == [("bascule", 7, 8), ("oubli", 1)]
