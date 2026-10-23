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
        return ("## L'essentiel\nUn VLAN… [D1], norme 802.1Q [W1]\n\n## Sources web\n"
                "- [W1] IEEE 802.1Q — https://fr.wikipedia.org/wiki/IEEE_802.1Q\n")

    monkeypatch.setattr(revision, "appeler_claude", faux)
    contenu, reperes, web = revision.generer("fiche", "VLAN", DOCS)
    assert contenu == "## L'essentiel\nUn VLAN… [D1], norme 802.1Q [W1]" and reperes["D1"] == 11
    assert web == [{"repere": "W1", "titre": "IEEE 802.1Q", "url": "https://fr.wikipedia.org/wiki/IEEE_802.1Q"}]
    assert "[D1] VLAN" in vu["entree"] and "« VLAN »" in vu["prompt"]
    # Le web est permis, l'agent décide ; les cours restent la base
    assert vu["outils"] == ["WebSearch", "WebFetch"] and "Ses cours d'abord" in vu["prompt"]
    assert "## Sources web" in vu["prompt"]
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


def test_consigne_et_epreuve_blanche():
    p = revision.prompt("examen", "Suites", consigne="Insiste sur les suites géométriques\nPas de calculatrice")
    assert "Suites" in p and "## Corrigé" in p and "> Pas de calculatrice" in p and "2 heures" in p
    assert "Consigne" not in revision.prompt("fiche", "VLAN")
    sujet, corrige = revision.separer_corrige("# Sujet\n## Exercice 1\nQ\n\n## Corrigé\nR")
    assert sujet == "# Sujet\n## Exercice 1\nQ" and corrige == "## Corrigé\nR"
    assert revision.separer_corrige("sans corrigé") == ("sans corrigé", "")


def test_parse_programme_garde_la_periode():
    debut, fin = dt.date(2026, 11, 2), dt.date(2026, 11, 4)
    texte = 'Voici : {"conseil": "Courage", "jours": [' \
            '{"date": "2026-11-03", "titre": "CEJM", "taches": [{"matiere": "CEJM", "quoi": "Épreuve", "duree": "240", "support": "examen"}]},' \
            '{"date": "2026-11-02", "titre": "Maths", "taches": [{"matiere": "Maths", "quoi": "Fiche", "duree": 999, "support": "inconnu"}, {"quoi": ""}]},' \
            '{"date": "2026-11-09", "titre": "hors période", "taches": []}, {"date": "pas une date"}]}'
    p = revision.parse_programme(texte, debut, fin)
    assert p["conseil"] == "Courage" and [j["date"] for j in p["jours"]] == ["2026-11-02", "2026-11-03"]
    assert p["jours"][0]["taches"] == [{"matiere": "Maths", "quoi": "Fiche", "duree": 300, "support": ""}]
    assert p["jours"][1]["taches"][0]["support"] == "examen" and p["jours"][1]["taches"][0]["duree"] == 240
    with pytest.raises(revision.ErreurAgent):
        revision.parse_programme('{"jours": [{"date": "2030-01-01"}]}', debut, fin)


def test_jours_programme_relie_les_supports():
    programme = {"jours": [{"date": "2026-11-02", "titre": "", "taches": [
        {"matiere": "Mathématiques", "quoi": "Épreuve", "duree": 120, "support": "examen"},
        {"matiere": "CEJM", "quoi": "Relire", "duree": 30, "support": ""}]}]}
    revs = [{"id": 7, "genre": "examen", "matiere": "Maths"}, {"id": 8, "genre": "examen", "matiere": "Mathématiques"}]
    jours = revision.jours_programme(programme, ["2026-11-02:1"], revs)
    assert jours[0]["libelle"] == "lundi 02/11" and jours[0]["minutes"] == 150
    t0, t1 = jours[0]["taches"]
    assert t0["revision"]["id"] == 8 and not t0["fait"] and t0["cle"] == "2026-11-02:0"
    assert t1["revision"] is None and t1["fait"]


def test_entree_programme():
    e = revision.entree_programme("BTS blanc lundi", [
        {"matiere": "Maths", "niveau": "BTS2", "sujet": "suites", "supports": ["examen", "fiche"], "cours": ["Suites"]}],
        [{"le": dt.datetime(2026, 11, 3, 8), "matiere": "MATHS", "genre": "evaluation", "titre": "DS"}])
    assert "BTS blanc lundi" in e and "Maths (BTS2) sur « suites » : supports préparés : Épreuve blanche, Fiche" in e
    assert "mardi 03/11/2026 : MATHS (evaluation) DS" in e


def test_sources_web_d_un_quiz():
    texte = ('{"questions": [{"question": "Q", "choix": ["a", "b"], "reponse": 0, "source": "W1"}], '
             '"web": [{"repere": "W1", "titre": "ANSSI", "url": "https://cyber.gouv.fr/x"}, '
             '{"repere": "W2", "url": "javascript:alert(1)"}, {"repere": "pas un repère", "url": "https://a.b"}]}')
    assert revision.web_json(texte) == [{"repere": "W1", "titre": "ANSSI", "url": "https://cyber.gouv.fr/x"}]
    assert revision.web_json('{"questions": []}') == [] and revision.web_json("rien") == []
    p = revision.prompt("quiz", "RGPD")
    assert '"web": [{"repere": "W1"' in p and "{{" not in p
    # Pas de section web : le texte reste entier
    assert revision.separer_web("# Fiche\n## Pièges") == ("# Fiche\n## Pièges", [])
