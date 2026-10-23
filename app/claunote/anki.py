"""Export des flashcards en paquet Anki (.apkg), lisible par Anki, AnkiDroid et AnkiMobile.

Les identifiants sont stables : réimporter un paquet met à jour les cartes déjà
importées au lieu de les dupliquer, et garde la progression faite dans Anki.
"""
import html
import tempfile
import zlib
from pathlib import Path

import genanki

MODELE = genanki.Model(
    1_607_392_319,  # identifiant fixe du modèle « Claunote »
    "Claunote",
    fields=[{"name": "Recto"}, {"name": "Verso"}, {"name": "Source"}],
    templates=[{
        "name": "Carte",
        "qfmt": "{{Recto}}",
        "afmt": '{{FrontSide}}<hr id="answer">{{Verso}}<div class="source">{{Source}}</div>',
    }],
    css=".card { font-family: system-ui, sans-serif; font-size: 20px; text-align: center; color: #1f1e1d; "
        "background: #faf9f5; } .source { margin-top: 14px; font-size: 13px; color: #6f6b63; } "
        ".nightMode .card, .card.nightMode { color: #eeebe4; background: #1c1b1a; }",
)


def _texte(s: str) -> str:
    return html.escape(s).replace("\n", "<br>")


def nom_paquet(sujet: str) -> str:
    # « :: » crée des sous-paquets dans Anki
    return "Claunote::" + sujet.replace("::", ":")


def paquet(rev_id: int, sujet: str, cartes: list[dict], sources: dict[str, str]) -> genanki.Deck:
    """`sources` : repère (D1…) → titre du cours, affiché sous la réponse."""
    deck = genanki.Deck(zlib.crc32(f"claunote-revision-{rev_id}".encode()) + 1_000_000, nom_paquet(sujet))
    for k in cartes:
        source = sources.get(k.get("source") or "", "")
        deck.add_note(genanki.Note(
            model=MODELE, fields=[_texte(k["recto"]), _texte(k["verso"]), _texte(source)],
            guid=genanki.guid_for("claunote", k["id"])))
    return deck


def exporter(paquets: list[genanki.Deck]) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        chemin = Path(tmp) / "claunote.apkg"
        genanki.Package(paquets).write_to_file(chemin)
        return chemin.read_bytes()
