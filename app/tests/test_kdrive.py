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
