"""Client minimal de l'API REST kDrive (Infomaniak).

Authentification : jeton API Infomaniak avec le scope « drive »,
envoyé en `Authorization: Bearer`. Les dossiers et fichiers sont
désignés par leur ID numérique (visible dans l'URL de l'appli web kDrive).
"""
import os
from pathlib import Path

import httpx

API_URL = "https://api.infomaniak.com/2/drive"
UPLOAD_URL = "https://api.kdrive.infomaniak.com/2/drive"
PAGE = 500


class KDrive:
    def __init__(self, token: str, drive_id: str, transport: httpx.BaseTransport | None = None):
        self.drive_id = drive_id
        self.http = httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            transport=transport,
            follow_redirects=True,
        )

    @classmethod
    def from_env(cls) -> "KDrive":
        return cls(os.environ["KDRIVE_TOKEN"], os.environ["KDRIVE_ID"])

    def _url(self, path: str, upload: bool = False) -> str:
        return f"{UPLOAD_URL if upload else API_URL}/{self.drive_id}{path}"

    def _data(self, resp: httpx.Response):
        resp.raise_for_status()
        return resp.json()["data"]

    def list_dir(self, folder_id: int) -> list[dict]:
        """Enfants directs d'un dossier : dicts avec id, name, type (dir/file), size…"""
        items, page = [], 1
        while True:
            batch = self._data(self.http.get(
                self._url(f"/files/{folder_id}/files"), params={"per_page": PAGE, "page": page}))
            items += batch
            if len(batch) < PAGE:
                return items
            page += 1

    def stat(self, file_id: int) -> dict:
        return self._data(self.http.get(self._url(f"/files/{file_id}")))

    def download(self, file_id: int, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.http.stream("GET", self._url(f"/files/{file_id}/download")) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
        return dest

    def mkdir(self, parent_id: int, name: str) -> dict:
        return self._data(self.http.post(self._url(f"/files/{parent_id}/directory"), json={"name": name}))

    def find_or_create_dir(self, parent_id: int, name: str) -> int:
        for item in self.list_dir(parent_id):
            if item["type"] == "dir" and item["name"] == name:
                return item["id"]
        return self.mkdir(parent_id, name)["id"]

    def ensure_path(self, parent_id: int, parts: list[str]) -> int:
        """Crée si besoin une suite de sous-dossiers et renvoie l'ID du dernier."""
        for name in parts:
            parent_id = self.find_or_create_dir(parent_id, name)
        return parent_id

    def rename(self, file_id: int, name: str) -> None:
        self.http.post(self._url(f"/files/{file_id}/rename"), json={"name": name}).raise_for_status()

    def delete(self, file_id: int) -> None:
        """Envoie un fichier ou dossier dans la corbeille kDrive."""
        self.http.delete(self._url(f"/files/{file_id}")).raise_for_status()

    def move(self, file_id: int, dest_dir_id: int) -> None:
        self.http.post(self._url(f"/files/{file_id}/move/{dest_dir_id}")).raise_for_status()

    def upload(self, dir_id: int, name: str, content: bytes, conflict: str = "version") -> dict:
        """Envoi en une fois (fichiers de quelques Mo : transcriptions, fiches)."""
        params = {"directory_id": dir_id, "file_name": name,
                  "total_size": len(content), "conflict": conflict}
        return self._data(self.http.post(self._url("/upload", upload=True), params=params, content=content))
