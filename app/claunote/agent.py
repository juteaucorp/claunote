"""Analyse d'un scan avec Claude Code en mode non interactif (`claude -p`).

Utilise le forfait Claude Pro via CLAUDE_CODE_OAUTH_TOKEN : pas de clé API.
Une seule passe par document : transcription Markdown + classement en JSON.
"""
import json
import os
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
import datetime as dt
from pathlib import Path
from xml.etree import ElementTree

MODELE = os.environ.get("CLAUDE_MODEL", "sonnet")
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

LIMITE = re.compile(r"usage limit|rate limit|limit reached|too many requests|\b429\b", re.I)


class LimiteAtteinte(Exception):
    """Limite d'usage du forfait atteinte : réessayer plus tard."""


class ErreurAgent(Exception):
    pass


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


def appeler_claude(prompt: str, cwd: Path, outils: list[str] | None = None,
                   entree: str | None = None, timeout: int = 1800) -> str:
    """Lance `claude -p` et renvoie le texte de la réponse.

    `entree` est passé sur l'entrée standard (Claude Code l'ajoute au prompt).
    Lève LimiteAtteinte si la limite d'usage du forfait est atteinte.
    """
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", MODELE]
    if outils:
        cmd += ["--allowedTools", ",".join(outils)]
    try:
        p = subprocess.run(cmd, cwd=cwd, input=entree, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ErreurAgent(f"Claude Code trop long ({timeout // 60} min)") from e
    try:
        sortie = json.loads(p.stdout)
    except json.JSONDecodeError:
        sortie = {"is_error": True, "result": p.stdout}
    texte = str(sortie.get("result") or "")
    if p.returncode != 0 or sortie.get("is_error"):
        message = f"{texte} {p.stderr}".strip()
        if LIMITE.search(message):
            raise LimiteAtteinte(message[:300])
        raise ErreurAgent(message[:500] or f"code de sortie {p.returncode}")
    return texte


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


def analyser(chemin: Path, dossiers: list[str] | None = None) -> Analyse:
    nom = chemin.name
    if chemin.suffix.lower() in OFFICE:
        try:
            texte = texte_office(chemin)
        except (zipfile.BadZipFile, ElementTree.ParseError, KeyError) as e:
            raise ErreurAgent(f"fichier {chemin.suffix} illisible : {e}") from e
        if not texte.strip():
            raise ErreurAgent(f"aucun texte dans {nom}")
        chemin.with_name(chemin.name + ".txt").write_text(texte)
        nom = chemin.name + ".txt"
    prompt = PROMPT.format(nom=nom, niveaux=", ".join(NIVEAUX), types=", ".join(TYPES))
    if nom != chemin.name:
        prompt += f"\nC'est le texte extrait du fichier bureautique `{chemin.name}` (mise en page perdue).\n"
    if dossiers and "Pronote" in dossiers:
        prompt += ("\nCe document a été déposé sur Pronote par un professeur (cours, sujet, corrigé…) : "
                   "ce n'est pas un scan, transcris son contenu de la même façon.\n")
    if dossiers:
        prompt += (f"\nIndice : l'élève a rangé ce fichier dans le dossier « {'/'.join(dossiers)} ». "
                   "Le nom du dossier indique souvent l'année d'étude, parfois la matière.\n")
    return parse_reponse(appeler_claude(prompt, chemin.parent, outils=["Read"]))


PROMPT_RECLASSER = """Voici la transcription d'un document d'un élève (du collège au BTS SIO), sur l'entrée.
Titre actuel : « {titre} ». {annee}
Classe-le. Réponds UNIQUEMENT avec un objet JSON valide, sans aucun texte autour :
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


def reclasser(doc: dict, matieres: list[str]) -> Analyse:
    """Nouveau classement d'un document déjà transcrit (pas de relecture du scan).

    Si le document a déjà une année d'étude, elle est imposée (choisie par l'élève).
    """
    if not (doc.get("transcription") or "").strip():
        raise ErreurAgent("pas de transcription : réessaie d'abord l'analyse du scan")
    annee = (f"L'élève a indiqué l'année d'étude : {doc['niveau']}. Garde-la." if doc.get("niveau") else "")
    prompt = PROMPT_RECLASSER.format(titre=doc.get("titre") or doc["nom"], annee=annee, niveaux=", ".join(NIVEAUX),
                                     types=", ".join(TYPES), matieres=", ".join(matieres) or "aucune")
    with tempfile.TemporaryDirectory() as vide:
        texte = appeler_claude(prompt, Path(vide), entree=doc["transcription"][:30_000], timeout=600)
    a = parse_reponse(texte)  # sans transcription : celle du document ne change pas
    if doc.get("niveau"):
        a.niveau = doc["niveau"]
    return a
