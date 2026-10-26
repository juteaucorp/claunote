"""Formules de maths et de physique écrites en LaTeX ($…$) dans les cours, fiches, cartes et réponses.

    Markdown avec $\\frac{V}{t}$ ──► proteger() ──► Markdown sans formule ──► HTML ──► restaurer()
                                     (jetons)                                        │
              site : <span class="math">\\frac{V}{t}</span>, dessinée par KaTeX ◄────┤
              PDF  : « V/t » en texte (WeasyPrint n'exécute pas de JavaScript) ◄──────┘

Protéger avant le Markdown évite qu'il abîme les formules (_ pris pour de l'italique, \\ mangés).
"""
import html
import re

from markupsafe import Markup

# Code (laissé tel quel) | formule : $$…$$, \[…\], \(…\), $…$ (pas « 5 $ et 10 $ » : collée aux $)
MOTIF = re.compile(
    r"(?P<code>```.*?```|`[^`\n]+`)"
    r"|\$\$(?P<bloc>.+?)\$\$|\\\[(?P<bloc2>.+?)\\\]|\\\((?P<ligne2>.+?)\\\)"
    r"|(?<![\\$\w])\$(?P<ligne>(?=\S)[^$\n]+?(?<=\S))\$(?![\w$])", re.S)
JETON = "MATHJETON{}FIN"
RE_JETON = re.compile(r"MATHJETON(\d+)FIN")


def proteger(texte: str) -> tuple[str, list[tuple[str, bool]]]:
    """Remplace chaque formule par un jeton ; renvoie (texte, [(latex, en bloc ?)])."""
    formules: list[tuple[str, bool]] = []

    def jeton(m):
        if m.group("code"):
            return m.group(0)
        bloc = m.group("bloc") or m.group("bloc2")
        formules.append(((bloc or m.group("ligne") or m.group("ligne2")).strip(), bloc is not None))
        return JETON.format(len(formules) - 1)
    return MOTIF.sub(jeton, texte), formules


def en_html(latex: str, bloc: bool) -> str:
    """Formule pour le site : KaTeX la dessine dans le navigateur (le texte LaTeX reste lisible sans)."""
    return f'<span class="math{" bloc" if bloc else ""}">{html.escape(latex, quote=False)}</span>'


def restaurer(rendu: str, formules: list[tuple[str, bool]], texte: bool = False) -> str:
    def remettre(m):
        i = int(m.group(1))
        if i >= len(formules):
            return m.group(0)
        latex, bloc = formules[i]
        return html.escape(en_texte(latex), quote=False) if texte else en_html(latex, bloc)
    return RE_JETON.sub(remettre, rendu)


def en_ligne(texte: str | None) -> Markup:
    """Texte simple (question de quiz, flashcard) : échappé, sauts de ligne gardés, formules pour KaTeX."""
    proteges, formules = proteger(texte or "")
    rendu = html.escape(proteges, quote=False).replace("\n", "<br>")
    return Markup(restaurer(rendu, formules))


def en_ligne_texte(texte: str | None) -> str:
    """Même chose pour le PDF : formules en texte lisible, le reste échappé par le gabarit."""
    proteges, formules = proteger(texte or "")
    return RE_JETON.sub(lambda m: en_texte(formules[int(m.group(1))][0]), proteges)


# --- LaTeX → texte lisible (PDF) --------------------------------------------------------

SYMBOLES = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "simeq": "≃", "equiv": "≡", "propto": "∝", "infty": "∞", "to": "→",
    "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔",
    "iff": "⇔", "implies": "⇒", "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "cup": "∪",
    "cap": "∩", "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃", "neg": "¬", "land": "∧",
    "lor": "∨", "wedge": "∧", "vee": "∨", "oplus": "⊕", "sum": "Σ", "prod": "Π", "int": "∫", "oint": "∮",
    "partial": "∂", "nabla": "∇", "degree": "°", "circ": "°", "ldots": "…", "cdots": "⋯", "dots": "…",
    "perp": "⊥", "parallel": "∥", "angle": "∠", "mid": "|", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈",
    "rceil": "⌉", "langle": "⟨", "rangle": "⟩", "quad": " ", "qquad": "  ", ",": " ", ";": " ", "!": "",
    " ": " ", "%": "%", "{": "{", "}": "}", "_": "_", "&": "&", "#": "#", "$": "$", "\\": " ",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "lim": "lim", "ln": "ln", "log": "log", "exp": "exp", "sin": "sin", "cos": "cos", "tan": "tan",
    "min": "min", "max": "max", "det": "det",
}
ENSEMBLES = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ"}
EXPOSANTS = str.maketrans("0123456789+-=()niox", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱᵒˣ")
INDICES = str.maketrans("0123456789+-=()aeoxnimkt", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₙᵢₘₖₜ")
GROUPE = r"\{((?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*)\}"   # {…} sur 3 niveaux d'accolades


def _petit(contenu: str, table: dict, signe: str) -> str:
    """x² si tous les caractères ont un exposant (ou un indice) Unicode, sinon x^(…)."""
    return contenu.translate(table) if all(ord(c) in table for c in contenu) else f"{signe}({contenu})" \
        if len(contenu) > 1 else f"{signe}{contenu}"


def en_texte(latex: str) -> str:
    t = latex.strip().replace("\\{", "\x01").replace("\\}", "\x02")   # accolades écrites, pas des groupes
    t = re.sub(r"\\(?:left|right|big|Big|bigg|Bigg)\b\s*([.]?)", "", t)
    t = re.sub(r"\\(?:text|mathrm|textbf|mathbf|operatorname|textit|mathit|mbox)\s*" + GROUPE, r"\1", t)
    t = re.sub(r"\\mathbb\s*\{?([A-Z])\}?", lambda m: ENSEMBLES.get(m.group(1), m.group(1)), t)
    t = re.sub(r"\\ce\s*" + GROUPE, lambda m: re.sub(r"(?<=[A-Za-z)])(\d+)", lambda n: n.group(1).translate(INDICES),
                                                      m.group(1)).replace("->", "→"), t)
    for _ in range(3):  # fractions et racines imbriquées
        t = re.sub(r"\\[dt]?frac\s*" + GROUPE + r"\s*" + GROUPE,
                   lambda m: f"{_parenthese(m.group(1))}/{_parenthese(m.group(2))}", t)
        t = re.sub(r"\\sqrt\s*\[(\d+)\]\s*" + GROUPE, lambda m: f"{m.group(1).translate(EXPOSANTS)}√({m.group(2)})", t)
        t = re.sub(r"\\sqrt\s*" + GROUPE, lambda m: f"√{_parenthese(m.group(1))}", t)
        t = re.sub(r"\\(?:vec|overrightarrow)\s*" + GROUPE, lambda m: f"{m.group(1)}⃗", t)
        t = re.sub(r"\\(?:bar|overline)\s*" + GROUPE, lambda m: f"{m.group(1)}̅", t)
        t = re.sub(r"\\hat\s*" + GROUPE, lambda m: f"{m.group(1)}̂", t)
    t = re.sub(r"\\vec\s*(\w)", "\\1⃗", t)
    t = re.sub(r"\\([A-Za-z]+|[^A-Za-z{}])", lambda m: SYMBOLES.get(m.group(1), m.group(1)), t)
    # Exposants et indices en une passe (sinon le « ( » d'un repli x_(…) serait repris)
    t = re.sub(r"([\^_])\s*(?:" + GROUPE + r"|([^\s{]))", lambda m: _petit(
        m.group(2) if m.group(2) is not None else m.group(3), EXPOSANTS if m.group(1) == "^" else INDICES, m.group(1)), t)
    t = t.replace("{", "").replace("}", "").replace("~", " ").replace("\x01", "{").replace("\x02", "}")
    return re.sub(r"\s+", " ", t).strip()


def _parenthese(x: str) -> str:
    return x if re.fullmatch(r"[\w.\\]+", x) else f"({x})"
