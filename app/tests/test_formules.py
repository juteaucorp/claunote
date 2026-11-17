from claunote import formules, pdf
from claunote.main import rendu_markdown


def test_proteger_et_restaurer():
    texte = "Prix : 5 $ et 10 $. Suite $u_{n+1} = q \\times u_n$, somme :\n\n$$\\sum_{k=0}^n q^k$$\n\n`$code$` et \\(a_1\\)"
    proteges, maths = formules.proteger(texte)
    assert "5 $ et 10 $" in proteges and "`$code$`" in proteges     # ni l'argent, ni le code
    assert maths == [("u_{n+1} = q \\times u_n", False), ("\\sum_{k=0}^n q^k", True), ("a_1", False)]
    rendu = rendu_markdown(texte)
    assert '<span class="math">u_{n+1} = q \\times u_n</span>' in rendu      # le _ n'est pas devenu de l'italique
    assert '<span class="math bloc">\\sum_{k=0}^n q^k</span>' in rendu
    assert "<em>" not in rendu


def test_formule_ne_laisse_pas_passer_de_html():
    rendu = rendu_markdown("$a < b$ et <script>alert(1)</script>")
    assert '<span class="math">a &lt; b</span>' in rendu and "<script>" not in rendu
    assert str(formules.en_ligne("$x<1$\n<b>")) == '<span class="math">x&lt;1</span><br>&lt;b&gt;'


def test_en_texte_pour_le_pdf():
    assert formules.en_texte(r"\frac{u_0(1-q^{n+1})}{1-q}") == "(u₀(1-qⁿ⁺¹))/(1-q)"
    assert formules.en_texte(r"\sqrt{x^2+1} \leq \alpha") == "√(x²+1) ≤ α"
    assert formules.en_texte(r"\ce{H2O + CO2 -> H2CO3}") == "H₂O + CO₂ → H₂CO₃"
    assert formules.en_texte(r"\lim_{n \to +\infty} u_n = 0") == "lim_(n → +∞) uₙ = 0"
    assert formules.en_texte(r"x \in \mathbb{R}, \{1, 2\}") == "x ∈ ℝ, {1, 2}"
    assert "V/t" in str(pdf.md("Vitesse : $v = \\frac{d}{t}$ soit $V/t$")) and "frac" not in str(pdf.md("$\\frac{a}{b}$"))
    assert formules.en_ligne_texte("Aire $\\pi r^2$") == "Aire π r²"


def test_latex_mal_ecrit_par_l_ia():
    """Ce qui sortait en texte brut dans les fiches : antislash doublé, arguments sans accolades, commandes
    inconnues du convertisseur, $ collé à un mot, LaTeX hors des $."""
    assert formules.en_texte(r"\\overline{a}+\\frac{1}{2}") == "a̅+1/2"
    assert formules.en_texte(r"\overline a\cdot\bar b") == "a̅·b̅"
    assert formules.en_texte(r"\frac12+\frac\pi2+\frac{1^{er}+dernier}{2}") == "1/2+π/2+(1^(er)+dernier)/2"
    assert formules.en_texte(r"\mathcal{L} \leftrightarrow \mathcal P(E)") == "ℒ ↔ P(E)"
    assert formules.en_texte(r"x \not\in A \Longleftrightarrow \binom{n}{k}") == "x ∉ A ⟺ (k parmi n)"
    assert formules.en_texte(r"\begin{cases} 1 & x>0 \\ 0 & \text{sinon}\end{cases}") == "1 x>0 ; 0 sinon"
    assert formules.en_texte_html(r"\overline{a+b}=e^{a+q}") == '<span class="sur">a+b</span>=e<sup>a+q</sup>'
    proteges, maths = formules.proteger(r"normalFRép$(a,b,\sigma)$ ; a+\overline{a}=1 ; \\frac{1}{2} ; 5 $ et 10 $")
    assert maths == [(r"(a,b,\sigma)", False), (r"\overline{a}", False), (r"\frac{1}{2}", False)]
    assert "5 $ et 10 $" in proteges
    # Pas de faux positif : chemins Windows, code
    assert formules.proteger(r"C:\Users\to\inetpub et `\alpha`")[1] == []
    rendu = str(pdf.md("- **Complément** : a+\\overline{a}=1, $\\\\frac{1}{2}$, $\\sigma$"))
    assert "overline" not in rendu and "frac" not in rendu and "σ" in rendu and "\\" not in rendu
