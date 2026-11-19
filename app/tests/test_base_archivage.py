"""Archivage sur une vraie base PostgreSQL (voir test_base_comptes.py : CLAUNOTE_TEST_DB, base JETABLE)."""
import pytest

from claunote import archivage, db
from tests.test_base_comptes import URL, base  # noqa: F401 (fixture)

pytestmark = pytest.mark.skipif(not URL, reason="CLAUNOTE_TEST_DB non défini (base PostgreSQL jetable)")


def _vieillir(table: str, ident: int, **colonnes):
    with db.connect() as c:
        for col, jours in colonnes.items():
            c.execute(f"UPDATE {table} SET {col} = now() - make_interval(days => %s) WHERE id = %s", (jours, ident))


def test_archiver_garder_restaurer(base):  # noqa: F811
    doc = db.creer(100, "vlan.pdf")
    fiche = db.creer_revision("fiche", "VLAN", [doc])
    paquet = db.creer_revision("cartes", "Ports", [doc])
    neuve = db.creer_revision("fiche", "Routage", [doc])
    for r in (fiche, paquet, neuve):
        db.maj_revision(r, "pret", contenu="x")
    db.creer_cartes(paquet, [{"recto": "SSH ?", "verso": "22"}, {"recto": "HTTPS ?", "verso": "443"}])
    _vieillir("revisions", fiche, cree_le=90)
    _vieillir("revisions", paquet, cree_le=40)
    import datetime as dt
    hier = dt.date.today() - dt.timedelta(days=40)
    k = db.creer_campagne("BTS blanc", "", hier - dt.timedelta(days=5), hier)
    db.maj_campagne(k, "pret", programme={"jours": []})
    dans_k = db.creer_revision("quiz", "Réseaux", [doc], campagne_id=k)
    db.maj_revision(dans_k, "pret", contenu="[]")

    props = {(p["type"], p["id"]) for p in archivage.propositions()}
    assert props == {("revision", fiche), ("revision", paquet), ("campagne", k)}   # pas la neuve, pas dans_k seule
    assert db.nb_cartes_a_revoir() == 2

    db.garder([fiche])
    db.archiver([paquet], [k])
    assert {(p["type"], p["id"]) for p in archivage.propositions()} == set()
    assert db.nb_cartes_a_revoir() == 0 and db.paquets() == [] and db.prochaine_carte() is None
    assert db.nb_cartes_a_revoir(paquet) == 2                                     # le paquet reste ouvrable
    assert db.campagnes() == [] and {r["id"] for r in db.historique()[0]} == {fiche, neuve}
    campagnes, revisions = db.archives()
    assert [c["id"] for c in campagnes] == [k] and [r["id"] for r in revisions] == [paquet]

    db.restaurer(campagnes=[k])
    assert [c["id"] for c in db.campagnes()] == [k] and db.revision(dans_k)["archive_le"] is None
    assert db.supprimer_archives([neuve]) == 0                                    # jamais ce qui n'est pas archivé
    assert db.supprimer_archives([paquet]) == 1 and db.revision(paquet) is None


def test_ouverture_notee(base):  # noqa: F811
    doc = db.creer(100, "vlan.pdf")
    r = db.creer_revision("fiche", "VLAN", [doc])
    db.noter_ouverture(r)
    assert db.revision(r)["ouverte_le"] is not None
