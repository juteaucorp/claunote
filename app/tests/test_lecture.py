import io
import zipfile
from pathlib import Path

import httpx
import pytest

from claunote import agent, lecture, question
from tests.test_site import anonyme, client  # noqa: F401  (fixtures du site)


class Faux:
    """Faux service lecture : réponses par chemin, appels gardés."""
    def __init__(self, reponses):
        self.reponses, self.appels = reponses, []

    def __call__(self, methode, url, timeout=None, **k):
        chemin = url.split("lecture:8000")[-1]
        self.appels.append(chemin)
        r = self.reponses.get(chemin)
        if isinstance(r, Exception):
            raise r
        code, corps = r
        return httpx.Response(code, json=corps)


@pytest.fixture(autouse=True)
def remise_a_zero(monkeypatch):
    monkeypatch.setattr(lecture, "_panne", {"jusqu_a": 0.0})


def test_meilleur_pdf(monkeypatch, tmp_path):
    f = tmp_path / "cours.pdf"
    f.write_bytes(b"%PDF")
    monkeypatch.setattr(httpx, "request", Faux({"/markdown": (200, {"markdown": "| a | b |\n|---|---|\n| 1 | 2 |"})}))
    assert lecture.meilleur_pdf(f, "a b 1 2") .startswith("| a | b |")
    # Docling a perdu du texte : on garde le texte brut
    assert lecture.meilleur_pdf(f, "x" * 1000) == "x" * 1000


def test_service_absent_mis_en_pause(monkeypatch, tmp_path):
    f = tmp_path / "cours.pdf"
    f.write_bytes(b"%PDF")
    faux = Faux({"/markdown": httpx.ConnectError("refusé"), "/classer": (200, {"scores": [1, 2]})})
    monkeypatch.setattr(httpx, "request", faux)
    assert lecture.meilleur_pdf(f, "texte") == "texte"
    assert lecture.classer("q", ["a", "b"]) is None and faux.appels == ["/markdown"]   # plus d'appel pendant la pause
    assert lecture.etat() == {}


def test_formule(monkeypatch):
    monkeypatch.setattr(httpx, "request", Faux({"/formule": (200, {"latex": " \\frac{a}{b} "})}))
    assert lecture.formule(b"png") == "\\frac{a}{b}"
    monkeypatch.setattr(httpx, "request", Faux({"/formule": (422, {"detail": "image illisible"})}))
    with pytest.raises(lecture.LectureIndisponible, match="image illisible"):
        lecture.formule(b"png")


def test_reordonner(monkeypatch):
    passages = [{"texte": "a"}, {"texte": "b"}, {"texte": "c"}]
    monkeypatch.setattr(lecture, "classer", lambda q, t: [0.1, 0.9, 0.5])
    assert [p["texte"] for p in question.reordonner("q", passages, 2)] == ["b", "c"]
    monkeypatch.setattr(lecture, "classer", lambda q, t: None)            # service absent : ordre de la recherche
    assert [p["texte"] for p in question.reordonner("q", passages, 2)] == ["a", "b"]
    assert lecture.classer("q", ["un seul"]) is None


def _docx(chemin: Path):
    xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           '<w:p><w:r><w:t>Les VLAN</w:t></w:r></w:p></w:body></w:document>')
    with zipfile.ZipFile(chemin, "w") as z:
        z.writestr("word/document.xml", xml)


def test_texte_bureautique_sans_docling(monkeypatch, tmp_path):
    f = tmp_path / "cours.docx"
    _docx(f)
    monkeypatch.setattr(lecture, "office", lambda c: None)
    assert agent.texte_bureautique(f) == "Les VLAN"
    monkeypatch.setattr(lecture, "office", lambda c: "## Les VLAN")
    assert agent.texte_bureautique(f) == "## Les VLAN"


def test_office_avec_docling(tmp_path):
    pytest.importorskip("docling")
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("Les VLAN", 1)
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(1, 0).text, t.cell(1, 1).text = "Mode", "Commande", "accès", "switchport"
    d.save(tmp_path / "cours.docx")
    texte = lecture.office(tmp_path / "cours.docx")
    assert "## Les VLAN" in texte and "| Mode" in texte and "switchport" in texte
    assert lecture.office(tmp_path / "cours.pdf") is None


def test_route_formule(client, monkeypatch):
    monkeypatch.setattr(lecture, "formule", lambda image, nom: "x^2")
    r = client.post("/formule", files={"image": ("f.png", io.BytesIO(b"png"), "image/png")})
    assert r.json() == {"latex": "x^2"}

    def indispo(image, nom):
        raise lecture.LectureIndisponible("service lecture injoignable")
    monkeypatch.setattr(lecture, "formule", indispo)
    r = client.post("/formule", files={"image": ("f.png", io.BytesIO(b"png"), "image/png")})
    assert r.status_code == 503 and "injoignable" in r.json()["erreur"]


def test_bouton_formule_si_le_service_sait_faire(client, monkeypatch):
    monkeypatch.setattr(lecture, "etat", lambda: {"formules": True})
    from claunote import db
    monkeypatch.setattr(db, "conversations", lambda n: [])
    monkeypatch.setattr(db, "niveaux_matieres_comptes", lambda: [])
    assert 'id="bouton-formule"' in client.get("/demander").text
    monkeypatch.setattr(lecture, "etat", lambda: {})
    assert 'id="bouton-formule"' not in client.get("/demander").text
