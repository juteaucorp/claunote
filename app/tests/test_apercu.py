"""« Voir en tant que » : l'admin voit le site comme un compte choisi, en lecture seule, noté dans Sécurité."""
import time

from fastapi.testclient import TestClient

from claunote import apercu, auth, db, main
from tests.test_site import anonyme, client  # noqa: F401  (fixtures du site)

SECRET = b"cle-du-site"
ADMIN = {"id": 1, "hash": "h", "generation": 0}


def test_cookie_signe_pour_cet_admin_seulement():
    cookie = apercu.creer(SECRET, ADMIN, 2)
    assert apercu.lire(SECRET, cookie, ADMIN)[0] == 2
    assert apercu.lire(SECRET, cookie, {**ADMIN, "id": 3}) is None             # un autre compte
    assert apercu.lire(SECRET, cookie, {**ADMIN, "hash": "nouveau"}) is None   # mot de passe changé
    assert apercu.lire(SECRET, cookie, {**ADMIN, "generation": 1}) is None     # « Déconnecter partout »
    assert apercu.lire(b"autre", cookie, ADMIN) is None
    assert apercu.lire(SECRET, cookie, ADMIN, maintenant=time.time() + apercu.DUREE + 1) is None   # 1 h passée
    assert apercu.lire(SECRET, apercu.creer(SECRET, ADMIN, 1), ADMIN) is None   # soi-même : rien à voir
    charge, signature = cookie.rsplit(".", 1)
    assert apercu.lire(SECRET, charge[:-2] + "xx." + signature, ADMIN) is None
    assert apercu.lire(SECRET, None, ADMIN) is None and apercu.lire(SECRET, cookie, None) is None


def _camille(faux_comptes, droits=("bibliotheque",)):
    return faux_comptes.creer_compte("camille", auth.hacher("un-mot-de-passe-solide"), "invite", list(droits),
                                     nom="Camille")


def test_voir_en_tant_que(client, faux_comptes):
    i = _camille(faux_comptes)
    assert "/comptes/2/apercu" in client.get("/comptes").text
    assert "/comptes/1/apercu" not in client.get("/comptes").text                # pas pour soi
    r = client.post(f"/comptes/{i}/apercu", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/bibliotheque"     # sa première page
    assert apercu.COOKIE in client.cookies
    page = client.get("/bibliotheque")
    assert page.status_code == 200 and "comme Camille" in page.text and "lecture seule" in page.text
    assert "/apercu/quitter" in page.text
    # Ses droits à lui : pas Réviser, pas l'administration
    assert client.get("/reviser").status_code == 403
    assert client.get("/comptes").status_code == 403
    assert client.get("/securite").status_code == 403
    # Rien n'est fait à sa place, même ce qu'il a le droit de faire
    r = client.post("/doc/1/visibilite", data={"valeur": "public"}, headers={"HX-Request": "true"})
    assert r.status_code == 403 and "lecture seule" in r.text
    assert "toast-apercu" in page.text and "en-apercu" in page.text   # bouton bloqué sur place, message intégré
    r = client.post(f"/comptes/{i}/apercu")
    assert r.status_code == 403 and "Lecture seule · Claunote" in r.text and "--accent" in r.text   # repli au style claunote
    debut = [j for j in client.journal if j["type"] == "apercu"]
    assert len(debut) == 1 and "« admin » voit le site comme « camille »" in debut[0]["detail"]
    # Retour à son compte
    r = client.post("/apercu/quitter", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/comptes#compte-{i}"
    assert apercu.COOKIE not in client.cookies
    assert client.get("/comptes").status_code == 200
    assert "ne voit plus le site comme « camille »" in [j for j in client.journal if j["type"] == "apercu"][-1]["detail"]


def test_base_en_lecture_seule_pendant_l_apercu(client, faux_comptes, monkeypatch):
    i = _camille(faux_comptes)
    vu = []
    monkeypatch.setattr(db, "bibliotheque", lambda: vu.append((db.compte_actuel(), db.en_lecture_seule())) or [])
    client.get("/bibliotheque")
    client.post(f"/comptes/{i}/apercu", follow_redirects=False)
    client.get("/bibliotheque")
    assert vu == [(1, False), (i, True)]


def test_un_invite_ne_peut_pas_s_en_servir(client, faux_comptes):
    i = _camille(faux_comptes)
    faux_comptes.creer_compte("dominique", auth.hacher("un-mot-de-passe-solide"), "invite", ["bibliotheque"])
    invite = TestClient(main.app)
    assert invite.post("/connexion", data={"utilisateur": "camille", "mot_de_passe": "un-mot-de-passe-solide"},
                       follow_redirects=False).status_code == 303
    assert invite.post("/comptes/3/apercu").status_code == 403                   # réservé à l'admin
    # Un cookie d'aperçu d'admin recopié chez l'invité ne vaut rien : signé pour la session de l'admin
    client.post("/comptes/3/apercu")
    invite.cookies.set(apercu.COOKIE, client.cookies[apercu.COOKIE])
    page = invite.get("/bibliotheque")
    assert page.status_code == 200 and "/apercu/quitter" not in page.text
    assert invite.get("/reglages").status_code == 200 and i == 2


def test_l_admin_qui_n_est_plus_admin_perd_l_apercu(client, faux_comptes):
    i = _camille(faux_comptes)
    faux_comptes.creer_compte("dominique", auth.hacher("x"), "admin", [])
    client.post(f"/comptes/{i}/apercu")
    assert "/apercu/quitter" in client.get("/bibliotheque").text
    faux_comptes.comptes[1]["role"] = "invite"
    from claunote import comptes
    comptes.oublier()
    assert "/apercu/quitter" not in client.get("/bibliotheque").text
