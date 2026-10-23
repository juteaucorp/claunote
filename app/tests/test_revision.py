import datetime as dt
import json

import pytest

from claunote import revision
from claunote.agent import ErreurAgent

DOCS = [{"id": 11, "nom": "a.pdf", "titre": "VLAN", "niveau": "BTS1", "matiere": "Réseaux", "type": "Cours",
         "transcription": "Un VLAN segmente un réseau."},
        {"id": 12, "nom": "b.pdf", "titre": None, "niveau": "BTS1", "matiere": "Réseaux", "type": "TP",
         "transcription": "x" * 200}]


def test_contexte_numerote_et_respecte_le_budget(monkeypatch):
    monkeypatch.setattr(revision, "BUDGET", 50)
    texte, reperes = revision.contexte(DOCS)
    assert reperes == {"D1": 11, "D2": 12}
    assert texte.startswith("[D1] VLAN (BTS1, Réseaux, Cours)") and "[D2] b.pdf" in texte
    assert texte.split("TP)\n")[1] == "x" * (50 - len("Un VLAN segmente un réseau."))


def test_prompt_plan_demande_une_date():
    with pytest.raises(ValueError):
        revision.prompt("plan", "VLAN")
    p = revision.prompt("plan", "VLAN", dt.date(2026, 10, 30), aujourdhui=dt.date(2026, 10, 23))
    assert "vendredi 30/10/2026" in p and "vendredi 23/10/2026" in p


def test_parse_quiz_ignore_les_questions_invalides():
    texte = json.dumps({"questions": [
        {"question": "Q1", "choix": ["a", "b", "c"], "reponse": 1, "explication": "car", "source": "D1"},
        {"question": "Q2", "choix": ["a"], "reponse": 0},
        {"question": "Q3", "choix": ["a", "b"], "reponse": 5}]})
    qs = revision.parse_quiz("```json\n" + texte + "\n```")
    assert [q["question"] for q in qs] == ["Q1"]
    with pytest.raises(ErreurAgent):
        revision.parse_quiz('{"questions": []}')


def test_generer_passe_les_cours_sur_l_entree(monkeypatch):
    vu = {}

    def faux(prompt, cwd, outils=None, entree=None, timeout=0):
        vu.update(prompt=prompt, entree=entree, outils=outils)
        return "## L'essentiel\nUn VLAN… [D1]"

    monkeypatch.setattr(revision, "appeler_claude", faux)
    contenu, reperes = revision.generer("fiche", "VLAN", DOCS)
    assert contenu.startswith("## L'essentiel") and reperes["D1"] == 11
    assert "[D1] VLAN" in vu["entree"] and "« VLAN »" in vu["prompt"] and vu["outils"] is None
    with pytest.raises(ErreurAgent):
        revision.generer("fiche", "VLAN", [])


def test_nom_fichier_et_quiz_markdown():
    assert revision.nom_fichier("quiz", "Les réseaux : VLAN", dt.date(2026, 10, 23)) == "2026-10-23_quiz_les-reseaux-vlan.md"
    md = revision.quiz_en_markdown([{"question": "Q", "choix": ["a", "b"], "reponse": 1, "explication": "car"}])
    assert "### 1. Q" in md and "- B. b" in md and "**Réponse : B.** car" in md
