"""Comptes du site : l'admin invite, chaque compte a ses droits et ses propres données.

    admin ──« Inviter »──► jeton (15 min, un seul usage) ──► /invitation : la personne choisit
                                                              identifiant + mot de passe
    chaque requête connectée ──► db.pour_compte(id) : la base ne montre que ses lignes (row-level security)
    admin ──« Tout voir »──────► db.pour_compte(id, tout_voir=True) : toutes les données, de tous les comptes

Droits par fonctionnalité (DROITS) ; l'admin les a tous. Le forfait Claude de l'admin a trois niveaux (FORFAIT) :
en autonomie (droit « claude »), sur demande (« claude_demande », l'admin dit oui ou non : demandes_ia.py), ou aucun.
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
    "actualites": ("Actualités", "Son propre fil d'actualités (sources, sujets, alertes)"),
    # Le forfait Claude : un choix à trois niveaux (FORFAIT), pas une case. Gardés ici pour les droits enregistrés.
    "claude": ("Forfait Claude de l'admin", "En autonomie : l'IA de Claude sur ton abonnement, sans demander"),
    "claude_demande": ("Forfait Claude sur demande", "Chaque fiche, quiz ou campagne attend ton oui (page Demandes IA)"),
}
# Accès au forfait Claude de l'admin, choisi pour chaque compte (invitation, page Comptes) :
#   autonomie ──► droit « claude » : Claude travaille pour lui directement
#   demande   ──► droit « claude_demande » : chaque tâche IA attend le oui de l'admin (demandes_ia.py)
#   aucun     ──► ni l'un ni l'autre : fiches, quiz, campagnes refusés ; le chat reste sur les modèles locaux
FORFAIT = {
    "autonomie": ("En autonomie", "Utilise ton forfait Claude directement, sans te demander."),
    "demande": ("Sur demande", "Chaque fiche, quiz, campagne ou modification attend ton oui (Réglages › Demandes IA)."),
    "aucun": ("Aucun", "Pas de fiche, quiz ni campagne par l'IA ; le chat reste sur les modèles locaux de la VM."),
}
FORFAIT_DROIT = {"autonomie": "claude", "demande": "claude_demande", "aucun": None}
DROITS_CASES = [d for d in DROITS if d not in ("claude", "claude_demande")]   # les autres : des cases à cocher
DROITS_INVITE = ["bibliotheque", "deposer", "organiser", "reviser", "actualites", "claude_demande"]   # par défaut
FORFAIT_INVITE = "demande"
ROLES = {"admin": "Admin", "invite": "Invité"}
DUREE_JETON = 15 * 60
CONDITIONS_VERSION = "2026-11-17"    # à changer quand les conditions changent : chacun les accepte de nouveau
IDENTIFIANT = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
MDP_MIN = 10

# Ce que demande chaque page. Pas dans la liste : tout compte connecté (Compte, Notifications, État,
# ses propres appareils par Internet : /appareils/…).
# « admin » : réservé aux admins. Premier préfixe qui correspond (du plus précis au plus large).
PAGES = [
    ("/comptes", "admin"), ("/demandes", "admin"), ("/securite", "admin"), ("/mise-a-jour", "admin"),
    ("/analyses", "admin"), ("/inbox", "admin"), ("/scan", "admin"),
    ("/claude/reessayer", "admin"), ("/installation", "admin"), ("/reglages/installation", "admin"), ("/organiser/matieres", "organiser"), ("/actualites/sources", "actualites"),
    ("/deposer", "deposer"),
    ("/import", "organiser"),   # chacun trie ses propres dépôts (fichiers inutiles, scans, documents personnels)
    ("/a-verifier", "organiser"), ("/organiser", "organiser"), ("/hors-cours", "bibliotheque"),
    ("/reviser", "reviser"), ("/revision/", "reviser"), ("/campagne", "reviser"), ("/demander", "reviser"),
    ("/formule", "reviser"), ("/cartes", "reviser"), ("/archives", "reviser"), ("/progression", "reviser"),
    # Pronote est celui de l'environnement (un seul compte relié) : le relier, le couper ou le synchroniser
    # change l'agenda de tout le monde, donc réservé à l'admin ; « agenda » ne donne que la lecture.
    ("/agenda/connexion", "admin"), ("/agenda/connexion-qr", "admin"), ("/agenda/deconnexion", "admin"),
    ("/agenda/synchroniser", "admin"),
    ("/agenda", "agenda"), ("/actualites", "actualites"),
    ("/bibliotheque", "bibliotheque"), ("/liste", "bibliotheque"), ("/recherche", "bibliotheque"),
    ("/piece/", "reviser"), ("/partages", "bibliotheque"), ("/acces-cours", "bibliotheque"),
]
# Pages de rangement où l'admin ne travaille que sur ses propres dépôts, même avec « Tout voir » : chaque compte
# trie ses imports, choisit sa formation et range ses matières (l'admin garde l'accès aux documents des autres
# par « Tout voir » sur Tous les documents, Bibliothèque…).
SES_DEPOTS = ("/import", "/organiser/matieres", "/deposer", "/actualites")   # « Actualités » : son fil à lui, pas un mélange


def tout_voir(compte: dict | None, chemin: str) -> bool:
    """« Tout voir » s'applique-t-il à cette page ?"""
    if not est_admin(compte) or not compte.get("tout_voir"):
        return False
    return not any(chemin == p or chemin.startswith(p + "/") for p in SES_DEPOTS)


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


# Pages où un invité se sert aussi des cours partagés (publics, ou tous s'il a l'accès complet), en lecture seule :
# choisir les cours d'une fiche ou d'une campagne, et le chat « Une question ». Ailleurs (historique, Organiser…),
# il ne voit que ses données.
LECTURE_PARTAGES = re.compile(r"^/(reviser/(nouvelle|documents|demande|lancer)|campagne/nouvelle|demander(/.*)?)$")
CREATION_PARTAGES = {"/reviser", "/campagne"}


def lit_les_partages(compte: dict | None, chemin: str, methode: str = "GET") -> bool:
    if not compte or est_admin(compte):
        return False   # l'admin : ses données (ou « Tout voir »), comme avant
    return bool(LECTURE_PARTAGES.match(chemin)) or (methode == "POST" and chemin in CREATION_PARTAGES)


def besoin_claude(chemin: str, methode: str) -> bool:
    return methode == "POST" and bool(CLAUDE_REQUIS.match(chemin))


# Créations par l'IA (fiches, quiz, campagnes, modifications) : refusées tout de suite à un compte « Aucun »
CREATION_IA = re.compile(r"^/(reviser|reviser/lancer|campagne|revision/\d+/(relancer|modifier)|campagne/\d+/relancer)$")


def creation_ia(chemin: str, methode: str) -> bool:
    return methode == "POST" and bool(CREATION_IA.match(chemin))


def niveau_forfait(compte: dict | None) -> str:
    """'autonomie', 'demande' ou 'aucun'."""
    if est_admin(compte):
        return "autonomie"
    droits = (compte or {}).get("droits") or []
    return "autonomie" if "claude" in droits else "demande" if "claude_demande" in droits else "aucun"


def avec_forfait(droits: list[str], niveau: str) -> list[str]:
    """Les droits cochés, plus celui du niveau de forfait choisi (un seul des deux droits Claude)."""
    garde = [d for d in droits if d in DROITS and d not in ("claude", "claude_demande")]
    droit = FORFAIT_DROIT.get(niveau if niveau in FORFAIT else FORFAIT_INVITE)
    return garde + ([droit] if droit else [])


def peut_demander_claude(compte_id: int | None) -> bool:
    """Ce compte peut-il soumettre une tâche IA à l'accord de l'admin ?"""
    return compte_id is None or niveau_forfait(trouver(compte_id)) != "aucun"


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
    migrer_forfait()
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


def migrer_forfait():
    """Une seule fois : les invités sans forfait passaient déjà par les demandes (#88) → « Sur demande »."""
    if db.etat_valeur("forfait_niveaux") == "fait":
        return
    for k in db.comptes():
        droits = list(k.get("droits") or [])
        if k["role"] != "admin" and "claude" not in droits and "claude_demande" not in droits:
            db.maj_compte(k["id"], droits=droits + ["claude_demande"])
    db.etat_ecrire("forfait_niveaux", "fait")
    oublier()


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


def inviter(admin_id: int, role: str = "invite", droits: list[str] | None = None, note: str = "",
            appareil: str = "") -> tuple[str, dt.datetime]:
    """Jeton de création de compte ; `appareil` : nom de l'appareil que le lien approuve pour Internet."""
    role = role if role in ROLES else "invite"
    droits = [d for d in (droits if droits is not None else DROITS_INVITE) if d in DROITS]
    jeton = nouveau_jeton()
    fin = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=DUREE_JETON)
    db.creer_invitation(empreinte(jeton), "compte", fin, role, droits, note.strip()[:120] or None, admin_id,
                        appareil=appareil.strip()[:60] or None)
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
