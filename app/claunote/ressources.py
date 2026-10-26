"""La VM peut-elle encore prendre du travail pour le modèle local, sans saturer ?

Le modèle local (Ollama, OCR Tesseract) tourne sur le processeur : on lui donne le plus possible,
mais pas au point de ralentir tout le serveur ni de faire attendre un document des heures.

    charge du processeur ─┐
    mémoire libre ────────┼──► saturé ? ──► oui : le travail en plus part chez Claude (s'il est là)
    attente estimée ──────┘                 non : le modèle local le prend

Réglages (.env) : LOCAL_CHARGE_MAX (charge moyenne sur 1 min par cœur, 0,85 par défaut),
LOCAL_MEMOIRE_MIN_MO (600 Mo libres au moins), LOCAL_ATTENTE_MAX_MIN (30 min d'attente au plus).
"""
import os
import threading
import time

CHARGE_MAX = float(os.environ.get("LOCAL_CHARGE_MAX") or 0.85)
MEMOIRE_MIN_MO = int(os.environ.get("LOCAL_MEMOIRE_MIN_MO") or 600)
ATTENTE_MAX_MIN = int(os.environ.get("LOCAL_ATTENTE_MAX_MIN") or 30)
DUREE_DEPART = 120.0  # secondes par document lu sur la VM, avant d'avoir mesuré


def coeurs() -> int:
    try:
        return len(os.sched_getaffinity(0)) or 1
    except AttributeError:
        return os.cpu_count() or 1


def charge() -> float:
    """Charge moyenne sur 1 minute, par cœur (1,0 : tous les cœurs occupés)."""
    try:
        return os.getloadavg()[0] / coeurs()
    except OSError:
        return 0.0


def memoire_libre_mo() -> int | None:
    try:
        with open("/proc/meminfo") as f:
            for ligne in f:
                if ligne.startswith("MemAvailable:"):
                    return int(ligne.split()[1]) // 1024
    except OSError:
        pass
    return None


class Durees:
    """Durée moyenne d'une analyse locale (moyenne glissante), pour estimer l'attente de la file."""

    def __init__(self, depart: float = DUREE_DEPART):
        self.moyenne, self._verrou = depart, threading.Lock()

    def noter(self, secondes: float):
        noter_local()
        with self._verrou:
            self.moyenne = 0.8 * self.moyenne + 0.2 * max(secondes, 1.0)


DUREES = Durees()


def attente_min(en_file: int, fils: int = 1) -> float:
    """Minutes avant que le modèle local ait fini `en_file` documents."""
    return en_file * DUREES.moyenne / max(fils, 1) / 60


_dernier_local = {"le": 0.0}


def local_actif(secondes: float = 90) -> bool:
    """Une analyse locale a tourné récemment : la charge du processeur vient alors surtout d'elle."""
    return time.monotonic() - _dernier_local["le"] < secondes


def noter_local():
    _dernier_local["le"] = time.monotonic()


def sature() -> str | None:
    """Pourquoi la VM ne doit pas prendre plus de travail local en ce moment, ou None.

    Ollama occupe tous les cœurs pendant une analyse : la charge du processeur ne compte que quand le
    modèle local est au repos (c'est alors le reste du serveur qui est occupé). Pendant qu'il travaille,
    c'est l'attente de la file qui décide (voir worker.repartir) et la mémoire.
    """
    c = charge()
    if c > CHARGE_MAX and not local_actif():
        return f"processeur chargé par d'autres travaux ({c:.0%} des {coeurs()} cœurs)"
    libre = memoire_libre_mo()
    if libre is not None and libre < MEMOIRE_MIN_MO:
        return f"mémoire presque pleine ({libre} Mo libres)"
    return None


def etat() -> dict:
    """Pour la page Activité."""
    return {"charge": round(charge(), 2), "coeurs": coeurs(), "memoire_libre_mo": memoire_libre_mo(),
            "sature": sature(), "duree_moyenne_s": round(DUREES.moyenne), "charge_max": CHARGE_MAX,
            "attente_max_min": ATTENTE_MAX_MIN}
