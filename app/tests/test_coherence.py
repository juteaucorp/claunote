"""Garde-fou hors sujet : anciens supports en repère, contrôle, consigne de recentrage."""
import datetime as dt
import json

import pytest

from claunote import coherence, db, revision
from claunote.agent import ErreurAgent

ANCIENS = [
    {"id": 1, "genre": "fiche", "sujet": "VLAN et trunk 802.1Q", "contenu": "# VLAN\n## Trunk 802.1Q\n## Natif",
     "cree_le": dt.datetime(2026, 10, 1), "niveau": "BTS1", "matiere": "Réseaux"},
    {"id": 2, "genre": "fiche", "sujet": "Routage OSPF", "contenu": "# OSPF\n## Aires\n## Coût",
     "cree_le": dt.datetime(2026, 10, 5), "niveau": "BTS1", "matiere": "Réseaux"},
    {"id": 3, "genre": "plan", "sujet": "Révisions VLAN", "contenu": "# Plan\n## Lundi : VLAN\n## Mardi : trunk",
     "cree_le": dt.datetime(2026, 9, 1), "niveau": "BTS1", "matiere": "Réseaux"},
]


def test_mots_sans_accents_ni_mots_vides():
    assert coherence.mots("Les Réseaux et la fiche sur les VLANs") == {"reseaux", "vlan"}


def test_plan_d_un_support():
    assert coherence.plan("# A\nblabla\n## B\ntexte", "fiche") == "# A\n## B"
    quiz = json.dumps([{"question": "Qu'est-ce qu'un VLAN ?"}, {"question": "Trunk ?"}])
    assert coherence.plan(quiz, "quiz") == "- Qu'est-ce qu'un VLAN ?\n- Trunk ?"
    assert coherence.plan("pas de titre ici", "fiche") == "pas de titre ici"


def test_anciens_supports_les_plus_proches(monkeypatch):
    vu = {}
    monkeypatch.setattr(db, "supports_de_la_matiere",
                        lambda matiere, sauf, genres: vu.update(m=matiere, sauf=sauf) or ANCIENS)
    anciens = coherence.anciens_supports({"id": 9, "sujet": "Fiche sur les VLAN", "matiere": "Réseaux"})
    assert [a["id"] for a in anciens] == [1, 3]   # OSPF n'a rien à voir
    assert vu == {"m": "Réseaux", "sauf": 9}


def test_bloc_de_reperes():
    extraits = [{"titre": "TP VLAN Cisco", "niveau": "BTS1", "texte": "switchport mode trunk"}]
    bloc = coherence.bloc_reperes(ANCIENS[:1], extraits)
    assert coherence.MARQUE in bloc and "VLAN et trunk" in bloc and "switchport mode trunk" in bloc
    assert coherence.bloc_reperes([], []) == ""


def test_preparer_ne_bloque_jamais(monkeypatch):
    def panne(*a, **k):
        raise RuntimeError("base injoignable")
    monkeypatch.setattr(db, "supports_de_la_matiere", panne)
    monkeypatch.setattr(db, "chercher_passages", panne)
    assert coherence.preparer({"id": 1, "sujet": "VLAN", "matiere": "Réseaux"}) == ("", [])


def test_autres_cours_sans_ceux_deja_choisis(monkeypatch):
    from claunote import index
    monkeypatch.setattr(index, "vecteur_question", lambda q: None)
    monkeypatch.setattr(db, "chercher_passages", lambda req, v, matiere=None, limite=8: [
        {"document_id": 3, "texte": "a"}, {"document_id": 7, "texte": "b"}])
    assert [p["document_id"] for p in coherence.autres_cours(
        {"sujet": "VLAN", "matiere": "Réseaux", "doc_ids": [3]})] == [7]
    assert coherence.autres_cours({"sujet": "VLAN", "matiere": None, "doc_ids": []}) == []


@pytest.mark.parametrize("reponse,attendu", [
    ('{"dans_le_sujet": true, "ecarts": []}', {"ok": True, "ecarts": []}),
    ('```json\n{"dans_le_sujet": false, "ecarts": ["partie OSPF : routage, pas VLAN"]}\n```',
     {"ok": False, "ecarts": ["partie OSPF : routage, pas VLAN"]}),
    ('{"dans_le_sujet": false, "ecarts": []}', {"ok": True, "ecarts": []}),   # rien à corriger : on garde
])
def test_lire_controle(reponse, attendu):
    assert coherence.lire_controle(reponse) == attendu


def test_lire_controle_illisible():
    with pytest.raises(ErreurAgent):
        coherence.lire_controle("je ne sais pas")


def test_controle_par_haiku_et_sans_blocage(monkeypatch):
    vu = {}

    def faux(tache, prompt, cwd, lire, entree, timeout):
        vu.update(tache=tache, entree=entree)
        return lire('{"dans_le_sujet": false, "ecarts": ["OSPF hors sujet"]}'), "haiku"
    monkeypatch.setattr(coherence, "demander", faux)
    rev = {"genre": "fiche", "sujet": "VLAN", "matiere": "Réseaux", "niveau": "BTS1"}
    c = coherence.controler(rev, "# VLAN\n## OSPF", [{"titre": "TP VLAN"}], ANCIENS[:1])
    assert c == {"ok": False, "ecarts": ["OSPF hors sujet"], "anciens": [1]} and vu["tache"] == "coherence"
    assert "# VLAN\n## OSPF" in vu["entree"] and "VLAN et trunk" in vu["entree"] and "TP VLAN" in vu["entree"]

    def panne(*a, **k):
        raise ErreurAgent("limite")
    monkeypatch.setattr(coherence, "demander", panne)
    assert coherence.controler(rev, "# VLAN\n## A", [], []) is None


def test_generer_avec_reperes_et_recentrage(monkeypatch):
    vu = {}

    def faux(genre, prompt, cwd, lire, outils, entree, timeout):
        vu.update(prompt=prompt, entree=entree)
        return lire("# VLAN\n" + "x" * 100), "opus"
    monkeypatch.setattr(revision, "demander", faux)
    docs = [{"id": 3, "titre": "TP VLAN", "nom": "a.pdf", "niveau": "BTS1", "matiere": "Réseaux", "type": "TP",
             "transcription": "Les VLAN séparent le réseau."}]
    revision.generer("fiche", "VLAN", docs, reperes_perimetre="\n\n" + coherence.MARQUE + "\n\nancienne",
                     recentrer=coherence.consigne_recentrage({"ecarts": ["OSPF hors sujet"]}))
    assert coherence.CONSIGNE_REPERES in vu["prompt"] and "OSPF hors sujet" in vu["prompt"]
    assert vu["entree"].endswith("ancienne") and "Les VLAN séparent" in vu["entree"]
    revision.generer("fiche", "VLAN", docs)
    assert coherence.MARQUE not in vu["prompt"] and coherence.MARQUE not in vu["entree"]
