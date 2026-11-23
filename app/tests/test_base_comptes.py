"""Séparation des données par compte, sur une vraie base PostgreSQL (row-level security).

Lancé seulement si CLAUNOTE_TEST_DB donne une base JETABLE : son schéma public est effacé.
    CLAUNOTE_TEST_DB=postgresql://claunote:test@localhost/claunote_test python -m pytest tests/test_base_comptes.py
(en CI : service postgres du workflow Tests).
"""
import os

import pytest

from claunote import db

URL = os.environ.get("CLAUNOTE_TEST_DB")
pytestmark = pytest.mark.skipif(not URL, reason="CLAUNOTE_TEST_DB non défini (base PostgreSQL jetable)")


@pytest.fixture
def base(monkeypatch):
    monkeypatch.undo()   # les vraies fonctions de db, pas les faux comptes en mémoire
    monkeypatch.setenv("DATABASE_URL", URL)
    db.fermer()
    db._pool = None
    db._etat_connexion.clear()
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.init()
    db.init()   # rejouable : le site et le worker l'exécutent à chaque démarrage
    yield
    db.fermer()
    db._pool = None


def _documents():
    with db.connect() as c:
        return sorted(r["nom"] for r in c.execute("SELECT nom FROM documents").fetchall())


def test_chaque_compte_ne_voit_que_ses_donnees(base):
    invite = db.creer_compte("camille", "h", "invite", ["bibliotheque"])
    autre = db.creer_compte("dominique", "h", "invite", ["bibliotheque"])
    d_admin = db.creer(100, "admin.pdf")                    # le worker, sans compte : document de l'admin (1)
    with db.pour_compte(invite):
        d_invite = db.creer(200, "camille.pdf")
        with db.connect() as c:
            c.execute("INSERT INTO passages (document_id, rang, texte) VALUES (%s, 0, 'vlan')", (d_invite,))
            c.execute("INSERT INTO revisions (genre, sujet, doc_ids) VALUES ('fiche', 'VLAN', %s) RETURNING id",
                      ([d_invite],))
        assert _documents() == ["camille.pdf"]
        assert db.document(d_admin) is None                 # même avec l'identifiant exact
        with db.connect() as c:                             # ni lire ni modifier les lignes d'un autre
            assert c.execute("UPDATE documents SET nom = 'x' WHERE id = %s RETURNING id", (d_admin,)).fetchone() is None
            with pytest.raises(Exception):
                c.execute("INSERT INTO documents (kdrive_id, nom, compte_id) VALUES (300, 'pirate.pdf', 1)")
    with db.pour_compte(autre):
        assert _documents() == []
    with db.pour_compte(1):
        assert _documents() == ["admin.pdf"]
    with db.pour_compte(1, tout_voir=True):                 # « Tout voir » de l'admin
        assert _documents() == ["admin.pdf", "camille.pdf"]
    # Les lignes filles suivent leur parent, même écrites par le worker (sans compte)
    with db.connect() as c:
        c.execute("INSERT INTO passages (document_id, rang, texte) VALUES (%s, 1, 'ip')", (d_invite,))
        assert {r["compte_id"] for r in c.execute("SELECT compte_id FROM passages").fetchall()} == {invite}
        assert c.execute("SELECT compte_id FROM revisions").fetchone()["compte_id"] == invite


def test_supprimer_un_compte_supprime_ses_donnees(base):
    invite = db.creer_compte("camille", "h", "invite", [])
    db.creer(100, "admin.pdf")
    with db.pour_compte(invite):
        db.creer(200, "camille.pdf")
    db.supprimer_compte(invite)
    assert _documents() == ["admin.pdf"]


def test_notifications_a_leur_compte(base):
    invite = db.creer_compte("camille", "h", "invite", [])
    db.enregistrer_abonnement("https://push/admin", "p", "a")              # sans compte : l'admin
    db.maj_compte(1, hash="h")
    with db.connect() as c:
        c.execute("UPDATE abonnements_push SET compte_id = 1")
    with db.pour_compte(invite):
        db.enregistrer_abonnement("https://push/camille", "p", "a")
        assert [a["endpoint"] for a in db.abonnements()] == ["https://push/camille"]
        db.notifier("revision:1", "Fiche prête", "VLAN")
    assert [a["endpoint"] for a in db.abonnements_de(invite)] == ["https://push/camille"]
    assert [a["endpoint"] for a in db.abonnements_de(None)] == ["https://push/admin"]   # alertes : les admins
    assert [n["compte_id"] for n in db.reserver_notifications()] == [invite]


def test_en_cours_seules_les_urgentes_partent(base):
    db.notifier("revision:2", "Fiche prête", "DHCP")
    db.notifier("jour:MATHS:2026-10-26T10:00", "MATHS aujourd'hui à 10:00")
    assert [n["cle"] for n in db.reserver_notifications(("jour:", "actu-alerte:"))] == ["jour:MATHS:2026-10-26T10:00"]
    assert [n["cle"] for n in db.reserver_notifications()] == ["revision:2"]   # à la pause


def test_admin_d_origine_et_invitations(base):
    assert db.compte(1)["role"] == "admin"
    import datetime as dt
    fin = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15)
    i = db.creer_invitation("empreinte", "compte", fin, "invite", ["bibliotheque"], cree_par=1)
    assert db.invitation("empreinte")["id"] == i
    k = db.creer_compte("camille", "h", "invite", [])
    assert db.utiliser_invitation(i, k) and not db.utiliser_invitation(i, k)   # une seule fois
    assert db.invitation("empreinte") is None


def test_reglages_jamais_lisibles_au_nom_d_un_compte(base):
    db.reglage_ecrire("KDRIVE_ID", "42", False, 1)
    assert [r["valeur"] for r in db.reglages()] == ["42"]
    assert db.etat_valeur("reglages_version")
    invite = db.creer_compte("camille", "h", "invite", [])
    with db.pour_compte(invite):
        with pytest.raises(Exception), db.connect() as c:   # même un admin passe par le site, jamais par SQL
            c.execute("SELECT * FROM reglages").fetchall()
    db.reglage_effacer("KDRIVE_ID")
    assert db.reglages() == []


def test_fichiers_sur_la_vm_numeros_stables(base):
    a, b = db.fichier_local_id("Inbox/2024"), db.fichier_local_id("Inbox/2024/tp.pdf")
    autre = db.fichier_local_id("Inbox/2024-bis/x.pdf")   # même début de nom : ne doit pas suivre
    assert a >= 10**12 and db.fichier_local_id("Inbox/2024") == a
    db.fichiers_locaux_deplacer("Inbox/2024", "Etudes/2024")
    assert db.fichier_local_chemin(b) == "Etudes/2024/tp.pdf" and db.fichier_local_chemin(a) == "Etudes/2024"
    assert db.fichier_local_chemin(autre) == "Inbox/2024-bis/x.pdf"
    db.copie_kdrive_faite("BTS1/x.pdf", 3, 1.5, 9)
    assert db.copies_kdrive()["BTS1/x.pdf"]["kdrive_id"] == 9


def test_formations_et_regles_par_compte(base):
    invite = db.creer_compte("camille", "h", "invite", [])
    with db.pour_compte(1):
        d = db.creer(100, "a.pdf")
        db.maj(d, statut="classe", niveau="BTS1", matiere="Maths", type="Cours")
        db.definir_formation("BTS1", "BTS SIO SISR")
        db.definir_formation("BTS1", "BTS SIO option SISR")          # nouveau nom : à rechercher
        assert db.fusionner_matieres("BTS1", ["Maths"], "Mathématiques") == 1
        assert db.fusionner_matieres("BTS1", ["Mathématiques"], "Mathématiques pour l'informatique") == 1
        # La règle suit la dernière fusion ; plus de règle qui part du nom gardé
        assert db.matieres_regles() == {("BTS1", "Maths"): "Mathématiques pour l'informatique",
                                        ("BTS1", "Mathématiques"): "Mathématiques pour l'informatique"}
        assert db.supprimer_regle("BTS1", "Maths") == 1
        db.rendre_matiere("BTS1", "Mathématiques pour l'informatique", True)
        d2 = db.creer(101, "b.pdf")
        db.maj(d2, statut="classe", niveau="BTS1", matiere="Maths info", type="Cours")
        db.fusionner_matieres("BTS1", ["Mathématiques pour l'informatique"], "Maths info")   # reste publique
        assert db.matieres_publiques() == {("BTS1", "Maths info")}
    with db.pour_compte(invite):
        assert db.formations() == [] and db.matieres_regles() == {}
        db.definir_formation("BTS1", "BTS SIO option SLAM")
    a_chercher = db.formations_a_chercher()                            # le worker : tous les comptes
    assert sorted((f["compte_id"], f["nom"]) for f in a_chercher) == [(1, "BTS SIO option SISR"),
                                                                       (invite, "BTS SIO option SLAM")]
    db.maj_formation(1, "BTS1", statut="pret", referentiel={"matieres": [{"nom": "Réseaux"}]}, modele="opus")
    with db.pour_compte(1):
        f = db.formations()[0]
        assert f["statut"] == "pret" and f["referentiel"]["matieres"][0]["nom"] == "Réseaux"
        db.definir_formation("BTS1", "")
        assert db.formations() == []
    with db.pour_compte(invite):
        assert len(db.formations()) == 1


def test_anciens_supports_de_la_matiere_par_compte(base):
    """Repères d'une nouvelle fiche : supports prêts de la même matière, du même compte, jamais elle-même."""
    invite = db.creer_compte("camille", "h", "invite", ["bibliotheque"])
    with db.connect() as c:
        for genre, sujet, matiere, statut in [("fiche", "VLAN", "Réseaux", "pret"), ("quiz", "VLAN", "Réseaux", "pret"),
                                              ("fiche", "OSPF", "Réseaux", "en_attente"),
                                              ("fiche", "Suites", "Maths", "pret")]:
            c.execute("INSERT INTO revisions (genre, sujet, doc_ids, matiere, statut, contenu) "
                      "VALUES (%s, %s, '{}', %s, %s, '# x')", (genre, sujet, matiere, statut))
    with db.pour_compte(1):
        lignes = db.supports_de_la_matiere("Réseaux", None)
        assert [r["sujet"] for r in lignes] == ["VLAN"]
        assert db.supports_de_la_matiere("Réseaux", lignes[0]["id"]) == []
        assert {r["sujet"] for r in db.supports_de_la_matiere(None, None)} == {"VLAN", "Suites"}
        db.maj_revision(lignes[0]["id"], "pret", contenu="# x", controle={"ok": True, "ecarts": []})
        assert db.revision(lignes[0]["id"])["controle"] == {"ok": True, "ecarts": []}
    with db.pour_compte(invite):
        assert db.supports_de_la_matiere("Réseaux", None) == []   # les fiches de l'admin restent à l'admin


def test_public_ou_prive(base):
    """Privé par défaut ; public : lisible par les autres sur les pages « Partagés » seulement, jamais modifiable."""
    camille = db.creer_compte("camille", "h", "invite", ["bibliotheque"])
    dominique = db.creer_compte("dominique", "h", "invite", ["bibliotheque"])
    with db.pour_compte(camille):
        vlan = db.creer(200, "vlan.pdf")
        ip = db.creer(201, "ip.pdf")
        prive = db.creer(202, "prive.pdf")
        with db.connect() as c:
            c.execute("UPDATE documents SET statut = 'classe', niveau = 'BTS1', matiere = 'Réseau', type = 'cours'")
            fiche = c.execute("INSERT INTO revisions (genre, sujet, doc_ids, statut, contenu) VALUES "
                              "('cartes', 'VLAN', %s, 'pret', '') RETURNING id", ([vlan, prive],)).fetchone()["id"]
            c.execute("INSERT INTO cartes (revision_id, recto, verso) VALUES (%s, 'VLAN ?', 'réseau virtuel')", (fiche,))
            k = c.execute("INSERT INTO campagnes (titre, debut, fin) VALUES ('BTS blanc', current_date, current_date) "
                          "RETURNING id").fetchone()["id"]
            dans_k = c.execute("INSERT INTO revisions (genre, sujet, doc_ids, statut, campagne_id) VALUES "
                               "('fiche', 'IP', %s, 'pret', %s) RETURNING id", ([ip], k)).fetchone()["id"]
    with db.pour_compte(dominique):                          # tout est privé au départ
        assert db.partages_documents() == [] and db.partages_revisions() == [] and db.partages_campagnes() == []
        assert db.document_partage(vlan) is None
        assert db.changer_visibilite("documents", vlan, True) is False   # pas chez un autre
    with db.pour_compte(camille):
        assert db.changer_visibilite("revisions", fiche, True)
        db.rendre_matiere("BTS1", "Réseau", True)              # toute la matière…
        assert db.changer_visibilite("documents", prive, False)  # … sauf ce cours
        assert db.matieres_publiques() == {("BTS1", "Réseau")}
    with db.pour_compte(dominique):
        assert sorted(d["nom"] for d in db.partages_documents()) == ["ip.pdf", "vlan.pdf"]
        assert db.document_partage(prive) is None
        assert [r["id"] for r in db.partages_revisions()] == [fiche]
        assert [c["recto"] for c in db.cartes_partagees(fiche)] == ["VLAN ?"]
        assert [d["nom"] for d in db.documents_partages([vlan, prive])] == ["vlan.pdf"]
        assert _documents() == []                              # ses autres pages ne changent pas
        with db.partages(), db.connect() as c:                 # lecture seule, même sur la page « Partagés »
            assert c.execute("UPDATE documents SET nom = 'x' WHERE id = %s RETURNING id", (vlan,)).fetchone() is None
            assert c.execute("DELETE FROM revisions WHERE id = %s RETURNING id", (fiche,)).fetchone() is None
            assert c.execute("UPDATE cartes SET recto = 'x' RETURNING id").fetchone() is None
        assert db.campagne_partagee(k) == (None, [])
    with db.pour_compte(camille):
        db.changer_visibilite("campagnes", k, True)
    with db.pour_compte(dominique):                          # une campagne publique montre ses révisions
        campagne, revs = db.campagne_partagee(k)
        assert campagne["auteur"] == "camille" and [r["id"] for r in revs] == [dans_k]
        assert {r["id"] for r in db.partages_revisions()} == {fiche, dans_k}
    # L'admin : « Tout voir » montre tout, privé compris, comme avant ; sans, ses pages restent les siennes
    with db.pour_compte(1, tout_voir=True):
        assert _documents() == ["ip.pdf", "prive.pdf", "vlan.pdf"]
        assert db.document_partage(prive) is None              # « Partagés » : la même chose que pour les autres
    with db.pour_compte(1):
        assert _documents() == []
        db.rendre_matiere("BTS1", "Réseau", False)             # sans effet sur la matière de camille
    with db.pour_compte(camille):
        assert db.matieres_publiques() == {("BTS1", "Réseau")}
        db.rendre_matiere("BTS1", "Réseau", False)
        assert db.changer_visibilite("documents", vlan, None)
    with db.pour_compte(dominique):
        assert db.partages_documents() == []


def test_a_revoir_par_matiere(base):
    fiche = db.creer_revision("fiche", "Réseaux : VLAN", [])
    paquet = db.creer_revision("cartes", "Réseaux : DHCP", [])
    db.maj_revision(fiche, "pret", "# VLAN")
    db.maj_revision(paquet, "pret", "[]")
    db.creer_cartes(paquet, [{"recto": "a", "verso": "b"}, {"recto": "c", "verso": "d"}])
    r = db.a_revoir_par_matiere()
    assert r["Réseaux"]["cartes"] == 2 and r["Réseaux"]["paquet"] == paquet
    assert r["Réseaux"]["fiche"]["id"] == fiche and r["Réseaux"]["fiche"]["sujet"] == "Réseaux : VLAN"


def test_matieres_laissees_separees_par_compte(base):
    """« Laisser telles quelles » et « Reproposer » d'un invité ne touchent pas aux choix de l'admin."""
    invite = db.creer_compte("camille", "h", "invite", ["organiser"])
    with db.pour_compte(1):
        db.garder_distinctes("BTS1", ["Maths", "Mathématiques"])
    with db.pour_compte(invite):
        db.garder_distinctes("BTS1", ["Maths", "Mathématiques"])   # la même paire, chez lui
        assert db.oublier_distinctes() == 1
        assert db.matieres_distinctes() == {}
    with db.pour_compte(1):
        assert db.matieres_distinctes() == {"BTS1": {frozenset(("Maths", "Mathématiques"))}}


def test_second_admin_synchronise_pronote(base, monkeypatch):
    """Pronote est rangé chez le compte 1 : un autre admin qui synchronise ne bute pas sur ses lignes."""
    from claunote import pronote
    from tests.test_pronote import SYNCHRO, faux_service
    monkeypatch.setattr(pronote, "PREPARATION_AUTO", False)
    c = faux_service({("GET", "/etat"): (200, {"connecte": True}), ("POST", "/synchro"): (200, SYNCHRO)})
    pronote.synchroniser(c)                       # le worker, puis un second admin
    admin2 = db.creer_compte("dominique", "h", "admin", [])
    with db.pour_compte(admin2):
        assert pronote.synchroniser(c)["evenements"] == 3
    with db.connect() as cx:
        assert {r["compte_id"] for r in cx.execute("SELECT compte_id FROM pronote_evenements").fetchall()} == {1}


def test_chacun_range_ses_propres_depots(base):
    """Tri des imports et « À vérifier » : à celui qui a déposé, pas à l'admin, même avec « Tout voir »."""
    from claunote.importation import Fichier
    camille = db.creer_compte("camille", "h", "invite", ["organiser"])
    db.noter_depot(500, camille)
    # Le worker (sans compte) trie un lot : chaque fichier revient à son déposant
    db.import_enregistrer("BTS1", [Fichier(500, "tp.py", ["BTS1"], categorie="code", decision="inutile"),
                                   Fichier(501, "vieux.py", ["BTS1"], categorie="code", decision="inutile")])
    db.import_a_comprendre("BTS1", camille)
    db.import_a_comprendre("BTS1", 1)                      # même nom de lot chez l'admin : deux lots distincts
    assert sorted((r["compte_id"], r["lot"]) for r in db.lots_a_comprendre()) == [(1, "BTS1"), (camille, "BTS1")]
    db.import_lot_maj("BTS1", "Par matière.", {"BTS1": {"niveau": "BTS1"}}, camille)
    db.lot_compris("BTS1", camille)
    assert [(r["compte_id"], r["lot"]) for r in db.lots_a_comprendre()] == [(1, "BTS1")]
    with db.pour_compte(camille):
        d = db.creer(600, "scan.pdf")
        db.maj(d, statut="a_verifier")
        assert [f["kdrive_id"] for f in db.import_fichiers("inutile")] == [500]
        assert db.nb_import_a_confirmer() == 1 and db.nb_a_verifier() == 1
        assert [b["resume"] for b in db.import_bilan()] == ["Par matière."]
        assert db.import_marquer([501], "supprimer") == 0   # le fichier de l'admin : ni lu ni modifié
    with db.pour_compte(1, tout_voir=True):                # « Tout voir » : l'admin peut regarder…
        assert len(db.import_fichiers("inutile")) == 2
        # …mais sa file et ses pastilles ne comptent que ses propres dépôts
        assert db.nb_import_a_confirmer() == 1 and db.nb_a_verifier() == 0 and db.a_verifier() == []
        assert [(r["qui"], r["n"]) for r in db.a_verifier_ailleurs()] == [("camille", 1)]
        assert db.reessayer_erreurs() == 0
    with db.pour_compte(1):
        assert [f["kdrive_id"] for f in db.import_fichiers("inutile")] == [501]
        assert [b["resume"] for b in db.import_bilan()] == [None]


def test_tri_des_imports_d_avant_rendu_a_son_deposant(base):
    """Installation d'avant : import_fichiers et import_lots sans compte, rendus au déposant à la mise à jour."""
    camille = db.creer_compte("camille", "h", "invite", ["organiser"])
    db.noter_depot(700, camille)
    with db.connect() as c:
        c.execute("DROP POLICY par_compte ON import_fichiers; DROP POLICY par_compte ON import_lots; "
                  "DROP TRIGGER proprietaire ON import_fichiers; DROP TRIGGER proprietaire ON import_lots")
        c.execute("ALTER TABLE import_fichiers DROP COLUMN compte_id")
        c.execute("ALTER TABLE import_lots DROP CONSTRAINT import_lots_pkey; ALTER TABLE import_lots DROP COLUMN compte_id; "
                  "ALTER TABLE import_lots ADD PRIMARY KEY (lot)")
        c.execute("INSERT INTO import_fichiers (kdrive_id, lot, chemin, nom, categorie, decision) VALUES "
                  "(700, 'V', 'V/a.py', 'a.py', 'code', 'inutile'), (701, 'V', 'V/b.py', 'b.py', 'code', 'inutile')")
        c.execute("INSERT INTO import_lots (lot, resume) VALUES ('V', 'Ancien')")
    db.init()
    with db.connect() as c:
        assert {(r["kdrive_id"], r["compte_id"]) for r in c.execute("SELECT kdrive_id, compte_id FROM import_fichiers")} \
            == {(700, camille), (701, 1)}
        assert sorted((r["compte_id"], r["resume"]) for r in c.execute("SELECT * FROM import_lots")) \
            == [(1, "Ancien"), (camille, "Ancien")]
