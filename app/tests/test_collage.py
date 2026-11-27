"""Collage depuis Word / Google Docs dans une note (HTML → Markdown), import d'un .docx, et leurs routes."""
import io
import zipfile

from claunote import collage, lecture, main
from tests.test_notes import FaussesNotes  # noqa: F401
from tests.test_site import DOC, anonyme, client  # noqa: F401  (fixtures du site)

# Ce que Word (Windows) met vraiment dans le presse-papiers, en plus court
WORD = """<html xmlns:o="urn:schemas-microsoft-com:office:office"><head><style>p.MsoNormal{margin:0}</style>
<!--[if gte mso 9]><xml><o:OfficeDocumentSettings></o:OfficeDocumentSettings></xml><![endif]--></head><body>
<!--StartFragment--><h1>Les <b>VLAN</b></h1>
<p class=MsoNormal>Un VLAN est un <b><span style='font-size:12pt'>réseau</span></b><b> local</b> <i>virtuel</i>.<o:p>&nbsp;</o:p></p>
<p class=MsoNormal><o:p>&nbsp;</o:p></p>
<p class=MsoListParagraphCxSpFirst style='text-indent:-18.0pt;mso-list:l0 level1 lfo1'><![if !supportLists]><span
style='font-family:Symbol;mso-list:Ignore'>·<span style='font:7.0pt "Times New Roman"'>&nbsp;&nbsp; </span></span><![endif]>Niveau 2</p>
<p class=MsoListParagraphCxSpLast style='margin-left:72pt;mso-list:l0 level2 lfo1'><span style='mso-list:Ignore'>o<span>&nbsp; </span></span>trunk 802.1Q</p>
<p class=MsoListParagraph style='mso-list:l1 level1 lfo2'><span style='mso-list:Ignore'>1.<span>&nbsp; </span></span>Premier</p>
<p class=MsoListParagraph style='mso-list:l1 level1 lfo2'><span style='mso-list:Ignore'>2.<span>&nbsp; </span></span>Second</p>
<table class=MsoTableGrid border=1><tr><td><p><b>Port</b></p></td><td><p><b>Mode</b></p></td></tr>
<tr><td><p>Fa0/1</p></td><td><p>access</p><p>VLAN 10</p></td></tr><tr><td colspan=2>tout | rien</td></tr></table>
<p>Fin avec * étoile et <a href="https://example.org">un lien</a> <a href="javascript:alert(1)">piège</a></p>
<!--EndFragment--></body></html>"""


def test_word_titres_gras_listes_tableau():
    md = collage.html_en_markdown(WORD)
    assert md.startswith("# Les VLAN\n\n") or md.startswith("# Les **VLAN**\n\n")
    assert "Un VLAN est un **réseau local** _virtuel_." in md
    assert "- Niveau 2\n    - trunk 802.1Q" in md                 # sous-liste Word au bon niveau
    assert "1. Premier\n2. Second" in md                         # puces dessinées par Word retirées
    assert "·" not in md and "OfficeDocumentSettings" not in md and "p.MsoNormal" not in md
    assert "| Port " in md and "**Port**" not in md               # en-têtes : déjà en gras
    assert "| access <br> VLAN 10 |" in md                        # deux paragraphes dans une case
    assert "tout \\| rien" in md                                  # | du texte échappé
    assert "\\* étoile" in md and "[un lien](https://example.org)" in md
    assert "javascript" not in md and "[un lien](https://example.org) piège" in md


def test_word_paragraphes_numerotes_un_par_un():
    html = "".join(f"<p style='mso-list:l3 level1 lfo4'><span style='mso-list:Ignore'>1.</span> {x}</p>" for x in "abc")
    assert collage.html_en_markdown(html) == "1. a\n2. b\n3. c"


def test_google_docs_n_est_pas_tout_en_gras():
    gdocs = ('<meta charset="utf-8"><b style="font-weight:normal;" id="docs-internal-guid-1"><p dir="ltr">'
             '<span style="font-weight:400">Texte </span><span style="font-weight:700">gras</span>'
             '<span style="font-style:italic">penché</span></p>'
             '<ul><li><p><span>un</span></p></li><ul><li><p>sous</p></li></ul><li>deux</li></ul></b>')
    assert collage.html_en_markdown(gdocs) == "Texte **gras**_penché_\n\n- un\n    - sous\n- deux"


def test_listes_html_tableau_irregulier_et_code():
    html = ("<ol start='3'><li>trois<ul><li>détail</li></ul></li><li>quatre</li></ol>"
            "<table><tr><th>A</th><th>B</th><th>C</th></tr><tr><td rowspan=2>x</td><td>y</td></tr><tr><td>z</td></tr></table>"
            "<pre>show vlan\n  brief</pre><p><code>ping</code> <span style='font-family:Consolas'>ipconfig</span></p>")
    md = collage.html_en_markdown(html)
    assert "3. trois\n    - détail\n4. quatre" in md
    lignes = [l for l in md.split("\n") if l.startswith("|")]
    assert len(lignes) == 4 and all(l.count("|") == 4 for l in lignes)   # cases manquantes complétées
    assert "```\nshow vlan\n  brief\n```" in md
    assert "`ping ipconfig`" in md                               # police Consolas = code aussi


def test_rien_de_dangereux_et_vide():
    assert collage.html_en_markdown("") == ""
    assert collage.html_en_markdown("<script>alert(1)</script><style>p{}</style><img src=x onerror=y>") == ""
    md = collage.html_en_markdown("<p>#1 priorité</p><p>&lt;b&gt;pas du HTML&lt;/b&gt;</p>")
    assert md.startswith("\\#1 priorité") and "<b>pas du HTML</b>" in md   # texte, neutralisé au rendu


def test_rendu_d_une_case_sur_deux_lignes():
    rendu = main.rendu_markdown("| a | b |\n| - | - |\n| x <br> y | `<br>` |")
    assert "x <br> y" in rendu and "<code>&amp;lt;br&gt;</code>" in rendu
    assert "<script" not in main.rendu_markdown("<script> <br onload=x>")


def test_ecrire_tableau_aligne():
    assert collage.ecrire_tableau([["Port", "Mode"], ["Fa0/1"]]) == (
        "| Port  | Mode |\n| ----- | ---- |\n| Fa0/1 |      |")


def _docx(corps: str) -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("word/document.xml", f"<w:document {w}><w:body>{corps}</w:body></w:document>")
    return b.getvalue()


DOCX = _docx(
    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Routage</w:t></w:r></w:p>'
    '<w:p><w:r><w:t xml:space="preserve">Le </w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>routeur</w:t></w:r>'
    '<w:r><w:t xml:space="preserve"> relie les réseaux.</w:t></w:r></w:p>'
    '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>statique</w:t></w:r></w:p>'
    '<w:p><w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>ip route</w:t></w:r></w:p>'
    '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Réseau</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Passerelle</w:t></w:r></w:p></w:tc></w:tr>'
    '<w:tr><w:tc><w:p><w:r><w:t>10.0.0.0/8</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>192.168.1.1</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')


def test_docx_sans_docling(tmp_path, monkeypatch):
    monkeypatch.setattr(lecture, "office", lambda chemin: None)
    f = tmp_path / "cours.docx"
    f.write_bytes(DOCX)
    md = collage.docx_en_markdown(f)
    assert md.startswith("# Routage\n\nLe **routeur** relie les réseaux.")
    assert "- statique\n    - ip route" in md
    assert "| Réseau     | Passerelle  |" in md and "| 10.0.0.0/8 | 192.168.1.1 |" in md


def test_docx_avec_docling(tmp_path, monkeypatch):
    monkeypatch.setattr(lecture, "office", lambda chemin: "# Titre\n\n<!-- image -->\n\n\n\nTexte")
    f = tmp_path / "x.docx"
    f.write_bytes(DOCX)
    assert collage.docx_en_markdown(f) == "# Titre\n\nTexte"


def test_routes_coller_et_importer(client, monkeypatch):
    FaussesNotes().installer(monkeypatch)
    monkeypatch.setattr(lecture, "office", lambda chemin: None)
    r = client.post("/notes/coller", data={"html": "<h2>Titre</h2><ul><li><b>un</b></li></ul>"})
    assert r.status_code == 200 and r.json() == {"markdown": "## Titre\n\n- **un**"}
    r = client.post("/notes/importer", files={"fichier": ("Mon cours.docx", DOCX)})
    assert r.status_code == 200 and r.json()["titre"] == "Mon cours" and "# Routage" in r.json()["markdown"]
    r = client.post("/notes/importer", files={"fichier": ("vieux.doc", b"x")})
    assert r.status_code == 400 and ".docx" in r.json()["detail"]
    r = client.post("/notes/importer", files={"fichier": ("faux.docx", b"pas un zip")})
    assert r.status_code == 400 and "Word" in r.json()["detail"]


def test_editeur_a_les_outils(client, monkeypatch):
    FaussesNotes().installer(monkeypatch)
    monkeypatch.setattr(main, "_matieres_note", lambda: [])
    r = client.get("/notes/nouvelle")
    assert r.status_code == 200
    for x in ('id="bouton-tableau"', 'id="bouton-word"', 'id="editeur-tableau"', "/notes/coller", "/notes/importer"):
        assert x in r.text
