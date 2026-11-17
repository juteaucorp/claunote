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

# Code (laissé tel quel) | formule : $$…$$, \[…\], \(…\), $…$ (pas « 5 $ et 10 $ » : collée aux $ ;
# « normalFRép$(a,b)$ » en est une : le $ peut suivre un mot)
MOTIF = re.compile(
    r"(?P<code>```.*?```|`[^`\n]+`)"
    r"|\$\$(?P<bloc>.+?)\$\$|\\\[(?P<bloc2>.+?)\\\]|\\\((?P<ligne2>.+?)\\\)"
    r"|(?<![\\$])\$(?P<ligne>(?=\S)[^$\n]+?(?<=\S))\$(?![\w$])", re.S)
JETON = "MATHJETON{}FIN"
RE_JETON = re.compile(r"MATHJETON(\d+)FIN")


def proteger(texte: str) -> tuple[str, list[tuple[str, bool]]]:
    """Remplace chaque formule par un jeton ; renvoie (texte, [(latex, en bloc ?)]).

    Rattrape aussi les commandes LaTeX connues écrites hors des $ (« a + \\overline{a} = 1 ») : sans
    ça, elles sortiraient en texte brut. Les antislashs doublés (« \\\\frac ») sont ramenés à un seul."""
    formules: list[tuple[str, bool]] = []

    def ajouter(latex: str, bloc: bool) -> str:
        formules.append((re.sub(r"\\\\(?=[A-Za-z])", r"\\", latex.strip()), bloc))
        return JETON.format(len(formules) - 1)

    def jeton(m):
        if m.group("code"):
            return m.group(0)
        bloc = m.group("bloc") or m.group("bloc2")
        return ajouter(bloc or m.group("ligne") or m.group("ligne2"), bloc is not None)

    def nue(m):
        return m.group(0) if m.group("code") else ajouter(m.group("nue"), False)
    return MOTIF_NU.sub(nue, MOTIF.sub(jeton, texte)), formules


def en_html(latex: str, bloc: bool) -> str:
    """Formule pour le site : KaTeX la dessine dans le navigateur (le texte LaTeX reste lisible sans)."""
    return f'<span class="math{" bloc" if bloc else ""}">{html.escape(latex, quote=False)}</span>'


def restaurer(rendu: str, formules: list[tuple[str, bool]], texte: bool = False) -> str:
    def remettre(m):
        i = int(m.group(1))
        if i >= len(formules):
            return m.group(0)
        latex, bloc = formules[i]
        return en_texte_html(latex) if texte else en_html(latex, bloc)
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
    "times": "×", "cdot": "·", "cdotp": "·", "div": "÷", "pm": "±", "mp": "∓", "leq": "≤", "le": "≤",
    "leqslant": "≤", "geq": "≥", "ge": "≥", "geqslant": "≥", "nleq": "≰", "ngeq": "≱", "ll": "≪", "gg": "≫",
    "neq": "≠", "ne": "≠", "approx": "≈", "simeq": "≃", "sim": "∼", "cong": "≅", "equiv": "≡", "propto": "∝",
    "infty": "∞", "to": "→", "gets": "←", "rightarrow": "→", "leftarrow": "←", "leftrightarrow": "↔",
    "longrightarrow": "⟶", "longleftarrow": "⟵", "longleftrightarrow": "⟷", "Rightarrow": "⇒",
    "Leftarrow": "⇐", "Leftrightarrow": "⇔", "Longrightarrow": "⟹", "Longleftarrow": "⟸",
    "Longleftrightarrow": "⟺", "iff": "⇔", "implies": "⇒", "impliedby": "⇐", "mapsto": "↦",
    "longmapsto": "⟼", "uparrow": "↑", "downarrow": "↓", "updownarrow": "↕", "nearrow": "↗", "searrow": "↘",
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇",
    "nsubseteq": "⊈", "cup": "∪", "cap": "∩", "bigcup": "⋃", "bigcap": "⋂", "setminus": "∖", "complement": "∁",
    "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃", "nexists": "∄", "neg": "¬", "lnot": "¬",
    "land": "∧", "lor": "∨", "wedge": "∧", "vee": "∨", "oplus": "⊕", "ominus": "⊖", "otimes": "⊗",
    "odot": "⊙", "bigoplus": "⊕", "top": "⊤", "bot": "⊥", "vdash": "⊢", "models": "⊨", "sum": "Σ",
    "prod": "Π", "coprod": "∐", "int": "∫", "iint": "∬", "iiint": "∭", "oint": "∮", "partial": "∂",
    "nabla": "∇", "degree": "°", "circ": "∘", "ldots": "…", "cdots": "⋯", "dots": "…", "vdots": "⋮",
    "ddots": "⋱", "perp": "⊥", "parallel": "∥", "angle": "∠", "triangle": "△", "square": "□", "Box": "□",
    "mid": "|", "vert": "|", "lvert": "|", "rvert": "|", "Vert": "‖", "lVert": "‖", "rVert": "‖",
    "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉", "langle": "⟨", "rangle": "⟩",
    "star": "⋆", "ast": "∗", "bullet": "•", "prime": "′", "ell": "ℓ", "hbar": "ℏ", "aleph": "ℵ",
    "Re": "ℜ", "Im": "ℑ", "wp": "℘", "checkmark": "✓", "dagger": "†", "colon": ":", "backslash": "\\",
    "quad": " ", "qquad": "  ", ",": " ", ";": " ", ":": " ", "!": "", ">": " ",
    " ": " ", "%": "%", "{": "{", "}": "}", "_": "_", "&": "&", "#": "#", "$": "$", "\\": " ",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "xi": "ξ", "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ",
    "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ",
    "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "lim": "lim", "limsup": "lim sup", "liminf": "lim inf", "sup": "sup", "inf": "inf", "ln": "ln",
    "log": "log", "lg": "lg", "exp": "exp", "sin": "sin", "cos": "cos", "tan": "tan", "cot": "cot",
    "sec": "sec", "arcsin": "arcsin", "arccos": "arccos", "arctan": "arctan", "sinh": "sh", "cosh": "ch",
    "tanh": "th", "min": "min", "max": "max", "det": "det", "dim": "dim", "ker": "ker", "deg": "deg",
    "arg": "arg", "gcd": "pgcd", "Pr": "P", "mod": "mod", "bmod": "mod",
    # Mise en forme sans équivalent en texte : retirée
    "displaystyle": "", "textstyle": "", "scriptstyle": "", "limits": "", "nolimits": "", "mathstrut": "",
    "nonumber": "", "notag": "", "relax": "", "middle": "",
}
ENSEMBLES = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "P": "ℙ", "K": "𝕂", "D": "𝔻"}
RONDES = {"B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ", "L": "ℒ", "M": "ℳ", "R": "ℛ"}  # \mathcal présentes dans DejaVu
EXPOSANTS = str.maketrans("0123456789+-=()niox", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱᵒˣ")   # sinon <sup> dans le PDF (plus régulier)
INDICES = str.maketrans("0123456789+-=()aeoxnimkt", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₙᵢₘₖₜ")
INTERNE = r"(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*"   # contenu d'un {…} sur 3 niveaux d'accolades
GROUPE = r"\{(" + INTERNE + r")\}"
JETON_TEX = r"\\[A-Za-z]+|[^\s{}\\]"                  # argument sans accolades : \frac12, \overline a, \frac\pi2
ARGUMENT = r"\{" + INTERNE + r"\}|" + JETON_TEX
UN_ARGUMENT = ("sqrt|overline|bar|vec|hat|widehat|tilde|widetilde|dot|ddot|underline|overrightarrow|mathbb|"
               "mathcal|mathscr|mathfrak|mathbf|mathrm|mathit|mathsf|boldsymbol|bm|textbf|textit|text|operatorname")
DEUX_ARGUMENTS = "dfrac|tfrac|cfrac|frac|binom|dbinom|tbinom"
SUR, FIN_SUR = "\x03", "\x04"   # entourent un texte surligné (\overline) : barre en CSS dans le PDF, sinon U+0305


def _petit(contenu: str, table: dict, signe: str) -> str:
    """x² si tous les caractères ont un exposant (ou un indice) Unicode, sinon x^(…)."""
    return contenu.translate(table) if all(ord(c) in table for c in contenu) else f"{signe}({contenu})" \
        if len(contenu) > 1 else f"{signe}{contenu}"


def _sans_accolades(x: str) -> str:
    return x[1:-1] if x.startswith("{") and x.endswith("}") else x


def _brut(latex: str) -> str:
    """LaTeX → texte, avec les repères SUR…FIN_SUR des barres."""
    # « \\frac » : antislash doublé (échappement en trop de l'IA), pas un saut de ligne suivi de « frac »
    t = re.sub(r"\\\\(?=[A-Za-z])", r"\\", latex.strip())
    t = t.replace("\\{", "\x01").replace("\\}", "\x02")   # accolades écrites, pas des groupes
    t = re.sub(r"\\begin\s*\{[^}]*\}(?:\s*\{[clr|]*\})?|\\end\s*\{[^}]*\}", " ", t)   # cases, array, aligned…
    t = re.sub(r"\s*\\\\\s*", " ; ", t)
    t = re.sub(r"(?<!\\)&", " ", t)
    t = re.sub(r"\\(?:left|right|big|Big|bigg|Bigg)[lr]?\b\s*([.]?)", "", t)
    t = re.sub(r"\\(?:phantom|hphantom|vphantom|hspace|vspace|label|tag)\*?\s*" + GROUPE, "", t)
    t = re.sub(r"\\not\s*(?:\\in\b|∈)", "∉", t)
    t = re.sub(r"\\not\s*=", "≠", t)
    t = re.sub(r"\\not\s*\\subset(?:eq)?\b", "⊄", t)
    t = re.sub(r"\\pmod\s*" + GROUPE, r" (mod \1)", t)
    # Arguments sans accolades : \overline a → \overline{a}, \frac12 → \frac{1}{2}
    t = re.sub(r"\\(" + UN_ARGUMENT + r")(?![A-Za-z])\s*(?![\s{\[])(" + JETON_TEX + ")", r"\\\1{\2}", t)
    t = re.sub(r"\\(" + DEUX_ARGUMENTS + r")(?![A-Za-z])\s*(" + ARGUMENT + r")\s*(" + ARGUMENT + ")",
               lambda m: f"\\{m.group(1)}{{{_sans_accolades(m.group(2))}}}{{{_sans_accolades(m.group(3))}}}", t)
    t = re.sub(r"\\(?:text|mathrm|textbf|mathbf|operatorname\*?|textit|mathit|mathsf|mbox|boldsymbol|bm)\s*" + GROUPE,
               r"\1", t)
    t = re.sub(r"\\mathbb\s*\{?([A-Z])\}?", lambda m: ENSEMBLES.get(m.group(1), m.group(1)), t)
    t = re.sub(r"\\(?:mathcal|mathscr|mathfrak)\s*" + GROUPE,
               lambda m: "".join(RONDES.get(c, c) for c in m.group(1)), t)
    t = re.sub(r"\\ce\s*" + GROUPE, lambda m: re.sub(r"(?<=[A-Za-z)])(\d+)", lambda n: n.group(1).translate(INDICES),
                                                      m.group(1)).replace("->", "→"), t)
    for _ in range(3):  # fractions et racines imbriquées
        t = re.sub(r"\\[dtc]?frac\s*" + GROUPE + r"\s*" + GROUPE,
                   lambda m: f"{_parenthese(m.group(1))}/{_parenthese(m.group(2))}", t)
        t = re.sub(r"\\[dt]?binom\s*" + GROUPE + r"\s*" + GROUPE, r"(\2 parmi \1)", t)
        t = re.sub(r"\\sqrt\s*\[(\d+)\]\s*" + GROUPE, lambda m: f"{m.group(1).translate(EXPOSANTS)}√({m.group(2)})", t)
        t = re.sub(r"\\sqrt\s*" + GROUPE, lambda m: f"√{_parenthese(m.group(1))}", t)
        t = re.sub(r"\\(?:vec|overrightarrow)\s*" + GROUPE, lambda m: f"{m.group(1)}⃗", t)
        t = re.sub(r"\\(?:bar|overline)\s*" + GROUPE, lambda m: f"{SUR}{m.group(1)}{FIN_SUR}", t)
        t = re.sub(r"\\underline\s*" + GROUPE, r"\1", t)
        t = re.sub(r"\\(?:hat|widehat)\s*" + GROUPE, lambda m: f"{m.group(1)}̂", t)
        t = re.sub(r"\\(?:tilde|widetilde)\s*" + GROUPE, lambda m: f"{m.group(1)}̃", t)
        t = re.sub(r"\\dot\s*" + GROUPE, lambda m: f"{m.group(1)}̇", t)
        t = re.sub(r"\\ddot\s*" + GROUPE, lambda m: f"{m.group(1)}̈", t)
    t = re.sub(r"\\([A-Za-z]+|[^A-Za-z{}])", lambda m: SYMBOLES.get(m.group(1), m.group(1)), t)
    # Exposants et indices en une passe (sinon le « ( » d'un repli x_(…) serait repris)
    t = re.sub(r"([\^_])\s*(?:" + GROUPE + r"|([^\s{]))", lambda m: _petit(
        m.group(2) if m.group(2) is not None else m.group(3), EXPOSANTS if m.group(1) == "^" else INDICES, m.group(1)), t)
    t = t.replace("{", "").replace("}", "").replace("~", " ").replace("\x01", "{").replace("\x02", "}")
    return re.sub(r"\s+", " ", t).strip()


def en_texte(latex: str) -> str:
    """Texte seul : la barre d'un \\overline devient un trait combinant (a̅) sur chaque caractère."""
    return re.sub(SUR + "(.*?)" + FIN_SUR, lambda m: "".join(c + ("̅" if not c.isspace() else "") for c in m.group(1)),
                  _brut(latex)).replace(SUR, "").replace(FIN_SUR, "")


def en_texte_html(latex: str) -> str:
    """Pour le PDF : le texte lisible, avec de vrais exposants et indices là où Unicode n'en a pas
    (« e^(a+b) » → e<sup>a+b</sup>, « P_B(A) » → P<sub>B</sub>(A)) et une vraie barre au-dessus (Ā, a+b)."""
    t = html.escape(_brut(latex), quote=False)
    for signe, balise in (("^", "sup"), ("_", "sub")):
        t = re.sub(re.escape(signe) + r"\(([^()]+)\)", rf"<{balise}>\1</{balise}>", t)
        t = re.sub(re.escape(signe) + r"([^\s()<&" + SUR + FIN_SUR + "])", rf"<{balise}>\1</{balise}>", t)
    t = re.sub(SUR + "(.*?)" + FIN_SUR, r'<span class="sur">\1</span>', t)
    return t.replace(SUR, "").replace(FIN_SUR, "")


def _parenthese(x: str) -> str:
    return x if re.fullmatch(r"[\w.\\]+", x) else f"({x})"


# Commande LaTeX connue hors des $ (avec ses arguments) ; pas « C:\\Users » ni un « \\n » de code
_SYMBOLES_NUS = sorted({n for n in SYMBOLES if n.isalpha() and len(n) > 1 and SYMBOLES[n]}
                       - {"in", "to", "ni", "mid", "top", "lim", "ln", "log", "lg", "exp", "sin", "cos", "tan", "cot",
                          "sec", "min", "max", "det", "dim", "ker", "deg", "arg", "inf", "sup", "mod", "star", "square",
                          "colon", "vert", "quad", "angle", "triangle", "circ", "prime", "Box", "Re", "Im", "dots"},
                       key=len, reverse=True)
_ARG_NU = r"\s*(?:\{" + INTERNE + r"\}|" + JETON_TEX + ")"
MOTIF_NU = re.compile(
    r"(?P<code>```.*?```|`[^`\n]+`)|(?P<nue>\\{1,2}(?:"
    r"(?:" + DEUX_ARGUMENTS + r")(?![A-Za-z])" + _ARG_NU * 2 +
    r"|(?:mathbb|" + UN_ARGUMENT.replace("textbf|textit|text|", "") + r")(?![A-Za-z])" + _ARG_NU +
    r"|(?:" + "|".join(_SYMBOLES_NUS) + r")(?![A-Za-z])))", re.S)
