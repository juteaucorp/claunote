"""Fichiers sur la VM : même interface que kDrive, numéros stables, corbeille, copie de sauvegarde sur kDrive."""
import io
import os
import time

import httpx
import pytest

from claunote import db, reglages, stockage_local
from claunote.kdrive import KDrive
from claunote.stockage_local import Local


@pytest.fixture
def local(monkeypatch, tmp_path):
    """Table fichiers_locaux et copies_kdrive en mémoire, comme en base."""
    chemins, copies = {}, {}

    def fichier_id(chemin):
        for i, c in chemins.items():
            if c == chemin:
                return i
        i = 10**12 + len(chemins)
        chemins[i] = chemin
        return i

    def deplacer(ancien, nouveau):
        for i, c in list(chemins.items()):
            if c == nouveau or c.startswith(nouveau + "/"):
                del chemins[i]
        for i, c in list(chemins.items()):
            if c == ancien or c.startswith(ancien + "/"):
                chemins[i] = nouveau + c[len(ancien):]

    monkeypatch.setattr(db, "fichier_local_id", fichier_id)
    monkeypatch.setattr(db, "fichier_local_chemin", chemins.get)
    monkeypatch.setattr(db, "fichiers_locaux_deplacer", deplacer)
    monkeypatch.setattr(db, "copies_kdrive", lambda: dict(copies))
    monkeypatch.setattr(db, "copie_kdrive_faite", lambda c, t, m, k: copies.update({c: {"taille": t, "mtime": m,
                                                                                        "kdrive_id": k}}))
    monkeypatch.setenv("FICHIERS_DIR", str(tmp_path / "fichiers"))
    loc = Local.from_env()
    loc.copies = copies
    return loc


def test_meme_interface_que_kdrive(local):
    etudes, inbox = local.racines()
    assert {i["name"] for i in local.list_dir(local._id(local.racine))} == {"Etudes", "Inbox"}   # pas la corbeille
    dossier = local.ensure_path(etudes, ["BTS1", "Réseaux"])
    assert local.ensure_path(etudes, ["BTS1", "Réseaux"]) == dossier             # retrouvé, pas recréé
    a = local.upload(dossier, "vlan.md", b"# VLAN")
    assert a["type"] == "file" and a["size"] == 6
    assert local.upload(dossier, "vlan.md", b"# VLAN v2")["id"] == a["id"]     # conflict=version : remplacé
    b = local.upload(dossier, "vlan.md", b"autre", conflict="rename")
    assert b["name"] == "vlan (1).md" and b["id"] != a["id"]
    gros = local.upload_flux(inbox, "scan.pdf", io.BytesIO(b"%PDF" * 1000), 4000)
    assert local.stat(gros["id"])["size"] == 4000
    assert [i["name"] for i in local.list_dir(inbox)] == ["scan.pdf"]
    dest = local.download(gros["id"], local.racine.parent / "cache" / "scan.pdf")
    assert dest.read_bytes().startswith(b"%PDF")


def test_numero_stable_quand_on_renomme_ou_deplace(local):
    etudes, inbox = local.racines()
    d = local.ensure_path(inbox, ["2024"])
    f = local.upload(d, "tp.pdf", b"x")["id"]
    local.move(d, etudes)                       # tout le dossier change de place : son contenu suit
    assert local.stat(f)["name"] == "tp.pdf" and (local.racine / "Etudes/2024/tp.pdf").exists()
    local.rename(f, "tp-vlan.pdf")
    assert local.stat(f)["name"] == "tp-vlan.pdf"
    local.delete(f)                              # à la corbeille, comme kDrive : plus listé, plus trouvable
    assert local.list_dir(d) == []
    with pytest.raises(httpx.HTTPStatusError) as e:
        local.stat(f)
    assert e.value.response.status_code == 404
    assert any(p.name.endswith("tp-vlan.pdf") for p in (local.racine / ".corbeille").iterdir())


def test_un_nom_ne_sort_jamais_de_son_dossier(local):
    _, inbox = local.racines()
    f = local.upload(inbox, "../../etc/passwd", b"x")
    assert f["name"] == ".._.._etc_passwd" and (local.racine / "Inbox" / ".._.._etc_passwd").exists()
    assert local.mkdir(inbox, "..")["name"] == "sans-nom"


def test_corbeille_videe_apres_30_jours(local):
    _, inbox = local.racines()
    local.delete(local.upload(inbox, "vieux.pdf", b"x")["id"])
    vieux = next((local.racine / ".corbeille").iterdir())
    os.utime(vieux, (time.time() - 31 * 86400,) * 2)
    local.delete(local.upload(inbox, "recent.pdf", b"x")["id"])
    assert local.purger_corbeille() == 1
    assert [p.name.split("-", 1)[1] for p in (local.racine / ".corbeille").iterdir()] == ["recent.pdf"]


def test_les_deux_copie_les_cours_sur_kdrive(local):
    etudes, inbox = local.racines()
    local.upload(local.ensure_path(etudes, ["BTS1", "Réseaux"]), "vlan.pdf", b"x")
    local.upload(inbox, "pas-encore-range.pdf", b"x")   # l'inbox n'est pas copiée

    class FauxKDrive:
        def __init__(self):
            self.envois = []

        def ensure_path(self, parent, parts):
            return hash((parent, *parts)) % 1000

        def upload_flux(self, dossier, nom, f, taille, conflict):
            self.envois.append((nom, f.read(), conflict))
            return {"id": len(self.envois)}
    kd = FauxKDrive()
    assert local.copier_vers_kdrive(kd, 77) == 1 and kd.envois == [("vlan.pdf", b"x", "version")]
    assert local.copier_vers_kdrive(kd, 77) == 0                      # déjà copié, inchangé
    (local.racine / "Etudes/BTS1/Réseaux/vlan.pdf").write_bytes(b"modifie")
    assert local.copier_vers_kdrive(kd, 77) == 1                      # modifié : recopié


def test_le_stockage_choisi_decide(monkeypatch, local):
    monkeypatch.setenv("STOCKAGE", "local")
    assert isinstance(KDrive.from_env(), Local) and reglages.stockage_pret()
    assert reglages.dossiers() == local.racines()
    monkeypatch.setenv("STOCKAGE", "les_deux")
    monkeypatch.delenv("KDRIVE_TOKEN", raising=False)
    assert isinstance(KDrive.from_env(), Local) and not reglages.stockage_pret()   # la copie attend kDrive
    monkeypatch.setenv("STOCKAGE", "kdrive")
    monkeypatch.setenv("KDRIVE_TOKEN", "t")
    monkeypatch.setenv("KDRIVE_ID", "1")
    assert isinstance(KDrive.from_env(), KDrive)
    assert stockage_local.nom_sur("a/b") == "a_b"
