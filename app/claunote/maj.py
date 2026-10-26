"""Mise à jour de Claunote depuis le site (Réglages → Mise à jour).

Le site ne touche ni à git ni à Docker : il dépose une demande dans le volume partagé /maj,
que le service `maj` (infra/maj) exécute, puis lit l'état et le journal que ce service y écrit.

    site ──« mettre_a_jour »──▶ /maj/demande ──▶ service maj : git pull, docker compose build + up -d
    site ◀── /maj/etat.json, /maj/journal.log ◀──┘
"""
import datetime as dt
import json
import os
from pathlib import Path

DOSSIER = Path(os.environ.get("MAJ_DIR", "/maj"))
ACTIONS = {"verifier": "Vérifier", "mettre_a_jour": "Mettre à jour",
           "redemarrer": "Redémarrer", "nettoyer": "Nettoyer"}
MUET = dt.timedelta(minutes=1)   # le service maj signe sa présence toutes les 3 s


def _instant(texte: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(texte.strip()) if texte else None
    except ValueError:
        return None


def present() -> bool:
    """Le service maj tourne-t-il ? (sinon : pas encore installé sur la VM)"""
    try:
        vu = _instant((DOSSIER / "vu").read_text())
    except OSError:
        return False
    return bool(vu) and dt.datetime.now(dt.timezone.utc) - vu < MUET


def etat() -> dict:
    try:
        e = json.loads((DOSSIER / "etat.json").read_text())
    except (OSError, ValueError):
        e = {}
    for cle in ("debut", "fin", "verifie"):
        e[cle] = _instant(e.get(cle))
    if e.get("version"):
        e["version"]["le"] = _instant(e["version"].get("le"))
    for n in e.get("nouveautes") or []:
        n["le"] = _instant(n.get("le"))
    e["en_attente"] = (DOSSIER / "demande").exists()
    e["en_cours"] = e["en_attente"] or e.get("statut") == "en_cours"
    return e


def journal(lignes: int = 60) -> str:
    try:
        return "\n".join((DOSSIER / "journal.log").read_text(errors="replace").splitlines()[-lignes:])
    except OSError:
        return ""


def demander(action: str) -> None:
    if action not in ACTIONS:
        raise ValueError(f"action inconnue : {action}")
    if etat()["en_cours"]:
        raise RuntimeError("Une action est déjà en cours.")
    temp = DOSSIER / "demande.tmp"
    temp.write_text(action)
    temp.replace(DOSSIER / "demande")   # d'un coup : le service ne lit jamais une demande à moitié écrite


def disponibles() -> int:
    """Nouveautés sur GitHub pas encore récupérées, + 1 si le code de la VM n'est pas celui qui tourne (0 si inconnu)."""
    try:
        e = json.loads((DOSSIER / "etat.json").read_text())
        return int(e.get("retard") or 0) + (1 if e.get("a_installer") else 0)
    except (OSError, ValueError, TypeError):
        return 0
