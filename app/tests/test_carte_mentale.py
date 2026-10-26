from claunote import carte_mentale
from tests.test_site import anonyme, client  # noqa: F401  (fixtures du site)
from claunote import db
from tests.test_site import DOC

FICHE = """# Les VLAN

## Principe
- Segmente un **commutateur**
  - en plusieurs réseaux
- Trunk `802.1Q`

## Formules
- Débit : $D = \\frac{V}{t}$

```
switchport mode trunk
- pas une puce
```

### Sous-partie
Un paragraphe ignoré.
1. Premier
2. Second <script>
"""


def test_arbre():
    racine, n = carte_mentale.arbre(FICHE, "Fiche VLAN")
    assert racine["content"] == "Les VLAN"            # le titre unique de niveau 1 devient la racine
    principe, formules = racine["children"]
    assert principe["content"] == "Principe"
    assert principe["children"][0]["content"] == "Segmente un <strong>commutateur</strong>"
    assert principe["children"][0]["children"][0]["content"] == "en plusieurs réseaux"
    assert principe["children"][1]["content"] == "Trunk <code>802.1Q</code>"
    assert formules["children"][0]["content"] == "Débit : $D = \\frac{V}{t}$"
    sous = formules["children"][1]
    assert sous["content"] == "Sous-partie" and [c["content"] for c in sous["children"]] == ["Premier", "Second &lt;script&gt;"]
    assert n == 9                                      # le bloc de code est ignoré


def test_arbre_sans_titre_et_tronque():
    racine, n = carte_mentale.arbre("- a\n- " + "x" * 300, "Cours")
    assert racine["content"] == "Cours" and n == 2 and racine["children"][1]["content"].endswith("…")
    assert carte_mentale.arbre("Juste du texte.", "Cours")[1] == 0


def test_pages_carte_mentale(client, monkeypatch):
    monkeypatch.setattr(db, "document", lambda i: {**DOC, "transcription": FICHE} if i == 1 else None)
    r = client.get("/doc/1/carte-mentale")
    assert r.status_code == 200 and "markmap-view.js" in r.text and "Segmente un" in r.text
    assert client.get("/doc/2/carte-mentale").status_code == 404
    monkeypatch.setattr(db, "revision", lambda i: {"id": 5, "genre": "fiche", "statut": "pret", "sujet": "VLAN",
                                                   "contenu": "Juste du texte."})
    assert "Pas assez de structure" in client.get("/revision/5/carte-mentale").text
    monkeypatch.setattr(db, "revision", lambda i: {"id": 5, "genre": "quiz", "statut": "pret", "sujet": "VLAN", "contenu": "[]"})
    assert client.get("/revision/5/carte-mentale").status_code == 404
