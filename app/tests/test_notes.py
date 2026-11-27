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
        m(db, "notes_publiques_du_document", lambda d: [dict(n) for n in self.notes.values() if n["document_id"] == d
                                                        and n["public"] and n["compte_id"] != self.compte])
        m(db, "notes_a_lier", lambda niv, mat, d: [n for n in self.liste() if n["matiere"] == mat and not n["document_id"]])
        m(db, "lier_note", self.lier)
        m(db, "notes_lisibles", lambda ids: [dict(self.notes[i]) for i in ids if i in self.notes
                                             and (self._a_moi(i) or self.notes[i]["public"])])
        m(db, "notes_pour_revision", lambda docs, matieres, sujet=None, choisies=(), exclues=():
          [dict(n) for n in self.notes.values() if (n["document_id"] in docs or n["id"] in choisies)
           and n["id"] not in exclues and (n["compte_id"] == self.compte or n["public"])])
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

    def lier(self, i, document_id, niveau=None, matiere=None):
        if not self._a_moi(i):
            return False
        n = self.notes[i]
        n["document_id"] = document_id
        if n["matiere"] is None:
            n["niveau"], n["matiere"] = niveau, matiere
        return True

    def changer(self, i, **champs):
        if not self._a_moi(i):
            return False
        self.notes[i].update(champs)
        return True

    def liste(self, q="", niveau=None, matiere=None, libres=False, archivees=False, publiques_des_autres=False):
        if publiques_des_autres:
            return [dict(n) for n in self.notes.values() if n["public"] and not n["archive_le"]
                    and n["compte_id"] != self.compte and (not q or q in n["titre"] + n["contenu"])]
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
    assert "Notes sur ce cours" in page and "/notes/nouvelle?doc=1" in page
    r = client.get("/notes/nouvelle?doc=1")
    assert 'value="BTS1|Réseaux" selected' in r.text and '<option value="1" selected>' in r.text
    client.post("/notes", data={"titre": "Oral du prof", "contenu": "STP bloque les boucles", "document_id": "1"},
                headers=AUTO)
    page = client.get("/doc/1").text
    assert "Oral du prof" in page and 'name="note_ids" value="1"' in page and "avec ta note sur ce cours" in page
    # « Fiche » depuis le cours : la note cochée, gardée pour la rédaction (plus de copie en pièce jointe)
    vu = {}
    main.db.creer_revision, ancien = (lambda *a, **k: vu.update(k) or 5), main.db.creer_revision
    try:
        client.post("/reviser", data={"doc_id": "1", "genre": "fiche", "note_ids": ["1", "1", "99"],
                                      "notes_vues": ["1", "4"]})
    finally:
        main.db.creer_revision = ancien
    assert vu["note_ids"] == [1, 99] and vu["notes_exclues"] == [4]
    assert not [a for a in client.appels if a[0] == "pieces"]
    # Formulaire complet : la note d'où l'on vient, cochée
    r = client.get("/reviser/nouvelle?note=1&matiere=Réseaux")
    assert 'name="note_ids" value="1" checked' in r.text and "/notes/pour-reviser" in r.text
    assert 'name="notes_vues" value="1"' in r.text
    r = client.get("/notes/pour-reviser?niveau_matiere=BTS1|Réseaux&deja=1")
    assert "Oral du prof" not in r.text   # déjà proposée plus haut
    carnet.notes[1]["matiere"] = "Réseaux"
    r = client.get("/notes/pour-reviser?niveau_matiere=BTS1|Réseaux").text
    assert "Oral du prof" in r and 'name="note_ids" value="1" checked' in r   # cochée d'office : lue par l'IA


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
    assert "Notes sur ce cours" not in page.text


def test_invite_avec_le_droit_et_partage(client, carnet, faux_comptes):
    i = _invite(client, faux_comptes, ["bibliotheque", "notes"])
    r = client.post("/notes", data={"titre": "À moi", "contenu": "x"}, headers=AUTO)
    assert r.status_code == 200 and carnet.notes[1]["compte_id"] == i
    page = client.get("/notes/1").text
    assert "Qui peut voir cette note" in page and "Comme la matière" not in page   # deux comptes : partage possible
    assert client.post("/notes/1/visibilite", data={"valeur": "public"}, follow_redirects=False).status_code == 303
    assert carnet.notes[1]["public"] is True
    assert client.post("/notes/1/visibilite", data={"valeur": "matiere"}).status_code == 400
    # Téléphone : Notes à la place d'Agenda en bas ; PC : toutes les rubriques en haut
    page = client.get("/notes").text
    onglets = page[page.index('<nav class="onglets'):page.index('<details id="plus"')]
    assert 'id="maj-o-notes" class="actif"' in onglets and 'href="/agenda"' not in onglets
    haut = page[page.index('<nav class="haut"'):page.index('</nav>', page.index('<nav class="haut"'))]
    assert 'id="maj-h-notes"' in haut


def test_choix_d_onglets_d_avant_les_notes(client):
    from claunote import apparence
    import json
    client.etat["apparence:1"] = json.dumps({"mode": "auto", "style": "claunote", "vigie": False,
                                             "onglets": ["cours", "agenda", "reviser"], "accueil": ""})
    assert apparence.lire({"id": 1})["onglets"] == ["cours", "notes", "reviser"]   # Notes prend la place d'Agenda
    client.post("/reglages/navigation", data={"onglet_cours": "1", "ordre_cours": "1", "onglet_agenda": "1", "ordre_agenda": "2"})
    assert apparence.lire({"id": 1})["onglets"] == ["cours", "agenda"]            # remis par le compte : gardé


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


def test_cours_facultatif_replie_et_seulement_avec_une_matiere(client, carnet):
    # Sans matière : aucune liste de cours, juste un champ vide (la note reste libre)
    r = client.get("/notes/nouvelle")
    assert 'name="document_id" value=""' in r.text and "Lier un cours" not in r.text
    # Avec une matière : le choix du cours existe mais reste replié (facultatif)
    r = client.get("/notes/nouvelle?matiere=BTS1|Réseaux")
    assert '<details class="lier-cours">' in r.text and "(facultatif)" in r.text
    # Ouverte depuis un cours : le cours est affiché et sélectionné
    r = client.get("/notes/nouvelle?doc=1")
    assert 'title="Cours rattaché"' in r.text and "Lier un cours" not in r.text
    r = client.get("/notes/cours?matiere=BTS1|Maths")
    assert "Lier un cours" not in r.text and 'name="document_id" value=""' in r.text


def test_notes_publiques_des_autres_dans_l_onglet_notes(client, carnet):
    for titre in ("Oral STP", "Brouillon"):
        carnet.creer(titre, "le prof a dit : STP" if titre == "Oral STP" else "privé")
    for n in carnet.notes.values():
        n["compte_id"], n["auteur"] = 2, "camille"            # écrites par un autre compte
    carnet.notes[1]["public"] = True
    page = client.get("/notes").text
    assert "Notes publiques des autres" in page and 'href="/partages/note/1"' in page
    assert "Brouillon" not in page                                    # privée : jamais
    assert "Oral STP" in client.get("/notes?q=STP").text              # la recherche les trouve aussi
    assert "Notes publiques des autres" not in client.get("/notes?vue=archives").text


def test_lier_une_note_au_cours_depuis_sa_page(client, carnet):
    client.post("/notes", data={"titre": "Questions STP", "contenu": "pourquoi le root bridge ?",
                                "matiere": "BTS1|Réseaux"}, headers=AUTO)
    page = client.get("/doc/1").text
    assert "Lier une de tes notes de Réseaux" in page and 'action="/notes/1/lier"' in page
    r = client.post("/notes/1/lier", data={"document_id": "1", "suite": "/doc/1#notes-du-cours"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/doc/1#notes-du-cours"
    assert carnet.notes[1]["document_id"] == 1
    page = client.get("/doc/1").text
    assert "Questions STP" in page and "Délier" in page and "Lier une de tes notes" not in page
    assert "📎 " in client.get("/notes").text                       # la liste des notes montre le cours lié
    assert client.post("/notes/1/lier", data={"document_id": "42"}).status_code == 404   # cours illisible
    client.post("/notes/1/lier", data={"document_id": ""})          # « Délier »
    assert carnet.notes[1]["document_id"] is None and carnet.notes[1]["matiere"] == "Réseaux"
    carnet.notes[1]["compte_id"] = 2                                  # la note d'un autre : jamais liée par moi
    assert client.post("/notes/1/lier", data={"document_id": "1"}).status_code in (403, 404)


def test_note_publique_d_un_autre_sur_le_cours(client, carnet):
    client.post("/notes", data={"titre": "Oral de Camille", "contenu": "STP : 802.1D", "document_id": "1"}, headers=AUTO)
    carnet.notes[1].update(compte_id=2, public=True, auteur="Camille")
    page = client.get("/doc/1").text
    assert "Notes publiques des autres" in page and "/partages/note/1" in page and "Camille" in page
    carnet.notes[1]["public"] = False                                 # privée : invisible
    assert "Oral de Camille" not in client.get("/doc/1").text


def test_page_revision_cite_les_notes(client, carnet, monkeypatch):
    client.post("/notes", data={"titre": "Oral du prof", "contenu": "STP", "document_id": "1"}, headers=AUTO)
    rev = {"id": 5, "genre": "quiz", "sujet": "STP", "statut": "pret", "reperes": {"D1": 1, "D2": -1},
           "contenu": '[{"question": "q", "choix": ["a", "b"], "reponse": 0, "explication": "e", "source": "D2"}]',
           "sources_web": [], "campagne_id": None, "date_eval": None, "cree_le": MAINTENANT, "matiere": "Réseaux",
           "niveau": "BTS1", "consigne": "", "retouches": [], "public": False, "compte_id": 1}
    monkeypatch.setattr(db, "revision", lambda i: dict(rev) if i == 5 else None)
    monkeypatch.setattr(db, "noter_ouverture", lambda i: None)
    page = client.get("/revision/5").text
    assert '<a href="/notes/1">[D2]</a>' in page                     # le repère de la note mène à la note
    assert "📝 Oral du prof" in page and "et tes notes" in page
    assert main._sources({"D1": 1, "D2": -1})[-1] == "Note : Oral du prof"


def test_sujet_precis():
    assert notes.sujet_precis("Réseaux", ["Réseaux"]) is None
    assert notes.sujet_precis("Réseaux (BTS1, BTS2)", ["Réseaux"]) is None
    assert notes.sujet_precis("Réseaux : VLAN et trunk", ["Réseaux"]) == "VLAN et trunk"
    assert notes.sujet_precis("Le protocole STP", ["Réseaux"]) == "Le protocole STP"
    assert notes.sujet_precis("", ["Réseaux"]) is None


def test_comme_source_et_consigne_des_notes():
    from claunote import revision
    mienne = {"id": 3, "compte_id": 1, "titre": "Oral", "contenu": " STP bloque les boucles ", "niveau": "BTS1",
              "matiere": "Réseaux", "auteur": "admin"}
    autre = {**mienne, "id": 4, "compte_id": 2, "auteur": "Camille"}
    s, a = notes.comme_source(mienne, 1), notes.comme_source(autre, 1)
    assert s["id"] == -3 and s["titre"] == "Ta note : Oral" and s["transcription"] == "STP bloque les boucles"
    assert a["titre"] == "Note de Camille : Oral"
    blocs, reperes = revision.blocs_cours([{"id": 1, "nom": "stp.pdf", "titre": "STP", "transcription": "cours"}, s])
    assert reperes == {"D1": 1, "D2": -3} and blocs[1].startswith("[D2] Ta note : Oral (BTS1, Réseaux, note personnelle)")
    assert "Ta note" in revision.consigne_notes([s]) and revision.consigne_notes([{"id": 1}]) == ""


def test_worker_lit_les_notes_de_la_revision(monkeypatch):
    from claunote import worker
    vu = {}

    def pour_revision(docs, matieres, sujet=None, choisies=(), exclues=()):
        vu.update(docs=docs, matieres=matieres, sujet=sujet, choisies=choisies, exclues=exclues)
        return [{"id": 7, "compte_id": 1, "titre": "Oral", "contenu": "trunk", "niveau": "BTS2", "matiere": "Réseaux"}]
    monkeypatch.setattr(db, "notes_pour_revision", pour_revision)
    monkeypatch.setattr(comptes, "trouver", lambda i: {"id": i, "role": "invite", "droits": ["notes"] if i == 1 else []})
    docs = [{"id": 10, "niveau": "BTS2", "matiere": "Réseaux"}, {"id": 11, "niveau": "BTS1", "matiere": "Réseaux"}]
    rev = {"id": 5, "compte_id": 1, "sujet": "Réseaux : VLAN", "note_ids": [3], "notes_exclues": [4]}
    lues = worker.notes_de_revision(rev, docs)
    assert [n["id"] for n in lues] == [-7] and lues[0]["titre"] == "Ta note : Oral"
    assert vu == {"docs": [10, 11], "matieres": [("BTS2", "Réseaux"), ("BTS1", "Réseaux")], "sujet": "VLAN",
                  "choisies": [3], "exclues": [4]}
    worker.notes_de_revision({**rev, "sujet": "Réseaux (BTS1, BTS2)"}, docs)
    assert vu["sujet"] is None                                       # matière entière : toutes ses notes
    assert worker.notes_de_revision({**rev, "compte_id": 2}, docs) == []   # sans le droit « notes »
    monkeypatch.setattr(db, "notes_pour_revision", lambda *a, **k: 1 / 0)
    assert worker.notes_de_revision(rev, docs) == []                 # une erreur ne bloque jamais la fiche
