"""Comptes du site : l'admin invite, chaque compte a ses droits et ses propres données.

    admin ──« Inviter »──► jeton (15 min, un seul usage) ──► /invitation : la personne choisit
                                                              identifiant + mot de passe
    chaque requête connectée ──► db.pour_compte(id) : la base ne montre que ses lignes (row-level security)
    admin ──« Tout voir »──────► db.pour_compte(id, tout_voir=True) : toutes les données, de tous les comptes

Droits par fonctionnalité (DROITS) ; l'admin les a tous. Le droit « claude » autorise le forfait Claude de
l'admin : sans lui, le compte passe par les modèles locaux, et ses fiches, quiz, campagnes attendent l'accord de
l'admin (demandes_ia.py) avant de partir sur le forfait.
"""
import datetime as dt
import hashlib
import os
import re
import secrets
import time
from contextlib import contextmanager

from claunote import agent, auth, db

DROITS = {
    "bibliotheque": ("Bibliothèque", "Voir et chercher dans ses cours"),
    "deposer": ("Déposer", "Envoyer des scans et des fichiers"),
    "organiser": ("Organiser", "Classer, corriger, « À vérifier », supprimer ses documents"),
    "reviser": ("Réviser", "Fiches, quiz, campagnes, flashcards, questions sur ses cours"),
    "agenda": ("Agenda Pronote", "Voir l'agenda et les notes Pronote de l'environnement"),
    "actualites": ("Actualités", "Lire et régler les actualités partagées"),
    "claude": ("Forfait Claude de l'admin", "L'IA de Claude sur ton abonnement sans demander ; sinon chaque fiche, "
                                            "quiz ou campagne attend ton oui (page Demandes IA)"),
}
DROITS_INVITE = ["bibliotheque", "deposer", "organiser", "reviser", "actualites"]   # cochés par défaut
ROLES = {"admin": "Admin", "invite": "Invité"}
DUREE_JETON = 15 * 60
CONDITIONS_VERSION = "2026-11-17"    # à changer quand les conditions changent : chacun les accepte de nouveau
IDENTIFIANT = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
MDP_MIN = 10

# Ce que demande chaque page. Pas dans la liste : tout compte connecté (Compte, Notifications, État…).
# « admin » : réservé aux admins. Premier préfixe qui correspond (du plus précis au plus large).
PAGES = [
    ("/comptes", "admin"), ("/demandes", "admin"), ("/securite", "admin"), ("/mise-a-jour", "admin"), ("/appareils/", "admin"),
    ("/analyses", "admin"), ("/import", "admin"), ("/inbox", "admin"), ("/scan", "admin"),
    ("/claude/reessayer", "admin"), ("/installation", "admin"), ("/reglages/installation", "admin"), ("/organiser/matieres", "organiser"), ("/actualites/sources", "actualites"),
    ("/deposer", "deposer"),
    ("/a-verifier", "organiser"), ("/organiser", "organiser"), ("/hors-cours", "bibliotheque"),
    ("/reviser", "reviser"), ("/revision/", "reviser"), ("/campagne", "reviser"), ("/demander", "reviser"),
    ("/formule", "reviser"), ("/cartes", "reviser"), ("/archives", "reviser"), ("/progression", "reviser"),
    # Pronote est celui de l'environnement (un seul compte relié) : le relier, le couper ou le synchroniser
    # change l'agenda de tout le monde, donc réservé à l'admin ; « agenda » ne donne que la lecture.
    ("/agenda/connexion", "admin"), ("/agenda/connexion-qr", "admin"), ("/agenda/deconnexion", "admin"),
    ("/agenda/synchroniser", "admin"),
    ("/agenda", "agenda"), ("/actualites", "actualites"),
    ("/bibliotheque", "bibliotheque"), ("/liste", "bibliotheque"), ("/recherche", "bibliotheque"),
    ("/piece/", "reviser"), ("/partages", "bibliotheque"),
]
ACTIONS_DOC = {"classer", "hors-cours", "supprimer", "reessayer"}


def droit_requis(chemin: str, methode: str = "GET") -> str | None:
    if chemin.startswith("/doc/"):
        action = chemin.rstrip("/").rsplit("/", 1)[-1]
        return "organiser" if methode != "GET" and action in ACTIONS_DOC else "bibliotheque"
    for prefixe, droit in PAGES:
        base = prefixe.rstrip("/")
        if chemin == base or chemin.startswith(base + "/"):
            return droit
    return None


# Actions qui ne marchent qu'avec Claude et qui répondent tout de suite : refusées à un compte sans le droit
# « claude ». Les fiches, quiz, campagnes et modifications passent, elles : sans forfait, elles attendent que l'admin
# dise oui sur la page Demandes (demandes_ia.py). Le chat (Demander) passe au modèle local.
CLAUDE_REQUIS = re.compile(r"^/organiser/matieres/ia$")


def besoin_claude(chemin: str, methode: str) -> bool:
    return methode == "POST" and bool(CLAUDE_REQUIS.match(chemin))


def est_admin(compte: dict | None) -> bool:
    return bool(compte) and compte.get("role") == "admin"


def peut(compte: dict | None, droit: str | None) -> bool:
    if droit is None:
        return bool(compte)
    if est_admin(compte):
        return True
    return bool(compte) and droit != "admin" and droit in (compte.get("droits") or [])


def utilisable(compte: dict | None) -> bool:
    """Compte actif et pas arrivé au bout de son accès temporaire."""
    if not compte or not compte.get("actif"):
        return False
    fin = compte.get("expire_le")
    return fin is None or fin > dt.datetime.now(dt.timezone.utc)


def doit_accepter(compte: dict | None) -> bool:
    return bool(compte) and compte.get("conditions_version") != CONDITIONS_VERSION


# --- Cache : une requête lit son compte sans aller en base à chaque fois ----------------

_cache: dict[int, tuple[float, dict | None]] = {}
CACHE_S = 5.0


def trouver(compte_id: int) -> dict | None:
    vu = _cache.get(compte_id)
    if vu and time.monotonic() - vu[0] < CACHE_S:
        return vu[1]
    compte = db.compte(compte_id)
    _cache[compte_id] = (time.monotonic(), compte)
    return compte


def oublier(compte_id: int | None = None):
    if compte_id is None:
        _cache.clear()
    else:
        _cache.pop(compte_id, None)


def peut_claude(compte_id: int | None) -> bool:
    """Le travail de ce compte peut-il passer par le forfait Claude de l'admin ?"""
    if compte_id is None:
        return True
    return peut(trouver(compte_id), "claude")


@contextmanager
def pour(compte_id: int | None, tout_voir: bool = False):
    """Le temps du bloc : données de ce compte seulement, et forfait Claude selon son droit (worker, site)."""
    with db.pour_compte(compte_id, tout_voir), agent.forfait(peut_claude(compte_id)):
        yield


# --- Migration : l'installation d'avant les comptes ------------------------------------

def migrer():
    """Le compte 1 (admin) reprend l'identifiant et le mot de passe d'avant (page Compte, sinon .env)."""
    admin = db.compte(1)
    if admin is None or admin.get("hash"):
        return
    identifiant = (os.environ.get("CLAUNOTE_UTILISATEUR") or auth.DEFAUT_UTILISATEUR).strip().lower()
    mdp = db.etat_valeur("mot_de_passe") or os.environ.get("CLAUNOTE_MOT_DE_PASSE") or auth.DEFAUT_MOT_DE_PASSE
    defaut = identifiant == auth.DEFAUT_UTILISATEUR and mdp == auth.DEFAUT_MOT_DE_PASSE
    hache = mdp if mdp.startswith("scrypt:") else auth.hacher(mdp)
    if identifiant != admin["identifiant"]:
        db.renommer_compte(1, identifiant)
    # L'admin d'avant a déjà accepté de fait : c'est son propre projet
    db.maj_compte(1, hash=hache, mdp_defaut=defaut, role="admin", actif=True,
                  conditions_version=CONDITIONS_VERSION, conditions_le=dt.datetime.now(dt.timezone.utc))
    oublier(1)


# --- Jetons d'invitation et de nouveau mot de passe ------------------------------------

def empreinte(jeton: str) -> str:
    return hashlib.sha256(jeton.strip().encode()).hexdigest()


def nouveau_jeton() -> str:
    """Lisible à dicter ou recopier : 5 groupes de 5 lettres et chiffres (~120 bits)."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(5))


def normaliser_jeton(jeton: str) -> str:
    brut = re.sub(r"[^A-Za-z0-9]", "", jeton or "").upper()
    return "-".join(brut[i:i + 5] for i in range(0, len(brut), 5))


def inviter(admin_id: int, role: str = "invite", droits: list[str] | None = None, note: str = "") -> tuple[str, dt.datetime]:
    role = role if role in ROLES else "invite"
    droits = [d for d in (droits if droits is not None else DROITS_INVITE) if d in DROITS]
    jeton = nouveau_jeton()
    fin = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=DUREE_JETON)
    db.creer_invitation(empreinte(jeton), "compte", fin, role, droits, note.strip()[:120] or None, admin_id)
    return jeton, fin


def lien_mot_de_passe(admin_id: int, compte_id: int) -> tuple[str, dt.datetime]:
    """Réinitialiser : l'admin ne connaît jamais le nouveau mot de passe, la personne le choisit."""
    jeton = nouveau_jeton()
    fin = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=DUREE_JETON)
    db.creer_invitation(empreinte(jeton), "mot_de_passe", fin, cree_par=admin_id, compte_id=compte_id)
    return jeton, fin


def invitation(jeton: str) -> dict | None:
    return db.invitation(empreinte(normaliser_jeton(jeton))) if jeton else None


def verifier_identifiant(identifiant: str) -> str | None:
    """None si l'identifiant convient, sinon pourquoi."""
    if not IDENTIFIANT.match(identifiant):
        return "Identifiant : 2 à 32 caractères, lettres minuscules, chiffres, point, tiret ou soulignement."
    if db.compte_par_identifiant(identifiant):
        return "Cet identifiant est déjà pris."
    return None


def verifier_mot_de_passe(mdp: str, confirmation: str, identifiant: str = "") -> str | None:
    if len(mdp) < MDP_MIN:
        return f"Le mot de passe doit faire au moins {MDP_MIN} caractères."
    if mdp != confirmation:
        return "Les deux mots de passe sont différents."
    if mdp == auth.DEFAUT_MOT_DE_PASSE or (identifiant and identifiant.lower() in mdp.lower()):
        return "Choisis un mot de passe moins facile à deviner."
    return None


def utiliser_invitation(inv: dict, identifiant: str, mdp: str, nom: str = "") -> int | None:
    """Crée le compte de l'invitation ; None si le jeton vient d'être utilisé ou l'identifiant pris.
    Le jeton n'est brûlé qu'une fois le compte créé ; deux envois en même temps : un seul compte reste."""
    compte_id = db.creer_compte(identifiant, auth.hacher(mdp), inv["role"], list(inv["droits"] or []),
                                nom.strip()[:60] or None, inv.get("cree_par"))
    if not compte_id:
        return None
    if not db.utiliser_invitation(inv["id"], compte_id):
        db.supprimer_compte(compte_id)
        return None
    db.maj_compte(compte_id, conditions_version=CONDITIONS_VERSION, conditions_le=dt.datetime.now(dt.timezone.utc))
    return compte_id


def utiliser_lien_mot_de_passe(inv: dict, mdp: str) -> bool:
    if not inv.get("compte_id") or not db.utiliser_invitation(inv["id"], inv["compte_id"]):
        return False
    changer_mot_de_passe(inv["compte_id"], mdp)
    db.deconnecter_compte(inv["compte_id"])
    oublier(inv["compte_id"])
    return True


def changer_mot_de_passe(compte_id: int, mdp: str):
    db.maj_compte(compte_id, hash=auth.hacher(mdp), mdp_defaut=False)
    oublier(compte_id)
