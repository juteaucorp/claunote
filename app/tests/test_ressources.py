import math

from claunote import ressources


class Horloge:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def budget(occupations, fenetre=900, maximum=0.6):
    """Budget nourri par une suite d'occupations (0 à 1), une mesure toutes les 30 s."""
    h, compteurs = Horloge(), [0, 0]

    def lire():
        return tuple(compteurs)
    b = ressources.Budget(maximum=maximum, fenetre=fenetre, lire=lire, horloge=h)
    b.echantillon()
    for occ in occupations:
        h.t += 30
        compteurs[0] += round(3000 * occ)
        compteurs[1] += 3000
        b.echantillon()
    return b


def test_un_pic_est_tolere():
    b = budget([1.0] * 10 + [0.05] * 20)            # 5 min à 100 %, puis presque rien
    assert not b.freine and b.moyenne < 0.4


def test_a_fond_en_permanence_freine():
    b = budget([1.0] * 40)                          # 20 min à 100 %
    assert b.freine and b.moyenne > 0.6 and "en moyenne sur 15 min" in b.raison()
    # La moyenne doit atteindre 60 % : il faut environ 0,92 × la fenêtre à 100 %
    assert math.isclose(1 - math.exp(-28 * 30 / 900), budget([1.0] * 28).moyenne, rel_tol=0.01)


def test_reprise_dix_points_plus_bas():
    b = budget([1.0] * 40)
    h_avant = b.moyenne
    for occ in [0.3] * 200:
        b._horloge.t += 30
        c = b._lire()
        b._lire = (lambda c: lambda: (c[0] + round(3000 * occ), c[1] + 3000))(c)
        b.echantillon()
        if b.moyenne < 0.5:
            break
        assert b.freine                             # entre 50 et 60 % : on freine encore (pas de va-et-vient)
    assert h_avant > 0.6 and not b.freine


def test_respirer_attend_puis_repart(monkeypatch):
    etats = iter([True, True, False])
    b = ressources.Budget(lire=lambda: None)
    monkeypatch.setattr(ressources, "BUDGET", b)
    monkeypatch.setattr(b, "echantillon", lambda: setattr(b, "freine", next(etats)) or 0.7)
    dodos = []
    monkeypatch.setattr(ressources.time, "sleep", dodos.append)
    ressources.respirer(pas=15)
    assert dodos == [15, 15]
    b.freine, dodos[:] = True, []
    monkeypatch.setattr(b, "echantillon", lambda: 0.9)
    ressources.respirer(arret=lambda: True)          # arrêt demandé : on ne reste pas bloqué
    assert dodos == []


def test_sature_par_le_budget(monkeypatch):
    b = ressources.Budget(lire=lambda: None)
    b.freine, b.moyenne = True, 0.75
    monkeypatch.setattr(ressources, "BUDGET", b)
    assert "75 %" in ressources.sature()
    assert ressources.publier() == {"moyenne": 0.75, "instant": 0.0, "freine": True}
