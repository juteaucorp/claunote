"""Stockage « kDrive seul » : le plus possible sur kDrive, la VM ne garde que ce qui doit rester sur place.

    Choix « kDrive (Infomaniak) » (STOCKAGE=kdrive)
        │
        ├── déjà sur kDrive : scans, cours rangés, transcriptions .md, PDF des fiches, documents Pronote
        │
        ├── ajouté ici, dans Etudes/Claunote/ (Etudes/comptes/<identifiant>/Claunote/ pour un autre compte) :
        │     Pièces jointes/   les fichiers joints aux révisions et campagnes : la base n'en garde plus les octets
        │     Notes/<année>/<matière>/<titre> (n).md   copie de chaque note, refaite quand elle change
        │     Sauvegardes/      la base entière chaque nuit (pg_dump), les 14 dernières gardées (admin seulement)
        │
        └── reste sur la VM (rien d'autre) :
              la base PostgreSQL vivante (recherche, chat, comptes), les secrets, les modèles locaux,
              et un cache limité (CACHE_KDRIVE_MO, 2 Go par défaut) des fichiers kDrive déjà ouverts

Les modes « Sur la VM » et « Les deux » ne changent pas : rien ici ne s'y applique (actif() est faux).
Ce qui était sur la VM avant ce changement part sur kDrive tout seul, par petits lots, au fil des tours du worker.
"""
import datetime as dt
import json
import os
import re
import subprocess
import threading
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

import httpx

from claunote import db, notes, notifications, reglages
from claunote.kdrive import KDrive, KDriveIndisponible
from claunote.stockage_local import nom_sur

DOSSIER = "Claunote"
CACHE_MAX = int(os.environ.get("CACHE_KDRIVE_MO") or 2048) * 1024 * 1024
SAUVEGARDES_GARDEES = int(os.environ.get("SAUVEGARDES_GARDEES") or 14)
HEURE_SAUVEGARDE = 3                     # heure de Paris : la nuit, quand personne n'écrit
REESSAI_SAUVEGARDE = dt.timedelta(hours=1)
COMPACTER_APRES = 8 * 1024 * 1024        # place encore prise par la table des pièces : au-delà, rendue au disque
NOM_SAUVEGARDE = re.compile(r"claunote-\d{4}-\d{2}-\d{2}\.dump")
_verrou = threading.Lock()


def actif() -> bool:
    return reglages.stockage() == "kdrive" and reglages.kdrive_configure()


def cache_dir() -> Path:
    return Path(os.environ.get("CACHE_DIR") or "/cache") / "kdrive"


def _court(nom: str, n: int) -> str:
    """Un nom de fichier sûr, sans dépasser n caractères (l'extension est gardée)."""
    nom = nom_sur(nom)
    if len(nom) <= n:
        return nom
    p = PurePosixPath(nom)
    ext = p.suffix if len(p.suffix) <= 10 else ""
    return p.stem[: n - len(ext)].rstrip(" .") + ext


def _suffixe(nom: str | None) -> str:
    s = PurePosixPath(nom or "").suffix.lower()
    return s if re.fullmatch(r"\.[a-z0-9]{1,8}", s) else ""


# --- Cache local des fichiers kDrive ------------------------------------------------------------

def en_cache(kd, kdrive_id: int, nom: str, verifier: bool = True) -> Path:
    """Le fichier kDrive, téléchargé une fois puis relu sur la VM. `verifier` : demande d'abord à kDrive sa taille
    et sa date (un appel court) pour ne jamais servir une ancienne version ; inutile pour une pièce jointe, qui
    ne change jamais."""
    racine = cache_dir()
    racine.mkdir(parents=True, exist_ok=True)
    kdrive_id = int(kdrive_id)
    version = ""
    if verifier:
        info = kd.stat(kdrive_id)
        version = f"-{int(info.get('size') or 0)}-{int(info.get('last_modified_at') or 0)}"
    chemin = racine / f"{kdrive_id}{version}{_suffixe(nom)}"
    if chemin.exists():
        try:
            os.utime(chemin)   # servi à l'instant : le dernier effacé quand le cache est plein
        except OSError:
            pass
        return chemin
    for ancien in racine.glob(f"{kdrive_id}-*"):   # une ancienne version du même fichier
        ancien.unlink(missing_ok=True)
    tmp = racine / f".{kdrive_id}-{os.getpid()}-{threading.get_ident()}.part"
    try:
        kd.download(kdrive_id, tmp)
        os.replace(tmp, chemin)   # d'un coup : un autre lecteur ne voit jamais un fichier à moitié écrit
    finally:
        tmp.unlink(missing_ok=True)
    elaguer()
    return chemin


def garder(kdrive_id: int, nom: str, contenu: bytes):
    """Une pièce que la VM vient d'envoyer : gardée dans le cache, pas retéléchargée au prochain usage."""
    try:
        racine = cache_dir()
        racine.mkdir(parents=True, exist_ok=True)
        (racine / f"{int(kdrive_id)}{_suffixe(nom)}").write_bytes(contenu)
        elaguer()
    except OSError:
        pass


def elaguer(maximum: int | None = None) -> int:
    """Cache plein : les fichiers les moins récemment servis sont effacés jusqu'à 80 % de la limite
    (ils restent sur kDrive). Renvoie le nombre de fichiers effacés."""
    maximum = CACHE_MAX if maximum is None else maximum
    racine = cache_dir()
    if not racine.is_dir():
        return 0
    with _verrou:
        fichiers = []
        for f in racine.iterdir():
            try:
                st = f.stat()
            except OSError:
                continue
            if f.is_file() and not f.name.endswith(".part"):
                fichiers.append((st.st_mtime, st.st_size, f))
        total = sum(t for _, t, _ in fichiers)
        if total <= maximum:
            return 0
        effaces = 0
        for _, taille, f in sorted(fichiers, key=lambda x: x[0]):
            if total <= maximum * 0.8:
                break
            f.unlink(missing_ok=True)
            total -= taille
            effaces += 1
        return effaces


# --- Pièces jointes -----------------------------------------------------------------------------

def contenu_piece(piece: dict, kd=None) -> bytes | None:
    """Les octets d'une pièce : dans la base (pas encore rangée), sinon relus sur kDrive (via le cache)."""
    if piece.get("contenu") is not None:
        return bytes(piece["contenu"])
    if not piece.get("kdrive_id"):
        return None
    return en_cache(kd or KDrive.distant(), piece["kdrive_id"], piece["nom"], verifier=False).read_bytes()


def avec_contenu(pieces: list[dict], kd=None) -> list[dict]:
    """Pièces lues pour l'IA. kDrive en panne : KDriveIndisponible (la révision attend). Une pièce effacée de
    kDrive à la main est laissée de côté, sans bloquer la révision."""
    resultat = []
    for p in pieces:
        if p.get("contenu") is not None or not p.get("kdrive_id"):
            resultat.append(p)   # encore dans la base : rien à relire
            continue
        try:
            resultat.append({**p, "contenu": contenu_piece(p, kd)})
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
    return resultat


def ranger_pieces(kd, etudes_id: int, racine, limite: int = 20) -> int:
    """Pièces encore gardées dans la base → Etudes/Claunote/Pièces jointes/ ; la base n'en garde que le nom.
    `racine(compte_id)` : le sous-dossier du compte. Renvoie le nombre de pièces rangées."""
    dossiers, n = {}, 0
    for p in db.pieces_a_ranger(limite):
        r = tuple(racine(p["compte_id"]))
        if r not in dossiers:
            dossiers[r] = kd.ensure_path(etudes_id, [*r, DOSSIER, "Pièces jointes"])
        contenu = bytes(p["contenu"])
        # Le numéro devant le nom : deux pièces du même nom ne s'écrasent pas
        info = kd.upload(dossiers[r], _court(f"{p['id']}-{p['nom']}", 200), contenu, conflict="version")
        if db.piece_rangee(p["id"], info["id"]):
            garder(info["id"], p["nom"], contenu)
        n += 1
    if n and not db.pieces_a_ranger(1) and db.place_pieces() > COMPACTER_APRES:
        db.compacter_pieces()   # toutes parties : la place qu'elles prenaient dans la base est rendue au disque
    return n


# --- Notes ---------------------------------------------------------------------------------------

def chemin_note(note: dict) -> tuple[list[str], str]:
    """(dossiers sous Claunote/, nom du fichier) : Notes/BTS2/Réseaux/VLAN (12).md, Notes/Libres/… sans matière."""
    if note.get("matiere"):
        dossiers = ["Notes", _court(note.get("niveau") or "Sans année", 60), _court(note["matiere"], 100)]
    else:
        dossiers = ["Notes", "Libres"]
    titre = notes.titre_affiche(note)
    return dossiers, _court(f"{_court(titre, 120)} ({note['id']}).md", 200)


def copier_notes(kd, etudes_id: int, racine, limite: int = 30) -> int:
    """Notes nouvelles ou modifiées → copie .md sur kDrive. Une note renommée ou changée de matière : l'ancienne
    copie part à la corbeille kDrive. Renvoie le nombre de notes copiées."""
    dossiers, n = {}, 0
    for note in db.notes_a_copier(limite):
        parties, nom = chemin_note(note)
        cle = (*racine(note["compte_id"]), DOSSIER, *parties)
        if cle not in dossiers:
            dossiers[cle] = kd.ensure_path(etudes_id, list(cle))
        info = kd.upload(dossiers[cle], nom, (note.get("contenu") or "").encode(), conflict="version")
        if note.get("kdrive_id") and int(note["kdrive_id"]) != int(info["id"]):
            db.mettre_a_la_corbeille([note["kdrive_id"]])
        db.note_copiee(note["id"], info["id"], note["maj_le"])
        n += 1
    return n


# --- Sauvegarde de la base -----------------------------------------------------------------------

def _paris(maintenant: dt.datetime | None = None) -> dt.datetime:
    return (maintenant or dt.datetime.now(dt.timezone.utc)).astimezone(notifications.PARIS)


def etat_sauvegarde() -> dict | None:
    try:
        return json.loads(db.etat_valeur("sauvegarde_kdrive") or "null")
    except ValueError:
        return None


def sauvegarde_due(maintenant: dt.datetime | None = None, etat: dict | None = None) -> bool:
    """Une fois par jour, à partir de 3 h (Paris) ; ratée : nouvel essai une heure plus tard."""
    ici = _paris(maintenant)
    if ici.hour < HEURE_SAUVEGARDE:
        return False
    etat = etat_sauvegarde() if etat is None else etat
    if not etat:
        return True
    if etat.get("jour") == ici.date().isoformat():
        return False
    try:
        essai = dt.datetime.fromisoformat(etat.get("essai") or "")
    except ValueError:
        return True
    return ici - essai >= REESSAI_SAUVEGARDE


def _env_pg(url: str) -> dict:
    """Connexion donnée à pg_dump par l'environnement : le mot de passe n'apparaît jamais dans la ligne de commande."""
    u = urlparse(url)
    env = {**os.environ, "PGHOST": u.hostname or "localhost", "PGPORT": str(u.port or 5432),
           "PGUSER": unquote(u.username or ""), "PGDATABASE": unquote(u.path.lstrip("/"))}
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)
    return env


def sauvegarder_base(kd, etudes_id: int, maintenant: dt.datetime | None = None, executer=subprocess.run) -> dict:
    """pg_dump de toute la base → Etudes/Claunote/Sauvegardes/claunote-AAAA-MM-JJ.dump, puis la copie locale est
    effacée ; seules les SAUVEGARDES_GARDEES dernières restent sur kDrive. La clé des secrets (volume secrets)
    n'y est jamais : sans elle, les jetons de la sauvegarde restent illisibles."""
    ici = _paris(maintenant)
    jour = ici.date().isoformat()
    fichier = cache_dir().parent / f"sauvegarde-{jour}.dump"
    etat = {"essai": ici.isoformat(timespec="seconds")}
    try:
        fichier.parent.mkdir(parents=True, exist_ok=True)
        executer(["pg_dump", "--format=custom", "--no-owner", "--file", str(fichier)],
                 env=_env_pg(os.environ["DATABASE_URL"]), check=True, capture_output=True, timeout=1800)
        taille = fichier.stat().st_size
        dossier = kd.ensure_path(etudes_id, [DOSSIER, "Sauvegardes"])
        with open(fichier, "rb") as f:
            kd.upload_flux(dossier, f"claunote-{jour}.dump", f, taille, conflict="version")
        anciennes = sorted((i for i in kd.list_dir(dossier)
                            if i.get("type") == "file" and NOM_SAUVEGARDE.fullmatch(i.get("name") or "")),
                           key=lambda i: i["name"], reverse=True)[SAUVEGARDES_GARDEES:]
        for i in anciennes:
            kd.delete(i["id"])
        etat.update(jour=jour, le=etat["essai"], taille=taille, erreur=None)
    except KDriveIndisponible:
        etat.update(_garder_reussite(), erreur="kDrive indisponible")
        db.etat_ecrire("sauvegarde_kdrive", json.dumps(etat))
        raise
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode(errors="replace").strip().splitlines()
        etat.update(_garder_reussite(), erreur=f"pg_dump : {detail[-1] if detail else e.returncode}"[:200])
    except (FileNotFoundError, subprocess.TimeoutExpired, httpx.HTTPError, OSError, KeyError) as e:
        etat.update(_garder_reussite(), erreur=f"{e.__class__.__name__} : {e}"[:200])
    finally:
        fichier.unlink(missing_ok=True)   # la VM ne garde jamais la sauvegarde
    db.etat_ecrire("sauvegarde_kdrive", json.dumps(etat))
    return etat


def _garder_reussite() -> dict:
    """La dernière sauvegarde réussie reste affichée quand un nouvel essai rate."""
    avant = etat_sauvegarde() or {}
    return {k: avant[k] for k in ("le", "taille") if k in avant}


# --- Un tour du worker ---------------------------------------------------------------------------

def entretien(kd, etudes_id: int, racine, log=print, arret=None) -> None:
    """Appelé à chaque tour du worker (kDrive joignable). Rien si un autre stockage est choisi."""
    if not actif() or not isinstance(kd, KDrive):
        return
    n = ranger_pieces(kd, etudes_id, racine)
    if n:
        log(f"kDrive seul : {n} pièce(s) jointe(s) rangée(s) sur kDrive")
    if arret is not None and arret.is_set():
        return
    n = copier_notes(kd, etudes_id, racine)
    if n:
        log(f"kDrive seul : {n} note(s) copiée(s) sur kDrive")
    if (arret is None or not arret.is_set()) and sauvegarde_due():
        etat = sauvegarder_base(kd, etudes_id)
        log("sauvegarde de la base sur kDrive : " + (f"{etat['taille'] / 1e6:.1f} Mo" if not etat.get("erreur")
                                                       else f"ratée ({etat['erreur']}), nouvel essai dans 1 h"))
