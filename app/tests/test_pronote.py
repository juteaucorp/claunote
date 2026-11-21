import datetime as dt
import json

import httpx
import pytest

from claunote import db, pronote

MATIERES = ["Anglais", "CEJM", "Développement", "Mathématiques", "Réseaux", "Systèmes"]


@pytest.mark.parametrize("nom, attendu", [
    ("CULTURE ECONOMIQUE, JURIDIQUE ET MANAGERIALE", "CEJM"),
    ("MATHEMATIQUES POUR L'INFORMATIQUE", "Mathématiques"),
    ("SISR - RESEAUX", "Réseaux"),
    ("ANGLAIS LV1", "Anglais"),
    ("Mathématiques", "Mathématiques"),
    ("CONCEPTION ET DEVELOPPEMENT D'APPLICATIONS", "Développement"),
    ("EDUCATION PHYSIQUE ET SPORTIVE", None),
])
def test_trouver_matiere(nom, attendu):
    assert pronote.trouver_matiere(nom, MATIERES) == attendu


def test_trouver_matiere_sans_cours():
    assert pronote.trouver_matiere("MATHEMATIQUES", []) is None


def test_texte_et_annonce():
    t = pronote.texte("<p>Réviser&nbsp;les <b>suites</b></p><p>pour le DS</p>")
    assert t == "Réviser les suites\npour le DS"
    assert pronote.annonce_evaluation("Contrôle sur les VLAN")
    assert pronote.annonce_evaluation("préparer l'interrogation")
    assert pronote.annonce_evaluation(t)
    assert not pronote.annonce_evaluation("Écrire les tests unitaires du TP")
    assert not pronote.annonce_evaluation("Lire le chapitre 3")


def test_niveau_actuel():
    nm = [{"niveau": "Terminale", "matiere": "Mathématiques"}, {"niveau": "BTS1", "matiere": "Mathématiques"},
          {"niveau": "BTS1", "matiere": "CEJM"}]
    from claunote.agent import NIVEAUX
    assert pronote.niveau_actuel(nm, "Mathématiques", NIVEAUX) == "BTS1"
    assert pronote.niveau_actuel(nm, "Réseaux", NIVEAUX) is None


def test_libelle_note():
    assert pronote.libelle_note({"genre": 0, "points": 15.5}, {"genre": 0, "points": 20}) == "15,5/20"
    assert pronote.libelle_note({"genre": 0, "points": 12}) == "12"
    assert pronote.libelle_note({"genre": 1, "points": 0}) == "Abs"
    assert pronote.libelle_note(None) == "–"


SYNCHRO = {
    "du": "2026-10-22T22:00:00.000Z", "au": "2026-11-19T23:00:00.000Z", "erreurs": [],
    "controles": [{"id": "c1", "matiere": "SISR - RESEAUX", "debut": "2026-10-28T08:00:00.000Z",
                   "fin": "2026-10-28T10:00:00.000Z", "profs": [], "salles": [], "notes": ""}],
    "devoirs": [
        {"id": "d1", "matiere": "MATHEMATIQUES", "description": "<p>DS sur les suites</p>",
         "pour_le": "2026-10-27T07:00:00.000Z", "fait": False, "themes": ["Suites"]},
        {"id": "d2", "matiere": "CEJM", "description": "<p>Lire le dossier 4</p>",
         "pour_le": "2026-10-26T07:00:00.000Z", "fait": True, "themes": []}],
    "notes": {"periode": "Semestre 1", "moyenne": None, "notes": [
        {"id": "g1", "matiere": "MATHEMATIQUES", "date": "2026-10-10T00:00:00.000Z",
         "valeur": {"genre": 0, "points": 15}, "sur": {"genre": 0, "points": 20},
         "moyenne_classe": None, "coefficient": 1, "commentaire": "DS"}]},
}


def test_evenements():
    evs = {e["id"].split(":")[0] + ":" + e["matiere"]: e for e in pronote.evenements(SYNCHRO)}
    assert evs["cours:SISR - RESEAUX"]["genre"] == "evaluation"
    assert evs["cours:SISR - RESEAUX"]["le"].hour == 9  # 8h UTC = 9h à Paris en hiver
    assert evs["devoir:MATHEMATIQUES"]["genre"] == "evaluation" and evs["devoir:MATHEMATIQUES"]["titre"] == "Suites"
    assert evs["devoir:CEJM"]["genre"] == "devoir" and evs["devoir:CEJM"]["titre"] == "Lire le dossier 4"
    assert evs["devoir:CEJM"]["fait"] is True


def test_ids_stables_entre_deux_connexions():
    """Pronote change ses ids à chaque connexion : les nôtres viennent du contenu, ils ne bougent pas."""
    autre = {**SYNCHRO, "controles": [{**c, "id": c["id"] + "-session2"} for c in SYNCHRO["controles"]],
             "devoirs": [{**d, "id": d["id"] + "-session2"} for d in SYNCHRO["devoirs"]]}
    assert [e["id"] for e in pronote.evenements(SYNCHRO)] == [e["id"] for e in pronote.evenements(autre)]


def faux_service(reponses: dict):
    appels = []

    def gerer(req: httpx.Request):
        corps = json.loads(req.content) if req.content else None
        appels.append((req.method, req.url.path, corps))
        statut, donnees = reponses[(req.method, req.url.path)]
        return httpx.Response(statut, json=donnees)
    client = pronote.Client("http://pronote", transport=httpx.MockTransport(gerer))
    client.appels = appels
    return client


def test_client_erreur_du_service():
    c = faux_service({("POST", "/connexion/qr"): (502, {"erreur": "Pronote a refusé la connexion"})})
    with pytest.raises(pronote.ErreurPronote, match="refusé"):
        c.connexion_qr("{}", "1234")
    assert c.appels == [("POST", "/connexion/qr", {"qr": "{}", "pin": "1234"})]


def test_client_service_injoignable():
    def panne(req):
        raise httpx.ConnectError("refused")
    c = pronote.Client("http://pronote", transport=httpx.MockTransport(panne))
    with pytest.raises(pronote.ErreurPronote, match="ne répond pas"):
        c.etat()


@pytest.fixture
def fausse_base(monkeypatch):
    base = {"enregistre": None, "etat": {}, "revisions": [], "liens": []}
    monkeypatch.setattr(db, "enregistrer_pronote",
                        lambda evs, debut, sources, notes, periode: base.update(enregistre=(evs, debut, sources, notes, periode)))
    monkeypatch.setattr(db, "etat_ecrire", lambda k, v: base["etat"].update({k: v}))
    monkeypatch.setattr(db, "etat_valeur", lambda k: base["etat"].get(k))
    monkeypatch.setattr(db, "notification_connue", lambda cle: False)
    monkeypatch.setattr(db, "notifier", lambda *a, **k: True)
    monkeypatch.setattr(db, "abonnements", lambda: [])
    monkeypatch.setattr(db, "niveaux_matieres", lambda: [{"niveau": "BTS1", "matiere": "Réseaux"},
                                                          {"niveau": "BTS2", "matiere": "Réseaux"},
                                                          {"niveau": "BTS2", "matiere": "Mathématiques"}])
    monkeypatch.setattr(db, "choisir_documents", lambda n, m, q="": [7, 8] if (n, m) == ("BTS2", "Réseaux") and not q
                        else [3] if (n, m, q) == ("BTS2", "Mathématiques", "Suites") else [])
    monkeypatch.setattr(db, "creer_revision",
                        lambda *a: base["revisions"].append(a) or len(base["revisions"]))
    monkeypatch.setattr(db, "lier_revision", lambda ev, rev: base["liens"].append((ev, rev)))
    monkeypatch.setattr(db, "demander_scan", lambda: None)
    return base


def test_synchroniser(fausse_base, monkeypatch):
    monkeypatch.setattr(pronote, "PREPARATION_AUTO", False)
    c = faux_service({("GET", "/etat"): (200, {"connecte": True}), ("POST", "/synchro"): (200, SYNCHRO)})
    r = pronote.synchroniser(c)
    assert r == {"connecte": True, "evenements": 3, "preparees": [], "erreurs": []}
    evs, debut, sources, notes, periode = fausse_base["enregistre"]
    assert sources == ["cours:", "devoir:"] and periode == "Semestre 1" and notes[0]["id"] == "g1"
    assert debut.isoformat() == "2026-10-23T00:00:00+02:00"
    assert ("POST", "/synchro", {"jours": 28}) in c.appels


def test_synchroniser_section_en_erreur(fausse_base, monkeypatch):
    monkeypatch.setattr(pronote, "PREPARATION_AUTO", False)
    donnees = {**SYNCHRO, "controles": None, "notes": None, "erreurs": ["emploi du temps : boum"]}
    c = faux_service({("GET", "/etat"): (200, {"connecte": True}), ("POST", "/synchro"): (200, donnees)})
    pronote.synchroniser(c)
    _, _, sources, notes, _ = fausse_base["enregistre"]
    assert sources == ["devoir:"] and notes is None  # les contrôles déjà connus ne sont pas effacés
    assert fausse_base["etat"]["pronote_erreurs"] == "emploi du temps : boum"


def test_synchroniser_pas_connecte(fausse_base):
    c = faux_service({("GET", "/etat"): (200, {"connecte": False})})
    assert pronote.synchroniser(c) == {"connecte": False}
    assert fausse_base["enregistre"] is None


def ev(id_, matiere, titre, jour):
    return {"id": id_, "matiere": matiere, "titre": titre, "revision_id": None,
            "le": dt.datetime(2026, 10, jour, 8, tzinfo=pronote.PARIS)}


def test_preparer_toujours_une_fiche(fausse_base):
    aujourd_hui = dt.date(2026, 10, 23)
    # Contrôle de l'emploi du temps dans 5 jours : fiche sur toute la matière, niveau le plus récent
    assert pronote.preparer(ev("cours:c1", "SISR - RESEAUX", "Évaluation", 28), aujourd_hui) == 1
    assert fausse_base["revisions"][0] == ("fiche", "Réseaux : évaluation du 28/10", [7, 8], dt.date(2026, 10, 28))
    # Devoir annonçant un DS dans 2 jours : fiche ciblée sur le thème
    assert pronote.preparer(ev("devoir:d1", "MATHEMATIQUES", "Suites", 25), aujourd_hui) == 2
    assert fausse_base["revisions"][1][:3] == ("fiche", "Mathématiques : Suites", [3])
    assert fausse_base["liens"] == [("cours:c1", 1), ("devoir:d1", 2)]
    # Pas de cours pour l'anglais
    assert pronote.preparer(ev("cours:c9", "ANGLAIS", "Évaluation", 28), aujourd_hui) is None


def test_preparer_a_venir_une_seule_par_jour(fausse_base, monkeypatch):
    evs = [ev("cours:c1", "SISR - RESEAUX", "Évaluation", 28), ev("devoir:d5", "SISR - RESEAUX", "VLAN", 28),
           {**ev("cours:c2", "MATHEMATIQUES", "Évaluation", 27), "revision_id": 9}]
    monkeypatch.setattr(db, "evenements_a_venir", lambda genre, jours: evs)
    assert pronote.preparer_a_venir(dt.date(2026, 10, 23)) == [1]
    assert len(fausse_base["revisions"]) == 1


class FauxKD:
    def __init__(self):
        self.faits = []

    def ensure_path(self, parent, parts):
        self.faits.append(("path", parent, parts))
        return 500 + len(self.faits)

    def upload(self, dossier, nom, contenu, conflict="version"):
        self.faits.append(("upload", dossier, nom, contenu, conflict))
        return {"id": 9000 + len(self.faits)}


def test_importer_documents(monkeypatch):
    import base64
    enregistres, appels = [], []
    monkeypatch.setattr(db, "cles_documents_pronote", lambda: ["deja"])
    monkeypatch.setattr(db, "enregistrer_document_pronote",
                        lambda d, statut, kdrive_id=None, erreur=None: enregistres.append((d["cle"], statut, kdrive_id, erreur)))
    monkeypatch.setattr(db, "demander_scan", lambda: appels.append("scan"))
    b64 = lambda t: base64.b64encode(t).decode()
    lots = [
        {"documents": [
            {"cle": "r:1", "source": "ressource", "matiere": "SISR - RESEAUX", "nom": "cours/vlan.pdf", "contenu": b64(b"%PDF1")},
            {"cle": "r:2", "source": "ressource", "matiere": "SISR - RESEAUX", "nom": "tp.docx", "contenu": b64(b"PK")},
            {"cle": "r:3", "source": "ressource", "matiere": "SISR - RESEAUX", "nom": "PT", "url": "https://netacad.com"},
            {"cle": "d:4", "source": "devoir", "matiere": "CEJM", "nom": "gros.pdf", "erreur": "fichier trop gros (30 Mo)"},
        ], "restant": 1, "erreurs": []},
        {"documents": [{"cle": "d:5", "source": "devoir", "matiere": "CEJM", "nom": "dossier.pdf", "contenu": b64(b"%PDF2")}],
         "restant": 0, "erreurs": []},
    ]
    c = faux_service({("GET", "/etat"): (200, {"connecte": True, "classe": "BTS SIO 2"})})
    c.documents = lambda deja: (appels.append(("deja", deja)), lots.pop(0))[1]
    kd = FauxKD()
    b = pronote.importer_documents(kd, 10, c)
    assert b == {"deposes": 3, "liens": 1, "erreurs": ["gros.pdf : fichier trop gros (30 Mo)"], "restant": 0}
    assert ("path", 10, ["BTS SIO 2", "Pronote", "SISR - RESEAUX"]) in kd.faits
    assert ("path", 10, ["BTS SIO 2", "Pronote", "CEJM"]) in kd.faits
    assert len([f for f in kd.faits if f[0] == "path"]) == 2
    assert ("upload", 502, "cours-vlan.pdf", b"%PDF1", "rename") in kd.faits[:2] or any(
        f[0] == "upload" and f[2] == "cours-vlan.pdf" and f[3] == b"%PDF1" and f[4] == "rename" for f in kd.faits)
    statuts = {cle: statut for cle, statut, _, _ in enregistres}
    assert statuts == {"r:1": "depose", "r:2": "depose", "r:3": "lien", "d:4": "erreur", "d:5": "depose"}
    assert appels.count("scan") == 1 and appels[0] == ("deja", ["deja"])


def test_importer_documents_pas_connecte():
    c = faux_service({("GET", "/etat"): (200, {"connecte": False})})
    assert pronote.importer_documents(FauxKD(), 10, c) is None


def test_niveau_du_dossier_de_classe():
    from claunote import rangement
    assert rangement.niveau_depuis_dossier(["BTS SIO 2", "Pronote", "SISR - RESEAUX"]) == "BTS2"


def test_libelle_note_format_inattendu():
    from claunote.pronote import libelle_note
    assert libelle_note({"genre": 0, "points": 15.5}, {"genre": 0, "points": 20}) == "15,5/20"
    assert libelle_note({"kind": 0}) == "?" and libelle_note("12") == "–" and libelle_note(None) == "–"


def test_verifier_emploi_du_temps_et_notes_seulement(fausse_base):
    verif = {"du": SYNCHRO["du"], "au": "2026-10-30T23:00:00.000Z", "erreurs": [], "emploi_du_temps": [],
             "cours": [{"debut": "2026-10-28T08:00:00.000Z", "fin": "2026-10-28T10:00:00.000Z", "matiere": "M",
                        "salle": "B12", "prof": "", "statut": "", "annule": False, "dispense": False}],
             "notes": SYNCHRO["notes"]}
    c = faux_service({("GET", "/etat"): (200, {"connecte": True}), ("POST", "/verification"): (200, verif)})
    r = pronote.verifier(c)
    assert r["connecte"] and r["erreurs"] == []
    evs, _, sources, notes, _ = fausse_base["enregistre"]
    assert evs == [] and sources == [] and notes[0]["id"] == "g1"   # contrôles et devoirs pas touchés
    assert "pronote_cours" in fausse_base["etat"] and "pronote_notes_suivies" in fausse_base["etat"]
    assert ("POST", "/verification", {"jours": 8}) in c.appels
