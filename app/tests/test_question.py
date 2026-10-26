import json

import pytest

from claunote import question
from claunote.agent import ErreurAgent

P1 = {"id": 11, "document_id": 7, "rang": 0, "nom": "vlan.pdf", "titre": "Les VLAN", "niveau": "BTS1",
      "matiere": "Réseaux", "type": "Cours", "statut": "classe",
      "texte": "Un VLAN est un réseau virtuel. " * 40, "extrait": "## Définition\n- un **VLAN** est un réseau virtuel"}
P2 = {**P1, "id": 12, "rang": 3, "texte": "Le trunk 802.1Q transporte plusieurs VLAN.", "extrait": "le **trunk**"}
P3 = {**P1, "id": 13, "document_id": 9, "titre": "Convocation BTS blanc", "statut": "hors_cours", "niveau": None,
      "matiere": None, "type": None, "texte": "Convocation le 3 novembre à 8 h.", "extrait": "**convocation**"}


def test_requete_ou():
    assert question.requete("C'est quoi un VLAN ? Et un VLAN natif ?") == "est | quoi | un | vlan | et | natif"
    assert question.requete("?!") == ""
    assert question.requete("a & b | c") == ""  # pas d'opérateur tsquery venu de la question


def test_titre():
    assert question.titre("  une   question ") == "une question"
    assert len(question.titre("x" * 200)) == 68


def test_par_document():
    docs = question.par_document([P1, P3, P2])
    assert [(d["repere"], d["id"], len(d["passages"])) for d in docs] == [("D1", 7, 2), ("D2", 9, 1)]


def test_reponse_locale():
    contenu, reperes = question.reponse_locale("vlan", [P1, P2, P3], " de Réseaux (BTS1)")
    assert reperes == {"D1": 7, "D2": 9}
    assert "tes documents de Réseaux (BTS1)" in contenu and "**[D1] Les VLAN (BTS1, Réseaux, Cours)**" in contenu
    assert "> … Définition un **VLAN** est un réseau virtuel …" in contenu  # titres et listes retirés
    assert "[D2] Convocation BTS blanc (rangé hors cours)" in contenu  # tous les documents servent


def test_reponse_locale_sans_resultat():
    contenu, reperes = question.reponse_locale("zorglub", [])
    assert reperes == {} and "rien trouvé" in contenu and "IA" in contenu


def test_entree_ia():
    historique = [{"role": "eleve", "contenu": "Qu'est-ce qu'un VLAN ?"}, {"role": "ia", "contenu": "Un réseau virtuel [D1]."}]
    texte, reperes = question.entree("Et le trunk ?", historique, [P1, P2], agenda="- jeu. 29/10 : évaluation de Réseaux",
                                     limite=500, brouillon="Le trunk relie deux switchs.")
    assert reperes == {"D1": 7}
    assert texte.index("Élève : Qu'est-ce qu'un VLAN ?") < texte.index("Toi : Un réseau virtuel") \
        < texte.index("Élève (question à laquelle répondre) : Et le trunk ?") < texte.index("Agenda de l'élève") \
        < texte.index("Brouillon de l'assistant local") < texte.index("[D1] Les VLAN")
    assert len(texte) < 1_200  # passages tronqués à la limite
    assert "(aucun document trouvé" in question.entree("Bonjour", [], [])[0]


def test_repondre_local(monkeypatch):
    from claunote import local
    vu = {}

    def ollama(systeme, texte, json_seul, timeout):
        vu.update(systeme=systeme, texte=texte, json=json_seul)
        return json.dumps({"reponse": "Un trunk transporte plusieurs VLAN [D1].", "confiance": 0.9})

    monkeypatch.setattr(local, "ollama", ollama)
    r = question.repondre_local("Et le trunk ?", [], [P1, P2], "", "de Réseaux (BTS1)")
    assert r["reponse"].startswith("Un trunk") and not r["aide"] and r["reperes"] == {"D1": 7} and vu["json"]
    assert len(vu["texte"]) < question.TEXTE_LOCAL + 1000
    monkeypatch.setattr(local, "ollama", lambda *a, **k: json.dumps(
        {"reponse": "Je ne sais pas.", "confiance": 0.9, "besoin_aide": True, "question_pour_aide": "Date du BTS ?"}))
    r = question.repondre_local("Quand est le BTS ?", [], [], "")
    assert r["aide"] and r["demande"] == "Date du BTS ?"
    monkeypatch.setattr(local, "ollama", lambda *a, **k: json.dumps({"reponse": "Bof.", "confiance": 0.3}))
    assert question.repondre_local("x", [], [])["aide"]  # confiance basse : il demande de l'aide
    monkeypatch.setattr(local, "ollama", lambda *a, **k: "pas du json")
    with pytest.raises(ErreurAgent):
        question.repondre_local("x", [], [])


def test_repondre_ia(monkeypatch):
    vu = {}

    def faux_demander(tache, prompt, cwd, lire, outils, entree, timeout):
        vu.update(tache=tache, prompt=prompt, outils=outils, entree=entree)
        return lire("Un trunk transporte plusieurs VLAN [D1] [W1].\n\n## Sources web\n- [W1] Cisco — https://cisco.com/trunk"), "sonnet"

    monkeypatch.setattr(question, "demander", faux_demander)
    contenu, reperes, web, modele, verdict = question.repondre_ia("Et le trunk ?", [], [P1], "de Réseaux (BTS1)")
    assert contenu == "Un trunk transporte plusieurs VLAN [D1] [W1]." and verdict is None
    assert web == [{"repere": "W1", "titre": "Cisco", "url": "https://cisco.com/trunk"}]
    assert reperes == {"D1": 7} and modele == "sonnet"
    assert vu["tache"] == "question" and "WebSearch" in vu["outils"] and "de Réseaux (BTS1)" in vu["prompt"]
    assert "brouillon" not in vu["prompt"].lower()


def test_claude_verifie_le_brouillon_local(monkeypatch):
    vu = {}

    def faux_demander(tache, prompt, cwd, lire, outils, entree, timeout):
        vu.update(prompt=prompt, entree=entree)
        return lire("Le trunk transporte plusieurs VLAN [D1].\n\nBROUILLON: corrigé"), "sonnet"

    monkeypatch.setattr(question, "demander", faux_demander)
    brouillon = {"reponse": "Le trunk relie deux routeurs.", "demande": "Est-ce bien entre switchs ?"}
    contenu, _, _, _, verdict = question.repondre_ia("Et le trunk ?", [], [P1], brouillon=brouillon)
    assert contenu == "Le trunk transporte plusieurs VLAN [D1]." and verdict is False
    assert "Est-ce bien entre switchs ?" in vu["prompt"] and "Le trunk relie deux routeurs." in vu["entree"]
