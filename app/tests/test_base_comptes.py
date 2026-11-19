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
