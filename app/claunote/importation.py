"""Tri d'un dossier importé en entier, avant toute analyse par l'IA (heuristiques gratuites).

Pour chaque fichier, à partir de son chemin, de son nom et de sa taille :
- catégorie : document à garder, ou fichier inutile (code, config, fichier système, autre) ;
- doublons : même document en PDF et en format éditable (un seul est analysé, l'autre suit),
  ou copie exacte (même nom, même taille) ;
- scans « une page = un fichier » : regroupés pour être fusionnés en un seul PDF ;
- indices de classement lus dans les dossiers (année, matière, type).
"""
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

from claunote import rangement

PDF = {".pdf"}
EDITABLES = {".docx", ".doc", ".odt", ".rtf", ".pptx", ".ppt", ".odp", ".xlsx", ".xls", ".ods"}
# Texte extrait sans perte par agent.texte_office : préférés au PDF pour l'analyse (moins coûteux)
EDITABLES_LISIBLES = {".docx", ".odt", ".xlsx", ".ods", ".doc", ".xls"}
IMAGES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp"}
TEXTES = {".txt", ".md"}
# Code d'un TP ou d'un exercice (BTS SIO) : lu comme un document s'il est seul, hors d'un projet de code
SOURCES = {".py", ".sql", ".ipynb", ".html", ".htm", ".js", ".php", ".java", ".c", ".cpp", ".cs", ".sh", ".ps1",
           ".bat", ".vb", ".kt", ".r", ".css", ".ts"}
SOURCE_MAX = 300_000  # octets : au-delà, c'est un fichier généré ou une bibliothèque, pas un exercice
DOCUMENTS = PDF | EDITABLES | IMAGES | TEXTES | SOURCES

CODE = {".py", ".pyc", ".ipynb", ".js", ".mjs", ".ts", ".jsx", ".tsx", ".vue", ".html", ".htm", ".css", ".scss",
        ".php", ".java", ".class", ".jar", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".vb", ".go", ".rs", ".rb",
        ".swift", ".dart", ".lua", ".pl", ".r", ".sql", ".sh", ".bash", ".bat", ".cmd", ".ps1", ".asm", ".s",
        ".sln", ".csproj", ".vbproj", ".suo", ".user", ".pdb", ".exe", ".dll", ".so", ".o", ".obj", ".lib", ".a",
        ".json", ".xml", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".config", ".env", ".properties",
        ".lock", ".log", ".map", ".gitignore", ".gitattributes", ".editorconfig", ".twig", ".blade", ".pkt",
        ".db", ".sqlite", ".sqlite3", ".mdb", ".accdb", ".cache", ".tmp", ".bak", ".swp", ".pyo", ".whl"}
SYSTEME_NOMS = {".ds_store", "thumbs.db", "desktop.ini", "icon\r", ".localized", ".directory"}
# Dossiers d'outils de développement ou générés : rien d'utile à réviser dedans
DOSSIERS_INUTILES = {".vscode", ".idea", ".vs", ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
                     ".gradle", ".mvn", "vendor", "bin", "obj", "dist", "build", "target", "out", ".next",
                     ".nuxt", ".pytest_cache", ".mypy_cache", ".ipynb_checkpoints", "__macosx", ".trash",
                     ".settings", "debug", "release", "packages", ".dart_tool", ".angular", "coverage"}
# Fichiers qui signalent un projet de code : ses images et ses .md/.txt sont des fichiers du projet
MARQUEURS_PROJET = {"package.json", "composer.json", "pom.xml", "build.gradle", "requirements.txt", "pyproject.toml",
                    "makefile", "cmakelists.txt", "dockerfile", "docker-compose.yml", "cargo.toml", "go.mod",
                    "index.html", "index.php", "manage.py", "app.py", "main.py", "readme.md", ".gitignore"}
EXTENSIONS_PROJET = {".sln", ".csproj", ".vbproj"}  # un notebook (.ipynb) est un cours, pas un projet

LIBELLES = {"code": "Code ou configuration", "systeme": "Fichier système", "doublon": "Copie exacte",
            "projet": "Fichier d'un projet de code", "autre": "Format non lu"}


def sans_accents(texte: str) -> str:
    return unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode().lower()


def _mots(texte: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", sans_accents(texte)).strip() + " "


# --- Indices de classement lus dans les noms de dossiers et de fichiers -------------------

_MATIERES = [  # motif (mots entiers, sans accents) → matière (noms proposés à l'IA)
    (r"maths?|mathematiques?|algebre|geometrie|probas?|statistiques?", "Mathématiques"),
    (r"francais|litterature|culture g\w*|cge|cgle|expression", "Français"),
    (r"anglais|english|lv1 anglais", "Anglais"),
    (r"espagnol|spanish|esp", "Espagnol"),
    (r"allemand|german", "Allemand"),
    (r"histoire|geo|geographie|hg|hist geo|histoire geo\w*|emc", "Histoire-Géographie"),
    (r"svt|biologie|sciences de la vie", "SVT"),
    (r"physique|chimie|spc|physique chimie", "Physique-Chimie"),
    (r"philo|philosophie", "Philosophie"),
    (r"ses|economie|eco|droit|management|cejm|eco droit", "CEJM"),
    (r"nsi|snt|algorithmique|algo", "Informatique"),
    (r"techno|technologie", "Technologie"),
    (r"reseaux?|cisco|sisr|routage|vlan|tcp ip|packet tracer", "Réseaux"),
    (r"systemes?|linux|windows server|active directory|virtualisation|serveurs?", "Systèmes"),
    (r"dev|developpement|slam|programmation|python|java|php|web|bdd|sql|bases? de donnees|c sharp|csharp", "Développement"),
    (r"cyber\w*|securite|cybersecurite|bloc 3|b3", "Cybersécurité"),
    (r"bloc 1|b1|support|mise a disposition", "Support informatique"),
    (r"ateliers? de professionnalisation|stage|ppe", "Atelier de professionnalisation"),
]
_TYPES = [
    (r"tp|tps|travaux pratiques|labo|lab|atelier|projet", "TP"),
    (r"td|tds|exercices?|exos?|entrainements?|activites?|fiches? d exercices", "Exercices"),
    (r"ds|dst|controles?|interros?|interrogations?|evals?|evaluations?|examens?|partiels?|bac blanc|brevet blanc|"
     r"bts blanc|devoirs? surveilles?|qcm|sujets?|epreuves?", "Évaluation"),
    (r"cours|lecons?|chapitres?|chap|ch\d+|synthese|resume|memo|fiches?", "Cours"),
    (r"notes?|brouillons?|perso|personnel", "Notes perso"),
]
_ANNEE_SCOLAIRE = re.compile(r"(20\d\d)\s*[-_/ ]\s*(20\d\d|\d\d)")


def _chercher(table, textes: list[str]):
    for texte in textes:
        mots = _mots(texte)
        for motif, valeur in table:
            if re.search(rf" (?:{motif})\d* ", mots):
                return valeur
    return None


def indices(dossiers: list[str], nom: str = "") -> dict:
    """Année, matière, type et année scolaire devinés depuis le chemin (le plus profond d'abord)."""
    profonds = list(reversed(dossiers))
    annee = None
    for d in profonds:
        m = _ANNEE_SCOLAIRE.search(d)
        if m:
            fin = m.group(2) if len(m.group(2)) == 4 else m.group(1)[:2] + m.group(2)
            annee = f"{m.group(1)}-{fin}"
            break
    stem = PurePosixPath(nom).stem
    return {
        "niveau": rangement.niveau_depuis_dossier(dossiers),
        "matiere": _chercher(_MATIERES, profonds + [stem]),
        # Le dossier racine d'un import (« Mes cours », « Vieux TP ») parle de tout le lot, pas d'un type
        "type": _chercher(_TYPES, [stem] + (profonds[:-1] if len(dossiers) > 1 else profonds)),
        "annee": annee,
    }


def texte_indices(dossiers: list[str], nom: str, compris: dict | None = None) -> str:
    """Phrase d'indice pour le prompt d'analyse, à partir des heuristiques et de l'organisation comprise par l'IA."""
    i = indices(dossiers, nom)
    for cle, valeur in (compris or {}).items():
        if valeur and not i.get(cle):
            i[cle] = valeur
    parts = [f"{libelle} probable : {i[cle]}" for cle, libelle in
             (("niveau", "année d'étude"), ("matiere", "matière"), ("type", "type"), ("annee", "année scolaire"))
             if i.get(cle)]
    return " ; ".join(parts)


# --- Tri d'un lot de fichiers ----------------------------------------------------------

@dataclass
class Fichier:
    id: int
    nom: str
    dossiers: list[str]
    taille: int = 0
    categorie: str = "document"     # document, code, systeme, autre, doublon, projet
    decision: str = "analyser"      # analyser, inutile, variante, page
    raison: str | None = None
    principal: int | None = None    # variante : fichier analysé à sa place
    groupe: str | None = None       # page : clé du groupe de scans à fusionner
    ordre: int = 0
    sur: bool = True                # page : fusion sûre, sinon à confirmer
    dossier: bool = False           # dossier d'outil (.vscode, node_modules…) gardé en un bloc

    @property
    def chemin(self) -> str:
        return "/".join(self.dossiers + [self.nom])

    @property
    def ext(self) -> str:
        return PurePosixPath(self.nom).suffix.lower()


def _projets(fichiers: list[Fichier]) -> set[tuple]:
    """Dossiers (chemins) qui contiennent un projet de code."""
    projets = set()
    for f in fichiers:
        if f.dossier and f.nom.lower() in (".vscode", ".git", ".idea", "node_modules", ".vs"):
            projets.add(tuple(f.dossiers))
        if f.nom.lower() in MARQUEURS_PROJET or f.ext in EXTENSIONS_PROJET:
            projets.add(tuple(f.dossiers))
        for i, d in enumerate(f.dossiers):
            if d.lower() in (".vscode", ".git", ".idea", "node_modules", ".vs"):
                projets.add(tuple(f.dossiers[:i]))
    # Un dossier dont la majorité des fichiers est du code est aussi un projet
    par_dossier = defaultdict(list)
    for f in fichiers:
        if not f.dossier:
            par_dossier[tuple(f.dossiers)].append(f.ext in CODE)
    projets |= {d for d, codes in par_dossier.items() if len(codes) >= 3 and sum(codes) / len(codes) > 0.5}
    return projets


def categorie(f: Fichier, projets: set[tuple]) -> tuple[str, str | None]:
    """Catégorie d'un fichier et, s'il est inutile, la raison à afficher."""
    nom, ext = f.nom.lower(), f.ext
    if f.dossier:
        return "code", f"dossier « {f.nom} » d'outil de développement, avec tout son contenu"
    dossier_inutile = next((d for d in f.dossiers if d.lower() in DOSSIERS_INUTILES), None)
    if nom in SYSTEME_NOMS or nom.startswith(("~$", "._", ".~lock")) or ext in {".lnk", ".url", ".tmp"}:
        return "systeme", "fichier créé par Windows, macOS ou Office"
    if dossier_inutile:
        return "code", f"dans le dossier « {dossier_inutile} » (outil de développement)"
    dans_projet = any(tuple(f.dossiers[:i]) in projets for i in range(len(f.dossiers) + 1))
    if ext in SOURCES and not dans_projet and f.taille <= SOURCE_MAX:
        return "document", None  # script SQL d'un TP, notebook, page HTML d'exercice : du cours
    if ext in CODE or (not ext and nom.startswith(".")):
        return "code", "code source ou fichier de configuration"
    if ext not in DOCUMENTS:
        return "autre", f"format {ext or 'sans extension'} non lu par l'agent"
    if dans_projet and ext in IMAGES | TEXTES:
        return "projet", "image ou texte d'un projet de code (icône, README…)"
    if ext in IMAGES and 0 < f.taille < 15_000:
        return "systeme", "image minuscule (icône, logo)"
    return "document", None


_COPIE = re.compile(r"(\s*[-_]?\s*(\(\d+\)|copie( \d+)?|copy( \d+)?|- copie))+$", re.I)


def cle_nom(nom: str) -> str:
    """Nom sans extension, accents, majuscules ni marque de copie (« TP1 (1) » = « tp1 »)."""
    stem = _COPIE.sub("", PurePosixPath(nom).stem)
    return re.sub(r"[^a-z0-9]+", "", sans_accents(stem))


def _doublons(docs: list[Fichier]):
    """Copies exactes (même nom, même taille).

    Gardée : celle sans marque de copie, hors d'un dossier « copie/sauvegarde », la mieux rangée (chemin profond).
    """
    vus: dict[tuple, Fichier] = {}

    def preference(f: Fichier):
        sauvegarde = any(re.search(r"copie|copy|sauvegarde|backup|old|ancien", sans_accents(d)) for d in f.dossiers)
        return (bool(_COPIE.search(PurePosixPath(f.nom).stem)), sauvegarde, -len(f.dossiers), len(f.nom), f.chemin)

    for f in sorted(docs, key=preference):
        cle = (cle_nom(f.nom), f.ext, f.taille)
        if f.taille and cle in vus:
            f.categorie, f.decision = "doublon", "inutile"
            f.raison, f.principal = f"copie exacte de « {vus[cle].chemin} »", vus[cle].id
        else:
            vus[cle] = f


def _variantes(docs: list[Fichier]):
    """Même document en PDF et en éditable dans le même dossier : un seul est analysé."""
    groupes = defaultdict(list)
    for f in docs:
        if f.decision == "analyser" and f.ext in PDF | EDITABLES:
            groupes[(tuple(f.dossiers), cle_nom(f.nom))].append(f)
    for groupe in groupes.values():
        pdfs = [f for f in groupe if f.ext in PDF]
        editables = [f for f in groupe if f.ext in EDITABLES]
        if not pdfs or not editables:
            continue
        lisibles = [f for f in editables if f.ext in EDITABLES_LISIBLES]
        # Word/Excel : texte exact et moins coûteux à analyser. PowerPoint : le PDF garde les schémas.
        principal = lisibles[0] if lisibles else pdfs[0]
        for f in groupe:
            if f is not principal:
                f.decision, f.principal = "variante", principal.id
                f.raison = f"autre format de « {principal.nom} »"


_PAGE_MARQUEE = re.compile(r"^(.*?)(?:^|[\s_\-.(])(?:p|pg|page|pages)[\s_\-.]?(\d{1,4})\)?$")
_NUMERO = re.compile(r"^(.*?\D|)[\s_\-.(]*(\d{1,4})\)?$")
# Noms donnés par un appareil photo ou un scanner : rien ne dit où commence un document
_APPAREIL = re.compile(r"^(img|pxl|dsc|dscn|dcim|photo|scan|numerisation|whatsapp image|capture|cnvt)\b.*\d{6,}")
GENERIQUES = {"", "scan", "scans", "numerisation", "numerise", "img", "image", "photo", "dsc", "dscn", "pxl", "document",
              "doc", "sans titre", "untitled", "nouveau document", "cnvt", "capture", "whatsapp image", "camscanner"}
# « TP 1, TP 2 » ou « Chapitre 3 » : des documents différents, pas des pages
MOTS_NUMEROTES = {"tp", "td", "ds", "dst", "exo", "exercice", "exercices", "chapitre", "chap", "ch", "cours", "eval",
                  "evaluation", "controle", "interro", "partie", "seance", "semaine", "sujet", "annexe", "fiche",
                  "lecon", "module", "theme", "sequence", "activite", "devoir", "projet", "version", "v", "bloc"}


def _page(f: Fichier) -> tuple[str, int, bool] | None:
    """(préfixe, numéro, marque « page ») si le nom ressemble à une page numérotée."""
    stem = re.sub(r"[^a-z0-9()]+", " ", sans_accents(PurePosixPath(f.nom).stem)).strip()
    m = _PAGE_MARQUEE.match(stem)
    if m:
        return m.group(1).strip(" ()"), int(m.group(2)), True
    a = _APPAREIL.match(stem)
    if a:
        return a.group(1), 0, False  # numéroté ensuite dans l'ordre des noms (horodatage)
    m = _NUMERO.match(stem)
    if m:
        return m.group(1).strip(" ()"), int(m.group(2)), False
    return None


def _pages(docs: list[Fichier]):
    """Scans d'une page par fichier, dans un même dossier : à fusionner en un PDF.

    Sûr : « page 1, page 2… » ou des photos au nom parlant (« suites 1.jpg, suites 2.jpg »), sans trou.
    À confirmer : noms d'appareil photo ou de scanner (« IMG_2023… », « Scan 5 »), numéros manquants,
    « TP1.jpg, TP2.jpg » : ce sont peut-être des documents différents.
    Des PDF numérotés sans mot « page » (« TP1.pdf, TP2.pdf ») restent des documents séparés.
    """
    groupes = defaultdict(list)
    for f in docs:
        if f.decision != "analyser" or f.ext not in IMAGES | PDF:
            continue
        p = _page(f)
        if p:
            famille = "image" if f.ext in IMAGES else "pdf"
            groupes[(tuple(f.dossiers), p[0], famille)].append((p[1], p[2], f))
    for (dossiers, prefixe, famille), membres in groupes.items():
        if len(membres) < 2:
            continue
        marque = all(m[1] for m in membres)
        generique = prefixe in GENERIQUES or bool(_APPAREIL.match(prefixe + " 000000"))
        numerote = (prefixe.split() or [""])[-1] in MOTS_NUMEROTES
        if famille == "pdf" and not marque and not generique:
            continue
        membres.sort(key=lambda m: (m[0], m[2].nom))
        numeros = [n for n, _, _ in membres]
        continu = len(set(numeros)) == len(numeros) and numeros[-1] - numeros[0] == len(numeros) - 1
        # Un PDF de plusieurs Mo n'est sans doute pas une seule page
        petits = famille == "image" or all(f.taille < 2_000_000 for _, _, f in membres)
        sur = continu and petits and len(membres) <= 40 and (marque or not (generique or numerote))
        cle = "/".join(dossiers + (prefixe or "scan",))
        for ordre, (_, _, f) in enumerate(membres):
            f.decision, f.groupe, f.ordre, f.sur = "page", cle, ordre, sur
            f.raison = f"page {ordre + 1}/{len(membres)} de « {prefixe or 'scan'} »"


def trier(fichiers: list[Fichier]) -> list[Fichier]:
    """Décide du sort de chaque fichier d'un lot (modifie et renvoie la liste)."""
    projets = _projets(fichiers)
    for f in fichiers:
        f.categorie, f.raison = categorie(f, projets)
        f.decision = "analyser" if f.categorie == "document" else "inutile"
    docs = [f for f in fichiers if f.categorie == "document"]
    _doublons(docs)
    _variantes(docs)
    _pages(docs)
    return fichiers


def nom_fusion(groupe: str, pages: int) -> str:
    """Nom du PDF fusionné : le préfixe commun des pages, sinon le dossier."""
    parts = groupe.split("/")
    base = parts[-1] if parts[-1] not in GENERIQUES and not _APPAREIL.match(parts[-1] + " 000000") else (
        (parts[-2] if len(parts) > 1 else "scan") + " " + parts[-1])
    return f"{rangement.slug(base) or 'scan'}-{pages}p.pdf"


def decouper(ordres: list[int], coupes: set[int]) -> list[list[int]]:
    """Pages [0..n] découpées avant chaque index de `coupes` (choix fait sur le site)."""
    groupes, courant = [], []
    for o in ordres:
        if courant and o in coupes:
            groupes.append(courant)
            courant = []
        courant.append(o)
    if courant:
        groupes.append(courant)
    return groupes


def fusionner_pdf(chemins: list) -> bytes:
    """Pages (photos ou PDF) réunies dans l'ordre en un seul PDF. Photos redressées et allégées."""
    import io

    from PIL import Image, ImageOps
    from pypdf import PdfReader, PdfWriter
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    sortie = PdfWriter()
    for chemin in chemins:
        if PurePosixPath(str(chemin)).suffix.lower() in PDF:
            for page in PdfReader(chemin).pages:
                sortie.add_page(page)
            continue
        image = ImageOps.exif_transpose(Image.open(chemin)).convert("RGB")
        image.thumbnail((2200, 2200))
        tampon = io.BytesIO()
        image.save(tampon, "PDF", resolution=200, quality=85)
        tampon.seek(0)
        for page in PdfReader(tampon).pages:
            sortie.add_page(page)
    resultat = io.BytesIO()
    sortie.write(resultat)
    return resultat.getvalue()


def miniature(chemin, taille: int = 480) -> bytes:
    """Aperçu JPEG d'une photo de scan, pour choisir où couper sur le site."""
    import io

    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    image = ImageOps.exif_transpose(Image.open(chemin)).convert("RGB")
    image.thumbnail((taille, taille))
    tampon = io.BytesIO()
    image.save(tampon, "JPEG", quality=75)
    return tampon.getvalue()
