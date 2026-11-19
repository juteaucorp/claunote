import datetime as dt

import pytest

from claunote import modeles

UTC = dt.timezone.utc


@pytest.fixture(autouse=True)
def propre(monkeypatch):
    monkeypatch.delenv("CLAUDE_MODELES", raising=False)
    monkeypatch.delenv("CLAUDE_MODELE_MAX", raising=False)
    modeles._indisponibles.clear()
    yield
    modeles._indisponibles.clear()


def test_un_modele_par_tache_et_escalade():
    assert modeles.choisir("dossiers") == "haiku" and modeles.choisir("scan") == "sonnet"
    assert modeles.choisir("examen") == "opus"
    assert modeles.plus_fort("haiku") == "sonnet" and modeles.plus_fort("opus") is None


def test_reglages_du_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODELES", "examen=sonnet, scan = OPUS,inconnu=opus,fiche=gpt")
    assert modeles.choisir("examen") == "sonnet" and modeles.choisir("scan") == "opus"
    assert modeles.choisir("fiche") == "opus"  # « gpt » ignoré : création de contenu, Opus 5.5
    monkeypatch.setenv("CLAUDE_MODELE_MAX", "sonnet")  # jamais Opus
    assert modeles.choisir("scan") == "sonnet" and modeles.plus_fort("sonnet") is None


def test_modele_indisponible_remplace_par_celui_du_dessous(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODELES", "scan=opus")
    modeles.marquer_indisponible("opus", None, "refusé par le forfait")
    assert modeles.choisir("scan") == "sonnet" and modeles.plus_fort("sonnet") is None
    assert "opus" in modeles.indisponibles()
    modeles.marquer_indisponible("opus", dt.datetime.now(UTC) - dt.timedelta(seconds=1), "limite")
    assert modeles.choisir("scan") == "opus"  # l'heure de reprise est passée


def test_creation_toujours_sur_opus_5_5(monkeypatch):
    monkeypatch.delenv("CLAUDE_REPLI_CREATION", raising=False)
    monkeypatch.delenv("CLAUDE_ID_OPUS", raising=False)
    assert all(modeles.choisir(t) == "opus" and modeles.obligatoire(t) for t in modeles.CREATION)
    assert not modeles.obligatoire("scan") and not modeles.obligatoire("question")
    assert modeles.identifiant("opus") == "claude-opus-5-5" and modeles.identifiant("sonnet") == "sonnet"
    reprise = dt.datetime.now(UTC) + dt.timedelta(hours=2)
    modeles.marquer_indisponible("opus", reprise, "limite")
    assert modeles.choisir("fiche") == "opus" and modeles.attente_opus() == reprise  # elle attend, pas de Sonnet
    monkeypatch.setenv("CLAUDE_REPLI_CREATION", "1")
    assert modeles.choisir("fiche") == "sonnet"
    monkeypatch.delenv("CLAUDE_REPLI_CREATION")
    monkeypatch.setenv("CLAUDE_MODELE_MAX", "sonnet")   # jamais Opus : choix de l'utilisateur
    assert not modeles.obligatoire("fiche") and modeles.choisir("fiche") == "sonnet"


@pytest.mark.parametrize("message,attendu", [
    ("Claude AI usage limit reached|1761235200", dt.datetime(2025, 10, 23, 16, 0, tzinfo=UTC)),
    ("5-hour limit reached ∙ resets 7pm (Europe/Paris)", dt.datetime(2026, 10, 23, 17, 0, tzinfo=UTC)),
    ("You've hit your limit · resets 3:30am (Europe/Paris)", dt.datetime(2026, 10, 24, 1, 30, tzinfo=UTC)),
    ("Weekly limit reached ∙ resets Oct 27, 9am (Europe/Paris)", dt.datetime(2026, 10, 27, 8, 0, tzinfo=UTC)),
    ("Claude usage limit reached. Your limit will reset at 11pm (UTC).", dt.datetime(2026, 10, 23, 23, 0, tzinfo=UTC)),
    ("usage limit reached", None),
])
def test_heure_de_reprise(message, attendu):
    maintenant = dt.datetime(2026, 10, 23, 14, 0, tzinfo=UTC)  # 16 h à Paris
    assert modeles.reprise(message, maintenant) == attendu
