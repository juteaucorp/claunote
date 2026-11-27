"""Prise de notes : à chaque compte ses notes, rangées par matière, rattachées à un cours, ou libres.

    Nouvelle note ──► /notes/nouvelle (?matiere=BTS2|Réseaux, ?doc=12, ?pronote=… depuis l'Agenda)
         │               éditeur Markdown, enregistré tout seul pendant la frappe (POST /notes/<id>)
         │               ▦ Tableau : grille de cases (créer, ou modifier celui sous le curseur)
         │               collage Word / Google Docs et import .docx → Markdown (collage.py)
         ▼
    table notes (une par ligne, compte_id + row-level security, comme les cours)
         ├──► /notes : épinglées, puis par matière ; recherche pendant la frappe ; archivées à part
         ├──► page du cours : ses notes liées, les publiques des autres, ses notes de la matière à lier
         ├──► /notes : le cours lié de chaque note ; la note : « Ouvrir le cours »
         ├──► Recherche du site : « Dans tes notes »
         ├──► chat « Une question » : les notes qui en parlent, repères [D…] vers la note
         ├──► toute révision (fiche, quiz, flashcards, épreuve, plan, campagne, « en une phrase », modification) :
         │      lues comme des cours, repères [Dn] vers la note (worker.notes_de_revision, db.notes_pour_revision)
         └──► 🔒 privée par défaut / 🌐 publique : lue par les autres comptes sur « Partagés »

Droit « notes » (comptes.DROITS). Deux appareils sur la même note : chaque envoi porte la version lue
(`base`) ; si la note a changé entre-temps, rien n'est écrasé, l'éditeur propose de garder la sienne.
"""
import datetime as dt
import re
import unicodedata

TITRE_MAX = 160
CONTENU_MAX = 1_000_000           # ~1 Mo de texte : une note, pas un livre (garde-fou contre un envoi absurde)
SANS_TITRE = "Note sans titre"
NB_POUR_QUESTION = 3              # notes ajoutées aux passages du chat « Une question »
TEXTE_POUR_QUESTION = 3000


def matiere_choisie(valeur: str | None) -> tuple[str | None, str | None]:
    """« BTS2|Réseaux » → ("BTS2", "Réseaux") ; vide → (None, None) : note libre."""
    niveau, _, matiere = (valeur or "").partition("|")
    niveau, matiere = niveau.strip()[:40] or None, matiere.strip()[:120] or None
    return (niveau, matiere) if matiere else (None, None)


def titre_auto(titre: str | None, contenu: str | None) -> str:
    """Le titre écrit, sinon la première ligne du texte (sans # ni puce), sinon « Note sans titre »."""
    t = (titre or "").strip()
    if t:
        return t[:TITRE_MAX]
    for ligne in (contenu or "").splitlines():
        ligne = re.sub(r"^\s*(#{1,6}|[-*+] \[[ xX]\]|[-*+>]|\d+[.)])\s*", "", ligne).strip(" *_`")
        if ligne:
            return ligne[:80]
    return SANS_TITRE


def titre_affiche(note: dict) -> str:
    return titre_auto(note.get("titre"), note.get("contenu") or note.get("debut"))


def extrait(texte: str | None, n: int = 160) -> str:
    """Début du texte sur une ligne, sans la mise en forme Markdown."""
    t = re.sub(r"```.*?```", " ", texte or "", flags=re.S)
    t = re.sub(r"(^|\n)\s*(#{1,6}|[-*+>]|\d+[.)])\s+", " ", t)
    t = re.sub(r"\[[ xX]\]\s*", "", t)
    t = re.sub(r"[*_`]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


def _sans_accents(texte: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texte) if unicodedata.category(c) != "Mn")


def requete_prefixe(q: str) -> str | None:
    """Recherche pendant la frappe : « vla rout » → « vla:* & rout:* » (to_tsquery). None : rien à chercher."""
    mots = re.findall(r"[\w]+", q or "", flags=re.U)[:8]
    mots = [m for m in mots if len(m) >= 2 or m.isdigit()]
    return " & ".join(f"{m}:*" for m in mots) or None


CASE = re.compile(r"<li>\s*\[([ xX])\]\s*")


def cases_a_cocher(rendu: str) -> str:
    """« - [ ] » / « - [x] » du Markdown en vraies cases (affichage seulement)."""
    return CASE.sub(lambda m: '<li class="tache"><input type="checkbox" disabled'
                    + (' checked' if m.group(1) != " " else "") + "> ", rendu)


def nom_fichier(note: dict) -> str:
    """Nom du .md téléchargé : titre lisible, sans caractère interdit."""
    base = re.sub(r"[^\w\- ]+", "", _sans_accents(titre_affiche(note)), flags=re.U).strip()[:60] or "note"
    return f"{base}.md"


def markdown_complet(note: dict) -> str:
    """La note telle qu'on la télécharge : titre, matière et date, puis le texte."""
    entete = [f"# {titre_affiche(note)}"]
    infos = [x for x in (note.get("matiere"), note.get("niveau")) if x]
    quand = note.get("maj_le")
    if isinstance(quand, dt.datetime):
        infos.append(f"modifiée le {quand:%d/%m/%Y}")
    if infos:
        entete.append("_" + " · ".join(infos) + "_")
    return "\n\n".join(entete) + "\n\n" + (note.get("contenu") or "").strip() + "\n"


def piece_jointe(note: dict) -> tuple[str, bytes]:
    """Une note cochée pour une fiche, un quiz…: jointe comme un fichier texte (contexte donné par l'élève)."""
    nom = f"Ma note - {nom_fichier(note)}"[:120]
    return nom, markdown_complet(note).encode()


def comme_passage(note: dict, compte_id: int | None = None) -> dict:
    """Une note au format des passages du chat (question.py). Son identifiant est négatif : le repère [D…]
    mène à /notes/<id> et non à un cours (main.avec_sources). Celle d'un autre compte (publique) porte son auteur."""
    texte = (note.get("contenu") or "").strip()[:TEXTE_POUR_QUESTION]
    autre = compte_id is not None and note.get("compte_id") not in (None, compte_id)
    titre = (f"Note de {note.get('auteur') or 'un autre élève'} : " if autre else "Ta note : ") + titre_affiche(note)
    return {"document_id": -int(note["id"]), "titre": titre, "nom": titre_affiche(note),
            "niveau": note.get("niveau"), "matiere": note.get("matiere"), "type": "note personnelle",
            "statut": "note", "texte": texte or titre_affiche(note), "extrait": extrait(texte, 300)}


def comme_source(note: dict, compte_id: int | None = None) -> dict:
    """Une note au format des cours lus par une révision (revision.blocs_cours) : repère [Dn] comme un cours,
    identifiant négatif pour que le lien mène à la note (main.avec_sources). La note publique d'un autre
    compte porte le nom de son auteur."""
    autre = compte_id is not None and note.get("compte_id") not in (None, compte_id)
    titre = (f"Note de {note.get('auteur') or 'un autre élève'} : " if autre else "Ta note : ") + titre_affiche(note)
    return {"id": -int(note["id"]), "note_id": int(note["id"]), "titre": titre, "nom": titre,
            "niveau": note.get("niveau"), "matiere": note.get("matiere"),
            "type": "note publique d'un autre élève" if autre else "note personnelle",
            "transcription": (note.get("contenu") or "").strip(), "statut": "note"}


def sujet_precis(sujet: str | None, matieres: list[str]) -> str | None:
    """Le sujet d'une révision sans le nom de la matière ; None si elle couvre la matière entière
    (« Réseaux », « Réseaux (BTS1, BTS2) ») : toutes les notes de la matière sont alors lues."""
    s = (sujet or "").strip()
    for m in sorted((m for m in matieres if m), key=len, reverse=True):
        if s.casefold() == m.casefold() or re.fullmatch(rf"{re.escape(m)}\s*\([^)]*\)", s, flags=re.I):
            return None
        if s.casefold().startswith(f"{m.casefold()} : "):
            s = s[len(m) + 3:].strip()
            break
    return s or None
