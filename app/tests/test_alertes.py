import datetime as dt
import json

import pytest

from claunote import alertes, db, notifications as nt, worker

PARIS = alertes.PARIS
VENDREDI = dt.datetime(2026, 11, 20, 18, 0, tzinfo=PARIS)   # vérification un vendredi soir
LUNDI = dt.date(2026, 11, 23)
MARDI = dt.date(2026, 11, 24)


def h(jour, hh, mm=0):
    return dt.datetime(jour.year, jour.month, jour.day, hh, mm, tzinfo=PARIS)


def cours(jour, hh, matiere="SISR - RESEAUX", salle="B12", prof="M. Prof", statut="", annule=False,
          dispense=False, duree=2):
    return {"debut": h(jour, hh).isoformat(), "fin": h(jour, hh + duree).isoformat(), "matiere": matiere,
            "salle": salle, "prof": prof, "statut": statut, "annule": annule, "dispense": dispense}


def releve(*liste, du=dt.date(2026, 11, 20), au=dt.date(2026, 11, 28)):
    return {"du": h(du, 0).isoformat(), "au": h(au, 0).isoformat(), "cours": list(liste)}


SEMAINE = [cours(LUNDI, 8), cours(LUNDI, 10, "MATHEMATIQUES", "A01", "Mme Calcul"),
           cours(MARDI, 8, "CEJM", "C04", "M. Droit"), cours(MARDI, 14, "ANGLAIS", dispense=True, statut="Dispensé")]


def test_rien_ne_change_rien_a_dire():
    assert alertes.changements(releve(*SEMAINE), releve(*SEMAINE), VENDREDI) == []


def test_salle_prof_et_annulation():
    apres = [cours(LUNDI, 8, salle="C10"),
             cours(LUNDI, 10, "MATHEMATIQUES", "A01", "Mme Calcul", statut="Prof. absent", annule=True),
             cours(MARDI, 8, "CEJM", "C04", "Mme Remplaçante", statut="Remplacement"), SEMAINE[3]]
    c = alertes.changements(releve(*SEMAINE), releve(*apres), VENDREDI)
    assert [(x["titre"], x["corps"]) for x in c] == [
        ("Changement de salle : SISR - RESEAUX", "Lun. 23/11 08:00 · salle B12 → C10"),
        ("Cours annulé : MATHEMATIQUES", "Lun. 23/11 10:00 · Mme Calcul · Prof. absent"),
        ("Changement de prof : CEJM", "Mar. 24/11 08:00 · M. Droit → Mme Remplaçante · Remplacement"),
    ]
    assert all(x["cle"].startswith("edt:") for x in c)


def test_aujourd_hui_et_demain():
    avant, apres = releve(cours(LUNDI, 8)), releve(cours(LUNDI, 8, annule=True))
    c = alertes.changements(avant, apres, h(LUNDI, 7))
    assert c[0]["corps"].startswith("Aujourd'hui 08:00")
    assert alertes.changements(avant, apres, h(dt.date(2026, 11, 22), 20))[0]["corps"].startswith("Demain 08:00")


def test_cours_deplace_retire_et_ajoute():
    apres = [cours(LUNDI, 8), cours(MARDI, 16, "MATHEMATIQUES", "A02"),     # maths de lundi 10 h → mardi 16 h
             cours(MARDI, 10, "SISR - CYBER", "D01"), SEMAINE[3]]            # CEJM disparu, cyber en plus
    c = {x["titre"]: x["corps"] for x in alertes.changements(releve(*SEMAINE), releve(*apres), VENDREDI)}
    assert c == {"Cours déplacé : MATHEMATIQUES": "Mar. 24/11 16:00 · au lieu de lun. 23/11 10:00 · salle A02",
                 "Cours retiré : CEJM": "Mar. 24/11 08:00 · n'apparaît plus sur Pronote",
                 "Cours ajouté : SISR - CYBER": "Mar. 24/11 10:00 · salle D01"}


def test_dispense_passe_et_au_dela_de_la_semaine_ignores():
    loin = dt.date(2026, 12, 2)
    avant = releve(cours(MARDI, 14, dispense=True), cours(LUNDI, 8), cours(loin, 8), au=dt.date(2026, 12, 18))
    apres = releve(cours(MARDI, 14, salle="Z", dispense=True), cours(loin, 8, annule=True), au=dt.date(2026, 12, 18))
    # lundi 8 h est passé à ce moment-là, le 2/12 est à plus de 7 jours, mardi 14 h en entreprise
    assert alertes.changements(avant, apres, h(LUNDI, 12)) == []
    # Un cours qui devient « Dispensé » n'est pas « retiré »
    assert alertes.changements(releve(cours(MARDI, 8)), releve(cours(MARDI, 8, dispense=True)), VENDREDI) == []


def test_remplacant_superpose_au_cours_annule():
    """Pronote garde le cours annulé et ajoute le remplaçant au même créneau : on lit le remplaçant."""
    apres = releve(cours(LUNDI, 8, annule=True, statut="Cours annulé"), cours(LUNDI, 8, salle="C10"))
    c = alertes.changements(releve(cours(LUNDI, 8)), apres, VENDREDI)
    assert [x["titre"] for x in c] == ["Changement de salle : SISR - RESEAUX"]


def test_fenetre_commune_seulement():
    """Synchro sur 28 jours puis vérification sur 8 : ce qui est hors des 8 jours n'a pas « disparu »."""
    avant = releve(cours(LUNDI, 8), cours(dt.date(2026, 11, 26), 8), au=dt.date(2026, 12, 18))
    apres = releve(cours(LUNDI, 8), du=dt.date(2026, 11, 20), au=dt.date(2026, 11, 26))
    assert alertes.changements(avant, apres, VENDREDI) == []


@pytest.fixture
def base(monkeypatch):
    b = {"etat": {}, "file": {}, "abos": [{"endpoint": "x"}]}
    monkeypatch.setattr(db, "etat_valeur", lambda k: b["etat"].get(k))
    monkeypatch.setattr(db, "etat_ecrire", lambda k, v: b["etat"].update({k: v}))
    monkeypatch.setattr(db, "abonnements", lambda: b["abos"])
    monkeypatch.setattr(db, "notification_connue", lambda cle: cle in b["file"])

    def notifier(cle, titre, corps="", url="/", statut="attente"):
        if cle in b["file"]:
            return False
        b["file"][cle] = {"titre": titre, "corps": corps, "statut": statut}
        return True
    monkeypatch.setattr(db, "notifier", notifier)
    return b


def envoyees(b):
    return [n["titre"] for n in b["file"].values() if n["statut"] == "attente"]


def test_suivre_cours_premier_releve_puis_changement(base):
    r = releve(*SEMAINE)
    assert alertes.suivre_cours(r["cours"], r["du"], r["au"], VENDREDI) == []   # rien à comparer : on garde
    assert base["file"] == {}
    autre = [cours(LUNDI, 8, salle="C10"), *SEMAINE[1:]]
    alertes.suivre_cours(autre, r["du"], r["au"], VENDREDI)
    assert envoyees(base) == ["Changement de salle : SISR - RESEAUX"]
    # Le relevé suivant, identique : pas de nouvelle alerte
    alertes.suivre_cours(autre, r["du"], r["au"], VENDREDI + dt.timedelta(minutes=15))
    assert len(base["file"]) == 1


def test_suivre_cours_journee_annulee_en_une_notification(base):
    jour = [cours(LUNDI, 8), cours(LUNDI, 10, "MATHEMATIQUES"), cours(LUNDI, 14, "CEJM")]
    r = releve(*jour)
    alertes.suivre_cours(r["cours"], r["du"], r["au"], VENDREDI)
    alertes.suivre_cours([{**c, "annule": True, "statut": "Prof. absent"} for c in jour], r["du"], r["au"], VENDREDI)
    assert envoyees(base) == ["3 cours annulés lun. 23/11"]
    assert next(iter(base["file"].values()))["corps"].startswith("08:00 SISR - RESEAUX : cours annulé")


def test_suivre_cours_releve_vide_ignore(base):
    r = releve(*SEMAINE)
    alertes.suivre_cours(r["cours"], r["du"], r["au"], VENDREDI)
    alertes.suivre_cours([], r["du"], r["au"], VENDREDI)   # Pronote a mal répondu
    assert base["file"] == {}
    assert len(json.loads(base["etat"]["pronote_cours"])["cours"]) == 4


def test_suivre_cours_notifications_pronote_coupees(base):
    base["etat"]["notifications"] = json.dumps({"pronote": False})
    r = releve(*SEMAINE)
    alertes.suivre_cours(r["cours"], r["du"], r["au"], VENDREDI)
    alertes.suivre_cours([cours(LUNDI, 8, salle="C10"), *SEMAINE[1:]], r["du"], r["au"], VENDREDI)
    assert base["file"] == {}


def note(matiere, points, jour="2026-11-20", commentaire="DS", coef=1):
    return {"id": f"g{points}", "matiere": matiere, "date": f"{jour}T00:00:00.000Z",
            "valeur": {"genre": 0, "points": points}, "sur": {"genre": 0, "points": 20},
            "moyenne_classe": {"genre": 0, "points": 11.5}, "coefficient": coef, "commentaire": commentaire}


def test_nouvelles_notes(base):
    deja = {"periode": "S1", "notes": [note("MATHEMATIQUES", 15)]}
    assert alertes.suivre_notes(deja)   # premier passage : notées sans être annoncées
    assert envoyees(base) == []
    # Ids Pronote différents à la connexion suivante : rien de nouveau
    assert alertes.suivre_notes({"periode": "S1", "notes": [{**note("MATHEMATIQUES", 15), "id": "autre"}]}) == []
    alertes.suivre_notes({"periode": "S1", "notes": deja["notes"] + [note("CEJM", 13.5, commentaire="Contrat", coef=2)]})
    assert envoyees(base) == ["Nouvelle note en CEJM"]
    n = next(n for n in base["file"].values() if n["statut"] == "attente")
    assert n["corps"] == "13,5/20 · classe 11,5 · coef 2 · Contrat"


def test_plusieurs_notes_d_un_coup(base):
    alertes.suivre_notes({"periode": "S1", "notes": []})
    alertes.suivre_notes({"periode": "S1", "notes": [note("MATHEMATIQUES", 15), note("ANGLAIS", 12, commentaire="Oral")]})
    assert envoyees(base) == ["2 nouvelles notes"]
    alertes.suivre_notes({"periode": "S1", "notes": [note("MATHEMATIQUES", 15), note("ANGLAIS", 12, commentaire="Oral")]})
    assert envoyees(base) == ["2 nouvelles notes"]   # chacune connue : pas ré-annoncée seule


def test_meme_devoir_deux_notes_distinctes():
    cles = [c for c, _ in alertes.cles_notes([note("MATHEMATIQUES", 15), note("MATHEMATIQUES", 8)])]
    assert len(set(cles)) == 2


def test_changement_de_cours_passe_en_cours():
    assert "edt:" in nt.URGENTES


def test_verification_toutes_les_15_min_et_a_7h12():
    jour = dt.date(2026, 11, 23)
    assert worker.verification_due(h(jour, 10), None)
    assert not worker.verification_due(h(jour, 10, 10), h(jour, 10))
    assert worker.verification_due(h(jour, 10, 15), h(jour, 10))
    assert not worker.verification_due(h(jour, 23), h(jour, 22, 0))           # la nuit : rien
    assert worker.verification_due(h(jour, 7, 12), h(jour, 7, 5))            # les notes de 7 h 10
    assert not worker.verification_due(h(jour, 7, 14), h(jour, 7, 12))
    assert not worker.verification_due(h(jour, 5, 30), h(jour, 1))
