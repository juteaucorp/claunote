"""La VM peut-elle encore prendre du travail pour le modèle local, sans saturer ?

Le modèle local (Ollama, OCR Tesseract) tourne sur le processeur : on lui donne le plus possible,
mais pas au point de ralentir tout le serveur ni de faire attendre un document des heures.

    charge du processeur ─┐
    mémoire libre ────────┼──► saturé ? ──► oui : le travail en plus part chez Claude (s'il est là)
    attente estimée ──────┘                 non : le modèle local le prend

Réglages (.env) : LOCAL_CHARGE_MAX (charge moyenne sur 1 min par cœur, 0,85 par défaut),
LOCAL_MEMOIRE_MIN_MO (600 Mo libres au moins), LOCAL_ATTENTE_MAX_MIN (30 min d'attente au plus).

Budget sur la durée : un pic à 100 % est normal (une analyse, une question), une VM à fond en
permanence ne l'est pas. Le worker mesure l'occupation moyenne du processeur sur CPU_FENETRE_MIN
minutes (15 par défaut) ; au-delà de CPU_MOYENNE_MAX (60 %), le travail de fond (analyses locales,
index) ralentit, ou part chez Claude s'il est là, jusqu'à redescendre 10 points plus bas.

    occupation ▲   pic toléré          budget dépassé : le fond fait des pauses
         100 % │  ███                  ▁▁█▁▁█▁▁█▁▁
    moyenne 60 ┼ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─   ← CPU_MOYENNE_MAX
               └──────────────────────────────────────► temps
"""
import math
import os
import threading
import time

CHARGE_MAX = float(os.environ.get("LOCAL_CHARGE_MAX") or 0.85)
MEMOIRE_MIN_MO = int(os.environ.get("LOCAL_MEMOIRE_MIN_MO") or 600)
ATTENTE_MAX_MIN = int(os.environ.get("LOCAL_ATTENTE_MAX_MIN") or 30)
DUREE_DEPART = 120.0  # secondes par document lu sur la VM, avant d'avoir mesuré


def locaux_eteints() -> bool:
    """Modèles locaux éteints depuis le site (Analyses → Modèles locaux) : ni Ollama, ni service lecture, ni
    lecture locale des scans ; tout passe par Claude. Lu dans la base (cache de quelques secondes)."""
    try:
        from claunote import db
        return db.etat_valeur("modeles_locaux") == "eteints"
    except Exception:   # pas de base (tests, démarrage) : allumés, comme avant
        return False


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
    budget = BUDGET.raison()   # mesuré par le worker (le site, lui, ne freine jamais une question)
    if budget:
        return budget
    c = charge()
    if c > CHARGE_MAX and not local_actif():
        return f"processeur chargé par d'autres travaux ({c:.0%} des {coeurs()} cœurs)"
    libre = memoire_libre_mo()
    if libre is not None and libre < MEMOIRE_MIN_MO:
        return f"mémoire presque pleine ({libre} Mo libres)"
    return None


# --- Budget du processeur sur la durée --------------------------------------------------

MOYENNE_MAX = float(os.environ.get("CPU_MOYENNE_MAX") or 0.6)
FENETRE = float(os.environ.get("CPU_FENETRE_MIN") or 15) * 60
REPRISE = 0.1   # on reprend à pleine vitesse 10 points sous la limite (pas de va-et-vient)


def _compteurs_cpu() -> tuple[int, int] | None:
    """(temps occupé, temps total) de tous les cœurs depuis le démarrage, en tics (/proc/stat de la VM)."""
    try:
        with open("/proc/stat") as f:
            valeurs = [int(v) for v in f.readline().split()[1:9]]
    except (OSError, ValueError):
        return None
    inactif = valeurs[3] + valeurs[4]   # idle + iowait
    return sum(valeurs) - inactif, sum(valeurs)


class Budget:
    """Occupation moyenne du processeur (moyenne glissante exponentielle sur FENETRE secondes)."""

    def __init__(self, maximum: float = MOYENNE_MAX, fenetre: float = FENETRE, lire=_compteurs_cpu, horloge=time.monotonic):
        self.maximum, self.fenetre, self._lire, self._horloge = maximum, fenetre, lire, horloge
        self.moyenne, self.instant, self.freine = 0.0, 0.0, False
        self._dernier, self._verrou = None, threading.Lock()

    def echantillon(self) -> float:
        """Mesure depuis l'appel précédent ; renvoie la moyenne longue (0 à 1)."""
        with self._verrou:
            compteurs, t = self._lire(), self._horloge()
            if compteurs is None:
                return self.moyenne
            if self._dernier:
                (occ0, tot0), t0 = self._dernier
                if compteurs[1] > tot0 and t > t0:
                    self.instant = min(max((compteurs[0] - occ0) / (compteurs[1] - tot0), 0.0), 1.0)
                    poids = 1 - math.exp(-(t - t0) / self.fenetre)
                    self.moyenne += poids * (self.instant - self.moyenne)
            self._dernier = (compteurs, t)
            if self.moyenne > self.maximum:
                self.freine = True
            elif self.moyenne < self.maximum - REPRISE:
                self.freine = False
            return self.moyenne

    def raison(self) -> str | None:
        if not self.freine:
            return None
        return (f"processeur occupé à {round(self.moyenne * 100)} % en moyenne sur {self.fenetre / 60:.0f} min "
                f"(limite {round(self.maximum * 100)} %) : le travail de fond ralentit")


BUDGET = Budget()


def respirer(arret=lambda: False, pas: float = 15.0, maximum: float = 1800.0) -> float:
    """Travail de fond : attend que la moyenne longue repasse sous la limite. Renvoie les secondes attendues.

    Au plus `maximum` secondes d'affilée : un document avance quand même, au ralenti, même si quelque
    chose d'autre occupe la VM en permanence."""
    debut = time.monotonic()
    while not arret() and time.monotonic() - debut < maximum:
        BUDGET.echantillon()
        if not BUDGET.freine:
            break
        time.sleep(pas)
    return time.monotonic() - debut


def etat() -> dict:
    """Pour la page Activité."""
    return {"charge": round(charge(), 2), "coeurs": coeurs(), "memoire_libre_mo": memoire_libre_mo(),
            "sature": sature(), "duree_moyenne_s": round(DUREES.moyenne), "charge_max": CHARGE_MAX,
            "attente_max_min": ATTENTE_MAX_MIN, "moyenne_max": MOYENNE_MAX, "fenetre_min": round(FENETRE / 60)}


def publier() -> dict:
    """Budget vu par le worker, publié pour le site (page Activité)."""
    return {"moyenne": round(BUDGET.moyenne, 3), "instant": round(BUDGET.instant, 3), "freine": BUDGET.freine}
