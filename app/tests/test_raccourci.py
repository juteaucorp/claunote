"""Scanner avec Apple : le raccourci iOS dépose dans l'inbox avec le jeton de son compte, rien d'autre."""
import re

import pytest

from claunote import main, raccourci
from tests.test_site import anonyme, client  # noqa: F401 (fixtures)

CF = {"cf-connecting-ip": "203.0.113.7", "cf-ray": "abc-CDG", "cf-ipcountry": "FR"}


@pytest.fixture
def kd(monkeypatch):
    faits = []

    class FauxKD:
        def ensure_path(self, parent, parts):
            return 900 + len(parts)

        def upload(self, dir_id, nom, contenu, conflict="version"):
            faits.append((dir_id, nom, contenu))
            return {"id": 4242}

    monkeypatch.setattr(main.KDrive, "from_env", classmethod(lambda cls: FauxKD()))
    monkeypatch.setattr(main.db, "noter_depot", lambda kid, compte_id: faits.append(("depot", kid, compte_id)))
    return faits


def _jeton(client):  # noqa: F811
    r = client.post("/raccourci/jeton")
    assert r.status_code == 200 and "montré une seule fois" in r.text
    return re.search(r"Bearer (\d+\.[A-Za-z0-9_-]+)", r.text).group(1)


def test_page_et_jeton_montre_une_fois(client):  # noqa: F811
    page = client.get("/raccourci").text
    assert "Créer mon jeton" in page and "/raccourci/envoi" in page and "Envoyer à Claunote" in page
    jeton = _jeton(client)
    assert jeton.startswith("1.")
    page = client.get("/raccourci").text
    assert jeton not in page and "Ouvrir Notes" in page and "Nouveau jeton" in page
    assert jeton.split(".")[1] not in str(client.etat)   # la base ne garde que l'empreinte
    assert any(j["type"] == "raccourci_cree" for j in client.journal)
    assert "Scanner avec Apple" in client.get("/deposer").text


def test_envoi_brut_par_internet_sans_cookie(client, kd):  # noqa: F811
    jeton = _jeton(client)
    client.cookies.clear()   # le raccourci n'a ni session ni appareil approuvé
    r = client.post("/raccourci/envoi", content=b"%PDF-1.7 scan", headers={
        **CF, "Authorization": f"Bearer {jeton}", "Content-Type": "application/pdf"})
    assert r.status_code == 200 and "1 scan envoyé" in r.text
    (dossier, nom, contenu), depot = kd[0], kd[1]
    assert re.fullmatch(r"scan-\d{4}-\d\d-\d\d-\d\d-\d\d-\d\d\.pdf", nom) and contenu == b"%PDF-1.7 scan"
    assert depot == ("depot", 4242, 1) and "scan" in client.appels
    assert raccourci.etat(1)["nb"] == 1


def test_envoi_formulaire_plusieurs_fichiers(client, kd):  # noqa: F811
    jeton = _jeton(client)
    client.cookies.clear()
    r = client.post("/raccourci/envoi", headers={"Authorization": jeton}, files=[
        ("fichier", ("Document numérisé.pdf", b"%PDF-a", "application/pdf")),
        ("fichier", ("IMG_0001", b"\xff\xd8", "image/jpeg"))])
    assert r.status_code == 200 and "2 scans envoyés" in r.text
    assert [f[1] for f in kd if f[0] != "depot"] == ["Document numérisé.pdf", "IMG_0001.jpg"]


def test_jeton_faux_retire_ou_remplace_refuse(client, kd):  # noqa: F811
    ancien = _jeton(client)
    nouveau = _jeton(client)
    client.cookies.clear()
    envoyer = lambda j: client.post("/raccourci/envoi", content=b"%PDF", headers={  # noqa: E731
        "Authorization": f"Bearer {j}", "Content-Type": "application/pdf"})
    assert envoyer(ancien).status_code == 401
    assert envoyer("1.faux-faux-faux-faux-faux-faux-faux-faux").status_code == 401
    assert envoyer("").status_code == 401
    assert envoyer(nouveau).status_code == 200
    assert sum(j["type"] == "raccourci_echec" for j in client.journal) == 3
    raccourci.retirer(1)
    assert envoyer(nouveau).status_code == 401
    assert not [f for f in kd if f[0] == "depot" and f[1] != 4242]


def test_trop_d_echecs_bloque_l_adresse(client):  # noqa: F811
    client.cookies.clear()
    for _ in range(12):
        r = client.post("/raccourci/envoi", content=b"x", headers={"Authorization": "Bearer 1.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"})
    assert r.status_code == 429


def test_jeton_ne_sert_qu_a_deposer(client):  # noqa: F811
    jeton = _jeton(client)
    client.cookies.clear()
    r = client.get("/bibliotheque", headers={"Authorization": f"Bearer {jeton}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/connexion")
    assert client.get("/raccourci", follow_redirects=False).status_code == 303


def test_nom_du_scan():
    assert raccourci.nom_du_scan("a/b\\c.pdf", "application/pdf") == "a b c.pdf"
    assert raccourci.nom_du_scan("Note", "image/heic") == "Note.heic"
    assert raccourci.nom_du_scan("", "application/octet-stream").endswith(".pdf")


def test_paysage_bloque_sur_telephone_seulement(client):  # noqa: F811
    page = client.get("/deposer").text
    assert "Tourne ton téléphone" in page
    assert "(orientation: landscape) and (pointer: coarse) and (max-height: 500px)" in page
