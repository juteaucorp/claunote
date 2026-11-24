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
    monkeypatch.setattr(db, "etat_valeur", lambda k: etat.get(k))
    edt.enregistrer(None, None, None)
    assert json.loads(etat["pronote_edt"]) == DONNEES
    edt.enregistrer([{"debut": "a", "fin": "b", "matiere": "M", "dispense": True, "statut": "Dispensé"}], "x", "y")
    assert json.loads(etat["pronote_edt"])["creneaux"] == [
        {"debut": "a", "fin": "b", "matiere": "M", "dispense": True, "genre": None, "salle": None, "prof": None,
         "statut": "Dispensé"}]


def test_verification_rapide_garde_les_semaines_suivantes(monkeypatch):
    """La vérification (8 jours) remplace ces jours-là, sans effacer la suite vue par la synchro (28 jours)."""
    etat = {"pronote_edt": json.dumps(DONNEES)}
    monkeypatch.setattr(db, "etat_ecrire", lambda k, v: etat.update({k: v}))
    monkeypatch.setattr(db, "etat_valeur", lambda k: etat.get(k))
    plus_tard = creneau(dt.date(2026, 11, 10), (8, 0), (10, 0))
    etat["pronote_edt"] = json.dumps({**DONNEES, "creneaux": DONNEES["creneaux"] + [plus_tard]})
    edt.enregistrer([creneau(LUNDI, (9, 0), (11, 0))], h(LUNDI, 0).isoformat(), h(dt.date(2026, 11, 3), 0).isoformat())
    d = json.loads(etat["pronote_edt"])
    assert [c["debut"] for c in d["creneaux"]] == [h(LUNDI, 9).isoformat(), plus_tard["debut"]]
    assert edt._date(d["au"]) == h(dt.date(2026, 11, 23), 0)


def test_file_attend_la_fin_du_cours(monkeypatch):
    """En cours, seules les urgentes de l'admin partent ; le reste attend la pause. Les invités ne suivent pas
    l'emploi du temps de l'admin (c'est son Pronote, pas le leur)."""
    etat = {"pronote_edt": json.dumps(DONNEES)}
    monkeypatch.setattr(db, "etat_valeur", lambda k: etat.get(k))
    file = [{"id": 1, "cle": "revision:4", "compte_id": None}, {"id": 2, "cle": "edt:x", "compte_id": None},
            {"id": 3, "cle": "revision:5", "compte_id": 2}]
    monkeypatch.setattr(db, "reserver_notifications", lambda seulement=None: [dict(n) for n in file])
    remis, partis = [], []
    monkeypatch.setattr(db, "remettre_en_attente", remis.extend)
    monkeypatch.setattr(nt, "_vider", lambda f, compte_id: partis.extend((compte_id, n["id"]) for n in f))
    nt.vider_file(h(LUNDI, 9))
    assert remis == [1] and partis == [(None, 2), (2, 3)]
    remis.clear(), partis.clear()
    nt.vider_file(h(LUNDI, 12, 30))
    assert remis == [] and partis == [(None, 1), (None, 2), (2, 3)]
    etat["notifications"] = json.dumps({"edt": False})   # réglage coupé : comme avant
    partis.clear()
    nt.vider_file(h(LUNDI, 9))
    assert len(partis) == 3


def test_silence_de_l_admin_ne_retient_plus_les_invites(monkeypatch):
    etat = {"notifications": json.dumps({"silence_debut": "08:00", "silence_fin": "12:00"})}
    monkeypatch.setattr(db, "etat_valeur", lambda k: etat.get(k))
    monkeypatch.setattr(db, "reserver_notifications", lambda seulement=None: [
        {"id": 1, "cle": "a", "compte_id": None}, {"id": 2, "cle": "b", "compte_id": 2}])
    remis, partis = [], []
    monkeypatch.setattr(db, "remettre_en_attente", remis.extend)
    monkeypatch.setattr(nt, "_vider", lambda f, compte_id: partis.extend(n["id"] for n in f))
    nt.vider_file(h(LUNDI, 9))
    assert remis == [1] and partis == [2]


def test_rappel_a_la_sortie_des_cours(monkeypatch):
    etat = {"pronote_edt": json.dumps(DONNEES)}
    monkeypatch.setattr(db, "etat_valeur", lambda k: etat.get(k))
    assert nt.moment_rappel(LUNDI) == h(LUNDI, 16, 40)
    etat["notifications"] = json.dumps({"edt": False})
    assert nt.moment_rappel(LUNDI) == h(LUNDI, 18)


def test_jours_affiches_entreprise_en_un_bloc():
    j = edt.jours(LUNDI, 7, DONNEES)
    assert [x["date"] for x in j] == [LUNDI, MARDI]
    assert len(j[0]["creneaux"]) == 4 and not j[0]["entreprise"]
    mardi = j[1]["creneaux"]   # matin et après-midi : la pause de midi les sépare
    assert j[1]["entreprise"] and [(c["debut"].hour, c["fin"].hour) for c in mardi] == [(8, 12), (13, 17)]


def test_prochain_jour_au_lycee():
    assert edt.prochain_jour_de_cours(LUNDI, DONNEES) is None          # mardi : entreprise, ensuite rien
    jour, cours = edt.prochain_jour_de_cours(LUNDI - dt.timedelta(days=1), DONNEES)
    assert jour == LUNDI and len(cours) == 4


def _avec_reseaux(monkeypatch, fiche_ouverte=None):
    donnees = json.loads(json.dumps(DONNEES))
    for c in donnees["creneaux"][:2]:
        c["matiere"] = "SISR - RESEAUX"
    etat = {"pronote_edt": json.dumps(donnees)}
    monkeypatch.setattr(db, "etat_valeur", lambda k: etat.get(k))
    monkeypatch.setattr(db, "etat_ecrire", lambda k, v: etat.update({k: v}))
    monkeypatch.setattr(db, "a_revoir_par_matiere", lambda: {
        "Réseaux": {"cartes": 12, "paquet": 7, "fiche": {"id": 3, "sujet": "VLAN", "ouverte_le": fiche_ouverte}},
        "CEJM": {"cartes": 4, "paquet": 9, "fiche": None}})
    return etat


def test_avant_les_cours(monkeypatch):
    _avec_reseaux(monkeypatch)
    a = edt.avant_les_cours(LUNDI - dt.timedelta(days=1))
    assert a["date"] == LUNDI
    assert [(s["matiere"], s["cartes"], s["fiche"]["id"]) for s in a["suggestions"]] == [("Réseaux", 12, 3)]
    assert a["suggestions"][0]["debut"] == h(LUNDI, 8)


def test_fiche_relue_recemment_pas_reproposee(monkeypatch):
    _avec_reseaux(monkeypatch, fiche_ouverte=dt.datetime.now(PARIS) - dt.timedelta(days=1))
    s = edt.avant_les_cours(LUNDI - dt.timedelta(days=1))["suggestions"][0]
    assert s["fiche"] is None and s["cartes"] == 12


def test_notification_avant_le_cours_une_fois_par_semaine(monkeypatch):
    _avec_reseaux(monkeypatch)
    dimanche = LUNDI - dt.timedelta(days=1)
    assert nt.avant_les_cours(dimanche) == (
        "Demain : Réseaux à 08:00", "Avant le cours : 12 flashcards à revoir (~2 min) et ta fiche « VLAN » à relire.",
        "/cartes/session?paquet=7")
    assert nt.avant_les_cours(dimanche) is None                        # déjà proposé cette semaine
    assert nt.avant_les_cours(dimanche + dt.timedelta(days=7)) is None  # plus de cours connus après
