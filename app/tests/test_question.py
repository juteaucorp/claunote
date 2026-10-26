from claunote import question

COURS = {"id": 7, "nom": "vlan.pdf", "titre": "Les VLAN", "niveau": "BTS1", "matiere": "Réseaux", "type": "Cours",
         "resume": "Segmenter un réseau.", "extrait": "## Définition\n- un **VLAN** est un réseau virtuel",
         "transcription": "# VLAN\n" + "x" * 20_000}


def test_requete_ou():
    assert question.requete("C'est quoi un VLAN ? Et un VLAN natif ?") == "est | quoi | un | vlan | et | natif"
    assert question.requete("?!") == ""
    assert question.requete("a & b | c") == ""  # pas d'opérateur tsquery venu de la question


def test_titre():
    assert question.titre("  une   question ") == "une question"
    assert len(question.titre("x" * 200)) == 68


def test_reponse_locale():
    contenu, reperes = question.reponse_locale("vlan", [COURS], " de Réseaux (BTS1)")
    assert reperes == {"D1": 7}
    assert "tes cours de Réseaux (BTS1)" in contenu and "**[D1] Les VLAN**" in contenu
    assert "> … Définition un **VLAN** est un réseau virtuel …" in contenu  # titres et listes du cours retirés


def test_reponse_locale_sans_resultat():
    contenu, reperes = question.reponse_locale("zorglub", [])
    assert reperes == {} and "rien trouvé" in contenu and "IA" in contenu


def test_entree_ia():
    historique = [{"role": "eleve", "contenu": "Qu'est-ce qu'un VLAN ?"}, {"role": "ia", "contenu": "Un réseau virtuel [D1]."}]
    texte, reperes = question.entree("Et le trunk ?", historique, [COURS])
    assert reperes == {"D1": 7}
    assert texte.index("Élève : Qu'est-ce qu'un VLAN ?") < texte.index("Toi : Un réseau virtuel") \
        < texte.index("Élève (question à laquelle répondre) : Et le trunk ?") < texte.index("[D1] Les VLAN")
    assert len(texte) < 13_000  # cours tronqué
    assert "(aucun cours trouvé" in question.entree("Bonjour", [], [])[0]


def test_repondre_ia(monkeypatch):
    vu = {}

    def faux_demander(tache, prompt, cwd, lire, outils, entree, timeout):
        vu.update(tache=tache, prompt=prompt, outils=outils, entree=entree)
        return lire("Un trunk transporte plusieurs VLAN [D1] [W1].\n\n## Sources web\n- [W1] Cisco — https://cisco.com/trunk"), "sonnet"

    monkeypatch.setattr(question, "demander", faux_demander)
    contenu, reperes, web, modele = question.repondre_ia("Et le trunk ?", [], [COURS], "de Réseaux (BTS1)")
    assert contenu == "Un trunk transporte plusieurs VLAN [D1] [W1]."
    assert web == [{"repere": "W1", "titre": "Cisco", "url": "https://cisco.com/trunk"}]
    assert reperes == {"D1": 7} and modele == "sonnet"
    assert vu["tache"] == "question" and "WebSearch" in vu["outils"] and "de Réseaux (BTS1)" in vu["prompt"]
