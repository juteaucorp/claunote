import pytest

from claunote import matieres
from claunote.agent import ErreurAgent


def c(niveau, matiere, n):
    return {"niveau": niveau, "matiere": matiere, "n": n}


def test_forme():
    assert matieres.forme("Réseaux") == matieres.forme("reseau") == "reseau"
    assert matieres.forme("Mathématiques") == "mathematique"
    assert matieres.forme("Histoire-Géographie") == "histoire geographie"
    assert matieres.forme("Principaux") == "principal"


@pytest.mark.parametrize("a, b, sur", [
    ("Maths", "Mathématiques", True),
    ("Réseau", "Réseaux", True),
    ("Francais", "Français", True),
    ("Anglais LV1", "Anglais", True),
    ("HG", "Histoire-Géographie", True),
    ("CEJM", "Culture économique, juridique et managériale", True),
    ("Developement", "Développement", False),     # faute de frappe
    ("Physique", "Physique-Chimie", False),       # l'un contient l'autre : à confirmer
])
def test_lien(a, b, sur):
    assert matieres.lien(a, b)[0] is sur


@pytest.mark.parametrize("a, b", [("Réseaux", "Systèmes"), ("SVT", "Physique-Chimie"), ("Anglais", "Espagnol"),
                                  ("Réseaux", "Cybersécurité"), ("SISR", "SLAM")])
def test_pas_de_lien(a, b):
    assert matieres.lien(a, b) is None


def test_proposer_par_annee():
    comptes = [c("BTS1", "Maths", 2), c("BTS1", "Mathématiques", 9), c("BTS1", "Réseaux", 5), c("BTS1", "Réseau", 1),
               c("BTS1", "Anglais", 4), c("BTS2", "Maths", 3)]  # BTS2 : seule, rien à fusionner
    groupes = matieres.proposer(comptes)
    assert len(groupes) == 2
    maths = next(g for g in groupes if g["nom"] == "Mathématiques")
    assert maths["niveau"] == "BTS1" and maths["sur"] and {n["nom"] for n in maths["noms"]} == {"Maths", "Mathématiques"}
    assert next(g for g in groupes if g["nom"] == "Réseaux")["noms"] == [{"nom": "Réseau", "n": 1}, {"nom": "Réseaux", "n": 5}]


def test_nom_propose_departage_par_les_autres_annees():
    groupes = matieres.proposer([c("BTS2", "Maths", 3), c("BTS2", "Mathématiques", 3)], {"Mathématiques": 40, "Maths": 3})
    assert groupes[0]["nom"] == "Mathématiques"


def test_groupe_a_confirmer():
    g = matieres.proposer([c("2nde", "Physique", 2), c("2nde", "Physique-Chimie", 6)])[0]
    assert not g["sur"] and g["nom"] == "Physique-Chimie"


def test_harmoniser():
    existantes = ["Mathématiques", "Réseaux", "Physique-Chimie"]
    assert matieres.harmoniser("Maths", existantes) == "Mathématiques"
    assert matieres.harmoniser("Réseau", existantes) == "Réseaux"
    assert matieres.harmoniser("Physique", existantes) == "Physique"  # seulement les évidences
    assert matieres.harmoniser("Anglais", existantes) == "Anglais"
    assert matieres.harmoniser(None, existantes) is None


def test_lire_ia_garde_les_matieres_existantes():
    comptes = [c("BTS1", "SLAM", 3), c("BTS1", "Développement", 7), c("BTS1", "Réseaux", 2)]
    texte = """```json
{"fusions": [{"niveau": "BTS1", "noms": ["SLAM", "Développement"], "nom": "Développement", "raison": "option dev"},
             {"niveau": "BTS1", "noms": ["Réseaux", "Inconnue"], "nom": "Réseaux"},
             {"niveau": "BTS2", "noms": ["SLAM", "Développement"]}]}
```"""
    groupes = matieres.lire_ia(texte, comptes)
    assert len(groupes) == 1
    assert groupes[0]["nom"] == "Développement" and groupes[0]["source"] == "ia"
    assert groupes[0]["noms"] == [{"nom": "SLAM", "n": 3}, {"nom": "Développement", "n": 7}]
    with pytest.raises(ErreurAgent):
        matieres.lire_ia("pas de JSON", comptes)


def test_entree_ia_et_fusion_des_propositions():
    comptes = [c("BTS1", "Maths", 1), c("BTS1", "Mathématiques", 2)]
    texte = matieres.entree_ia(comptes, {("BTS1", "Maths"): ["Suites", "Probabilités"]})
    assert "## BTS1" in texte and "- Maths (1 doc.) : Suites; Probabilités" in texte
    auto = matieres.proposer(comptes)
    deja = {**auto[0], "source": "ia"}
    autre = {"niveau": "BTS1", "noms": [{"nom": "A", "n": 1}, {"nom": "B", "n": 1}], "nom": "A", "source": "ia"}
    assert matieres.fusionner_propositions(auto, [deja, autre]) == auto + [autre]


def test_a_egalite_le_nom_complet():
    assert matieres.proposer([c("BTS1", "Maths", 1), c("BTS1", "Mathématiques", 1)])[0]["nom"] == "Mathématiques"
