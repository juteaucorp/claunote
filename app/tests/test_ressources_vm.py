"""Économies de ressources : Pronote moins souvent les jours calmes, verdicts d'actualités repris d'un compte à
l'autre, restes du cache des scans effacés."""
import datetime as dt
import json
import os
import time

from claunote import actualites as ac, agent, db, worker
from tests.test_actualites import SUJETS, Base, article
from tests.test_emploi_du_temps import DONNEES, LUNDI, MARDI, creneau, h


def test_verification_toutes_les_heures_un_jour_calme():
    jour = dt.date(2026, 11, 28)
    assert worker.verification_due(h(jour, 10, 15), h(jour, 10))                  # jour normal : 15 min
    assert not worker.verification_due(h(jour, 10, 15), h(jour, 10), calme=True)  # calme : pas encore
    assert not worker.verification_due(h(jour, 10, 59), h(jour, 10), calme=True)
    assert worker.verification_due(h(jour, 11), h(jour, 10), calme=True)
    assert worker.verification_due(h(jour, 7, 12), h(jour, 7, 5), calme=True)     # les notes de 7 h 10, toujours
    assert not worker.verification_due(h(jour, 23), h(jour, 20), calme=True)      # la nuit : rien


def test_jour_calme_selon_l_emploi_du_temps():
    mercredi, jeudi = MARDI + dt.timedelta(days=1), MARDI + dt.timedelta(days=2)
    d = {**DONNEES, "creneaux": DONNEES["creneaux"] + [creneau(mercredi, (8, 0), (12, 0), True)]}
    assert not worker.jour_calme(h(LUNDI, 9), d)          # cours aujourd'hui
    assert worker.jour_calme(h(MARDI, 9), d)              # entreprise mardi et mercredi : calme
    assert worker.jour_calme(h(jeudi, 9), d)              # jeudi et vendredi sans rien : calme
    d2 = {**d, "creneaux": d["creneaux"] + [creneau(jeudi, (8, 0), (10, 0))]}
    assert not worker.jour_calme(h(mercredi, 9), d2)      # cours demain : on surveille les changements
    assert not worker.jour_calme(h(dt.date(2026, 11, 22), 9), d)   # demain hors de l'emploi du temps relevé
    assert not worker.jour_calme(h(LUNDI, 9), {})         # rien de relevé : comme d'habitude


def test_verdict_repris_pour_un_compte_qui_suit_la_meme_veille(monkeypatch):
    b = Base(SUJETS, [article(1, "Faille dans Exchange"), article(2, "Promo casques")])
    b.brancher(monkeypatch)
    appels = []

    def demander(tache, prompt, cwd, entree=None, timeout=0, lire=None, **k):
        ids = [json.loads(l)["id"] for l in entree.splitlines()]
        appels.append(ids)
        return lire(json.dumps([{"id": i, "sujets": ["S1"] if i == 1 else [], "score": 0.9 if i == 1 else 0.05,
                                 "resume": f"résumé {i}", "alerte": i == 1, "evenement": f"info {i}"} for i in ids])), "haiku"
    monkeypatch.setattr(agent, "demander", demander)
    ac.trier(lambda: True)
    premier = list(b.tries)
    # Un autre compte : mêmes sujets (autres numéros dans la base), mêmes articles, plus un nouveau
    autres = [{**s, "id": s["id"] + 100} for s in SUJETS]
    b2 = Base(autres, [article(11, "Faille dans Exchange", cle="ex.fr/1"), article(12, "Promo casques", cle="ex.fr/2"),
                       article(13, "Python 4")])
    b2.brancher(monkeypatch)
    ac.trier(lambda: True)
    assert appels == [[1, 2], [13]]   # seul le nouvel article repart chez Claude
    repris = {t["id"]: t for t in b2.tries}
    assert repris[11] == {**premier[0], "id": 11, "sujets": [107]} and repris[12] == {**premier[1], "id": 12}
    # Sujets différents : consigne différente, rien n'est repris
    b3 = Base([{**SUJETS[0], "description": "Autre chose"}], [article(21, "Faille dans Exchange", cle="ex.fr/1")])
    b3.brancher(monkeypatch)
    ac.trier(lambda: True)
    assert appels[-1] == [21]


def test_verdict_trop_vieux_pas_repris():
    ac._retenir("e", "ex.fr/1", {"sujets": ["S1"], "tri": "ia"}, maintenant=1000.0)
    a = {"id": 5, "cle": "ex.fr/1"}
    assert ac._verdict_repris(a, "e", {"S1": 7}, maintenant=1000.0 + 3600)["sujets"] == [7]
    assert ac._verdict_repris(a, "e", {"S1": 7}, maintenant=1000.0 + ac.VERDICTS_DUREE + 1) is None


def test_memoire_des_verdicts_bornee(monkeypatch):
    monkeypatch.setattr(ac, "VERDICTS_MAX", 10)
    for i in range(25):
        ac._retenir("e", f"ex.fr/{i}", {"sujets": []}, maintenant=1000.0)
    assert len(ac._VERDICTS) <= 10 and ("e", "ex.fr/24") in ac._VERDICTS


def test_initialisation_une_fois_par_compte(monkeypatch):
    vus = []
    monkeypatch.setattr(db, "comptes_actualites", lambda: [1, 2])
    monkeypatch.setattr(db, "etat_consommer", lambda k: False)
    monkeypatch.setattr(db, "etat_valeur", lambda k: str(time.time()))
    monkeypatch.setattr(ac, "initialiser", lambda: vus.append(db.compte_actuel()))
    monkeypatch.setattr(ac, "_tour_compte", lambda claude_ok: None)
    ac.tour(lambda: True)
    ac.tour(lambda: True)
    assert vus == [2]


def vieillir(chemin, jours):
    t = time.time() - jours * 86400
    for f in [chemin, *chemin.rglob("*")] if chemin.is_dir() else [chemin]:
        os.utime(f, (t, t), follow_symlinks=False)


def test_nettoyer_cache_des_scans(tmp_path):
    for nom in ("123", "456", "fusion-9", "decoupage-7", "decoupage-8", "morceaux/1", "morceaux/2"):
        (tmp_path / nom).mkdir(parents=True)
        (tmp_path / nom / "page.pdf").write_bytes(b"x")
    vieillir(tmp_path / "123", 2)            # téléchargement resté après un plantage
    vieillir(tmp_path / "fusion-9", 2)
    vieillir(tmp_path / "decoupage-7", 10)   # gros scan en cours de découpage : gardé
    vieillir(tmp_path / "decoupage-8", 40)
    vieillir(tmp_path / "morceaux/1", 40)    # morceaux transcrits il y a 40 jours : effacés
    vieillir(tmp_path / "morceaux/2", 5)
    assert worker.nettoyer_cache(tmp_path) == 4
    restes = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.name != "page.pdf")
    assert restes == ["456", "decoupage-7", "morceaux", "morceaux/2"]
    assert worker.nettoyer_cache(tmp_path / "absent") == 0
