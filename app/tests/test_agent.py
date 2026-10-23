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
