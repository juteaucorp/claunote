from datetime import date

from claunote.agent import Analyse
from claunote.rangement import dossier, est_classe, nouveau_nom


def test_est_classe():
    ok = Analyse("t", niveau="BTS1", matiere="Réseaux", type="TP", confiance=0.8)
    assert est_classe(ok)
    assert not est_classe(Analyse("t", niveau="BTS1", matiere="Réseaux", type="TP", confiance=0.6))
    assert not est_classe(Analyse("t", niveau=None, matiere="Réseaux", type="TP", confiance=0.9))


def test_dossier():
    assert dossier("BTS1", "Réseaux", "TP") == ["archives", "BTS1", "Réseaux", "TP"]


def test_nouveau_nom():
    a = Analyse("t", titre="Les VLAN : configuration", date=date(2025, 3, 12))
    assert nouveau_nom(a, "SCAN_0012.PDF") == "2025-03-12_les-vlan-configuration.pdf"
    assert nouveau_nom(Analyse("t"), "scan 3.pdf") == "scan-3.pdf"
