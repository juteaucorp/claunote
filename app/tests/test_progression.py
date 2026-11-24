import datetime as dt

from claunote import progression, revision
from tests.test_site import anonyme, client  # noqa: F401  (fixtures du site)
from claunote import db

AUJ = dt.date(2026, 10, 26)
MAINTENANT = dt.datetime(2026, 10, 26, 18, tzinfo=dt.timezone.utc)


def test_sur_20():
    assert progression.sur_20({"genre": 0, "points": 15}, {"genre": 0, "points": 20}) == 15
    assert progression.sur_20({"genre": 0, "points": 8}, {"genre": 0, "points": 10}) == 16
    assert progression.sur_20({"genre": 1}, None) is None          # absent
    assert progression.sur_20({"genre": 0, "points": 12}, None) == 12


def test_serie():
    j = [AUJ - dt.timedelta(days=i) for i in (1, 2, 3, 5)]
    assert progression.serie(j, AUJ) == 3            # aujourd'hui pas encore fait : la série tient jusqu'à hier
    assert progression.serie([AUJ, *j], AUJ) == 4
    assert progression.serie([AUJ - dt.timedelta(days=2)], AUJ) == 0


def _carte(i, matiere, **k):
    return {"id": i, "fsrs": None, "repetitions": 0, "facilite": 2.5, "intervalle": 0, "prochaine": AUJ,
            "vue_le": None, "niveau": "BTS1", "matiere": matiere, "sujet": "x", **k}


def donnees():
    sue = revision.repetition(_carte(2, "Maths"), 5, MAINTENANT - dt.timedelta(days=60), hasard=False)
    for _ in range(3):
        k = _carte(2, "Maths", fsrs=sue["fsrs"], repetitions=1)
        sue = revision.repetition(k, 5, sue["echeance"], hasard=False)
    return {
        "revues": [{"jour": AUJ, "reponses": 10, "reussies": 8}, {"jour": AUJ - dt.timedelta(days=1), "reponses": 4, "reussies": 2}],
        "revisions": [{"jour": AUJ, "n": 1}],
        "jours_actifs": [AUJ, AUJ - dt.timedelta(days=1)],
        "cartes": [_carte(1, "Réseaux"), _carte(2, "Maths", fsrs=sue["fsrs"], repetitions=4),
                   _carte(3, "Réseaux", repetitions=3, intervalle=30, vue_le=MAINTENANT - dt.timedelta(days=10))],
        "reussite": [{"matiere": "Maths", "reponses": 6, "reussies": 6}, {"matiere": "Réseaux", "reponses": 8, "reussies": 4}],
        "quiz": [{"matiere": "Réseaux", "n": 2, "bonnes": 15, "total": 20}],
        "faites": [{"matiere": "Réseaux", "n": 3}, {"matiere": None, "n": 1}],
        "notes": [{"matiere": "MATHEMATIQUES", "le": None, "valeur": {"genre": 0, "points": p}, "sur": {"genre": 0, "points": 20},
                   "moyenne_classe": {"genre": 0, "points": 11}, "coefficient": 1} for p in (8, 9, 10, 14, 15, 16)]}


def test_calculer():
    p = progression.calculer(donnees(), AUJ, MAINTENANT)
    assert len(p["activite"]) == 30 and p["activite"][-1] == {"jour": AUJ, "reponses": 10, "reussies": 8, "revisions": 1}
    assert (p["reponses"], p["reussite"], p["revisions"], p["serie"], p["max_jour"]) == (14, 71, 1, 2, 10)
    c = p["cartes"]
    assert (c["total"], c["nouvelles"], c["sues"]) == (3, 1, 2) and 0 < c["retenue"] <= 100
    m = {x["matiere"]: x for x in p["matieres"]}
    assert m["Réseaux"]["quiz"] == 75 and m["Réseaux"]["reussite"] == 50 and m["Maths"]["reussite"] == 100
    assert m["Sans matière"]["faites"] == 1 and m["Réseaux"]["cartes"] == 2
    n = p["notes"][0]
    assert n["moyenne"] == 12 and n["classe"] == 11 and n["tendance"] == 6 and n["notes"][-1] == 16


def test_page_progression(client, monkeypatch):
    monkeypatch.setattr(db, "progression_donnees", donnees)
    from claunote import main

    class Fige(dt.datetime):   # la page calcule « aujourd'hui » : figé au jour des données (sinon le test expire)
        @classmethod
        def now(cls, tz=None):
            return MAINTENANT.astimezone(tz)

    monkeypatch.setattr(main.dt, "datetime", Fige)
    r = client.get("/progression")
    assert r.status_code == 200
    assert "2 jours" in r.text and "71 % de bonnes réponses" in r.text and "Réseaux" in r.text
    assert "↗ +6,0" in r.text and "Voir en tableau" in r.text
    assert '/progression' in client.get("/reviser").text


def test_page_progression_vide(client, monkeypatch):
    vide = {k: [] for k in donnees()}
    monkeypatch.setattr(db, "progression_donnees", lambda: vide)
    r = client.get("/progression")
    assert r.status_code == 200 and "Rien depuis 30 jours" in r.text and "Notes Pronote" not in r.text
