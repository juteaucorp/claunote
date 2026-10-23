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


def test_parse_cartes():
    texte = json.dumps({"cartes": [{"recto": "VLAN ?", "verso": "Réseau virtuel", "source": "D1"},
                                   {"recto": "", "verso": "x"}, "pas une carte"]})
    assert revision.parse_cartes("Voici :\n" + texte) == [{"recto": "VLAN ?", "verso": "Réseau virtuel", "source": "D1"}]
    with pytest.raises(ErreurAgent):
        revision.parse_cartes('{"cartes": []}')
    assert "### 1. VLAN ?\n\nRéseau virtuel" in revision.cartes_en_markdown([{"recto": "VLAN ?", "verso": "Réseau virtuel"}])
    assert "flashcards" in revision.prompt("cartes", "VLAN") and "20 flashcards" in revision.prompt("cartes", "VLAN")


def test_repetition_espacee():
    j = dt.date(2026, 10, 23)
    f, i, n, le = revision.repetition(2.5, 0, 0, 4, j)
    assert (i, n, le) == (1, 1, j + dt.timedelta(days=1))
    f, i, n, le = revision.repetition(f, i, n, 4, j)
    assert (i, n) == (3, 2)
    f2, i2, n2, _ = revision.repetition(f, i, n, 4, j)
    assert i2 == round(3 * f) and n2 == 3
    # Ratée : on recommence, aujourd'hui même, et elle devient plus difficile
    f3, i3, n3, le3 = revision.repetition(f2, i2, n2, 0, j)
    assert (i3, n3, le3) == (0, 0, j) and f3 < f2
    assert revision.repetition(1.3, 0, 0, 0, j)[0] == 1.3
    assert revision.repetition(2.5, 0, 0, 5, j)[1] > revision.repetition(2.5, 0, 0, 4, j)[1]
    with pytest.raises(ValueError):
        revision.repetition(2.5, 0, 0, 2, j)
