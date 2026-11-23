"""Demandes IA des comptes sans forfait Claude : l'admin dit oui ou non avant que Claude travaille.

    invité sans droit « claude » ──► fiche, quiz, campagne, modification… créée « en attente » comme d'habitude
    worker ──► pas de forfait pour ce compte : validation = 'attente', notification aux admins
    admin, page /demandes ──► Oui : 'acceptee', le worker la lance sur le forfait de l'admin (agent.forfait(True))
                          └─► Non : 'refusee', la demande passe en erreur avec la raison, l'invité est prévenu

Un compte avec le droit « claude » (et l'admin) ne passe jamais par là : validation reste NULL.
Le chat « Une question » reste sur les modèles locaux (réponse immédiate, pas de file d'attente).

Accès à tous les cours : un invité lit, utilise dans le chat et dans ses fiches les cours publics des autres.
Il peut demander à lire TOUS les cours de l'environnement, privés compris (comptes.acces_cours) :
    invité ──« Demander l'accès »──► 'attente' + notification aux admins ──► /demandes : Oui 'accorde' / Non 'refuse'
Toujours en lecture seule ; l'admin peut le retirer à tout moment.
"""
import datetime as dt

from claunote import db

REFUS = "L'admin a refusé cette demande : elle ne passera pas par Claude."
GENRES = {"fiche": "Fiche", "quiz": "Quiz", "cartes": "Flashcards", "examen": "Épreuve blanche", "plan": "Plan",
          "campagne": "Campagne"}   # comme revision.COURTS, sans charger l'agent


def libelle(genre: str) -> str:
    return GENRES.get(genre, genre.capitalize())


def accordee(item: dict) -> bool:
    return item.get("validation") == "acceptee"


def demander(table: str, item: dict, auteur: str | None = None) -> bool:
    """Envoie la demande à l'admin (une seule fois) ; vrai si elle vient d'être créée."""
    if not db.mettre_en_demande(table, item["id"]):
        return False
    quoi = "campagne" if table == "campagnes" else item.get("genre", "")
    sujet = item.get("titre") or item.get("sujet") or ""
    qui = auteur or _auteur(item.get("compte_id"))
    action = "Modifier : " if item.get("retouche") else ""
    db.notifier_compte(None, f"demande-ia:{table}:{item['id']}:{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M%S}",
                       f"🙋 {qui} demande l'IA Claude", f"{action}{libelle(quoi)} « {sujet} » : oui ou non ?",
                       "/demandes")
    return True


def decider(quoi: str, item_id: int, oui: bool, raison: str = "") -> dict | None:
    """Décision de l'admin ; prévient l'invité d'un refus (un oui se voit quand le résultat arrive)."""
    if quoi not in ("revision", "campagne"):
        return None
    erreur = REFUS + (f" Raison : {raison.strip()[:300]}" if raison.strip() else "")
    ligne = db.decider_demande(quoi, item_id, oui, erreur)
    if ligne and not oui:
        url = f"/campagne/{item_id}" if quoi == "campagne" else f"/revision/{item_id}"
        db.notifier_compte(ligne["compte_id"], f"demande-ia-refus:{quoi}:{item_id}:{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M%S}",
                           "Demande IA refusée", f"« {ligne['sujet']} » : l'admin a dit non.", url)
    return ligne


def nb_en_attente() -> int:
    """Pastille de l'admin (Réglages) : une panne de la base ne casse pas la page."""
    try:
        return db.nb_demandes_ia()
    except Exception:
        return 0


def attend_admin(item: dict | None, peut_claude: bool) -> bool:
    """La page de l'invité : sa demande attend-elle l'accord de l'admin ?"""
    return bool(item) and not peut_claude and item.get("statut") == "en_attente" and not accordee(item)


def _auteur(compte_id: int | None) -> str:
    c = db.compte(compte_id) if compte_id else None
    return (c.get("nom") or c.get("identifiant")) if c else "Un compte"


# --- Accès à tous les cours de l'environnement (privés compris) -------------------------

def demander_acces(compte: dict) -> bool:
    if not db.demander_acces_cours(compte["id"]):
        return False
    qui = compte.get("nom") or compte.get("identifiant") or "Un compte"
    db.notifier_compte(None, f"acces-cours:{compte['id']}:{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M%S}",
                       f"🔑 {qui} demande l'accès à tous les cours",
                       "Lecture seule de tous les cours, privés compris : oui ou non ?", "/demandes")
    return True


def decider_acces(compte_id: int, oui: bool) -> bool:
    if not db.decider_acces_cours(compte_id, "accorde" if oui else "refuse"):
        return False
    db.notifier_compte(compte_id, f"acces-cours-reponse:{compte_id}:{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M%S}",
                       "Accès à tous les cours accordé" if oui else "Accès à tous les cours refusé",
                       "Ils sont dans ta Bibliothèque, en lecture seule." if oui else "L'admin a dit non.",
                       "/bibliotheque")
    return True


def retirer_acces(compte_id: int) -> bool:
    return db.decider_acces_cours(compte_id, None)
