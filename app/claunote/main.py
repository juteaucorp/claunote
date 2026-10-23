import os

from fastapi import FastAPI

from claunote.kdrive import KDrive

INBOX_ID = int(os.environ.get("KDRIVE_INBOX_ID", "0"))

app = FastAPI(title="Claunote")


@app.get("/health")
def health():
    # Phase 0 : vérifie que le jeton kDrive donne bien accès au dossier inbox
    try:
        KDrive.from_env().stat(INBOX_ID)
        return {"ok": True, "kdrive": True}
    except Exception as e:
        return {"ok": False, "kdrive": False, "erreur": str(e)}


@app.get("/inbox")
def inbox():
    items = KDrive.from_env().list_dir(INBOX_ID)
    return {"fichiers": sorted(i["name"] for i in items if i["type"] == "file")}
