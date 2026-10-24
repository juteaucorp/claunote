import json

import httpx

from claunote.kdrive import KDrive


def make(handler):
    return KDrive("tok", "42", transport=httpx.MockTransport(handler))


def test_list_dir_auth_et_pagination():
    seen = []

    def handler(req):
        seen.append(req)
        page = int(req.url.params["page"])
        data = [{"id": i, "name": f"f{i}", "type": "file"} for i in range(500 if page == 1 else 3)]
        return httpx.Response(200, json={"result": "success", "data": data})

    items = make(handler).list_dir(7)
    assert len(items) == 503
    assert seen[0].url.path == "/2/drive/42/files/7/files"
    assert seen[0].headers["authorization"] == "Bearer tok"
    assert len(seen) == 2


def test_upload_sur_hote_upload():
    def handler(req):
        assert req.url.host == "api.kdrive.infomaniak.com"
        assert req.url.path == "/2/drive/42/upload"
        assert req.url.params["directory_id"] == "9"
        assert req.url.params["total_size"] == "5"
        assert req.content == b"hello"
        return httpx.Response(200, json={"data": {"id": 1, "name": "a.md"}})

    assert make(handler).upload(9, "a.md", b"hello")["id"] == 1


def test_find_or_create_dir_cree_si_absent():
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"data": [{"id": 3, "name": "Maths", "type": "dir"}]})
        assert req.url.path == "/2/drive/42/files/1/directory"
        assert json.loads(req.content) == {"name": "BTS1"}
        return httpx.Response(200, json={"data": {"id": 8}})

    kd = make(handler)
    assert kd.find_or_create_dir(1, "Maths") == 3
    assert kd.find_or_create_dir(1, "BTS1") == 8


def test_panne_passagere_reessayee(monkeypatch):
    import pytest
    from claunote import kdrive
    monkeypatch.setattr(kdrive.time, "sleep", lambda s: None)
    reponses = [httpx.Response(503), httpx.Response(429, headers={"Retry-After": "3"}),
                httpx.Response(200, json={"data": {"id": 7}})]
    assert make(lambda req: reponses.pop(0)).stat(7) == {"id": 7}

    with pytest.raises(kdrive.KDriveIndisponible):  # toujours en panne : le worker patientera
        make(lambda req: httpx.Response(502)).stat(7)
    with pytest.raises(httpx.HTTPStatusError):        # 404 : pas une panne
        make(lambda req: httpx.Response(404)).stat(7)
    with pytest.raises(kdrive.KDriveIndisponible, match="jeton"):
        make(lambda req: httpx.Response(401)).stat(7)


def test_envoi_rename_pas_rejoue_s_il_est_peut_etre_parti(monkeypatch):
    import pytest
    from claunote import kdrive
    monkeypatch.setattr(kdrive.time, "sleep", lambda s: None)
    vus = []

    def handler(req):
        vus.append(req)
        return httpx.Response(500)
    with pytest.raises(kdrive.KDriveIndisponible):
        make(handler).upload(1, "a.pdf", b"x", conflict="rename")
    assert len(vus) == 1  # un 500 peut avoir créé le fichier : pas de doublon
    vus.clear()
    with pytest.raises(kdrive.KDriveIndisponible):
        make(handler).upload(1, "a.md", b"x")  # "version" : rejouable
    assert len(vus) == 4

    def coupe(req):
        vus.append(req)
        raise httpx.ConnectError("réseau coupé")
    vus.clear()
    with pytest.raises(kdrive.KDriveIndisponible):
        make(coupe).upload(1, "a.pdf", b"x", conflict="rename")
    assert len(vus) == 4  # connexion impossible : rien n'est parti, on peut réessayer


def test_upload_flux_par_morceaux_avec_taille(monkeypatch):
    import io
    import claunote.kdrive as k
    monkeypatch.setattr(k.time, "sleep", lambda s: None)
    recus = []

    def handler(req):
        corps = req.read()
        recus.append((req.headers.get("content-length"), req.headers.get("transfer-encoding"), len(corps)))
        if len(recus) == 1:
            return httpx.Response(429)  # refusé avant traitement : renvoyé depuis le début
        return httpx.Response(200, json={"data": {"id": 9, "name": req.url.params["file_name"]}})

    contenu = io.BytesIO(b"x" * (3 * 1024 * 1024 + 5))
    r = make(handler).upload_flux(3, "annee.pdf", contenu, 3 * 1024 * 1024 + 5)
    assert r == {"id": 9, "name": "annee.pdf"}
    assert recus == [(str(3 * 1024 * 1024 + 5), None, 3 * 1024 * 1024 + 5)] * 2
