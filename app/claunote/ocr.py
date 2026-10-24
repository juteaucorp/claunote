"""Lecture locale d'un fichier, sans IA : texte des PDF, OCR des scans, pages blanches.

Sert à décider avant d'envoyer quoi que ce soit à Claude (documents personnels, découpage des
gros scans). Outils du conteneur : poppler (pdftotext, pdftoppm) et Tesseract (français).
Sans Tesseract (poste de développement), l'OCR rend un texte vide : seuls le nom du fichier
et le texte déjà présent dans les PDF sont lus.
"""
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

IMAGES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp"}
TEXTE_MIN = 40          # caractères : en dessous, la page est un scan sans texte, on fait l'OCR
RESOLUTION_OCR = 150    # dpi : assez pour lire un IBAN, rapide (~1 s par page)
PARALLELE = max(1, min(4, (os.cpu_count() or 2)))


def disponible() -> bool:
    return shutil.which("tesseract") is not None


def nb_pages(chemin: Path) -> int:
    from pypdf import PdfReader
    try:
        return len(PdfReader(chemin).pages)
    except Exception:
        return 0


def texte_pdf(chemin: Path, premiere: int = 1, derniere: int | None = None) -> list[str]:
    """Texte déjà présent dans le PDF, une entrée par page (vide pour une page scannée)."""
    cmd = ["pdftotext", "-layout", "-f", str(premiere)] + (["-l", str(derniere)] if derniere else []) + [str(chemin), "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return []
    pages = p.stdout.decode("utf-8", errors="replace").split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    return pages


def rendre_pages(chemin: Path, dossier: Path, premiere: int, derniere: int, dpi: int = RESOLUTION_OCR) -> list[Path]:
    """Pages premiere..derniere (1 = première) en PNG gris, dans l'ordre."""
    dossier.mkdir(parents=True, exist_ok=True)
    prefixe = dossier / f"p{premiere:05d}"
    subprocess.run(["pdftoppm", "-r", str(dpi), "-gray", "-png", "-f", str(premiere), "-l", str(derniere),
                    str(chemin), str(prefixe)], capture_output=True, timeout=600, check=False)
    return sorted(dossier.glob(f"{prefixe.name}*.png"))


def ocr_image(chemin: Path) -> str:
    """Texte d'une image (français), vide si Tesseract manque ou échoue."""
    if not disponible():
        return ""
    try:
        p = subprocess.run(["tesseract", str(chemin), "stdout", "-l", "fra", "--psm", "3"], capture_output=True,
                           timeout=120, env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.decode("utf-8", errors="replace")


def image_pour_ocr(chemin: Path, dossier: Path, cote: int = 2000) -> Path:
    """Photo (même HEIC) redressée, en gris et réduite : Tesseract va bien plus vite."""
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    image = ImageOps.exif_transpose(Image.open(chemin)).convert("L")
    image.thumbnail((cote, cote))
    sortie = dossier / (chemin.stem + "-ocr.png")
    image.save(sortie)
    return sortie


def encre(chemin: Path) -> float:
    """Part de pixels foncés d'une page rendue : presque 0 pour une page blanche (ou un intercalaire)."""
    from PIL import Image
    image = Image.open(chemin).convert("L")
    image.thumbnail((400, 400))
    histo = image.histogram()
    return sum(histo[:160]) / max(1, sum(histo))


def blanche(taux_encre: float) -> bool:
    return taux_encre < 0.004


def lire_pdf(chemin: Path, pages: list[int] | None = None, avec_rendu=None) -> dict[int, dict]:
    """{page (1…): {"texte", "encre"}} ; OCR des seules pages sans texte, en parallèle.

    Sans `avec_rendu`, une page qui a déjà du texte n'est pas rendue (encre inconnue : 1).
    Avec, chaque page est rendue et `avec_rendu(page, png)` appelé (miniatures du découpage).
    """
    total = nb_pages(chemin)
    pages = pages or list(range(1, total + 1))
    resultat: dict[int, dict] = {}
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(PARALLELE) as pool:
        # Par tranches de 20 pages : 300 pages rendues d'un coup prendraient des centaines de Mo
        for debut in range(0, len(pages), 20):
            tranche = pages[debut:debut + 20]
            textes = {}
            for n in tranche:
                t = texte_pdf(chemin, n, n)
                textes[n] = t[0] if t else ""
            rendus = []
            for n in tranche:
                if not avec_rendu and len(textes[n].strip()) >= TEXTE_MIN:
                    rendus.append((n, None))
                    continue
                png = rendre_pages(chemin, Path(tmp) / str(n), n, n)
                rendus.append((n, png[0] if png else None))
            a_lire = [(n, png) for n, png in rendus if png and len(textes[n].strip()) < TEXTE_MIN]
            for (n, _), texte in zip(a_lire, pool.map(lambda r: ocr_image(r[1]), a_lire)):
                textes[n] = texte
            for n, png in rendus:
                resultat[n] = {"texte": textes[n], "encre": encre(png) if png else 1.0}
                if png and avec_rendu:
                    avec_rendu(n, png)
                if png:
                    png.unlink(missing_ok=True)
    return resultat


def lire_image(chemin: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            return ocr_image(image_pour_ocr(chemin, Path(tmp)))
        except Exception:
            return ""
