from pathlib import PurePosixPath

import pytest
from PIL import Image
from pypdf import PdfReader

from claunote import agent, importation
from claunote.importation import Fichier, trier


def lot(*chemins):
    """Fichiers d'un lot à partir de « chemin:taille »."""
    fichiers = []
    for i, c in enumerate(chemins):
        chemin, _, taille = c.partition(":")
        p = PurePosixPath(chemin)
        dossier = chemin.endswith("/")
        fichiers.append(Fichier(i, p.name, list(p.parts[:-1]), int(taille or 100_000), dossier=dossier))
    return {f.chemin: f for f in trier(fichiers)}


def test_code_config_et_fichiers_systeme_sont_inutiles():
    t = lot("BTS1/Dev/projet/.vscode/", "BTS1/Dev/projet/index.php", "BTS1/Dev/projet/logo.png",
            "BTS1/Dev/projet/sujet.pdf", "BTS1/.DS_Store", "BTS1/~$cours.docx", "BTS1/archive.zip",
            "BTS1/Dev/notes.txt", "BTS1/Dev/projet/README.md", "BTS1/Maths/icone.png:2000")
    assert t["BTS1/Dev/projet/.vscode"].categorie == "code"
    assert t["BTS1/Dev/projet/index.php"].decision == "inutile"
    assert t["BTS1/Dev/projet/logo.png"].categorie == "projet"     # image d'un projet de code
    assert t["BTS1/Dev/projet/README.md"].categorie == "projet"
    assert t["BTS1/Dev/projet/sujet.pdf"].decision == "analyser"   # le sujet du TP reste un document
    assert t["BTS1/.DS_Store"].categorie == t["BTS1/~$cours.docx"].categorie == "systeme"
    assert t["BTS1/archive.zip"].categorie == "autre"
    assert t["BTS1/Dev/notes.txt"].decision == "analyser"
    assert t["BTS1/Maths/icone.png"].categorie == "systeme"


def test_pdf_et_word_du_meme_document():
    t = lot("BTS1/Réseaux/TP1.pdf", "BTS1/Réseaux/TP1.docx", "BTS1/Réseaux/TP3.pptx", "BTS1/Réseaux/TP3.pdf",
            "BTS1/Réseaux/TP2.pdf", "BTS1/Autre/TP2.docx")
    assert t["BTS1/Réseaux/TP1.docx"].decision == "analyser"      # Word : texte exact, moins cher
    assert t["BTS1/Réseaux/TP1.pdf"].decision == "variante" and t["BTS1/Réseaux/TP1.pdf"].principal == 1
    assert t["BTS1/Réseaux/TP3.pdf"].decision == "analyser"       # PowerPoint : le PDF garde les schémas
    assert t["BTS1/Réseaux/TP3.pptx"].decision == "variante"
    assert t["BTS1/Réseaux/TP2.pdf"].decision == t["BTS1/Autre/TP2.docx"].decision == "analyser"  # autre dossier


def test_copies_exactes():
    t = lot("BTS1/Réseaux/TP/TP1.pdf:300", "BTS1/Copie/TP1.pdf:300", "BTS1/Réseaux/TP/TP1 (1).pdf:300",
            "BTS1/Réseaux/TP/TP1 - Copie.pdf:999")
    assert t["BTS1/Réseaux/TP/TP1.pdf"].decision == "analyser"
    assert t["BTS1/Copie/TP1.pdf"].categorie == "doublon" and t["BTS1/Copie/TP1.pdf"].decision == "inutile"
    assert t["BTS1/Réseaux/TP/TP1 (1).pdf"].categorie == "doublon"
    assert t["BTS1/Réseaux/TP/TP1 - Copie.pdf"].decision == "analyser"  # taille différente : modifié


def test_pages_de_scans():
    t = lot("M/suites/suites 1.jpg", "M/suites/suites 2.jpg", "M/suites/suites 3.jpg",
            "M/IMG_20231004_143012.jpg", "M/IMG_20231004_143100.jpg",
            "Philo/cours p1.pdf", "Philo/cours p2.pdf",
            "R/TP1.pdf", "R/TP2.pdf", "R/TP1.jpg", "R/TP2.jpg",
            "H/scan 1.pdf", "H/scan 2.pdf", "S/vlan 1.jpg", "S/vlan 3.jpg")
    suites = [t[f"M/suites/suites {i}.jpg"] for i in (1, 2, 3)]
    assert all(f.decision == "page" and f.sur for f in suites) and [f.ordre for f in suites] == [0, 1, 2]
    assert len({f.groupe for f in suites}) == 1
    photos = t["M/IMG_20231004_143012.jpg"], t["M/IMG_20231004_143100.jpg"]
    assert all(f.decision == "page" and not f.sur for f in photos)       # appareil photo : à confirmer
    assert t["Philo/cours p1.pdf"].decision == "page" and t["Philo/cours p1.pdf"].sur
    assert t["R/TP1.pdf"].decision == t["R/TP2.pdf"].decision == "analyser"  # deux TP, pas deux pages
    assert t["R/TP1.jpg"].decision == "page" and not t["R/TP1.jpg"].sur
    assert t["H/scan 1.pdf"].decision == "page" and not t["H/scan 1.pdf"].sur
    assert not t["S/vlan 1.jpg"].sur                                      # page 2 manquante


@pytest.mark.parametrize("dossiers,nom,attendu", [
    (["2022-2023 Terminale", "Histoire geo", "DS"], "ds3.pdf",
     {"niveau": "Terminale", "matiere": "Histoire-Géographie", "type": "Évaluation", "annee": "2022-2023"}),
    (["BTS SIO 1", "SISR", "Cours"], "vlan.pdf", {"niveau": "BTS1", "matiere": "Réseaux", "type": "Cours", "annee": None}),
    (["2021_22", "Divers"], "x.pdf", {"niveau": None, "matiere": None, "type": None, "annee": "2021-2022"}),
    (["BTS2", "SLAM"], "TP4 PHP.docx", {"niveau": "BTS2", "matiere": "Développement", "type": "TP", "annee": None}),
])
def test_indices(dossiers, nom, attendu):
    assert importation.indices(dossiers, nom) == attendu


def test_le_dossier_racine_ne_donne_pas_le_type():
    assert importation.indices(["Vieux cours", "2022-2023", "Maths"], "x.pdf")["type"] is None
    assert importation.indices(["TP"], "x.pdf")["type"] == "TP"


def test_texte_indices_complete_avec_l_organisation_comprise():
    t = importation.texte_indices(["2023-2024", "Réseaux"], "a.pdf", {"niveau": "BTS1", "matiere": "Autre"})
    assert "année d'étude probable : BTS1" in t and "matière probable : Réseaux" in t and "2023-2024" in t


def test_decouper():
    assert importation.decouper([0, 1, 2, 3, 4], {2, 4}) == [[0, 1], [2, 3], [4]]
    assert importation.decouper([0, 1], set()) == [[0, 1]]


def test_nom_fusion():
    assert importation.nom_fusion("BTS1/Maths/suites", 3) == "suites-3p.pdf"
    assert importation.nom_fusion("BTS1/Maths/img", 2) == "maths-img-2p.pdf"


def test_fusionner_pdf(tmp_path):
    chemins = []
    for i, couleur in enumerate(("red", "blue")):
        chemins.append(tmp_path / f"{i}.jpg")
        Image.new("RGB", (300, 400), couleur).save(chemins[-1])
    autre = tmp_path / "2.pdf"
    Image.new("RGB", (300, 400), "green").save(autre, "PDF")
    contenu = importation.fusionner_pdf(chemins + [autre])
    (tmp_path / "f.pdf").write_bytes(contenu)
    assert len(PdfReader(tmp_path / "f.pdf").pages) == 3
    assert importation.miniature(chemins[0])[:2] == b"\xff\xd8"


def test_interpreter_arborescence(monkeypatch):
    vu = {}

    def faux(prompt, cwd, outils=None, entree=None, **k):
        vu["entree"] = entree
        return ('{"resume": "Par année scolaire.", "dossiers": {"Cours/2023-2024": {"niveau": "BTS1", "matiere": null,'
                ' "type": null}, "Cours/2023-2024/SISR": {"niveau": "BTS1", "matiere": "Réseaux", "type": "tp"},'
                ' "Inventé": {"niveau": "BTS2"}}}')
    monkeypatch.setattr(agent, "appeler_claude", faux)
    resume, compris = agent.interpreter_arborescence({"Cours/2023-2024": 3, "Cours/2023-2024/SISR": 2}, ["Réseaux"])
    assert resume == "Par année scolaire." and "Cours/2023-2024/SISR (2)" in vu["entree"]
    assert compris == {"Cours/2023-2024": {"niveau": "BTS1", "matiere": None, "type": None},
                       "Cours/2023-2024/SISR": {"niveau": "BTS1", "matiere": "Réseaux", "type": "TP"}}


def test_code_seul_d_un_tp_est_un_document(tmp_path):
    """Un script SQL ou un notebook isolé est un cours ; le même fichier dans un projet de code reste écarté."""
    from claunote import agent
    fichiers = importation.trier([
        importation.Fichier(1, "tp-requetes.sql", ["BTS1", "SQL"], 2_000),
        importation.Fichier(2, "exo.ipynb", ["BTS1", "Python"], 5_000),
        importation.Fichier(3, "index.js", ["BTS1", "Site"], 1_000),
        importation.Fichier(4, "package.json", ["BTS1", "Site"], 500),
        importation.Fichier(5, "enorme.js", ["BTS1"], 2_000_000)])
    decision = {f.nom: (f.categorie, f.decision) for f in fichiers}
    assert decision["tp-requetes.sql"] == ("document", "analyser") and decision["exo.ipynb"] == ("document", "analyser")
    assert decision["index.js"][1] == "inutile" and decision["enorme.js"][1] == "inutile"
    nb = tmp_path / "exo.ipynb"
    nb.write_text('{"cells": [{"cell_type": "markdown", "source": ["# Boucles"]}, '
                  '{"cell_type": "code", "source": ["for i in range(3):\\n", "    print(i)"]}]}')
    assert agent.texte_source(nb) == "# Boucles\n\n```python\nfor i in range(3):\n    print(i)\n```"
    sql = tmp_path / "tp.sql"
    sql.write_text("SELECT * FROM eleve;")
    assert agent.texte_source(sql) == "# tp.sql\n\n```sql\nSELECT * FROM eleve;\n```"
