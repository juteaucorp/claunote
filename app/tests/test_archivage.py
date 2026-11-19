"""Ménage proposé : quoi archiver, et jamais ce qui sert encore."""
import datetime as dt

from claunote import archivage, db

PARIS = archivage.PARIS
MAINTENANT = dt.datetime(2026, 11, 19, 18, 0, tzinfo=PARIS)


def il_y_a(jours: int) -> dt.datetime:
    return MAINTENANT - dt.timedelta(days=jours)


def rev(**k):
    return {"id": 1, "genre": "fiche", "sujet": "VLAN", "matiere": "Réseaux", "statut": "pret", "date_eval": None,
            "cree_le": il_y_a(5), "ouverte_le": None, "fait_le": None, "garder_jusqu_au": None, "nb_cartes": 0,
            "cartes_dues": 0, "derniere_revue": None, "evaluation_a_venir": False, **k}


def test_fiche_recente_ou_rouverte_gardee():
    assert archivage.raison_revision(rev(), MAINTENANT) is None
    assert archivage.raison_revision(rev(cree_le=il_y_a(200), ouverte_le=il_y_a(3)), MAINTENANT) is None
    assert archivage.raison_revision(rev(cree_le=il_y_a(90)), MAINTENANT) == "Pas ouverte depuis 90 jours"


def test_evaluation_passee():
    r = rev(date_eval=MAINTENANT.date() - dt.timedelta(days=10))
    assert archivage.raison_revision(r, MAINTENANT).startswith("Évaluation du 09/11/2026 passée")
    assert archivage.raison_revision(rev(date_eval=MAINTENANT.date() - dt.timedelta(days=2)), MAINTENANT) is None


def test_jamais_propose_si_controle_a_venir_ou_garde():
    vieux = dict(cree_le=il_y_a(300))
    assert archivage.raison_revision(rev(**vieux, date_eval=MAINTENANT.date() + dt.timedelta(days=3)), MAINTENANT) is None
    assert archivage.raison_revision(rev(**vieux, evaluation_a_venir=True), MAINTENANT) is None
    garde = MAINTENANT + dt.timedelta(days=10)
    assert archivage.raison_revision(rev(**vieux, garder_jusqu_au=garde), MAINTENANT) is None
    assert archivage.raison_revision(rev(**vieux, garder_jusqu_au=il_y_a(1)), MAINTENANT)   # garde finie


def test_flashcards_delaissees():
    paquet = dict(genre="cartes", nb_cartes=20)
    assert archivage.raison_revision(rev(**paquet, cree_le=il_y_a(10)), MAINTENANT) is None
    assert archivage.raison_revision(rev(**paquet, cree_le=il_y_a(25)), MAINTENANT) == \
        "Jamais commencé, créé il y a 25 jours"
    assert archivage.raison_revision(rev(**paquet, cree_le=il_y_a(90), derniere_revue=il_y_a(5)), MAINTENANT) is None
    assert archivage.raison_revision(rev(**paquet, cree_le=il_y_a(90), derniere_revue=il_y_a(40), cartes_dues=12),
                                     MAINTENANT) == "Plus révisé depuis 40 jours · 12 cartes en retard"
    assert archivage.raison_revision(rev(genre="cartes", nb_cartes=0, cree_le=il_y_a(90)), MAINTENANT) is None


def test_erreur_ancienne():
    assert archivage.raison_revision(rev(statut="erreur", cree_le=il_y_a(3)), MAINTENANT) is None
    assert archivage.raison_revision(rev(statut="erreur", cree_le=il_y_a(20)), MAINTENANT).startswith("Jamais générée")


def test_campagne_finie():
    k = {"id": 3, "titre": "BTS blanc", "fin": MAINTENANT.date() - dt.timedelta(days=3), "nb": 6, "faites": 4,
         "garder_jusqu_au": None}
    assert archivage.raison_campagne(k, MAINTENANT) is None
    k["fin"] = MAINTENANT.date() - dt.timedelta(days=10)
    assert archivage.raison_campagne(k, MAINTENANT) == "Terminée le 09/11/2026 · 4/6 supports faits"


def test_propositions_et_resume(monkeypatch):
    fin = MAINTENANT.date() - dt.timedelta(days=30)
    monkeypatch.setattr(db, "candidats_archivage", lambda: (
        [rev(id=1, cree_le=il_y_a(90)), rev(id=2), rev(id=3, genre="cartes", nb_cartes=8, cree_le=il_y_a(40))],
        [{"id": 9, "titre": "Partiels", "fin": fin, "nb": 4, "faites": 4, "garder_jusqu_au": None}]))
    props = archivage.propositions(MAINTENANT)
    assert [(p["type"], p["id"]) for p in props] == [("campagne", 9), ("revision", 1), ("revision", 3)]
    assert archivage.resume(props) == ("1 campagne terminée, 1 paquet de flashcards délaissé, 1 support plus ouvert : "
                                       "je les archive ? Rien n'est supprimé.")


def test_separer_les_cases():
    assert archivage.separer(["revision:4", "campagne:2", "revision:x", "autre:3", ""]) == ([4], [2])
