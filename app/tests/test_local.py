
import httpx
import pytest

from claunote import local
from claunote.agent import ErreurAgent

TEXTE = """# Les VLAN

Un VLAN est un réseau local virtuel. Le trunk 802.1Q transporte plusieurs VLAN entre commutateurs.
Configuration sur le commutateur : switchport mode trunk, switchport access vlan 10."""


@pytest.fixture(autouse=True)
def sans_ollama(monkeypatch):
    monkeypatch.setattr(local, "etat_ollama", lambda reverifier=False: {"pret": False, "message": "absent"})


def test_transcrire_texte_et_office(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text(TEXTE)
    assert local.transcrire(f) == (TEXTE, "fichier texte")


def test_transcrire_scan_sans_tesseract(tmp_path, monkeypatch):
    monkeypatch.setattr(local.ocr, "disponible", lambda: False)
    f = tmp_path / "scan.jpg"
    f.write_bytes(b"x")
    with pytest.raises(ErreurAgent, match="Tesseract"):
        local.transcrire(f)


def test_transcrire_pdf_scanne_par_ocr_et_arret(tmp_path, monkeypatch):
    monkeypatch.setattr(local.ocr, "disponible", lambda: True)
    monkeypatch.setattr(local.agent, "texte_pdf", lambda chemin: None)
    monkeypatch.setattr(local.ocr, "nb_pages", lambda chemin: 6)
    lues = []
    monkeypatch.setattr(local.ocr, "lire_pdf", lambda chemin, pages: lues.append(pages) or
                        {n: {"texte": f"texte {n}" if n != 2 else " ", "encre": 1} for n in pages})
    texte, origine = local.transcrire(tmp_path / "scan.pdf")
    assert lues == [[1, 2, 3, 4], [5, 6]] and "## Page 1\n\ntexte 1" in texte and "## Page 2" not in texte
    assert "Tesseract" in origine
    with pytest.raises(local.Interrompu):
        local.transcrire(tmp_path / "scan.pdf", arret=lambda: True)


def test_mots_cles_et_titre():
    assert local.mots_cles(TEXTE)[0] == "vlan"
    assert "dans" not in local.mots_cles("dans dans dans réseau")
    assert local._titre(TEXTE, "x.pdf") == "Les VLAN"
    assert local._titre("12 3", "tp_reseau-vlan.pdf") == "tp reseau vlan"


def test_classer_par_voisins(monkeypatch):
    requetes = []
    monkeypatch.setattr(local.db, "voisins", lambda q, n: requetes.append(q) or [
        {"niveau": "BTS1", "matiere": "Réseaux", "type": "Cours", "score": 0.6},
        {"niveau": "BTS1", "matiere": "Réseaux", "type": "TP", "score": 0.3},
        {"niveau": "BTS2", "matiere": "Cybersécurité", "type": "Cours", "score": 0.1}])
    a = local.classer_par_voisins(TEXTE, "tp-vlan.pdf")
    assert requetes[0].startswith("vlan | ")
    assert (a.niveau, a.matiere, a.type, a.titre) == ("BTS1", "Réseaux", "TP", "Les VLAN")  # « tp » dans le nom
    assert 0.65 < a.confiance <= local.CONFIANCE_VOISINS
    monkeypatch.setattr(local.db, "voisins", lambda q, n: [])
    vide = local.classer_par_voisins(TEXTE, "x.pdf")
    assert vide.matiere is None and vide.confiance == 0 and vide.type == "Cours"


def test_analyser_sans_ollama(tmp_path, monkeypatch):
    monkeypatch.setattr(local.db, "voisins", lambda q, n: [{"niveau": "BTS1", "matiere": "Réseaux", "type": "Cours",
                                                            "score": 0.5}] * 2)
    f = tmp_path / "vlan.txt"
    f.write_text(TEXTE)
    a = local.analyser(f)
    assert a.modele == "local" and a.matiere == "Réseaux" and "ressemblance" in a.avis and a.transcription == TEXTE
    f.write_text("   ")
    with pytest.raises(ErreurAgent, match="relire avec Claude"):
        local.analyser(f)


def test_analyser_avec_ollama(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "etat_ollama", lambda reverifier=False: {"pret": True})
    envoye = {}

    def post(url, json, timeout):
        envoye.update(url=url, corps=json)
        reponse = {"titre": "VLAN", "niveau": "BTS1", "matiere": "Réseaux", "type": "Cours", "confiance": 0.99}
        return httpx.Response(200, json={"message": {"content": __import__("json").dumps(reponse)}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(local.httpx, "post", post)
    f = tmp_path / "vlan.txt"
    f.write_text(TEXTE)
    a = local.analyser(f, ["BTS1"], matieres=["Réseaux", "Maths"])
    assert envoye["url"].endswith("/api/chat") and envoye["corps"]["format"] == "json"
    systeme = envoye["corps"]["messages"][0]["content"]
    assert "Réseaux, Maths" in systeme and "« BTS1 »" in systeme
    assert a.matiere == "Réseaux" and a.confiance == local.CONFIANCE_MAX and "Ollama" in a.avis


def test_ollama_en_panne_repli_sur_les_voisins(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "etat_ollama", lambda reverifier=False: {"pret": True})

    def post(url, json, timeout):
        raise httpx.ConnectError("refusé")

    monkeypatch.setattr(local.httpx, "post", post)
    monkeypatch.setattr(local.db, "voisins", lambda q, n: [])
    f = tmp_path / "vlan.txt"
    f.write_text(TEXTE)
    assert "ressemblance" in local.analyser(f).avis


def test_etat_ollama(monkeypatch):
    monkeypatch.undo()
    local._ollama.update(vu=0.0, telechargement=False)
    tires = []
    monkeypatch.setattr(local.httpx, "get", lambda url, timeout: httpx.Response(
        200, json={"models": [{"name": "autre:1b"}]}, request=httpx.Request("GET", url)))
    monkeypatch.setattr(local.threading, "Thread", lambda target, **k: type("T", (), {"start": lambda s: tires.append(1)})())
    e = local.etat_ollama(reverifier=True)
    assert not e["pret"] and "Téléchargement" in e["message"] and tires == [1]
    monkeypatch.setattr(local.httpx, "get", lambda url, timeout: httpx.Response(
        200, json={"models": [{"name": local.OLLAMA_MODELE}]}, request=httpx.Request("GET", url)))
    assert local.etat_ollama(reverifier=True)["pret"]

    def absent(url, timeout):
        raise httpx.ConnectError("non")

    monkeypatch.setattr(local.httpx, "get", absent)
    assert "pas installé" in local.etat_ollama(reverifier=True)["message"]
    local._ollama.update(vu=0.0, telechargement=False, pret=False)
