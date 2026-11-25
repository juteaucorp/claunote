from claunote import version


def test_worker_a_jour():
    assert len(version.code()) == 12 and version.code() == version.code()
    assert version.worker_a_jour({"code": version.code()}) is True
    assert version.worker_a_jour({"code": "ancien"}) is False
    assert version.worker_a_jour({}) is None and version.worker_a_jour(None) is None
