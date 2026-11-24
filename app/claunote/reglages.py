"""Réglages de l'installation choisis sur le site (assistant d'installation, puis Réglages → Configuration du site).

    page web (admin) ──► reglages.ecrire() ──► table reglages (secrets chiffrés, jamais réaffichés)
    démarrage du site et du worker ──► charger_dans_env() : la base passe avant le .env

Les secrets (jeton Claude, jeton kDrive, jeton du tunnel Cloudflare, clé secrète Turnstile,
jeton de supervision Cloudflare) sont chiffrés (Fernet : AES + HMAC) avec
une clé gardée HORS de la base, dans le volume Docker `secrets` (fichier /secrets/claunote.key) : une copie de
la base seule ne suffit pas à les lire. Un secret enregistré n'est jamais renvoyé au navigateur.

Ce qui est déjà dans le .env continue de marcher : un réglage absent de la base garde sa valeur du .env.
"""
import datetime as dt
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

CLE = Path(os.environ.get("CLAUNOTE_CLE") or "/secrets/claunote.key")
JETON_TUNNEL = Path(os.environ.get("CLAUNOTE_JETON_TUNNEL") or "/tunnel/token")   # lu par cloudflared


@dataclass(frozen=True)
class Reglage:
    cle: str
    libelle: str
    groupe: str
    secret: bool = False
    aide: str = ""


CATALOGUE = [
    Reglage("STOCKAGE", "Où sont les fichiers", "stockage", aide="kdrive, local ou les_deux"),
    Reglage("KDRIVE_TOKEN", "Jeton API Infomaniak (scope « drive »)", "stockage", secret=True,
            aide="manager.infomaniak.com → Mon profil → Jetons API → Créer un jeton, scope « drive »"),
    Reglage("KDRIVE_ID", "Numéro du kDrive", "stockage", aide="le nombre après /drive/ dans l'adresse de kDrive"),
    Reglage("KDRIVE_ETUDES_ID", "Dossier des cours rangés", "stockage"),
    Reglage("KDRIVE_INBOX_ID", "Dossier inbox (scans à analyser)", "stockage"),
    Reglage("CLAUDE_CODE_OAUTH_TOKEN", "Jeton du forfait Claude", "claude", secret=True,
            aide="sur un ordinateur avec Claude Code : claude setup-token (valable 1 an)"),
    Reglage("CLOUDFLARE_TUNNEL_TOKEN", "Jeton du tunnel Cloudflare", "cloudflare", secret=True,
            aide="Zero Trust → Réseaux → Tunnels → ton tunnel → jeton"),
    Reglage("TURNSTILE_SITE_KEY", "Captcha Turnstile : clé du site", "cloudflare",
            aide="dash.cloudflare.com → Turnstile → Ajouter un widget → « Clé du site » (vide = pas de captcha)"),
    Reglage("TURNSTILE_SECRET_KEY", "Captcha Turnstile : clé secrète", "cloudflare", secret=True,
            aide="même widget → « Clé secrète »"),
    Reglage("CLOUDFLARE_API_TOKEN", "Supervision : jeton API Cloudflare (lecture seule)", "cloudflare", secret=True,
            aide="dash.cloudflare.com → Mon profil → Jetons API → Créer un jeton personnalisé : "
                 "Zone · Analytics · Lecture (+ Compte · Account Analytics · Lecture pour Turnstile)"),
    Reglage("CLOUDFLARE_ZONE_ID", "Supervision : identifiant de zone", "cloudflare",
            aide="ton domaine → Vue d'ensemble → colonne de droite « API » → ID de zone (32 caractères)"),
    Reglage("CLOUDFLARE_ACCOUNT_ID", "Supervision : identifiant de compte (facultatif, pour Turnstile)", "cloudflare",
            aide="même endroit → ID de compte ; vide = pas de chiffres Turnstile"),
    Reglage("CLAUNOTE_PAYS", "Pays acceptés par Internet", "cloudflare", aide="codes à 2 lettres séparés par des virgules (FR,BE), ou * pour tous"),
    Reglage("CLAUNOTE_CONTACT", "Contact affiché dans les mentions légales", "compte"),
]
PAR_CLE = {r.cle: r for r in CATALOGUE}
STOCKAGES = {"kdrive": "kDrive (Infomaniak)", "local": "Sur la VM", "les_deux": "Les deux (VM + copie kDrive)"}
# L'adresse du site (CLAUNOTE_HOTE) reste dans le .env : Caddy la lit au démarrage pour son certificat HTTPS.


# --- Chiffrement ----------------------------------------------------------------------

class CleIndisponible(RuntimeError):
    pass


def _fernet() -> Fernet:
    try:
        if not CLE.exists():
            CLE.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(CLE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        return Fernet(CLE.read_bytes().strip())
    except FileExistsError:   # créée au même moment par l'autre service
        return Fernet(CLE.read_bytes().strip())
    except (OSError, ValueError) as e:
        raise CleIndisponible(f"clé de chiffrement illisible ({CLE}) : volume « secrets » monté ? ({e})") from e


def chiffrer(texte: str) -> str:
    return _fernet().encrypt(texte.encode()).decode()


def dechiffrer(texte: str) -> str | None:
    try:
        return _fernet().decrypt(texte.encode()).decode()
    except (InvalidToken, CleIndisponible):
        return None


# --- Lecture, écriture ----------------------------------------------------------------

def _db():
    from claunote import db   # import tardif : claunote/__init__ charge les réglages avant tout le reste
    return db


def ecrire(cle: str, valeur: str | None, compte_id: int | None = None):
    """None ou vide : le réglage est retiré de la base (le .env reprend la main)."""
    r = PAR_CLE[cle]
    valeur = (valeur or "").strip()
    if not valeur:
        _db().reglage_effacer(cle)
    else:
        _db().reglage_ecrire(cle, chiffrer(valeur) if r.secret else valeur, r.secret, compte_id)
    os.environ.pop(cle, None) if not valeur else os.environ.__setitem__(cle, valeur)
    if cle == "CLOUDFLARE_TUNNEL_TOKEN":
        ecrire_jeton_tunnel(valeur)


def valeurs() -> dict[str, str]:
    """Réglages de la base, déchiffrés (pour le serveur seulement, jamais pour une page)."""
    sortie = {}
    for ligne in _db().reglages():
        v = dechiffrer(ligne["valeur"]) if ligne["chiffre"] else ligne["valeur"]
        if v is not None:
            sortie[ligne["cle"]] = v
    return sortie


def etat() -> dict[str, dict]:
    """Pour les pages : d'où vient chaque réglage et quand il a changé, sans jamais la valeur d'un secret."""
    en_base = {ligne["cle"]: ligne for ligne in _db().reglages()}
    sortie = {}
    for r in CATALOGUE:
        ligne = en_base.get(r.cle)
        present = bool(ligne) or bool(os.environ.get(r.cle))
        sortie[r.cle] = {
            "reglage": r, "present": present, "source": "site" if ligne else ("env" if present else None),
            "maj_le": ligne["maj_le"] if ligne else None,
            "valeur": None if r.secret else ((ligne or {}).get("valeur") or os.environ.get(r.cle) or ""),
            "illisible": bool(ligne and ligne["chiffre"] and dechiffrer(ligne["valeur"]) is None)}
    return sortie


def charger_dans_env() -> int:
    """Au démarrage : les réglages du site remplacent ceux du .env. Renvoie combien ont été chargés."""
    v = valeurs()
    os.environ.update(v)
    return len(v)


def stockage() -> str:
    s = (os.environ.get("STOCKAGE") or "").strip()
    return s if s in STOCKAGES else "kdrive"


def kdrive_configure() -> bool:
    return all(os.environ.get(k) for k in ("KDRIVE_TOKEN", "KDRIVE_ID", "KDRIVE_ETUDES_ID", "KDRIVE_INBOX_ID"))


def copie_kdrive_configuree() -> bool:
    """« Les deux » : la copie de sauvegarde n'a besoin que du jeton, du kDrive et d'un dossier."""
    return all(os.environ.get(k) for k in ("KDRIVE_TOKEN", "KDRIVE_ID", "KDRIVE_ETUDES_ID"))


def stockage_pret() -> bool:
    """Le worker peut-il travailler ? Sur la VM : toujours ; avec kDrive : une fois kDrive réglé."""
    s = stockage()
    return True if s == "local" else copie_kdrive_configuree() if s == "les_deux" else kdrive_configure()


def dossiers() -> tuple[int, int]:
    """(cours rangés, inbox) du stockage choisi : numéros kDrive, ou ceux des dossiers de la VM."""
    if stockage() in ("local", "les_deux"):
        from claunote.stockage_local import Local
        return Local.from_env().racines()
    return int(os.environ.get("KDRIVE_ETUDES_ID") or 0), int(os.environ.get("KDRIVE_INBOX_ID") or 0)


# --- Cloudflare : le jeton dans un fichier lu par cloudflared (--token-file) --------------

def ecrire_jeton_tunnel(jeton: str):
    try:
        JETON_TUNNEL.parent.mkdir(parents=True, exist_ok=True)
        if jeton:
            tmp = JETON_TUNNEL.with_suffix(".tmp")
            tmp.write_text(jeton)
            os.chmod(tmp, 0o644)   # cloudflared tourne sans être root ; le volume n'est monté que chez lui et ici
            tmp.replace(JETON_TUNNEL)
        else:
            JETON_TUNNEL.unlink(missing_ok=True)
    except OSError as e:
        raise CleIndisponible(f"jeton du tunnel non écrit ({JETON_TUNNEL}) : volume « tunnel » monté ? ({e})") from e


# --- Tests de connexion (boutons « Tester ») -------------------------------------------

def tester_kdrive(jeton: str, drive_id: str, *ids: int) -> tuple[bool, str, list[dict]]:
    """(ok, message, dossiers à la racine du kDrive) ; vérifie aussi que les dossiers donnés existent."""
    from claunote.kdrive import KDrive
    try:
        kd = KDrive(jeton, drive_id)
        racine = [i for i in kd.list_dir(1) if i.get("type") == "dir"]
        noms = [kd.stat(i).get("name", "?") for i in ids if i]
    except Exception as e:   # jeton refusé, numéro faux, réseau
        return False, f"kDrive refuse : {str(e)[:200]}", []
    dossiers = f" Dossiers : {', '.join(noms)}." if noms else ""
    return True, f"Connexion à kDrive réussie.{dossiers}", racine


def creer_dossiers_kdrive(jeton: str, drive_id: str) -> tuple[int, int]:
    """Crée Claunote/Etudes et Claunote/Inbox à la racine du kDrive (ou les retrouve) : (études, inbox)."""
    from claunote.kdrive import KDrive
    kd = KDrive(jeton, drive_id)
    return kd.ensure_path(1, ["Claunote", "Etudes"]), kd.ensure_path(1, ["Claunote", "Inbox"])


def tester_claude(jeton: str) -> tuple[bool, str]:
    """Le plus petit appel possible (Haiku, une ligne) avec ce jeton. Aucun crédit acheté, jamais de clé API."""
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = jeton
    try:
        p = subprocess.run(["claude", "-p", "Réponds seulement : OK", "--model", "haiku", "--output-format", "text"],
                           cwd=tempfile.gettempdir(), env=env, capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        return False, "Claude Code n'est pas installé dans le conteneur (image à reconstruire)."
    except subprocess.TimeoutExpired:
        return False, "Claude ne répond pas (90 s)."
    if p.returncode == 0 and p.stdout.strip():
        return True, "Claude répond : le jeton marche."
    return False, f"Claude refuse : {(p.stdout + p.stderr).strip()[:200] or f'code {p.returncode}'}"


def maintenant() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
