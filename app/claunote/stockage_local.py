"""Fichiers sur la VM (STOCKAGE=local ou les_deux) : la même interface que le client kDrive.

    KDrive.from_env() ──► kDrive (Infomaniak)              STOCKAGE=kdrive
                     └──► Local(/fichiers)                 STOCKAGE=local ou les_deux
                             ├── Inbox/       scans à analyser (déposés sur le site, ou copiés à la main)
                             ├── Etudes/      cours rangés par année et matière
                             └── .corbeille/  documents supprimés, vidée après 30 jours
    les_deux : en plus, le worker copie Etudes/ sur kDrive (copie de sauvegarde, dans un seul sens)

Le reste du code désigne fichiers et dossiers par un numéro, comme kDrive : la table fichiers_locaux donne un
numéro stable à chaque chemin (il suit un fichier renommé ou déplacé). Un fichier introuvable lève la même
erreur 404 que kDrive, pour que le worker le traite pareil.
"""
import os
import shutil
import tempfile
import time
from pathlib import Path

import httpx

from claunote import db

RACINE = "."
CORBEILLE = ".corbeille"
GARDE_CORBEILLE = 30 * 86400


def dossier() -> Path:
    return Path(os.environ.get("FICHIERS_DIR") or "/fichiers")


def _introuvable(fichier_id) -> httpx.HTTPStatusError:
    requete = httpx.Request("GET", f"local://fichiers/{fichier_id}")
    return httpx.HTTPStatusError(f"fichier {fichier_id} introuvable sur la VM", request=requete,
                                 response=httpx.Response(404, request=requete))


def nom_sur(nom: str) -> str:
    """Un nom de fichier venu d'ailleurs (dépôt, Pronote) ne sort jamais de son dossier."""
    propre = "".join("_" if c in "/\\\0" else c for c in (nom or "")).strip()
    return propre if propre not in ("", ".", "..") else "sans-nom"


class Local:
    def __init__(self, racine: Path):
        self.racine = racine.resolve()
        for sous in ("Inbox", "Etudes", CORBEILLE):
            (self.racine / sous).mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "Local":
        return cls(dossier())

    # --- numéros <-> chemins ---
    def _rel(self, p: Path) -> str:
        rel = p.relative_to(self.racine).as_posix()
        return rel or RACINE

    def _id(self, p: Path) -> int:
        return db.fichier_local_id(self._rel(p))

    def _chemin(self, fichier_id: int) -> Path:
        rel = db.fichier_local_chemin(int(fichier_id))
        if rel is None:
            raise _introuvable(fichier_id)
        p = (self.racine / rel).resolve()
        dedans = p == self.racine or self.racine in p.parents
        if not dedans or not p.exists() or rel.split("/")[0] == CORBEILLE:   # mis à la corbeille : introuvable
            raise _introuvable(fichier_id)
            raise _introuvable(fichier_id)
        return p

    def _info(self, p: Path) -> dict:
        st = p.stat()
        return {"id": self._id(p), "name": p.name, "type": "dir" if p.is_dir() else "file",
                "size": 0 if p.is_dir() else st.st_size, "last_modified_at": int(st.st_mtime)}

    def _libre(self, dossier_: Path, nom: str) -> Path:
        """« nom (1).pdf », « nom (2).pdf »… si le nom est pris (comme conflict=rename de kDrive)."""
        cible = dossier_ / nom
        base, ext = Path(nom).stem, Path(nom).suffix
        n = 1
        while cible.exists():
            cible = dossier_ / f"{base} ({n}){ext}"
            n += 1
        return cible

    def _deplacer(self, p: Path, cible: Path) -> Path:
        ancien = self._rel(p)
        p.rename(cible)
        db.fichiers_locaux_deplacer(ancien, self._rel(cible))
        return cible

    # --- l'interface de KDrive ---
    def racines(self) -> tuple[int, int]:
        """(Etudes, Inbox) : ce que KDRIVE_ETUDES_ID et KDRIVE_INBOX_ID sont pour kDrive."""
        return self._id(self.racine / "Etudes"), self._id(self.racine / "Inbox")

    def list_dir(self, folder_id: int) -> list[dict]:
        d = self._chemin(folder_id)
        return [self._info(e) for e in sorted(d.iterdir()) if not e.name.startswith(".")]

    def stat(self, file_id: int) -> dict:
        return self._info(self._chemin(file_id))

    def download(self, file_id: int, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._chemin(file_id), dest)
        return dest

    def mkdir(self, parent_id: int, name: str) -> dict:
        d = self._libre(self._chemin(parent_id), nom_sur(name))
        d.mkdir()
        return self._info(d)

    def find_or_create_dir(self, parent_id: int, name: str) -> int:
        d = self._chemin(parent_id) / nom_sur(name)
        if d.is_dir():
            return self._id(d)
        d.mkdir(parents=True, exist_ok=True)
        return self._id(d)

    def ensure_path(self, parent_id: int, parts: list[str]) -> int:
        for name in parts:
            parent_id = self.find_or_create_dir(parent_id, name)
        return parent_id

    def rename(self, file_id: int, name: str) -> None:
        p = self._chemin(file_id)
        if nom_sur(name) != p.name:
            self._deplacer(p, self._libre(p.parent, nom_sur(name)))

    def delete(self, file_id: int) -> None:
        """Comme la corbeille kDrive : rien n'est effacé tout de suite (purger_corbeille, après 30 jours)."""
        p = self._chemin(file_id)
        self._deplacer(p, self._libre(self.racine / CORBEILLE, f"{int(time.time())}-{p.name}"))

    def move(self, file_id: int, dest_dir_id: int) -> None:
        p, dest = self._chemin(file_id), self._chemin(dest_dir_id)
        if p.parent != dest:
            self._deplacer(p, self._libre(dest, p.name))

    def _ecrire(self, dir_id: int, name: str, conflict: str, remplir) -> dict:
        d = self._chemin(dir_id)
        cible = d / nom_sur(name) if conflict == "version" else self._libre(d, nom_sur(name))
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".envoi-")
        try:
            with os.fdopen(fd, "wb") as f:
                remplir(f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, cible)   # d'un coup : le worker ne lit jamais un fichier à moitié écrit
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return self._info(cible)

    def upload(self, dir_id: int, name: str, content: bytes, conflict: str = "version") -> dict:
        return self._ecrire(dir_id, name, conflict, lambda f: f.write(content))

    def upload_flux(self, dir_id: int, name: str, fichier, taille: int, conflict: str = "rename") -> dict:
        fichier.seek(0)
        return self._ecrire(dir_id, name, conflict, lambda f: shutil.copyfileobj(fichier, f, 1024 * 1024))

    # --- propre au stockage sur la VM ---
    def purger_corbeille(self, garde: float = GARDE_CORBEILLE) -> int:
        n = 0
        for e in (self.racine / CORBEILLE).iterdir():
            if time.time() - e.lstat().st_mtime > garde:
                shutil.rmtree(e) if e.is_dir() and not e.is_symlink() else e.unlink()
                n += 1
        return n

    def copier_vers_kdrive(self, kd, dest_id: int, arret=None) -> int:
        """« Les deux » : copie sur kDrive ce qui est nouveau ou modifié dans Etudes/ (rien n'y est jamais effacé).
        Renvoie le nombre de fichiers copiés."""
        faites, n, dossiers = db.copies_kdrive(), 0, {}
        etudes = self.racine / "Etudes"
        for p in sorted(etudes.rglob("*")):
            if arret is not None and arret.is_set():
                break
            rel = p.relative_to(etudes)
            if not p.is_file() or any(x.startswith(".") for x in rel.parts):
                continue
            st, cle = p.stat(), rel.as_posix()
            deja = faites.get(cle)
            if deja and deja["taille"] == st.st_size and abs(deja["mtime"] - st.st_mtime) < 1:
                continue
            parties = rel.parts[:-1]
            if parties not in dossiers:
                dossiers[parties] = kd.ensure_path(dest_id, list(parties))
            with open(p, "rb") as f:
                info = kd.upload_flux(dossiers[parties], p.name, f, st.st_size, conflict="version")
            db.copie_kdrive_faite(cle, st.st_size, st.st_mtime, (info or {}).get("id"))
            n += 1
        return n
