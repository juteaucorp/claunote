import base64
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
def anonyme(monkeypatch):
    """Site sans session : seule la page de connexion répond."""
    appels = []
    monkeypatch.delenv("CLAUNOTE_UTILISATEUR", raising=False)
    monkeypatch.delenv("CLAUNOTE_MOT_DE_PASSE", raising=False)
    monkeypatch.delenv("CLAUNOTE_SECRET", raising=False)
    monkeypatch.setattr(db, "init", lambda: None)
    monkeypatch.setattr(db, "secret_session", lambda: "secret-de-test")
    etat = {}
    monkeypatch.setattr(db, "etat_valeur", lambda k: etat.get(k))
    monkeypatch.setattr(db, "etat_ecrire", lambda k, v: etat.update({k: v}))
    monkeypatch.setattr(db, "nb_hors_cours", lambda: 2)
    monkeypatch.setattr(db, "nb_documents", lambda: 8)
    monkeypatch.setattr(db, "evenements_a_venir", lambda genre=None, jours=60: [])
    monkeypatch.setattr(db, "documents_pronote", lambda limite=30: [])
    monkeypatch.setattr(db, "nb_cartes_a_revoir", lambda rev_id=None: 0)
    monkeypatch.setattr(db, "bibliotheque", lambda: [{"niveau": "BTS1", "matiere": "Réseaux", "type": "TP", "n": 3}])
    monkeypatch.setattr(db, "a_verifier", lambda: [{**DOC, "id": 2, "statut": "a_verifier", "confiance": 0.4,
                                                    "note": "Le dossier « BTS1 » indique BTS1", "origine": "BTS1"}])
    monkeypatch.setattr(db, "nb_a_verifier", lambda: 1)
    monkeypatch.setattr(db, "nb_import_a_confirmer", lambda: 0)
    monkeypatch.setattr(db, "nb_evaluations_proches", lambda: 0)
    monkeypatch.setattr(db, "document", lambda i: DOC if i == 1 else None)
    monkeypatch.setattr(db, "liste", lambda *a: [DOC])
    monkeypatch.setattr(db, "matieres", lambda: ["Réseaux"])
    monkeypatch.setattr(db, "recherche", lambda q: [{**DOC, "extrait": "les <b>VLAN</b> <i>x</i>"}])
    monkeypatch.setattr(db, "demander_scan", lambda: appels.append("scan"))
    monkeypatch.setattr(db, "maj", lambda i, **k: appels.append(("maj", i, k)))
    monkeypatch.setattr(db, "niveaux_matieres", lambda: [{"niveau": "BTS1", "matiere": "Réseaux"}])
    monkeypatch.setattr(db, "revisions", lambda limite=30: [])
    monkeypatch.setattr(db, "choisir_documents", lambda n, m, q="": [1] if q == "" or q == "vlan" else [])
    monkeypatch.setattr(db, "creer_revision", lambda *a, **k: appels.append(("rev",) + a + ((k,) if k.get("campagne_id") else ())) or 5)
    monkeypatch.setattr(db, "campagne_du_jour", lambda jour: None)
    monkeypatch.setattr(db, "campagnes", lambda limite=20: [])
    monkeypatch.setattr(db, "historique", lambda *a: ([], 0))
    monkeypatch.setattr(db, "compte_revisions", lambda: {"genres": {}, "attente": 0, "total": 0})
    monkeypatch.setattr(db, "matieres_revisions", lambda: ["Réseaux"])
    monkeypatch.setattr(db, "niveaux_matieres_comptes", lambda: [{"niveau": "BTS1", "matiere": "Réseaux", "n": 3}])
    monkeypatch.setattr(db, "documents_matiere", lambda n, m: [DOC] if m == "Réseaux" else [])
    monkeypatch.setattr(db, "documents", lambda ids: [DOC] if 1 in ids else [])
    with TestClient(main.app) as c:
        c.appels = appels
        c.etat = etat
        yield c


@pytest.fixture
def client(anonyme):
    r = anonyme.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "claunote"}, follow_redirects=False)
    assert r.status_code == 303
    return anonyme


def test_bibliotheque(client):
    r = client.get("/bibliotheque")
    assert r.status_code == 200
    assert "BTS1" in r.text and "TP · 3" in r.text and "1 document à vérifier" in r.text and '<span class="badge">1</span>' in r.text


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
    assert "confiance 40 %" in r.text and "indique BTS1" in r.text and "déposé dans « BTS1 »" in r.text


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
                                            "type": "Cours", "confiance": 1.0, "note": None, "erreur": None})
    assert client.post("/doc/1/classer", data={"niveau": "Master", "matiere": "x", "type": "TP"}).status_code == 400


def test_page_reviser(client):
    r = client.get("/reviser")
    assert r.status_code == 200 and "/reviser/nouvelle" in r.text and "/campagne/nouvelle" in r.text
    assert "Déjà fait" in r.text and "Épreuve blanche" in r.text
    r = client.get("/reviser/nouvelle")
    assert 'value="BTS1|Réseaux"' in r.text and "Plan de révision" in r.text and 'name="consigne"' in r.text


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


def test_logo_et_page_deposer(client):
    assert client.get("/static/logo.svg").status_code == 200
    r = client.get("/deposer")
    assert r.status_code == 200 and "webkitdirectory" in r.text and 'class="actif"' in r.text


def test_deposer_garde_les_dossiers(client, monkeypatch):
    faits = []

    class FauxKD:
        def ensure_path(self, parent, parts):
            faits.append(("path", parent, parts))
            return 900 + len(parts)

        def upload(self, dir_id, nom, contenu, conflict="version"):
            faits.append(("upload", dir_id, nom, contenu, conflict))
            return {"id": 1}

    monkeypatch.setattr(main.KDrive, "from_env", classmethod(lambda cls: FauxKD()))
    monkeypatch.setattr(main, "INBOX_ID", 10)
    fichiers = [("fichiers", ("Terminale/Maths/suites.pdf", b"%PDF-1", "application/pdf")),
                ("fichiers", ("Terminale/Maths/cours2.pdf", b"%PDF-2", "application/pdf")),
                ("fichiers", ("script.py", b"x", "text/plain")),
                ("fichiers", ("Projet/node_modules/lib/doc.pdf", b"x", "application/pdf")),
                ("fichiers", ("Terminale/~$suites.docx", b"x", "application/octet-stream")),
                ("fichiers", ("../../photo.JPG", b"img", "image/jpeg"))]
    r = client.post("/deposer", files=fichiers, data={"niveau": ""})
    assert r.status_code == 200 and "3 fichiers envoyés" in r.text and "script.py" in r.text
    assert "node_modules" in r.text and "~$suites.docx" in r.text
    assert ("path", 10, ["Terminale", "Maths"]) in faits and ("path", 10, []) in faits
    assert [f for f in faits if f[0] == "path"].count(("path", 10, ["Terminale", "Maths"])) == 1
    assert ("upload", 902, "suites.pdf", b"%PDF-1", "rename") in faits
    assert ("upload", 900, "photo.JPG", b"img", "rename") in faits
    assert "scan" in client.appels


def test_deposer_par_paquets_en_json(client, monkeypatch):
    class FauxKD:
        def ensure_path(self, parent, parts):
            return 900

        def upload(self, dir_id, nom, contenu, conflict="version"):
            return {"id": 1}

    monkeypatch.setattr(main.KDrive, "from_env", classmethod(lambda cls: FauxKD()))
    r = client.post("/deposer?paquet=1", files=[("fichiers", ("BTS1/Dev/cours.docx", b"PK", "application/octet-stream")),
                                                ("fichiers", ("BTS1/Dev/app.js", b"x", "text/javascript"))])
    assert r.json() == {"envoyes": ["BTS1/Dev/cours.docx"], "ignores": ["BTS1/Dev/app.js"]}
    page = client.get("/deposer").text
    assert '".docx"' in page and '"node_modules"' in page  # mêmes filtres côté navigateur


def test_page_import(client, monkeypatch):
    inutile = {"kdrive_id": 5, "lot": "Cours", "chemin": "Cours/Dev/.vscode", "nom": ".vscode", "categorie": "code",
               "decision": "inutile", "raison": "dossier « .vscode » d'outil de développement", "dossier": True}
    page = {"kdrive_id": 7, "lot": "Cours", "chemin": "Cours/Maths/IMG_1.jpg", "nom": "IMG_1.jpg", "ordre": 0}
    page2 = {**page, "kdrive_id": 8, "nom": "scan2.pdf", "ordre": 1}
    bilan = {"lot": "Cours", "le": None, "total": 9, "documents": 4, "en_attente": 2, "variantes": 1,
             "pages_fusionnees": 3, "fusions": 1, "inutiles": 1, "inutiles_traites": 0,
             "resume": "Rangé par année scolaire puis par matière.", "compris": {"Cours/2023-2024": {"niveau": "BTS1"}}}
    monkeypatch.setattr(db, "import_bilan", lambda: [bilan])
    monkeypatch.setattr(db, "import_dossiers", lambda lot: {"Cours/2023-2024": 3, "Cours/2023-2024/Réseaux/TP": 1})
    monkeypatch.setattr(db, "import_fichiers", lambda d, ids=None: [inutile] if d == "inutile" else [])
    monkeypatch.setattr(db, "import_groupes", lambda sur: ["Cours/Maths/img"])
    monkeypatch.setattr(db, "import_groupe", lambda g: [page, page2])
    r = client.get("/import")
    assert r.status_code == 200
    assert ".vscode" in r.text and "Supprimer la sélection" in r.text
    assert "/import/fichier/7" in r.text and 'name="coupes" value="1"' in r.text
    assert "Rangé par année scolaire" in r.text and "BTS1" in r.text and "Réseaux" in r.text and "TP" in r.text


def test_choix_import(client, monkeypatch):
    marques = []
    lignes = [{"kdrive_id": 5, "nom": "a.py", "dossier": False}, {"kdrive_id": 6, "nom": "b.pdf", "dossier": False}]
    monkeypatch.setattr(db, "import_fichiers", lambda d, ids=None: [l for l in lignes if l["kdrive_id"] in ids])
    monkeypatch.setattr(db, "import_marquer", lambda ids, d, **k: marques.append((ids, d)) or len(ids))
    r = client.post("/import/choix", data={"ids": ["5", "6"], "action": "supprimer"}, follow_redirects=False)
    assert r.status_code == 303 and marques[-1] == ([5, 6], "supprimer") and "corbeille" in r.headers["location"]
    client.post("/import/choix", data={"ids": ["5", "6"], "action": "analyser"}, follow_redirects=False)
    assert marques[-1] == ([6], "analyser")  # le .py ne s'analyse pas
    assert client.post("/import/choix", data={"ids": ["5"], "action": "tout"}).status_code == 400


def test_decouper_les_scans(client, monkeypatch):
    marques = []
    pages = [{"kdrive_id": 10 + i, "ordre": i} for i in range(5)]
    monkeypatch.setattr(db, "import_groupe", lambda g: pages)
    monkeypatch.setattr(db, "import_marquer", lambda ids, d, **k: marques.append((ids, d, k)))
    r = client.post("/import/decouper", data={"groupe": "C/img", "coupes": ["2", "4"]}, follow_redirects=False)
    assert r.status_code == 303 and "3+documents" in r.headers["location"]
    assert ([10], "page", {"groupe": "C/img#1", "ordre": 0, "sur": True}) in marques
    assert ([13], "page", {"groupe": "C/img#2", "ordre": 1, "sur": True}) in marques
    assert ([14], "analyser", {"groupe": None}) in marques
    marques.clear()
    client.post("/import/decouper", data={"groupe": "C/img", "action": "separer"}, follow_redirects=False)
    assert [m[1] for m in marques] == ["analyser"] * 5


def test_apercu_import_seulement_pour_les_fichiers_du_tri(client, monkeypatch):
    monkeypatch.setattr(db, "import_connus", lambda ids: {})
    assert client.get("/import/fichier/123").status_code == 404


def test_deposer_avec_niveau(client, monkeypatch):
    faits = []

    class FauxKD:
        def ensure_path(self, parent, parts):
            faits.append(parts)
            return 5

        def upload(self, *a, **k):
            return {}

    monkeypatch.setattr(main.KDrive, "from_env", classmethod(lambda cls: FauxKD()))
    client.post("/deposer", files=[("fichiers", ("a.pdf", b"1", "application/pdf"))], data={"niveau": "BTS2"})
    assert faits == [["BTS2"]]
    assert client.post("/deposer", files=[("fichiers", ("a.pdf", b"1"))], data={"niveau": "Master"}).status_code == 400


class FauxPronote:
    etat_ = {"connecte": False}
    appels = []

    def __init__(self, *a, **k):
        pass

    def etat(self):
        return self.etat_

    def connexion_qr(self, qr, pin):
        FauxPronote.appels.append(("qr", qr, pin))
        if pin == "0000":
            from claunote.pronote import ErreurPronote
            raise ErreurPronote("Pronote a refusé la connexion : code PIN incorrect.")
        FauxPronote.etat_ = {"connecte": True, "nom": "JUTEAU Anto", "classe": "BTS SIO 2"}

    def deconnexion(self):
        FauxPronote.appels.append(("deconnexion",))
        FauxPronote.etat_ = {"connecte": False}


@pytest.fixture
def agenda(client, monkeypatch):
    from datetime import datetime, timedelta
    from claunote import pronote
    FauxPronote.etat_, FauxPronote.appels = {"connecte": False}, []
    monkeypatch.setattr(pronote, "Client", FauxPronote)
    monkeypatch.setattr(pronote, "synchroniser", lambda: {"connecte": True, "evenements": 2, "preparees": [], "erreurs": []})
    bientot = datetime.now(pronote.PARIS).replace(hour=8, minute=0) + timedelta(days=5)
    evs = {"cours:c1": {"id": "cours:c1", "genre": "evaluation", "matiere": "SISR - RESEAUX", "titre": "Évaluation de Sisr",
                        "description": "", "le": bientot, "fait": False, "revision_id": None, "revision_statut": None},
           "devoir:d1": {"id": "devoir:d1", "genre": "evaluation", "matiere": "ANGLAIS", "titre": "Unit 4",
                         "description": "Test <b>unit 4</b>", "le": bientot, "fait": False, "revision_id": None,
                         "revision_statut": None},
           "devoir:d2": {"id": "devoir:d2", "genre": "devoir", "matiere": "CEJM", "titre": "Lire",
                         "description": "Lire le dossier 4", "le": bientot, "fait": True, "revision_id": None}}
    monkeypatch.setattr(db, "evenements_a_venir", lambda genre, jours: [e for e in evs.values() if e["genre"] == genre])
    monkeypatch.setattr(db, "evenement", lambda i: evs.get(i))
    monkeypatch.setattr(db, "notes_recentes", lambda limite=20: [
        {"le": bientot, "matiere": "MATHEMATIQUES", "commentaire": "DS suites", "coefficient": 2.0,
         "valeur": {"genre": 0, "points": 15.5}, "sur": {"genre": 0, "points": 20},
         "moyenne_classe": {"genre": 0, "points": 11}}])
    monkeypatch.setattr(db, "etat_valeur", lambda k: "2026-10-23T14:00" if k == "pronote_synchro" else None)
    monkeypatch.setattr(db, "etat_ecrire", lambda k, v: client.appels.append(("etat", k, v)))
    monkeypatch.setattr(db, "documents_pronote", lambda limite=30: [
        {"le": bientot, "matiere": "SISR - RESEAUX", "nom": "cours-vlan.pdf", "titre": "Chapitre 3", "categorie": "Cours",
         "statut": "depose", "url": None, "erreur": None, "doc_id": 12, "doc_statut": "classe", "doc_titre": "Les VLAN"},
        {"le": bientot, "matiere": "SISR - RESEAUX", "nom": "Packet Tracer", "titre": "", "categorie": "Cours",
         "statut": "lien", "url": "https://www.netacad.com/pt", "erreur": None, "doc_id": None, "doc_statut": None,
         "doc_titre": None},
        {"le": bientot, "matiere": "CEJM", "nom": "piege", "titre": "", "categorie": "", "statut": "lien",
         "url": "javascript:alert(1)", "erreur": None, "doc_id": None, "doc_statut": None, "doc_titre": None},
        {"le": bientot, "matiere": "CEJM", "nom": "dossier4.pdf", "titre": "Lire", "categorie": "Travail à faire",
         "statut": "depose", "url": None, "erreur": None, "doc_id": None, "doc_statut": None, "doc_titre": None}])
    monkeypatch.setattr(pronote, "preparer", lambda e: 42 if e["matiere"] == "SISR - RESEAUX" else None)
    client.evs = evs
    return client


def test_agenda_connexion_qr(agenda):
    r = agenda.get("/agenda")
    assert r.status_code == 200 and "Connecter Pronote" in r.text and "/static/jsQR.js" in r.text
    r = agenda.post("/agenda/connexion-qr", data={"qr": '{"jeton":"j"}', "pin": "0000"})
    assert "code PIN incorrect" in r.text and "Connecter Pronote" in r.text
    r = agenda.post("/agenda/connexion-qr", data={"qr": ' {"jeton":"j"} ', "pin": "1234"})
    assert ("qr", '{"jeton":"j"}', "1234") in FauxPronote.appels
    assert "Connecté à Pronote : 2 évaluation(s)" in r.text and "JUTEAU Anto" in r.text
    assert ("etat", "pronote_demande", "oui") in agenda.appels and "scan" in agenda.appels


def test_agenda_connecte(agenda):
    FauxPronote.etat_ = {"connecte": True, "nom": "JUTEAU Anto", "classe": "BTS SIO 2"}
    r = agenda.get("/agenda")
    assert "Évaluations à venir" in r.text and "tes cours : Réseaux" in r.text
    assert "pas de cours classés pour cette matière" in r.text  # anglais
    assert "Test &lt;b&gt;unit 4&lt;/b&gt;" in r.text
    assert "15,5/20" in r.text and "coef 2" in r.text and "2026-10-23 à 14:00" in r.text
    assert 'class="carte evenement fait"' in r.text
    assert '<a href="/doc/12">Les VLAN</a>' in r.text and "classé" in r.text
    assert 'href="https://www.netacad.com/pt"' in r.text and "javascript:" not in r.text
    assert "en analyse…" in r.text


def test_agenda_service_absent(agenda, monkeypatch):
    from claunote import pronote

    class Panne(FauxPronote):
        def etat(self):
            raise pronote.ErreurPronote("Le service Pronote ne répond pas (ConnectError).")
    monkeypatch.setattr(pronote, "Client", Panne)
    r = agenda.get("/agenda")
    assert "ne répond pas" in r.text and "docker compose up" in r.text


def test_agenda_preparer(agenda):
    r = agenda.post("/agenda/preparer", data={"ev_id": "cours:c1"}, follow_redirects=False)
    assert r.headers["location"] == "/revision/42"
    # Pas de cours : formulaire de révision prérempli
    r = agenda.post("/agenda/preparer", data={"ev_id": "devoir:d1"}, follow_redirects=False)
    assert r.headers["location"].startswith("/reviser/nouvelle?sujet=Unit+4&genre=plan&date_eval=")
    agenda.evs["cours:c1"]["revision_id"] = 7
    assert agenda.post("/agenda/preparer", data={"ev_id": "cours:c1"}, follow_redirects=False).headers["location"] == "/revision/7"
    assert agenda.post("/agenda/preparer", data={"ev_id": "x"}).status_code == 404


def test_reviser_prerempli(client):
    r = client.get("/reviser", params={"sujet": "VLAN", "matiere": "Réseaux", "genre": "plan", "date_eval": "2030-01-02"})
    assert str(r.url).startswith("http://testserver/reviser/nouvelle?")  # anciens liens : vers le formulaire
    assert 'value="VLAN"' in r.text and "selected" in r.text and 'value="2030-01-02"' in r.text
    assert 'value="plan" checked' in r.text


def test_deconnexion(agenda):
    FauxPronote.etat_ = {"connecte": True, "nom": "X"}
    agenda.post("/agenda/deconnexion")
    assert ("deconnexion",) in FauxPronote.appels


# --- Connexion ----------------------------------------------------------------

def test_pages_protegees(anonyme):
    r = anonyme.get("/revision/5?x=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/connexion?suite=%2Frevision%2F5%3Fx%3D1"
    assert anonyme.post("/scan").status_code == 401
    assert anonyme.post("/scan", headers={"HX-Request": "true"}).headers["HX-Redirect"] == "/connexion"
    assert anonyme.get("/cartes/anki", follow_redirects=False).status_code == 303
    for public in ("/connexion", "/static/logo.svg", "/manifest.webmanifest", "/sw.js", "/hors-ligne"):
        assert anonyme.get(public).status_code == 200, public
    assert "Bibliothèque" not in anonyme.get("/connexion").text  # pas de menu avant connexion


def test_connexion_et_deconnexion(anonyme):
    r = anonyme.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "faux", "suite": "/cartes"})
    assert r.status_code == 401 and "incorrect" in r.text
    r = anonyme.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "claunote", "suite": "/cartes"},
                     follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cartes"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=2592000" in cookie
    assert "Tu utilises le mot de passe par défaut" in anonyme.get("/", follow_redirects=False).text
    anonyme.post("/deconnexion")
    assert anonyme.get("/", follow_redirects=False).status_code == 303


def test_connexion_pas_de_redirection_externe(anonyme):
    for suite in ("https://mechant.example", "//mechant.example", "/\\mechant.example"):
        r = anonyme.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "claunote", "suite": suite},
                         follow_redirects=False)
        assert r.headers["location"] == "/"


def test_identifiants_du_env(monkeypatch, anonyme):
    from claunote import auth
    monkeypatch.setenv("CLAUNOTE_UTILISATEUR", "anto")
    monkeypatch.setenv("CLAUNOTE_MOT_DE_PASSE", auth.hacher("s3cret"))
    with TestClient(main.app) as c:
        assert c.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "claunote"}).status_code == 401
        r = c.post("/connexion", data={"utilisateur": "anto", "mot_de_passe": "s3cret"}, follow_redirects=False)
        assert r.status_code == 303
        assert "Tu utilises le mot de passe par défaut" not in c.get("/").text


def test_blocage_apres_5_echecs(anonyme):
    for _ in range(5):
        anonyme.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "faux"})
    r = anonyme.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "claunote"}, follow_redirects=False)
    assert r.status_code == 401 and "Trop d&#39;essais" in r.text


# --- Flashcards, Anki, PWA ----------------------------------------------------

CARTE = {"id": 31, "revision_id": 8, "recto": "Qu'est-ce qu'un VLAN ?", "verso": "Un réseau virtuel\n<b>isolé</b>",
         "source": "D1", "facilite": 2.5, "intervalle": 0, "repetitions": 0, "prochaine": date(2026, 10, 23),
         "vue_le": None, "sujet": "VLAN", "reperes": {"D1": 1}}


@pytest.fixture
def cartes(client, monkeypatch):
    monkeypatch.setattr(db, "paquets", lambda: [{"id": 8, "sujet": "VLAN", "cree_le": date(2026, 10, 23),
                                                 "total": 1, "a_revoir": 1, "nouvelles": 1}])
    monkeypatch.setattr(db, "prochaine_carte", lambda paquet=None: CARTE if paquet in (None, 8) else None)
    monkeypatch.setattr(db, "cartes", lambda rev_id=None: [CARTE] if rev_id in (None, 8) else [])
    monkeypatch.setattr(db, "carte", lambda i: CARTE if i == 31 else None)
    monkeypatch.setattr(db, "maj_carte", lambda *a: client.appels.append(("carte",) + a))
    return client


def test_page_cartes(cartes):
    r = cartes.get("/cartes")
    assert "1 carte à revoir" in r.text and "/cartes/session?paquet=8" in r.text and "/cartes/anki?paquet=8" in r.text


def test_session_et_notation(cartes):
    r = cartes.get("/cartes/session?paquet=8")
    assert "Qu'est-ce qu'un VLAN ?" in r.text.replace("&#39;", "'")
    assert "Un réseau virtuel<br>&lt;b&gt;isolé&lt;/b&gt;" in r.text and 'href="/doc/1"' in r.text
    assert "maintenant" in r.text and "demain" in r.text
    assert "plus rien à revoir" in cartes.get("/cartes/session?paquet=9").text
    r = cartes.post("/cartes/31/noter", data={"note": "4", "paquet": "8"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cartes/session?paquet=8"
    _, cid, facilite, intervalle, reps, prochaine = cartes.appels[-1]
    assert (cid, intervalle, reps) == (31, 1, 1)
    assert cartes.post("/cartes/31/noter", data={"note": "2"}).status_code == 400
    assert cartes.post("/cartes/99/noter", data={"note": "4"}).status_code == 400


def test_export_anki(cartes):
    import sqlite3
    import tempfile
    import zipfile
    from pathlib import Path
    r = cartes.get("/cartes/anki?paquet=8")
    assert r.status_code == 200 and 'filename="claunote-vlan.apkg"' in r.headers["content-disposition"]
    with tempfile.TemporaryDirectory() as tmp:
        apkg = Path(tmp) / "x.apkg"
        apkg.write_bytes(r.content)
        zipfile.ZipFile(apkg).extract("collection.anki2", tmp)
        champs = sqlite3.connect(Path(tmp) / "collection.anki2").execute("SELECT flds FROM notes").fetchone()[0]
    recto, verso, source = champs.split("\x1f")
    assert recto == "Qu&#x27;est-ce qu&#x27;un VLAN ?" and verso == "Un réseau virtuel<br>&lt;b&gt;isolé&lt;/b&gt;"
    assert source == "Les VLAN"
    assert cartes.get("/cartes/anki?paquet=9").status_code == 404


def test_revision_flashcards(cartes, monkeypatch):
    monkeypatch.setattr(db, "revision", lambda i: {"id": 8, "genre": "cartes", "sujet": "VLAN", "statut": "pret",
                                                   "contenu": "[]", "reperes": {"D1": 1}, "date_eval": None})
    r = cartes.get("/revision/8")
    assert "1 flashcard" in r.text and "/cartes/session?paquet=8" in r.text
    assert 'value="cartes"' in cartes.get("/reviser").text


def test_pwa(anonyme):
    m = anonyme.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    assert m.json()["start_url"] == "/" and len(m.json()["icons"]) == 3
    assert "serviceWorker" in anonyme.get("/connexion").text and 'rel="manifest"' in anonyme.get("/connexion").text
    assert anonyme.get("/static/icone-512.png").status_code == 200


# --- Compte, reclassement, hors cours, actions de masse -------------------------

def test_changer_mot_de_passe(client):
    assert "Change-le maintenant" in client.get("/").text
    r = client.post("/compte", data={"actuel": "faux", "nouveau": "nouveau-mdp", "confirmation": "nouveau-mdp"})
    assert r.status_code == 400 and "actuel incorrect" in r.text
    r = client.post("/compte", data={"actuel": "claunote", "nouveau": "court", "confirmation": "court"})
    assert "8 caractères" in r.text
    r = client.post("/compte", data={"actuel": "claunote", "nouveau": "nouveau-mdp", "confirmation": "autre-mdp"})
    assert "différents" in r.text
    r = client.post("/compte", data={"actuel": "claunote", "nouveau": "nouveau-mdp", "confirmation": "nouveau-mdp"})
    assert r.status_code == 200 and "Mot de passe changé" in r.text
    assert client.etat["mot_de_passe"].startswith("scrypt:")
    # Cet appareil reste connecté, l'ancien mot de passe ne marche plus
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 200 and "Change-le maintenant" not in page.text
    client.post("/deconnexion")
    assert client.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "claunote"}).status_code == 401
    r = client.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "nouveau-mdp"}, follow_redirects=False)
    assert r.status_code == 303


def test_mot_de_passe_du_site_prioritaire(anonyme):
    from claunote import auth
    anonyme.etat["mot_de_passe"] = auth.hacher("choisi-sur-le-site")
    with TestClient(main.app) as c:
        assert c.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "claunote"}).status_code == 401
        assert c.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "choisi-sur-le-site"},
                      follow_redirects=False).status_code == 303


class FauxKD:
    def __init__(self):
        self.moves = []

    def ensure_path(self, parent, parts):
        self.moves.append(("path", parts))
        return 500

    def move(self, fid, dest):
        self.moves.append(("move", fid, dest))


def test_reclasser_depuis_la_page_du_document(client, monkeypatch):
    kd = FauxKD()
    monkeypatch.setattr(main.KDrive, "from_env", classmethod(lambda cls: kd))
    r = client.get("/doc/1")
    assert "Modifier le classement" in r.text and 'name="retour" value="/doc/1"' in r.text
    r = client.post("/doc/1/classer", data={"niveau": "BTS2", "matiere": "Réseaux", "type": "Cours", "retour": "/doc/1"},
                    follow_redirects=False)
    assert r.headers["location"] == "/doc/1"
    r = client.post("/doc/1/hors-cours", data={"retour": "/doc/1"}, follow_redirects=False)
    assert r.headers["location"] == "/doc/1" and ("path", ["hors-cours"]) in kd.moves
    assert client.appels[-1] == ("maj", 1, {"statut": "hors_cours", "note": None, "erreur": None})
    r = client.post("/doc/1/classer", data={"niveau": "BTS2", "matiere": "x", "type": "Cours",
                                            "retour": "https://ailleurs.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_page_hors_cours(client, monkeypatch):
    import datetime as dt
    monkeypatch.setattr(db, "hors_cours", lambda: [{"id": 4, "nom": "bulletin.pdf", "titre": "Bulletin T1",
                                                    "resume": "Notes du 1er trimestre", "date_doc": None,
                                                    "cree_le": dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc),
                                                    "origine": None}])
    r = client.get("/hors-cours")
    assert "Bulletin T1" in r.text and "23/10/2026 14:00" in r.text and "Remettre dans les cours" in r.text
    assert "2 documents" in client.get("/bibliotheque").text


@pytest.fixture
def organiser(client, monkeypatch):
    import datetime as dt
    vu = {}
    doc = {"id": 1, "nom": "a.pdf", "titre": "Les VLAN", "statut": "a_verifier", "niveau": None, "matiere": "Réseaux",
           "type": "TP", "origine": "Scans/lot 3", "cree_le": dt.datetime(2026, 10, 23, 13, 5, tzinfo=dt.timezone.utc),
           "a_ranger": False, "erreur": None}
    monkeypatch.setattr(db, "organiser", lambda filtres, limite=25, decalage=0: vu.update(
        filtres=filtres, limite=limite, decalage=decalage) or ([doc], 60))
    monkeypatch.setattr(db, "ids_filtres", lambda filtres: vu.update(tous=filtres) or [1, 2, 3])
    monkeypatch.setattr(db, "maj_masse", lambda ids, **k: vu.setdefault("masse", []).append((ids, k)) or len(ids))
    monkeypatch.setattr(db, "completer_classement", lambda ids: vu.setdefault("completer", []).append(ids))
    client.vu = vu
    return client


def test_page_organiser_filtre_par_heure_d_ajout(organiser):
    import datetime as dt
    r = organiser.get("/organiser", params={"du": "2026-10-23T14:00", "au": "2026-10-23T16:00", "niveau": "-"})
    assert r.status_code == 200 and "Les VLAN" in r.text and "Scans/lot 3" in r.text and "23/10/2026 15:05" in r.text
    f = organiser.vu["filtres"]
    assert f["du"].isoformat() == "2026-10-23T14:00:00+02:00" and f["au"].hour == 16 and f["niveau"] == "-"
    assert f["du"].utcoffset() == dt.timedelta(hours=2)
    assert organiser.get("/organiser", params={"du": "hier"}).status_code == 400


def test_organiser_pagination(organiser):
    r = organiser.get("/organiser", params={"page": 2, "statut": "classe"})
    assert organiser.vu["limite"] == 25 and organiser.vu["decalage"] == 25
    assert "page 2/3" in r.text and "26–50 sur 60" in r.text and "Sélectionner les 60 résultats" in r.text
    assert "page=1" in r.text and "page=3" in r.text and "statut=classe" in r.text
    organiser.get("/organiser", params={"par": 100})
    assert organiser.vu["limite"] == 100
    organiser.get("/organiser", params={"par": 7})
    assert organiser.vu["limite"] == 25
    organiser.get("/organiser", params={"page": 9})  # au-delà de la fin : dernière page
    assert organiser.vu["decalage"] == 50


def test_action_sur_tous_les_resultats(organiser):
    r = organiser.post("/organiser", data={"action": "niveau", "nouveau_niveau": "BTS2", "tous": "1",
                                           "requete": "du=2026-10-23T14%3A00&niveau=&statut=classe&par=25"},
                       follow_redirects=False)
    assert organiser.vu["masse"][-1] == ([1, 2, 3], {"niveau": "BTS2"})
    assert organiser.vu["tous"]["statut"] == "classe" and organiser.vu["tous"]["du"].hour == 14
    assert "3+documents" in r.headers["location"]


def test_supprimer(organiser, monkeypatch):
    supprimes, kd_suppr = [], []

    class KD:
        def delete(self, fid):
            if fid == 78:
                raise RuntimeError("déjà supprimé")
            kd_suppr.append(fid)

    monkeypatch.setattr(main.KDrive, "from_env", classmethod(lambda cls: KD()))
    monkeypatch.setattr(db, "supprimer", lambda i: supprimes.append(i))
    r = organiser.post("/doc/1/supprimer", data={"retour": "/a-verifier"}, follow_redirects=False)
    assert r.headers["location"] == "/a-verifier" and kd_suppr == [77] and supprimes == [1]
    assert organiser.post("/doc/99/supprimer").status_code == 404
    r = organiser.post("/organiser", data={"ids": ["1", "99"], "action": "supprimer", "requete": "par=25"},
                       follow_redirects=False)
    assert "1+document+supprim" in r.headers["location"] and supprimes == [1, 1]


def test_actions_de_masse(organiser):
    requete = "du=2026-10-23T14%3A00&au=&niveau=&matiere=&type=&statut=&texte="
    r = organiser.post("/organiser", data={"ids": ["1", "2"], "action": "niveau", "nouveau_niveau": "BTS2",
                                           "nouveau_type": "TP", "requete": requete}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/organiser?du=2026-10-23T14%3A00")
    assert "2+documents+modifi" in r.headers["location"]
    assert organiser.vu["masse"][-1] == ([1, 2], {"niveau": "BTS2"}) and organiser.vu["completer"][-1] == [1, 2]
    assert "scan" in organiser.appels
    organiser.post("/organiser", data={"ids": ["1"], "action": "ia"})
    assert organiser.vu["masse"][-1] == ([1], {"statut": "reclasser"})
    organiser.post("/organiser", data={"ids": ["1"], "action": "matiere", "nouvelle_matiere": " Maths "})
    assert organiser.vu["masse"][-1] == ([1], {"matiere": "Maths"})
    organiser.post("/organiser", data={"ids": ["1"], "action": "hors_cours"})
    assert organiser.vu["masse"][-1] == ([1], {"statut": "hors_cours"})
    organiser.post("/organiser", data={"ids": ["1"], "action": "cours"})
    assert organiser.vu["masse"][-1] == ([1], {"statut": "a_verifier"})
    assert organiser.post("/organiser", data={"ids": ["1"], "action": "niveau", "nouveau_niveau": "Master"}).status_code == 400
    assert organiser.post("/organiser", data={"ids": ["1"], "action": "matiere"}).status_code == 400
    assert organiser.post("/organiser", data={"ids": ["1"], "action": "effacer"}).status_code == 400


def test_accueil_aujourd_hui(client, monkeypatch):
    import datetime as dt
    maintenant = dt.datetime.now(dt.timezone.utc)
    evs = {"evaluation": [{"id": "cours:1", "genre": "evaluation", "matiere": "SISR - RESEAUX", "titre": "DS VLAN",
                           "description": "", "le": maintenant + dt.timedelta(days=2), "fait": False,
                           "revision_id": None, "revision_statut": None}],
           "devoir": [{"id": "devoir:1", "genre": "devoir", "matiere": "ANGLAIS", "titre": "", "description": "Oral",
                       "le": maintenant + dt.timedelta(days=1), "fait": False},
                      {"id": "devoir:2", "genre": "devoir", "matiere": "MATHS", "titre": "", "description": "Déjà fait",
                       "le": maintenant + dt.timedelta(days=1), "fait": True}]}
    monkeypatch.setattr(db, "evenements_a_venir", lambda genre=None, jours=60: evs[genre])
    monkeypatch.setattr(db, "nb_cartes_a_revoir", lambda rev_id=None: 4)
    r = client.get("/")
    assert "Relie Pronote" in r.text and "4</strong> flashcards" in r.text and "/cartes/session" in r.text
    client.etat["pronote_synchro"] = "2026-10-23T15:00"
    r = client.get("/")
    assert "DS VLAN" in r.text and "J-2" in r.text and "Réseaux" in r.text and 'value="cours:1"' in r.text
    assert "Oral" in r.text and "Déjà fait" not in r.text and "8 documents" in r.text
    assert "1 document à vérifier" in r.text
    # Navigation mobile : onglets en bas et menu Plus
    assert 'class="onglets verre"' in r.text and 'id="plus"' in r.text and "Scanner l'inbox" in r.text


def test_documents_en_erreur(client, monkeypatch):
    moves = []
    erreur = {**DOC, "id": 3, "statut": "erreur", "titre": None, "nom": "scan_flou.pdf", "niveau": None,
              "matiere": None, "type": None, "erreur": "pas de JSON dans la réponse : 'Je ne peux pas'"}
    monkeypatch.setattr(db, "a_verifier", lambda: [erreur, {**DOC, "id": 2, "statut": "a_verifier", "note": None,
                                                            "origine": None, "confiance": 0.5}])
    r = client.get("/a-verifier")
    assert "En erreur" in r.text and "scan_flou.pdf" in r.text and "réussi à lire le document" in r.text
    assert "Classer à la main" in r.text and "/doc/3/supprimer" in r.text and "/doc/3/hors-cours" in r.text
    assert "À confirmer" in r.text and "Tout réessayer" not in r.text  # un seul en erreur

    class KD:
        def move(self, fid, dest):
            moves.append((fid, dest))

    monkeypatch.setattr(main.KDrive, "from_env", classmethod(lambda cls: KD()))
    monkeypatch.setattr(db, "document", lambda i: {**DOC, "statut": "erreur"} if i == 3 else DOC)
    client.post("/doc/3/reessayer")
    assert moves == [] and client.appels[-2] == ("maj", 3, {"statut": "nouveau", "erreur": None})
    client.post("/doc/1/reessayer")  # déjà rangé : remis dans l'inbox
    assert moves == [(77, main.INBOX_ID)]
    monkeypatch.setattr(db, "reessayer_erreurs", lambda: 2)
    assert client.post("/a-verifier/reessayer-tout", follow_redirects=False).status_code == 303


def test_explication_erreur():
    assert "30 minutes" in main.explication_erreur("Claude Code trop long (30 min)")[0]
    assert "abîmé" in main.explication_erreur("fichier .docx illisible : File is not a zip file")[0]
    assert "limite" in main.explication_erreur("usage limit reached")[0]
    assert main.explication_erreur(None)[0] == "L'analyse a échoué."


def test_installer_et_certificat_sans_connexion(anonyme, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUNOTE_HOTE", "10.8.0.5")
    monkeypatch.setattr(main, "CA", tmp_path / "root.crt")
    r = anonyme.get("/installer")
    assert r.status_code == 200 and "https://10.8.0.5/" in r.text and "pas encore créé" in r.text
    assert anonyme.get("/certificat.crt").status_code == 404
    der = b"\x30\x82certificat"
    (tmp_path / "root.crt").write_text("-----BEGIN CERTIFICATE-----\n"
                                       + base64.b64encode(der).decode() + "\n-----END CERTIFICATE-----\n")
    r = anonyme.get("/certificat.crt")
    assert r.content == der and r.headers["content-type"] == "application/x-x509-ca-cert"
    assert "Télécharger le certificat" in anonyme.get("/installer").text
    assert anonyme.get("/notifications", follow_redirects=False).status_code == 303  # le reste demande la connexion


def test_page_notifications(client, monkeypatch):
    abos = []
    monkeypatch.setattr(db, "abonnements", lambda: list(abos))
    monkeypatch.setattr(db, "enregistrer_abonnement", lambda *a: abos.append(dict(zip(
        ("endpoint", "p256dh", "auth", "appareil"), a), cree_le=None)))
    monkeypatch.setattr(db, "retirer_abonnement", lambda e: abos.clear())
    monkeypatch.setattr(db, "notifications_recentes", lambda limite=10: [])
    monkeypatch.setattr(db, "etat_initialiser", lambda k, v: client.etat.setdefault(k, v))
    r = client.get("/notifications")
    assert r.status_code == 200 and "Rappel du jour" in r.text and "applicationServerKey" in r.text
    assert client.post("/notifications/test").text.startswith("Aucun appareil")
    assert client.post("/notifications/abonnement", json={"endpoint": "e"}).status_code == 400
    client.post("/notifications/abonnement", json={"endpoint": "https://web.push.apple.com/x",
                                                   "keys": {"p256dh": "p", "auth": "a"}, "appareil": "iPhone (appli)"})
    assert abos[0]["appareil"] == "iPhone (appli)" and "iPhone (appli)" in client.get("/notifications").text
    monkeypatch.setattr(main.notifications, "envoyer_a_tous", lambda *a: (1, []))
    assert client.post("/notifications/test").text == "Envoyée sur 1 appareil."
    r = client.post("/notifications/reglages", data={"heure": "19:00", "silence_debut": "22:30", "silence_fin": "07:00",
                                                     "pronote": "true"}, follow_redirects=False)
    assert r.status_code == 303
    p = main.notifications.preferences()
    assert p["heure"] == "19:00" and p["pronote"] is True and p["rappel"] is False and p["revisions"] is False
    assert client.post("/notifications/reglages", data={"heure": "25h"}).status_code == 400
    client.post("/notifications/desabonnement", json={"endpoint": "https://web.push.apple.com/x"})
    assert abos == []


REV = {"id": 5, "genre": "examen", "sujet": "Suites", "date_eval": None, "erreur": None, "reperes": {"D1": 1},
       "statut": "pret", "contenu": "# Épreuve\n## Exercice 1\nCalculer [D1]\n\n## Corrigé\nRéponse",
       "matiere": "Maths", "niveau": "BTS2", "campagne_id": None, "campagne_titre": None, "consigne": "Sans calculatrice",
       "fait_le": None, "score": None, "cree_le": None}


def test_epreuve_blanche_et_pdf(client, monkeypatch):
    monkeypatch.setattr(db, "revision", lambda i: REV if i == 5 else None)
    r = client.get("/revision/5")
    assert "Voir le corrigé" in r.text and r.text.index("Exercice 1") < r.text.index("Réponse")
    assert "Sans calculatrice" in r.text and "data-partager" in r.text and "/revision/5/pdf?corrige=0" in r.text
    r = client.get("/revision/5/pdf")
    assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF-")
    assert "inline" in r.headers["content-disposition"]
    sujet = client.get("/revision/5/pdf?corrige=0&telecharger=1")
    assert "attachment" in sujet.headers["content-disposition"] and "sujet" in sujet.headers["content-disposition"]
    assert len(sujet.content) < len(r.content)  # sans la page du corrigé
    monkeypatch.setattr(db, "revision", lambda i: {**REV, "statut": "en_attente"})
    assert client.get("/revision/5/pdf").status_code == 404


def test_sources_web_affichees(client, monkeypatch):
    web = [{"repere": "W1", "titre": "Légifrance", "url": "https://www.legifrance.gouv.fr/a?b=1&c=2"}]
    monkeypatch.setattr(db, "revision", lambda i: {**REV, "genre": "fiche", "contenu": "Le contrat [D1] [W1] [W9]",
                                                   "sources_web": web})
    r = client.get("/revision/5")
    assert 'href="https://www.legifrance.gouv.fr/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">[W1]' in r.text
    assert "[W9]" in r.text and "Complété avec le web · 1" in r.text and "www.legifrance.gouv.fr · web" in r.text
    assert client.get("/revision/5/pdf").content.startswith(b"%PDF-")


def test_quiz_pdf_et_score(client, monkeypatch):
    quiz = {**REV, "genre": "quiz", "contenu": '[{"question": "Q?", "choix": ["a", "b"], "reponse": 1, '
                                                '"explication": "car", "source": "D1"}]'}
    monkeypatch.setattr(db, "revision", lambda i: quiz)
    vu = []
    monkeypatch.setattr(db, "marquer_fait", lambda i, fait=True, score=None: vu.append((i, fait, score)))
    assert client.get("/revision/5/pdf").content.startswith(b"%PDF-")
    r = client.post("/revision/5/fait", data={"bonnes": "1", "total": "1"}, follow_redirects=False)
    assert r.status_code == 303 and vu == [(5, True, {"bonnes": 1, "total": 1})]
    client.post("/revision/5/fait", data={"fait": "0"}, follow_redirects=False)
    assert vu[-1] == (5, False, None)


def test_supprimer_et_relancer_revision(client, monkeypatch):
    vu = []
    monkeypatch.setattr(db, "revision", lambda i: {**REV, "campagne_id": 3})
    monkeypatch.setattr(db, "supprimer_revision", lambda i: vu.append(("suppr", i)))
    monkeypatch.setattr(db, "relancer_revision", lambda i: vu.append(("relance", i)))
    assert client.post("/revision/5/supprimer", follow_redirects=False).headers["location"] == "/campagne/3"
    assert client.post("/revision/5/relancer", follow_redirects=False).headers["location"] == "/revision/5"
    assert vu == [("suppr", 5), ("relance", 5)] and "scan" in client.appels


def test_historique_filtre(client, monkeypatch):
    vu = {}
    lignes = [{"id": 5, "genre": "quiz", "sujet": "VLAN", "statut": "pret", "date_eval": None,
               "cree_le": date(2026, 10, 20), "niveau": "BTS1", "matiere": "Réseaux", "fait_le": None,
               "score": {"bonnes": 7, "total": 10}, "campagne_id": 2, "campagne_titre": "BTS blanc"}]
    monkeypatch.setattr(db, "historique", lambda *a: vu.update(args=a) or (lignes, 45))
    import datetime as dt
    monkeypatch.setattr(main.templates.env, "filters", {**main.templates.env.filters,
                                                        "paris": lambda d: d.strftime("%d/%m/%Y 00:00")})
    r = client.get("/reviser", params={"filtre": "quiz", "m": "Réseaux", "q": "vl", "page": 2})
    assert vu["args"] == ("quiz", "Réseaux", "vl", 20, 20)
    assert "7/10" in r.text and "BTS blanc" in r.text and "Page 2 / 3" in r.text and "20/10/2026" in r.text
    # Filtre en direct : seulement la liste
    r = client.get("/reviser", params={"filtre": "inconnu"}, headers={"HX-Request": "true", "HX-Target": "historique"})
    assert vu["args"][0] == "" and r.text.lstrip().startswith('<div id="historique">') and "<h1>" not in r.text


def test_nouvelle_revision_avec_contexte_et_cours(client):
    r = client.post("/reviser", data={"genre": "examen", "niveau_matiere": "BTS1|Réseaux", "sujet": "VLAN",
                                      "consigne": "  2 h  ", "doc_ids": ["1", "x"]}, follow_redirects=False)
    assert r.status_code == 303 and ("rev", "examen", "VLAN", [1], None) in client.appels
    r = client.get("/reviser/documents", params={"niveau_matiere": "BTS1|Réseaux", "nom": "docs:BTS1|Réseaux"})
    assert 'name="docs:BTS1|Réseaux" value="1"' in r.text and "Les VLAN" in r.text
    assert "Les VLAN" in client.get("/reviser/nouvelle?doc=1").text


def test_creer_campagne(client, monkeypatch):
    monkeypatch.setattr(db, "creer_campagne", lambda *a: client.appels.append(("campagne",) + a) or 9)
    r = client.get("/campagne/nouvelle")
    assert 'value="BTS1|Réseaux"' in r.text and 'name="genres" value="examen" checked' in r.text
    donnees = {"titre": "BTS blanc", "consigne": "Maths lundi", "debut": "2026-11-02", "fin": "2026-11-06",
               "matieres": ["BTS1|Réseaux"], "genres": ["examen", "fiche", "plan"], "sujet:BTS1|Réseaux": "vlan"}
    r = client.post("/campagne", data=donnees, follow_redirects=False)
    assert r.headers["location"] == "/campagne/9"
    assert ("campagne", "BTS blanc", "Maths lundi", date(2026, 11, 2), date(2026, 11, 6)) in client.appels
    revs = [a for a in client.appels if a[0] == "rev"]
    assert revs == [("rev", "examen", "Réseaux : vlan", [1], date(2026, 11, 6), {"consigne": "Maths lundi", "campagne_id": 9}),
                    ("rev", "fiche", "Réseaux : vlan", [1], None, {"consigne": "Maths lundi", "campagne_id": 9})]
    assert client.post("/campagne", data={**donnees, "fin": "2026-10-01"}).status_code == 400
    assert client.post("/campagne", data={**donnees, "genres": ["plan"]}).status_code == 400


def test_page_campagne(client, monkeypatch):
    import datetime as dt
    jour = dt.date.today()
    campagne = {"id": 9, "titre": "BTS blanc", "consigne": "Maths lundi", "debut": jour, "fin": jour + dt.timedelta(days=2),
                "statut": "pret", "erreur": None, "faits": [f"{jour}:0"],
                "programme": {"conseil": "Courage !", "jours": [{"date": jour.isoformat(), "titre": "Réseaux", "taches": [
                    {"matiere": "Réseaux", "quoi": "Faire l'épreuve", "duree": 120, "support": "examen"},
                    {"matiere": "Réseaux", "quoi": "Relire la fiche", "duree": 20, "support": "fiche"}]}]}}
    revs = [{"id": 5, "genre": "examen", "sujet": "Réseaux", "statut": "pret", "niveau": "BTS1", "matiere": "Réseaux",
             "fait_le": None, "score": None, "erreur": None, "doc_ids": [1]},
            {"id": 6, "genre": "fiche", "sujet": "Réseaux", "statut": "en_attente", "niveau": "BTS1", "matiere": "Réseaux",
             "fait_le": None, "score": None, "erreur": None, "doc_ids": [1]}]
    monkeypatch.setattr(db, "campagne", lambda i: campagne if i == 9 else None)
    monkeypatch.setattr(db, "revisions_campagne", lambda i: revs)
    r = client.get("/campagne/9")
    assert "Courage !" in r.text and "1</strong>/2 tâches" in r.text and 'id="aujourdhui"' in r.text
    assert 'href="/revision/5"' in r.text and "Génération en cours · 1/2" in r.text and 'http-equiv="refresh"' in r.text
    assert "/campagne/9/pdf" in r.text
    vu = []
    monkeypatch.setattr(db, "basculer_tache", lambda i, cle: vu.append(cle) or True)
    r = client.post("/campagne/9/tache", data={"cle": f"{jour}:1"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and vu == [f"{jour}:1"] and '"fait": true' in r.headers["HX-Trigger"]
    assert client.post("/campagne/9/tache", data={"cle": "x"}).status_code == 400
    monkeypatch.setattr(db, "revision", lambda i: {**REV, "id": i, "matiere": "Réseaux"} if i == 5 else None)
    pdf = client.get("/campagne/9/pdf")
    assert pdf.content.startswith(b"%PDF-") and "BTS-blanc.pdf" in pdf.headers["content-disposition"]
    monkeypatch.setattr(db, "supprimer_campagne", lambda i, avec_revisions=True: vu.append(("suppr", avec_revisions)))
    client.post("/campagne/9/supprimer", data={"garder": "true"})
    assert vu[-1] == ("suppr", False)
    assert client.get("/campagne/1").status_code == 404


def test_bandeau_et_page_etat(client, monkeypatch):
    import datetime as dt
    import json
    maintenant = dt.datetime.now(dt.timezone.utc)
    reprise = (maintenant + dt.timedelta(hours=2)).replace(second=0, microsecond=0)
    client.etat["service"] = json.dumps({
        "vu": maintenant.isoformat(), "demarre": maintenant.isoformat(), "activite": "Analyse de « a.pdf »",
        "claude": {"etat": "limite", "message": "5-hour limit reached", "jusqu_a": reprise.isoformat(),
                   "depuis": maintenant.isoformat()},
        "kdrive": {"etat": "ok"}, "modeles": {}})
    monkeypatch.setattr(db, "file_attente", lambda: {"scans": 12, "revisions": 1, "campagnes": 0, "reclassements": 0})
    monkeypatch.setattr(db, "bilan_ia", lambda h: [{"modele": "sonnet", "appels": 4, "escalades": 1, "minutes": 9,
                                                    "jetons_sortie": 0}])
    monkeypatch.setattr(db, "appels_ia", lambda n: [])
    heure = reprise.astimezone(main.pronote.PARIS)
    page = client.get("/bibliotheque").text
    assert "Limite de ton forfait Claude atteinte" in page and f"{heure:%H} h {heure:%M}" in page
    page = client.get("/etat").text
    assert "Limite d'usage atteinte" in page and "12</strong> fichiers à analyser" in page
    assert "Épreuve blanche" in page and "Opus" in page and "Limite de ton forfait" not in page

    # Worker muet depuis 10 min : on le dit, avec la commande pour le relancer
    client.etat["service"] = json.dumps({"vu": (maintenant - dt.timedelta(minutes=10)).isoformat(),
                                         "claude": {"etat": "ok"}, "kdrive": {"etat": "ok"}})
    assert "Le worker ne répond plus" in client.get("/bibliotheque").text
    client.etat["service"] = json.dumps({"vu": maintenant.isoformat(), "claude": {"etat": "ok"},
                                         "kdrive": {"etat": "ok"}})
    assert "bandeau" not in client.get("/bibliotheque").text.split("<main>")[1]


def test_kdrive_en_panne_page_claire(client, monkeypatch):
    from claunote.kdrive import KDriveIndisponible

    class KD:
        def download(self, *a):
            raise KDriveIndisponible("kDrive répond 503")
    monkeypatch.setattr(main.KDrive, "from_env", classmethod(lambda cls: KD()))
    r = client.get("/doc/1/fichier")
    assert r.status_code == 503 and "kDrive ne répond pas" in r.text


def test_powerpoint_jamais_dans_la_page(client, monkeypatch, tmp_path):
    pptx = {**DOC, "nom": "2025-03-12_vlan.pptx", "variante_kdrive_id": None, "variante_nom": None}
    monkeypatch.setattr(db, "document", lambda i: pptx)
    page = client.get("/doc/1").text
    assert "<iframe" not in page and "Télécharger le PPTX" in page
    # Avec son PDF à côté : l'aperçu montre le PDF
    monkeypatch.setattr(db, "document", lambda i: {**pptx, "variante_kdrive_id": 8, "variante_nom": "2025-03-12_vlan.pdf"})
    page = client.get("/doc/1").text
    assert 'src="/doc/1/fichier?variante=1"' in page and "télécharger le PPTX" in page

    class KD:
        def download(self, fid, dest):
            dest.write_bytes(b"PK")
            return dest
    monkeypatch.setattr(main.KDrive, "from_env", classmethod(lambda cls: KD()))
    r = client.get("/doc/1/fichier")
    assert r.headers["content-disposition"].startswith("attachment")  # téléchargé, pas ouvert dans la page
    r = client.get("/doc/1/fichier?variante=1")
    assert r.headers["content-disposition"].startswith("inline") and r.headers["content-type"] == "application/pdf"
