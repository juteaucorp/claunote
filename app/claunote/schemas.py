"""Schémas dessinés par l'IA dans les fiches, épreuves, quiz et réponses du chat.

L'IA écrit un bloc de code dans son Markdown ; le serveur en fait une image SVG, la même sur le site et
dans le PDF (WeasyPrint n'exécute pas de JavaScript : tout est dessiné ici, rien dans le navigateur).

    ```dot Légende             Graphviz place les boîtes et les flèches tout seul
    digraph { A -> B }   ──►   (organigramme, réseau, arbre, cycle, étapes, états, MCD…)
    ```
                                                            ┌─► site : <figure class="schema"><svg>…
    ```svg Légende             dessin libre, nettoyé   ─────┤
    <svg viewBox=…>…</svg>     (courbe, circuit, frise,     └─► PDF  : le même <svg>
    ```                        géométrie, échange de trames…)

Un schéma illisible (code faux) est retiré du support à la génération (nettoyer) : jamais de code brut
à la place d'un dessin.
"""
import functools
import hashlib
import html
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

from markupsafe import Markup

LANGAGES = {"dot": "dot", "graphviz": "dot", "svg": "svg"}
# Bloc ```dot|svg Légende … ``` ou <svg>…</svg> écrit directement
MOTIF = re.compile(r"^[ \t]*```[ \t]*(?P<langage>dot|graphviz|svg)\b[ \t]*(?P<legende>[^\n]*)\n(?P<code>.*?)^[ \t]*```[ \t]*$"
                   r"|(?P<nu><svg\b[^>]*>.*?</svg>)", re.M | re.S | re.I)
JETON = "SCHEMAJETON{}FIN"
RE_JETON = re.compile(r"(?:<p>\s*)?SCHEMAJETON(\d+)FIN(?:\s*</p>)?")
TAILLE_MAX = 60_000  # caractères d'un schéma : au-delà, ce n'est plus un schéma
LISIBLE = 540        # largeur en dessous de laquelle un schéma large n'est plus rétréci
LARGEUR_MAX = 760    # pixels sur le site ; le PDF le ramène à la largeur de la page

SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG)
ET.register_namespace("xlink", XLINK)
# Dessin seulement : pas de script, de style global, d'image externe, de lien ni de HTML embarqué
BALISES = {"svg", "g", "defs", "title", "desc", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
           "text", "tspan", "marker", "lineargradient", "radialgradient", "stop", "clippath", "use", "symbol",
           "pattern"}
URL_EXTERNE = re.compile(r"url\(\s*['\"]?(?!#)", re.I)

# Allure par défaut des schémas Graphviz (couleurs du site) ; le code de l'IA peut les changer
DOT = ["dot", "-Tsvg", "-Gbgcolor=transparent", "-Gfontname=DejaVu Sans", "-Gfontsize=12", "-Gpad=0.15",
       "-Nfontname=DejaVu Sans", "-Nfontsize=12", "-Nshape=box", "-Nstyle=rounded,filled", "-Nfillcolor=#fdf1ea",
       "-Ncolor=#d97757", "-Npenwidth=1.2", "-Nmargin=0.14,0.08", "-Efontname=DejaVu Sans", "-Efontsize=10",
       "-Ecolor=#6f6b63", "-Earrowsize=0.8"]


def graphviz_present() -> bool:
    return shutil.which("dot") is not None


def _nombre(valeur: str | None) -> float | None:
    m = re.match(r"\s*([0-9.]+)\s*(pt|px)?\s*$", valeur or "")
    if not m:
        return None
    try:
        return float(m.group(1)) * (4 / 3 if m.group(2) == "pt" else 1)
    except ValueError:
        return None


def nettoyer_svg(code: str) -> str | None:
    """SVG sûr et redimensionnable (balises de dessin seulement), ou None s'il est illisible."""
    code = code.strip()
    debut = code.find("<svg")
    if debut < 0 or len(code) > TAILLE_MAX or "<!ENTITY" in code:
        return None
    code = code[debut:]
    if "xmlns=" not in code.split(">", 1)[0]:
        code = code.replace("<svg", f'<svg xmlns="{SVG}"', 1)
    if "xlink:" in code and "xmlns:xlink" not in code.split(">", 1)[0]:
        code = code.replace("<svg", f'<svg xmlns:xlink="{XLINK}"', 1)
    try:
        racine = ET.fromstring(code)
    except ET.ParseError:
        return None
    if racine.tag != f"{{{SVG}}}svg":
        return None

    def propre(el) -> bool:
        if not isinstance(el.tag, str) or not el.tag.startswith(f"{{{SVG}}}") \
                or el.tag.split("}", 1)[1].lower() not in BALISES:
            return False
        for nom, valeur in list(el.attrib.items()):
            court = nom.split("}", 1)[-1].lower()
            if court.startswith("on") or URL_EXTERNE.search(valeur) or "javascript:" in valeur.lower() \
                    or (court == "href" and not valeur.startswith("#")) or court in ("class", "id") and el is racine:
                del el.attrib[nom]
        for enfant in list(el):
            if not propre(enfant):
                reste = enfant.tail
                el.remove(enfant)
                if reste and reste.strip():  # texte qui suivait la balise retirée : gardé
                    el.text = (el.text or "") + reste
        return True

    propre(racine)
    # Identifiants propres à ce schéma : deux schémas d'une même page ne se marchent pas dessus (flèches, dégradés)
    prefixe = "s" + hashlib.sha1(code.encode()).hexdigest()[:8] + "-"
    for el in racine.iter():
        for nom, valeur in list(el.attrib.items()):
            court = nom.split("}", 1)[-1].lower()
            if court == "id":
                el.set(nom, prefixe + valeur)
            elif court == "href":
                el.set(nom, "#" + prefixe + valeur[1:])
            elif "url(" in valeur:
                el.set(nom, re.sub(r"url\(\s*['\"]?#([^)'\"]+)['\"]?\s*\)", lambda m: f"url(#{prefixe}{m.group(1)})", valeur))
    if not any(True for _ in racine.iter() if _ is not racine and _.tag.split("}", 1)[-1] not in ("title", "desc", "defs")):
        return None
    # Taille : une viewBox (déduite de width/height au besoin), puis largeur naturelle bornée ; le CSS fait le reste
    boite = [float(x) for x in re.findall(r"-?[0-9.]+(?:e-?\d+)?", racine.get("viewBox") or "")][:4]
    largeur, hauteur = _nombre(racine.get("width")), _nombre(racine.get("height"))
    if len(boite) != 4 or boite[2] <= 0 or boite[3] <= 0:
        if not largeur or not hauteur:
            return None
        boite = [0, 0, largeur, hauteur]
        racine.set("viewBox", f"0 0 {largeur:g} {hauteur:g}")
    largeur = min(largeur or boite[2], LARGEUR_MAX)
    racine.set("width", f"{largeur:.0f}")
    racine.set("height", f"{largeur * boite[3] / boite[2]:.0f}")
    # Sur téléphone, un schéma large garde une taille lisible et défile dans son cadre plutôt que de rétrécir
    racine.set("style", f"min-width:{min(largeur, LISIBLE):.0f}px")
    racine.set("role", "img")
    # Le registre des préfixes est commun à tout le programme (d'autres bibliothèques y écrivent) : remis ici
    ET.register_namespace("", SVG)
    ET.register_namespace("xlink", XLINK)
    return ET.tostring(racine, encoding="unicode")


@functools.lru_cache(maxsize=256)
def dot_en_svg(code: str) -> str | None:
    """Graphviz → SVG nettoyé ; None si le code est faux ou si Graphviz n'est pas installé."""
    if len(code) > TAILLE_MAX or not graphviz_present():
        return None
    try:
        r = subprocess.run(DOT, input=code.encode(), capture_output=True, timeout=20, cwd="/")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or b"<svg" not in r.stdout:
        return None
    svg = re.sub(r"<!DOCTYPE.*?>", "", r.stdout.decode("utf-8", "replace"), count=1, flags=re.S)
    return nettoyer_svg(svg)


def langage(code: str) -> str | None:
    """Devine le langage d'un schéma donné sans bloc (champ « schema » d'un quiz)."""
    code = code.strip()
    if code.startswith("<svg") or code.startswith("<?xml"):
        return "svg"
    return "dot" if re.match(r"(strict\s+)?(di)?graph\b", code, re.I) and "{" in code else None


def dessiner(code: str, quoi: str | None = None) -> str | None:
    quoi = quoi or langage(code)
    return nettoyer_svg(code) if quoi == "svg" else dot_en_svg(code.strip()) if quoi == "dot" else None


def _bloc(m) -> tuple[str, str, str]:
    """(langage, code, légende) d'un bloc trouvé par MOTIF ; un bloc seul dans une chaîne est accepté."""
    if m.group("nu"):
        return "svg", m.group("nu"), ""
    return LANGAGES[m.group("langage").lower()], m.group("code"), m.group("legende").strip()


def figure(code: str | None, legende: str = "") -> Markup:
    """<figure> d'un schéma (bloc ```…``` ou code seul) ; vide s'il est illisible."""
    code = (code or "").strip()
    m = MOTIF.search(code)
    quoi = None
    if m:
        quoi, code, legende = _bloc(m)[0], _bloc(m)[1], legende or _bloc(m)[2]
    svg = dessiner(code, quoi)
    if not svg:
        return Markup("")
    titre = f"<figcaption>{html.escape(legende, quote=False)}</figcaption>" if legende else ""
    return Markup(f'<figure class="schema">{svg}{titre}</figure>')


def proteger(texte: str) -> tuple[str, list[str]]:
    """Remplace chaque schéma par un jeton avant le Markdown ; renvoie (texte, [figures HTML]).
    Un schéma illisible reste un bloc de code ordinaire."""
    figures: list[str] = []

    def jeton(m):
        quoi, code, legende = _bloc(m)
        rendu = figure(m.group(0), legende)
        if not rendu:
            return m.group(0)
        figures.append(str(rendu))
        return f"\n\n{JETON.format(len(figures) - 1)}\n\n"
    return _separer_citations(MOTIF.sub(jeton, texte or "")), figures


COUPE = "CITATIONCOUPEFIN"


def _separer_citations(texte: str) -> str:
    """Deux citations séparées par une ligne vide restent deux blocs (Markdown les fusionnerait) :
    « > **À retenir** … » puis « > **Attention** … » donnent deux encadrés."""
    lignes, dans_code, sortie = texte.split("\n"), False, []
    for i, ligne in enumerate(lignes):
        if ligne.lstrip().startswith("```"):
            dans_code = not dans_code
        sortie.append(ligne)
        if not dans_code and not ligne.strip() and 0 < i < len(lignes) - 1 \
                and lignes[i - 1].startswith(">") and lignes[i + 1].startswith(">"):
            sortie += [COUPE, ""]
    return "\n".join(sortie)


def restaurer(rendu: str, figures: list[str]) -> str:
    """Remet les schémas à leur place dans le HTML, et colore les encadrés."""
    return RE_JETON.sub(lambda m: figures[int(m.group(1))] if int(m.group(1)) < len(figures) else "",
                        encadres(re.sub(rf"<p>{COUPE}</p>\n?", "", rendu)))


def nettoyer(texte: str) -> tuple[str, int, int]:
    """À la génération : retire les schémas illisibles. Renvoie (texte, schémas gardés, schémas retirés).
    Sans Graphviz installé (poste de développement), les schémas dot sont gardés tels quels."""
    gardes = retires = 0

    def trier(m):
        nonlocal gardes, retires
        quoi, code, _ = _bloc(m)
        if (quoi == "dot" and not graphviz_present()) or dessiner(code, quoi):
            gardes += 1
            return m.group(0)
        retires += 1
        return ""
    return re.sub(r"\n{3,}", "\n\n", MOTIF.sub(trier, texte or "")), gardes, retires


def sans_schemas(texte: str) -> str:
    """Le texte sans ses schémas (extraits, recherche, cartes mentales)."""
    return MOTIF.sub("", texte or "")


# --- Encadrés : « > **À retenir** … » devient un bloc coloré (site et PDF) ---------------------

ENCADRES = {"retenir": r"à retenir|a retenir|l'essentiel|essentiel|à savoir|par cœur|à connaître",
            "attention": r"attention|piège|pièges|erreur fréquente|erreurs fréquentes|à ne pas confondre",
            "exemple": r"exemple|exemples|application|cas concret",
            "methode": r"méthode|astuce|conseil|moyen mnémotechnique|mnémo",
            "definition": r"définition|définitions|vocabulaire"}
RE_ENCADRE = re.compile(r"<blockquote>\s*<p>\s*<strong>\s*(?P<mot>" + "|".join(f"(?P<{k}>{v})" for k, v in ENCADRES.items())
                        + r")\b", re.I)


def encadres(rendu: str) -> str:
    """Donne sa couleur à une citation qui commence par un mot-clé en gras (À retenir, Attention, Exemple…)."""
    def classe(m):
        genre = next(k for k in ENCADRES if m.group(k))
        return f'<blockquote class="encadre e-{genre}"><p><strong>{m.group("mot")}'
    return RE_ENCADRE.sub(classe, rendu)
