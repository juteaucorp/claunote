"""Demandes IA des invités sans forfait et cours publics dans leur bibliothèque, sur une vraie base PostgreSQL.

Lancé seulement si CLAUNOTE_TEST_DB donne une base JETABLE (voir test_base_comptes.py).
"""
import pytest

from claunote import db, demandes_ia
from tests.test_base_comptes import URL, base  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(not URL, reason="CLAUNOTE_TEST_DB non défini (base PostgreSQL jetable)")


def _revision(**champs):
    cols = {"genre": "fiche", "sujet": "VLAN", "doc_ids": [], **champs}
    with db.connect() as c:
        return c.execute(f"INSERT INTO revisions ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                         "RETURNING id", list(cols.values())).fetchone()["id"]


def test_demande_oui_non(base):
    camille = db.creer_compte("camille", "h", "invite", ["reviser"])
    with db.pour_compte(camille):
        fiche, quiz = _revision(consigne="Fais court"), _revision(genre="quiz", sujet="DHCP")
        pret = _revision(sujet="IP", statut="pret", contenu="# IP")
        assert db.demander_retouche(pret, "ajoute un schéma")
        k = db.creer_campagne("BTS blanc", "", "2026-12-01", "2026-12-05")
        dans_k = _revision(sujet="NAT", campagne_id=k)
    for rev in (fiche, quiz, pret):
        assert db.mettre_en_demande("revisions", rev)
    assert not db.mettre_en_demande("revisions", fiche)           # une seule notification par demande
    assert db.mettre_en_demande("campagnes", k)
    demandes = db.demandes_ia()
    assert {(d["quoi"], d["id"]) for d in demandes} == {("revision", fiche), ("revision", quiz), ("revision", pret),
                                                        ("campagne", k)}   # ses supports : avec la campagne
    assert db.nb_demandes_ia() == 4 and {d["auteur"] for d in demandes} == {"camille"}
    # Un invité ne décide pas : ses lignes restent les siennes, la page /demandes est réservée à l'admin
    assert db.decider_demande("revision", fiche, True, "")["compte_id"] == camille
    assert db.decider_demande("revision", fiche, False, "x") is None          # déjà décidée
    assert db.decider_demande("revision", quiz, False, "Refusé")
    assert db.decider_demande("revision", pret, False, "Refusé")
    assert db.decider_demande("campagne", k, True, "")
    with db.pour_compte(camille):
        assert db.revision(fiche)["validation"] == "acceptee" and db.revision(fiche)["statut"] == "en_attente"
        r = db.revision(quiz)
        assert r["statut"] == "erreur" and r["erreur"] == "Refusé"
        r = db.revision(pret)                                    # modification refusée : la fiche d'avant reste
        assert r["statut"] == "pret" and r["contenu"] == "# IP" and r["retouche"] is None
        assert db.revision(dans_k)["validation"] == "acceptee"
        db.relancer_revision(quiz)                               # réessayer : une nouvelle demande
        assert db.revision(quiz)["validation"] is None
    assert db.nb_demandes_ia() == 0
    # le worker les voit toutes en attente ; c'est lui qui laisse de côté celles sans accord
    assert {r["id"] for r in db.revisions_en_attente()} == {fiche, quiz, dans_k}


def test_cours_publics_dans_la_bibliotheque_d_un_invite(base):
    camille = db.creer_compte("camille", "h", "invite", ["bibliotheque"])
    for kd, nom, matiere in ((100, "vlan.pdf", "Réseau"), (101, "secret.pdf", "Maths")):
        d = db.creer(kd, nom)                                    # sans compte : les cours de l'admin
        db.maj(d, statut="classe", niveau="BTS1", matiere=matiere, type="cours", titre=nom[:-4])
    with db.pour_compte(1):
        db.rendre_matiere("BTS1", "Réseau", True)
        assert db.bibliotheque_partagee() == []                  # l'admin ne voit pas les siens en double
    with db.pour_compte(camille):
        assert db.bibliotheque() == []                           # sa bibliothèque à lui reste vide
        rangs = db.bibliotheque_partagee()
        assert [(r["matiere"], r["n"], r["auteurs"]) for r in rangs] == [("Réseau", 1, "admin")]
        docs = db.liste_partagee("BTS1", "Réseau", "cours")
        assert [d["titre"] for d in docs] == ["vlan"] and db.liste_partagee("BTS1", "Maths", "cours") == []
        with db.connect() as c:                                  # lecture seule : jamais modifiable
            assert c.execute("UPDATE documents SET titre = 'x' RETURNING id").fetchall() == []


def test_libelle_et_attente():
    assert demandes_ia.libelle("examen") == "Épreuve blanche"
    rev = {"statut": "en_attente", "validation": "attente"}
    assert demandes_ia.attend_admin(rev, peut_claude=False) and not demandes_ia.attend_admin(rev, peut_claude=True)
    assert not demandes_ia.attend_admin({**rev, "validation": "acceptee"}, peut_claude=False)


def _cours_admin():
    ids = {}
    for kd, nom, matiere in ((100, "vlan.pdf", "Réseau"), (101, "secret.pdf", "Maths")):
        d = db.creer(kd, nom)
        db.maj(d, statut="classe", niveau="BTS1", matiere=matiere, type="cours", titre=nom[:-4])
        with db.connect() as c:
            c.execute("INSERT INTO passages (document_id, rang, texte) VALUES (%s, 0, %s)",
                      (d, f"cours sur {nom[:-4]} trunk"))
        ids[nom] = d
    with db.pour_compte(1):
        db.rendre_matiere("BTS1", "Réseau", True)
    return ids


def test_cours_partages_dans_le_chat_et_les_fiches(base):
    ids = _cours_admin()
    camille = db.creer_compte("camille", "h", "invite", ["bibliotheque", "reviser"])
    with db.pour_compte(camille):
        assert db.chercher_passages("trunk") == []               # hors des pages qui lisent les partages
        with db.partages():
            assert [p["titre"] for p in db.chercher_passages("trunk")] == ["vlan"]   # public seulement
            assert [d["id"] for d in db.documents([ids["vlan.pdf"], ids["secret.pdf"]])] == [ids["vlan.pdf"]]
            rev = db.creer_revision("fiche", "VLAN", [ids["vlan.pdf"]])
            assert db.revision(rev)["matiere"] == "Réseau"        # la fiche prend la matière du cours partagé
            with db.connect() as c:                               # jamais modifiable
                assert c.execute("UPDATE passages SET texte = 'x' RETURNING id").fetchall() == []
        assert db.revision(rev)["compte_id"] == camille


def test_acces_a_tous_les_cours(base):
    ids = _cours_admin()
    camille = db.creer_compte("camille", "h", "invite", ["bibliotheque"])
    assert db.demander_acces_cours(camille) and not db.demander_acces_cours(camille)
    assert not db.demander_acces_cours(1)                         # l'admin n'en a pas besoin
    assert [c["id"] for c in db.acces_cours("attente")] == [camille] and db.nb_demandes_ia() == 1
    with db.pour_compte(camille), db.partages():                  # en attente : toujours les publics seulement
        assert [p["titre"] for p in db.chercher_passages("trunk")] == ["vlan"]
    assert db.decider_acces_cours(camille, "accorde")
    with db.pour_compte(camille):
        assert db.bibliotheque() == [] and db.chercher_passages("secret") == []   # ailleurs : ses données
        with db.partages():
            assert sorted(p["titre"] for p in db.chercher_passages("trunk")) == ["secret", "vlan"]
            assert {r["matiere"] for r in db.bibliotheque_partagee()} == {"Maths", "Réseau"}
            with db.connect() as c:
                assert c.execute("UPDATE documents SET titre = 'x' RETURNING id").fetchall() == []
    assert db.decider_acces_cours(camille, None)                  # retiré
    with db.pour_compte(camille), db.partages():
        assert [p["titre"] for p in db.chercher_passages("trunk")] == ["vlan"]
