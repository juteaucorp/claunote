"""Phase 0 : surveille l'inbox et signale les nouveaux fichiers.

Phase 1 remplacera le log par OCR + classement.
"""
import os
import time
from pathlib import Path

ETUDES_DIR = Path(os.environ.get("ETUDES_DIR", "/etudes"))
INTERVALLE = 30  # secondes ; le polling évite les soucis d'inotify sur un montage rclone


def main():
    vus: set[str] = set()
    print(f"worker : surveillance de {ETUDES_DIR / 'inbox'}", flush=True)
    while True:
        inbox = ETUDES_DIR / "inbox"
        if inbox.is_dir():
            for p in inbox.iterdir():
                if p.is_file() and p.name not in vus:
                    vus.add(p.name)
                    print(f"nouveau fichier : {p.name}", flush=True)
        time.sleep(INTERVALLE)


if __name__ == "__main__":
    main()
