"""Client minimal de l'API REST kDrive (Infomaniak).

Authentification : jeton API Infomaniak avec le scope « drive »,
envoyé en `Authorization: Bearer`. Les dossiers et fichiers sont
désignés par leur ID numérique (visible dans l'URL de l'appli web kDrive).

Une coupure passagère (réseau, 429 « trop de requêtes », 5xx) est réessayée quelques fois ;
au-delà, KDriveIndisponible dit au worker de patienter sans rien marquer en erreur.
Une requête qui créerait un doublon si on la rejouait (envoi « rename », création de dossier)
n'est réessayée que si elle n'est sûrement pas partie (connexion impossible, 429, 503).
"""
import os
import time
from pathlib import Path

import httpx

API_URL = "https://api.infomaniak.com/2/drive"
UPLOAD_URL = "https://api.kdrive.infomaniak.com/2/drive"
PAGE = 500
TRANSITOIRES = {429, 500, 502, 503, 504}
PAS_PARTIES = {429, 503}            # refusées avant traitement : sans risque de doublon
ATTENTES = (2, 8, 30)               # secondes entre deux essais


class KDriveIndisponible(Exception):
    """kDrive ne répond pas (réseau, panne Infomaniak, jeton refusé) : réessayer plus tard."""


def _attente_demandee(resp: httpx.Response | None, defaut: float) -> float:
    try:
        return min(max(float(resp.headers.get("Retry-After")), defaut), 120) if resp is not None else defaut
    except (TypeError, ValueError):
        return defaut


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

    def _rejouer(self, faire, rejouable: bool = True):
        """Exécute `faire()` en réessayant les erreurs passagères."""
        for attente in (*ATTENTES, None):
            try:
                return faire()
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code == 401:
                    raise KDriveIndisponible("jeton kDrive refusé (401) : vérifie KDRIVE_TOKEN") from e
                if code not in TRANSITOIRES:
                    raise
                if attente is None or not (rejouable or code in PAS_PARTIES):
                    raise KDriveIndisponible(f"kDrive répond {code}") from e
                time.sleep(_attente_demandee(e.response, attente))
            except httpx.TransportError as e:
                pas_partie = isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))
                if attente is None or not (rejouable or pas_partie):
                    raise KDriveIndisponible(f"kDrive injoignable ({e.__class__.__name__})") from e
                time.sleep(attente)

    def list_dir(self, folder_id: int) -> list[dict]:
        """Enfants directs d'un dossier : dicts avec id, name, type (dir/file), size…"""
        items, page = [], 1
        while True:
            batch = self._rejouer(lambda: self._data(self.http.get(
                self._url(f"/files/{folder_id}/files"), params={"per_page": PAGE, "page": page})))
            items += batch
            if len(batch) < PAGE:
                return items
            page += 1

    def stat(self, file_id: int) -> dict:
        return self._rejouer(lambda: self._data(self.http.get(self._url(f"/files/{file_id}"))))

    def download(self, file_id: int, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)

        def telecharger():
            with self.http.stream("GET", self._url(f"/files/{file_id}/download")) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
            return dest
        return self._rejouer(telecharger)

    def mkdir(self, parent_id: int, name: str) -> dict:
        return self._rejouer(lambda: self._data(self.http.post(
            self._url(f"/files/{parent_id}/directory"), json={"name": name})), rejouable=False)

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
        self._rejouer(lambda: self.http.post(self._url(f"/files/{file_id}/rename"), json={"name": name}).raise_for_status())

    def delete(self, file_id: int) -> None:
        """Envoie un fichier ou dossier dans la corbeille kDrive."""
        self._rejouer(lambda: self.http.delete(self._url(f"/files/{file_id}")).raise_for_status())

    def move(self, file_id: int, dest_dir_id: int) -> None:
        self._rejouer(lambda: self.http.post(self._url(f"/files/{file_id}/move/{dest_dir_id}")).raise_for_status())

    def upload(self, dir_id: int, name: str, content: bytes, conflict: str = "version") -> dict:
        """Envoi en une fois (fichiers de quelques Mo : transcriptions, fiches).

        conflict="version" remplace le fichier du même nom : rejouer l'envoi est sans risque ;
        "rename" en créerait une copie, il n'est rejoué que si le premier envoi n'est pas parti.
        """
        params = {"directory_id": dir_id, "file_name": name,
                  "total_size": len(content), "conflict": conflict}
        return self._rejouer(lambda: self._data(self.http.post(self._url("/upload", upload=True), params=params,
                                                               content=content)), rejouable=conflict != "rename")

    def upload_flux(self, dir_id: int, name: str, fichier, taille: int, conflict: str = "rename") -> dict:
        """Envoi d'un gros fichier (scan d'une année entière) lu par morceaux, sans le charger en mémoire.

        `fichier` : objet fichier binaire, relu depuis le début à chaque essai.
        """
        params = {"directory_id": dir_id, "file_name": name, "total_size": taille, "conflict": conflict}

        def morceaux():
            fichier.seek(0)
            while bloc := fichier.read(1024 * 1024):
                yield bloc

        def envoyer():
            return self._data(self.http.post(self._url("/upload", upload=True), params=params, content=morceaux(),
                                              headers={"Content-Length": str(taille)}, timeout=httpx.Timeout(60, write=600)))
        return self._rejouer(envoyer, rejouable=conflict != "rename")

