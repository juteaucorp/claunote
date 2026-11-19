"""Public ou privé : boutons sur ses propres éléments, pages « Partagés » en lecture seule.
La séparation elle-même (row-level security) est testée sur une vraie base : test_base_comptes.py."""
from datetime import date

from claunote import db
from tests.test_site import DOC, anonyme, client  # noqa: F401  (fixtures du site)

AUTRE = {**DOC, "id": 9, "compte_id": 2, "auteur": "camille", "public": True}


def _a_plusieurs(faux_comptes):
    faux_comptes.creer_compte("camille", "h", "invite", ["bibliotheque", "reviser"])


def test_seul_sur_l_environnement_rien_ne_change(client):
    assert "Rendre public" not in client.get("/doc/1").text
    r = client.get("/bibliotheque")
    assert "/bibliotheque/visibilite" not in r.text and "/partages" not in r.text


def test_matiere_publique_depuis_la_bibliotheque(client, faux_comptes):
    _a_plusieurs(faux_comptes)
    r = client.get("/bibliotheque")
    assert "/bibliotheque/visibilite" in r.text and "🔒" in r.text and 'href="/partages"' in r.text
    r = client.post("/bibliotheque/visibilite", data={"niveau": "BTS1", "matiere": "Réseaux", "valeur": "public"},
                    follow_redirects=False)
    assert r.status_code == 303 and faux_comptes.matieres_publiques == {("BTS1", "Réseaux")}
    assert "Rendre privée" in client.get("/bibliotheque").text
    # Le cours suit sa matière tant qu'on ne choisit rien pour lui
    r = client.get("/doc/1")
    assert "🌐 Public" in r.text and "comme la matière" in r.text and "Rendre privé" in r.text
    assert client.post("/bibliotheque/visibilite", data={"niveau": "BTS1", "matiere": "Réseaux", "valeur": "x"}
                       ).status_code == 400


def test_tout_voir_ne_montre_pas_les_boutons_des_matieres(client, faux_comptes):
    _a_plusieurs(faux_comptes)
    faux_comptes.comptes[1]["tout_voir"] = True
    assert "/bibliotheque/visibilite" not in client.get("/bibliotheque").text


def test_seul_le_proprietaire_choisit(client, faux_comptes, monkeypatch):
    _a_plusieurs(faux_comptes)
    appels = []
    monkeypatch.setattr(db, "changer_visibilite", lambda t, i, v: appels.append((t, i, v)) or i == 1)
    r = client.post("/doc/1/visibilite", data={"valeur": "public"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/doc/1"
    assert client.post("/doc/1/visibilite", data={"valeur": "matiere"}, follow_redirects=False).status_code == 303
    assert appels == [("documents", 1, True), ("documents", 1, None)]
    assert client.post("/doc/2/visibilite", data={"valeur": "prive"}).status_code == 404   # pas à ce compte
    assert client.post("/revision/1/visibilite", data={"valeur": "matiere"}).status_code == 400
    assert client.post("/campagne/1/visibilite", data={"valeur": "public"}, follow_redirects=False).status_code == 303
    # Cours d'un autre compte vu avec « Tout voir » : pas de bouton
    monkeypatch.setattr(db, "document", lambda i: {**DOC, "compte_id": 2})
    assert "Rendre public" not in client.get("/doc/1").text


def test_pages_partages(client, faux_comptes, monkeypatch):
    _a_plusieurs(faux_comptes)
    fiche = {"id": 4, "genre": "fiche", "sujet": "VLAN", "statut": "pret", "niveau": "BTS1", "matiere": "Réseaux",
             "auteur": "camille", "reperes": {"D1": 9, "D2": 10}, "sources_web": [], "campagne_id": None,
             "contenu": "## L'essentiel\nUn VLAN [D1], privé [D2]", "cree_le": None}
    quiz = {**fiche, "id": 5, "genre": "quiz", "contenu": '[{"question": "Q?", "choix": ["a", "b"], "reponse": 1, '
            '"explication": "car", "source": "D1"}]'}
    cartes = {**fiche, "id": 6, "genre": "cartes", "contenu": ""}
    revs = {4: fiche, 5: quiz, 6: cartes}
    monkeypatch.setattr(db, "partages_documents", lambda: [AUTRE])
    monkeypatch.setattr(db, "partages_revisions", lambda: [fiche])
    monkeypatch.setattr(db, "partages_campagnes", lambda: [{"id": 3, "titre": "BTS blanc", "auteur": "camille",
                                                            "debut": date(2026, 12, 1), "fin": date(2026, 12, 5)}])
    monkeypatch.setattr(db, "document_partage", lambda i: AUTRE if i == 9 else None)
    monkeypatch.setattr(db, "revision_partagee", lambda i: revs.get(i))
    monkeypatch.setattr(db, "documents_partages", lambda ids: [AUTRE] if 9 in ids else [])
    monkeypatch.setattr(db, "cartes_partagees", lambda i: [{"recto": "VLAN ?", "verso": "réseau virtuel"}])
    monkeypatch.setattr(db, "campagne_partagee", lambda i: ({"id": 3, "titre": "BTS blanc", "auteur": "camille",
                                                             "debut": date(2026, 12, 1), "fin": date(2026, 12, 5),
                                                             "epreuves": [{"matiere": "Réseaux", "date": "2026-12-03"}]},
                                                            [fiche]) if i == 3 else (None, []))
    r = client.get("/partages")
    assert r.status_code == 200 and "camille" in r.text and "/partages/doc/9" in r.text
    assert "/partages/revision/4" in r.text and "/partages/campagne/3" in r.text
    r = client.get("/partages/doc/9")
    assert "Partagé par" in r.text and "<script>alert" not in r.text and "Modifier le classement" not in r.text
    assert client.get("/partages/doc/1").status_code == 404
    r = client.get("/partages/revision/4")
    # Repère vers un cours public : lien dans « Partagés » ; vers un cours resté privé : pas de lien
    assert '<a href="/partages/doc/9">[D1]</a>' in r.text and "/doc/10" not in r.text and "[D2]" in r.text
    assert "Renommer" not in r.text and "/revision/4/visibilite" not in r.text
    assert "B. b" in client.get("/partages/revision/5").text
    assert "VLAN ?" in client.get("/partages/revision/6").text
    assert client.get("/partages/revision/99").status_code == 404
    r = client.get("/partages/campagne/3")
    assert "BTS blanc" in r.text and "/partages/revision/4" in r.text and "03/12" in r.text
    assert client.get("/partages/campagne/8").status_code == 404


def test_partages_demande_le_droit_bibliotheque():
    from claunote import comptes
    assert comptes.droit_requis("/partages") == "bibliotheque"
    assert comptes.droit_requis("/partages/revision/4") == "bibliotheque"
    assert comptes.droit_requis("/doc/1/visibilite", "POST") == "bibliotheque"
