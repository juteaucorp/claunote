"""Comptes : invitations de 15 min, droits par fonctionnalité, données séparées, « Tout voir », conditions."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from claunote import comptes, db, disque, main
from tests.test_site import anonyme, client  # noqa: F401  (fixtures du site)

MDP = "un-mot-de-passe-solide"


def _jeton(page: str) -> str:
    return re.search(r"[A-Z2-9]{5}(?:-[A-Z2-9]{5}){4}", page).group(0)


def _inviter(client, droits=None, role="invite"):
    data = {"role": role, "note": "Camille", "droits": droits if droits is not None else comptes.DROITS_INVITE}
    r = client.post("/comptes/inviter", data=data)
    assert r.status_code == 200 and "Affiché <strong>une seule fois</strong>" in r.text
    return _jeton(r.text)


def _creer(jeton, identifiant="camille", accepte="1"):
    c = TestClient(main.app)
    r = c.post("/invitation", data={"jeton": jeton, "identifiant": identifiant, "mot_de_passe": MDP,
                                    "confirmation": MDP, "accepte": accepte}, follow_redirects=False)
    return c, r


def test_droit_requis():
    assert comptes.droit_requis("/comptes") == comptes.droit_requis("/securite/debloquer", "POST") == "admin"
    assert comptes.droit_requis("/reviser/nouvelle") == comptes.droit_requis("/cartes/anki") == "reviser"
    assert comptes.droit_requis("/doc/3") == "bibliotheque"
    assert comptes.droit_requis("/doc/3/supprimer", "POST") == "organiser"
    assert comptes.droit_requis("/organiser/matieres") == "organiser"
    assert comptes.droit_requis("/appareils/code", "POST") == "admin" and comptes.droit_requis("/appareil") is None
    assert comptes.droit_requis("/compte") is None and comptes.droit_requis("/notifications") is None
    assert comptes.besoin_claude("/reviser", "POST") and comptes.besoin_claude("/revision/4/modifier", "POST")
    assert not comptes.besoin_claude("/reviser/demande", "POST") and not comptes.besoin_claude("/reviser", "GET")


def test_jeton_lisible_et_normalise():
    j = comptes.nouveau_jeton()
    assert re.fullmatch(r"[A-Z2-9]{5}(-[A-Z2-9]{5}){4}", j)
    assert comptes.normaliser_jeton(j.lower().replace("-", " ")) == j


def test_invitation_cree_un_compte_une_seule_fois(client, faux_comptes):
    jeton = _inviter(client, ["bibliotheque", "reviser"])
    invite, r = _creer(jeton)
    assert r.status_code == 303                                      # connecté tout de suite
    k = db.compte_par_identifiant("camille")
    assert k["role"] == "invite" and k["droits"] == ["bibliotheque", "reviser"]
    assert k["conditions_version"] == comptes.CONDITIONS_VERSION    # consentement enregistré
    assert invite.get("/bibliotheque").status_code == 200
    _, r = _creer(jeton, "dominique")                                 # jeton déjà utilisé
    assert r.status_code == 403 and db.compte_par_identifiant("dominique") is None
    assert "compte_cree" in [j["type"] for j in client.journal]


def test_invitation_expiree_ou_sans_consentement(client, faux_comptes):
    jeton = _inviter(client)
    _, r = _creer(jeton, accepte="")
    assert r.status_code == 400 and "accepter les conditions" in r.text
    assert db.compte_par_identifiant("camille") is None
    for inv in faux_comptes.invitations.values():                    # 15 min passées
        inv["expire_le"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    _, r = _creer(jeton)
    assert r.status_code == 403 and "expiré" in r.text


def test_invitation_identifiant_et_mot_de_passe_verifies(client):
    jeton = _inviter(client)
    c = TestClient(main.app)
    r = c.post("/invitation", data={"jeton": jeton, "identifiant": "admin", "mot_de_passe": MDP,
                                    "confirmation": MDP, "accepte": "1"})
    assert r.status_code == 400 and "déjà pris" in r.text
    r = c.post("/invitation", data={"jeton": jeton, "identifiant": "camille", "mot_de_passe": "court",
                                    "confirmation": "court", "accepte": "1"})
    assert r.status_code == 400 and "10 caractères" in r.text
    _, r = _creer(jeton)                                              # le jeton n'a pas été brûlé par les erreurs
    assert r.status_code == 303


def test_jeton_devine_bloque(anonyme):
    c = TestClient(main.app)
    for _ in range(5):
        c.post("/invitation", data={"jeton": "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"})
    r = c.post("/invitation", data={"jeton": "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"})
    assert r.status_code == 429


def test_invite_ne_voit_pas_l_administration(client):
    invite, _ = _creer(_inviter(client, ["bibliotheque"]))
    for page in ("/comptes", "/securite", "/analyses", "/mise-a-jour", "/import", "/reviser", "/agenda"):
        assert invite.get(page).status_code == 403, page
    assert invite.post("/comptes/inviter", data={"role": "admin"}).status_code == 403   # pas d'auto-promotion
    assert invite.post("/comptes/2/modifier", data={"role": "admin", "actif": "1"}).status_code == 403
    page = invite.get("/").text
    assert 'id="maj-h-reviser"' not in page and 'id="maj-h-agenda"' not in page
    assert "/comptes/tout-voir" not in page and 'href="/analyses"' not in page


def test_sans_forfait_claude_pas_de_fiche(client):
    invite, _ = _creer(_inviter(client, ["bibliotheque", "reviser"]))
    r = invite.post("/reviser", data={"genre": "fiche", "niveau": "BTS1", "matiere": "Réseaux"})
    assert r.status_code == 403 and "Forfait Claude" in r.text
    assert invite.get("/reviser").status_code == 200                  # réviser ses cartes reste possible


def test_chaque_requete_travaille_pour_son_compte(client, monkeypatch):
    """La base est interrogée au nom du compte connecté (row-level security) ; « Tout voir » pour l'admin."""
    vus = []
    monkeypatch.setattr(db, "liste", lambda *a: vus.append((db.compte_actuel(), db._compte.get())) or [])
    invite, _ = _creer(_inviter(client))
    invite.get("/liste", params={"niveau": "BTS1", "matiere": "Réseaux", "type": "TP"})
    client.get("/liste", params={"niveau": "BTS1", "matiere": "Réseaux", "type": "TP"})
    assert vus[0] == (2, (2, False)) and vus[1] == (1, (1, False))
    client.post("/comptes/tout-voir", data={"actif": "1", "suite": "/liste"})
    r = client.get("/liste", params={"niveau": "BTS1", "matiere": "Réseaux", "type": "TP"})
    assert vus[-1] == (1, (1, True)) and "Revenir à mes données" in r.text
    invite.post("/comptes/tout-voir", data={"actif": "1"})            # refusé à un invité
    invite.get("/liste", params={"niveau": "BTS1", "matiere": "Réseaux", "type": "TP"})
    assert vus[-1] == (2, (2, False))


def test_desactiver_supprimer_et_garde_fous(client, faux_comptes):
    invite, _ = _creer(_inviter(client))
    r = client.post("/comptes/2/modifier", data={"role": "invite", "droits": ["bibliotheque"], "nom": "Camille"},
                    follow_redirects=False)
    assert r.status_code == 303 and not faux_comptes.comptes[2]["actif"]
    assert invite.get("/bibliotheque", follow_redirects=False).status_code == 303   # déconnecté
    r = client.post("/connexion", data={"utilisateur": "camille", "mot_de_passe": MDP})
    assert "désactivé" in TestClient(main.app).post(
        "/connexion", data={"utilisateur": "camille", "mot_de_passe": MDP}).text
    # pas d'admin qui se retire ses droits, pas de suppression du compte d'origine
    assert client.post("/comptes/1/modifier", data={"role": "invite", "actif": "1"}).status_code == 400
    assert client.post("/comptes/1/supprimer", data={"confirmation": "admin"}).status_code == 400
    assert client.post("/comptes/2/supprimer", data={"confirmation": "autre"}).status_code == 400
    assert client.post("/comptes/2/supprimer", data={"confirmation": "camille"}, follow_redirects=False).status_code == 303
    assert 2 not in faux_comptes.comptes


def test_lien_de_nouveau_mot_de_passe(client, faux_comptes):
    _creer(_inviter(client))
    r = client.post("/comptes/2/mot-de-passe")
    assert "Lien de nouveau mot de passe pour « camille »" in r.text
    jeton = _jeton(r.text)
    c = TestClient(main.app)
    assert "choisis ton nouveau mot de passe" in c.get("/invitation", params={"jeton": jeton}).text
    r = c.post("/invitation", data={"jeton": jeton, "mot_de_passe": "nouveau-mot-de-passe",
                                    "confirmation": "nouveau-mot-de-passe"}, follow_redirects=False)
    assert r.status_code == 303
    assert TestClient(main.app).post("/connexion", data={"utilisateur": "camille", "mot_de_passe": "nouveau-mot-de-passe"},
                                     follow_redirects=False).status_code == 303


def test_nouvelles_conditions_a_accepter(client, faux_comptes, monkeypatch):
    monkeypatch.setattr(comptes, "CONDITIONS_VERSION", "2099-01-01")
    comptes.oublier()
    r = client.get("/bibliotheque", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/conditions"
    assert "ont changé" in client.get("/conditions").text
    assert client.post("/conditions", data={"accepte": "1"}, follow_redirects=False).status_code == 303
    assert faux_comptes.comptes[1]["conditions_version"] == "2099-01-01"
    assert client.get("/bibliotheque").status_code == 200


def test_pages_juridiques_publiques(anonyme):
    for page, mot in (("/conditions", "non commercial"), ("/tes-donnees", "CNIL")):
        r = TestClient(main.app).get(page)
        assert r.status_code == 200 and mot in r.text and "conseil juridique" in r.text


def test_disque(monkeypatch):
    def faux(go_libres, total=40):
        monkeypatch.setattr(disque, "mesurer", lambda chemin="/": {"total": total * disque.GO, "utilise": 0,
                                                                   "libre": go_libres * disque.GO})
        disque._cache["le"] = float("-inf")
        return disque.etat()
    assert faux(30)["niveau"] == "ok"
    assert faux(10)["niveau"] == "juste"
    assert faux(4)["niveau"] == "attention" and disque.a_signaler(disque.etat())
    assert faux(1)["niveau"] == "critique"
    assert faux(30, total=1000)["niveau"] == "attention"     # moins de 10 % libres
