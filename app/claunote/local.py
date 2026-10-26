"""Analyse d'un document sans Claude, entièrement sur la VM (processeur seul, pas de carte graphique).

Sert quand Claude n'est pas disponible (limite du forfait, panne, jeton expiré), ou tout le temps si
tu le choisis (page Analyses). Deux outils libres :

    fichier ──► texte ─────────────────────────────► classement
                │ Word, PowerPoint, PDF numérique :   │ Ollama (si installé) : petit modèle de langue
                │   texte extrait tel quel             │   local, qwen2.5:3b par défaut (~2 Go de RAM)
                │ scan, photo : OCR Tesseract          │ sinon : vote des cours déjà classés qui lui
                │   (français, page par page)          │   ressemblent le plus (recherche PostgreSQL)
                ▼                                      ▼
              transcription                    titre, année, matière, type, résumé, confiance

Plus lent et moins fin que Claude, surtout pour l'écriture manuscrite (Tesseract lit mal l'écriture à
la main) : la confiance est plafonnée, et le document peut être relu par Claude à son retour
(« Relire avec Claude », automatique si tu le laisses activé).
"""
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from pathlib import Path

import httpx

from claunote import agent, db, ocr
from claunote.agent import NIVEAUX, TYPES, Analyse, ErreurAgent, Interrompu

OLLAMA_URL = (os.environ.get("OLLAMA_URL") or "http://ollama:11434").rstrip("/")
OLLAMA_MODELE = os.environ.get("OLLAMA_MODELE") or "qwen2.5:3b"
PARALLELE = min(max(int(os.environ.get("LOCAL_PARALLELE") or 1), 1), 4)  # le CPU est vite saturé
TEXTE_MAX = 8_000          # caractères lus par le modèle local (au-delà, un CPU met plusieurs minutes)
CONFIANCE_MAX = 0.8        # un classement local n'est jamais tout à fait sûr
CONFIANCE_VOISINS = 0.75   # vote unanime des cours voisins : juste au-dessus du seuil de classement
MODES = {"auto": "Quand Claude n'est pas disponible", "toujours": "Toujours (jamais Claude pour les analyses)",
         "jamais": "Jamais (attendre Claude)"}
MOTS_VIDES = set("""alors aussi autre avec avoir cela cette ceux comme dans donc elle elles entre est etre fait faire
leur leurs mais meme nous pour plus sans sont sous tout tous tres vous avez peut quand quel quelle
quels quelles chaque deux trois page exercice question reponse cours corrige""".split())


def _simple(texte: str) -> str:
    return unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode().lower()


# --- Ollama : modèle de langue local ---------------------------------------------------

_ollama = {"vu": 0.0, "pret": False, "message": "", "telechargement": False}
_verrou = threading.Lock()


def etat_ollama(reverifier: bool = False) -> dict:
    """{"pret", "message"} ; vérifié au plus toutes les 2 min. Télécharge le modèle s'il manque."""
    with _verrou:
        if not reverifier and time.monotonic() - _ollama["vu"] < 120:
            return dict(_ollama)
        _ollama["vu"] = time.monotonic()
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
        noms = {m.get("name") for m in r.json().get("models") or []}
    except Exception:
        _ollama.update(pret=False, message="Ollama n'est pas installé (classement par les cours voisins)")
        return dict(_ollama)
    if OLLAMA_MODELE in noms or f"{OLLAMA_MODELE}:latest" in noms:
        _ollama.update(pret=True, message=f"Ollama prêt ({OLLAMA_MODELE})")
    elif not _ollama["telechargement"]:
        _ollama.update(pret=False, telechargement=True, message=f"Téléchargement de {OLLAMA_MODELE}…")
        threading.Thread(target=_telecharger, daemon=True, name="ollama-pull").start()
    return dict(_ollama)


def _telecharger():
    try:
        httpx.post(f"{OLLAMA_URL}/api/pull", json={"model": OLLAMA_MODELE, "stream": False}, timeout=3600)
        _ollama.update(vu=0.0, message=f"{OLLAMA_MODELE} téléchargé")
    except Exception as e:
        _ollama.update(message=f"Téléchargement de {OLLAMA_MODELE} impossible : {e}")
    finally:
        _ollama["telechargement"] = False


def ollama(systeme: str, texte: str, json_seul: bool = True, timeout: int = 900) -> str:
    """Une réponse du modèle local (CPU : de quelques secondes à quelques minutes)."""
    corps = {"model": OLLAMA_MODELE, "stream": False, "keep_alive": "10m",
             "messages": [{"role": "system", "content": systeme}, {"role": "user", "content": texte}],
             "options": {"temperature": 0.1, "num_ctx": 8192}}
    if json_seul:
        corps["format"] = "json"
    debut = time.monotonic()
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/chat", json=corps, timeout=timeout)
        r.raise_for_status()
        reponse = r.json()["message"]["content"]
    except Exception as e:
        agent.journal(tache="local", modele="ollama", resultat="erreur", duree=round(time.monotonic() - debut, 1),
                      detail=str(e)[:300])
        raise ErreurAgent(f"Ollama : {e}") from e
    agent.journal(tache="local", modele="ollama", resultat="ok", duree=round(time.monotonic() - debut, 1))
    return reponse


# --- Texte du document -------------------------------------------------------------------

def transcrire(chemin: Path, arret=None) -> tuple[str, str]:
    """(texte, d'où il vient) sans IA. `arret()` vrai : on abandonne entre deux pages."""
    suffixe = chemin.suffix.lower()
    if suffixe in agent.VIEUX_OFFICE:
        return agent.markdown_office(agent.texte_vieux_office(chemin)), "fichier bureautique"
    if suffixe in agent.OFFICE:
        try:
            return agent.markdown_office(agent.texte_office(chemin)), "fichier bureautique"
        except Exception as e:
            raise ErreurAgent(f"fichier {suffixe} illisible : {e}") from e
    if suffixe in (".txt", ".md"):
        return chemin.read_text(errors="replace"), "fichier texte"
    if not ocr.disponible():
        raise ErreurAgent("Tesseract (OCR) n'est pas installé : impossible de lire un scan sans Claude")
    if suffixe == ".pdf":
        texte = agent.texte_pdf(chemin)
        if texte:
            return texte, "PDF numérique"
        total = ocr.nb_pages(chemin)
        if not total:
            raise ErreurAgent("PDF illisible")
        morceaux = []
        for debut in range(1, total + 1, 4):  # par 4 pages : l'arrêt demandé est pris en compte vite
            if arret and arret():
                raise Interrompu("analyse arrêtée")
            pages = ocr.lire_pdf(chemin, list(range(debut, min(debut + 3, total) + 1)))
            morceaux += [f"## Page {n}\n\n{p['texte'].strip()}" for n, p in sorted(pages.items()) if p["texte"].strip()]
        return "\n\n".join(morceaux), "OCR d'un scan (Tesseract)"
    if suffixe in (".heic", ".heif"):
        chemin = agent.heic_en_jpeg(chemin)
    return ocr.lire_image(chemin), "OCR d'une photo (Tesseract)"


def _titre(texte: str, nom: str) -> str:
    for ligne in texte.splitlines():
        ligne = re.sub(r"^#+\s*|[*_`]", "", ligne).strip()
        if 6 <= len(ligne) <= 80 and sum(c.isalpha() for c in ligne) >= 0.6 * len(ligne) and not ligne.startswith("Page "):
            return ligne
    return Path(nom).stem.replace("_", " ").replace("-", " ")[:80]


def _resume(texte: str) -> str:
    t = re.sub(r"\s+", " ", re.sub(r"^#+ .*$", "", texte, flags=re.M)).strip()
    return (t[:240].rsplit(" ", 1)[0] + "…") if len(t) > 240 else t


def mots_cles(texte: str, n: int = 25) -> list[str]:
    """Mots les plus fréquents (hors mots vides), pour chercher les cours qui ressemblent."""
    mots = [m for m in re.findall(r"[a-z0-9]{4,}", _simple(texte)) if m not in MOTS_VIDES and not m.isdigit()]
    return [m for m, _ in Counter(mots).most_common(n)]


TYPES_NOM = [("TP", r"\btp\d*\b|travaux pratiques|\blab"), ("Exercices", r"\bexo|exercice|\btd\d*\b"),
             ("Évaluation", r"\beval|controle|\bds\d*\b|interro|examen|partiel|bac blanc|bts blanc")]


def _type_par_nom(nom: str, texte: str) -> str | None:
    tete = _simple(f"{nom} {texte[:300]}")
    for type_, motif in TYPES_NOM:
        if re.search(motif, tete):
            return type_
    return None


def classer_par_voisins(texte: str, nom: str) -> Analyse:
    """Sans modèle de langue : les cours déjà classés les plus proches votent (pondérés par leur score)."""
    voisins = db.voisins(" | ".join(mots_cles(texte)), 12) if texte.strip() else []
    votes = {k: Counter() for k in ("niveau", "matiere", "type")}
    for v in voisins:
        for k in votes:
            if v.get(k):
                votes[k][v[k]] += float(v["score"]) or 0.01
    total = sum(votes["matiere"].values())
    matiere = votes["matiere"].most_common(1)[0][0] if total else None
    part = votes["matiere"][matiere] / total if total else 0.0
    type_ = _type_par_nom(nom, texte) or (votes["type"].most_common(1)[0][0] if votes["type"] else "Cours")
    return Analyse(transcription=texte, titre=_titre(texte, nom), matiere=matiere, type=type_,
                   niveau=votes["niveau"].most_common(1)[0][0] if votes["niveau"] else None,
                   resume=_resume(texte), mots_cles=mots_cles(texte, 6),
                   confiance=round(min(CONFIANCE_VOISINS, part * CONFIANCE_VOISINS) if len(voisins) >= 2 else 0.3 * part, 2))


def classer_par_ollama(texte: str, nom: str, origine: str, contexte: str) -> Analyse:
    prompt = agent.PROMPT_TEXTE.format(nom=nom, origine=origine, niveaux=", ".join(NIVEAUX),
                                       types=", ".join(TYPES)) + contexte
    a = agent.parse_reponse(ollama(prompt, texte[:TEXTE_MAX]))
    a.transcription = texte
    a.confiance = min(a.confiance, CONFIANCE_MAX)
    return a


def analyser(chemin: Path, dossiers: list[str] | None = None, indice: str = "", matieres: list[str] | None = None,
             memoire=None, arret=None) -> Analyse:
    """Même contrat qu'agent.analyser : transcription + classement, sans Claude."""
    texte, origine = transcrire(chemin, arret)
    if not texte.strip():
        raise ErreurAgent("aucun texte lisible sans Claude (écriture manuscrite ou image) : à relire avec Claude")
    if arret and arret():
        raise Interrompu("analyse arrêtée")
    outil = "voisins"
    if etat_ollama()["pret"]:
        contexte = ""
        if dossiers:
            contexte += f"\nIndice : le fichier était rangé dans le dossier « {'/'.join(dossiers)} ».\n"
        if indice:
            contexte += f"D'après ses dossiers : {indice}.\n"
        if matieres:
            contexte += f"Matières déjà utilisées : {', '.join(matieres)}. Reprends exactement l'un de ces noms s'il convient.\n"
        try:
            a, outil = classer_par_ollama(texte, chemin.name, origine, contexte), "ollama"
        except ErreurAgent:
            a = classer_par_voisins(texte, chemin.name)
    else:
        a = classer_par_voisins(texte, chemin.name)
    a.modele = "local"
    a.avis = (f"Analysé sans Claude : {origine}, classement par "
              + (f"Ollama ({OLLAMA_MODELE})." if outil == "ollama" else "ressemblance avec tes cours déjà classés."))
    return a

