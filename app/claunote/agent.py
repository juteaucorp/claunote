"""Analyse d'un scan avec Claude Code en mode non interactif (`claude -p`).

Utilise le forfait Claude Pro via CLAUDE_CODE_OAUTH_TOKEN : pas de clé API.
Une passe par document : transcription Markdown + classement en JSON, avec le modèle
de la tâche (modeles.py). Si l'agent doute, un modèle plus fort donne un second avis.

Les échecs sont triés pour que le worker sache quoi faire :
- LimiteAtteinte : limite du forfait, pause jusqu'à l'heure de reprise annoncée ;
- ClaudeIndisponible : panne, réseau, surcharge (déjà réessayé ici) : on réessaie plus tard,
  sans marquer le document en erreur ; ClaudeDeconnecte : jeton refusé ou expiré ;
- ErreurAgent : le document lui-même pose problème (illisible, réponse invalide).
"""
import json
import re
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
import datetime as dt
from pathlib import Path
from xml.etree import ElementTree

from claunote import modeles

SEUIL_CONFIANCE = 0.7
NIVEAUX = ["6e", "5e", "4e", "3e", "Seconde", "Première", "Terminale", "BTS1", "BTS2"]
TYPES = ["Cours", "TP", "Exercices", "Évaluation", "Notes perso"]

PROMPT = """Tu analyses un document scanné d'un élève (du collège au BTS SIO) : le fichier `{nom}`.
Lis-le en entier avec l'outil Read (s'il a plus de 10 pages, lis-le par tranches de pages).

Réponds UNIQUEMENT avec un objet JSON valide, sans aucun texte autour :
{{"transcription": "...", "titre": "...", "niveau": "...", "matiere": "...", "type": "...",
  "chapitre": "...", "date": "AAAA-MM-JJ ou null", "mots_cles": ["..."], "resume": "...",
  "hors_cours": false, "confiance": 0.0}}

Règles :
- transcription : tout le texte, fidèlement, en Markdown. Formules en LaTeX ($...$), schémas
  décrits entre [crochets], passages illisibles notés [illisible]. N'invente rien.
- titre : court et parlant (ex. « Suites géométriques »).
- niveau : exactement un parmi {niveaux}.
- matiere : nom court en français (Mathématiques, Français, Anglais, Histoire-Géographie, SVT,
  Physique-Chimie, Réseaux, Systèmes, Développement, Cybersécurité, CEJM, …).
- type : exactement un parmi {types}.
- date : la date écrite sur le document, sinon null.
- resume : 2 ou 3 phrases sur ce que le document enseigne.
- hors_cours : true si le document ne sert pas à réviser : administratif (bulletin, relevé de notes,
  convocation, certificat, inscription, autorisation, emploi du temps, circulaire, règlement,
  attestation de stage…) ou sans contenu de cours (page blanche, brouillon vide, publicité…).
- confiance : de 0 à 1, ta certitude sur niveau + matière + type (ou sur hors_cours).
  Mets moins de 0.7 si tu hésites.
"""

LIMITE = re.compile(r"usage limit|limit reached|hit your .{0,40}limit|(session|weekly|5-hour) limit|"
                    r"out of (extra )?usage|rate.?limit|too many requests|\b429\b", re.I)
CONNEXION = re.compile(r"invalid api key|oauth|authenticat|unauthori[sz]ed|\b401\b|/login|not logged in|"
                       r"token (has )?expired|invalid.{0,20}credentials", re.I)
MODELE_REFUSE = re.compile(r"model.{0,80}(not (found|available|supported)|invalid|unknown|does not exist|no access)|"
                           r"not_found_error|(don't|do not) have access to", re.I)
PANNE = re.compile(r"overloaded|\b(500|502|503|504|529)\b|internal server error|connection (error|refused|reset)|"
                   r"econn|etimedout|enotfound|eai_again|fetch failed|network|socket hang up|service unavailable|"
                   r"temporarily", re.I)
ATTENTES_PANNE = (20, 90)  # secondes entre deux essais quand Claude ne répond pas


class LimiteAtteinte(Exception):
    """Limite d'usage du forfait atteinte : réessayer à `reprise` (None : heure inconnue)."""

    def __init__(self, message: str, reprise: dt.datetime | None = None, modele: str = ""):
        super().__init__(message)
        self.reprise, self.modele = reprise, modele


class ClaudeIndisponible(Exception):
    """Claude ne répond pas (panne, réseau, surcharge) : réessayer plus tard, rien n'est perdu."""


class ClaudeDeconnecte(ClaudeIndisponible):
    """Jeton CLAUDE_CODE_OAUTH_TOKEN refusé ou expiré : à refaire avec `claude setup-token`."""


class ModeleIndisponible(Exception):
    """Le forfait refuse ce modèle : on passe à celui du dessous."""


class ErreurAgent(Exception):
    pass


def journal(**appel):
    """Remplacé par le worker pour garder une trace de chaque appel (page État)."""


@dataclass
class Analyse:
    transcription: str
    titre: str | None = None
    niveau: str | None = None
    matiere: str | None = None
    type: str | None = None
    chapitre: str | None = None
    date: dt.date | None = None
    mots_cles: list[str] = field(default_factory=list)
    resume: str | None = None
    confiance: float = 0.0
    hors_cours: bool = False
    modele: str = ""     # modèle qui a lu le document
    avis: str = ""       # second avis d'un modèle plus fort, s'il y en a eu un


def _choix(valeur, options):
    if not isinstance(valeur, str):
        return None
    for o in options:
        if valeur.strip().lower() == o.lower():
            return o
    return None


def _date(valeur):
    try:
        return dt.date.fromisoformat(valeur) if valeur else None
    except (TypeError, ValueError):
        return None


def parse_reponse(texte: str) -> Analyse:
    """Extrait le JSON de la réponse (tolère des balises ```json autour)."""
    debut, fin = texte.find("{"), texte.rfind("}")
    if debut < 0 or fin < debut:
        raise ErreurAgent(f"pas de JSON dans la réponse : {texte[:200]!r}")
    try:
        d = json.loads(texte[debut:fin + 1])
    except json.JSONDecodeError as e:
        raise ErreurAgent(f"JSON invalide : {e}") from e
    niveau, type_ = _choix(d.get("niveau"), NIVEAUX), _choix(d.get("type"), TYPES)
    try:
        confiance = min(max(float(d.get("confiance") or 0), 0.0), 1.0)
    except (TypeError, ValueError):
        confiance = 0.0
    hors_cours = d.get("hors_cours") is True
    if not hors_cours and (niveau is None or type_ is None or not d.get("matiere")):
        confiance = min(confiance, 0.5)  # classement incomplet : à vérifier
    return Analyse(
        transcription=str(d.get("transcription") or ""),
        titre=d.get("titre") or None,
        niveau=niveau,
        matiere=(d.get("matiere") or "").strip() or None,
        type=type_,
        chapitre=d.get("chapitre") or None,
        date=_date(d.get("date")),
        mots_cles=[str(m) for m in d.get("mots_cles") or []],
        resume=d.get("resume") or None,
        confiance=confiance,
        hors_cours=hors_cours,
    )


def _classer_echec(message: str, code: int, modele: str) -> Exception:
    if LIMITE.search(message):
        return LimiteAtteinte(message[:300], modeles.reprise(message), modele)
    if CONNEXION.search(message):
        return ClaudeDeconnecte(message[:300])
    if MODELE_REFUSE.search(message):
        return ModeleIndisponible(message[:300])
    if PANNE.search(message) or not message:
        return ClaudeIndisponible(message[:300] or f"Claude Code s'est arrêté (code {code})")
    return ErreurAgent(message[:500])


def appeler_claude(prompt: str, cwd: Path, outils: list[str] | None = None,
                   entree: str | None = None, timeout: int = 1800, modele: str = "sonnet",
                   tache: str = "") -> str:
    """Lance `claude -p` et renvoie le texte de la réponse.

    `entree` est passé sur l'entrée standard (Claude Code l'ajoute au prompt).
    Une panne passagère est réessayée deux fois avant de lever ClaudeIndisponible.
    """
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", modele]
    if modele == "opus":
        cmd += ["--fallback-model", "sonnet"]  # Opus surchargé : Sonnet prend le relais
    if outils:
        cmd += ["--allowedTools", ",".join(outils)]
    for attente in (*ATTENTES_PANNE, None):
        debut = time.monotonic()
        try:
            p = subprocess.run(cmd, cwd=cwd, input=entree, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            journal(tache=tache, modele=modele, duree=timeout, resultat="erreur", detail="trop long")
            raise ErreurAgent(f"Claude Code trop long ({timeout // 60} min)") from e
        except OSError as e:  # binaire absent ou cassé
            journal(tache=tache, modele=modele, duree=0, resultat="panne", detail=str(e))
            raise ClaudeIndisponible(f"Claude Code introuvable : {e}") from e
        try:
            sortie = json.loads(p.stdout)
        except json.JSONDecodeError:
            sortie = {"is_error": True, "result": p.stdout}
        if not isinstance(sortie, dict):
            sortie = {"is_error": True, "result": p.stdout}
        texte = str(sortie.get("result") or "")
        duree = round(time.monotonic() - debut, 1)
        usage = sortie.get("usage") if isinstance(sortie.get("usage"), dict) else {}
        if p.returncode == 0 and not sortie.get("is_error"):
            journal(tache=tache, modele=modele, duree=duree, resultat="ok",
                    jetons_entree=sum(int(usage.get(k) or 0) for k in
                                      ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
                    jetons_sortie=int(usage.get("output_tokens") or 0))
            return texte
        echec = _classer_echec(f"{texte} {p.stderr}".strip(), p.returncode, modele)
        genre = {LimiteAtteinte: "limite", ClaudeDeconnecte: "connexion", ClaudeIndisponible: "panne",
                 ModeleIndisponible: "modele"}.get(type(echec), "erreur")
        journal(tache=tache, modele=modele, duree=duree, resultat=genre, detail=str(echec)[:300])
        if type(echec) is not ClaudeIndisponible or attente is None:
            raise echec
        time.sleep(attente)
    raise AssertionError("inatteignable")


def demander(tache: str, prompt: str, cwd: Path, lire=lambda texte: texte, outils: list[str] | None = None,
             entree: str | None = None, timeout: int = 1800, modele: str | None = None):
    """Appelle Claude avec le modèle prévu pour la tâche et lit la réponse avec `lire`.

    Réponse illisible (lire lève ErreurAgent) : un essai avec le modèle au-dessus.
    Modèle refusé par le forfait, ou limite propre à Opus : le modèle en dessous.
    Renvoie (réponse lue, modèle qui l'a produite).
    """
    modele = modele or modeles.choisir(tache)
    deja_monte = False
    while True:
        try:
            texte = appeler_claude(prompt, cwd, outils=outils, entree=entree, timeout=timeout, modele=modele, tache=tache)
        except ModeleIndisponible as e:
            modeles.marquer_indisponible(modele, None, "refusé par le forfait")
            modele = modeles.plus_faible(modele)
            if modele is None:
                raise ErreurAgent(f"aucun modèle accepté : {e}") from e
            continue
        except LimiteAtteinte as e:
            # Opus a sa propre limite : Sonnet peut souvent continuer, on essaie avant de tout mettre en pause
            if modele != "opus" or modeles.plus_faible(modele) is None:
                raise
            modeles.marquer_indisponible(modele, e.reprise or modeles._maintenant() + dt.timedelta(hours=1),
                                         "limite d'usage atteinte")
            modele = modeles.plus_faible(modele)
            continue
        try:
            return lire(texte), modele
        except ErreurAgent:
            fort = modeles.plus_fort(modele)
            if deja_monte or fort is None:
                raise
            journal(tache=tache, modele=fort, duree=0, resultat="escalade",
                    detail=f"réponse de {modeles.NOMS[modele]} illisible, nouvel essai")
            modele, deja_monte = fort, True


# Fichiers bureautiques (souvent déposés par les profs) : Claude Code ne sait pas les lire,
# on en extrait le texte dans un .txt à côté.
OFFICE = {
    ".docx": ("word/document.xml", "w:p"),
    ".pptx": ("ppt/slides/slide*.xml", "a:p"),
    ".xlsx": ("xl/sharedStrings.xml", "x:si"),
    ".odt": ("content.xml", "text:p"),
    ".odp": ("content.xml", "text:p"),
    ".ods": ("content.xml", "text:p"),
}
ESPACES_XML = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}


def texte_office(chemin: Path) -> str:
    """Texte d'un .docx/.pptx/.xlsx/.odt/.odp/.ods, un paragraphe par ligne (diapos dans l'ordre)."""
    motif, balise = OFFICE[chemin.suffix.lower()]
    prefixe, nom = balise.split(":")
    with zipfile.ZipFile(chemin) as z:
        noms = sorted((n for n in z.namelist() if Path(n).match(motif)),
                      key=lambda n: int(re.sub(r"\D", "", Path(n).stem) or 0))
        lignes = []
        for n in noms:
            racine = ElementTree.fromstring(z.read(n))
            if len(noms) > 1:
                lignes.append(f"\n--- {Path(n).stem} ---")
            for p in racine.iter(f"{{{ESPACES_XML[prefixe]}}}{nom}"):
                texte = "".join(p.itertext()).strip()
                if texte:
                    lignes.append(texte)
    return "\n".join(lignes)


# Anciens formats Office (.doc, .xls, .ppt) : texte extrait par les outils de catdoc
VIEUX_OFFICE = {".doc": ["catdoc", "-w"], ".rtf": ["catdoc", "-w"], ".xls": ["xls2csv"], ".ppt": ["catppt"]}


def texte_vieux_office(chemin: Path) -> str:
    try:
        p = subprocess.run([*VIEUX_OFFICE[chemin.suffix.lower()], str(chemin)], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ErreurAgent(f"fichier {chemin.suffix} illisible : {e}") from e
    if p.returncode != 0:
        raise ErreurAgent(f"fichier {chemin.suffix} illisible : {p.stderr.decode(errors='replace')[:200]}")
    return p.stdout.decode("utf-8", errors="replace")


def heic_en_jpeg(chemin: Path) -> Path:
    """Photo d'iPhone (HEIC) convertie en JPEG : Claude Code ne lit pas le HEIC."""
    from PIL import Image
    from pillow_heif import register_heif_opener
    register_heif_opener()
    jpeg = chemin.with_suffix(".jpg")
    Image.open(chemin).convert("RGB").save(jpeg, quality=90)
    return jpeg


def illisible(transcription: str) -> bool:
    """Beaucoup de passages notés [illisible] : un modèle plus fort relira le scan."""
    n = transcription.count("[illisible")
    return n >= 4 or (n >= 2 and n / max(1, len(transcription.split())) > 0.02)


def analyser(chemin: Path, dossiers: list[str] | None = None, indice: str = "",
             matieres: list[str] | None = None) -> Analyse:
    nom, tache = chemin.name, "scan"
    if chemin.suffix.lower() in (".heic", ".heif"):
        try:
            nom = heic_en_jpeg(chemin).name
        except Exception as e:
            raise ErreurAgent(f"photo HEIC illisible : {e}") from e
    elif chemin.suffix.lower() in VIEUX_OFFICE:
        texte = texte_vieux_office(chemin)
        if not texte.strip():
            raise ErreurAgent(f"aucun texte dans {nom}")
        chemin.with_name(chemin.name + ".txt").write_text(texte)
        nom, tache = chemin.name + ".txt", "texte"
    elif chemin.suffix.lower() in OFFICE:
        try:
            texte = texte_office(chemin)
        except (zipfile.BadZipFile, ElementTree.ParseError, KeyError) as e:
            raise ErreurAgent(f"fichier {chemin.suffix} illisible : {e}") from e
        if not texte.strip():
            raise ErreurAgent(f"aucun texte dans {nom}")
        chemin.with_name(chemin.name + ".txt").write_text(texte)
        nom, tache = chemin.name + ".txt", "texte"
    prompt = PROMPT.format(nom=nom, niveaux=", ".join(NIVEAUX), types=", ".join(TYPES))
    if tache == "texte":
        prompt += f"\nC'est le texte extrait du fichier bureautique `{chemin.name}` (mise en page perdue).\n"
    if dossiers and "Pronote" in dossiers:
        prompt += ("\nCe document a été déposé sur Pronote par un professeur (cours, sujet, corrigé…) : "
                   "ce n'est pas un scan, transcris son contenu de la même façon.\n")
    indices = ""
    if dossiers:
        indices += (f"Indice : l'élève a rangé ce fichier dans le dossier « {'/'.join(dossiers)} ». "
                    "Le nom du dossier indique souvent l'année d'étude, parfois la matière.\n")
    if indice:
        indices += f"D'après l'organisation de ses dossiers (imparfaite, à confirmer par le contenu) : {indice}.\n"
    if indices:
        prompt += "\n" + indices
    a, modele = demander(tache, prompt, chemin.parent, lire=parse_reponse, outils=["Read"])
    a.modele = modele
    if a.confiance >= SEUIL_CONFIANCE or (fort := modeles.plus_fort(modele)) is None:
        return a
    # L'agent doute : un modèle plus fort relit le scan s'il était mal lisible,
    # sinon donne un second avis sur le classement à partir du texte (bien moins coûteux).
    if tache == "scan" and illisible(a.transcription):
        try:
            b, m = demander(tache, prompt, chemin.parent, lire=parse_reponse, outils=["Read"], modele=fort)
        except ErreurAgent:
            return a
        if b.confiance >= a.confiance:
            b.modele, b.avis = m, f"Relu par {modeles.NOMS[m]} (scan peu lisible pour {modeles.NOMS[modele]})."
            return b
        return a
    try:
        c = classer(a.transcription, a.titre or chemin.name, matieres or [], indices=indices, modele=fort)
    except ErreurAgent:
        return a
    if c.confiance > a.confiance:
        a.avis = (f"{modeles.NOMS[modele]} hésitait ({a.confiance:.0%}) ; second avis de {modeles.NOMS[c.modele]} : "
                  f"{c.matiere or '?'} / {c.niveau or '?'} / {c.type or '?'} ({c.confiance:.0%}).")
        a.niveau, a.matiere, a.type = c.niveau, c.matiere, c.type
        a.chapitre, a.hors_cours, a.confiance = c.chapitre or a.chapitre, c.hors_cours, c.confiance
    return a


PROMPT_RECLASSER = """Voici la transcription d'un document d'un élève (du collège au BTS SIO), sur l'entrée.
Titre actuel : « {titre} ». {annee}
{indices}Classe-le. Réponds UNIQUEMENT avec un objet JSON valide, sans aucun texte autour :
{{"niveau": "...", "matiere": "...", "type": "...", "chapitre": "...", "titre": "...",
  "hors_cours": false, "confiance": 0.0}}

Règles :
- niveau : exactement un parmi {niveaux}.
- matiere : nom court en français. Reprends de préférence une matière déjà utilisée : {matieres}.
- type : exactement un parmi {types}.
- hors_cours : true si le document ne sert pas à réviser (administratif : bulletin, convocation,
  certificat, inscription, emploi du temps, circulaire, attestation… ; ou sans contenu de cours).
- confiance : de 0 à 1. Mets moins de 0.7 si tu hésites.
"""


def classer(transcription: str, titre: str, matieres: list[str], annee: str = "", indices: str = "",
            modele: str | None = None) -> Analyse:
    """Classement d'un texte déjà transcrit (pas de relecture du scan). Analyse sans transcription."""
    prompt = PROMPT_RECLASSER.format(titre=titre, annee=annee, indices=indices, niveaux=", ".join(NIVEAUX),
                                     types=", ".join(TYPES), matieres=", ".join(matieres) or "aucune")
    with tempfile.TemporaryDirectory() as vide:
        a, m = demander("classement", prompt, Path(vide), lire=parse_reponse, entree=transcription[:30_000],
                        timeout=600, modele=modele)
    a.modele = m
    return a


def reclasser(doc: dict, matieres: list[str]) -> Analyse:
    """Nouveau classement d'un document déjà transcrit, demandé depuis la page Organiser.

    Commence par le modèle léger et monte d'un cran tant que l'agent doute.
    Si le document a déjà une année d'étude, elle est imposée (choisie par l'élève).
    """
    if not (doc.get("transcription") or "").strip():
        raise ErreurAgent("pas de transcription : réessaie d'abord l'analyse du scan")
    annee = (f"L'élève a indiqué l'année d'étude : {doc['niveau']}. Garde-la." if doc.get("niveau") else "")
    args = (doc["transcription"], doc.get("titre") or doc["nom"], matieres, annee)
    a = classer(*args)
    dernier = a.modele
    while a.confiance < SEUIL_CONFIANCE and (fort := modeles.plus_fort(dernier)):
        b = classer(*args, modele=fort)
        dernier = b.modele
        if b.confiance >= a.confiance:
            b.avis = f"{modeles.NOMS[a.modele]} hésitait ({a.confiance:.0%}), classé par {modeles.NOMS[b.modele]}."
            a = b
    if doc.get("niveau"):
        a.niveau = doc["niveau"]
    return a


PROMPT_ARBORESCENCE = """Voici, sur l'entrée, l'arborescence d'un dossier de cours importé par un élève
(du collège au BTS SIO, il est aujourd'hui en BTS SIO). Une ligne par dossier, avec le nombre de documents.
Son organisation est ancienne et imparfaite : noms abrégés, années scolaires (« 2021-2022 ») au lieu
de l'année d'étude, matières mélangées, dossiers « divers »…

Déduis pour chaque dossier ce qu'il contient le plus probablement. Pour les années scolaires, sers-toi
des dossiers voisins (ex. « 2023-2024 » à côté de « BTS2 » et « 2024-2025 ») ; laisse null si tu ne sais pas.
Réponds UNIQUEMENT avec un objet JSON valide, sans aucun texte autour :
{{"resume": "2 phrases sur la façon dont c'est rangé", "dossiers": {{"chemin/du/dossier":
  {{"niveau": "... ou null", "matiere": "... ou null", "type": "... ou null"}}}}}}

Règles :
- niveau : exactement un parmi {niveaux}, ou null.
- matiere : nom court en français (Mathématiques, Français, Anglais, Réseaux, Systèmes, Développement,
  Cybersécurité, CEJM, …), ou null. Reprends de préférence : {matieres}.
- type : exactement un parmi {types}, ou null.
- n'invente rien : null vaut mieux qu'une supposition sans indice.
"""


def interpreter_arborescence(dossiers: dict[str, int], matieres: list[str]) -> tuple[str, dict[str, dict]]:
    """Une seule passe texte, peu coûteuse, sur les noms de dossiers d'un import (pas sur les fichiers).

    Renvoie (résumé, {chemin: {niveau, matiere, type}}).
    """
    lignes = "\n".join(f"{chemin or '(racine)'} ({n})" for chemin, n in sorted(dossiers.items()))
    prompt = PROMPT_ARBORESCENCE.format(niveaux=", ".join(NIVEAUX), types=", ".join(TYPES),
                                        matieres=", ".join(matieres) or "aucune")
    def lire(texte: str) -> dict:
        debut, fin = texte.find("{"), texte.rfind("}")
        try:
            d = json.loads(texte[debut:fin + 1])
        except (json.JSONDecodeError, ValueError) as e:
            raise ErreurAgent(f"JSON invalide : {e}") from e
        if not isinstance(d, dict):
            raise ErreurAgent("réponse sans objet JSON")
        return d

    with tempfile.TemporaryDirectory() as vide:
        d, _ = demander("dossiers", prompt, Path(vide), lire=lire, entree=lignes[:40_000], timeout=600)
    compris = {}
    for chemin, v in (d.get("dossiers") or {}).items():
        if chemin in dossiers and isinstance(v, dict):
            compris[chemin] = {"niveau": _choix(v.get("niveau"), NIVEAUX), "type": _choix(v.get("type"), TYPES),
                               "matiere": (v.get("matiere") or "").strip() or None if isinstance(v.get("matiere"), str) else None}
    return str(d.get("resume") or ""), compris
