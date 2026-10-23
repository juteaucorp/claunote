import os
from pathlib import Path

from fastapi import FastAPI

ETUDES_DIR = Path(os.environ.get("ETUDES_DIR", "/etudes"))

app = FastAPI(title="Claunote")


@app.get("/health")
def health():
    # Phase 0 : vérifie que le montage kDrive est visible depuis le conteneur
    return {"ok": True, "kdrive_monte": (ETUDES_DIR / "inbox").is_dir()}


@app.get("/inbox")
def inbox():
    inbox_dir = ETUDES_DIR / "inbox"
    if not inbox_dir.is_dir():
        return {"fichiers": [], "erreur": "inbox introuvable, montage kDrive ?"}
    return {"fichiers": sorted(p.name for p in inbox_dir.iterdir() if p.is_file())}
