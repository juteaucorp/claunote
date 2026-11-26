"""« kDrive seul » : pièces jointes, notes et sauvegardes rangées sur kDrive, cache borné, autres modes intacts."""
import datetime as dt
import json
import os
import subprocess

import httpx
import pytest

from claunote import db, kdrive_seul
from claunote.kdrive import KDrive


class FauxKDrive(KDrive):
    """kDrive en mémoire : dossiers par chemin, fichiers par numéro."""

    def __init__(self):
        self.dossiers = {(): 1}
        self.fichiers = {}            # id -> {"dossier", "name", "contenu", "size", "last_modified_at"}
        self.telechargements = 0
        self.suivant = 100

    def _id(self):
        self.suivant += 1
        return self.suivant

    def ensure_path(self, parent_id, parts):
        chemin = next(k for k, v in self.dossiers.items() if v == parent_id)
        for p in parts:
            chemin = (*chemin, p)
            self.dossiers.setdefault(chemin, self._id())
        return self.dossiers[chemin]

    def upload(self, dir_id, name, content, conflict="version"):
        for i, f in self.fichiers.items():
            if f["dossier"] == dir_id and f["name"] == name and conflict == "version":
                f.update(contenu=content, size=len(content), last_modified_at=f["last_modified_at"] + 1)
                return {"id": i}
        i = self._id()
        self.fichiers[i] = {"dossier": dir_id, "name": name, "contenu": content, "size": len(content),
                            "last_modified_at": 1000}
        return {"id": i}

    def upload_flux(self, dir_id, name, fichier, taille, conflict="rename"):
        fichier.seek(0)
        return self.upload(dir_id, name, fichier.read(), conflict)

    def stat(self, file_id):
        if file_id not in self.fichiers:
            req = httpx.Request("GET", "https://kdrive")
            raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
        f = self.fichiers[file_id]
        return {"id": file_id, "name": f["name"], "size": f["size"], "last_modified_at": f["last_modified_at"]}

    def download(self, file_id, dest):
        self.stat(file_id)
        self.telechargements += 1
        dest.write_bytes(self.fichiers[file_id]["contenu"])
        return dest

    def list_dir(self, folder_id):
        return [{"id": i, "name": f["name"], "type": "file"} for i, f in self.fichiers.items()
                if f["dossier"] == folder_id]

    def delete(self, file_id):
        self.fichiers.pop(file_id, None)

    def chemin(self, file_id):
        f = self.fichiers[file_id]
        d = next(k for k, v in self.dossiers.items() if v == f["dossier"])
        return "/".join((*d, f["name"]))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    return tmp_path / "kdrive"


def _kdrive_seul(monkeypatch, stockage="kdrive"):
    monkeypatch.setenv("STOCKAGE", stockage)
    for k in ("KDRIVE_TOKEN", "KDRIVE_ID", "KDRIVE_ETUDES_ID", "KDRIVE_INBOX_ID"):
        monkeypatch.setenv(k, "1")


def test_actif_seulement_en_kdrive_seul(monkeypatch):
    _kdrive_seul(monkeypatch, "kdrive")
    assert kdrive_seul.actif()
    for autre in ("local", "les_deux"):
        monkeypatch.setenv("STOCKAGE", autre)
        assert not kdrive_seul.actif()


def test_autres_stockages_rien_ne_change(monkeypatch):
    """Sur la VM ou « les deux » : l'entretien ne touche à rien (ni base, ni kDrive)."""
    _kdrive_seul(monkeypatch, "les_deux")
    monkeypatch.setattr(db, "pieces_a_ranger", lambda *a: pytest.fail("pièces touchées hors « kDrive seul »"))
    monkeypatch.setattr(db, "notes_a_copier", lambda *a: pytest.fail("notes touchées hors « kDrive seul »"))
    kdrive_seul.entretien(FauxKDrive(), 1, lambda c: [])


def test_cache_sert_sans_retelecharger_et_suit_les_versions(cache):
    kd = FauxKDrive()
    i = kd.upload(1, "cours.pdf", b"v1")["id"]
    a = kdrive_seul.en_cache(kd, i, "cours.pdf")
    b = kdrive_seul.en_cache(kd, i, "cours.pdf")
    assert a == b and a.read_bytes() == b"v1" and kd.telechargements == 1
    kd.upload(1, "cours.pdf", b"version 2")            # remplacé sur kDrive : la nouvelle version est servie
    c = kdrive_seul.en_cache(kd, i, "cours.pdf")
    assert c.read_bytes() == b"version 2" and kd.telechargements == 2
    assert not a.exists()                               # l'ancienne version ne reste pas dans le cache
    assert [p.name for p in cache.iterdir()] == [c.name]


def test_cache_borne_efface_les_moins_recents(cache):
    cache.mkdir(parents=True)
    for n in range(10):
        f = cache / f"{n}.pdf"
        f.write_bytes(b"x" * 100)
        os.utime(f, (1000 + n, 1000 + n))
    assert kdrive_seul.elaguer(maximum=2000) == 0       # sous la limite : rien
    assert kdrive_seul.elaguer(maximum=500) == 6        # jusqu'à 80 % de la limite (400 octets)
    assert sorted(p.name for p in cache.iterdir()) == ["6.pdf", "7.pdf", "8.pdf", "9.pdf"]


def test_pieces_rangees_sur_kdrive_puis_relues(cache, monkeypatch):
    kd = FauxKDrive()
    base = {1: {"id": 1, "nom": "emploi du temps.pdf", "contenu": b"%PDF edt", "compte_id": 1, "kdrive_id": None},
            2: {"id": 2, "nom": "sujet.png", "contenu": memoryview(b"png"), "compte_id": 2, "kdrive_id": None}}
    compacte = []
    monkeypatch.setattr(db, "pieces_a_ranger", lambda limite=20: [dict(p) for p in base.values()
                                                                   if p["contenu"] is not None][:limite])
    monkeypatch.setattr(db, "piece_rangee", lambda i, k: base[i].update(kdrive_id=k, contenu=None) or True)
    monkeypatch.setattr(db, "compacter_pieces", lambda: compacte.append(1))
    monkeypatch.setattr(db, "place_pieces", lambda: 50 * 1024 * 1024)
    racine = lambda compte: [] if compte == 1 else ["comptes", "camille"]   # noqa: E731
    assert kdrive_seul.ranger_pieces(kd, 1, racine) == 2
    assert compacte == [1]                              # toutes parties : la place est rendue au disque
    chemins = sorted(kd.chemin(p["kdrive_id"]) for p in base.values())
    assert chemins == ["Claunote/Pièces jointes/1-emploi du temps.pdf",
                       "comptes/camille/Claunote/Pièces jointes/2-sujet.png"]
    # Relue pour l'IA : depuis le cache (gardée à l'envoi), sans retélécharger
    lues = kdrive_seul.avec_contenu([dict(base[1])], kd)
    assert lues[0]["contenu"] == b"%PDF edt" and kd.telechargements == 0
    # Cache vidé : relue sur kDrive
    for f in cache.iterdir():
        f.unlink()
    assert kdrive_seul.contenu_piece(base[2], kd) == b"png" and kd.telechargements == 1
    # Effacée de kDrive à la main : laissée de côté, la révision n'est pas bloquée
    kd.delete(base[2]["kdrive_id"])
    for f in cache.iterdir():
        f.unlink()
    assert kdrive_seul.avec_contenu([dict(base[2])], kd) == []
    # Pas encore rangée : lue dans la base
    assert kdrive_seul.contenu_piece({"contenu": b"abc", "kdrive_id": None, "nom": "a.txt"}) == b"abc"


def test_notes_copiees_et_suivies(cache, monkeypatch):
    kd = FauxKDrive()
    maj = dt.datetime(2026, 11, 26, 10, tzinfo=dt.timezone.utc)
    note = {"id": 7, "compte_id": 1, "titre": "VLAN", "contenu": "trunk 802.1Q", "niveau": "BTS2",
            "matiere": "Réseaux", "maj_le": maj, "kdrive_id": None}
    libre = {"id": 8, "compte_id": 1, "titre": "", "contenu": "# Idées / projet\nà faire", "niveau": None,
             "matiere": None, "maj_le": maj, "kdrive_id": None}
    a_copier, copiees, corbeille = [note, libre], {}, []
    monkeypatch.setattr(db, "notes_a_copier", lambda limite=30: [dict(n) for n in a_copier])
    monkeypatch.setattr(db, "note_copiee", lambda i, k, v: copiees.update({i: (k, v)}) or True)
    monkeypatch.setattr(db, "mettre_a_la_corbeille", lambda ids: corbeille.extend(ids))
    assert kdrive_seul.copier_notes(kd, 1, lambda c: []) == 2
    assert kd.chemin(copiees[7][0]) == "Claunote/Notes/BTS2/Réseaux/VLAN (7).md"
    assert kd.chemin(copiees[8][0]) == "Claunote/Notes/Libres/Idées _ projet (8).md"
    assert kd.fichiers[copiees[7][0]]["contenu"] == b"trunk 802.1Q" and copiees[7][1] == maj
    # Modifiée sans changer de titre : même fichier, rien à la corbeille
    a_copier = [{**note, "contenu": "trunk + VLAN natif", "kdrive_id": copiees[7][0]}]
    kdrive_seul.copier_notes(kd, 1, lambda c: [])
    assert kd.fichiers[copiees[7][0]]["contenu"] == b"trunk + VLAN natif" and corbeille == []
    # Renommée : nouvelle copie, l'ancienne part à la corbeille kDrive
    ancien = copiees[7][0]
    a_copier = [{**note, "titre": "VLAN et trunk", "kdrive_id": ancien}]
    kdrive_seul.copier_notes(kd, 1, lambda c: [])
    assert corbeille == [ancien] and kd.chemin(copiees[7][0]) == "Claunote/Notes/BTS2/Réseaux/VLAN et trunk (7).md"


def test_sauvegarde_due_une_fois_par_jour_apres_3h():
    paris = lambda h, j=26: dt.datetime(2026, 11, j, h, 30, tzinfo=dt.timezone.utc) - dt.timedelta(hours=1)  # noqa: E731
    assert not kdrive_seul.sauvegarde_due(paris(2), {})
    assert kdrive_seul.sauvegarde_due(paris(3), {})
    assert not kdrive_seul.sauvegarde_due(paris(9), {"jour": "2026-11-26"})
    assert kdrive_seul.sauvegarde_due(paris(9), {"jour": "2026-11-25", "essai": "2026-11-25T03:30:00+01:00"})
    raté = {"jour": "2026-11-25", "essai": "2026-11-26T03:30:00+01:00"}
    assert not kdrive_seul.sauvegarde_due(paris(4, 26).replace(minute=0), raté)   # raté il y a 30 min
    assert kdrive_seul.sauvegarde_due(paris(5), raté)


def test_sauvegarde_envoyee_sur_kdrive_14_gardees_rien_sur_la_vm(cache, monkeypatch):
    kd = FauxKDrive()
    dossier = kd.ensure_path(1, ["Claunote", "Sauvegardes"])
    for j in range(1, 21):
        kd.upload(dossier, f"claunote-2026-11-{j:02d}.dump", b"vieux")
    kd.upload(dossier, "a-garder.txt", b"autre chose")
    etats = {}
    monkeypatch.setattr(db, "etat_ecrire", lambda cle, v: etats.update({cle: json.loads(v)}))
    monkeypatch.setattr(db, "etat_valeur", lambda cle: json.dumps(etats[cle]) if cle in etats else None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://claunote:mot%20de%20passe@db:5432/claunote")
    appels = []

    def faux_pg_dump(cmd, env, **k):
        appels.append((cmd, env))
        open(cmd[cmd.index("--file") + 1], "wb").write(b"PGDMP base")
        return subprocess.CompletedProcess(cmd, 0)

    quand = dt.datetime(2026, 11, 26, 2, 30, tzinfo=dt.timezone.utc)
    etat = kdrive_seul.sauvegarder_base(kd, 1, quand, executer=faux_pg_dump)
    assert etat["jour"] == "2026-11-26" and etat["taille"] == 10 and etat["erreur"] is None
    cmd, env = appels[0]
    assert "mot de passe" not in " ".join(cmd)          # jamais dans la ligne de commande
    assert env["PGPASSWORD"] == "mot de passe" and env["PGHOST"] == "db" and env["PGDATABASE"] == "claunote"
    noms = sorted(f["name"] for f in kd.fichiers.values() if f["dossier"] == dossier)
    assert len([n for n in noms if n.endswith(".dump")]) == 14 and "a-garder.txt" in noms
    assert "claunote-2026-11-26.dump" in noms and "claunote-2026-11-07.dump" not in noms
    assert not list(cache.parent.glob("sauvegarde-*"))  # la VM ne garde rien


def test_sauvegarde_ratee_garde_la_derniere_reussie(cache, monkeypatch):
    etats = {"sauvegarde_kdrive": {"jour": "2026-11-25", "le": "2026-11-25T03:30:00+01:00", "taille": 5}}
    monkeypatch.setattr(db, "etat_ecrire", lambda cle, v: etats.update({cle: json.loads(v)}))
    monkeypatch.setattr(db, "etat_valeur", lambda cle: json.dumps(etats[cle]) if cle in etats else None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/claunote")

    def rate(cmd, **k):
        raise subprocess.CalledProcessError(1, cmd, stderr=b"pg_dump: error: connection refused")

    etat = kdrive_seul.sauvegarder_base(FauxKDrive(), 1, dt.datetime(2026, 11, 26, 2, 30, tzinfo=dt.timezone.utc),
                                        executer=rate)
    assert etat["erreur"] == "pg_dump : pg_dump: error: connection refused"
    assert etat["le"] == "2026-11-25T03:30:00+01:00" and "jour" not in etat   # nouvel essai dans 1 h
