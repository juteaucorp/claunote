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


DUREE_MAX = dt.timedelta(hours=3)   # au-delà, une action « en cours » est considérée comme interrompue


def _lu(nom: str) -> dt.datetime | None:
    try:
        return _instant((DOSSIER / nom).read_text())
    except OSError:
        return None


def _frais(t: dt.datetime | None) -> bool:
    return bool(t) and dt.datetime.now(dt.timezone.utc) - t < MUET


def _etat_brut() -> dict:
    try:
        return json.loads((DOSSIER / "etat.json").read_text())
    except (OSError, ValueError):
        return {}


def action_en_cours() -> bool:
    """Une action (mise à jour…) tourne en ce moment. La boucle du service ne signe pas pendant une action :
    on se fie au signal de l'action (action-vu), ou, pour un ancien script qui n'en envoie pas, à l'état
    « en cours » depuis moins de 3 h."""
    if _frais(_lu("action-vu")) or (DOSSIER / "demande").exists():
        return True
    e = _etat_brut()
    debut = _instant(e.get("debut"))
    return e.get("statut") == "en_cours" and bool(debut) and \
        dt.datetime.now(dt.timezone.utc) - debut < DUREE_MAX and not _frais(_lu("vu"))


def present() -> bool:
    """Le service maj tourne-t-il ? Oui aussi pendant une action, même longue (sinon : pas installé)."""
    return _frais(_lu("vu")) or action_en_cours()


def etat() -> dict:
    e = _etat_brut()
    for cle in ("debut", "fin", "verifie"):
        e[cle] = _instant(e.get(cle))
    if e.get("version"):
        e["version"]["le"] = _instant(e["version"].get("le"))
    for n in e.get("nouveautes") or []:
        n["le"] = _instant(n.get("le"))
    e["en_attente"] = (DOSSIER / "demande").exists()
    e["en_cours"] = action_en_cours()
    if e.get("statut") == "en_cours" and not e["en_cours"]:   # coupée net (redémarrage de la machine…)
        e["statut"], e["message"] = "erreur", "La dernière action a été interrompue (redémarrage ?) : relance-la."
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
