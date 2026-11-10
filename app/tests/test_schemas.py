import pytest

from claunote import pdf, revision, schemas

DOT = '```dot Le DHCP\ndigraph { rankdir=LR; pc [label="PC"]; pc -> srv [label="1. Discover"] }\n```'
SVG = ('```svg Une trame\n<svg viewBox="0 0 200 100"><script>alert(1)</script><style>p{color:red}</style>'
       '<defs><marker id="f" orient="auto"><path d="M0,0 L8,4 L0,8 z"/></marker></defs>'
       '<path d="M10 50 L150 50" stroke="black" marker-end="url(#f)" onclick="x()"/>'
       '<text x="20" y="20">a &lt; b</text><image href="https://x.test/y.png"/><a href="https://x.test">lien</a>'
       '<rect width="10" height="10" fill="url(https://x.test/z)"/></svg>\n```')
avec_graphviz = pytest.mark.skipif(not schemas.graphviz_present(), reason="Graphviz (dot) n'est pas installé")


def test_svg_nettoye_et_redimensionnable():
    texte, figures = schemas.proteger(f"Avant\n\n{SVG}\n\nAprès")
    assert "SCHEMAJETON0FIN" in texte and "<svg" not in texte and len(figures) == 1
    f = figures[0]
    assert f.startswith('<figure class="schema"><svg') and "<figcaption>Une trame</figcaption>" in f
    for interdit in ("script", "alert", "<style", "onclick", "<image", "x.test", "<a "):
        assert interdit not in f
    assert "a &lt; b" in f and 'width="200"' in f and 'height="100"' in f
    # Les identifiants sont propres au schéma : deux flèches « f » sur une même page ne se mélangent pas
    assert 'id="f"' not in f and "url(#s" in f


def test_svg_sans_viewbox_ou_faux():
    assert 'viewBox="0 0 300 150"' in schemas.nettoyer_svg('<svg width="300" height="150"><rect width="5" height="5"/></svg>')
    assert schemas.nettoyer_svg("<svg><rect/></svg>") is None          # aucune taille
    assert schemas.nettoyer_svg('<svg viewBox="0 0 10 10"><rect>') is None  # XML faux
    assert schemas.nettoyer_svg('<svg viewBox="0 0 10 10"></svg>') is None  # vide
    assert schemas.nettoyer_svg('<!DOCTYPE svg [<!ENTITY a "b">]><svg viewBox="0 0 1 1"><rect/></svg>') is None


@avec_graphviz
def test_dot_dessine_et_code_faux_retire():
    texte, figures = schemas.proteger(f"# Fiche\n\n{DOT}\n\nSuite")
    assert len(figures) == 1 and "Discover" in figures[0] and "<figcaption>Le DHCP</figcaption>" in figures[0]
    faux = "```dot\ndigraph { a -> \n```"
    assert schemas.proteger(faux)[1] == []  # reste un bloc de code à l'affichage
    propre, gardes, retires = schemas.nettoyer(f"Début\n\n{DOT}\n\n{faux}\n\nFin")
    assert (gardes, retires) == (1, 1) and "a -> \n" not in propre and "Discover" in propre and "Fin" in propre


@avec_graphviz
def test_schema_dans_le_site_et_le_pdf(monkeypatch):
    from claunote import main
    contenu = f"## Vue d'ensemble\nVoir [D1].\n\n{DOT}\n\n{SVG}\n\n> **Attention** : piège.\n\n> Citation simple."
    rendu = main.rendu_markdown(contenu)
    assert rendu.count('<figure class="schema">') == 2 and "SCHEMAJETON" not in rendu and "```" not in rendu
    assert '<blockquote class="encadre e-attention">' in rendu and "<blockquote>\n<p>Citation" in rendu
    assert str(pdf.md(contenu)).count("<svg") == 2
    rev = {"sujet": "DHCP", "genre": "fiche", "statut": "pret", "contenu": contenu, "reperes": {}, "sources_web": []}
    assert pdf.revision_pdf(rev, {})[:4] == b"%PDF"


def test_code_ordinaire_intact():
    from claunote import main
    rendu = main.rendu_markdown("```python\nprint('<svg>')\n```\n\n`dot -Tsvg`")
    assert "figure" not in rendu and "print(" in rendu


def test_quiz_melange_et_schema():
    q = ('{"questions": [{"question": "Q", "choix": ["bonne", "b", "c", "d"], "reponse": 0, "explication": "e",'
         ' "schema": "<svg viewBox=\\"0 0 10 10\\"><rect width=\\"5\\" height=\\"5\\"/></svg>"},'
         ' {"question": "R", "choix": ["x", "y", "Les deux"], "reponse": 2, "schema": "<svg>faux"}]}')
    positions = set()
    for _ in range(40):
        a, b = revision.parse_quiz(q, melanger=True)
        assert a["choix"][a["reponse"]] == "bonne" and sorted(a["choix"]) == ["b", "bonne", "c", "d"]
        positions.add(a["reponse"])
        assert b["choix"] == ["x", "y", "Les deux"] and b["reponse"] == 2  # « Les deux » reste en dernier
    assert len(positions) > 1 and "schema" in a and "schema" not in b
    assert revision.parse_quiz(q)[0]["reponse"] == 0  # sans mélange : tel quel
    assert "<svg" in str(schemas.figure(a["schema"]))


def test_prompts_autorisent_les_schemas():
    for genre in ("fiche", "examen", "plan"):
        p = revision.prompt(genre, "VLAN", date_eval=__import__("datetime").date(2030, 1, 2))
        assert "```dot" in p and "```svg" in p and "digraph { rankdir=LR" in p
    assert '"schema"' not in revision.prompt("cartes", "VLAN") and "`schema`" in revision.prompt("quiz", "VLAN")
