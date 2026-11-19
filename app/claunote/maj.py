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


LOCAUX_ETEINTS = "locaux_eteints"   # présent : le service maj garde Ollama et le service lecture arrêtés
LOCAUX_ALLUMER = "locaux_allumer"   # demande ponctuelle : il les reconstruit si besoin et les relance


def locaux(eteindre: bool) -> str:
    """Éteint ou rallume les conteneurs des modèles locaux (ollama, lecture) par le service maj.

    Le site pose un fichier dans /maj puis demande une vérification : le service applique ce fichier au
    début de chaque action (et toutes les 6 h), donc aussi après un « docker compose up -d » fait à la main.
    Renvoie ce qui va se passer, pour la page.
    """
    if eteindre:
        (DOSSIER / LOCAUX_ETEINTS).write_text("oui")
        (DOSSIER / LOCAUX_ALLUMER).unlink(missing_ok=True)
    else:
        (DOSSIER / LOCAUX_ETEINTS).unlink(missing_ok=True)
        (DOSSIER / LOCAUX_ALLUMER).write_text("oui")
    if not present():
        return ("Le service maj ne tourne pas : sur la VM, " +
                ("docker compose stop ollama lecture" if eteindre else "docker compose up -d ollama lecture") + ".")
    if action_en_cours():
        return "Appliqué à la fin de l'action en cours (mise à jour…)."
    try:
        demander("verifier")
    except RuntimeError:
        return "Appliqué à la fin de l'action en cours (mise à jour…)."
    return ("Ollama et le service lecture s'arrêtent dans quelques secondes." if eteindre
            else "Ollama et le service lecture redémarrent (reconstruction si le code a changé).")


TUNNEL_ACTIF = "tunnel_actif"         # présent : le service maj garde cloudflared démarré (profil « tunnel »)
TUNNEL_RELANCER = "tunnel_relancer"   # demande ponctuelle : (re)démarrer ou arrêter cloudflared maintenant


def tunnel(actif: bool) -> str:
    """Allume ou éteint le tunnel Cloudflare depuis le site (Réglages → Installation), par le service maj.
    cloudflared relit son jeton (/tunnel/token) à chaque démarrage. Renvoie ce qui va se passer."""
    if actif:
        (DOSSIER / TUNNEL_ACTIF).write_text("oui")
    else:
        (DOSSIER / TUNNEL_ACTIF).unlink(missing_ok=True)
    (DOSSIER / TUNNEL_RELANCER).write_text("oui")
    if not present():
        return ("Le service maj ne tourne pas : sur la VM, " +
                ("docker compose --profile tunnel up -d cloudflared" if actif else "docker compose stop cloudflared") + ".")
    try:
        demander("verifier")
    except RuntimeError:
        return "Appliqué à la fin de l'action en cours (mise à jour…)."
    return "Le tunnel démarre dans quelques secondes." if actif else "Le tunnel s'arrête dans quelques secondes."


def tunnel_actif() -> bool:
    return (DOSSIER / TUNNEL_ACTIF).exists()


def conso() -> tuple[list[dict], dt.datetime | None]:
    """Processeur et mémoire de chaque conteneur au dernier relevé du service maj (docker stats)."""
    e = _etat_brut()
    lignes = [c for c in e.get("conso") or [] if isinstance(c, dict)]

    def cpu(c):
        try:
            return float(str(c.get("cpu") or "0").rstrip("%"))
        except ValueError:
            return 0.0
    return sorted(lignes, key=cpu, reverse=True), _instant(e.get("conso_le"))


def disponibles() -> int:
    """Nouveautés sur GitHub pas encore récupérées, + 1 si le code de la VM n'est pas celui qui tourne (0 si inconnu)."""
    try:
        e = json.loads((DOSSIER / "etat.json").read_text())
        return int(e.get("retard") or 0) + (1 if e.get("a_installer") else 0)
    except (OSError, ValueError, TypeError):
        return 0
