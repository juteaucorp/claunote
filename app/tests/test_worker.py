import time

import pytest

from claunote.agent import Analyse
from claunote.rangement import niveau_depuis_dossier


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("KDRIVE_INBOX_ID", "10")
    monkeypatch.setenv("KDRIVE_ETUDES_ID", "1")
    import importlib

    from claunote import db, worker as w
    monkeypatch.setattr(db, "sensible_decision", lambda kdrive_id: None)
    monkeypatch.setattr(db, "import_choix", lambda ids: (set(), {}))
    monkeypatch.setattr(db, "etat_valeur", lambda cle: None)
    monkeypatch.delenv("ANALYSE_LOCALE", raising=False)
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
    monkeypatch.setattr(worker.db, "statuts_kdrive", lambda ids: {2: {"statut": "classe", "essais": 0}})
    monkeypatch.setattr(worker.db, "lots_a_comprendre", lambda: [])
    monkeypatch.setattr(worker.db, "documents_orphelins", lambda ids, n: [])
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
    import datetime as dt
    import json
    vu = {}
    cartes = [{"recto": "VLAN ?", "verso": "Réseau virtuel", "source": "D1"}]
    monkeypatch.setattr(worker.revision, "generer", lambda *a, **k: vu.update(pieces=k["pieces"]) or
                        (json.dumps(cartes), {"D1": 3}, []))
    monkeypatch.setattr(db, "pieces_jointes", lambda r=None, c=None, contenu=False: [{"nom": "edt.png", "contenu": b"x"}])
    monkeypatch.setattr(db, "Condenses", dict)
    monkeypatch.setattr(db, "documents", lambda ids: [{"id": 3, "titre": "TP VLAN", "nom": "x.pdf"}])
    monkeypatch.setattr(db, "creer_cartes", lambda rev_id, k: vu.update(cartes=(rev_id, k)))
    monkeypatch.setattr(db, "maj_revision", lambda rev_id, statut, **k: vu.update(statut=statut))
    monkeypatch.setattr(db, "maj_pdf_revision", lambda rev_id, kdrive_id: vu.update(kdrive=kdrive_id))
    monkeypatch.setattr(db, "revision", lambda i: {"id": i, "genre": "cartes", "sujet": "VLAN", "statut": "pret",
                                                   "cree_le": dt.datetime(2026, 10, 23, 18),
                                                   "contenu": json.dumps(cartes), "reperes": {"D1": 3},
                                                   "matiere": "Réseaux", "niveau": "BTS1", "campagne_titre": None})
    monkeypatch.setattr(notifications, "revision_prete", lambda rev: vu.update(notif=rev["id"]))
    monkeypatch.setattr(worker.coherence, "preparer", lambda rev: ("", []))
    monkeypatch.setattr(worker.coherence, "controler", lambda *a: None)

    class FauxKD:
        def ensure_path(self, parent, parts):
            vu["dossier"] = parts
            return 9

        def upload(self, dossier, nom, contenu):
            vu["fichier"] = (nom, contenu[:5])
            return {"id": 5}

    worker.traiter_revision(FauxKD(), {"id": 4, "genre": "cartes", "sujet": "VLAN", "doc_ids": [3], "date_eval": None})
    assert vu["cartes"] == (4, cartes) and vu["statut"] == "pret" and vu["kdrive"] == 5 and vu["notif"] == 4
    assert vu["pieces"] == [{"nom": "edt.png", "contenu": b"x"}]
    # Le PDF part dans Etudes/fiches/<matière>/
    assert vu["dossier"] == ["fiches", "Réseaux"] and vu["fichier"][0] == "2026-10-23_cartes_vlan.pdf"
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

    monkeypatch.setattr(db, "pieces_jointes", lambda revision_id=None, campagne_id=None, contenu=False:
                        [{"nom": "planning.pdf", "campagne_id": campagne_id}])

    def faux(campagne, matieres, evenements, pieces):
        vu["matieres"], vu["pieces"] = matieres, pieces
        return {"conseil": "", "jours": []}
    monkeypatch.setattr(worker.revision, "generer_programme", faux)
    worker.traiter_campagne({"id": 8, "titre": "BTS blanc", "consigne": "", "debut": dt.date(2026, 11, 2),
                             "fin": dt.date(2026, 11, 6)})
    assert vu["statut"] == "pret" and vu["notif"] == 8 and vu["pieces"] == [{"nom": "planning.pdf", "campagne_id": 8}]
    assert vu["matieres"] == [
        {"niveau": "BTS2", "matiere": "Maths", "sujet": "suites", "supports": ["examen", "fiche"],
         "cours": ["Cours 3", "Cours 4"]},
        {"niveau": "BTS2", "matiere": "CEJM", "sujet": "", "supports": ["fiche"], "cours": ["Cours 5"]}]
    # Même matière sur plusieurs années : le programme voit l'année de chaque cours
    revs[:] = [{"id": 1, "genre": "fiche", "sujet": "Maths", "niveau": "BTS2", "matiere": "Maths", "doc_ids": [3, 1],
                "statut": "pret"}]
    monkeypatch.setattr(db, "documents", lambda ids: [{"id": i, "titre": f"Cours {i}", "nom": "",
                                                       "niveau": "BTS2" if i == 3 else "BTS1"} for i in ids])
    worker.traiter_campagne({"id": 8, "titre": "BTS blanc", "consigne": "", "debut": dt.date(2026, 11, 2),
                             "fin": dt.date(2026, 11, 6)})
    assert vu["matieres"] == [{"niveau": "BTS1, BTS2", "matiere": "Maths", "sujet": "", "supports": ["fiche"],
                               "cours": ["[BTS2] Cours 3", "[BTS1] Cours 1"]}]


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
    monkeypatch.setattr(db, "variantes_de", lambda kid: [])
    monkeypatch.setattr(db, "etat_ecrire", lambda k, v: None)
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
    monkeypatch.setattr(worker.db, "statuts_kdrive", lambda ids: {})
    monkeypatch.setattr(worker.db, "import_a_comprendre", lambda lot: pytest.fail("trop peu de documents"))
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
    fichiers = [importation_fichier(i, f"d{i}.pdf", ["V", "2023-2024"]) for i in range(6)]
    assert worker.besoin_organisation(fichiers)
    assert not worker.besoin_organisation(fichiers[:3])  # trop peu de documents
    assert not worker.besoin_organisation([importation_fichier(i, f"d{i}.pdf", ["V", "BTS1", "Maths"]) for i in range(6)])
    sauves, finis = [], []
    monkeypatch.setattr(worker.db, "lots_a_comprendre", lambda: ["V"])
    monkeypatch.setattr(worker.db, "import_dossiers", lambda lot: {"V/2023-2024": 6})
    monkeypatch.setattr(worker.db, "matieres", lambda: [])
    monkeypatch.setattr(worker.db, "etat_ecrire", lambda k, v: None)
    monkeypatch.setattr(worker.db, "import_lot_maj", lambda lot, r, c: sauves.append((lot, r, c)))
    monkeypatch.setattr(worker.db, "lot_compris", lambda lot: finis.append(lot))
    monkeypatch.setattr(worker.agent, "interpreter_arborescence",
                        lambda dossiers, m, essai_local=None: ("Par année scolaire.", {"V/2023-2024": {"niveau": "BTS1"}}))
    worker.comprendre_en_attente()
    assert sauves == [("V", "Par année scolaire.", {"V/2023-2024": {"niveau": "BTS1"}})] and finis == ["V"]

    # Limite du forfait : le lot attend le retour de Claude (pas marqué compris)
    def limite(*a):
        raise worker.LimiteAtteinte("limit reached")
    monkeypatch.setattr(worker.agent, "interpreter_arborescence", limite)
    with pytest.raises(worker.LimiteAtteinte):
        worker.comprendre_en_attente()
    assert finis == ["V"]


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


# --- Fiabilité ------------------------------------------------------------------------

def test_garde_pause_croissante_et_limite(worker, monkeypatch):
    import datetime as dt
    g = worker.Garde("Claude")
    g.echec("panne", "529")
    assert not g.disponible() and 55 < g.jusqu_a - time.time() <= 60
    g.echec("panne", "529")
    assert 115 < g.jusqu_a - time.time() <= 120
    g.ok()
    assert g.disponible() and g.etat == "ok" and g.echecs == 0
    reprise = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
    g.limite(worker.LimiteAtteinte("limit reached", reprise))
    assert g.etat == "limite" and abs(g.jusqu_a - reprise.timestamp() - 60) < 1
    publie = g.publier()
    # Après un redémarrage, la limite est restaurée : pas d'appel inutile avant l'heure
    h = worker.Garde("Claude")
    h.restaurer(publie)
    assert not h.disponible() and h.etat == "limite"
    h2 = worker.Garde("Claude")
    h2.restaurer({**publie, "jusqu_a": "2020-01-01T00:00:00+00:00"})
    assert h2.disponible()


def test_etape_ouvre_le_bon_disjoncteur(worker, monkeypatch):
    monkeypatch.setattr(worker.db, "etat_ecrire", lambda k, v: None)
    monkeypatch.setattr(worker, "CLAUDE", worker.Garde("Claude"))
    monkeypatch.setattr(worker, "KDRIVE", worker.Garde("kDrive"))

    def leve(e):
        def f():
            raise e
        return f
    worker.etape("x", leve(worker.LimiteAtteinte("usage limit reached")))
    assert worker.CLAUDE.etat == "limite" and worker.KDRIVE.disponible()
    worker.etape("x", leve(worker.KDriveIndisponible("kDrive répond 503")))
    assert worker.KDRIVE.etat == "panne"
    worker.CLAUDE.ok()
    worker.etape("x", leve(worker.ClaudeDeconnecte("Invalid API key")))
    assert worker.CLAUDE.etat == "connexion"
    assert worker.etape("x", leve(ValueError("bug"))) is None  # une erreur imprévue n'arrête pas le worker


def test_l_analyse_est_gardee_si_kdrive_tombe_au_rangement(worker, monkeypatch, tmp_path):
    maj, ranges, marques = [], [], []
    monkeypatch.setattr(worker, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(worker.db, "creer", lambda kid, nom: 1)
    monkeypatch.setattr(worker.db, "import_lot", lambda lot: None)
    monkeypatch.setattr(worker.db, "maj", lambda i, **k: maj.append(k))
    monkeypatch.setattr(worker.db, "marquer_a_ranger", lambda ids, v=True: ranges.append((ids, v)))
    monkeypatch.setattr(worker.db, "import_marquer", lambda ids, d, **k: marques.append((ids, d)))
    monkeypatch.setattr(worker.db, "document", lambda i: {"id": 1, "kdrive_id": 7, "nom": "x.pdf", "statut": "classe",
                                                           "niveau": "BTS1", "matiere": "Réseaux", "type": "TP"})
    monkeypatch.setattr(worker, "analyser", lambda *a: Analyse("# TP", titre="VLAN", niveau="BTS1", matiere="Réseaux",
                                                              type="TP", confiance=0.9, modele="sonnet"))

    class Panne(KDImport):
        def move(self, fid, dest):
            raise worker.KDriveIndisponible("kDrive répond 503")

    with pytest.raises(worker.KDriveIndisponible):
        worker.traiter(Panne({}), {"id": 7, "name": "x.pdf", "dossiers": []})
    # L'analyse est en base, le document reste « à ranger » : le tour suivant le range sans rappeler Claude
    assert maj[0]["statut"] == "classe" and maj[0]["transcription"] == "# TP" and maj[0]["modele"] == "sonnet"
    assert ranges == [([1], True)] and marques == [([7], "fait")]


def test_claude_qui_plante_toujours_sur_un_fichier(worker, monkeypatch, tmp_path):
    maj, essais = [], {"n": 0}
    monkeypatch.setattr(worker, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(worker, "CLAUDE", worker.Garde("Claude"))
    monkeypatch.setattr(worker.db, "creer", lambda kid, nom: 1)
    monkeypatch.setattr(worker.db, "import_lot", lambda lot: None)
    monkeypatch.setattr(worker.db, "maj", lambda i, **k: maj.append(k))

    def compter(table, i):
        essais["n"] += 1
        return essais["n"]
    monkeypatch.setattr(worker.db, "compter_essai", compter)

    def plante(*a):
        raise worker.ClaudeIndisponible("Claude Code s'est arrêté (code 1)")
    monkeypatch.setattr(worker, "analyser", plante)
    item = {"id": 7, "name": "x.pdf", "dossiers": []}
    for _ in range(2):
        with pytest.raises(worker.ClaudeIndisponible):  # panne : le worker patientera
            worker.traiter(KDImport({}), item)
    worker.traiter(KDImport({}), item)  # 3e fois alors que Claude marche : ce fichier pose problème
    assert maj == [{"statut": "erreur", "erreur": "Claude Code s'arrête à chaque essai sur ce fichier : "
                                                  "Claude Code s'est arrêté (code 1)"}]
    # Pendant une vraie panne de Claude (déjà en échec), on ne met rien en erreur
    essais["n"], maj[:] = 3, []
    worker.CLAUDE.echec("panne", "x")
    with pytest.raises(worker.ClaudeIndisponible):
        worker.traiter(KDImport({}), item)
    assert maj == []


def test_documents_perdus_hors_de_l_inbox_sont_repris(worker, monkeypatch):
    import httpx
    supprimes = []
    monkeypatch.setattr(worker.db, "statuts_kdrive", lambda ids: {})
    monkeypatch.setattr(worker.db, "import_connus", lambda ids: {})
    monkeypatch.setattr(worker.db, "lots_a_comprendre", lambda: [])
    monkeypatch.setattr(worker.db, "supprimer", lambda i: supprimes.append(i))
    monkeypatch.setattr(worker.db, "documents_orphelins", lambda ids, n: [
        {"id": 1, "kdrive_id": 70, "nom": "a.pdf", "essais": 0, "origine": "BTS1/Réseaux"},
        {"id": 2, "kdrive_id": 80, "nom": "b.pdf", "essais": 0, "origine": None}])

    class KD(FauxKD):
        def stat(self, fid):
            if fid == 80:
                raise httpx.HTTPStatusError("404", request=None, response=httpx.Response(404))
            return {"id": fid, "name": "a.pdf"}
    items = worker.a_traiter(KD({}), [])
    assert items == [{"id": 70, "name": "a.pdf", "type": "file", "dossiers": ["BTS1", "Réseaux"], "moteur": None}]
    assert supprimes == [2]  # le fichier n'existe plus : on oublie le document


def test_priorites_de_l_inbox(worker, monkeypatch):
    monkeypatch.setattr(worker.db, "statuts_kdrive", lambda ids: {3: {"statut": "nouveau", "essais": 2}})
    monkeypatch.setattr(worker.db, "import_connus", lambda ids: {1: "analyser", 2: "analyser", 4: "analyser"})
    monkeypatch.setattr(worker.db, "lots_a_comprendre", lambda: ["Vieux"])
    monkeypatch.setattr(worker.db, "documents_orphelins", lambda ids, n: [])
    items = [{"id": 1, "name": "a.pdf", "type": "file", "dossiers": ["Divers"]},
             {"id": 2, "name": "b.pdf", "type": "file", "dossiers": ["BTS2", "Pronote", "CEJM"]},
             {"id": 3, "name": "c.pdf", "type": "file", "dossiers": []},
             {"id": 4, "name": "d.pdf", "type": "file", "dossiers": ["Vieux", "Maths"]}]
    # Pronote d'abord, le fichier qui a déjà planté en dernier, le lot pas encore compris attend
    assert [i["id"] for i in worker.a_traiter(FauxKD({}), items)] == [2, 1, 3]


def _tour(worker, monkeypatch, fait):
    monkeypatch.setattr(worker.db, "etat_ecrire", lambda k, v: None)
    for nom in ("revisions_en_attente", "reclasser_en_attente", "comprendre_en_attente", "relire_avec_claude",
                "ranger_en_attente", "pdf_manquants", "nettoyer_inbox"):
        monkeypatch.setattr(worker, nom, lambda *a, n=nom: fait.append(n))
    monkeypatch.setattr(worker, "lire_inbox", lambda kd: fait.append("inbox") or [])
    monkeypatch.setattr(worker, "a_traiter", lambda kd, items, sans_claude=False: fait.append(
        f"a_traiter sans_claude={sans_claude}") or [])
    monkeypatch.setattr(worker, "analyser_inbox", lambda kd, items, mode, claude_ok: fait.append(
        f"analyses {mode} claude={claude_ok}"))


def test_tour_sans_claude_continue_le_reste(worker, monkeypatch):
    fait = []
    _tour(worker, monkeypatch, fait)
    monkeypatch.setattr(worker, "CLAUDE", worker.Garde("Claude"))
    monkeypatch.setattr(worker, "KDRIVE", worker.Garde("kDrive"))
    worker.CLAUDE.echec("limite", "usage limit", time.time() + 3600)
    worker.tour(None, float("-inf"))
    # Claude en pause : les scans sont lus sur la VM, sans attendre la passe IA sur les dossiers
    assert fait == ["ranger_en_attente", "pdf_manquants", "inbox", "a_traiter sans_claude=True",
                    "analyses local claude=False", "nettoyer_inbox"]
    fait.clear()
    monkeypatch.setattr(worker, "reglages_analyse", lambda: {"mode": "jamais", "relire": True})
    worker.tour(None, float("-inf"))
    assert fait == ["ranger_en_attente", "pdf_manquants", "inbox", "nettoyer_inbox"]
    fait.clear()
    worker.PAUSE.set()  # analyses en pause : ni Claude ni local
    monkeypatch.setattr(worker, "reglages_analyse", lambda: {"mode": "local", "relire": True})
    worker.tour(None, float("-inf"))
    worker.PAUSE.clear()
    assert fait == ["ranger_en_attente", "pdf_manquants", "inbox", "nettoyer_inbox"]


def test_tour_avec_claude_et_mode_toujours_local(worker, monkeypatch):
    fait = []
    _tour(worker, monkeypatch, fait)
    monkeypatch.setattr(worker, "CLAUDE", worker.Garde("Claude"))
    monkeypatch.setattr(worker, "KDRIVE", worker.Garde("kDrive"))
    worker.tour(None, time.monotonic())
    assert "analyses local claude=True" in fait and "relire_avec_claude" in fait
    fait.clear()
    monkeypatch.setattr(worker, "reglages_analyse", lambda: {"mode": "toujours", "relire": False})
    worker.tour(None, time.monotonic())
    assert "analyses toujours claude=False" in fait and "relire_avec_claude" not in fait


def test_repartir_local_d_abord(worker, monkeypatch):
    items = [{"id": i, "name": f"{i}.pdf", "dossiers": []} for i in range(10)]
    monkeypatch.setattr(worker.ressources, "sature", lambda: None)
    monkeypatch.setattr(worker.ressources.DUREES, "moyenne", 60.0)  # 10 documents : 10 min, sous les 30 min
    loc, cla = worker.repartir(items, "local", True)
    assert [i["id"] for i in loc] == list(range(worker.LOT_LOCAL)) and cla == []  # le reste attend le local
    monkeypatch.setattr(worker.ressources.DUREES, "moyenne", 600.0)  # 100 min de file : Claude prend le surplus
    loc, cla = worker.repartir(items, "local", True)
    assert len(loc) == worker.LOT_LOCAL and [i["id"] for i in cla] == list(range(worker.LOT_LOCAL, 10))[:worker.LOT]
    monkeypatch.setattr(worker.ressources, "sature", lambda: "mémoire presque pleine")
    loc, cla = worker.repartir(items, "local", True)
    assert loc == [] and len(cla) == min(10, worker.LOT)
    loc, cla = worker.repartir(items, "local", False)  # sans Claude, le local fait tout, à son rythme
    assert len(loc) == worker.LOT_LOCAL and cla == []


def test_repartir_choix_a_la_main_et_autres_modes(worker, monkeypatch):
    monkeypatch.setattr(worker.ressources, "sature", lambda: None)
    items = [{"id": 1, "name": "a", "dossiers": [], "moteur": "claude"},
             {"id": 2, "name": "b", "dossiers": [], "moteur": "local"},
             {"id": 3, "name": "c", "dossiers": []}]
    ids = lambda l: [i["id"] for i in l]
    assert tuple(map(ids, worker.repartir(items, "local", True))) == ([2, 3], [1])
    assert tuple(map(ids, worker.repartir(items, "auto", True))) == ([2], [1, 3])
    assert tuple(map(ids, worker.repartir(items, "jamais", True))) == ([2], [1, 3])
    assert tuple(map(ids, worker.repartir(items, "toujours", False))) == ([2, 3], [])
    assert tuple(map(ids, worker.repartir(items, "auto", False))) == ([2, 3], [])  # le choix « Claude » attend


def test_analyses_en_parallele_et_arret_a_la_limite(worker, monkeypatch):
    import threading
    from claunote.agent import LimiteAtteinte
    monkeypatch.setattr(worker, "activite", lambda texte: None)
    monkeypatch.setattr(worker.db, "matieres", lambda: [])
    barriere, faits, limite_vue = threading.Barrier(worker.PARALLELE, timeout=5), [], threading.Event()

    def traiter(kd, item, matieres, dossiers):
        if item["id"] < worker.PARALLELE:
            barriere.wait()  # PARALLELE analyses tournent bien en même temps
        else:
            limite_vue.wait(5)  # les suivants attendent la limite : sinon le fil en plus les enchaîne tous
        if item["id"] == 1:
            limite_vue.set()
            raise LimiteAtteinte("limit reached")
        faits.append(item["id"])

    monkeypatch.setattr(worker, "traiter", traiter)
    items = [{"id": i, "name": f"{i}.pdf", "dossiers": []} for i in range(20)]
    with pytest.raises(LimiteAtteinte):
        worker.analyser_inbox(None, items, "auto", True)
    # la limite arrête la distribution : les documents suivants attendent le tour d'après
    assert 1 not in faits and len(faits) < len(items[:worker.LOT]) - 1


# --- Documents personnels et gros scans ---------------------------------------------------

class KDFichier(KDImport):
    """Faux kDrive dont les fichiers téléchargés ont un vrai contenu."""

    def __init__(self, contenus):
        super().__init__({})
        self.contenus, self.n = contenus, 1000

    def download(self, fid, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.contenus[fid])
        return dest

    def upload(self, dir_id, nom, contenu, conflict="version"):
        self.n += 1
        self.envois.append((dir_id, nom, self.n))
        return {"id": self.n, "name": nom}


@pytest.fixture
def base(worker, monkeypatch, tmp_path):
    """Base de données en mémoire pour les tables touchées par le garde-fou et le découpage."""
    b = {"notes": [], "import": [], "marques": [], "supprimes": [], "decoupage": {}, "notifs": []}
    monkeypatch.setattr(worker, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(worker.db, "creer", lambda kid, nom: 1)
    monkeypatch.setattr(worker.db, "supprimer", lambda i: b["supprimes"].append(i))
    monkeypatch.setattr(worker.db, "import_oublier", lambda ids: b["marques"].append((ids, "oublie")))
    monkeypatch.setattr(worker.db, "import_enregistrer", lambda lot, fs: b["import"].extend(fs))
    monkeypatch.setattr(worker.db, "import_marquer", lambda ids, d, **k: b["marques"].append((ids, d)))
    monkeypatch.setattr(worker.db, "sensible_noter", lambda *a, **k: b["notes"].append(a))
    monkeypatch.setattr(worker.db, "decoupage", lambda kid: b["decoupage"].get(kid))
    monkeypatch.setattr(worker.db, "decoupage_maj", lambda kid, **c: b["decoupage"].setdefault(kid, {}).update(c))
    monkeypatch.setattr(worker.notifications, "document_personnel", lambda *a: b["notifs"].append(a))
    monkeypatch.setattr(worker.notifications, "decoupage_fini", lambda *a: b["notifs"].append(a))
    monkeypatch.setattr(worker, "analyser", lambda *a: pytest.fail("un document personnel est parti à l'IA"))
    return b


def test_rib_a_la_corbeille_sans_ia(worker, base):
    from tests.test_sensible import RIB
    kd = KDFichier({7: RIB.encode()})
    worker.traiter(kd, {"id": 7, "name": "notes.txt", "dossiers": ["Vrac"]})
    assert kd.supprimes == [7] and base["supprimes"] == [1]
    assert base["notes"][0][:2] == (7, "notes.txt") and base["notes"][0][5] == "corbeille"
    assert base["notifs"] == [("notes.txt", "coordonnées bancaires", True)]


def test_document_douteux_attend_ton_avis(worker, base):
    kd = KDFichier({7: "Exercice : analysez ce bulletin de paie.".encode()})
    worker.traiter(kd, {"id": 7, "name": "exo.txt", "dossiers": ["Vrac"]})
    assert kd.supprimes == [] and base["marques"] == [([7], "sensible")]
    assert base["notes"][0][5] == "doute" and base["import"][0].decision == "sensible"


def test_pas_de_garde_fou_si_tu_as_dit_que_c_est_un_cours(worker, base, monkeypatch):
    from tests.test_sensible import RIB
    monkeypatch.setattr(worker.db, "sensible_decision", lambda kid: "analyser")
    vus = []
    monkeypatch.setattr(worker, "analyser", lambda chemin, *a: vus.append(chemin.name) or (_ for _ in ()).throw(
        worker.ErreurAgent("stop")))
    monkeypatch.setattr(worker.db, "variantes_de", lambda kid: [])
    monkeypatch.setattr(worker.db, "maj", lambda *a, **k: None)
    monkeypatch.setattr(worker.db, "import_lot", lambda lot: None)
    worker.traiter(KDFichier({7: RIB.encode()}), {"id": 7, "name": "exemple-rib-cejm.txt", "dossiers": ["CEJM"]})
    assert vus == ["exemple-rib-cejm.txt"]


def test_gros_scan_decoupe_et_reprise(worker, base, monkeypatch, tmp_path):
    from pypdf import PdfWriter
    import io
    w = PdfWriter()
    for _ in range(8):
        w.add_blank_page(100, 100)
    tampon = io.BytesIO()
    w.write(tampon)
    monkeypatch.setattr(worker.decoupage, "SEUIL_PAGES", 5)
    monkeypatch.setattr(worker.decoupage, "PAQUET", 4)
    perso = {"niveau": "sur", "categorie": "banque", "raisons": ["IBAN valide"]}
    lecture = [{"page": n, "blanche": n == 3, "sensible": perso if n == 6 else None, "debut": n in (1, 4),
                "titre": None, "matiere": None, "mots": 10} for n in range(1, 9)]

    def lire_local(chemin, miniatures, progression=None):
        miniatures.mkdir(parents=True)
        return lecture
    monkeypatch.setattr(worker.decoupage, "lire_local", lire_local)
    appels = []

    def demander(paquet, miniatures, precedente):
        appels.append(paquet[0]["page"])
        if paquet[0]["page"] == 5:
            raise worker.LimiteAtteinte("limite")
        return {1: {"nouveau": True, "titre": "Suites"}, 2: {"nouveau": False}, 4: {"nouveau": True, "titre": "VLAN"}}
    monkeypatch.setattr(worker.decoupage, "demander_coupes", demander)
    kd = KDFichier({7: tampon.getvalue()})
    item = {"id": 7, "name": "Année BTS1.pdf", "dossiers": ["Scans"], "size": 1}
    with pytest.raises(worker.LimiteAtteinte):  # limite du forfait au 2e paquet : on reprendra
        worker.traiter(kd, item)
    assert base["decoupage"][7]["statut"] == "decoupage" and list(base["decoupage"][7]["coupes"]) == ["1"]
    assert kd.envois == []

    def demander_ensuite(paquet, miniatures, precedente):
        appels.append(paquet[0]["page"])
        assert precedente == {"page": 4, "titre": "VLAN", "matiere": None}
        return {5: {"nouveau": False}, 7: {"nouveau": True, "titre": "DS anglais"}, 8: {"nouveau": False}}
    monkeypatch.setattr(worker.decoupage, "demander_coupes", demander_ensuite)
    worker.traiter(kd, item)
    assert appels == [1, 5, 5]  # le 1er paquet n'est pas redemandé à Claude
    noms = [e[1] for e in kd.envois]
    assert noms[:3] == ["annee-bts1-1-suites.pdf", "annee-bts1-2-vlan.pdf", "annee-bts1-3-ds-anglais.pdf"]
    assert noms[3].startswith("annee-bts1-pages-6-personnel") and kd.supprimes == [1004, 7]
    plan = base["decoupage"][7]["plan"]
    assert [d["pages"] for d in plan["documents"]] == [[1, 2], [4, 5], [7, 8]]
    assert [f.raison for f in base["import"][:1]] == ["document 1/3 de « Année BTS1.pdf » (pages 1-2)"]
    assert base["decoupage"][7]["statut"] == "fait" and ([7], "decoupe") in base["marques"]
    assert ("Année BTS1.pdf", 3) in base["notifs"]


def test_illisible_sans_claude_attend_claude(worker, monkeypatch):
    """Un scan manuscrit illisible en local ne part pas en erreur : il attend Claude, et n'est pas relu en local."""
    from claunote.agent import ErreurAgent
    majs = []
    monkeypatch.setattr(worker.db, "creer", lambda i, n: 7)
    monkeypatch.setattr(worker.db, "maj", lambda i, **k: majs.append(k))
    monkeypatch.setattr(worker, "avant_analyse", lambda *a: False)
    monkeypatch.setattr(worker, "indice", lambda dossiers, nom: "")

    class KD:
        def download(self, fid, chemin):
            return chemin

    def illisible(*a, **k):
        raise ErreurAgent("aucun texte lisible")

    monkeypatch.setattr(worker.local, "analyser", illisible)
    worker.traiter(KD(), {"id": 5, "name": "cahier.pdf", "dossiers": [], "local": True})
    assert majs == [{"relire": True, "note": "Pas lisible sans Claude (aucun texte lisible) : en attente de Claude."}]
    kd = FauxKD({10: [{"id": 5, "name": "cahier.pdf", "type": "file"}]})
    monkeypatch.setattr(worker.db, "statuts_kdrive", lambda ids: {5: {"statut": "nouveau", "essais": 0, "relire": True}})
    monkeypatch.setattr(worker.db, "import_connus", lambda ids: {5: "analyser"})
    monkeypatch.setattr(worker.db, "lots_a_comprendre", lambda: [])
    monkeypatch.setattr(worker.db, "documents_orphelins", lambda ids, n: [])
    assert worker.a_traiter(kd, sans_claude=True) == []
    assert [i["name"] for i in worker.a_traiter(kd)] == ["cahier.pdf"]  # Claude revenu : il le lit


def test_arreter_puis_reprendre_aussitot_ne_bloque_pas(worker, monkeypatch):
    etat = {"analyses_arreter": "oui"}
    monkeypatch.setattr(worker.db, "etat_consommer", lambda k: etat.pop(k, None) is not None)
    monkeypatch.setattr(worker.db, "etat_valeur", lambda k: etat.get(k))
    monkeypatch.setattr(worker.ARRET, "wait", lambda s: worker.ARRET.set())
    worker.ARRET.clear()
    worker.surveiller()
    worker.ARRET.clear()
    assert not worker.STOP.is_set() and not worker.PAUSE.is_set()
    etat.update(analyses_arreter="oui", analyses_pause="oui")
    tues = []
    monkeypatch.setattr(worker.agent, "interrompre", lambda: tues.append(1) or 2)
    monkeypatch.setattr(worker, "publier", lambda: None)
    worker.surveiller()
    worker.ARRET.clear()
    assert worker.STOP.is_set() and worker.PAUSE.is_set() and tues == [1]
    worker.STOP.clear()
    worker.PAUSE.clear()


def test_lot_mixte_claude_et_local(worker, monkeypatch):
    monkeypatch.setattr(worker, "activite", lambda texte: None)
    monkeypatch.setattr(worker.db, "matieres", lambda: [])
    vus = []
    monkeypatch.setattr(worker, "traiter", lambda kd, item, m, d: vus.append((item["name"], bool(item.get("local")))))
    items = [{"id": 1, "name": "a.pdf", "dossiers": [], "moteur": "claude"},
             {"id": 2, "name": "b.jpg", "dossiers": [], "moteur": "local"}]
    worker.analyser_inbox(None, items, "local", True)
    assert sorted(vus) == [("a.pdf", False), ("b.jpg", True)]


def test_reclasser_local_d_abord(worker, monkeypatch):
    majs = []
    monkeypatch.setattr(worker.db, "maj", lambda i, **k: majs.append(k))
    monkeypatch.setattr(worker.db, "marquer_a_ranger", lambda ids, v=True: None)
    monkeypatch.setattr(worker.agent, "reclasser", lambda doc, m: pytest.fail("Claude ne doit pas être appelé"))
    monkeypatch.setattr(worker.local, "reclasser", lambda doc, m, claude, claude_echec: Analyse(
        "t", niveau="BTS1", matiere="Réseaux", type="TP", confiance=0.8, modele="local", avis="Reclassé sur la VM."))
    worker.reclasser({"id": 1, "nom": "a.pdf", "titre": None, "chapitre": None}, ["Réseaux"])
    assert majs[0]["matiere"] == "Réseaux" and majs[0]["statut"] == "classe" and majs[0]["modele"] == "local"
    # local indisponible et Claude en pause : le document attend
    monkeypatch.setattr(worker.local, "reclasser", lambda *a, **k: None)
    monkeypatch.setattr(worker, "CLAUDE", worker.Garde("Claude"))
    worker.CLAUDE.echec("limite", "x", time.time() + 3600)
    majs.clear()
    worker.reclasser({"id": 1, "nom": "a.pdf", "titre": None, "chapitre": None}, ["Réseaux"])
    assert majs == []


def test_besoin_claude_lit_le_scan(worker, monkeypatch):
    majs = []
    monkeypatch.setattr(worker.db, "creer", lambda i, n: 7)
    monkeypatch.setattr(worker.db, "maj", lambda i, **k: majs.append(k))
    monkeypatch.setattr(worker, "avant_analyse", lambda *a: False)
    monkeypatch.setattr(worker, "indice", lambda dossiers, nom: "")
    monkeypatch.setattr(worker, "ranger_doc", lambda *a: None)
    for nom in ("marquer_a_ranger", "import_marquer"):
        monkeypatch.setattr(worker.db, nom, lambda *a, **k: None)
    monkeypatch.setattr(worker.db, "document", lambda i: {"id": 7, "statut": "classe", "niveau": "BTS1",
                                                          "matiere": "Réseaux", "type": "Cours"})

    class KD:
        def download(self, fid, chemin):
            return chemin

        def rename(self, *a):
            pass

    def besoin(*a, **k):
        raise worker.local.BesoinClaude("manuscrit")

    monkeypatch.setattr(worker.local, "analyser", besoin)
    monkeypatch.setattr(worker, "analyser", lambda *a: Analyse("lu par Claude", titre="VLAN", niveau="BTS1",
                                                               matiere="Réseaux", type="Cours", confiance=0.9,
                                                               modele="sonnet"))
    worker.traiter(KD(), {"id": 5, "name": "cahier.pdf", "dossiers": [], "local": True})
    assert majs[0]["transcription"] == "lu par Claude" and majs[0]["modele"] == "sonnet" and majs[0]["relire"] is False


def test_delai_entre_deux_tours(worker):
    assert worker.delai(True, 99999) == 5                                  # encore du travail : on enchaîne
    assert worker.delai(False, 60) == worker.INTERVALLE                    # quelque chose vient de bouger
    assert worker.delai(False, worker.CALME) == worker.INTERVALLE_CALME   # inbox inchangée depuis 30 min


def test_analyses_locales_respirent(worker, monkeypatch):
    """Budget dépassé : une pause avant chaque analyse locale, pas pendant le travail pour Claude."""
    pauses, faits = [], []
    monkeypatch.setattr(worker.ressources.BUDGET, "freine", True)
    monkeypatch.setattr(worker.ressources, "respirer", lambda arret: pauses.append(1) or 0)
    monkeypatch.setattr(worker, "activite", lambda t: None)
    worker.en_parallele([1, 2], faits.append, str, 1, continuer=lambda: True, freiner=True)
    assert faits == [1, 2] and len(pauses) == 2
    worker.en_parallele([3], faits.append, str, 1, continuer=lambda: True)
    assert faits == [1, 2, 3] and len(pauses) == 2


def test_memoire_basse_decharge_les_modeles_au_repos(worker, monkeypatch):
    appels = []
    monkeypatch.setattr(worker, "_liberation", {"le": float("-inf")})
    monkeypatch.setattr(worker.local, "liberer", lambda: appels.append("ollama") or True)
    monkeypatch.setattr(worker.lecture, "liberer", lambda: appels.append("lecture") or ["docling"])
    monkeypatch.setattr(worker.ressources, "local_actif", lambda: False)
    monkeypatch.setattr(worker.ressources, "memoire_libre_mo", lambda: 5000)
    assert not worker.liberer_si_besoin(1000) and appels == []           # mémoire large : rien
    monkeypatch.setattr(worker.ressources, "memoire_libre_mo", lambda: 700)
    assert worker.liberer_si_besoin(1000) and appels == ["ollama", "lecture"]
    assert not worker.liberer_si_besoin(1300)                             # pas deux fois en 10 min
    monkeypatch.setattr(worker.ressources, "local_actif", lambda: True)
    assert not worker.liberer_si_besoin(5000)                             # une analyse locale tourne : on attend


def test_corbeille_kdrive(worker, monkeypatch):
    import httpx
    faits, a_faire = [], [11, 12, 13]

    class KD:
        def delete(self, i):
            if i == 12:   # déjà supprimé à la main dans kDrive
                raise httpx.HTTPStatusError("x", request=None, response=httpx.Response(404))
            if i == 13:
                raise worker.KDriveIndisponible("panne")
    monkeypatch.setattr(worker.db, "corbeille", lambda: list(a_faire))
    monkeypatch.setattr(worker.db, "corbeille_faite", faits.append)
    try:
        worker.vider_corbeille(KD())
    except worker.KDriveIndisponible:
        pass
    assert faits == [11, 12]          # 13 reste dans la file : réessayé quand kDrive revient


def test_index_avance_pendant_une_panne_kdrive(worker, monkeypatch):
    fait = []
    monkeypatch.setattr(worker.KDRIVE, "disponible", lambda: False)
    monkeypatch.setattr(worker.CLAUDE, "disponible", lambda: False)
    monkeypatch.setattr(worker.local, "disponible", lambda: "absent")
    monkeypatch.setattr(worker.index, "indexer", lambda n, a: fait.append("index") or 0)
    monkeypatch.setattr(worker.index, "vectoriser", lambda n, a: fait.append("sens") or 0)
    monkeypatch.setitem(worker.ETAT, "reste", False)
    worker.tour(None, 0.0)
    assert fait == ["index", "sens"]


def test_panne_kdrive_ne_perd_rien(worker, monkeypatch):
    """Panne kDrive : copie de PDF et choix d'import remontent la panne (disjoncteur), rien n'est oublié."""
    oublies = []

    class KD:
        def ensure_path(self, *a):
            raise worker.KDriveIndisponible("panne")
        delete = move = ensure_path
    monkeypatch.setattr(worker.db, "documents", lambda ids: [])
    monkeypatch.setattr(worker.db, "maj_pdf_revision", lambda *a: oublies.append("pdf"))
    with pytest.raises(worker.KDriveIndisponible):
        worker.copier_pdf(KD(), {"id": 1, "reperes": {}, "genre": "fiche", "sujet": "x", "matiere": "Maths"})
    monkeypatch.setattr(worker.db, "import_a_executer", lambda: [{"kdrive_id": 5, "decision": "supprimer", "chemin": "a"}])
    monkeypatch.setattr(worker.db, "import_oublier", oublies.append)
    with pytest.raises(worker.KDriveIndisponible):
        worker.executer_choix(KD())
    assert oublies == []          # ni essai de PDF compté, ni choix d'import oublié


def test_index_en_retard_garde_le_rythme(worker, monkeypatch):
    monkeypatch.setitem(worker.ETAT, "reste", False)
    monkeypatch.setattr(worker.index, "indexer", lambda n, a: 3)
    monkeypatch.setattr(worker.index, "vectoriser", lambda n, a: 128)      # lot plein : il en reste
    worker.indexer_si_calme()
    assert worker.ETAT["index_en_retard"] is True
    monkeypatch.setattr(worker.index, "vectoriser", lambda n, a: 40)       # fini
    worker.indexer_si_calme()
    assert worker.ETAT["index_en_retard"] is False


def test_repartir_modeles_locaux_eteints(worker, monkeypatch):
    items = [{"id": 1, "moteur": "local"}, {"id": 2, "moteur": "claude"}, {"id": 3}]
    monkeypatch.setattr(worker.ressources, "locaux_eteints", lambda: True)
    loc, cla = worker.repartir(items, "local", True)
    assert loc == [] and [i["id"] for i in cla] == [1, 2, 3]   # même ceux confiés au local partent chez Claude
    assert worker.repartir(items, "toujours", False) == ([], [])


def test_modification_de_revision(worker, monkeypatch):
    """Une révision avec une modification en attente est retouchée, pas régénérée ; ratée, l'ancienne reste."""
    from claunote import db
    from claunote.agent import ErreurAgent
    vu = {}
    monkeypatch.setattr(worker.revision, "generer", lambda *a, **k: pytest.fail("pas une régénération"))
    monkeypatch.setattr(worker.revision, "retoucher", lambda rev, demande, docs, **k: vu.update(demande=demande) or
                        ("# Fiche modifiée", []))
    monkeypatch.setattr(db, "pieces_jointes", lambda r=None, c=None, contenu=False: [])
    monkeypatch.setattr(db, "Condenses", dict)
    monkeypatch.setattr(db, "documents", lambda ids: [])
    monkeypatch.setattr(db, "finir_retouche", lambda i, contenu=None, web=None, erreur=None: vu.update(
        fin=(i, contenu, erreur)))
    monkeypatch.setattr(worker, "copier_pdf", lambda kd, rev: vu.update(pdf=True))
    monkeypatch.setattr(db, "revision", lambda i: {"id": i})
    rev = {"id": 4, "genre": "fiche", "sujet": "Suites", "doc_ids": [3], "date_eval": None, "contenu": "# Fiche",
           "retouche": "plus court"}
    worker.traiter_revision(None, rev)
    assert vu["demande"] == "plus court" and vu["fin"] == (4, "# Fiche modifiée", None) and vu["pdf"]

    def rate(*a, **k):
        raise ErreurAgent("réponse trop courte")
    monkeypatch.setattr(worker.revision, "retoucher", rate)
    worker.traiter_revision(None, rev)
    assert vu["fin"][1] is None and "réponse trop courte" in vu["fin"][2]


def _revision_simple(worker, monkeypatch, vu):
    from claunote import db
    monkeypatch.setattr(db, "pieces_jointes", lambda r=None, c=None, contenu=False: [])
    monkeypatch.setattr(db, "Condenses", dict)
    monkeypatch.setattr(db, "documents", lambda ids: [{"id": 3, "titre": "TP VLAN", "nom": "x.pdf"}])
    monkeypatch.setattr(db, "maj_revision", lambda rev_id, statut, **k: vu.update(statut=statut, **k))
    monkeypatch.setattr(worker, "copier_pdf", lambda kd, rev: None)
    monkeypatch.setattr(db, "revision", lambda i: {"id": i})
    monkeypatch.setattr(worker.notifications, "revision_prete", lambda rev: None)
    monkeypatch.setattr(worker.coherence, "preparer", lambda rev: ("\n\nREPERES", [{"id": 1}]))


def test_fiche_hors_sujet_refaite_une_fois(worker, monkeypatch):
    """Contrôle hors sujet : une nouvelle version recentrée par Opus, avec les écarts de la première."""
    vu, versions, controles = {}, [], [{"ok": False, "ecarts": ["partie OSPF"], "anciens": [1]},
                                       {"ok": True, "ecarts": [], "anciens": [1]}]
    _revision_simple(worker, monkeypatch, vu)
    monkeypatch.setattr(worker.revision, "generer", lambda *a, **k: versions.append(k) or
                        (f"# Fiche {len(versions)}", {"D1": 3}, []))
    monkeypatch.setattr(worker.coherence, "controler", lambda rev, contenu, docs, anciens: controles.pop(0))
    worker.traiter_revision(None, {"id": 4, "genre": "fiche", "sujet": "VLAN", "doc_ids": [3], "date_eval": None})
    assert [k["reperes_perimetre"] for k in versions] == ["\n\nREPERES"] * 2
    assert versions[0]["recentrer"] == "" and "partie OSPF" in versions[1]["recentrer"]
    assert vu["contenu"] == "# Fiche 2" and vu["controle"]["ok"] and vu["controle"]["recentre"]
    assert vu["controle"]["ecarts_avant"] == ["partie OSPF"]


def test_fiche_dans_le_sujet_pas_refaite(worker, monkeypatch):
    vu, versions = {}, []
    _revision_simple(worker, monkeypatch, vu)
    monkeypatch.setattr(worker.revision, "generer", lambda *a, **k: versions.append(k) or ("# Fiche", {}, []))
    monkeypatch.setattr(worker.coherence, "controler", lambda *a: {"ok": True, "ecarts": [], "anciens": []})
    worker.traiter_revision(None, {"id": 4, "genre": "fiche", "sujet": "VLAN", "doc_ids": [3], "date_eval": None})
    assert len(versions) == 1 and vu["statut"] == "pret"


def test_recentrage_rate_garde_la_premiere_version(worker, monkeypatch):
    vu, versions = {}, []
    _revision_simple(worker, monkeypatch, vu)

    def generer(*a, **k):
        versions.append(k)
        if k["recentrer"]:
            raise worker.agent.OpusEnPause("limite", None)
        return "# Fiche 1", {}, []
    monkeypatch.setattr(worker.revision, "generer", generer)
    monkeypatch.setattr(worker.coherence, "controler", lambda *a: {"ok": False, "ecarts": ["OSPF"], "anciens": []})
    worker.traiter_revision(None, {"id": 4, "genre": "fiche", "sujet": "VLAN", "doc_ids": [3], "date_eval": None})
    assert vu["contenu"] == "# Fiche 1" and vu["controle"]["ok"] is False  # écarts affichés sur la fiche


def test_les_fiches_attendent_opus(worker, monkeypatch):
    """Opus 5.5 en limite : les créations restent en attente (pas de Sonnet), sans mettre Claude en pause."""
    from claunote import modeles
    modeles._indisponibles.clear()
    monkeypatch.delenv("CLAUDE_REPLI_CREATION", raising=False)
    faites = []
    monkeypatch.setattr(worker.db, "campagnes_en_attente", lambda: [])
    monkeypatch.setattr(worker.db, "revisions_en_attente", lambda: [
        {"id": 5, "genre": "fiche", "sujet": "VLAN"}, {"id": 6, "genre": "quiz", "sujet": "OSPF"}])

    def traiter(kd, rev):
        faites.append(rev["id"])
        raise worker.agent.OpusEnPause("Opus weekly limit", None)
    monkeypatch.setattr(worker, "traiter_revision", traiter)
    try:
        worker.revisions_en_attente(FauxKD({}))
        assert faites == [5]   # la 2e n'essaie même pas : Opus est en pause
        assert modeles.attente_opus() is not None and worker.CLAUDE.disponible()
    finally:
        modeles._indisponibles.clear()


# --- Comptes : le travail de chacun, avec ou sans le forfait Claude de l'admin ------------------------

def _invite(faux_comptes, claude: bool):
    i = faux_comptes.creer_compte("camille", "h", "invite", ["bibliotheque", "deposer"] + (["claude"] if claude else []))
    return i


def test_fichier_d_un_invite_sans_forfait_lu_en_local(worker, monkeypatch, faux_comptes):
    invite = _invite(faux_comptes, claude=False)
    faux_comptes.depots[2] = invite
    monkeypatch.setattr(worker.db, "statuts_kdrive", lambda ids: {})
    monkeypatch.setattr(worker.db, "import_connus", lambda ids: {1: "analyser", 2: "analyser"})
    monkeypatch.setattr(worker.db, "lots_a_comprendre", lambda: [])
    monkeypatch.setattr(worker.db, "documents_orphelins", lambda ids, n: [])
    items = worker.a_traiter(FauxKD({}), [{"id": 1, "name": "a.pdf", "type": "file", "dossiers": []},
                                          {"id": 2, "name": "b.pdf", "type": "file", "dossiers": []}])
    par_id = {i["id"]: i for i in items}
    assert par_id[1].get("moteur") is None and "compte_id" not in par_id[1]        # l'admin : comme avant
    assert par_id[2]["compte_id"] == invite and par_id[2]["moteur"] == "local" and par_id[2]["sans_forfait"]
    # modèles locaux éteints : le fichier de l'invité attend, il ne part jamais chez Claude
    monkeypatch.setattr(worker.ressources, "locaux_eteints", lambda: True)
    pour_local, pour_claude = worker.repartir(items, "local", True)
    assert [i["id"] for i in pour_claude] == [1] and pour_local == []


def test_revision_d_un_invite_sans_forfait(worker, monkeypatch, faux_comptes):
    invite = _invite(faux_comptes, claude=False)
    fait = []
    monkeypatch.setattr(worker.db, "campagnes_en_attente", lambda: [])
    monkeypatch.setattr(worker.db, "revisions_en_attente", lambda: [
        {"id": 5, "genre": "fiche", "sujet": "VLAN", "compte_id": invite}])
    monkeypatch.setattr(worker.db, "maj_revision", lambda i, statut, **k: fait.append((i, statut, k.get("erreur"))))
    monkeypatch.setattr(worker, "traiter_revision", lambda kd, rev: fait.append("claude appelé"))
    monkeypatch.setattr(worker.db, "mettre_en_demande", lambda table, i: fait.append(("demande", table, i)) or True)
    notifs = []
    monkeypatch.setattr(worker.db, "notifier_compte", lambda *a: notifs.append(a) or True)
    worker.revisions_en_attente(FauxKD({}))
    assert fait == [("demande", "revisions", 5)]            # chez l'admin, Claude pas appelé
    assert notifs and notifs[0][0] is None and notifs[0][4] == "/demandes"   # notification aux admins


def test_revision_d_un_invite_acceptee_par_l_admin(worker, monkeypatch, faux_comptes):
    invite = _invite(faux_comptes, claude=False)
    vus = []
    monkeypatch.setattr(worker.db, "campagnes_en_attente", lambda: [
        {"id": 3, "titre": "BTS blanc", "compte_id": invite, "validation": "attente"}])
    monkeypatch.setattr(worker.db, "revisions_en_attente", lambda: [
        {"id": 5, "genre": "fiche", "sujet": "VLAN", "compte_id": invite, "validation": "acceptee"},
        {"id": 6, "genre": "quiz", "sujet": "DHCP", "compte_id": invite, "validation": "attente"}])
    monkeypatch.setattr(worker.db, "mettre_en_demande", lambda table, i: False)   # déjà chez l'admin
    monkeypatch.setattr(worker, "traiter_campagne", lambda k: vus.append(("campagne", k["id"])))
    monkeypatch.setattr(worker, "traiter_revision",
                        lambda kd, rev: vus.append((rev["id"], worker.db.compte_actuel(), worker.agent.forfait_autorise())))
    worker.revisions_en_attente(FauxKD({}))
    assert vus == [(5, invite, True)]   # oui de l'admin : sur son forfait, au nom de l'invité ; le reste attend


def test_revision_d_un_invite_avec_forfait(worker, monkeypatch, faux_comptes):
    invite = _invite(faux_comptes, claude=True)
    vus = []
    monkeypatch.setattr(worker.db, "campagnes_en_attente", lambda: [])
    monkeypatch.setattr(worker.db, "revisions_en_attente", lambda: [
        {"id": 5, "genre": "fiche", "sujet": "VLAN", "compte_id": invite}])
    monkeypatch.setattr(worker, "traiter_revision",
                        lambda kd, rev: vus.append((worker.db.compte_actuel(), worker.agent.forfait_autorise())))
    worker.revisions_en_attente(FauxKD({}))
    assert vus == [(invite, True)]   # travail fait au nom de l'invité (ses cours seulement)


def test_aucun_appel_a_claude_sans_forfait():
    from claunote import agent
    with agent.forfait(False):
        with pytest.raises(agent.SansForfait):
            agent.appeler_claude("x", __import__("pathlib").Path("."))


def test_rangement_des_fichiers_d_un_invite(worker, faux_comptes):
    invite = _invite(faux_comptes, claude=False)
    assert worker.racine(1) == [] and worker.racine(None) == []
    assert worker.racine(invite) == ["comptes", "camille"]

def test_titre_raccourci_remplace_par_celui_du_support(worker, monkeypatch):
    from claunote import db, notifications
    vu = {}
    monkeypatch.setattr(worker.revision, "generer", lambda *a, **k: (
        "# ALGÈBRE DE BOOLE — FICHE SYNTHÈSE\n## 1. AXIOMES\n- a", {}, []))
    monkeypatch.setattr(db, "pieces_jointes", lambda r=None, c=None, contenu=False: [])
    monkeypatch.setattr(db, "Condenses", dict)
    monkeypatch.setattr(db, "documents", lambda ids: [])
    monkeypatch.setattr(db, "maj_revision", lambda *a, **k: None)
    monkeypatch.setattr(db, "renommer_revision", lambda i, s: vu.update(titre=s))
    monkeypatch.setattr(worker, "copier_pdf", lambda kd, rev: None)
    monkeypatch.setattr(db, "revision", lambda i: {"id": i})
    monkeypatch.setattr(notifications, "revision_prete", lambda rev: None)
    rev = {"id": 4, "genre": "fiche", "sujet": "Mathématiques : existe parce que la numération…", "doc_ids": [3],
           "date_eval": None, "matiere": "Mathématiques"}
    worker.traiter_revision(None, rev)
    assert vu["titre"] == "Mathématiques : Algèbre de boole"
    vu.clear()
    worker.traiter_revision(None, {**rev, "sujet": "Mathématiques : Boole"})   # titre déjà court : gardé
    assert "titre" not in vu



# --- Formation et règles de rangement (formations.py) ----------------------------------

REF_BTS1 = {"formation": "BTS SIO option SISR", "matieres": [{"nom": "Mathématiques", "officiel": "Maths info",
                                                              "contenus": []}, {"nom": "Réseaux", "contenus": []}],
            "correspondances": []}


def test_ranger_matiere(worker, monkeypatch):
    monkeypatch.setattr(worker.db, "matieres_distinctes", lambda: {})
    refs = {"BTS1": REF_BTS1}
    a = Analyse("t", niveau="BTS1", matiere="Maths")
    assert worker.ranger_matiere(a, [], {}, refs) == "Mathématiques"
    a = Analyse("t", niveau="BTS1", matiere="Bureautique")
    assert worker.ranger_matiere(a, [], {("BTS1", "Bureautique"): "Réseaux"}, refs) == "Réseaux"
    a = Analyse("t", niveau="BTS1", matiere="Mathématiques")
    assert worker.ranger_matiere(a, [], {}, refs, ["BTS1", "Réseaux"]) == "Réseaux"   # matière choisie au dépôt
    assert "choisie au dépôt" in a.avis


def test_reglages_matieres_sans_base(worker, monkeypatch):
    def panne():
        raise RuntimeError("pas de base")
    monkeypatch.setattr(worker.db, "matieres_regles", panne)
    assert worker.reglages_matieres() == ({}, {}, "")
    monkeypatch.setattr(worker.db, "matieres_regles", lambda: {("BTS1", "Maths"): "Mathématiques"})
    monkeypatch.setattr(worker.db, "formations", lambda: [
        {"niveau": "BTS1", "nom": "SISR", "statut": "pret", "referentiel": REF_BTS1},
        {"niveau": "BTS2", "nom": "SISR", "statut": "a_chercher", "referentiel": None}])
    regles, refs, texte = worker.reglages_matieres()
    assert list(refs) == ["BTS1"] and "« Réseaux »" in texte and regles


def test_referentiels_en_attente(worker, monkeypatch):
    from claunote import agent
    maj = []
    monkeypatch.setattr(worker.db, "formations_a_chercher", lambda: [
        {"compte_id": 1, "niveau": "BTS1", "nom": "BTS SIO option SISR"},
        {"compte_id": 1, "niveau": "BTS2", "nom": "???"}])
    monkeypatch.setattr(worker.db, "maj_formation", lambda c, n, **k: maj.append((n, k.get("statut"))))
    monkeypatch.setattr(worker.db, "matieres_par_annee", lambda: [])
    monkeypatch.setattr(worker.db, "titres_par_matiere", lambda n=4: {})
    monkeypatch.setattr(agent, "forfait_autorise", lambda: True)

    def chercher(niveau, nom, comptes, titres):
        if nom == "???":
            raise worker.ErreurAgent("inconnue")
        return REF_BTS1, "opus"
    monkeypatch.setattr(worker.formations, "chercher", chercher)
    worker.referentiels_en_attente()
    assert maj == [("BTS1", "en_cours"), ("BTS1", "pret"), ("BTS2", "en_cours"), ("BTS2", "erreur")]
    maj.clear()

    def limite(*a):
        raise worker.LimiteAtteinte("limite")
    monkeypatch.setattr(worker.formations, "chercher", limite)
    with pytest.raises(worker.LimiteAtteinte):
        worker.referentiels_en_attente()
    assert maj == [("BTS1", "en_cours"), ("BTS1", "a_chercher")]
