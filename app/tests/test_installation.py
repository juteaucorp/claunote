"""Assistant d'installation et Réglages → Installation : secrets chiffrés, jamais réaffichés, appliqués tout de suite."""
import os
import stat

import pytest

from claunote import agent, db, installation, main, maj, reglages
from tests.test_comptes import _creer, _inviter
from tests.test_site import anonyme, client  # noqa: F401  (fixtures du site)

JETON_CLAUDE = "sk-ant-oat01-" + "x" * 40


@pytest.fixture
def base_reglages(monkeypatch, tmp_path):
    """Table reglages en mémoire, clé et jeton du tunnel dans un dossier jetable, variables remises à la fin."""
    lignes = {}
    monkeypatch.setattr(reglages, "CLE", tmp_path / "secrets" / "claunote.key")
    monkeypatch.setattr(reglages, "JETON_TUNNEL", tmp_path / "tunnel" / "token")
    monkeypatch.setattr(db, "reglages", lambda: [dict(cle=k, **v) for k, v in sorted(lignes.items())])
    monkeypatch.setattr(db, "reglage_ecrire", lambda cle, valeur, chiffre, compte_id: lignes.update(
        {cle: {"valeur": valeur, "chiffre": chiffre, "maj_le": None, "maj_par": compte_id}}))
    monkeypatch.setattr(db, "reglage_effacer", lambda cle: lignes.pop(cle, None))
    for r in reglages.CATALOGUE:
        monkeypatch.delenv(r.cle, raising=False)
    monkeypatch.setattr(main, "INBOX_ID", main.INBOX_ID)
    monkeypatch.setattr(main, "ETUDES_ID", main.ETUDES_ID)
    monkeypatch.setattr(agent, "ENV_FORFAIT", agent.ENV_FORFAIT)
    monkeypatch.setattr(maj, "DOSSIER", tmp_path)
    return lignes


def test_secret_chiffre_avec_une_cle_hors_de_la_base(base_reglages, tmp_path):
    reglages.ecrire("CLAUDE_CODE_OAUTH_TOKEN", JETON_CLAUDE, 1)
    ligne = base_reglages["CLAUDE_CODE_OAUTH_TOKEN"]
    assert ligne["chiffre"] and JETON_CLAUDE not in ligne["valeur"]
    assert stat.S_IMODE(os.stat(reglages.CLE).st_mode) == 0o600
    assert reglages.valeurs()["CLAUDE_CODE_OAUTH_TOKEN"] == JETON_CLAUDE
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == JETON_CLAUDE
    # Une copie de la base sans la clé ne suffit pas : autre clé, secret illisible (et signalé)
    reglages.CLE.unlink()
    assert reglages.valeurs() == {}
    assert reglages.etat()["CLAUDE_CODE_OAUTH_TOKEN"]["illisible"]
    # Jamais la valeur d'un secret pour une page ; les réglages ordinaires, si
    reglages.ecrire("KDRIVE_ID", "12345", 1)
    e = reglages.etat()
    assert e["CLAUDE_CODE_OAUTH_TOKEN"]["valeur"] is None and e["KDRIVE_ID"]["valeur"] == "12345"
    reglages.ecrire("KDRIVE_ID", "", 1)   # vide : retiré de la base, le .env reprend la main
    assert "KDRIVE_ID" not in base_reglages and "KDRIVE_ID" not in os.environ


def test_jeton_du_tunnel_dans_un_fichier_pour_cloudflared(base_reglages):
    reglages.ecrire("CLOUDFLARE_TUNNEL_TOKEN", "eyJ-tunnel", 1)
    assert reglages.JETON_TUNNEL.read_text() == "eyJ-tunnel"
    reglages.ecrire("CLOUDFLARE_TUNNEL_TOKEN", None, 1)
    assert not reglages.JETON_TUNNEL.exists()


def test_installation_existante_jamais_d_assistant(client):
    assert client.etat["installation"] == "faite"   # 8 documents : rien d'imposé
    assert client.get("/", follow_redirects=False).status_code == 200


def test_installation_neuve_ouvre_l_assistant(client, base_reglages, monkeypatch):
    client.etat.pop("installation")
    monkeypatch.setattr(db, "nb_documents", lambda: 0)
    installation.detecter()
    assert client.etat["installation"] == "a_faire"
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/installation"
    page = client.get("/installation/disque").text
    assert "Espace disque" in page and "Je règlerai plus tard" in page
    r = client.post("/installation/disque", data={"action": "enregistrer"}, follow_redirects=False)
    assert r.headers["location"] == "/installation/compte"
    r = client.post("/installation/fin", data={"action": "quitter"}, follow_redirects=False)
    assert r.headers["location"] == "/" and client.etat["installation"] == "faite"


def test_compte_admin_mot_de_passe_obligatoire_si_par_defaut(client, base_reglages, faux_comptes):
    faux_comptes.comptes[1]["mdp_defaut"] = True
    r = client.post("/installation/compte", data={"identifiant": "anto", "mot_de_passe": "", "contact": ""})
    assert r.status_code == 400 and "au moins 10" in r.text
    r = client.post("/installation/compte", data={"identifiant": "anto", "mot_de_passe": "un-vrai-mot-de-passe",
                                                  "confirmation": "un-vrai-mot-de-passe", "contact": "moi@exemple.fr"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/installation/stockage"
    assert faux_comptes.comptes[1]["identifiant"] == "anto" and not faux_comptes.comptes[1]["mdp_defaut"]
    assert os.environ["CLAUNOTE_CONTACT"] == "moi@exemple.fr"
    assert client.get("/installation/stockage").status_code == 200   # toujours connecté sur cet appareil


def test_claude_enregistre_chiffre_et_jamais_reaffiche(client, base_reglages):
    r = client.post("/installation/claude", data={"claude_token": "pas-un-jeton"})
    assert r.status_code == 400 and "sk-ant-" in r.text
    r = client.post("/installation/claude", data={"claude_token": JETON_CLAUDE}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/installation/cloudflare"
    assert base_reglages["CLAUDE_CODE_OAUTH_TOKEN"]["chiffre"]
    assert agent.ENV_FORFAIT["CLAUDE_CODE_OAUTH_TOKEN"] == JETON_CLAUDE   # utilisé dès le prochain appel
    for page in ("/installation/claude", "/reglages/installation"):
        texte = client.get(page).text
        assert JETON_CLAUDE not in texte and "laisse vide pour le garder" in texte
    # Laissé vide : le jeton enregistré est gardé
    client.post("/installation/claude", data={"claude_token": "", "retour": "reglages"})
    assert reglages.valeurs()["CLAUDE_CODE_OAUTH_TOKEN"] == JETON_CLAUDE


def test_sans_claude_passe_en_local(client, base_reglages):
    r = client.post("/installation/claude", data={"action": "sans_claude"})
    assert r.status_code == 200 and "modèles locaux" in r.text and client.etat["analyse_locale"] == "toujours"


def test_kdrive_teste_avant_d_enregistrer(client, base_reglages, monkeypatch):
    essais = []
    monkeypatch.setattr(reglages, "tester_kdrive", lambda jeton, drive, *ids: essais.append((jeton, drive, ids)) or (
        (False, "kDrive refuse : 401", []) if jeton == "faux" else (True, "Connexion à kDrive réussie.", [])))
    monkeypatch.setattr(reglages, "creer_dossiers_kdrive", lambda jeton, drive: (501, 502))
    donnees = {"stockage": "kdrive", "kdrive_token": "faux", "kdrive_id": "42", "kdrive_etudes_id": "7",
               "kdrive_inbox_id": "8"}
    r = client.post("/installation/stockage", data=donnees)
    assert r.status_code == 400 and "été enregistré" in r.text and "KDRIVE_TOKEN" not in base_reglages
    assert client.post("/installation/stockage", data={**donnees, "kdrive_id": "abc"}).status_code == 400
    r = client.post("/installation/stockage", data={**donnees, "kdrive_token": "bon-jeton", "kdrive_etudes_id": "",
                                                    "kdrive_inbox_id": "", "action": "creer_dossiers"})
    assert r.status_code == 200 and "Claunote/Etudes" in r.text
    assert main.INBOX_ID == 502 and main.ETUDES_ID == 501 and reglages.kdrive_configure()
    assert base_reglages["KDRIVE_TOKEN"]["chiffre"] and "bon-jeton" not in r.text
    r = client.post("/installation/stockage", data={**donnees, "stockage": "local", "kdrive_token": ""})
    assert r.status_code == 400   # PR suivante : stockage sur la VM


def test_cloudflare_allume_le_tunnel_par_le_service_maj(client, base_reglages, monkeypatch):
    assert client.post("/installation/cloudflare", data={"pays": "France"}).status_code == 400
    assert client.post("/installation/cloudflare", data={"pays": "FR", "tunnel": "1"}).status_code == 400   # sans jeton
    demandes = []
    monkeypatch.setattr(maj, "present", lambda: True)
    monkeypatch.setattr(maj, "demander", demandes.append)
    r = client.post("/installation/cloudflare", data={"pays": "fr, be", "tunnel": "1", "tunnel_token": "eyJ-t",
                                                      "retour": "reglages"})
    assert r.status_code == 200 and "Le tunnel démarre" in r.text and demandes == ["verifier"]
    assert maj.tunnel_actif() and (maj.DOSSIER / maj.TUNNEL_RELANCER).exists()
    assert os.environ["CLAUNOTE_PAYS"] == "FR,BE" and reglages.JETON_TUNNEL.read_text() == "eyJ-t"
    assert any(j["type"] == "reglage" for j in client.journal)   # noté au journal de sécurité, avec alerte


def test_reserve_a_l_admin(client, base_reglages):
    invite, _ = _creer(_inviter(client))
    for page in ("/installation/claude", "/reglages/installation"):
        assert invite.get(page).status_code == 403
    assert invite.post("/installation/claude", data={"claude_token": JETON_CLAUDE}).status_code == 403
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in base_reglages
