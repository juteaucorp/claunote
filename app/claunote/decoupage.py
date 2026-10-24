"""Découpage d'un très gros scan (une année entière, toutes matières) en documents séparés.

Un PDF de plus de DECOUPAGE_PAGES pages n'est pas analysé d'un bloc :
1. lecture locale de chaque page, sans IA (ocr.py) : texte ou OCR, pages blanches, en-têtes
   qui annoncent un début (« Chapitre », « TP n° », « Page 1/3 »…), pages personnelles (sensible.py) ;
2. Claude regarde les pages en petites images grises, par paquets de PAQUET pages, avec ces indices,
   et dit pour chaque page si un nouveau document y commence (titre, matière). Les pages
   personnelles ne lui sont jamais montrées ;
3. chaque document devient un PDF (pages d'origine, pleine qualité) déposé dans l'inbox à côté du
   gros scan, puis analysé comme les autres.

Sans réponse exploitable de Claude, on coupe sur les seuls indices locaux. Un morceau trop long
est recoupé (PARTIE_MAX pages) pour que l'analyse reste possible.
"""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from claunote import agent, importation, ocr, rangement, sensible

SEUIL_PAGES = int(os.environ.get("DECOUPAGE_PAGES", "40"))
PAQUET = 24          # pages montrées à Claude par appel
PARTIE_MAX = 40      # pages au plus par document produit
MINIATURE = 900      # pixels, grand côté : assez pour lire un titre, léger pour le forfait


def a_decouper(chemin: Path) -> bool:
    return chemin.suffix.lower() == ".pdf" and ocr.nb_pages(chemin) > SEUIL_PAGES


_DEBUT = re.compile(
    r"^\s*(chapitre|chap\.?|partie|th[eè]me|s[ée]quence|le[cç]on|cours|tp|td|travaux pratiques|devoir|"
    r"contr[oô]le|interro\w*|[ée]valuation|ds|dst|sujet|exercices?|fiche|activit[ée]|bac blanc|bts blanc|"
    r"brevet blanc|examen)\b[\s:n°.-]*\w", re.I | re.M)
_PAGE_UN = re.compile(r"\b(page\s*1\s*(/|sur)\s*\d+|p\.?\s*1\s*/\s*\d+|1\s*/\s*[2-9]\b)", re.I)


def indices_page(texte: str) -> dict:
    """Ce que le haut de la page laisse deviner, sans IA."""
    haut = "\n".join(l for l in texte.splitlines() if l.strip())[:300]
    debut = _DEBUT.search(haut)
    matiere = importation._chercher(importation._MATIERES, [haut]) if haut else None
    return {"debut": bool(debut or _PAGE_UN.search(haut)), "titre": (debut.group(0).strip()[:60] if debut else None),
            "matiere": matiere, "mots": len(texte.split())}


def lire_local(chemin: Path, miniatures: Path, progression=None) -> list[dict]:
    """Une entrée par page : blanche, personnelle (verdict), indices ; miniatures JPEG pour Claude."""
    from PIL import Image
    miniatures.mkdir(parents=True, exist_ok=True)
    total = ocr.nb_pages(chemin)

    def miniature(n: int, png: Path):
        image = Image.open(png).convert("L")
        image.thumbnail((MINIATURE, MINIATURE))
        image.save(miniatures / f"page-{n:04d}.jpg", quality=60)

    pages = []
    for debut in range(1, total + 1, 20):
        lues = ocr.lire_pdf(chemin, list(range(debut, min(debut + 19, total) + 1)), avec_rendu=miniature)
        for n in sorted(lues):
            texte, encre = lues[n]["texte"], lues[n]["encre"]
            v = sensible.examiner_texte(texte)
            pages.append({"page": n, "blanche": ocr.blanche(encre) and len(texte.strip()) < 15,
                          "sensible": {"niveau": v.niveau, "categorie": v.categorie, "raisons": v.raisons} if v else None,
                          **indices_page(texte)})
        if progression:
            progression(min(debut + 19, total), total)
    return pages


PROMPT = """Ces images sont les pages {premiere} à {derniere} d'un très gros scan d'un élève (du collège au
BTS SIO) : une année de cours mélangés, toutes matières, mis bout à bout. Il faut le recouper en documents.
Lis chaque image du dossier courant avec l'outil Read, dans l'ordre : {fichiers}.
{contexte}
Indices trouvés sans IA (OCR, parfois faux) :
{indices}

Pour CHAQUE page listée, dis si un NOUVEAU document y commence : nouveau cours ou chapitre, autre matière,
TP, exercice, contrôle, polycopié distribué… Une suite de pages du même cours (même matière, même sujet,
numérotation qui continue) reste un seul document.
Réponds UNIQUEMENT avec un objet JSON valide, sans aucun texte autour :
{{"pages": [{{"page": {premiere}, "nouveau": true, "titre": "titre court du document qui commence, sinon null",
  "matiere": "matière ou null"}}]}}
"""


def _lire_reponse(texte: str) -> dict[int, dict]:
    debut, fin = texte.find("{"), texte.rfind("}")
    try:
        d = json.loads(texte[debut:fin + 1])
    except (json.JSONDecodeError, ValueError) as e:
        raise agent.ErreurAgent(f"JSON invalide : {e}") from e
    pages = {}
    for p in (d.get("pages") if isinstance(d, dict) else None) or []:
        if isinstance(p, dict) and isinstance(p.get("page"), int):
            pages[p["page"]] = {"nouveau": p.get("nouveau") is True, "titre": p.get("titre") or None,
                                "matiere": p.get("matiere") or None}
    if not pages:
        raise agent.ErreurAgent("aucune page dans la réponse")
    return pages


def demander_coupes(pages: list[dict], miniatures: Path, precedente: dict | None) -> dict[int, dict]:
    """Un appel à Claude pour un paquet de pages (sans les pages blanches ni personnelles)."""
    montrees = [p for p in pages if not p["blanche"] and not p["sensible"]]
    if not montrees:
        return {}
    with tempfile.TemporaryDirectory() as tmp:
        fichiers = []
        for p in montrees:
            nom = f"page-{p['page']:04d}.jpg"
            if (miniatures / nom).exists():
                shutil.copy(miniatures / nom, Path(tmp) / nom)
                fichiers.append(nom)
        lignes = []
        for p in pages:
            if p["blanche"]:
                lignes.append(f"- page {p['page']} : blanche (souvent un intercalaire entre deux documents), non montrée")
            elif p["sensible"]:
                lignes.append(f"- page {p['page']} : document personnel retiré, non montrée")
            else:
                details = [x for x in (("en-tête de début : " + (p["titre"] or "oui")) if p["debut"] else None,
                                       f"matière devinée : {p['matiere']}" if p["matiere"] else None) if x]
                lignes.append(f"- page {p['page']} : " + ("; ".join(details) or "rien de particulier"))
        contexte = ""
        if precedente:
            contexte = (f"La page juste avant ({precedente['page']}) appartenait au document « "
                        f"{precedente.get('titre') or '?'} » ({precedente.get('matiere') or 'matière ?'}) : "
                        "la première page peut en être la suite.\n")
        prompt = PROMPT.format(premiere=montrees[0]["page"], derniere=montrees[-1]["page"],
                               fichiers=", ".join(fichiers), contexte=contexte, indices="\n".join(lignes))
        reponse, _ = agent.demander("decoupage", prompt, Path(tmp), lire=_lire_reponse, outils=["Read"],
                                    timeout=1200)
    return reponse


def coupes_locales(pages: list[dict]) -> dict[int, dict]:
    """Sans Claude : un document commence après une page blanche ou sur un en-tête de début."""
    coupes, apres_blanche = {}, True
    for p in pages:
        if p["blanche"] or p["sensible"]:
            apres_blanche = apres_blanche or p["blanche"]
            continue
        coupes[p["page"]] = {"nouveau": apres_blanche or p["debut"], "titre": p["titre"], "matiere": p["matiere"]}
        apres_blanche = False
    return coupes


def plan(pages: list[dict], coupes: dict[int, dict]) -> dict:
    """Documents (listes de pages), pages personnelles et blanches, à partir des coupes."""
    documents, courant = [], None
    for p in pages:
        if p["blanche"] or p["sensible"]:
            continue
        c = coupes.get(p["page"]) or {"nouveau": False}
        if courant is None or c.get("nouveau") or len(courant["pages"]) >= PARTIE_MAX:
            suite = courant is not None and not c.get("nouveau")
            courant = {"pages": [], "titre": c.get("titre") or (courant["titre"] if suite else None) or p["titre"],
                       "matiere": c.get("matiere") or (courant["matiere"] if suite else None) or p["matiere"],
                       "kdrive_id": None}
            documents.append(courant)
        courant["pages"].append(p["page"])
    sensibles = {}
    for p in pages:  # pages personnelles, séparées entre sûres (corbeille) et douteuses (ton avis)
        if p["sensible"]:
            g = sensibles.setdefault(p["sensible"]["niveau"], {"pages": [], "categorie": p["sensible"]["categorie"],
                                                               "raisons": [], "fait": False})
            g["pages"].append(p["page"])
            g["raisons"] += [r for r in p["sensible"]["raisons"] if r not in g["raisons"]]
    return {"documents": documents, "sensibles": sensibles, "blanches": [p["page"] for p in pages if p["blanche"]]}


def extraire(chemin: Path, numeros: list[int]) -> bytes:
    """Pages choisies (1 = première) du PDF d'origine, sans perte de qualité."""
    import io

    from pypdf import PdfReader, PdfWriter
    lecteur, sortie = PdfReader(chemin), PdfWriter()
    for n in numeros:
        sortie.add_page(lecteur.pages[n - 1])
    tampon = io.BytesIO()
    sortie.write(tampon)
    return tampon.getvalue()


def nom_partie(nom: str, k: int, total: int, doc: dict) -> str:
    base = rangement.slug(Path(nom).stem)[:30] or "scan"
    titre = rangement.slug(doc.get("titre") or "")[:40]
    return f"{base}-{k:0{len(str(total))}d}{('-' + titre) if titre else ''}.pdf"


def plages(numeros: list[int]) -> str:
    """[3, 4, 5, 9] → « 3-5, 9 »."""
    morceaux, debut = [], None
    for i, n in enumerate(numeros):
        if debut is None:
            debut = n
        if i + 1 == len(numeros) or numeros[i + 1] != n + 1:
            morceaux.append(f"{debut}-{n}" if n != debut else str(n))
            debut = None
    return ", ".join(morceaux)
