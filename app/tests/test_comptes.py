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
    assert comptes.besoin_claude("/organiser/matieres/ia", "POST")
    # fiches, quiz, campagnes, modifications : sans forfait, elles attendent l'accord de l'admin (pas de refus)
    assert not comptes.besoin_claude("/reviser", "POST") and not comptes.besoin_claude("/revision/4/modifier", "POST")
    assert not comptes.besoin_claude("/reviser/demande", "POST") and not comptes.besoin_claude("/reviser", "GET")
    assert comptes.droit_requis("/demandes") == comptes.droit_requis("/demandes/revision/3", "POST") == "admin"


def test_jeton_lisible_et_normalise():
    j = comptes.nouveau_jeton()
    assert re.fullmatch(r"[A-Z2-9]{5}(-[A-Z2-9]{5}){4}", j)
    assert comptes.normaliser_jeton(j.lower().replace("-", " ")) == j


def test_invitation_cree_un_compte_une_seule_fois(client, faux_comptes):
    jeton = _inviter(client, ["bibliotheque", "reviser"])
    invite, r = _creer(jeton)
    assert r.status_code == 303                                      # connecté tout de suite
    k = db.compte_par_identifiant("camille")
    assert k["role"] == "invite" and k["droits"] == ["bibliotheque", "reviser", "claude_demande"]   # « Sur demande »
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


def test_chacun_trie_ses_imports(client, monkeypatch):
    """Ranger : un invité qui a le droit Organiser répond lui-même au tri de ses dépôts (page Import)."""
    assert comptes.droit_requis("/import") == comptes.droit_requis("/import/choix", "POST") == "organiser"
    for nom in ("import_bilan", "import_groupes", "decoupages"):
        monkeypatch.setattr(db, nom, lambda *a, **k: [])
    monkeypatch.setattr(db, "import_fichiers", lambda d, ids=None: [])
    monkeypatch.setattr(db, "sensibles", lambda decision=None, jours=30: [])
    invite, _ = _creer(_inviter(client, ["bibliotheque", "deposer", "organiser"]))
    r = invite.get("/import")
    assert r.status_code == 200 and "inbox kDrive" not in r.text   # l'inbox, c'est l'affaire de l'admin
    # « Tout voir » de l'admin : pas sur ses pages de rangement (tri, matières, dépôt), partout ailleurs oui
    admin = {"role": "admin", "tout_voir": True}
    assert comptes.tout_voir(admin, "/organiser") and comptes.tout_voir(admin, "/a-verifier")
    assert not any(comptes.tout_voir(admin, p) for p in ("/import", "/import/choix", "/organiser/matieres", "/deposer"))
    assert not comptes.tout_voir({"role": "admin", "tout_voir": False}, "/organiser")
    assert not comptes.tout_voir({"role": "invite", "tout_voir": True}, "/organiser")


def test_sans_forfait_claude_la_fiche_attend_l_admin(client, monkeypatch):
    invite, _ = _creer(_inviter(client, ["bibliotheque", "reviser"]))
    crees = []
    monkeypatch.setattr(db, "choisir_documents", lambda *a: [7])
    monkeypatch.setattr(db, "creer_revision", lambda *a, **k: crees.append(a) or 42)
    monkeypatch.setattr(db, "demander_scan", lambda: None)
    r = invite.post("/reviser", data={"genre": "fiche", "niveau_matiere": "BTS1|Réseaux"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/revision/42" and crees   # en file, pas refusée
    assert invite.post("/organiser/matieres/ia", data={}).status_code == 403            # réponse immédiate : refus
    assert invite.get("/reviser").status_code == 200
    assert invite.get("/demandes").status_code == 403                                   # la page est pour l'admin


def test_admin_dit_oui_ou_non(client, monkeypatch):
    decisions = []
    monkeypatch.setattr(db, "demandes_ia", lambda: [
        {"quoi": "revision", "id": 5, "genre": "fiche", "sujet": "VLAN", "consigne": "Fais court", "retouche": None,
         "doc_ids": [7], "niveau": "BTS1", "matiere": "Réseaux", "demande_le": None, "compte_id": 2, "auteur": "camille"}])
    monkeypatch.setattr(db, "titres_documents_tous", lambda ids: {7: "Cours VLAN"})
    monkeypatch.setattr(db, "decider_demande",
                        lambda quoi, i, oui, erreur: decisions.append((quoi, i, oui, erreur)) or {"compte_id": 2, "sujet": "VLAN"})
    notifs = []
    monkeypatch.setattr(db, "notifier_compte", lambda *a: notifs.append(a) or True)
    monkeypatch.setattr(db, "demander_scan", lambda: None)
    page = client.get("/demandes").text
    assert "camille" in page and "VLAN" in page and "Fais court" in page and "Cours VLAN" in page
    client.post("/demandes/revision/5", data={"reponse": "oui"})
    client.post("/demandes/revision/5", data={"reponse": "non", "raison": "trop long"})
    assert decisions[0][:3] == ("revision", 5, True)
    assert decisions[1][:3] == ("revision", 5, False) and "trop long" in decisions[1][3]
    assert len(notifs) == 1 and notifs[0][0] == 2          # seul le refus prévient tout de suite l'invité
    assert client.post("/demandes/document/5", data={"reponse": "oui"}).status_code == 400


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


def test_pronote_relie_par_l_admin_seulement(client):
    """Pronote est celui du site : un invité avec « Agenda » le lit, mais ne peut ni le relier, ni le couper."""
    invite, _ = _creer(_inviter(client, ["agenda"]))
    for chemin, data in (("/agenda/connexion", {"url": "http://10.0.0.1", "identifiant": "x", "mot_de_passe": "y"}),
                         ("/agenda/connexion-qr", {"qr": "{}", "pin": "0000"}),
                         ("/agenda/deconnexion", {}), ("/agenda/synchroniser", {})):
        assert invite.post(chemin, data=data).status_code == 403, chemin
    assert comptes.droit_requis("/agenda") == "agenda"
    assert comptes.droit_requis("/agenda/preparer", "POST") == "agenda"


def test_invite_voit_les_cours_publics_en_lecture_seule(client, monkeypatch):
    invite, _ = _creer(_inviter(client, ["bibliotheque"]))
    monkeypatch.setattr(db, "bibliotheque_partagee", lambda: [
        {"niveau": "BTS1", "matiere": "Réseau", "type": "cours", "n": 3, "auteurs": "admin"}])
    monkeypatch.setattr(db, "liste_partagee", lambda n, m, t: [
        {"id": 9, "titre": "VLAN", "nom": "vlan.pdf", "chapitre": None, "date_doc": None, "auteur": "admin"}])
    page = invite.get("/bibliotheque").text
    assert "Cours partagés" in page and "Lecture seule" in page and "/partages/liste?niveau=BTS1" in page
    assert "/bibliotheque/visibilite" not in page.split("Cours partagés")[1]    # pas de bouton Public / Privé
    liste = invite.get("/partages/liste", params={"niveau": "BTS1", "matiere": "Réseau", "type": "cours"}).text
    assert 'href="/partages/doc/9"' in liste and 'href="/doc/9"' not in liste and "lecture seule" in liste
    assert "Cours partagés" not in client.get("/bibliotheque").text             # l'admin : ses propres cours


def test_invite_se_sert_des_partages_sur_reviser_et_le_chat():
    invite = {"id": 2, "role": "invite"}
    assert comptes.lit_les_partages(invite, "/demander") and comptes.lit_les_partages(invite, "/demander/message/3")
    assert comptes.lit_les_partages(invite, "/reviser/nouvelle") and comptes.lit_les_partages(invite, "/reviser", "POST")
    assert comptes.lit_les_partages(invite, "/campagne", "POST") and comptes.lit_les_partages(invite, "/reviser/lancer", "POST")
    assert not comptes.lit_les_partages(invite, "/reviser") and not comptes.lit_les_partages(invite, "/revision/4/modifier", "POST")
    assert not comptes.lit_les_partages(invite, "/organiser") and not comptes.lit_les_partages(invite, "/bibliotheque")
    assert not comptes.lit_les_partages({"id": 1, "role": "admin"}, "/demander")   # l'admin : comme avant


def test_demander_l_acces_a_tous_les_cours(client, monkeypatch):
    invite, _ = _creer(_inviter(client, ["bibliotheque"]))
    demandes, decisions, notifs = [], [], []
    monkeypatch.setattr(db, "demander_acces_cours", lambda i: demandes.append(i) or True)
    monkeypatch.setattr(db, "decider_acces_cours", lambda i, statut: decisions.append((i, statut)) or True)
    monkeypatch.setattr(db, "notifier_compte", lambda *a: notifs.append(a) or True)
    assert "Demander l'accès à tous les cours" in invite.get("/bibliotheque").text
    invite.post("/acces-cours/demander")
    assert demandes == [2] and notifs[-1][0] is None and notifs[-1][4] == "/demandes"
    assert client.post("/acces-cours/demander").status_code == 400                # pas pour l'admin
    assert invite.post("/demandes/acces/2", data={"reponse": "oui"}).status_code == 403   # l'invité ne décide pas
    monkeypatch.setattr(db, "demandes_ia", lambda: [])
    monkeypatch.setattr(db, "acces_cours", lambda statut: [{"id": 2, "auteur": "camille", "acces_cours_le": None}]
                        if statut == "attente" else [])
    page = client.get("/demandes").text
    assert "Accès à tous les cours · camille" in page and "privés compris" in page
    client.post("/demandes/acces/2", data={"reponse": "oui"})
    client.post("/demandes/acces/2", data={"reponse": "retirer"})
    assert decisions == [(2, "accorde"), (2, None)] and notifs[-1][0] == 2


def test_forfait_claude_a_trois_niveaux(client, monkeypatch):
    assert comptes.avec_forfait(["reviser", "claude"], "demande") == ["reviser", "claude_demande"]
    assert comptes.avec_forfait(["reviser"], "autonomie") == ["reviser", "claude"]
    assert comptes.avec_forfait(["reviser", "claude_demande"], "aucun") == ["reviser"]
    assert comptes.niveau_forfait({"role": "admin", "droits": []}) == "autonomie"
    assert comptes.niveau_forfait({"role": "invite", "droits": ["claude_demande"]}) == "demande"
    assert comptes.niveau_forfait({"role": "invite", "droits": []}) == "aucun"
    page = client.get("/comptes").text
    assert "En autonomie" in page and "Sur demande" in page and 'name="forfait"' in page
    assert 'value="claude"' not in page and 'value="claude_demande"' not in page   # plus des cases à cocher
    client.post("/comptes/inviter", data={"role": "invite", "droits": ["bibliotheque", "reviser"], "forfait": "aucun"})
    invite, _ = _creer(_inviter(client, ["bibliotheque", "reviser"]), "dominique")
    k = db.compte_par_identifiant("dominique")
    client.post(f"/comptes/{k['id']}/modifier", data={"role": "invite", "droits": ["bibliotheque", "reviser"],
                                                      "actif": "1", "forfait": "aucun"})
    assert db.compte_par_identifiant("dominique")["droits"] == ["bibliotheque", "reviser"]
    comptes.oublier()
    r = invite.post("/reviser", data={"genre": "fiche", "niveau_matiere": "BTS1|Réseaux"})
    assert r.status_code == 403 and "forfait Claude" in r.text                      # « Aucun » : refusé tout de suite
    client.post(f"/comptes/{k['id']}/modifier", data={"role": "invite", "droits": ["bibliotheque", "reviser"],
                                                      "actif": "1", "forfait": "autonomie"})
    assert db.compte_par_identifiant("dominique")["droits"] == ["bibliotheque", "reviser", "claude"]


def test_migration_invites_sur_demande(faux_comptes, monkeypatch):
    etat = {}
    monkeypatch.setattr(db, "etat_valeur", lambda cle: etat.get(cle))
    monkeypatch.setattr(db, "etat_ecrire", lambda cle, v: etat.__setitem__(cle, v))
    a = faux_comptes.creer_compte("camille", "h", "invite", ["reviser"])
    b = faux_comptes.creer_compte("dominique", "h", "invite", ["reviser", "claude"])
    comptes.migrer_forfait()
    assert faux_comptes.comptes[a]["droits"] == ["reviser", "claude_demande"]
    assert faux_comptes.comptes[b]["droits"] == ["reviser", "claude"] and faux_comptes.comptes[1]["droits"] == []
    faux_comptes.comptes[a]["droits"] = ["reviser"]                 # l'admin choisit « Aucun » ensuite
    comptes.migrer_forfait()                                         # une seule fois : son choix reste
    assert faux_comptes.comptes[a]["droits"] == ["reviser"]
