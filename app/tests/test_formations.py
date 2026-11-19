import json

import pytest

from claunote import formations
from claunote.agent import ErreurAgent

REF = {"formation": "BTS SIO option SISR", "source": "", "resume": "", "matieres": [
    {"nom": "Mathématiques", "officiel": "Mathématiques pour l'informatique", "description": "", "contenus": ["Logique"],
     "epreuve": "E2"},
    {"nom": "Réseaux", "officiel": "Administration des systèmes et des réseaux", "description": "", "contenus": [],
     "epreuve": ""},
    {"nom": "CEJM", "officiel": "Culture économique, juridique et managériale", "description": "", "contenus": [],
     "epreuve": ""}],
    "correspondances": [{"actuelle": "SISR", "cible": "Réseaux", "raison": "option réseaux"}]}


def test_lire_garde_les_noms_existants_et_filtre():
    texte = "Voici :\n" + json.dumps({
        "formation": "BTS SIO SISR", "source": "javascript:alert(1)", "matieres": [
            {"nom": "Mathematiques", "officiel": "Maths info", "contenus": ["a"] * 30},
            {"nom": "Mathématiques"},                      # doublon
            {"nom": ""}, "pas un objet",
            {"nom": "Réseaux"}],
        "correspondances": [{"actuelle": "Maths", "cible": "mathematiques"}, {"actuelle": "Inconnue", "cible": "Réseaux"},
                            {"actuelle": "Sport", "cible": None}]})
    ref = formations.lire(texte, ["Mathématiques", "Maths", "Sport"])
    assert [m["nom"] for m in ref["matieres"]] == ["Mathématiques", "Réseaux"]   # nom existant exact repris
    assert len(ref["matieres"][0]["contenus"]) == 12 and ref["source"] == ""
    assert ref["correspondances"] == [{"actuelle": "Maths", "cible": "Mathématiques", "raison": ""},
                                      {"actuelle": "Sport", "cible": None, "raison": ""}]


@pytest.mark.parametrize("texte", ["pas de json", '{"matieres": []}', '{"autre": 1}'])
def test_lire_refuse(texte):
    with pytest.raises(ErreurAgent):
        formations.lire(texte, [])


def test_ranger_regle_puis_officielle_puis_harmonise():
    refs = {"BTS1": REF}
    regles = {("BTS1", "Archi"): "Systèmes", ("BTS1", "Systèmes"): "Réseaux"}
    assert formations.ranger("BTS1", "Archi", [], regles, refs) == "Réseaux"           # règles enchaînées
    assert formations.ranger("BTS1", "Maths", [], {}, refs) == "Mathématiques"         # évidence → officielle
    assert formations.ranger("BTS1", "SISR", [], {}, refs) == "Réseaux"                # correspondance de l'IA
    assert formations.ranger("BTS1", "Mathématiques pour l'informatique", [], {}, refs) == "Mathématiques"
    assert formations.ranger("BTS2", "Maths", ["Mathématiques"], {}, refs) == "Mathématiques"  # sans référentiel
    assert formations.ranger("BTS1", "Anglais", ["Anglais"], {}, refs) == "Anglais"    # hors référentiel : gardée
    assert formations.ranger("BTS1", None, [], {}, refs) is None
    boucle = {("BTS1", "A"): "B", ("BTS1", "B"): "A"}
    assert formations.ranger("BTS1", "A", [], boucle, {}) in ("A", "B")              # pas de boucle infinie


def test_matiere_depuis_dossier():
    connues = ["Mathématiques", "Réseaux"]
    assert formations.matiere_depuis_dossier(["BTS1", "reseaux", "tp"], "BTS1", connues) == "Réseaux"
    assert formations.matiere_depuis_dossier(["BTS1", "Divers"], "BTS1", connues) is None
    assert formations.matiere_depuis_dossier(["Réseaux"], None, connues) is None


def test_texte_pour_classer():
    t = formations.texte_pour_classer([{"niveau": "BTS1", "nom": "x", "referentiel": REF},
                                       {"niveau": "BTS2", "nom": "y", "referentiel": None}])
    assert "BTS1 : BTS SIO option SISR" in t and "« Mathématiques » (Logique)" in t and "BTS2" not in t
    assert formations.texte_pour_classer([]) == ""


def test_suggestions_reorganisation():
    s = formations.suggestions_reorganisation("BTS1", ["Maths", "SISR", "Réseaux", "Anglais", "Archi"], REF,
                                              {("BTS1", "Archi"): "Réseaux"})
    assert s["Maths"]["cible"] == "Mathématiques" and s["SISR"] == {"cible": "Réseaux", "raison": "option réseaux"}
    assert s["Réseaux"]["cible"] is None and s["Anglais"]["cible"] is None and s["Archi"]["cible"] == "Réseaux"


def test_contexte_dans_le_prompt(monkeypatch):
    from claunote import agent
    vu = {}
    monkeypatch.setattr(agent, "demander", lambda tache, prompt, *a, **k: vu.update(prompt=prompt) or (
        agent.Analyse("", niveau="BTS1", matiere="Réseaux", type="Cours", confiance=0.9), "haiku"))
    with formations.contexte_pour("\nFORMATION-TEST\n"):
        agent.classer("texte", "titre", ["Réseaux"])
    assert "FORMATION-TEST" in vu["prompt"]
    agent.classer("texte", "titre", ["Réseaux"])
    assert "FORMATION-TEST" not in vu["prompt"]


def test_chercher_utilise_le_web(monkeypatch):
    from claunote import agent
    vu = {}

    def demander(tache, prompt, cwd, lire, outils=None, entree=None, timeout=0, modele=None):
        vu.update(tache=tache, outils=outils, entree=entree, prompt=prompt)
        return lire(json.dumps({"matieres": [{"nom": "Réseaux"}]})), "opus"
    monkeypatch.setattr(agent, "demander", demander)
    ref, modele = formations.chercher("BTS1", "BTS SIO option SISR",
                                      [{"niveau": "BTS1", "matiere": "Réseaux", "n": 3}], {("BTS1", "Réseaux"): ["VLAN"]})
    assert modele == "opus" and vu["tache"] == "referentiel" and "WebSearch" in vu["outils"]
    assert "Réseaux (3 doc.) : VLAN" in vu["entree"] and "BTS SIO option SISR" in vu["prompt"]
