"""Notes sur une vraie base PostgreSQL : séparation par compte, partage, conflits, recherche.
Lancé seulement si CLAUNOTE_TEST_DB donne une base JETABLE (voir test_base_comptes.py)."""
import datetime as dt

import pytest

from claunote import db
from tests.test_base_comptes import URL, base  # noqa: F401  (fixture : base vide à chaque test)

pytestmark = pytest.mark.skipif(not URL, reason="CLAUNOTE_TEST_DB non défini (base PostgreSQL jetable)")


def _cours(kdrive_id: int, nom: str, compte: int | None = None, public: bool = False) -> int:
    with db.pour_compte(compte):
        i = db.creer(kdrive_id, nom)
        db.maj(i, statut="classe", niveau="BTS2", matiere="Réseaux", type="Cours", titre=nom)
        if public:
            with db.connect() as c:
                c.execute("UPDATE documents SET public = true WHERE id = %s", (i,))
    return i


def test_chaque_compte_ses_notes_et_seul_l_auteur_modifie(base):
    invite = db.creer_compte("camille", "h", "invite", ["notes"])
    with db.pour_compte(1):
        a = db.creer_note("VLAN", "trunk 802.1Q", "BTS2", "Réseaux")["id"]
    with db.pour_compte(invite):
        b = db.creer_note("", "Mes questions sur OSPF", "BTS2", "Réseaux")["id"]
        assert [n["id"] for n in db.notes()] == [b]
        assert db.note(a) is None
        assert db.maj_note(a, None, contenu="pirate") == ("absente", None)
        assert db.supprimer_note(a) is False
        with db.connect() as c, pytest.raises(Exception):
            c.execute("INSERT INTO notes (titre, compte_id) VALUES ('pirate', 1)")
    with db.pour_compte(1, tout_voir=True):                 # « Tout voir » : l'admin les lit toutes…
        assert {n["id"] for n in db.notes()} == {a, b}
        assert db.maj_note(b, None, contenu="x")[0] == "absente"   # … mais ne modifie que les siennes
        assert db.changer_note(b, epinglee=True) is False
    with db.pour_compte(1):
        assert db.maj_note(a, None, contenu="trunk 802.1Q, VLAN natif")[0] == "ok"
    db.supprimer_compte(invite)                              # ses notes partent avec lui
    with db.pour_compte(1, tout_voir=True):
        assert [n["id"] for n in db.notes()] == [a]


def test_conflit_entre_deux_appareils(base):
    with db.pour_compte(1):
        cree = db.creer_note("TP", "version 1")
        statut, v2 = db.maj_note(cree["id"], cree["maj_le"].isoformat(), contenu="version du téléphone")
        assert statut == "ok" and v2 > cree["maj_le"]
        # Le PC écrit encore sur la version 1 : rien n'est écrasé, la version actuelle est renvoyée
        statut, actuelle = db.maj_note(cree["id"], cree["maj_le"].isoformat(), contenu="version du PC")
        assert statut == "conflit" and actuelle["contenu"] == "version du téléphone"
        assert db.maj_note(cree["id"], None, contenu="version du PC")[0] == "ok"   # « Garder ma version »
        assert db.note(cree["id"])["contenu"] == "version du PC"


def test_listes_recherche_et_archives(base):
    with db.pour_compte(1):
        vlan = db.creer_note("VLAN", "Le trunk transporte plusieurs VLAN", "BTS2", "Réseaux")["id"]
        maths = db.creer_note("Suites", "suite géométrique de raison q", "BTS1", "Maths")["id"]
        libre = db.creer_note("Idées", "acheter un switch")["id"]
        db.changer_note(maths, epinglee=True)
        assert [n["id"] for n in db.notes()][0] == maths                 # épinglée en tête
        assert [n["id"] for n in db.notes("vla")] == [vlan]              # début de mot, pendant la frappe
        assert [n["id"] for n in db.notes("géométrique")] == [maths]
        assert [n["id"] for n in db.notes("100%")] == []                 # caractères spéciaux sans erreur
        assert [n["id"] for n in db.notes(niveau="BTS2", matiere="Réseaux")] == [vlan]
        assert [n["id"] for n in db.notes(libres=True)] == [libre]
        assert db.nb_notes() == {"actives": 3, "libres": 1, "archivees": 0}
        assert [(m["matiere"], m["n"]) for m in db.matieres_notes()] == [("Réseaux", 1), ("Maths", 1)]
        assert [n["id"] for n in db.chercher_notes("trunk")] == [vlan]
        assert [n["id"] for n in db.notes_pour_question("à quoi sert un trunk ?", "BTS2", "Réseaux")] == [vlan]
        assert db.notes_pour_question("à quoi sert un trunk ?", "BTS1", "Maths") == []
        db.changer_note(vlan, archive_le=dt.datetime.now(dt.timezone.utc))
        assert vlan not in [n["id"] for n in db.notes()] and [n["id"] for n in db.notes(archivees=True)] == [vlan]
        assert db.chercher_notes("trunk") == [] and db.notes_pour_question("trunk", None, None) == []
        assert {n["id"] for n in db.notes_choisies([vlan, maths, 999])} == {vlan, maths}


def test_note_sur_un_cours_partage_et_note_publique(base):
    invite = db.creer_compte("camille", "h", "invite", ["bibliotheque", "notes"])
    prive = _cours(100, "cours-prive.pdf")
    public = _cours(101, "cours-public.pdf", public=True)
    with db.pour_compte(invite):
        assert db.cours_lisible(prive) is None and db.cours_lisible(public)["id"] == public
        assert [d["id"] for d in db.cours_pour_note("BTS2", "Réseaux")] == [public]
        n = db.creer_note("Ce que le prof a dit", "à l'oral : STP", "BTS2", "Réseaux", public)["id"]
        assert db.note(n)["compte_id"] == invite                       # la note reste à son auteur
        assert [x["id"] for x in db.notes_du_document(public)] == [n]
    with db.pour_compte(1):
        assert db.notes_du_document(public) == []                       # l'admin ne voit pas ses notes privées
        assert db.partages_notes() == [] and db.note_partagee(n) is None
    with db.pour_compte(invite):
        assert db.changer_visibilite("notes", n, True)
    with db.pour_compte(1):
        assert [x["id"] for x in db.partages_notes()] == [n] and db.note_partagee(n)["auteur"] == "camille"
        assert db.note(n) is None                                       # hors des pages Partagés : rien
    with db.pour_compte(invite):
        db.changer_note(n, archive_le=dt.datetime.now(dt.timezone.utc))
    with db.pour_compte(1):
        assert db.partages_notes() == []                                # archivée : plus partagée
    # Le cours supprimé : la note reste, sans cours rattaché
    with db.connect() as c:
        c.execute("DELETE FROM documents WHERE id = %s", (public,))
    with db.pour_compte(invite):
        assert db.note(n)["document_id"] is None


def test_cours_pour_note_sans_doublons(base):
    a = _cours(200, "squid-1.pdf")
    with db.pour_compte(None):
        db.maj(a, titre="Squid et SquidGuard")
    b = _cours(201, "squid-2.pdf")
    with db.pour_compte(None):
        db.maj(b, titre="squid et  squidguard")
    c = _cours(202, "glpi.pdf")
    with db.pour_compte(1):
        assert len(db.cours_pour_note("BTS2", "Réseaux")) == 2
        assert c in [d["id"] for d in db.cours_pour_note("BTS2", "Réseaux")]


def test_notes_publiques_des_autres_dans_leur_liste(base):
    invite = db.creer_compte("camille", "h", "invite", ["notes"])
    with db.pour_compte(invite):
        pub = db.creer_note("STP", "à l'oral : STP", "BTS2", "Réseaux")["id"]
        db.creer_note("Privée", "rien à voir")
        db.changer_visibilite("notes", pub, True)
    with db.pour_compte(1):
        assert db.notes() == []                                            # ses notes à lui : aucune
        assert [n["id"] for n in db.notes(publiques_des_autres=True)] == [pub]
        assert [n["id"] for n in db.notes("oral", publiques_des_autres=True)] == [pub]
        assert db.notes(niveau="BTS1", matiere="Maths", publiques_des_autres=True) == []
    with db.pour_compte(invite):
        assert db.notes(publiques_des_autres=True) == []                   # pas les siennes en double
