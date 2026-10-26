"""Le modèle local : analyse des documents sur la VM (processeur seul, pas de carte graphique).

Par défaut (« local d'abord »), il fait le plus de travail possible et Claude n'intervient que pour
l'aider ou le vérifier (voir analyser) ; il sert aussi de secours quand Claude n'est pas disponible.
Deux outils libres :

    fichier ──► texte ─────────────────────────────► classement
                │ Word, PowerPoint, PDF numérique :   │ Ollama (si installé) : petit modèle de langue
                │   texte extrait tel quel             │   local, qwen2.5:3b par défaut (~2 Go de RAM)
                │ scan, photo : OCR Tesseract          │ sinon : vote des cours déjà classés qui lui
                │   (français, page par page)          │   ressemblent le plus (recherche PostgreSQL)
                ▼                                      ▼
              transcription                    titre, année, matière, type, résumé, confiance

Plus lent et moins fin que Claude, surtout pour l'écriture manuscrite (Tesseract lit mal l'écriture à
la main). D'où les appels à l'aide, quand Claude est disponible :

    lecture locale ──► OCR pauvre (manuscrit) ───────────► Claude lit le scan lui-même (BesoinClaude)
                   └─► classement local ──► confiance < 70 % ──► Claude vérifie et corrige (texte seul, Haiku)
                                        └─► sûr ──► gardé ; 1 fois sur 10 Claude contrôle quand même,
                                                    pour mesurer la fiabilité du local (page Activité)
Sans Claude, un document douteux est relu par Claude à son retour (« Relire avec Claude »).
"""
import json
import os
import random
import re
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from pathlib import Path

import httpx

from claunote import agent, db, lecture, ocr, ressources
from claunote.agent import (NIVEAUX, SEUIL_CONFIANCE, TYPES, Analyse, ClaudeIndisponible, ErreurAgent, Interrompu,
                            LimiteAtteinte)

OLLAMA_URL = (os.environ.get("OLLAMA_URL") or "http://ollama:11434").rstrip("/")
OLLAMA_MODELE = os.environ.get("OLLAMA_MODELE") or "qwen2.5:3b"
OLLAMA_EMBED = os.environ.get("OLLAMA_EMBED") or "nomic-embed-text"  # sens des passages (recherche), ~280 Mo
PARALLELE = min(max(int(os.environ.get("LOCAL_PARALLELE") or 1), 1), 4)  # le CPU est vite saturé
# Le modèle reste en mémoire ce temps après sa dernière réponse (~2,3 Go), puis Ollama le décharge
GARDER = os.environ.get("OLLAMA_GARDER") or "5m"
# Cœurs donnés à Ollama par calcul : tous sauf un, que le site et la base gardent toujours pour eux
THREADS = int(os.environ.get("OLLAMA_THREADS") or max(ressources.coeurs() - 1, 1))
TEXTE_MAX = 8_000          # caractères lus par le modèle local (au-delà, un CPU met plusieurs minutes)
CONFIANCE_MAX = 0.8        # un classement local n'est jamais tout à fait sûr
CONFIANCE_VOISINS = 0.75   # vote unanime des cours voisins : juste au-dessus du seuil de classement
CONTROLE = float(os.environ.get("LOCAL_CONTROLE") or 0.1)  # part des analyses locales sûres que Claude contrôle
MODES = {"local": "Local d'abord (recommandé) : Claude n'aide que si besoin et vérifie",
         "auto": "Claude d'abord, local seulement s'il est indisponible",
         "toujours": "Local uniquement (jamais Claude pour les analyses)",
         "jamais": "Claude uniquement (attendre Claude)"}
MODE_DEFAUT = "local"


def reglages() -> dict:
    """Réglages de la page Activité : qui analyse (MODES), relecture par Claude, pause des analyses."""
    mode = db.etat_valeur("analyse_locale") or os.environ.get("ANALYSE_LOCALE") or MODE_DEFAUT
    return {"mode": mode if mode in MODES else MODE_DEFAUT,
            "relire": (db.etat_valeur("relire_avec_claude") or "oui") == "oui",
            "pause": db.etat_valeur("analyses_pause") == "oui"}


class BesoinClaude(Exception):
    """Le modèle local ne s'en sort pas (scan manuscrit illisible par l'OCR) : Claude doit lire le fichier."""
MOTS_VIDES = set("""alors aussi autre avec avoir cela cette ceux comme dans donc elle elles entre est etre fait faire
leur leurs mais meme nous pour plus sans sont sous tout tous tres vous avez peut quand quel quelle
quels quelles chaque deux trois page exercice question reponse cours corrige""".split())


def _simple(texte: str) -> str:
    return unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode().lower()


# --- Ollama : modèle de langue local ---------------------------------------------------

_ollama = {"vu": 0.0, "pret": False, "embed": False, "message": "", "telechargement": False}
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
    present = {m: m in noms or f"{m}:latest" in noms for m in (OLLAMA_MODELE, OLLAMA_EMBED)}
    _ollama.update(pret=present[OLLAMA_MODELE], embed=present[OLLAMA_EMBED])
    manquants = [m for m, ok in present.items() if not ok]
    if not manquants:
        _ollama.update(message=f"Ollama prêt ({OLLAMA_MODELE}, recherche par le sens {OLLAMA_EMBED})")
    elif _ollama["telechargement"]:
        _ollama.update(message=f"Téléchargement de {', '.join(manquants)}…")
    else:
        _ollama.update(telechargement=True, message=f"Téléchargement de {', '.join(manquants)}…")
        threading.Thread(target=_telecharger, args=(manquants,), daemon=True, name="ollama-pull").start()
    return dict(_ollama)


def _telecharger(modeles: list[str]):
    try:
        for m in modeles:
            httpx.post(f"{OLLAMA_URL}/api/pull", json={"model": m, "stream": False}, timeout=3600)
        _ollama.update(vu=0.0, message=f"{', '.join(modeles)} téléchargé(s)")
    except Exception as e:
        _ollama.update(message=f"Téléchargement de {', '.join(modeles)} impossible : {e}")
    finally:
        _ollama["telechargement"] = False


def disponible() -> str | None:
    """Pourquoi le modèle de langue local ne peut pas prendre de travail maintenant, ou None."""
    if not etat_ollama()["pret"]:
        return "modèle local pas installé"
    return ressources.sature()


def essayer_json(systeme: str, texte: str, timeout: int = 600) -> dict | None:
    """Une réponse JSON du modèle local, ou None (pas installé, VM saturée, réponse inutilisable).

    Pour les petites tâches internes : l'appelant passe à Claude si c'est None.
    """
    if disponible():
        return None
    try:
        d = json.loads(ollama(systeme, texte, timeout=timeout))
    except (ErreurAgent, ValueError):
        return None
    return d if isinstance(d, dict) else None


def vecteurs(textes: list[str]) -> list[list[float]] | None:
    """Sens de chaque texte (embeddings), pour la recherche ; None si le modèle n'est pas là."""
    if not textes or not etat_ollama()["embed"]:
        return None
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/embed", json={"model": OLLAMA_EMBED, "input": textes, "keep_alive": GARDER,
                                                              "options": {"num_thread": THREADS}},
                       timeout=300)
        r.raise_for_status()
        v = r.json().get("embeddings")
    except Exception:
        return None
    return v if isinstance(v, list) and len(v) == len(textes) else None


def liberer() -> bool:
    """Décharge le modèle de langue d'Ollama tout de suite (mémoire de la VM basse) ; rechargé au besoin."""
    try:
        httpx.post(f"{OLLAMA_URL}/api/generate", json={"model": OLLAMA_MODELE, "keep_alive": 0}, timeout=30)
        return True
    except httpx.HTTPError:
        return False


def ollama(systeme: str, texte: str, json_seul: bool = True, timeout: int = 900) -> str:
    """Une réponse du modèle local (CPU : de quelques secondes à quelques minutes)."""
    corps = {"model": OLLAMA_MODELE, "stream": False, "keep_alive": GARDER,
             "messages": [{"role": "system", "content": systeme}, {"role": "user", "content": texte}],
             "options": {"temperature": 0.1, "num_ctx": 8192, "num_thread": THREADS}}
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
    ressources.noter_local()
    return reponse


# --- Texte du document -------------------------------------------------------------------

def transcrire(chemin: Path, arret=None) -> tuple[str, str]:
    """(texte, d'où il vient) sans IA. `arret()` vrai : on abandonne entre deux pages."""
    suffixe = chemin.suffix.lower()
    if suffixe in agent.VIEUX_OFFICE:
        return agent.markdown_office(agent.texte_vieux_office(chemin)), "fichier bureautique"
    if suffixe in agent.OFFICE:
        try:
            return agent.texte_bureautique(chemin), "fichier bureautique"
        except Exception as e:
            raise ErreurAgent(f"fichier {suffixe} illisible : {e}") from e
    if suffixe in (".txt", ".md"):
        return chemin.read_text(errors="replace"), "fichier texte"
    if suffixe in agent.SOURCES:
        return agent.texte_source(chemin), "code source d'un TP ou d'un exercice"
    if not ocr.disponible():
        raise ErreurAgent("Tesseract (OCR) n'est pas installé : impossible de lire un scan sans Claude")
    if suffixe == ".pdf":
        texte = agent.texte_pdf(chemin)
        if texte:
            return lecture.meilleur_pdf(chemin, texte), "PDF numérique"
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


MOT = re.compile(r"[a-zàâäçéèêëîïôöùûüÿœæ]{2,}", re.I)
VOYELLE = re.compile(r"[aeiouyàâäéèêëîïôöùûü]", re.I)


def qualite_ocr(texte: str) -> float:
    """Part des « mots » qui ressemblent à de vrais mots (un OCR raté donne « l1 |Ii ;c rn »)."""
    morceaux = texte.split()
    if not morceaux:
        return 0.0
    bons = sum(1 for m in morceaux if MOT.fullmatch(m.strip(".,;:!?()«»\"'’-")) and VOYELLE.search(m))
    return bons / len(morceaux)


OCR_PAUVRE = 0.55  # en dessous : écriture manuscrite ou scan flou, mieux vaut que Claude lise le fichier


PROMPT_CONTROLE = """Un modèle local, plus petit que toi, a lu et classé un document d'élève (du collège au BTS SIO).
Sur l'entrée : son classement, puis le texte du document ({origine}). Vérifie ce classement avec le texte :
garde ce qui est juste, corrige le reste. {raison}
Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour :
""" + agent.CLASSEMENT_JSON.replace("}}", ', "accord": true, "remarque": "..."}}') + """
- accord : true si le classement local (année, matière, type) était juste, false si tu l'as corrigé.
- remarque : une phrase pour l'élève si tu as corrigé, sinon "".
Règles :
""" + agent.REGLES


def verifier_par_claude(a: Analyse, texte: str, nom: str, origine: str, matieres: list[str] | None = None,
                        raison: str = "") -> tuple[Analyse, bool]:
    """Claude (Haiku, texte seul : peu coûteux) relit le classement local. Renvoie (classement final, accord)."""
    local = {k: getattr(a, k) for k in ("titre", "niveau", "matiere", "type", "chapitre", "resume", "confiance")}
    entree = (f"Classement du modèle local :\n{json.dumps(local, ensure_ascii=False)}\n\n"
              + (f"Matières déjà utilisées : {', '.join(matieres)}.\n\n" if matieres else "")
              + f"Texte du document « {nom} » :\n{texte[:20_000]}")
    prompt = PROMPT_CONTROLE.format(origine=origine, raison=raison, niveaux=", ".join(NIVEAUX), types=", ".join(TYPES))

    def lire(reponse: str):
        b = agent.parse_reponse(reponse)
        debut, fin = reponse.find("{"), reponse.rfind("}")
        try:
            d = json.loads(reponse[debut:fin + 1])
        except ValueError:
            d = {}
        return b, d.get("accord") is True, str(d.get("remarque") or "")

    with tempfile.TemporaryDirectory() as vide:
        (b, accord, remarque), modele = agent.demander("controle", prompt, Path(vide), lire=lire, entree=entree,
                                                       timeout=300)
    b.transcription, b.modele = a.transcription, "local"
    b.mots_cles = b.mots_cles or a.mots_cles
    b.titre, b.resume = b.titre or a.titre, b.resume or a.resume
    b.chapitre, b.date = b.chapitre or a.chapitre, b.date or a.date
    from claunote import modeles
    b.avis = (f"Classé sur la VM, vérifié par Claude {modeles.NOMS.get(modele, modele)} : "
              + ("confirmé." if accord else f"corrigé{' (' + remarque + ')' if remarque else ''}."))
    try:
        db.noter_controle("analyse", accord, f"{nom} : local {a.matiere}/{a.niveau}/{a.type}, "
                                             f"Claude {b.matiere}/{b.niveau}/{b.type}")
    except Exception:
        pass
    return b, accord


def analyser(chemin: Path, dossiers: list[str] | None = None, indice: str = "", matieres: list[str] | None = None,
             memoire=None, arret=None, claude: bool = False, claude_echec=lambda e: None) -> Analyse:
    """Même contrat qu'agent.analyser : transcription + classement, sur la VM.

    `claude` : Claude est disponible pour aider (lever BesoinClaude, vérifier un classement douteux).
    `claude_echec(e)` : Claude est tombé (limite, panne) pendant une vérification ; le résultat local est gardé.
    """
    debut = time.monotonic()
    texte, origine = transcrire(chemin, arret)
    scan = origine.startswith("OCR")
    pauvre = scan and qualite_ocr(texte) < OCR_PAUVRE
    if (not texte.strip() or pauvre) and claude:
        raise BesoinClaude("écriture manuscrite ou scan flou : l'OCR n'a pas pu le lire correctement")
    if not texte.strip():
        raise ErreurAgent("aucun texte lisible sans Claude (écriture manuscrite ou image) : à relire avec Claude")
    if arret and arret():
        raise Interrompu("analyse arrêtée")
    outil = "voisins"
    if not disponible():
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
    a.avis = (f"Analysé sur la VM : {origine}, classement par "
              + (f"Ollama ({OLLAMA_MODELE})." if outil == "ollama" else "ressemblance avec tes cours déjà classés."))
    ressources.DUREES.noter(time.monotonic() - debut)
    return aide_de_claude(a, texte, chemin.name, origine, matieres, claude, claude_echec, pauvre)


def aide_de_claude(a: Analyse, texte: str, nom: str, origine: str, matieres: list[str] | None, claude: bool,
                   claude_echec, pauvre: bool = False) -> Analyse:
    """Le modèle local doute : Claude vérifie (et 1 fois sur 10 même s'il ne doute pas, pour le mesurer)."""
    doute = a.confiance < SEUIL_CONFIANCE
    if claude and (doute or random.random() < CONTROLE):
        raison = ("Le modèle local n'est pas sûr de lui." if doute
                  else "C'est un contrôle par échantillon : sois exigeant.")
        try:
            b, _ = verifier_par_claude(a, texte, nom, origine, matieres, raison)
            return b
        except (LimiteAtteinte, ClaudeIndisponible) as e:
            claude_echec(e)
        except ErreurAgent:
            pass
    a.a_relire = pauvre or doute
    return a


def reclasser(doc: dict, matieres: list[str], claude: bool = False, claude_echec=lambda e: None) -> Analyse | None:
    """Reclassement d'un document déjà transcrit par le modèle local (Organiser → Ranger par l'IA).

    None si le modèle local n'est pas disponible (pas installé, VM saturée) : Claude s'en charge.
    """
    texte = (doc.get("transcription") or "").strip()
    if not texte or disponible():
        return None
    nom = doc.get("titre") or doc["nom"]
    contexte = (f"\nL'élève a indiqué l'année d'étude : {doc['niveau']}. Garde-la.\n" if doc.get("niveau") else "")
    if matieres:
        contexte += f"Matières déjà utilisées : {', '.join(matieres)}. Reprends exactement l'un de ces noms s'il convient.\n"
    try:
        a = classer_par_ollama(texte, nom, "transcription déjà faite", contexte)
    except ErreurAgent:
        return None
    if doc.get("niveau"):
        a.niveau = doc["niveau"]
    a.modele, a.avis = "local", f"Reclassé sur la VM (Ollama, {OLLAMA_MODELE})."
    b = aide_de_claude(a, texte, nom, "transcription déjà faite", matieres, claude, claude_echec)
    if doc.get("niveau"):
        b.niveau = doc["niveau"]
    return b
