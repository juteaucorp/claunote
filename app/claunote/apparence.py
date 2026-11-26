"""Apparence de chaque compte : thème (auto, clair, sombre) et style (Claunote, ou Vigie une fois l'easter egg trouvé).

Le style Vigie (débloqué par 10 appuis rapides sur l'onglet Actualités ; hommage à https://vigie.dix-neuf.ovh, l'outil de veille d'un ami) ne change que les couleurs
d'accent, les titres et le logo (toujours le papillon), et il est toujours en sombre (fond sombre de Claunote).
"""
import json

from claunote import db

MODES = {"auto": "Auto", "clair": "Clair", "sombre": "Sombre"}
STYLES = {"claunote": "Claunote", "vigie": "Vigie"}
# Rubriques des onglets : (nom, adresse, droit, icône, aide dans Plus). Chaque compte choisit l'ordre, celles
# qu'il garde en onglets (les autres passent dans Plus) et sa page d'accueil.
RUBRIQUES = {
    "actualites": ("Actualités", "/actualites", "actualites", "actualites", "ton fil, les alertes"),
    "cours": ("Cours", "/bibliotheque", "bibliotheque", "cours", "tes cours classés"),
    "reviser": ("Réviser", "/reviser", "reviser", "reviser", "fiches, quiz, flashcards"),
    "agenda": ("Agenda", "/agenda", "agenda", "agenda", "Pronote, contrôles"),
    "notes": ("Notes", "/notes", "notes", "notes", "tes notes par matière"),
}
ORDRE = list(RUBRIQUES)
# Onglets d'origine : quatre (la barre du téléphone garde de la place) ; les Notes sont dans Plus tant que le
# compte ne les met pas en onglet (Réglages › Apparence).
ONGLETS_DEFAUT = ["actualites", "cours", "reviser", "agenda"]
DEFAUT = {"mode": "auto", "style": "claunote", "vigie": False,   # vigie : easter egg trouvé
          "onglets": ONGLETS_DEFAUT, "accueil": ""}                       # accueil vide : Actualités, sinon la 1re permise


def _cle(compte_id: int) -> str:
    return f"apparence:{int(compte_id)}"


def lire(compte: dict | None) -> dict:
    """Jamais d'erreur (la page doit s'afficher) ; sans compte (pages publiques) : l'apparence par défaut."""
    if not compte or not compte.get("id"):
        return {**DEFAUT, "onglets": list(ONGLETS_DEFAUT), "caches": [k for k in ORDRE if k not in ONGLETS_DEFAUT]}
    try:
        p = {**DEFAUT, **json.loads(db.etat_valeur(_cle(compte["id"])) or "{}")}
    except Exception:
        return {**DEFAUT, "onglets": list(ONGLETS_DEFAUT), "caches": [k for k in ORDRE if k not in ONGLETS_DEFAUT]}
    if p["mode"] not in MODES:
        p["mode"] = "auto"
    if p["style"] not in STYLES or (p["style"] == "vigie" and not p["vigie"]):
        p["style"] = "claunote"
    p["onglets"] = _onglets(p.get("onglets"))
    p["caches"] = [k for k in ORDRE if k not in p["onglets"]]
    if p["accueil"] not in RUBRIQUES:
        p["accueil"] = ""
    p["theme"] = "sombre" if p["style"] == "vigie" else p["mode"]   # thème affiché ; Vigie : sombre uniquement
    return p


def _onglets(valeur) -> list[str]:
    """Rubriques gardées en onglets, dans l'ordre choisi ; sans doublon ni inconnue. Aucune : celles d'origine."""
    if not isinstance(valeur, list):
        return list(ONGLETS_DEFAUT)
    vus = list(dict.fromkeys(k for k in valeur if k in RUBRIQUES))
    return vus or list(ONGLETS_DEFAUT)


def _ecrire(compte_id: int, p: dict):
    db.etat_ecrire(_cle(compte_id), json.dumps({k: p[k] for k in DEFAUT}))


def enregistrer(compte: dict, mode: str, style: str) -> dict:
    p = lire(compte)
    p["mode"] = mode if mode in MODES else "auto"
    p["style"] = style if style in STYLES and (style != "vigie" or p["vigie"]) else "claunote"
    _ecrire(compte["id"], p)
    return lire(compte)


def enregistrer_navigation(compte: dict, onglets: list[str], accueil: str) -> dict:
    """Onglets gardés (dans l'ordre) et page d'accueil du compte."""
    p = lire(compte)
    p["onglets"], p["accueil"] = _onglets(onglets), accueil if accueil in RUBRIQUES else ""
    _ecrire(compte["id"], p)
    return lire(compte)


def accueil(compte: dict | None, peut) -> str | None:
    """Adresse de la page d'accueil choisie, si le compte y a droit (sinon None : le choix d'origine)."""
    cle = lire(compte)["accueil"]
    if cle and peut(compte, RUBRIQUES[cle][2]):
        return RUBRIQUES[cle][1]
    return None


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
