"""Logo animé : les créations IA en cours d'un compte, et celles finies depuis, sur une vraie base PostgreSQL.

Lancé seulement si CLAUNOTE_TEST_DB donne une base JETABLE (voir test_base_comptes.py).
"""
import pytest

from claunote import db
from tests.test_base_comptes import URL, base  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(not URL, reason="CLAUNOTE_TEST_DB non défini (base PostgreSQL jetable)")


def _revision(**champs):
    cols = {"genre": "fiche", "sujet": "VLAN", "doc_ids": [], **champs}
    with db.connect() as c:
        return c.execute(f"INSERT INTO revisions ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                         "RETURNING id", list(cols.values())).fetchone()["id"]


def test_chacun_ses_generations(base):
    camille = db.creer_compte("camille", "h", "invite", ["reviser"])
    admin = 1
    with db.pour_compte(admin):
        fiche = _revision()
        pret = _revision(sujet="IP", statut="pret", contenu="# IP")
        k = db.creer_campagne("BTS blanc", "", "2026-12-01", "2026-12-05")
    with db.pour_compte(camille):
        quiz = _revision(genre="quiz", sujet="DHCP")
        chez_admin = _revision(sujet="NAT")
    assert db.mettre_en_demande("revisions", chez_admin)   # attend l'accord de l'admin : ne tourne pas
    with db.connect() as c:
        c.execute("INSERT INTO formations (compte_id, niveau, nom) VALUES (%s, 'BTS1', 'BTS SIO')", (camille,))

    # L'admin, même en « Tout voir », ne voit tourner que les siennes
    with db.pour_compte(admin, True):
        assert db.generations(admin) == {"actives": sorted([f"r{fiche}", f"c{k}"]), "finies": []}
    with db.pour_compte(camille):
        assert db.generations(camille)["actives"] == sorted([f"r{quiz}", "f:BTS1"])
        assert db.generations(camille, [f"r{fiche}"])["finies"] == []   # pas la sienne

    # Finie : la coche une fois, pour qui la suivait
    db.maj_revision(fiche, "pret", contenu="# VLAN")
    db.maj_campagne(k, "erreur", erreur="x")
    with db.pour_compte(admin):
        assert db.generations(admin, [f"r{fiche}", f"c{k}", f"r{pret}"]) == {"actives": [], "finies": [f"r{fiche}", f"r{pret}"]}
        assert db.generations(admin) == {"actives": [], "finies": []}
        # Une modification demandée repart en cours
        assert db.demander_retouche(fiche, "ajoute un schéma")
        assert db.generations(admin)["actives"] == [f"r{fiche}"]
    db.maj_formation(camille, "BTS1", statut="pret")
    with db.pour_compte(camille):
        assert db.generations(camille, ["f:BTS1"]) == {"actives": [f"r{quiz}"], "finies": ["f:BTS1"]}
