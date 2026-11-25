"""Empreinte du code qui tourne, pour voir si le site et le worker sont à la même version.

Les deux viennent de la même image, mais une mise à jour peut n'en reconstruire qu'un
(`docker compose up -d --build web`) : le worker garde alors l'ancien code, par exemple l'ancienne
façon d'envoyer les notifications. Le worker publie son empreinte, le site compare avec la sienne.
"""
import hashlib
from functools import lru_cache
from pathlib import Path

DOSSIER = Path(__file__).parent


@lru_cache(maxsize=1)
def code() -> str:
    h = hashlib.sha256()
    for f in sorted(DOSSIER.glob("*.py")):
        h.update(f.name.encode() + b"\0" + f.read_bytes())
    return h.hexdigest()[:12]


def worker_a_jour(service: dict | None) -> bool | None:
    """None : on ne sait pas (worker arrêté ou d'avant cette vérification)."""
    if not service or not service.get("code"):
        return None
    return service["code"] == code()
