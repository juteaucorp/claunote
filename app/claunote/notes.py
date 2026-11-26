"""Prise de notes : à chaque compte ses notes, rangées par matière, rattachées à un cours, ou libres.

    Nouvelle note ──► /notes/nouvelle (?matiere=BTS2|Réseaux, ?doc=12, ?pronote=… depuis l'Agenda)
         │               éditeur Markdown, enregistré tout seul pendant la frappe (POST /notes/<id>)
         ▼
    table notes (une par ligne, compte_id + row-level security, comme les cours)
         ├──► /notes : épinglées, puis par matière ; recherche pendant la frappe ; archivées à part
         ├──► page du cours : « Mes notes sur ce cours »
         ├──► Recherche du site : « Dans tes notes »
         ├──► chat « Une question » : les notes qui en parlent, repères [D…] vers la note
         ├──► fiches, quiz, flashcards : notes cochées jointes au support (contexte de l'élève)
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


def comme_passage(note: dict) -> dict:
    """Une note au format des passages du chat (question.py). Son identifiant est négatif : le repère [D…]
    mène à /notes/<id> et non à un cours (main.avec_sources)."""
    texte = (note.get("contenu") or "").strip()[:TEXTE_POUR_QUESTION]
    return {"document_id": -int(note["id"]), "titre": f"Ta note : {titre_affiche(note)}", "nom": titre_affiche(note),
            "niveau": note.get("niveau"), "matiere": note.get("matiere"), "type": "note personnelle",
            "statut": "note", "texte": texte or titre_affiche(note), "extrait": extrait(texte, 300)}
