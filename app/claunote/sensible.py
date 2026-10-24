"""Garde-fou : documents personnels oubliés dans un import (RIB, pièce d'identité, impôts, santé…).

Tout se passe sur la VM, AVANT l'IA : nom du fichier, dossier, texte du PDF ou OCR local.
Un document repéré n'est jamais envoyé à Claude.

- « sûr » : une preuve qui ne trompe pas (IBAN dont la clé est juste, bande MRZ d'une carte
  d'identité ou d'un passeport, numéro de sécurité sociale avec sa clé, numéro de carte bancaire
  valide près du mot « carte »), un nom de fichier sans ambiguïté (« RIB.pdf », « passeport.jpg »)
  ou plusieurs expressions typiques dans un document court → corbeille kDrive tout de suite
  (récupérable 30 jours), noté dans le journal et notifié.
- « doute » : une seule expression (« avis d'imposition » en haut de page…), un dossier « Banque » :
  le fichier n'est ni analysé ni supprimé, tu choisis sur la page Import.

Les cours en parlent aussi (CEJM : bulletin de paie, RIB…) : une expression seule dans un long
document ne compte que si elle est dans l'en-tête, et sans preuve chiffrée ce n'est qu'un doute.
"""
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from claunote import ocr

LIBELLES = {"banque": "coordonnées bancaires", "identite": "pièce d'identité", "sante": "santé / sécurité sociale",
            "impots": "impôts", "paie": "fiche de paie", "secret": "mots de passe ou codes"}

PAGES_DOCUMENT = 5   # un document ordinaire : 4 premières pages et la dernière (~3 s d'OCR pour un scan)


@dataclass
class Verdict:
    niveau: str | None = None                  # "sur", "doute" ou None
    categorie: str | None = None
    raisons: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)   # pages concernées (1 = première), si connues

    def __bool__(self):
        return self.niveau is not None

    @property
    def libelle(self) -> str:
        return LIBELLES.get(self.categorie or "", "document personnel")

    def ajouter(self, niveau: str, categorie: str, raison: str):
        if self.niveau != "sur":
            if niveau == "sur" or self.niveau is None:
                self.categorie = categorie
            self.niveau = "sur" if niveau == "sur" else "doute"
        if raison not in self.raisons:
            self.raisons.append(raison)

    def fusionner(self, autre: "Verdict", page: int | None = None):
        for r in autre.raisons:
            self.ajouter(autre.niveau, autre.categorie, r)
        if autre and page is not None:
            self.pages.append(page)


def _simple(texte: str) -> str:
    t = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode().lower()
    return " " + re.sub(r"[^a-z0-9]+", " ", t).strip() + " "


# --- Nom du fichier et dossiers ----------------------------------------------------------

_NOMS_SURS = [
    (r"rib|iban|releve d identite bancaire|releve de compte|releves? bancaires?", "banque"),
    (r"cni|carte (nationale )?d identite|carte identite|passeport|passport|titre de sejour|permis de conduire",
     "identite"),
    (r"carte vitale|attestation (de )?(droits|securite sociale|secu)", "sante"),
    (r"avis d (imposition|impot)|declaration (de |d )?(revenus|impots?)", "impots"),
    (r"fiches? de paie|bulletins? de (paie|salaire)", "paie"),
    (r"mots? de passe|passwords?|identifiants? (perso|personnels?)|codes? (pin|puk|secrets?)", "secret"),
]
_NOMS_DOUTE = [
    (r"impots?|fiscal", "impots"),
    (r"ordonnance|certificat medical|mutuelle|ameli|securite sociale|secu", "sante"),
    (r"mdp", "secret"),
    (r"banque|bancaire", "banque"),
]


def examiner_nom(nom: str, dossiers: list[str] | None = None) -> Verdict:
    v = Verdict()
    stem = _simple(PurePosixPath(nom).stem)
    for motif, cat in _NOMS_SURS:
        if re.search(rf" (?:{motif}) ", stem):
            v.ajouter("sur", cat, f"nom du fichier « {nom} »")
            return v
    for motif, cat in _NOMS_DOUTE:
        if re.search(rf" (?:{motif}) ", stem):
            v.ajouter("doute", cat, f"nom du fichier « {nom} »")
    for d in dossiers or []:
        sd = _simple(d)
        for motif, cat in _NOMS_SURS + _NOMS_DOUTE[:2] + [(r"banque|bancaire", "banque")]:
            if re.search(rf" (?:{motif}) ", sd):
                v.ajouter("doute", cat, f"rangé dans le dossier « {d} »")
                break
    return v


# --- Preuves chiffrées -------------------------------------------------------------------

_IBAN = re.compile(r"\b([A-Z]{2}[0-9O]{2}(?:\s?[A-Z0-9]{2,4}){3,8})\b")
_LONGUEURS_IBAN = {"FR": 27, "MC": 27, "BE": 16, "DE": 22, "ES": 24, "IT": 27, "LU": 20, "CH": 21, "GB": 22,
                   "NL": 18, "PT": 25}


def iban_valide(candidat: str) -> bool:
    s = re.sub(r"\s", "", candidat.upper())
    s = s[:2] + s[2:4].replace("O", "0") + s[4:]
    if len(s) < 15 or len(s) > 34 or (s[:2] in _LONGUEURS_IBAN and len(s) != _LONGUEURS_IBAN[s[:2]]):
        return False
    chiffres = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(chiffres) % 97 == 1


def trouver_iban(texte: str) -> str | None:
    for m in _IBAN.finditer(texte.upper()):
        bloc = m.group(1)
        # le motif peut avaler un mot qui suit : on retire des groupes à la fin jusqu'à un IBAN valide
        morceaux = bloc.split()
        for fin in range(len(morceaux), 0, -1):
            essai = " ".join(morceaux[:fin])
            if iban_valide(essai):
                return essai
        if iban_valide(bloc):
            return bloc
    return None


_NIR = re.compile(r"\b([12])\s?(\d{2})\s?(0[1-9]|1[0-2]|[2-9]\d)\s?(\d{2}|2[AB])\s?(\d{3})\s?(\d{3})\s?(\d{2})\b")


def trouver_nir(texte: str) -> str | None:
    """Numéro de sécurité sociale dont la clé (97 - n mod 97) est juste."""
    for m in _NIR.finditer(texte.upper()):
        sexe, an, mois, dep, commune, ordre, cle = m.groups()
        dep_n = {"2A": "19", "2B": "18"}.get(dep, dep)
        n = int(sexe + an + mois + dep_n + commune + ordre)
        if 97 - n % 97 == int(cle):
            return m.group(0)
    return None


_CARTE = re.compile(r"\b(?:\d[ -]?){15}\d\b")


def luhn(chiffres: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(chiffres)):
        d = int(c) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def trouver_carte(texte: str) -> str | None:
    simple = _simple(texte)
    if not re.search(r" (carte|cb|visa|mastercard|cvv|cvc|cryptogramme|expire|expiration) ", simple):
        return None
    for m in _CARTE.finditer(texte):
        chiffres = re.sub(r"\D", "", m.group(0))
        if len(set(chiffres)) > 3 and luhn(chiffres):
            return m.group(0)
    return None


_MRZ = re.compile(r"^(?:P<|ID|IR|I<|AC)[A-Z0-9<]{3}[A-Z0-9<]{20,}$|^[A-Z0-9<]{25,}<<[A-Z0-9<]*$", re.M)


def trouver_mrz(texte: str) -> bool:
    lignes = re.sub(r"[ \t«»]", "", texte.upper().replace("K<", "<<"))
    return sum(1 for _ in _MRZ.finditer(lignes)) >= 1 and lignes.count("<<") >= 3


# --- Expressions typiques --------------------------------------------------------------

_EXPRESSIONS = [
    (r"releve d identite bancaire|titulaire du compte|domiciliation bancaire|code (bic|banque|guichet)|"
     r"cle rib|iban", "banque"),
    (r"releve de compte|solde (crediteur|debiteur|au)|ancien solde|nouveau solde", "banque"),
    (r"carte nationale d identite|passeport|titre de sejour|permis de conduire|date de delivrance|"
     r"lieu de naissance", "identite"),
    (r"carte vitale|numero (d )?(assure|securite sociale)|attestation de droits|ameli|"
     r"caisse primaire d assurance maladie|ordonnance|certificat medical|mutuelle|tiers payant", "sante"),
    (r"avis d (imposition|impot)|revenu fiscal de reference|numero fiscal|impots gouv|"
     r"declaration (des |de )?revenus|taxe d habitation|prelevement a la source", "impots"),
    (r"bulletin de (paie|salaire)|net a payer|cotisations salariales|net imposable", "paie"),
    (r"code (pin|puk|confidentiel)|mes mots de passe|liste des mots de passe", "secret"),
]
COURT = 350        # mots : en dessous, c'est un formulaire ou un papier, pas un cours
EN_TETE = 600      # caractères lus comme en-tête d'un long document


def examiner_texte(texte: str) -> Verdict:
    v = Verdict()
    if not texte.strip():
        return v
    iban = trouver_iban(texte)
    if iban:
        v.ajouter("sur", "banque", f"IBAN valide ({iban[:4]} … {iban.replace(' ', '')[-3:]})")
    if trouver_mrz(texte):
        v.ajouter("sur", "identite", "bande lisible par machine (MRZ) d'une pièce d'identité")
    if trouver_nir(texte):
        v.ajouter("sur", "sante", "numéro de sécurité sociale avec sa clé")
    if trouver_carte(texte):
        v.ajouter("sur", "banque", "numéro de carte bancaire")
    simple = _simple(texte)
    court = len(simple.split()) < COURT
    zone = simple if court else _simple(texte[:EN_TETE])
    trouvees = {}
    for motif, cat in _EXPRESSIONS:
        for m in re.finditer(rf"(?<= )(?:{motif})(?= )", zone):
            trouvees.setdefault(m.group(0).strip(), cat)
    if trouvees:
        cats = list(trouvees.values())
        cat = max(set(cats), key=cats.count)
        liste = ", ".join(f"« {e} »" for e in list(trouvees)[:3])
        # Trois expressions différentes d'une même famille dans un papier court : c'est un vrai document
        niveau = "sur" if court and cats.count(cat) >= 3 else "doute"
        v.ajouter(niveau, cat, f"{'texte' if court else 'en-tête'} : {liste}")
    return v


# --- Fichier entier ----------------------------------------------------------------------

def textes_fichier(chemin: Path) -> dict[int, str]:
    """Texte lu localement, par page (une seule « page » pour une image ou un fichier bureautique)."""
    ext = chemin.suffix.lower()
    if ext == ".pdf":
        total = ocr.nb_pages(chemin)
        pages = list(range(1, min(total, PAGES_DOCUMENT - 1) + 1))
        if total >= PAGES_DOCUMENT:
            pages.append(total)
        return {n: p["texte"] for n, p in ocr.lire_pdf(chemin, pages).items()}
    if ext in ocr.IMAGES:
        return {1: ocr.lire_image(chemin)}
    if ext in {".txt", ".md"}:
        return {1: chemin.read_text(errors="replace")[:200_000]}
    from claunote import agent  # texte des fichiers bureautiques, extrait sans IA
    try:
        if ext in agent.OFFICE:
            return {1: agent.texte_office(chemin)}
        if ext in agent.VIEUX_OFFICE:
            return {1: agent.texte_vieux_office(chemin)}
    except Exception:
        pass
    return {}


def examiner(chemin: Path, nom: str | None = None, dossiers: list[str] | None = None) -> Verdict:
    """Nom, dossier puis contenu (texte ou OCR) : le verdict le plus grave l'emporte."""
    v = examiner_nom(nom or chemin.name, dossiers)
    if v.niveau == "sur":
        return v
    try:
        textes = textes_fichier(chemin)
    except Exception:
        textes = {}
    for n, texte in textes.items():
        v.fusionner(examiner_texte(texte), n if len(textes) > 1 else None)
        if v.niveau == "sur":
            break
    return v
