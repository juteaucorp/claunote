"""PDF des révisions (à imprimer ou partager) avec WeasyPrint.

Une épreuve blanche ou un quiz met son corrigé sur une page à part : on peut imprimer le sujet
seul. Une fiche sort en synthèse dense sur trois colonnes. Une campagne donne un livret de ses supports.
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
    texte, figures = schemas.proteger(revision.aerer_listes(texte or ""))
    texte, maths = formules.proteger(texte)
    texte = texte.replace("&", "&amp;").replace("<", "&lt;")
    rendu = formules.restaurer(markdown.markdown(texte, extensions=["tables", "fenced_code"]), maths,
                               texte=True)
    rendu = rendu.replace("[IA]", '<span class="ajout-ia">pas dans tes cours</span>')
    return Markup(schemas.restaurer(rendu, figures))


COULEURS = 6  # couleurs des bandeaux de partie d'une fiche (classes c0…c5 de pdf.html)
RE_H1 = re.compile(r"\s*<h1>(.*?)</h1>", re.S)
RE_QUESTIONS = re.compile(r"<h2>(?:\s*\d+\s*[.)]\s*)?(questions|pour s'entra[iî]ner)", re.I)
RE_REPONSES = re.compile(r"<h2>(?:\s*\d+\s*[.)]\s*)?(r[ée]ponses|corrig[ée])", re.I)


def fiche_dense(rendu: str) -> tuple[Markup, Markup]:
    """Fiche synthèse : (titre, corps) où chaque partie « ## » est un bloc avec sa couleur de bandeau.

        <h1>…</h1> intro <h2>1. …</h2> … <h2>2. …</h2> …
           │        └────────┬──────────────────┬───────► <section class="partie c0">, c1, c2…
           └► titre, au-dessus des trois colonnes        (questions et réponses : classes à part)
    """
    titre = ""
    m = RE_H1.match(rendu)
    if m:
        titre, rendu = m.group(1), rendu[m.end():]
    morceaux = re.split(r"(?=<h2>)", rendu)
    blocs, n = [], 0
    for morceau in morceaux:
        if not morceau.strip():
            continue
        if not morceau.startswith("<h2>"):
            blocs.append(f'<section class="partie intro">{morceau}</section>')
        elif RE_REPONSES.match(morceau):
            blocs.append(f'<section class="partie reponses">{morceau}</section>')
        elif RE_QUESTIONS.match(morceau):
            blocs.append(f'<section class="partie questions">{morceau}</section>')
        else:
            blocs.append(f'<section class="partie c{n % COULEURS}">{morceau}</section>')
            n += 1
    return Markup(titre), Markup("".join(blocs))


def section(rev: dict, sources: dict[int, str], cartes: list[dict] | None = None, corrige: bool = True) -> dict:
    """Ce qu'il faut pour afficher une révision dans le PDF (sujet, corrigé à part)."""
    s = {"rev": rev, "titre": rev["sujet"], "genre": revision.GENRES.get(rev["genre"], rev["genre"]),
         "corps": Markup(""), "corrige": Markup(""), "questions": [], "cartes": [], "dense": False, "titre_dense": "",
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
    elif rev["genre"] == "fiche":
        s["dense"] = True
        s["titre_dense"], s["corps"] = fiche_dense(str(md(rev["contenu"])))
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


def campagne_pdf(campagne: dict, revisions: list[dict], sources: dict[int, str], corrige: bool = True,
                 programme: bool = False) -> bytes:
    """Livret d'une campagne : ses supports. Le programme jour par jour n'y est que si on le demande
    (il se suit sur le site, où l'on coche les tâches)."""
    html = _env.get_template("pdf.html").render(
        titre=campagne["titre"],
        sous_titre=f"Du {campagne['debut']:%d/%m/%Y} au {campagne['fin']:%d/%m/%Y} · {len(revisions)} supports",
        sections=[section(r, sources, None, corrige) for r in revisions if r["statut"] == "pret"],
        programme=campagne.get("programme") if programme else None, consigne=campagne.get("consigne"), logo=LOGO,
        le=dt.date.today(), jours=revision.jours_programme(campagne.get("programme"), []) if programme else [])
    return rendre(html)


def nom(texte: str, extension: str = "pdf") -> str:
    base = re.sub(r"[^\w\- ]+", "", texte, flags=re.UNICODE).strip().replace(" ", "-")[:60] or "revision"
    return f"{base}.{extension}"
