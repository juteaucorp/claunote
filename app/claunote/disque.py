"""Espace disque de la machine : assez pour installer et faire tourner Claunote ?

    disque de la VM ──► libre ≥ 15 Go ................ ok
                        libre < 15 Go ................ juste : l'installation complète ne tient pas
                        libre < 5 Go ou < 10 % ....... attention (bandeau admin, notification)
                        libre < 2 Go ................. critique : la base et Docker peuvent planter

Le conteneur voit le disque de Docker (images, volumes : base, modèles, cache des scans), le même que
celui de la VM dans une installation standard. Seuils réglables : DISQUE_ATTENTION_GO, DISQUE_CRITIQUE_GO.
"""
import os
import shutil
import time

GO = 1024 ** 3
# Ce que prend une installation complète, hors documents (à peu près, mesuré sur une VM réelle)
BESOINS = [
    ("Images Docker (site, worker, lecture, Pronote, Caddy)", 6.0),
    ("Modèles locaux Ollama (qwen2.5:3b + recherche par le sens)", 2.5),
    ("Modèles du service lecture (tableaux, formules)", 1.5),
    ("Base de données (transcriptions, index, fiches)", 1.0),
    ("Cache des scans en cours et marge de mise à jour", 4.0),
]
BESOIN_INSTALLATION = sum(go for _, go in BESOINS)   # ~15 Go
ATTENTION_GO = float(os.environ.get("DISQUE_ATTENTION_GO") or 5)
CRITIQUE_GO = float(os.environ.get("DISQUE_CRITIQUE_GO") or 2)
CHEMIN = os.environ.get("DISQUE_CHEMIN") or "/"

_cache = {"le": float("-inf"), "valeur": None}


def mesurer(chemin: str = CHEMIN) -> dict | None:
    try:
        u = shutil.disk_usage(chemin)
    except OSError:
        return None
    return {"total": u.total, "utilise": u.used, "libre": u.free}


def etat(chemin: str = CHEMIN, stockage_local_go: float = 0.0) -> dict | None:
    """{total_go, libre_go, part_libre, niveau (ok, juste, attention, critique), phrase} ; mis en cache 60 s.

    `stockage_local_go` : documents gardés sur la VM (stockage local) en plus du reste."""
    if chemin == CHEMIN and not stockage_local_go and time.monotonic() - _cache["le"] < 60:
        return _cache["valeur"]
    m = mesurer(chemin)
    if m is None:
        return None
    libre, total = m["libre"] / GO, m["total"] / GO
    part = m["libre"] / m["total"] if m["total"] else 0.0
    besoin = BESOIN_INSTALLATION + stockage_local_go
    if libre < CRITIQUE_GO:
        niveau, phrase = "critique", (f"Disque presque plein : {libre:.1f} Go libres. La base et Docker risquent de "
                                      "planter : libère de la place ou agrandis le disque de la VM.")
    elif libre < ATTENTION_GO or part < 0.10:
        niveau, phrase = "attention", (f"Peu de place sur le disque : {libre:.1f} Go libres ({part:.0%}). "
                                       "Pense à agrandir le disque ou à nettoyer (Réglages → Serveur → Libérer de la place).")
    elif libre < besoin:
        niveau, phrase = "juste", (f"{libre:.1f} Go libres : une installation complète demande environ {besoin:.0f} Go. "
                                   "Ça peut marcher sans les modèles locaux, mais c'est juste.")
    else:
        niveau, phrase = "ok", f"{libre:.0f} Go libres sur {total:.0f} Go : assez de place."
    valeur = {"total_go": round(total, 1), "libre_go": round(libre, 1), "utilise_go": round(m["utilise"] / GO, 1),
              "part_libre": part, "niveau": niveau, "phrase": phrase, "besoin_go": round(besoin, 1)}
    if chemin == CHEMIN and not stockage_local_go:
        _cache.update(le=time.monotonic(), valeur=valeur)
    return valeur


def a_signaler(e: dict | None) -> bool:
    return bool(e) and e["niveau"] in ("attention", "critique")
