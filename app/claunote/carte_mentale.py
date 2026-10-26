"""Carte mentale d'une fiche ou d'un cours : l'arbre des titres et des listes du Markdown.

    # Les VLAN                          Les VLAN
    ## Principe                ──►        ├─ Principe ── Segmente un commutateur
    - Segmente un commutateur             └─ Commandes ─ switchport mode trunk
    ## Commandes
    - switchport mode trunk

L'arbre est fait ici (pas de Markdown à analyser dans le navigateur) ; markmap-view (embarqué
dans static/vendor) le dessine. Les formules $…$ restent telles quelles, KaTeX les rend côté navigateur.
"""
import html
import re

TITRE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
PUCE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.+)$")
LONGUEUR = 140          # caractères au plus par branche : une carte mentale se lit d'un coup d'œil
BRANCHES_MAX = 400      # au-delà, la carte devient illisible : on coupe


def _en_ligne(texte: str) -> str:
    """Mise en forme d'une ligne : gras, italique, code ; le reste échappé."""
    texte = texte.strip()
    if len(texte) > LONGUEUR:
        texte = texte[:LONGUEUR - 1].rstrip() + "…"
    texte = html.escape(texte, quote=False)
    texte = re.sub(r"`([^`]+)`", r"<code>\1</code>", texte)
    texte = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", texte)
    return re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<em>\1</em>", texte)


def arbre(markdown: str, titre: str) -> tuple[dict, int]:
    """(racine {content, children}, nombre de branches). Un seul titre de niveau 1 devient la racine."""
    racine = {"content": _en_ligne(titre), "children": []}
    lignes = (markdown or "").splitlines()
    niveaux_1 = [l for l in lignes if re.match(r"^#\s", l)]
    if len(niveaux_1) == 1:
        racine["content"] = _en_ligne(TITRE.match(niveaux_1[0]).group(2))
    titres = [(0, racine)]          # pile des titres ouverts : (niveau, nœud)
    puces: list[tuple[int, dict]] = []  # pile des puces ouvertes sous le titre courant : (retrait, nœud)
    n = 0
    dans_code = False
    for ligne in lignes:
        if ligne.lstrip().startswith("```"):
            dans_code = not dans_code
            continue
        if dans_code or n >= BRANCHES_MAX:
            continue
        if m := TITRE.match(ligne):
            niveau = len(m.group(1))
            if niveau == 1 and len(niveaux_1) == 1:
                continue
            while titres[-1][0] >= niveau:
                titres.pop()
            noeud = {"content": _en_ligne(m.group(2)), "children": []}
            titres[-1][1]["children"].append(noeud)
            titres.append((niveau, noeud))
            puces, n = [], n + 1
        elif m := PUCE.match(ligne):
            retrait = len(m.group(1).replace("\t", "    "))
            while puces and puces[-1][0] >= retrait:
                puces.pop()
            parent = puces[-1][1] if puces else titres[-1][1]
            noeud = {"content": _en_ligne(m.group(2)), "children": []}
            parent["children"].append(noeud)
            puces.append((retrait, noeud))
            n += 1
    return racine, n
