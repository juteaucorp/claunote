from claunote import cles, db
from tests.test_site import anonyme, client  # noqa: F401  (fixtures du site)


def test_possible():
    assert cles.possible("10.8.0.5:8443", True) == "ip"
    assert cles.possible("[::1]:8000", True) == "ip"
    assert cles.possible("claunote.lan", False) == "http"
    assert cles.possible("localhost:8077", False) is None          # localhost : WebAuthn accepte le HTTP
    assert cles.possible("claunote.10.8.0.5.sslip.io", True) is None
    assert cles.nom_sslip("10.8.0.5:8443") == "claunote.10.8.0.5.sslip.io" and cles.nom_sslip("claunote.lan") is None
    assert cles.hote("claunote.lan:8443") == "claunote.lan"


def test_options_et_defi_a_usage_unique():
    jeton, options = cles.options_connexion("claunote.lan")
    assert '"rpId": "claunote.lan"' in options and '"userVerification": "required"' in options
    cles._defi(jeton)
    try:
        cles._defi(jeton)                       # un défi ne sert qu'une fois
        assert False
    except cles.CleRefusee:
        pass


def test_compte_explique_l_adresse_ip(client):
    r = client.get("/compte", headers={"host": "10.8.0.5:8000"})
    assert "CLAUNOTE_HOTE=claunote.10.8.0.5.sslip.io" in r.text and "Ajouter cet appareil" not in r.text
    assert client.post("/cles/inscription/options", headers={"host": "10.8.0.5:8000"}).status_code == 400


def test_compte_propose_face_id(client):
    r = client.get("/compte", headers={"host": "localhost:8077"})
    assert "Ajouter cet appareil" in r.text and "/static/cles.js" in r.text
    r = client.post("/cles/inscription/options", headers={"host": "localhost:8077"})
    assert r.status_code == 200 and '"rp": {"name": "Claunote", "id": "localhost"}' in r.text and "claunote_defi" in r.cookies


def test_connexion_par_cle_refusee(anonyme, monkeypatch):
    monkeypatch.setattr(db, "cle_acces", lambda i: None)
    assert anonyme.post("/cles/connexion/options", headers={"host": "localhost"}).status_code == 200   # page publique
    r = anonyme.post("/cles/connexion", json={"cle": {"id": "inconnue"}}, headers={"host": "localhost"})
    assert r.status_code == 400 and "clé inconnue" in r.json()["erreur"]
    assert anonyme.get("/compte", follow_redirects=False).status_code == 303          # le reste demande une session
    assert anonyme.post("/cles/inscription/options").status_code == 401


def test_bouton_face_id_seulement_avec_une_cle(anonyme, monkeypatch):
    assert "Face ID" not in anonyme.get("/connexion", headers={"host": "localhost"}).text
    monkeypatch.setattr(db, "cles_acces", lambda: [{"id": "x"}])
    assert "Se connecter avec Face ID" in anonyme.get("/connexion", headers={"host": "localhost"}).text
    assert "Se connecter avec Face ID" not in anonyme.get("/connexion", headers={"host": "10.8.0.5"}).text
