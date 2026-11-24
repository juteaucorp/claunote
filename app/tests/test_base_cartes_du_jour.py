"""Nouvelles flashcards par jour (Mon profil) sur une vraie base PostgreSQL (voir test_base_comptes.py)."""
import datetime as dt

import pytest

from claunote import db
from tests.test_base_comptes import URL, base  # noqa: F401 (fixture)

pytestmark = pytest.mark.skipif(not URL, reason="CLAUNOTE_TEST_DB non défini (base PostgreSQL jetable)")

ETAT = {"fsrs": {}, "echeance": dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3),
        "prochaine": dt.date.today() + dt.timedelta(days=3), "intervalle": 3, "repetitions": 1,
        "retenue": 0.9, "stabilite": 3.0, "difficulte": 5.0}


def test_limite_des_nouvelles_cartes(base):  # noqa: F811
    doc = db.creer(100, "ports.pdf")
    paquet = db.creer_revision("cartes", "Ports", [doc])
    db.maj_revision(paquet, "pret", contenu="x")
    db.creer_cartes(paquet, [{"recto": f"Port {i} ?", "verso": str(i)} for i in range(5)])
    assert db.nb_cartes_a_revoir() == 5 and db.nb_cartes_a_revoir(nouvelles_max=2) == 2
    # Une carte vue avant aujourd'hui puis revue aujourd'hui : pas une nouvelle du jour
    vieille = db.prochaine_carte()["id"]
    db.maj_carte(vieille, 4, ETAT)
    with db.connect() as c:
        c.execute("UPDATE revues_cartes SET le = now() - interval '2 days' WHERE carte_id = %s", (vieille,))
        c.execute("UPDATE cartes SET echeance = now() - interval '1 hour' WHERE id = %s", (vieille,))
    db.maj_carte(vieille, 4, {**ETAT})
    with db.connect() as c:
        c.execute("UPDATE cartes SET echeance = now() - interval '1 hour' WHERE id = %s", (vieille,))
    assert db.nb_cartes_a_revoir(nouvelles_max=2) == 1 + 2   # la déjà vue + 2 nouvelles permises
    # Deux nouvelles vues aujourd'hui : la limite est atteinte, seules les déjà vues reviennent
    assert db.prochaine_carte(nouvelles_max=2)["id"] == vieille   # les déjà vues passent d'abord
    for k in [k for k in db.cartes(paquet) if k["vue_le"] is None][:2]:
        db.maj_carte(k["id"], 4, ETAT)
    assert db.nb_cartes_a_revoir(nouvelles_max=2) == 1
    assert db.prochaine_carte(nouvelles_max=2)["id"] == vieille
    assert db.nb_cartes_a_revoir() == 3   # sans limite : la vieille + les 2 jamais vues
    assert db.prochaine_carte(paquet, nouvelles_max=0) is not None
    with db.connect() as c:
        c.execute("UPDATE cartes SET echeance = now() + interval '2 days' WHERE id = %s", (vieille,))
    assert db.prochaine_carte(nouvelles_max=2) is None and db.prochaine_carte()["vue_le"] is None
