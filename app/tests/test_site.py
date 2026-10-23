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
    monkeypatch.setattr(db, "nb_cartes_a_revoir", lambda rev_id=None: 0)
    monkeypatch.setattr(db, "bibliotheque", lambda: [{"niveau": "BTS1", "matiere": "Réseaux", "type": "TP", "n": 3}])
    monkeypatch.setattr(db, "a_verifier", lambda: [{**DOC, "id": 2, "statut": "a_verifier", "confiance": 0.4,
                                                    "note": "Le dossier « BTS1 » indique BTS1", "origine": "BTS1"}])
    monkeypatch.setattr(db, "nb_a_verifier", lambda: 1)
    monkeypatch.setattr(db, "nb_evaluations_proches", lambda: 0)
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


@pytest.fixture
def client(anonyme):
    r = anonyme.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "claunote"}, follow_redirects=False)
    assert r.status_code == 303
    return anonyme


def test_bibliotheque(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "BTS1" in r.text and "TP · 3" in r.text and "1 document à confirmer" in r.text and '<span class="badge">1</span>' in r.text


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
                                            "type": "Cours", "confiance": 1.0, "note": None})
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
                ("fichiers", ("notes.txt", b"x", "text/plain")),
                ("fichiers", ("../../photo.JPG", b"img", "image/jpeg"))]
    r = client.post("/deposer", files=fichiers, data={"niveau": ""})
    assert r.status_code == 200 and "3 fichiers envoyés" in r.text and "notes.txt" in r.text
    assert ("path", 10, ["Terminale", "Maths"]) in faits and ("path", 10, []) in faits
    assert [f for f in faits if f[0] == "path"].count(("path", 10, ["Terminale", "Maths"])) == 1
    assert ("upload", 902, "suites.pdf", b"%PDF-1", "rename") in faits
    assert ("upload", 900, "photo.JPG", b"img", "rename") in faits
    assert "scan" in client.appels


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
    assert r.headers["location"].startswith("/reviser?sujet=Unit+4&genre=plan&date_eval=")
    agenda.evs["cours:c1"]["revision_id"] = 7
    assert agenda.post("/agenda/preparer", data={"ev_id": "cours:c1"}, follow_redirects=False).headers["location"] == "/revision/7"
    assert agenda.post("/agenda/preparer", data={"ev_id": "x"}).status_code == 404


def test_reviser_prerempli(client):
    r = client.get("/reviser", params={"sujet": "VLAN", "matiere": "Réseaux", "genre": "plan", "date_eval": "2030-01-02"})
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
    assert "Mot de passe par défaut" in anonyme.get("/", follow_redirects=False).text
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
        assert "Mot de passe par défaut" not in c.get("/").text


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
