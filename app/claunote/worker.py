"""Phase 0 : surveille l'inbox kDrive via l'API et télécharge les nouveaux fichiers.

Phase 1 remplacera le simple téléchargement par OCR + classement, puis
rangera le fichier dans archives/ avec KDrive.move().
"""
import os
import time
from pathlib import Path

from claunote.kdrive import KDrive

INBOX_ID = int(os.environ["KDRIVE_INBOX_ID"])
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/cache"))
INTERVALLE = 60  # secondes entre deux vérifications de l'inbox


def main():
    kd = KDrive.from_env()
    vus: set[int] = set()
    print(f"worker : surveillance du dossier kDrive {INBOX_ID}", flush=True)
    while True:
        try:
            for item in kd.list_dir(INBOX_ID):
                if item["type"] == "file" and item["id"] not in vus:
                    kd.download(item["id"], CACHE_DIR / "inbox" / item["name"])
                    vus.add(item["id"])
                    print(f"nouveau fichier : {item['name']}", flush=True)
        except Exception as e:
            print(f"erreur kDrive : {e}", flush=True)
        time.sleep(INTERVALLE)


if __name__ == "__main__":
    main()
