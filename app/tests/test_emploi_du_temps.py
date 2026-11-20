import datetime as dt
import json

from claunote import db, emploi_du_temps as edt, notifications as nt

PARIS = edt.PARIS
LUNDI = dt.date(2026, 10, 26)
MARDI = dt.date(2026, 10, 27)


def h(jour, hh, mm=0):
    return dt.datetime(jour.year, jour.month, jour.day, hh, mm, tzinfo=PARIS)


def creneau(jour, de, a, dispense=False):
    return {"debut": h(jour, *de).isoformat(), "fin": h(jour, *a).isoformat(), "matiere": "X", "dispense": dispense,
            "genre": "lesson"}


# Lundi au lycée, mardi en entreprise (tout « Dispensé »), sur une synchro du 26/10 au 23/11
DONNEES = {"du": h(LUNDI, 0).isoformat(), "au": h(dt.date(2026, 11, 23), 0).isoformat(), "creneaux": [
    creneau(LUNDI, (8, 0), (10, 0)), creneau(LUNDI, (10, 5), (12, 0)),    # 5 min d'interclasse : pas une pause
    creneau(LUNDI, (13, 30), (15, 30)), creneau(LUNDI, (15, 45), (16, 30)),  # 15 min de récré : une pause
    creneau(MARDI, (8, 0), (12, 0), True), creneau(MARDI, (13, 30), (17, 0), True),
]}


def test_cours_pauses_et_midi():
    occ = lambda *t: edt.occupation(h(LUNDI, *t), DONNEES)[0]
    assert occ(7, 30) is None and occ(9) == "cours"
    assert occ(10, 2) == "cours"                # interclasse trop court
    assert occ(12, 5) == "cours" and occ(12, 15) is None   # 10 min pour sortir, puis midi
    assert occ(15, 35) == "cours" and occ(15, 40) is None  # la récré de 15 min
    assert occ(16, 45) is None
    assert edt.libre_a_partir(h(LUNDI, 9), DONNEES) == h(LUNDI, 12, 10)


def test_entreprise_dispense():
    occ = lambda *t: edt.occupation(h(MARDI, *t), DONNEES)[0]
    assert occ(9) == "entreprise" and occ(12, 30) is None   # pause déjeuner
    assert occ(17, 15) == "entreprise" and occ(17, 31) is None  # trajet compté
    assert edt.libre_a_partir(h(MARDI, 14), DONNEES) == h(MARDI, 17, 30)


def test_jour_inconnu_rien_ne_change():
    hors = dt.date(2026, 12, 1)
    assert edt.journee(hors, DONNEES) is None and edt.occupation(h(hors, 10), DONNEES) == (None, None)
    assert edt.heure_rappel(hors, dt.time(18), DONNEES) == (h(hors, 18), "heure choisie")
    assert edt.occupation(h(LUNDI, 9), {}) == (None, None)


def test_heure_du_rappel():
    assert edt.heure_rappel(LUNDI, dt.time(18), DONNEES) == (h(LUNDI, 16, 40), "à la sortie des cours")
    assert edt.heure_rappel(LUNDI, dt.time(10), DONNEES) == (h(LUNDI, 12, 10), "après les cours")
    assert edt.heure_rappel(MARDI, dt.time(17), DONNEES) == (h(MARDI, 17, 30), "après le travail")
    assert edt.heure_rappel(MARDI, dt.time(19), DONNEES) == (h(MARDI, 17, 30), "en rentrant du travail")
    mercredi = dt.date(2026, 10, 28)   # pas de cours : l'heure choisie
    assert edt.heure_rappel(mercredi, dt.time(18), DONNEES) == (h(mercredi, 18), "heure choisie")


def test_synchro_ratee_garde_l_ancien(monkeypatch):
    etat = {"pronote_edt": json.dumps(DONNEES)}
    monkeypatch.setattr(db, "etat_ecrire", lambda k, v: etat.update({k: v}))
    edt.enregistrer(None, None, None)
    assert json.loads(etat["pronote_edt"]) == DONNEES
    edt.enregistrer([{"debut": "a", "fin": "b", "matiere": "M", "dispense": True, "statut": "Dispensé"}], "x", "y")
    assert json.loads(etat["pronote_edt"])["creneaux"] == [
        {"debut": "a", "fin": "b", "matiere": "M", "dispense": True, "genre": None}]


def test_file_attend_la_fin_du_cours(monkeypatch):
    """En cours, seules les urgentes partent ; le reste attend la pause."""
    etat = {"pronote_edt": json.dumps(DONNEES)}
    monkeypatch.setattr(db, "etat_valeur", lambda k: etat.get(k))
    pris = []
    monkeypatch.setattr(db, "reserver_notifications", lambda seulement=None: pris.append(seulement) or [])
    nt.vider_file(h(LUNDI, 9))
    nt.vider_file(h(LUNDI, 12, 30))
    assert pris == [nt.URGENTES, None]
    etat["notifications"] = json.dumps({"edt": False})   # réglage coupé : comme avant
    nt.vider_file(h(LUNDI, 9))
    assert pris[-1] is None


def test_rappel_a_la_sortie_des_cours(monkeypatch):
    etat = {"pronote_edt": json.dumps(DONNEES)}
    monkeypatch.setattr(db, "etat_valeur", lambda k: etat.get(k))
    assert nt.moment_rappel(LUNDI) == h(LUNDI, 16, 40)
    etat["notifications"] = json.dumps({"edt": False})
    assert nt.moment_rappel(LUNDI) == h(LUNDI, 18)
