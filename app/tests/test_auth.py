import time

from claunote import auth


def compte(mdp="claunote", secret=b"s"):
    return auth.Compte("admin", mdp, secret)


def test_hash_scrypt():
    h = auth.hacher("claunote")
    assert h.startswith("scrypt:") and "claunote" not in h and "$" not in h  # « $ » casse le .env de compose
    assert auth.verifier_hash("claunote", h) and not auth.verifier_hash("autre", h)
    assert not auth.verifier_hash("x", "pas-un-hash")


def test_mot_de_passe_en_clair_ou_hache():
    assert compte().par_defaut
    c = compte(auth.hacher("secret"))
    assert not c.par_defaut and c.verifier("ip", "admin", "secret") and not c.verifier("ip", "admin", "claunote")
    assert not c.verifier("ip", "root", "secret")


def test_session_signee_et_limitee_dans_le_temps():
    c = compte()
    s = c.creer_session()
    assert c.session_valide(s)
    assert not c.session_valide(s[:-1] + ("0" if s[-1] != "0" else "1"))
    assert not c.session_valide(s, maintenant=time.time() + auth.DUREE + 1)
    assert not c.session_valide(None) and not c.session_valide("n'importe quoi")
    assert not compte(secret=b"autre").session_valide(s)


def test_changer_le_mot_de_passe_deconnecte():
    s = compte().creer_session()
    assert not compte("nouveau").session_valide(s)


def test_blocage_apres_trop_d_echecs():
    c = compte()
    for _ in range(auth.ESSAIS_MAX):
        assert c.bloque("1.2.3.4") == 0
        c.verifier("1.2.3.4", "admin", "faux")
    assert c.bloque("1.2.3.4") > 0 and c.bloque("5.6.7.8") == 0
