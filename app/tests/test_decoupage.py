import pytest

from claunote import decoupage, importation, ocr
from tests.test_sensible import RIB, _image_texte

PAGES = [
    "Chapitre 1 : Les suites numériques\nUne suite est une fonction de N dans R.",
    "On note u(n) le terme de rang n.\nExemple : u(n) = 2n + 1.",
    None,  # page blanche (intercalaire)
    "TP 3 : configuration des VLAN\nCréer les VLAN 10 et 20 sur le commutateur.",
    "Tester la connectivité entre les postes avec ping.",
    RIB,
    "Devoir surveillé n°2 d'anglais\nTranslate the following text.",
]


@pytest.fixture
def gros_scan(tmp_path):
    chemins = []
    for i, texte in enumerate(PAGES):
        chemin = tmp_path / f"p{i}.png"
        _image_texte(chemin, texte or "")
        chemins.append(chemin)
    pdf = tmp_path / "annee.pdf"
    pdf.write_bytes(importation.fusionner_pdf(chemins))
    return pdf


def page(n, blanche=False, sensible=None, debut=False, titre=None, matiere=None):
    return {"page": n, "blanche": blanche, "sensible": sensible, "debut": debut, "titre": titre, "matiere": matiere,
            "mots": 50}


def test_indices_page():
    assert decoupage.indices_page("Chapitre 2 : Les VLAN\nblabla")["debut"]
    assert decoupage.indices_page("TP n°4 routage")["titre"].startswith("TP n°4")
    assert decoupage.indices_page("Page 1/3\nexercice")["debut"]
    assert not decoupage.indices_page("la suite du cours sur les VLAN")["debut"]


def test_plan_et_coupes():
    perso = {"niveau": "sur", "categorie": "banque", "raisons": ["IBAN valide"]}
    pages = [page(1, debut=True, titre="Chapitre 1"), page(2), page(3, blanche=True), page(4), page(5),
             page(6, sensible=perso), page(7, debut=True, titre="DS 2")]
    locales = decoupage.coupes_locales(pages)
    assert [n for n, c in locales.items() if c["nouveau"]] == [1, 4, 7]
    plan = decoupage.plan(pages, locales)
    assert [d["pages"] for d in plan["documents"]] == [[1, 2], [4, 5], [7]]
    assert plan["blanches"] == [3] and plan["sensibles"]["sur"]["pages"] == [6]
    # Claude dit que la page 5 commence un autre document et que la 7 continue le précédent
    claude = {1: {"nouveau": True, "titre": "Les suites"}, 2: {"nouveau": False}, 4: {"nouveau": True, "titre": "VLAN"},
              5: {"nouveau": True, "titre": "Ping"}, 7: {"nouveau": False}}
    plan = decoupage.plan(pages, claude)
    assert [(d["pages"], d["titre"]) for d in plan["documents"]] == [([1, 2], "Les suites"), ([4], "VLAN"),
                                                                     ([5, 7], "Ping")]


def test_document_trop_long_recoupe(monkeypatch):
    monkeypatch.setattr(decoupage, "PARTIE_MAX", 3)
    pages = [page(n) for n in range(1, 8)]
    plan = decoupage.plan(pages, {1: {"nouveau": True, "titre": "Cours"}})
    assert [d["pages"] for d in plan["documents"]] == [[1, 2, 3], [4, 5, 6], [7]]
    assert all(d["titre"] == "Cours" for d in plan["documents"])


def test_noms_et_plages():
    assert decoupage.plages([3, 4, 5, 9, 11, 12]) == "3-5, 9, 11-12"
    assert decoupage.nom_partie("Année BTS1.pdf", 3, 24, {"titre": "Les VLAN"}) == "annee-bts1-03-les-vlan.pdf"


def test_reponse_claude():
    r = decoupage._lire_reponse('```json\n{"pages": [{"page": 4, "nouveau": true, "titre": "VLAN"}]}\n```')
    assert r == {4: {"nouveau": True, "titre": "VLAN", "matiere": None}}
    with pytest.raises(decoupage.agent.ErreurAgent):
        decoupage._lire_reponse("je ne sais pas")


@pytest.mark.skipif(not ocr.disponible(), reason="Tesseract absent")
def test_lecture_locale_d_un_vrai_scan(gros_scan, tmp_path, monkeypatch):
    monkeypatch.setattr(decoupage, "SEUIL_PAGES", 5)
    assert decoupage.a_decouper(gros_scan)
    pages = decoupage.lire_local(gros_scan, tmp_path / "mini")
    assert [p["page"] for p in pages if p["blanche"]] == [3]
    assert [p["page"] for p in pages if p["sensible"]] == [6]
    assert pages[0]["debut"] and pages[3]["debut"] and pages[6]["debut"] and not pages[1]["debut"]
    assert (tmp_path / "mini" / "page-0004.jpg").exists()
    plan = decoupage.plan(pages, decoupage.coupes_locales(pages))
    assert [d["pages"] for d in plan["documents"]] == [[1, 2], [4, 5], [7]]
    from pypdf import PdfReader
    import io
    assert len(PdfReader(io.BytesIO(decoupage.extraire(gros_scan, [4, 5]))).pages) == 2


def test_claude_ne_voit_ni_pages_blanches_ni_personnelles(tmp_path, monkeypatch):
    from PIL import Image
    for n in (1, 2, 3):
        Image.new("L", (10, 10)).save(tmp_path / f"page-{n:04d}.jpg")
    vu = {}

    def demander(tache, prompt, cwd, lire, outils, timeout):
        vu.update(tache=tache, prompt=prompt, fichiers=sorted(p.name for p in cwd.iterdir()))
        return lire('{"pages": [{"page": 1, "nouveau": true}]}'), "sonnet"
    monkeypatch.setattr(decoupage.agent, "demander", demander)
    pages = [page(1, debut=True, titre="TP 1"), page(2, blanche=True),
             page(3, sensible={"niveau": "doute", "categorie": "sante", "raisons": ["x"]})]
    assert decoupage.demander_coupes(pages, tmp_path, {"page": 0, "titre": "Cours", "matiere": None})[1]["nouveau"]
    assert vu["tache"] == "decoupage" and vu["fichiers"] == ["page-0001.jpg"]
    assert "page 2 : blanche" in vu["prompt"] and "page 3 : document personnel retiré" in vu["prompt"]
    assert "en-tête de début : TP 1" in vu["prompt"] and "« Cours »" in vu["prompt"]
