"""PDF des révisions (à imprimer ou partager) avec WeasyPrint.

Une épreuve blanche ou un quiz met son corrigé sur une page à part : on peut imprimer le sujet
seul. Une campagne donne un livret : programme jour par jour puis tous ses supports.
"""
import datetime as dt
import json
import re
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from claunote import formules, revision, schemas

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape())
_env.filters["maths"] = formules.en_ligne_texte
_env.filters["schema"] = schemas.figure
LOGO = (Path(__file__).parent / "static" / "logo.svg").as_uri()


def md(texte: str | None) -> Markup:
    """Markdown → HTML ; le HTML brut éventuel est neutralisé, les repères [D1] restent du texte.
    Les formules $…$ deviennent du texte lisible (« √(x²+1) ≤ α ») : pas de JavaScript dans un PDF."""
    texte, figures = schemas.proteger(texte or "")
    texte, maths = formules.proteger(texte)
    texte = texte.replace("&", "&amp;").replace("<", "&lt;")
    rendu = formules.restaurer(markdown.markdown(texte, extensions=["tables", "fenced_code"]), maths,
                               texte=True)
    rendu = rendu.replace("[IA]", '<span class="ajout-ia">pas dans tes cours</span>')
    return Markup(schemas.restaurer(rendu, figures))


def section(rev: dict, sources: dict[int, str], cartes: list[dict] | None = None, corrige: bool = True) -> dict:
    """Ce qu'il faut pour afficher une révision dans le PDF (sujet, corrigé à part)."""
    s = {"rev": rev, "titre": rev["sujet"], "genre": revision.GENRES.get(rev["genre"], rev["genre"]),
         "corps": Markup(""), "corrige": Markup(""), "questions": [], "cartes": [],
         "sources": [(r, sources[i]) for r, i in (rev.get("reperes") or {}).items() if i in sources],
         "web": rev.get("sources_web") or []}
    if rev["statut"] != "pret":
        s["corps"] = Markup("<p><em>Pas encore prêt.</em></p>")
    elif rev["genre"] == "quiz":
        s["questions"] = json.loads(rev["contenu"])
    elif rev["genre"] == "cartes":
        s["cartes"] = cartes if cartes is not None else json.loads(rev["contenu"] or "[]")
    elif rev["genre"] == "examen":
        sujet, corr = revision.separer_corrige(rev["contenu"] or "")
        s["corps"], s["corrige"] = md(sujet), md(corr)
    else:
        s["corps"] = md(rev["contenu"])
    if not corrige:
        s["corrige"] = Markup("")
    s["avec_corrige"] = corrige
    return s


def rendre(html: str) -> bytes:
    from weasyprint import HTML  # lourd à charger : seulement quand on fabrique un PDF
    return HTML(string=html, base_url=str(Path(__file__).parent)).write_pdf()


def revision_pdf(rev: dict, sources: dict[int, str], cartes: list[dict] | None = None, corrige: bool = True) -> bytes:
    html = _env.get_template("pdf.html").render(
        titre=rev["sujet"], sous_titre=" · ".join(x for x in [revision.GENRES.get(rev["genre"]), rev.get("matiere"),
                                                             rev.get("niveau")] if x),
        sections=[section(rev, sources, cartes, corrige)], programme=None, logo=LOGO, le=dt.date.today())
    return rendre(html)


def campagne_pdf(campagne: dict, revisions: list[dict], sources: dict[int, str], corrige: bool = True) -> bytes:
    html = _env.get_template("pdf.html").render(
        titre=campagne["titre"],
        sous_titre=f"Du {campagne['debut']:%d/%m/%Y} au {campagne['fin']:%d/%m/%Y} · {len(revisions)} supports",
        sections=[section(r, sources, None, corrige) for r in revisions if r["statut"] == "pret"],
        programme=campagne.get("programme"), consigne=campagne.get("consigne"), logo=LOGO, le=dt.date.today(),
        jours=revision.jours_programme(campagne.get("programme"), []))
    return rendre(html)


def nom(texte: str, extension: str = "pdf") -> str:
    base = re.sub(r"[^\w\- ]+", "", texte, flags=re.UNICODE).strip().replace(" ", "-")[:60] or "revision"
    return f"{base}.{extension}"
