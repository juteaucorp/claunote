"""Prise de notes : pages, enregistrement automatique, droit « notes », intégration aux cours, à la recherche,
au chat et aux fiches. La séparation par compte elle-même est testée sur une vraie base : test_base_notes.py."""
import datetime as dt

import pytest

from claunote import auth, comptes, db, main, notes
from tests.test_site import DOC, anonyme, client  # noqa: F401  (fixtures du site)

MAINTENANT = dt.datetime(2026, 11, 26, 10, 0, tzinfo=dt.timezone.utc)


class FaussesNotes:
    """Notes en mémoire, avec les mêmes règles que la base : seul l'auteur modifie, conflit si la version change."""

    def __init__(self):
        self.notes: dict[int, dict] = {}
        self.auteur = None            # hors requête : compte qui écrit (sinon celui de la requête, comme la base)
        self._actuel = db.compte_actuel

    @property
    def compte(self):
        return self.auteur or self._actuel() or 1

    def installer(self, monkeypatch):
        m = monkeypatch.setattr
        m(db, "creer_note", self.creer)
        m(db, "note", lambda i: dict(self.notes[i]) if i in self.notes and self._lisible(self.notes[i]) else None)
        m(db, "maj_note", self.maj)
        m(db, "changer_note", self.changer)
        m(db, "supprimer_note", lambda i: bool(self._a_moi(i) and self.notes.pop(i)))
        m(db, "notes", self.liste)
        m(db, "matieres_notes", lambda: [])
        m(db, "nb_notes", lambda: {"actives": sum(1 for n in self.notes.values() if not n["archive_le"]),
                                   "libres": 0, "archivees": sum(1 for n in self.notes.values() if n["archive_le"])})
        m(db, "notes_du_document", lambda d: [n for n in self.liste() if n["document_id"] == d])
        m(db, "notes_matiere", lambda niv, mat: [n for n in self.liste() if n["matiere"] == mat])
        m(db, "notes_choisies", lambda ids: [dict(self.notes[i]) for i in ids if self._a_moi(i)])
        m(db, "notes_pour_question", lambda q, niv, mat, limite=3: [n for n in self.liste() if "trunk" in n["contenu"]])
        m(db, "chercher_notes", lambda q, limite=10: [{**n, "extrait": "le <b>trunk</b>"} for n in self.liste()
                                                      if q in n["contenu"]])
        m(db, "cours_lisible", lambda i: DOC if i == 1 else None)
        m(db, "cours_pour_note", lambda niv, mat: [{**DOC, "compte_id": 1}] if mat == "Réseaux" else [])
        m(db, "note_partagee", lambda i: None)
        m(db, "partages_notes", lambda: [])
        m(db, "changer_visibilite", lambda t, i, v: t == "notes" and self._a_moi(i)
                                     and self.notes[i].update(public=v) is None)

    def _lisible(self, n):
        return n["compte_id"] == self.compte or self.tout_voir

    tout_voir = False

    def _a_moi(self, i):
        return i in self.notes and self.notes[i]["compte_id"] == self.compte

    def creer(self, titre="", contenu="", niveau=None, matiere=None, document_id=None):
        i = max(self.notes, default=0) + 1
        self.notes[i] = {"id": i, "compte_id": self.compte, "titre": titre, "contenu": contenu, "niveau": niveau,
                         "matiere": matiere, "document_id": document_id, "epinglee": False, "public": False,
                         "archive_le": None, "cree_le": MAINTENANT, "maj_le": MAINTENANT, "auteur": "admin",
                         "debut": contenu[:600]}
        return {"id": i, "maj_le": MAINTENANT}

    def maj(self, i, base=None, **champs):
        if not self._a_moi(i):
            return "absente", None
        n = self.notes[i]
        if base is not None and base != n["maj_le"]:
            return "conflit", dict(n)
        n.update(champs, maj_le=n["maj_le"] + dt.timedelta(seconds=1))
        n["debut"] = n["contenu"][:600]
        return "ok", n["maj_le"]

    def changer(self, i, **champs):
        if not self._a_moi(i):
            return False
        self.notes[i].update(champs)
        return True

    def liste(self, q="", niveau=None, matiere=None, libres=False, archivees=False):
        r = [dict(n) for n in self.notes.values() if self._lisible(n) and bool(n["archive_le"]) == archivees
             and (not q or q in n["titre"] + n["contenu"]) and (not matiere or n["matiere"] == matiere)
             and (not libres or n["matiere"] is None)]
        return sorted(r, key=lambda n: (not n["epinglee"], -n["maj_le"].timestamp()))


@pytest.fixture
def carnet(client, monkeypatch):
    f = FaussesNotes()
    f.installer(monkeypatch)
    return f


AUTO = {"X-Claunote-Auto": "1"}


def test_page_vide_puis_premiere_note_enregistree_pendant_la_frappe(client, carnet):
    r = client.get("/notes")
    assert r.status_code == 200 and "Aucune note pour l" in r.text and 'href="/notes/nouvelle"' in r.text
    r = client.get("/notes/nouvelle?matiere=BTS1|Réseaux")
    assert r.status_code == 200 and 'value="BTS1|Réseaux" selected' in r.text and 'id="editeur"' in r.text
    assert "Les VLAN" in r.text   # les cours de la matière, pour y rattacher la note
    # Première frappe : la note est créée, l'éditeur passe sur son adresse
    r = client.post("/notes", data={"titre": "", "contenu": "# Trunk\n- 802.1Q", "matiere": "BTS1|Réseaux",
                                    "document_id": "1"}, headers=AUTO)
    assert r.status_code == 200 and r.json()["url"] == "/notes/1"
    n = carnet.notes[1]
    assert (n["niveau"], n["matiere"], n["document_id"], n["compte_id"]) == ("BTS1", "Réseaux", 1, 1)
    # Frappes suivantes : seulement le texte, avec la version lue
    r = client.post("/notes/1", data={"contenu": "# Trunk\n- 802.1Q\n- [ ] revoir STP", "base": MAINTENANT.isoformat()},
                    headers=AUTO)
    assert r.status_code == 200 and "maj_le" in r.json()
    assert carnet.notes[1]["contenu"].endswith("revoir STP") and carnet.notes[1]["matiere"] == "Réseaux"
    page = client.get("/notes").text
    assert "Trunk" in page and "Réseaux" in page and "📎 cours" in page
    assert "Ouvrir le cours" in client.get("/notes/1").text


def test_sans_javascript_le_bouton_enregistre_aussi(client, carnet):
    r = client.post("/notes", data={"titre": "Idées", "contenu": "x", "matiere": ""}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/notes/1" and carnet.notes[1]["matiere"] is None
    r = client.post("/notes/1", data={"titre": "Idées 2"}, follow_redirects=False)
    assert r.status_code == 303 and carnet.notes[1]["titre"] == "Idées 2"


def test_autre_matiere(client, carnet):
    client.post("/notes", data={"contenu": "x", "matiere": "__autre__", "niveau_autre": "BTS2",
                                "matiere_autre": " Cybersécurité "}, headers=AUTO)
    assert (carnet.notes[1]["niveau"], carnet.notes[1]["matiere"]) == ("BTS2", "Cybersécurité")


def test_conflit_rien_n_est_ecrase(client, carnet):
    client.post("/notes", data={"contenu": "v1"}, headers=AUTO)
    client.post("/notes/1", data={"contenu": "v2 (téléphone)", "base": MAINTENANT.isoformat()}, headers=AUTO)
    r = client.post("/notes/1", data={"contenu": "v2 (PC)", "base": MAINTENANT.isoformat()}, headers=AUTO)
    assert r.status_code == 409 and r.json()["contenu"] == "v2 (téléphone)"
    assert carnet.notes[1]["contenu"] == "v2 (téléphone)"
    r = client.post("/notes/1", data={"contenu": "v2 (PC)", "base": MAINTENANT.isoformat(), "forcer": "1"}, headers=AUTO)
    assert r.status_code == 200 and carnet.notes[1]["contenu"] == "v2 (PC)"   # « Garder ma version »
    assert "Cette note a été modifiée ailleurs" in client.get("/notes/1").text


def test_epingler_archiver_restaurer_supprimer_telecharger(client, carnet):
    client.post("/notes", data={"titre": "Suites", "contenu": "raison q", "matiere": "BTS1|Maths"}, headers=AUTO)
    client.post("/notes/1/epingler")
    assert carnet.notes[1]["epinglee"] and "📌 Épinglées" in client.get("/notes").text
    r = client.get("/notes/1/telecharger")
    assert r.status_code == 200 and r.text.startswith("# Suites") and "Maths" in r.text
    assert "Suites.md" in r.headers["content-disposition"]
    client.post("/notes/1/archiver")
    assert carnet.notes[1]["archive_le"] and not carnet.notes[1]["epinglee"]
    assert "Suites" not in client.get("/notes").text and "Suites" in client.get("/notes?vue=archives").text
    assert "Restaurer" in client.get("/notes/1").text
    client.post("/notes/1/restaurer")
    assert carnet.notes[1]["archive_le"] is None
    assert client.post("/notes/1/inconnue").status_code == 404
    r = client.post("/notes/1/supprimer", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/notes" and not carnet.notes


def test_recherche_pendant_la_frappe(client, carnet):
    client.post("/notes", data={"titre": "VLAN", "contenu": "trunk"}, headers=AUTO)
    client.post("/notes", data={"titre": "Suites", "contenu": "raison"}, headers=AUTO)
    r = client.get("/notes?q=trunk", headers={"HX-Request": "true", "HX-Target": "resultats-notes"})
    assert "VLAN" in r.text and "Suites" not in r.text and "<html" not in r.text   # la liste seulement
    assert "Rien trouvé pour « <strong>zzz</strong> »" in client.get("/notes?q=zzz").text


def test_apercu_markdown_cases_et_html_neutralise(client, carnet):
    r = client.post("/notes/apercu", data={"contenu": "- [ ] STP\n- [x] VLAN\n\n<script>alert(1)</script>"})
    assert 'type="checkbox" disabled>' in r.text and 'disabled checked>' in r.text
    assert "<script>" not in r.text
    assert "Rien à afficher" in client.post("/notes/apercu", data={"contenu": " "}).text


def test_note_d_un_autre_compte_en_lecture_seule(client, carnet):
    carnet.auteur = 2
    carnet.creer("Note de Camille", "secret")
    carnet.auteur = None
    carnet.notes[7] = {**carnet.notes.pop(1), "id": 7, "compte_id": 2, "auteur": "camille"}
    carnet.tout_voir = True   # l'admin avec « Tout voir »
    r = client.get("/notes/7")
    assert r.status_code == 200 and "Note de <strong>camille</strong>" in r.text and 'id="editeur"' not in r.text
    assert client.post("/notes/7", data={"contenu": "pirate"}).status_code == 403
    assert client.post("/notes/7/archiver").status_code == 403
    assert carnet.notes[7]["contenu"] == "secret"
    carnet.tout_voir = False
    assert client.get("/notes/7").status_code == 404


def test_page_du_cours_et_reviser_avec_ses_notes(client, carnet):
    page = client.get("/doc/1").text
    assert "Mes notes sur ce cours" in page and "/notes/nouvelle?doc=1" in page
    r = client.get("/notes/nouvelle?doc=1")
    assert 'value="BTS1|Réseaux" selected' in r.text and '<option value="1" selected>' in r.text
    client.post("/notes", data={"titre": "Oral du prof", "contenu": "STP bloque les boucles", "document_id": "1"},
                headers=AUTO)
    page = client.get("/doc/1").text
    assert "Oral du prof" in page and 'name="note_ids" value="1"' in page and "avec ta note sur ce cours" in page
    # « Fiche » depuis le cours : la note jointe au support, comme le contexte donné par l'élève
    client.post("/reviser", data={"doc_id": "1", "genre": "fiche", "note_ids": ["1", "1", "99"]})
    assert ("pieces", ["Ma note - Oral du prof.md"], 5, None) in client.appels
    # Formulaire complet : la note d'où l'on vient, cochée
    r = client.get("/reviser/nouvelle?note=1&matiere=Réseaux")
    assert 'name="note_ids" value="1" checked' in r.text and "/notes/pour-reviser" in r.text
    r = client.get("/notes/pour-reviser?niveau_matiere=BTS1|Réseaux&deja=1")
    assert "Oral du prof" not in r.text   # déjà proposée plus haut
    carnet.notes[1]["matiere"] = "Réseaux"
    assert "Oral du prof" in client.get("/notes/pour-reviser?niveau_matiere=BTS1|Réseaux").text


def test_recherche_du_site_et_chat(client, carnet, monkeypatch):
    client.post("/notes", data={"titre": "Trunk", "contenu": "le trunk transporte les VLAN"}, headers=AUTO)
    r = client.get("/recherche?q=trunk")
    assert "Dans tes notes" in r.text and 'href="/notes/1"' in r.text and "<b>trunk</b>" in r.text
    # Chat : la note en tête des passages, son repère mène à la note
    monkeypatch.setattr(db, "chercher_passages", lambda *a, **k: [])
    monkeypatch.setattr(db, "cours_pour_question", lambda *a, **k: [])
    monkeypatch.setattr(main.index, "vecteur_question", lambda q: None)
    jeton = main._moi.set({"id": 1, "role": "admin"})
    try:
        passages = main._passages("trunk ?", {"niveau": None, "matiere": None})
    finally:
        main._moi.reset(jeton)
    assert passages[0]["document_id"] == -1 and passages[0]["titre"] == "Ta note : Trunk"
    rendu = main.avec_sources("<p>Voir [D1] et [D2]</p>", {"D1": -1, "D2": 1})
    assert '<a href="/notes/1" class="repere-note">[D1]</a>' in rendu and '<a href="/doc/1">[D2]</a>' in rendu


def test_agenda_prend_des_notes_dans_la_bonne_matiere(client, carnet, monkeypatch):
    monkeypatch.setattr(db, "niveaux_matieres", lambda: [{"niveau": "BTS1", "matiere": "Réseaux"},
                                                         {"niveau": "BTS2", "matiere": "Réseaux"}])
    r = client.get("/notes/nouvelle?pronote=RESEAUX&jour=2026-11-26")
    assert 'value="BTS2|Réseaux" selected' in r.text and "du 26/11" in r.text


def _invite(client, faux_comptes, droits):
    i = faux_comptes.creer_compte("camille", auth.hacher("un-mot-de-passe-solide"), "invite", droits)
    faux_comptes.comptes[i]["conditions_version"] = comptes.CONDITIONS_VERSION
    client.post("/deconnexion")
    client.cookies.clear()
    r = client.post("/connexion", data={"utilisateur": "camille", "mot_de_passe": "un-mot-de-passe-solide"},
                    follow_redirects=False)
    assert r.status_code == 303
    return i


def test_droit_notes(client, carnet, faux_comptes):
    assert "notes" in comptes.DROITS and "notes" in comptes.DROITS_INVITE and "notes" in comptes.DROITS_CASES
    assert "Prendre ses notes par matière" in client.get("/comptes").text   # case dans Comptes et invitations
    _invite(client, faux_comptes, ["bibliotheque"])
    assert client.get("/notes").status_code == 403
    assert client.get("/partages/note/3").status_code == 403
    assert client.post("/notes", data={"contenu": "x"}).status_code == 403
    page = client.get("/doc/1")
    assert "Mes notes sur ce cours" not in page.text


def test_invite_avec_le_droit_et_partage(client, carnet, faux_comptes):
    i = _invite(client, faux_comptes, ["bibliotheque", "notes"])
    r = client.post("/notes", data={"titre": "À moi", "contenu": "x"}, headers=AUTO)
    assert r.status_code == 200 and carnet.notes[1]["compte_id"] == i
    page = client.get("/notes/1").text
    assert "Qui peut voir cette note" in page and "Comme la matière" not in page   # deux comptes : partage possible
    assert client.post("/notes/1/visibilite", data={"valeur": "public"}, follow_redirects=False).status_code == 303
    assert carnet.notes[1]["public"] is True
    assert client.post("/notes/1/visibilite", data={"valeur": "matiere"}).status_code == 400
    # Les onglets d'origine ne bougent pas : Notes est dans Plus
    page = client.get("/notes").text
    onglets = page[page.index('<nav class="onglets'):page.index('<details id="plus"')]
    assert 'href="/notes"' not in onglets and 'href="/notes"' in page[page.index('<div class="feuille verre">'):]


def test_voir_en_tant_que_lecture_seule(client, carnet, faux_comptes):
    i = faux_comptes.creer_compte("camille", "h", "invite", ["bibliotheque", "notes"])
    carnet.auteur = i
    carnet.creer("Note de Camille", "secret")
    carnet.auteur = None
    client.post(f"/comptes/{i}/apercu", follow_redirects=False)
    r = client.get("/notes/1")
    assert r.status_code == 200 and "Note de Camille" in r.text and 'id="editeur"' not in r.text
    assert client.post("/notes/1", data={"contenu": "pirate"}).status_code == 403
    assert carnet.notes[1]["contenu"] == "secret"


def test_migration_donne_les_notes_aux_invites_qui_lisent_leurs_cours(client, faux_comptes):
    a = faux_comptes.creer_compte("camille", "h", "invite", ["bibliotheque"])
    b = faux_comptes.creer_compte("dominique", "h", "invite", ["actualites"])
    client.etat.pop("droit_notes", None)
    comptes.migrer_notes()
    assert "notes" in faux_comptes.comptes[a]["droits"] and "notes" not in faux_comptes.comptes[b]["droits"]
    faux_comptes.comptes[a]["droits"].remove("notes")
    comptes.migrer_notes()   # une seule fois : l'admin peut ensuite décocher
    assert "notes" not in faux_comptes.comptes[a]["droits"]


def test_outils_du_module():
    assert notes.matiere_choisie("BTS2|Réseaux") == ("BTS2", "Réseaux")
    assert notes.matiere_choisie("|") == (None, None) and notes.matiere_choisie("") == (None, None)
    assert notes.titre_auto("", "\n## Le trunk 802.1Q\nsuite") == "Le trunk 802.1Q"
    assert notes.titre_auto("", "- [ ] revoir STP") == "revoir STP"
    assert notes.titre_auto("  ", "") == notes.SANS_TITRE
    assert notes.requete_prefixe("vla rout") == "vla:* & rout:*"
    assert notes.requete_prefixe("a ' ;") is None
    assert notes.extrait("# Titre\n\n**gras** et `code`\n- [x] fait") == "Titre gras et code fait"
    assert notes.nom_fichier({"titre": "Réseaux : VLAN/trunk ?"}) == "Reseaux  VLANtrunk.md"
    p = notes.comme_passage({"id": 4, "titre": "T", "contenu": "texte", "niveau": "BTS1", "matiere": "Maths"})
    assert p["document_id"] == -4 and p["texte"] == "texte"
