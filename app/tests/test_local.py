
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


# --- Local d'abord : Claude aide et vérifie ---------------------------------------------

def test_qualite_ocr():
    assert local.qualite_ocr("Un VLAN est un réseau local virtuel.") > 0.9
    assert local.qualite_ocr("l1 |Ii ;c rn 3# ~ Il ll 1l") < local.OCR_PAUVRE
    assert local.qualite_ocr("") == 0


def test_ocr_pauvre_demande_claude(tmp_path, monkeypatch):
    f = tmp_path / "cahier.jpg"
    f.write_bytes(b"x")
    monkeypatch.setattr(local, "transcrire", lambda chemin, arret=None: ("l1 |Ii ;c rn 3# ~", "OCR d'une photo (Tesseract)"))
    with pytest.raises(local.BesoinClaude):
        local.analyser(f, claude=True)
    monkeypatch.setattr(local.db, "voisins", lambda q, n: [])
    a = local.analyser(f, claude=False)  # sans Claude : gardé, à relire par Claude plus tard
    assert a.a_relire and a.modele == "local"


def _voisins_surs(monkeypatch):
    monkeypatch.setattr(local.db, "voisins", lambda q, n: [{"niveau": "BTS1", "matiere": "Réseaux", "type": "Cours",
                                                            "score": 0.5}] * 3)


def test_doute_verifie_par_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(local.db, "voisins", lambda q, n: [])  # aucun voisin : confiance nulle
    controles, prompts = [], []
    monkeypatch.setattr(local.db, "noter_controle", lambda t, accord, detail="": controles.append(accord))

    def demander(tache, prompt, cwd, lire, entree, timeout):
        prompts.append((tache, entree))
        return lire('{"titre": "Les VLAN", "niveau": "BTS1", "matiere": "Réseaux", "type": "Cours", '
                    '"confiance": 0.9, "accord": false, "remarque": "c\'est un cours de réseaux"}'), "haiku"

    monkeypatch.setattr(local.agent, "demander", demander)
    f = tmp_path / "vlan.txt"
    f.write_text(TEXTE)
    a = local.analyser(f, claude=True, matieres=["Réseaux"])
    assert (a.matiere, a.confiance, a.transcription) == ("Réseaux", 0.9, TEXTE)
    assert "vérifié par Claude Haiku : corrigé" in a.avis and not a.a_relire
    assert prompts[0][0] == "controle" and '"matiere": null' in prompts[0][1] and controles == [False]


def test_sur_de_lui_pas_de_claude_sauf_controle(tmp_path, monkeypatch):
    _voisins_surs(monkeypatch)
    monkeypatch.setattr(local.agent, "demander", lambda *a, **k: pytest.fail("Claude ne doit pas être appelé"))
    monkeypatch.setattr(local.random, "random", lambda: 0.99)
    f = tmp_path / "vlan.txt"
    f.write_text(TEXTE)
    a = local.analyser(f, claude=True)
    assert a.matiere == "Réseaux" and not a.a_relire and "vérifié" not in a.avis
    # 1 fois sur 10 : contrôle par échantillon
    monkeypatch.setattr(local.random, "random", lambda: 0.01)
    monkeypatch.setattr(local.db, "noter_controle", lambda *a, **k: None)
    monkeypatch.setattr(local.agent, "demander", lambda tache, prompt, cwd, lire, entree, timeout: (lire(
        '{"niveau": "BTS1", "matiere": "Réseaux", "type": "Cours", "confiance": 0.95, "accord": true}'), "haiku"))
    assert "confirmé" in local.analyser(f, claude=True).avis


def test_claude_tombe_pendant_la_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(local.db, "voisins", lambda q, n: [])
    from claunote.agent import LimiteAtteinte

    def limite(*a, **k):
        raise LimiteAtteinte("usage limit reached")

    monkeypatch.setattr(local.agent, "demander", limite)
    echecs = []
    f = tmp_path / "vlan.txt"
    f.write_text(TEXTE)
    a = local.analyser(f, claude=True, claude_echec=echecs.append)
    assert a.modele == "local" and a.a_relire and len(echecs) == 1  # le résultat local est gardé


def test_essayer_json(monkeypatch):
    monkeypatch.setattr(local, "disponible", lambda: "modèle local pas installé")
    assert local.essayer_json("s", "t") is None
    monkeypatch.setattr(local, "disponible", lambda: None)
    monkeypatch.setattr(local, "ollama", lambda s, t, timeout=600: '{"ok": 1}')
    assert local.essayer_json("s", "t") == {"ok": 1}
    monkeypatch.setattr(local, "ollama", lambda s, t, timeout=600: "pas du json")
    assert local.essayer_json("s", "t") is None


def test_garder_et_liberer_ollama(monkeypatch):
    envois = []
    monkeypatch.setattr(local.httpx, "post", lambda url, json=None, timeout=None: envois.append((url, json)))
    assert local.liberer() and envois[-1][1] == {"model": local.OLLAMA_MODELE, "keep_alive": 0}
    assert local.GARDER == "5m"
