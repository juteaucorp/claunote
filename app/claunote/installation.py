"""Assistant de première installation (tout sur le site), puis Réglages → Configuration du site pour tout modifier.

    disque ─► compte admin ─► stockage (kDrive) ─► Claude ─► Cloudflare ─► Pronote ─► fini
      │            │                 │                │            │
      │            │                 └── reglages.ecrire() : secrets chiffrés, jamais réaffichés
      │            └── vrai mot de passe à la place de admin / claunote
      └── l'espace libre de la VM suffit-il ? (disque.py)

L'assistant ne s'ouvre que sur une installation NEUVE (aucun document, et mot de passe par défaut ou kDrive pas
réglé). Une installation qui tourne déjà est notée « faite » au premier démarrage : rien ne lui est imposé.
"""
import os
import re

from claunote import db, reglages

ETAPES = [
    ("disque", "Espace disque"),
    ("compte", "Ton compte admin"),
    ("stockage", "Fichiers"),
    ("claude", "Claude"),
    ("cloudflare", "Accès par Internet"),
    ("pronote", "Pronote"),
    ("fin", "C'est prêt"),
]
NOMS = dict(ETAPES)
ORDRE = [e for e, _ in ETAPES]
PAYS = re.compile(r"^(\*|[A-Z]{2}(,[A-Z]{2})*)$")


def a_faire() -> bool:
    return db.etat_valeur("installation") == "a_faire"


def detecter():
    """Au démarrage du site, une seule fois : installation neuve (assistant) ou installation existante."""
    if db.etat_valeur("installation"):
        return
    neuve = db.nb_documents() == 0 and (bool((db.compte(1) or {}).get("mdp_defaut")) or not reglages.stockage_pret())
    db.etat_ecrire("installation", "a_faire" if neuve else "faite")


def terminer():
    db.etat_ecrire("installation", "faite")


def suivante(etape: str) -> str:
    i = ORDRE.index(etape)
    return ORDRE[min(i + 1, len(ORDRE) - 1)]


def precedente(etape: str) -> str | None:
    i = ORDRE.index(etape)
    return ORDRE[i - 1] if i else None


def nombre(valeur: str, nom: str) -> tuple[str, str | None]:
    v = (valeur or "").strip()
    if v and not v.isdigit():
        return v, f"{nom} : un nombre (les chiffres de l'adresse kDrive)."
    return v, None


def pays(valeur: str) -> tuple[str, str | None]:
    v = re.sub(r"\s+", "", (valeur or "").upper())
    if v and not PAYS.match(v):
        return v, "Pays : codes à 2 lettres séparés par des virgules (FR,BE), ou * pour tous."
    return v, None


def secret_ou_garde(cle: str, saisi: str) -> str:
    """Champ secret laissé vide : on garde celui déjà enregistré (jamais renvoyé à la page)."""
    return (saisi or "").strip() or os.environ.get(cle, "")
