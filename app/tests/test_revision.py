import datetime as dt
import json
from pathlib import Path

import pytest

from claunote import agent, revision
from claunote.agent import ErreurAgent

DOCS = [{"id": 11, "nom": "a.pdf", "titre": "VLAN", "niveau": "BTS1", "matiere": "Réseaux", "type": "Cours",
         "transcription": "Un VLAN segmente un réseau."},
        {"id": 12, "nom": "b.pdf", "titre": None, "niveau": "BTS1", "matiere": "Réseaux", "type": "TP",
         "transcription": "x" * 200}]


def test_contexte_numerote_sans_condenser_quand_tout_tient(monkeypatch):
    monkeypatch.setattr(agent, "appeler_claude", lambda *a, **k: pytest.fail("aucune condensation attendue"))
    texte, reperes, condenses = revision.contexte(DOCS)
    assert reperes == {"D1": 11, "D2": 12} and condenses == 0
    assert texte.startswith("[D1] VLAN (BTS1, Réseaux, Cours)") and "[D2] b.pdf" in texte and "x" * 200 in texte


def test_grouper_coupe_les_cours_trop_longs():
    blocs = ["[D1] A\n" + "a" * 50, "[D2] B\n" + "b" * 50, "[D3] C\n" + "c" * 250]
    groupes = revision.grouper(blocs, taille=130)
    assert all(len(g) <= 130 for g in groupes)
    assert groupes[0].startswith("[D1] A") and "[D2] B" in groupes[0]
    assert "[D3] C (partie 1)" in groupes[1] and "[D3] C (partie 3)" in "".join(groupes)
    assert "".join(groupes).count("c") == 250  # rien de perdu


def test_beaucoup_de_cours_condenses_par_groupes_puis_tous_utilises(monkeypatch):
    monkeypatch.setattr(revision, "BUDGET", 500)
    monkeypatch.setattr(revision, "GROUPE", 450)
    docs = [{"id": 100 + i, "nom": f"c{i}.pdf", "titre": f"Cours {i}", "niveau": "BTS1", "matiere": "Réseaux",
             "type": "Cours", "transcription": "notion " * 40} for i in range(1, 9)]
    appels = []

    def faux(prompt, cwd, outils=None, entree=None, timeout=0, modele="", **k):
        appels.append((k.get("tache"), modele, entree))
        if k.get("tache") == "condense":
            reperes = sorted(set(re.findall(r"\[D\d+\]", entree)), key=lambda r: int(r[2:-1]))
            return "\n".join(f"### {r} résumé" for r in reperes)
        return "## L'essentiel\n" + "Tout le cours [D1] [D8]. " * 5

    import re
    monkeypatch.setattr(agent, "appeler_claude", faux)
    memoire, etapes = revision.Memoire(), []
    contenu, reperes, web = revision.generer("fiche", "VLAN", docs, memoire=memoire, progres=etapes.append)
    condenses = [a for a in appels if a[0] == "condense"]
    assert len(condenses) >= 3 and all(m == "haiku" for _, m, _ in condenses)
    assert len(reperes) == 8 and reperes["D8"] == 108  # aucun cours laissé de côté
    final = appels[-1][2]
    assert all(f"[D{i}]" in final for i in range(1, 9)) and "notion notion" not in final
    assert etapes[0].startswith("cours condensés 1/") and etapes[-1] == "rédaction"
    # Deuxième support sur les mêmes cours (campagne, ou reprise après une limite) : rien n'est recondensé
    appels.clear()
    revision.generer("fiche", "VLAN", docs, memoire=memoire)
    assert [a[0] for a in appels] == ["fiche"]


def test_pieces_jointes_lues_par_l_agent(monkeypatch, tmp_path):
    vu = {}

    def faux(prompt, cwd, outils=None, entree=None, **k):
        vu.update(prompt=prompt, outils=outils, entree=entree, fichiers=sorted(p.name for p in Path(cwd).iterdir()))
        return "## L'essentiel\nRévise lundi [D1] pour l'épreuve de mardi 8 h.\n" + "x" * 80

    import zipfile
    docx = tmp_path / "c.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   '<w:body><w:p><w:r><w:t>Épreuve de maths mardi 8 h</w:t></w:r></w:p></w:body></w:document>')
    monkeypatch.setattr(agent, "appeler_claude", faux)
    pieces = [{"nom": "Emploi du temps.png", "contenu": b"\x89PNG"}, {"nom": "convocation.docx", "contenu": docx.read_bytes()},
              {"nom": "virus.exe", "contenu": b"MZ"}]
    revision.generer("fiche", "VLAN", DOCS, pieces=pieces)
    assert vu["fichiers"] == ["piece1_emploi-du-temps.png", "piece2_convocation.docx", ]
    assert "Read" in vu["outils"] and "`piece1_emploi-du-temps.png`" in vu["prompt"] and "pas des cours" in vu["prompt"]
    assert "Épreuve de maths mardi 8 h" in vu["entree"] and vu["entree"].index("Épreuve") < vu["entree"].index("[D1] VLAN")
    # Sans pièce jointe : pas d'outil Read
    revision.generer("fiche", "VLAN", DOCS)
    assert vu["outils"] == ["WebSearch", "WebFetch"] and "Read" not in vu["prompt"]


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

    def faux(prompt, cwd, outils=None, entree=None, timeout=0, **k):
        vu.update(prompt=prompt, entree=entree, outils=outils)
        return ("## L'essentiel\nUn VLAN… [D1], norme 802.1Q [W1]\n\n## Sources web\n"
                "- [W1] IEEE 802.1Q — https://fr.wikipedia.org/wiki/IEEE_802.1Q\n")

    monkeypatch.setattr(agent, "appeler_claude", faux)
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


def test_repetition_espacee_fsrs():
    t = dt.datetime(2026, 10, 23, 18, tzinfo=dt.timezone.utc)
    neuve = {"id": 1, "facilite": 2.5, "intervalle": 0, "repetitions": 0, "prochaine": t.date(), "vue_le": None}
    # Nouvelle carte : « À revoir » et « Bien » la refont voir dans quelques minutes (étapes d'apprentissage)
    assert revision.delai(neuve, 0, t) == "1 min" and revision.delai(neuve, 4, t) == "10 min"
    assert revision.delai(neuve, 5, t) not in ("1 min", "10 min")       # « Facile » : directement dans des jours
    m = revision.repetition(neuve, 4, t)
    assert m["repetitions"] == 1 and m["retenue"] is None and m["fsrs"]["card_id"] == 1
    # Suivie : l'état FSRS est repris, l'intervalle grandit avec les bonnes réponses
    k = {**neuve, "fsrs": m["fsrs"], "repetitions": 1}
    m2 = revision.repetition(k, 4, m["echeance"], hasard=False)
    k2 = {**k, "fsrs": m2["fsrs"], "repetitions": 2}
    m3 = revision.repetition(k2, 4, m2["echeance"], hasard=False)
    assert m3["intervalle"] > m2["intervalle"] >= 1 and 0 < m3["retenue"] <= 1
    # Ratée : elle revient vite et devient plus difficile
    m4 = revision.repetition({**k2, "fsrs": m3["fsrs"]}, 0, m3["echeance"], hasard=False)
    assert m4["intervalle"] == 0 and m4["difficulte"] > m3["difficulte"] and m4["repetitions"] == 2
    with pytest.raises(ValueError):
        revision.repetition(neuve, 2, t)


def test_carte_sm2_reprise_par_fsrs():
    """Une carte suivie avec l'ancien SM-2 garde son rythme : intervalle de 30 j → elle n'est pas remise à zéro."""
    t = dt.datetime(2026, 10, 23, 18, tzinfo=dt.timezone.utc)
    ancienne = {"id": 2, "facilite": 2.6, "intervalle": 30, "repetitions": 5, "prochaine": t.date(),
                "vue_le": t - dt.timedelta(days=30)}
    c = revision.carte_fsrs(ancienne)
    assert c.state.name == "Review" and c.stability == 30 and 1 <= c.difficulty <= 10
    assert revision.repetition(ancienne, 4, t, hasard=False)["intervalle"] > 30
    assert revision.carte_fsrs({**ancienne, "facilite": 1.3}).difficulty > c.difficulty


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


def test_jours_programme_ancien_format():
    """Un programme sans « support » (ancienne version) s'affiche au lieu de planter l'accueil."""
    from claunote import revision
    jours = revision.jours_programme({"jours": [{"date": "2026-11-02", "taches": [
        {"matiere": "Réseaux", "quoi": "Relire", "duree": 30}]}]}, [])
    assert jours[0]["taches"][0]["support"] == "" and jours[0]["minutes"] == 30 and jours[0]["titre"] == ""
