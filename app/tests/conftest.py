"""Sans base de données : comptes et invitations en mémoire, au départ un seul compte (l'admin d'origine,
comme une installation d'avant les comptes)."""
import datetime as dt

import pytest

from claunote import comptes, db


class FauxComptes:
    def __init__(self):
        self.comptes = {1: {"id": 1, "identifiant": "admin", "nom": None, "hash": None, "mdp_defaut": False,
                            "role": "admin", "droits": [], "actif": True, "generation": 0, "tout_voir": False,
                            "expire_le": None, "conditions_version": None, "conditions_le": None, "cree_par": None,
                            "connecte_le": None, "cree_le": dt.datetime.now(dt.timezone.utc),
                            "nb_documents": 0, "nb_revisions": 0}}
        self.invitations = {}
        self.depots = {}
        self.matieres_publiques = set()

    def installer(self, monkeypatch):
        m = monkeypatch.setattr
        m(db, "etat_frais", lambda cle: db.etat_valeur(cle))   # même faux état que etat_valeur dans les tests
        m(db, "compte", lambda i: dict(self.comptes[i]) if i in self.comptes else None)
        m(db, "compte_par_identifiant", lambda ident: next(
            (dict(k) for k in self.comptes.values() if k["identifiant"] == ident.strip().lower()), None))
        m(db, "comptes", lambda: [dict(k) for k in self.comptes.values()])
        m(db, "creer_compte", self.creer_compte)
        m(db, "maj_compte", lambda i, **ch: self.comptes[i].update(ch) if i in self.comptes else None)
        m(db, "renommer_compte", lambda i, ident: self.comptes[i].update(identifiant=ident) or True)
        m(db, "deconnecter_compte", lambda i: self.comptes[i].update(generation=self.comptes[i]["generation"] + 1))
        m(db, "supprimer_compte", lambda i: self.comptes.pop(i, None))
        m(db, "nb_admins_actifs", lambda sauf=None: sum(1 for k in self.comptes.values()
                                                        if k["role"] == "admin" and k["actif"] and k["id"] != sauf))
        m(db, "creer_invitation", self.creer_invitation)
        m(db, "invitation", lambda e: next((dict(i) for i in self.invitations.values() if i["empreinte"] == e
                                            and not i["utilise_le"] and i["expire_le"] > dt.datetime.now(dt.timezone.utc)),
                                           None))
        m(db, "utiliser_invitation", self.utiliser_invitation)
        m(db, "invitations_recentes", lambda limite=20: [dict(i) for i in reversed(self.invitations.values())])
        m(db, "annuler_invitation", lambda i: self.invitations[i].update(expire_le=dt.datetime.now(dt.timezone.utc)))
        m(db, "proprietaires", lambda ids: {i: self.depots.get(i, 1) for i in ids})
        m(db, "compte_du_fichier", lambda kdrive_id: self.depots.get(kdrive_id, 1))
        m(db, "noter_depot", lambda kdrive_id, compte_id: self.depots.update({kdrive_id: compte_id}))
        m(db, "abonnements_de", lambda compte_id: db.abonnements())
        # Public ou privé : le partage ne s'affiche qu'à plusieurs comptes
        m(db, "plusieurs_comptes", lambda: sum(1 for k in self.comptes.values() if k["actif"]) > 1)
        m(db, "matieres_publiques", lambda: set(self.matieres_publiques))
        m(db, "bibliotheque_partagee", lambda: [])
        m(db, "acces_cours", lambda statut: [])
        m(db, "rendre_matiere", lambda n, mat, pub: (self.matieres_publiques.add if pub
                                                     else self.matieres_publiques.discard)((n, mat)))

    def creer_compte(self, identifiant, hash_, role, droits, nom=None, cree_par=None):
        if any(k["identifiant"] == identifiant for k in self.comptes.values()):
            return None
        i = max(self.comptes) + 1
        self.comptes[i] = {**self.comptes[1], "id": i, "identifiant": identifiant, "hash": hash_, "role": role,
                           "droits": list(droits), "nom": nom, "cree_par": cree_par, "mdp_defaut": False,
                           "tout_voir": False, "generation": 0, "conditions_version": None}
        return i

    def creer_invitation(self, empreinte, genre, expire_le, role="invite", droits=None, note=None, cree_par=None,
                         compte_id=None):
        i = len(self.invitations) + 1
        self.invitations[i] = {"id": i, "empreinte": empreinte, "genre": genre, "expire_le": expire_le, "role": role,
                               "droits": droits or [], "note": note, "cree_par": cree_par, "compte_id": compte_id,
                               "utilise_le": None, "utilise_par": None, "cree_le": dt.datetime.now(dt.timezone.utc),
                               "pour": None, "utilise_par_identifiant": None}
        return i

    def utiliser_invitation(self, i, compte_id):
        inv = self.invitations.get(i)
        if not inv or inv["utilise_le"] or inv["expire_le"] <= dt.datetime.now(dt.timezone.utc):
            return False
        inv.update(utilise_le=dt.datetime.now(dt.timezone.utc), utilise_par=compte_id)
        return True


@pytest.fixture(autouse=True)
def faux_comptes(monkeypatch):
    comptes.oublier()
    faux = FauxComptes()
    faux.installer(monkeypatch)
    yield faux
    comptes.oublier()
