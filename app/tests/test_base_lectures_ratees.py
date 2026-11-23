"""Cours enregistrés avec un message de lecture ratée à la place du texte (vraie base, CLAUNOTE_TEST_DB)."""
import pytest

from claunote import db
from tests.test_base_comptes import URL, base  # noqa: F401 (fixture)
from tests.test_agent import ECHEC_ANTO

pytestmark = pytest.mark.skipif(not URL, reason="CLAUNOTE_TEST_DB non défini (base PostgreSQL jetable)")


def test_lecture_ratee_repasse_a_verifier_et_relue(base):  # noqa: F811
    rate = db.creer(100, "1. E-Waste.pdf")
    db.maj(rate, statut="classe", niveau="BTS2", matiere="Anglais", type="Cours", titre="E-Waste",
           resume="Le contenu du document n'a pas pu être lu.", transcription=ECHEC_ANTO)
    bon = db.creer(101, "poppler.pdf")
    db.maj(bon, statut="classe", niveau="BTS1", matiere="Systèmes", type="TP", titre="Paquets",
           resume="apt.", transcription="# TP Linux\n\n" + "Installer des paquets avec apt. " * 30 + "poppler-utils")
    with db.connect() as c:
        c.execute("INSERT INTO passages (document_id, rang, texte) VALUES (%s, 0, %s)", (rate, ECHEC_ANTO))
    db.init()   # au démarrage du site ou du worker
    d = db.document(rate)
    assert (d["statut"], d["relire"], d["resume"], d["transcription"]) == ("a_verifier", True, None, None)
    assert "relire" in d["note"] and d["matiere"] == "Anglais"   # classement d'après le dossier gardé
    assert db.document(bon)["statut"] == "classe" and not db.document(bon)["relire"]
    with db.connect() as c:
        assert c.execute("SELECT count(*) AS n FROM passages WHERE document_id = %s", (rate,)).fetchone()["n"] == 0
    assert [r["id"] for r in db.a_relire()] == [rate]
    db.maj(rate, relire=False)
    db.init()   # rejouable : plus rien à changer
    assert not db.document(rate)["relire"]
