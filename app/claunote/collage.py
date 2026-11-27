"""Collage depuis Word (ou Google Docs, LibreOffice, une page web) dans une note : HTML → Markdown propre.

    Ctrl+V / « Coller » dans l'éditeur de notes
         │  le presse-papiers contient du HTML (titres, listes, gras, tableaux…)
         ▼
    POST /notes/coller ──► html_en_markdown() ──► inséré à la place du curseur
         │                                        # Titre   - puce   **gras**   | tableau |
         └─ HTML sans mise en forme, ou pas de réseau : le texte brut, collé comme avant

Ce que Word met en plus dans le HTML est ignoré : styles, commentaires conditionnels, <o:p>, polices.
Ses listes « à l'ancienne » (paragraphes MsoListParagraph + style mso-list:l0 level2) redeviennent de
vraies listes, imbriquées au bon niveau. Google Docs entoure tout d'un <b style="font-weight:normal"> :
ce n'est pas du gras. Images et formes : laissées de côté (le texte seulement).

Fichier .docx importé : docx_en_markdown() (Docling, comme pour les cours ; sinon le texte, paragraphe
par paragraphe).
"""
import html
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

HTML_MAX = 5_000_000
VIDES = {"br", "hr", "img", "meta", "link", "input", "col", "area", "base", "wbr", "source"}
IGNORES = {"style", "script", "head", "title", "xml", "svg", "object", "iframe", "noscript", "template", "o:p",
           "v:shape", "v:imagedata", "button", "select", "textarea"}
BLOCS = {"p", "div", "section", "article", "header", "footer", "main", "aside", "center", "address", "figure",
         "figcaption", "dd", "dt", "dl", "nav", "form", "fieldset", "body", "html"}
TITRES = {f"h{i}": i for i in range(1, 7)}
# Fermetures implicites du HTML (Word et les navigateurs en laissent)
FERME = {"p": {"p"}, "li": {"li", "p"}, "td": {"td", "th", "p"}, "th": {"td", "th", "p"}, "tr": {"tr", "td", "th", "p"},
         "dt": {"dt", "dd"}, "dd": {"dt", "dd"}}
LIMITES = {"li": {"ul", "ol"}, "td": {"tr", "table"}, "th": {"tr", "table"}, "tr": {"table", "tbody", "thead", "tfoot"},
           "p": {"td", "th", "li", "div", "table", "blockquote"}}
CLASSES_TITRE = {"msotitle": 1, "msosubtitle": 2, "title": 1, "subtitle": 2}
RE_MSO_LISTE = re.compile(r"mso-list\s*:\s*l\d+\s+level(\d+)", re.I)
RE_PUCE_ORDONNEE = re.compile(r"^\s*(\(?[0-9]+|[a-zA-Z]|[ivxlcIVXLC]+)[.)]\s*$")
RE_ESPACES = re.compile(r"[ \t\r\n\f    ​]+")
RE_STYLE_GRAS = re.compile(r"font-weight\s*:\s*(bold|bolder|[6-9]00)", re.I)
RE_STYLE_MAIGRE = re.compile(r"font-weight\s*:\s*(normal|lighter|[1-5]00)", re.I)
RE_STYLE_ITALIQUE = re.compile(r"font-style\s*:\s*italic", re.I)
RE_STYLE_PAS_ITALIQUE = re.compile(r"font-style\s*:\s*normal", re.I)
RE_STYLE_MONO = re.compile(r"font-family\s*:[^;]*\b(courier|consolas|monospace|menlo|monaco|source code)", re.I)
RE_STYLE_CACHE = re.compile(r"display\s*:\s*none|mso-hide\s*:\s*all", re.I)
RE_COMMENTAIRES = re.compile(r"<!--.*?-->|<!\[if[^\]]*\]>|<!\[endif\]>", re.S | re.I)
RE_FRAGMENT = re.compile(r"<!--\s*StartFragment\s*-->(.*)<!--\s*EndFragment\s*-->", re.S | re.I)


class Noeud:
    __slots__ = ("balise", "attrs", "enfants", "parent")

    def __init__(self, balise: str, attrs: dict | None = None, parent=None):
        self.balise, self.attrs, self.enfants, self.parent = balise, attrs or {}, [], parent

    def style(self) -> str:
        return self.attrs.get("style") or ""

    def classes(self) -> set[str]:
        return {c.lower() for c in (self.attrs.get("class") or "").split()}


class _Arbre(HTMLParser):
    """HTML → petit arbre, tolérant (balises non fermées, fermetures en trop)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.racine = Noeud("racine")
        self.pile = [self.racine]

    def _fermer_jusqua(self, i: int):
        del self.pile[i:]

    def handle_starttag(self, balise, attrs):
        balise = balise.lower()
        a_fermer = FERME.get(balise)
        if a_fermer:
            for i in range(len(self.pile) - 1, 0, -1):
                b = self.pile[i].balise
                if b in LIMITES.get(balise, set()):
                    break
                if b in a_fermer:
                    self._fermer_jusqua(i)
                    break
        if balise in ("ul", "ol", "table") and self.pile[-1].balise == "p":   # une liste dans un <p> le ferme
            self.pile.pop()
        n = Noeud(balise, {k.lower(): (v or "") for k, v in attrs}, self.pile[-1])
        self.pile[-1].enfants.append(n)
        if balise not in VIDES:
            self.pile.append(n)

    def handle_startendtag(self, balise, attrs):
        balise = balise.lower()
        n = Noeud(balise, {k.lower(): (v or "") for k, v in attrs}, self.pile[-1])
        self.pile[-1].enfants.append(n)

    def handle_endtag(self, balise):
        balise = balise.lower()
        for i in range(len(self.pile) - 1, 0, -1):
            if self.pile[i].balise == balise:
                self._fermer_jusqua(i)
                return

    def handle_data(self, data):
        if data:
            self.pile[-1].enfants.append(data)


def _arbre(source: str) -> Noeud:
    m = RE_FRAGMENT.search(source)        # Windows : seul le morceau copié, pas l'en-tête du presse-papiers
    source = RE_COMMENTAIRES.sub("", m.group(1) if m else source)
    p = _Arbre()
    p.feed(source)
    p.close()
    return p.racine


# --- Texte en ligne : morceaux (texte + gras/italique/code/lien), regroupés puis écrits -----------------

def _echapper(texte: str) -> str:
    """Le texte de Word tel quel, sans qu'un * ou un ` deviennent de la mise en forme."""
    return texte.replace("\\", "\\\\").replace("*", "\\*").replace("`", "\\`")


def _entourer(texte: str, marque: str) -> str:
    """« ** gras ** » n'est pas du gras en Markdown : les espaces sortent des marques."""
    if not texte.strip():
        return texte
    debut = texte[: len(texte) - len(texte.lstrip())]
    fin = texte[len(texte.rstrip()):]
    return f"{debut}{marque}{texte.strip()}{marque}{fin}"


def _ecrire_morceaux(morceaux: list[tuple[str, dict]]) -> str:
    # Morceaux voisins de même forme fusionnés : « **Les** **VLAN** » → « **Les VLAN** »
    fusion: list[list] = []
    for texte, forme in morceaux:
        if fusion and fusion[-1][1] == forme:
            fusion[-1][0] += texte
        elif fusion and not texte.strip() and texte != "\n":   # un espace seul garde la forme d'avant
            fusion[-1][0] += texte
        else:
            fusion.append([texte, forme])
    sortie, i = [], 0
    while i < len(fusion):
        lien = fusion[i][1].get("lien")
        if lien:
            groupe = []
            while i < len(fusion) and fusion[i][1].get("lien") == lien:
                groupe.append(fusion[i])
                i += 1
            brut = "".join(_forme(t, f) for t, f in groupe)
            dedans, texte_lien = brut.strip(), "".join(t for t, _ in groupe).strip()
            if not dedans:
                sortie.append(brut)
                continue
            avant, apres = brut[: len(brut) - len(brut.lstrip())], brut[len(brut.rstrip()):]   # espaces hors du lien
            sortie.append(avant + (f"<{lien}>" if texte_lien == lien else f"[{dedans}]({lien})") + apres)
        else:
            sortie.append(_forme(*fusion[i]))
            i += 1
    return "".join(sortie)


def _forme(texte: str, forme: dict) -> str:
    if texte == "\n":
        return "\n"
    if forme.get("code"):
        t = texte.strip()
        if not t:
            return texte
        bord = "``" if "`" in t else "`"
        return _entourer(texte.replace(t, f"{bord}{t}{bord}", 1), "")
    t = _echapper(texte)
    if forme.get("gras") and forme.get("italique"):
        return _entourer(t, "***")
    if forme.get("gras"):
        return _entourer(t, "**")
    if forme.get("italique"):
        return _entourer(t, "_")
    return t


def _forme_de(n: Noeud, forme: dict) -> dict:
    f = dict(forme)
    style = n.style()
    if n.balise in ("b", "strong") and not RE_STYLE_MAIGRE.search(style):
        f["gras"] = True
    if n.balise in ("i", "em", "cite", "dfn"):
        f["italique"] = True
    if n.balise in ("code", "kbd", "samp", "tt"):
        f["code"] = True
    if RE_STYLE_GRAS.search(style):
        f["gras"] = True
    elif RE_STYLE_MAIGRE.search(style):
        f.pop("gras", None)
    if RE_STYLE_ITALIQUE.search(style):
        f["italique"] = True
    elif RE_STYLE_PAS_ITALIQUE.search(style):
        f.pop("italique", None)
    if RE_STYLE_MONO.search(style) and n.balise in ("span", "font"):
        f["code"] = True
    if n.balise == "a":
        href = (n.attrs.get("href") or "").strip()
        if re.match(r"^(https?://|mailto:)", href, re.I):
            f["lien"] = href.replace(" ", "%20").replace(")", "%29")
    return f


class _Ecrivain:
    """Parcourt l'arbre et produit les blocs Markdown (paragraphes, titres, listes, tableaux, code)."""

    def __init__(self):
        self.blocs: list[str] = []
        self.morceaux: list[tuple[str, dict]] = []
        self.prefixe = ""            # titre « ## » ou puce de liste Word du paragraphe en cours

    # Paragraphe en cours → bloc
    def _texte_courant(self) -> str:
        brut = _ecrire_morceaux(self.morceaux)
        self.morceaux = []
        lignes = [RE_ESPACES.sub(" ", l).strip() for l in brut.split("\n")]
        return "\n".join(l for l in lignes if l)

    def finir(self, prefixe: str | None = None):
        texte = self._texte_courant()
        prefixe = self.prefixe if prefixe is None else prefixe
        self.prefixe = ""
        if not texte:
            return
        if prefixe.startswith("#"):
            texte = texte.replace("\n", " ")
            texte = re.sub(r"^\*\*(.*)\*\*$", r"\1", texte)   # titre déjà en gras dans Word : le # suffit
        if not prefixe and texte.startswith("#"):
            texte = "\\" + texte          # « #1 priorité » écrit dans Word n'est pas un titre
        self.blocs.append(prefixe + texte)

    # En ligne
    def en_ligne(self, n, forme: dict):
        if isinstance(n, str):
            self.morceaux.append((n, forme))
            return
        if n.balise in IGNORES or RE_STYLE_CACHE.search(n.style()):
            return
        if n.balise == "span" and "mso-list:ignore" in n.style().replace(" ", "").lower():
            return                     # la puce dessinée par Word : déjà rendue par « - » ou « 1. »
        if n.balise == "br":
            self.morceaux.append(("\n", {}))
            return
        if n.balise == "img":
            return
        f = _forme_de(n, forme)
        for e in n.enfants:
            self.parcourir(e, f)

    def parcourir(self, n, forme: dict | None = None):
        forme = forme or {}
        if isinstance(n, str):
            self.morceaux.append((n, forme))
            return
        b = n.balise
        if b in IGNORES or RE_STYLE_CACHE.search(n.style()):
            return
        if b in TITRES:
            self.finir()
            self._contenu(n, _sans_gras(forme))
            self.finir("#" * TITRES[b] + " ")
        elif b in ("ul", "ol"):
            self.finir()
            lignes = _liste(n, 0, forme)
            if lignes:
                self.blocs.append("\n".join(lignes))
        elif b == "table":
            self.finir()
            t = _tableau(n)
            if t:
                self.blocs.append(t)
        elif b == "pre":
            self.finir()
            code = _texte_brut(n).strip("\n")
            if code.strip():
                self.blocs.append(f"```\n{code}\n```")
        elif b == "hr":
            self.finir()
            self.blocs.append("---")
        elif b == "blockquote":
            self.finir()
            dedans = _Ecrivain()
            for e in n.enfants:
                dedans.parcourir(e, forme)
            dedans.finir()
            if dedans.blocs:
                self.blocs.append("\n>\n".join("\n".join("> " + l for l in bl.split("\n")) for bl in dedans.blocs))
        elif b == "li":                 # <li> hors d'une liste (HTML bancal) : une puce quand même
            self.finir()
            self._contenu(n, forme)
            self.finir("- ")
        elif b in BLOCS or b in ("tbody", "tr", "td", "th"):
            self.finir()
            niveau_titre = next((v for c, v in CLASSES_TITRE.items() if c in n.classes()), None)
            liste = RE_MSO_LISTE.search(n.style())
            if b == "p" and liste:
                niveau = int(liste.group(1))
                puce = _puce_word(n)
                self._contenu(n, forme)
                self.finir("    " * (niveau - 1) + ("1. " if puce else "- "))
                return
            self._contenu(n, _sans_gras(forme) if niveau_titre else forme)
            self.finir("#" * niveau_titre + " " if niveau_titre else None)
        else:
            self.en_ligne(n, forme)

    def _contenu(self, n: Noeud, forme: dict):
        for e in n.enfants:
            self.parcourir(e, forme)


def _sans_gras(forme: dict) -> dict:
    return {k: v for k, v in forme.items() if k != "gras"}


def _puce_word(p: Noeud) -> bool:
    """Liste Word à l'ancienne : True si la puce dessinée est un numéro (« 1. », « a) »), False pour « • »."""
    def cherche(n):
        for e in n.enfants:
            if isinstance(e, Noeud):
                if "mso-list:ignore" in e.style().replace(" ", "").lower():
                    return _texte_brut(e)
                r = cherche(e)
                if r is not None:
                    return r
        return None
    marque = cherche(p)
    return bool(marque and RE_PUCE_ORDONNEE.match(RE_ESPACES.sub(" ", marque).strip()))


def _texte_brut(n) -> str:
    if isinstance(n, str):
        return n
    if n.balise in IGNORES:
        return ""
    if n.balise == "br":
        return "\n"
    return "".join(_texte_brut(e) for e in n.enfants)


def _liste(n: Noeud, niveau: int, forme: dict) -> list[str]:
    """<ul>/<ol> → lignes « - » / « 1. », sous-listes décalées de 4 espaces (ce qu'attend le rendu Markdown)."""
    ordonnee = n.balise == "ol"
    try:
        numero = int(n.attrs.get("start") or 1)
    except ValueError:
        numero = 1
    lignes, retrait = [], "    " * niveau
    for e in n.enfants:
        if isinstance(e, str):
            continue
        if e.balise in ("ul", "ol"):         # sous-liste mise directement dans la liste (Google Docs, Word en ligne)
            lignes += _liste(e, niveau + 1, forme)
            continue
        if e.balise != "li":
            continue
        ecrivain, sous = _Ecrivain(), []
        for x in e.enfants:
            if isinstance(x, Noeud) and x.balise in ("ul", "ol"):
                ecrivain.finir()
                sous += _liste(x, niveau + 1, forme)
            elif not sous:
                ecrivain.parcourir(x, forme)
        ecrivain.finir()
        texte = " ".join(b.replace("\n", " ") for b in ecrivain.blocs)
        puce = f"{numero}. " if ordonnee else "- "
        numero += 1
        if texte or sous:
            lignes.append(retrait + puce + texte)
        lignes += sous
    return lignes


def _cellule(c: Noeud) -> str:
    """Une case du tableau sur une seule ligne : ses paragraphes et puces séparés par <br>."""
    e = _Ecrivain()
    for x in c.enfants:
        e.parcourir(x)
    e.finir()
    morceaux = []
    for bloc in e.blocs:
        for ligne in bloc.split("\n"):
            ligne = ligne.strip()
            ligne = re.sub(r"^\s*[-*+] ", "• ", ligne)
            ligne = re.sub(r"^#+ ", "", ligne)
            if ligne and ligne != "---":
                morceaux.append(ligne)
    return " <br> ".join(morceaux).replace("|", "\\|")


def _rangees(table: Noeud) -> list[Noeud]:
    rangs = []
    for e in table.enfants:
        if isinstance(e, Noeud):
            if e.balise == "tr":
                rangs.append(e)
            elif e.balise in ("thead", "tbody", "tfoot"):
                rangs += [x for x in e.enfants if isinstance(x, Noeud) and x.balise == "tr"]
    return rangs


def _tableau(table: Noeud) -> str:
    grille: list[list[str]] = []
    report: dict[tuple[int, int], str] = {}        # cases couvertes par un rowspan d'au-dessus
    for r, tr in enumerate(_rangees(table)):
        rang, col = [], 0
        for c in tr.enfants:
            if not isinstance(c, Noeud) or c.balise not in ("td", "th"):
                continue
            while (r, col) in report:
                rang.append(report.pop((r, col)))
                col += 1
            texte = _cellule(c)
            try:
                largeur = max(1, min(int(c.attrs.get("colspan") or 1), 50))
                hauteur = max(1, min(int(c.attrs.get("rowspan") or 1), 200))
            except ValueError:
                largeur = hauteur = 1
            for k in range(largeur):
                rang.append(texte if k == 0 else "")
                for h in range(1, hauteur):
                    report[(r + h, col + k)] = ""
            col += largeur
        while (r, col) in report:
            rang.append(report.pop((r, col)))
            col += 1
        if any(x.strip() for x in rang):
            grille.append(rang)
    if not grille:
        return ""
    largeur = max(len(r) for r in grille)
    if largeur == 1 and len(grille) == 1:       # un « tableau » d'une seule case (cadre Word) : son texte
        return grille[0][0].replace(" <br> ", "\n").replace("\\|", "|")
    grille = [r + [""] * (largeur - len(r)) for r in grille]
    grille[0] = [re.sub(r"^\*\*(.*)\*\*$", r"\1", x) for x in grille[0]]   # en-têtes : déjà en gras
    return ecrire_tableau(grille)


def ecrire_tableau(grille: list[list[str]]) -> str:
    """Grille (1re rangée = en-têtes) → tableau Markdown, colonnes alignées pour rester lisible à l'écrit."""
    largeur = max(len(r) for r in grille)
    grille = [[(x or "").strip() for x in r] + [""] * (largeur - len(r)) for r in grille]
    tailles = [max(3, *(len(r[i]) for r in grille)) for i in range(largeur)]
    tailles = [min(t, 40) for t in tailles]

    def rang(r):
        return "| " + " | ".join(x.ljust(tailles[i]) for i, x in enumerate(r)) + " |"
    lignes = [rang(grille[0]), "| " + " | ".join("-" * t for t in tailles) + " |"]
    lignes += [rang(r) for r in grille[1:]]
    return "\n".join(lignes)


def html_en_markdown(source: str) -> str:
    """HTML du presse-papiers (Word, Google Docs, LibreOffice, page web) → Markdown de note."""
    source = (source or "")[:HTML_MAX]
    if not source.strip():
        return ""
    e = _Ecrivain()
    e.parcourir(_arbre(source))
    e.finir()
    blocs = _coller_listes_word(e.blocs)
    texte = "\n\n".join(blocs)
    return re.sub(r"\n{3,}", "\n\n", texte).strip()


RE_LIGNE_PUCE = re.compile(r"^\s*([-*+]|\d+\.) ")


def _coller_listes_word(blocs: list[str]) -> list[str]:
    """Les paragraphes-puces de Word arrivent un par un : réunis en une seule liste (sans ligne vide entre)."""
    sortie: list[str] = []
    for b in blocs:
        if sortie and RE_LIGNE_PUCE.match(b) and RE_LIGNE_PUCE.match(sortie[-1].split("\n")[-1]) \
                and "\n" not in b and (b.startswith(" ") or _ordonnee(b) == _ordonnee(sortie[-1])):
            sortie[-1] += "\n" + b
        else:
            sortie.append(b)
    return [_renumeroter(b) for b in sortie]


def _ordonnee(bloc: str) -> bool:
    return bool(re.match(r"^\d+\. ", bloc))


def _renumeroter(bloc: str) -> str:
    """« 1. 1. 1. » (paragraphes Word numérotés un par un) → « 1. 2. 3. », par niveau."""
    if "1. " not in bloc:
        return bloc
    compteurs: dict[int, int] = {}
    lignes = []
    for l in bloc.split("\n"):
        m = re.match(r"^(\s*)1\. (.*)$", l)
        retrait = len(l) - len(l.lstrip())
        for k in [k for k in compteurs if k > retrait]:
            del compteurs[k]
        if m:
            compteurs[retrait] = compteurs.get(retrait, 0) + 1
            l = f"{m.group(1)}{compteurs[retrait]}. {m.group(2)}"
        elif RE_LIGNE_PUCE.match(l):
            compteurs.pop(retrait, None)
        lignes.append(l)
    return "\n".join(lignes)


def utile(source: str) -> bool:
    """Le HTML apporte-t-il une mise en forme ? Sinon le texte brut du presse-papiers suffit."""
    return bool(re.search(r"<(h[1-6]|table|ul|ol|li|b|strong|i|em|pre|code|a)\b|font-weight|mso-list|font-style",
                          source or "", re.I))


# --- Fichier .docx ---------------------------------------------------------------------------------------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_en_markdown(chemin: Path) -> str:
    """Un .docx en Markdown de note : Docling (titres, listes, tableaux, équations) ; sinon lecture simple
    du document.xml (titres, gras, puces, tableaux), toujours possible même sans Docling."""
    from claunote import lecture
    texte = lecture.office(chemin)
    if texte and texte.strip():
        return _nettoyer_docling(texte)
    return _docx_simple(chemin)


def _nettoyer_docling(texte: str) -> str:
    texte = texte.replace("<!-- image -->", "")
    return re.sub(r"\n{3,}", "\n\n", texte).strip()


def _docx_simple(chemin: Path) -> str:
    """Lecture directe du .docx (sans Docling) : paragraphes → HTML minimal → même conversion que le collage."""
    try:
        with zipfile.ZipFile(chemin) as z:
            racine = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, OSError) as e:
        raise ValueError("ce fichier n'est pas un document Word (.docx) lisible") from e
    corps = racine.find(f"{W}body")
    if corps is None:
        return ""
    morceaux = []
    for e in corps:
        if e.tag == f"{W}p":
            morceaux.append(_paragraphe_docx(e))
        elif e.tag == f"{W}tbl":
            morceaux.append(_tableau_docx(e))
    return html_en_markdown("".join(morceaux))


def _runs_docx(p) -> str:
    sortie = []
    for r in p.iter(f"{W}r"):
        texte = "".join((t.text or "") if t.tag == f"{W}t" else ("\t" if t.tag == f"{W}tab" else "<br>")
                        for t in r if t.tag in (f"{W}t", f"{W}tab", f"{W}br"))
        if not texte:
            continue
        texte = html.escape(texte).replace("&lt;br&gt;", "<br>")
        rpr = r.find(f"{W}rPr")
        def actif(nom):
            x = rpr.find(f"{W}{nom}") if rpr is not None else None
            return x is not None and x.get(f"{W}val", "true") not in ("0", "false")
        if actif("b"):
            texte = f"<b>{texte}</b>"
        if actif("i"):
            texte = f"<i>{texte}</i>"
        sortie.append(texte)
    return "".join(sortie)


def _paragraphe_docx(p) -> str:
    ppr = p.find(f"{W}pPr")
    style = ""
    niveau_liste = None
    if ppr is not None:
        s = ppr.find(f"{W}pStyle")
        style = (s.get(f"{W}val") or "").lower() if s is not None else ""
        num = ppr.find(f"{W}numPr")
        if num is not None:
            ilvl = num.find(f"{W}ilvl")
            niveau_liste = int(ilvl.get(f"{W}val") or 0) + 1 if ilvl is not None else 1
    contenu = _runs_docx(p)
    m = re.match(r"^(heading|titre)\s*(\d)$", style)
    if m:
        return f"<h{min(int(m.group(2)), 6)}>{contenu}</h{min(int(m.group(2)), 6)}>"
    if style in ("title", "titre"):
        return f"<h1>{contenu}</h1>"
    if niveau_liste or style.startswith(("listparagraph", "paragraphedeliste")):
        return f"<p style='mso-list:l0 level{niveau_liste or 1}'>{contenu}</p>"
    return f"<p>{contenu}</p>"


def _tableau_docx(t) -> str:
    rangs = []
    for tr in t.findall(f"{W}tr"):
        cases = []
        for tc in tr.findall(f"{W}tc"):
            tcpr = tc.find(f"{W}tcPr")
            span = tcpr.find(f"{W}gridSpan") if tcpr is not None else None
            colspan = f" colspan='{span.get(f'{W}val')}'" if span is not None else ""
            cases.append(f"<td{colspan}>" + "".join(_paragraphe_docx(p) for p in tc.findall(f"{W}p")) + "</td>")
        rangs.append("<tr>" + "".join(cases) + "</tr>")
    return "<table>" + "".join(rangs) + "</table>"
