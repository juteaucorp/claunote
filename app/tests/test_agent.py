import json
import subprocess
from datetime import date

import pytest

from claunote import agent
from claunote.agent import ErreurAgent, LimiteAtteinte, parse_reponse

REPONSE = {
    "transcription": "# Suites\n$u_{n+1} = q u_n$", "titre": "Suites géométriques",
    "niveau": "première", "matiere": "Mathématiques", "type": "cours", "chapitre": "Suites",
    "date": "2023-11-04", "mots_cles": ["suite", "raison"], "resume": "Définition.", "confiance": 0.92,
}


def test_parse_normalise_et_tolere_les_balises():
    a = parse_reponse("```json\n" + json.dumps(REPONSE) + "\n```")
    assert (a.niveau, a.type, a.matiere) == ("Première", "Cours", "Mathématiques")
    assert a.date == date(2023, 11, 4)
    assert a.confiance == 0.92


def test_parse_classement_incomplet_baisse_la_confiance():
    a = parse_reponse(json.dumps({**REPONSE, "niveau": "Master", "date": "pas une date"}))
    assert a.niveau is None and a.date is None
    assert a.confiance <= 0.5


def test_parse_sans_json():
    with pytest.raises(ErreurAgent):
        parse_reponse("désolé, je ne peux pas")


def _faux_claude(monkeypatch, code, sortie, stderr=""):
    def run(cmd, **kw):
        assert cmd[:2] == ["claude", "-p"] and "--allowedTools" in cmd
        return subprocess.CompletedProcess(cmd, code, stdout=json.dumps(sortie), stderr=stderr)
    monkeypatch.setattr(agent.subprocess, "run", run)


def test_analyser_ok(monkeypatch, tmp_path):
    _faux_claude(monkeypatch, 0, {"is_error": False, "result": json.dumps(REPONSE)})
    assert agent.analyser(tmp_path / "scan.pdf").titre == "Suites géométriques"


def test_analyser_limite(monkeypatch, tmp_path):
    _faux_claude(monkeypatch, 1, {"is_error": True, "result": "Claude AI usage limit reached|1761235200"})
    with pytest.raises(LimiteAtteinte):
        agent.analyser(tmp_path / "scan.pdf")


def test_analyser_erreur(monkeypatch, tmp_path):
    _faux_claude(monkeypatch, 1, {"is_error": True, "result": "Invalid API key"})
    with pytest.raises(ErreurAgent):
        agent.analyser(tmp_path / "scan.pdf")


def _office(chemin, fichiers):
    import zipfile
    with zipfile.ZipFile(chemin, "w") as z:
        for nom, contenu in fichiers.items():
            z.writestr(nom, contenu)
    return chemin


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def test_texte_office(tmp_path):
    from claunote.agent import texte_office
    docx = _office(tmp_path / "cours.docx", {"word/document.xml":
        f'<w:document {W}><w:body><w:p><w:r><w:t>Les </w:t></w:r><w:r><w:t>VLAN</w:t></w:r></w:p>'
        f'<w:p/><w:p><w:r><w:t>802.1Q</w:t></w:r></w:p></w:body></w:document>'})
    assert texte_office(docx) == "Les VLAN\n802.1Q"
    pptx = _office(tmp_path / "diapo.pptx", {
        "ppt/slides/slide10.xml": f'<p:sld xmlns:p="p" {A}><a:p><a:r><a:t>Fin</a:t></a:r></a:p></p:sld>',
        "ppt/slides/slide2.xml": f'<p:sld xmlns:p="p" {A}><a:p><a:r><a:t>Début</a:t></a:r></a:p></p:sld>'})
    assert texte_office(pptx) == "\n--- slide2 ---\nDébut\n\n--- slide10 ---\nFin"


def test_analyser_office_et_pronote(tmp_path, monkeypatch):
    from claunote import agent
    vus = {}

    def faux_claude(prompt, cwd, outils=None):
        vus["prompt"], vus["fichiers"] = prompt, sorted(p.name for p in cwd.iterdir())
        return ('{"transcription": "x", "titre": "VLAN", "niveau": "BTS2", "matiere": "Réseaux", "type": "Cours", '
                '"chapitre": "", "date": null, "mots_cles": [], "resume": "", "confiance": 0.9}')
    monkeypatch.setattr(agent, "appeler_claude", faux_claude)
    docx = _office(tmp_path / "cours.docx", {"word/document.xml":
        f'<w:document {W}><w:body><w:p><w:r><w:t>Les VLAN</w:t></w:r></w:p></w:body></w:document>'})
    a = agent.analyser(docx, ["BTS SIO 2", "Pronote", "SISR - RESEAUX"])
    assert a.titre == "VLAN"
    assert "`cours.docx.txt`" in vus["prompt"] and "fichier bureautique `cours.docx`" in vus["prompt"]
    assert "déposé sur Pronote par un professeur" in vus["prompt"]
    assert vus["fichiers"] == ["cours.docx", "cours.docx.txt"]
    vide = _office(tmp_path / "vide.docx", {"word/document.xml": f'<w:document {W}><w:body/></w:document>'})
    with pytest.raises(agent.ErreurAgent, match="aucun texte"):
        agent.analyser(vide)
    casse = tmp_path / "casse.docx"
    casse.write_bytes(b"pas un zip")
    with pytest.raises(agent.ErreurAgent, match="illisible"):
        agent.analyser(casse)


def test_hors_cours_sans_matiere_reste_sur():
    a = agent.parse_reponse('{"transcription": "Bulletin", "hors_cours": true, "confiance": 0.9}')
    assert a.hors_cours and a.confiance == 0.9
    assert not agent.parse_reponse('{"transcription": "x", "hors_cours": "oui", "confiance": 0.9}').hors_cours


def test_reclasser_garde_l_annee_choisie(monkeypatch):
    vu = {}

    def faux(prompt, cwd, outils=None, entree=None, timeout=0):
        vu.update(prompt=prompt, entree=entree, outils=outils)
        return '{"niveau": "BTS1", "matiere": "Réseaux", "type": "Cours", "confiance": 0.9}'

    monkeypatch.setattr(agent, "appeler_claude", faux)
    doc = {"nom": "a.pdf", "titre": "VLAN", "niveau": "BTS2", "transcription": "Les VLAN " * 10_000}
    a = agent.reclasser(doc, ["Réseaux", "CEJM"])
    assert (a.niveau, a.matiere, a.type) == ("BTS2", "Réseaux", "Cours") and a.transcription == ""
    assert "BTS2. Garde-la" in vu["prompt"] and "Réseaux, CEJM" in vu["prompt"] and len(vu["entree"]) == 30_000
    assert vu["outils"] is None
    a = agent.reclasser({**doc, "niveau": None}, [])
    assert a.niveau == "BTS1" and "Garde-la" not in vu["prompt"]
    with pytest.raises(agent.ErreurAgent):
        agent.reclasser({**doc, "transcription": ""}, [])
