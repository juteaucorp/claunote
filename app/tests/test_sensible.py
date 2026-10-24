import pytest

from claunote import ocr, sensible

RIB = """RELEVÉ D'IDENTITÉ BANCAIRE
Titulaire du compte : M. Antonin Dupont
Domiciliation : Agence Angers
IBAN : FR76 3000 6000 0112 3456 7890 189   BIC : AGRIFRPP"""


def test_iban():
    assert sensible.iban_valide("FR76 3000 6000 0112 3456 7890 189")
    assert not sensible.iban_valide("FR76 3000 6000 0112 3456 7890 188")      # clé fausse
    assert sensible.trouver_iban("iban fr76 3000 6000 0112 3456 7890 189 bic agrifrpp")
    assert sensible.trouver_iban("FR7O 3000 6000 0112 3456 7890 189") is None  # O lu à la place du 0, clé fausse
    assert sensible.trouver_iban("le TP 12 de routage, VLAN 10, 20 et 30") is None


def test_numeros_securite_sociale_et_carte():
    assert sensible.trouver_nir("N° de sécurité sociale : 1 85 05 78 006 084 91")
    assert sensible.trouver_nir("1 85 05 78 006 084 36") is None
    assert sensible.trouver_carte("Carte Visa 4970 1012 3456 7893 expire 12/27")
    assert sensible.trouver_carte("4970 1012 3456 7893") is None   # sans le mot carte : un nombre quelconque


def test_mrz():
    texte = "CARTE NATIONALE D'IDENTITE\nIDFRADUPONT<<<<<<<<<<<<<<<<<<<<<<<<<<<<<\n8806923102858JEAN<<<<<<<<<7608121M8"
    assert sensible.trouver_mrz(texte)
    assert not sensible.trouver_mrz("x <- y ; while (a << 2)")


def test_verdict_sur_doute_rien():
    v = sensible.examiner_texte(RIB)
    assert v.niveau == "sur" and v.categorie == "banque" and "IBAN valide" in v.raisons[0]
    assert "189" in v.raisons[0] and "0112" not in v.raisons[0]  # l'IBAN n'est jamais recopié en entier
    assert sensible.examiner_texte("AVIS D'IMPOSITION 2024\nRevenu fiscal de référence\nNuméro fiscal").niveau == "sur"
    assert sensible.examiner_texte("Exercice : analysez ce bulletin de paie.").niveau == "doute"
    cours = "Chapitre 4. Le contrat de travail. " + "Le salarié est subordonné à l'employeur. " * 80
    assert not sensible.examiner_texte(cours + " Le bulletin de paie indique le net à payer.")
    assert not sensible.examiner_texte("TP Linux : créer l'utilisateur, mot de passe P@ssw0rd, login admin")
    assert not sensible.examiner_texte("")


@pytest.mark.parametrize("nom,dossiers,niveau,categorie", [
    ("RIB.pdf", [], "sur", "banque"), ("Scan CNI recto.jpg", [], "sur", "identite"),
    ("passeport anto.pdf", [], "sur", "identite"), ("Avis d'impôt 2024.pdf", [], "sur", "impots"),
    ("fiche de paie mars.pdf", [], "sur", "paie"), ("mots de passe.txt", [], "sur", "secret"),
    ("ordonnance.jpg", [], "doute", "sante"), ("releve.pdf", ["Banque"], "doute", "banque"),
    ("TP routage.pdf", ["BTS1", "Réseaux"], None, None), ("Cours CEJM.pdf", [], None, None),
    ("TP carte réseau.pdf", [], None, None), ("scrib.pdf", [], None, None)])
def test_nom_du_fichier(nom, dossiers, niveau, categorie):
    v = sensible.examiner_nom(nom, dossiers)
    assert (v.niveau, v.categorie) == (niveau, categorie)


def test_examiner_fichier_texte(tmp_path):
    (tmp_path / "notes.txt").write_text(RIB)
    v = sensible.examiner(tmp_path / "notes.txt")
    assert v.niveau == "sur" and v.libelle == "coordonnées bancaires"
    (tmp_path / "cours.txt").write_text("Les suites géométriques : u(n+1) = q × u(n).")
    assert not sensible.examiner(tmp_path / "cours.txt")


def _image_texte(chemin, texte):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (1240, 1754), "white")
    dessin = ImageDraw.Draw(image)
    try:
        police = ImageFont.truetype("DejaVuSans.ttf", 34)
    except OSError:
        police = ImageFont.load_default(size=34)
    for i, ligne in enumerate(texte.splitlines()):
        dessin.text((90, 120 + i * 60), ligne, fill="black", font=police)
    image.save(chemin)


@pytest.mark.skipif(not ocr.disponible(), reason="Tesseract absent")
def test_ocr_d_une_photo_de_rib(tmp_path):
    _image_texte(tmp_path / "IMG_2041.jpg", RIB)
    v = sensible.examiner(tmp_path / "IMG_2041.jpg")
    assert v.niveau == "sur" and v.categorie == "banque"
