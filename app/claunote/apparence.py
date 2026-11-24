"""Apparence de chaque compte : thème (auto, clair, sombre) et style (Claunote, ou Vigie une fois l'easter egg trouvé).

Le style Vigie (débloqué par 10 appuis rapides sur l'onglet Actualités ; hommage à https://vigie.dix-neuf.ovh, l'outil de veille d'un ami) ne change que les couleurs
d'accent, les titres et le logo (toujours le papillon), et il est toujours en sombre (fond sombre de Claunote).
"""
import json

from claunote import db

MODES = {"auto": "Auto", "clair": "Clair", "sombre": "Sombre"}
STYLES = {"claunote": "Claunote", "vigie": "Vigie"}
DEFAUT = {"mode": "auto", "style": "claunote", "vigie": False}   # vigie : easter egg trouvé


def _cle(compte_id: int) -> str:
    return f"apparence:{int(compte_id)}"


def lire(compte: dict | None) -> dict:
    """Jamais d'erreur (la page doit s'afficher) ; sans compte (pages publiques) : l'apparence par défaut."""
    if not compte or not compte.get("id"):
        return dict(DEFAUT)
    try:
        p = {**DEFAUT, **json.loads(db.etat_valeur(_cle(compte["id"])) or "{}")}
    except Exception:
        return dict(DEFAUT)
    if p["mode"] not in MODES:
        p["mode"] = "auto"
    if p["style"] not in STYLES or (p["style"] == "vigie" and not p["vigie"]):
        p["style"] = "claunote"
    p["theme"] = "sombre" if p["style"] == "vigie" else p["mode"]   # thème affiché ; Vigie : sombre uniquement
    return p


def _ecrire(compte_id: int, p: dict):
    db.etat_ecrire(_cle(compte_id), json.dumps({k: p[k] for k in DEFAUT}))


def enregistrer(compte: dict, mode: str, style: str) -> dict:
    p = lire(compte)
    p["mode"] = mode if mode in MODES else "auto"
    p["style"] = style if style in STYLES and (style != "vigie" or p["vigie"]) else "claunote"
    _ecrire(compte["id"], p)
    return lire(compte)


def debloquer_vigie(compte: dict | None) -> bool:
    """Easter egg trouvé : le style Vigie apparaît dans Réglages → Apparence. True la première fois."""
    if not compte or not compte.get("id"):
        return False
    p = lire(compte)
    if p["vigie"]:
        return False
    p["vigie"] = True
    _ecrire(compte["id"], p)
    return True
