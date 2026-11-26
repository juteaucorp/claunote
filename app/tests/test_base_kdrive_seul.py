"""« kDrive seul » sur une vraie base PostgreSQL : pièces vidées de la base, notes suivies, corbeille kDrive.
Lancé seulement si CLAUNOTE_TEST_DB donne une base JETABLE (voir test_base_comptes.py)."""
import pytest

from claunote import db
from tests.test_base_comptes import URL, base  # noqa: F401  (fixture : base vide à chaque test)

pytestmark = pytest.mark.skipif(not URL, reason="CLAUNOTE_TEST_DB non défini (base PostgreSQL jetable)")


def _corbeille():
    with db.connect() as c:
        return sorted(r["kdrive_id"] for r in c.execute("SELECT kdrive_id FROM kdrive_corbeille").fetchall())


def test_pieces_videes_de_la_base_et_corbeille(base):
    with db.pour_compte(1):
        rev = db.creer_revision("fiche", "VLAN", [])
        db.ajouter_pieces([("edt.pdf", b"%PDF" * 1000), ("sujet.png", b"png")], revision_id=rev)
    a_ranger = db.pieces_a_ranger()
    assert [p["nom"] for p in a_ranger] == ["edt.pdf", "sujet.png"] and bytes(a_ranger[0]["contenu"])[:4] == b"%PDF"
    assert db.piece_rangee(a_ranger[0]["id"], 5001) and db.piece_rangee(a_ranger[1]["id"], 5002)
    assert db.pieces_a_ranger() == []
    assert db.place_pieces() > 0
    db.compacter_pieces()
    with db.pour_compte(1):
        pieces = db.pieces_jointes(rev, contenu=True)
        assert [(p["kdrive_id"], p["contenu"]) for p in pieces] == [(5001, None), (5002, None)]
        assert db.piece_jointe(pieces[0]["id"])["kdrive_id"] == 5001   # la page /piece la retrouve
    assert not db.piece_rangee(999, 5003) and _corbeille() == [5003]   # supprimée entre-temps : fichier jeté
    with db.pour_compte(1):
        db.supprimer_revision(rev)                     # la révision part : ses pièces vont à la corbeille kDrive
    assert _corbeille() == [5001, 5002, 5003]


def test_notes_a_copier_puis_suivies(base):
    invite = db.creer_compte("camille", "h", "invite", ["notes"])
    with db.pour_compte(1):
        a = db.creer_note("VLAN", "trunk", "BTS2", "Réseaux")
    with db.pour_compte(invite):
        b = db.creer_note("", "OSPF")
    assert {n["id"] for n in db.notes_a_copier()} == {a["id"], b["id"]}   # le worker voit celles de tous
    for n in db.notes_a_copier():
        assert db.note_copiee(n["id"], 7000 + n["id"], n["maj_le"])
    assert db.notes_a_copier() == []
    with db.pour_compte(1):
        assert db.maj_note(a["id"], None, contenu="trunk 802.1Q")[0] == "ok"
        db.changer_note(a["id"], epinglee=True)        # épingler ne change pas le texte : pas de recopie
    assert [n["id"] for n in db.notes_a_copier()] == [a["id"]]
    with db.pour_compte(invite):
        assert db.supprimer_note(b["id"])
    assert _corbeille() == [7000 + b["id"]]
    db.supprimer_compte(invite)
    assert not db.note_copiee(b["id"], 9999, None) and 9999 in _corbeille()
