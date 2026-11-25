"""Scanner avec le scanner d'Apple (iPhone) : un raccourci iOS envoie le PDF à Claunote.

Une appli web ne peut pas ouvrir le scanner de documents d'Apple (VisionKit) ; Notes et Fichiers, si. Le scan
part ensuite par la feuille de partage vers le raccourci « Envoyer à Claunote », qui le dépose dans l'inbox :

    Notes / Fichiers ─ « Scanner des documents » (Apple) ─► PDF
        └─ Partager ─► raccourci « Envoyer à Claunote »
               └─ POST /raccourci/envoi  (Authorization: Bearer <jeton du compte>)
                      └─ inbox, comme « Déposer » : analysé puis rangé pour CE compte

Le raccourci n'a ni session ni cookie d'appareil : son jeton en tient lieu. Un jeton par compte, créé sur la page
/raccourci (montré une seule fois, la base n'en garde que l'empreinte), il ne sert qu'à déposer. « Retirer » le coupe,
un nouveau remplace l'ancien, un compte désactivé ou sans le droit « Déposer » ne passe plus. Les jetons refusés
comptent comme des échecs de connexion (blocage, ban 24 h par Internet, alertes : securite.py).
"""
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets

from claunote import db

CLE = "raccourci_scan"          # etat « raccourci_scan:<compte> » = {"empreinte", "cree_le", "utilise_le", "nb"}
NOM = "Envoyer à Claunote"      # nom du raccourci à créer dans l'app Raccourcis
FORME = re.compile(r"^(\d{1,9})\.([A-Za-z0-9_-]{30,100})$")


def _cle(compte_id: int) -> str:
    return f"{CLE}:{int(compte_id)}"


def _empreinte(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def etat(compte_id: int) -> dict | None:
    """Jeton en place pour ce compte (sans le secret), ou None."""
    brut = db.etat_valeur(_cle(compte_id))
    try:
        return json.loads(brut) if brut else None
    except ValueError:
        return None


def creer(compte_id: int) -> str:
    """Nouveau jeton (remplace l'ancien) : « <compte>.<secret> », à coller une fois dans le raccourci."""
    secret = secrets.token_urlsafe(32)
    db.etat_ecrire(_cle(compte_id), json.dumps({
        "empreinte": _empreinte(secret), "cree_le": dt.datetime.now(dt.timezone.utc).isoformat(),
        "utilise_le": None, "nb": 0}))
    return f"{int(compte_id)}.{secret}"


def retirer(compte_id: int):
    db.etat_ecrire(_cle(compte_id), None)


def lire_entete(autorisation: str | None) -> str:
    """« Bearer <jeton> » (ou le jeton seul, si le raccourci a été rempli sans « Bearer »)."""
    valeur = (autorisation or "").strip()
    if valeur.lower().startswith("bearer "):
        valeur = valeur[7:].strip()
    return valeur


def verifier(jeton: str) -> int | None:
    """Le compte de ce jeton, ou None (forme fausse, jeton retiré ou remplacé)."""
    m = FORME.match(jeton or "")
    if not m:
        return None
    compte_id, secret = int(m.group(1)), m.group(2)
    vu = etat(compte_id)
    if not vu or not hmac.compare_digest(vu.get("empreinte", ""), _empreinte(secret)):
        return None
    return compte_id


def noter_usage(compte_id: int, nb: int):
    vu = etat(compte_id)
    if not vu:
        return
    vu.update(utilise_le=dt.datetime.now(dt.timezone.utc).isoformat(), nb=int(vu.get("nb") or 0) + nb)
    db.etat_ecrire(_cle(compte_id), json.dumps(vu))


EXTENSIONS_TYPE = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic",
                   "image/heif": ".heic", "image/webp": ".webp", "image/tiff": ".tiff"}


def nom_du_scan(nom: str | None, type_contenu: str | None, maintenant: dt.datetime | None = None) -> str:
    """Nom du fichier reçu : celui du raccourci s'il en donne un, sinon « scan-AAAA-MM-JJ-HH-MM-SS.pdf »."""
    nom = re.sub(r"[\\/\x00-\x1f]", " ", (nom or "")).strip(" .")[:120]
    type_ = (type_contenu or "").split(";")[0].strip().lower()
    if nom and "." in nom:
        return nom
    ext = EXTENSIONS_TYPE.get(type_, ".pdf")
    if nom:
        return nom + ext
    quand = (maintenant or dt.datetime.now()).strftime("%Y-%m-%d-%H-%M-%S")
    return f"scan-{quand}{ext}"
