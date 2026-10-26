from claunote import index


def test_decouper_aux_paragraphes():
    texte = "\n\n".join(f"Paragraphe {i} : " + "mot " * 50 for i in range(10))
    passages = index.decouper(texte, taille=600, recouvrement=50)
    assert all(len(p) <= 600 for p in passages) and len(passages) >= 4
    assert passages[0].startswith("Paragraphe 0") and "Paragraphe 9" in passages[-1]
    assert index.decouper("") == [] and index.decouper("court") == ["court"]


def test_decouper_un_bloc_sans_ponctuation():
    passages = index.decouper("x" * 3000, taille=1000, recouvrement=100)
    assert all(len(p) <= 1000 for p in passages) and sum(len(p) for p in passages) >= 3000


def test_indexer_avec_et_sans_le_sens(monkeypatch):
    poses = []
    monkeypatch.setattr(index.db, "a_indexer", lambda limite: [{"id": 1, "nom": "a.pdf", "titre": "VLAN",
                                                                "transcription": "Un VLAN.\n\nUn trunk."}])
    monkeypatch.setattr(index.db, "remplacer_passages", lambda i, textes, vecteurs=None: poses.append((i, textes, vecteurs)))
    monkeypatch.setattr(index.db, "passages_existants", lambda i: [])
    monkeypatch.setattr(index, "_refus", {"dimension": False})
    monkeypatch.setattr(index.ressources, "sature", lambda: None)
    monkeypatch.setattr(index.local, "vecteurs", lambda textes: [[0.1] * 3 for _ in textes])
    assert index.indexer() == 1
    assert poses[0][1] == ["Un VLAN.\n\nUn trunk."] and poses[0][2] == [[0.1] * 3]
    monkeypatch.setattr(index.ressources, "sature", lambda: "processeur chargé")  # VM saturée : les mots seulement
    monkeypatch.setattr(index.local, "vecteurs", lambda textes: (_ for _ in ()).throw(AssertionError("pas de calcul")))
    index.indexer()
    assert poses[1][2] is None


def test_vectoriser_par_lots(monkeypatch):
    lots = [[{"id": 1, "texte": "a"}, {"id": 2, "texte": "b"}], []]
    poses = []
    monkeypatch.setattr(index.db, "passages_sans_vecteur", lambda n: lots.pop(0))
    monkeypatch.setattr(index.db, "poser_vecteurs", lambda ids, v: poses.append(ids))
    monkeypatch.setattr(index.ressources, "sature", lambda: None)
    monkeypatch.setattr(index.local, "vecteurs", lambda textes: [[0.0]] * len(textes))
    assert index.vectoriser() == 2 and poses == [[1, 2]]


def test_indexer_reutilise_les_vecteurs(monkeypatch):
    """Document rangé ou reclassé sans changer de texte : pas un seul calcul de sens."""
    doc = {"id": 1, "nom": "a.pdf", "titre": "VLAN", "transcription": "Un VLAN.\n\nUn trunk."}
    texte = "Un VLAN.\n\nUn trunk."
    calculs, poses, marques = [], [], []
    monkeypatch.setattr(index.db, "a_indexer", lambda limite: [doc])
    monkeypatch.setattr(index.db, "marquer_indexe", marques.append)
    monkeypatch.setattr(index.db, "remplacer_passages", lambda i, textes, vecteurs=None: poses.append((textes, vecteurs)))
    monkeypatch.setattr(index, "_refus", {"dimension": False})
    monkeypatch.setattr(index.ressources, "sature", lambda: None)
    monkeypatch.setattr(index.local, "vecteurs", lambda textes: calculs.append(textes) or [[0.2] * 3 for _ in textes])
    monkeypatch.setattr(index.db, "passages_existants", lambda i: [(texte, "[0.1,0.1,0.1]")])
    assert index.indexer() == 1 and marques == [1] and not poses and not calculs
    # Un paragraphe ajouté : seul le nouveau passage est calculé, l'ancien garde son vecteur
    doc["transcription"] = texte + "\n\n" + "Le routage inter-VLAN. " * 52
    index.indexer()
    textes, vecteurs = poses[-1]
    assert len(textes) == 2 and vecteurs[0] == "[0.1,0.1,0.1]" and vecteurs[1] == [0.2] * 3
    assert len(calculs) == 1 and len(calculs[0]) == 1


def test_vectoriser_arrete_apres_un_refus(monkeypatch):
    """Vecteurs d'une autre taille que la base : on ne recalcule pas le même lot à chaque tour."""
    calculs = []
    monkeypatch.setattr(index, "_refus", {"dimension": False})
    monkeypatch.setattr(index.db, "passages_sans_vecteur", lambda n: [{"id": 1, "texte": "a"}])
    monkeypatch.setattr(index.db, "poser_vecteurs", lambda ids, v: (_ for _ in ()).throw(ValueError("dimension")))
    monkeypatch.setattr(index.ressources, "sature", lambda: None)
    monkeypatch.setattr(index.local, "vecteurs", lambda textes: calculs.append(1) or [[0.0]])
    assert index.vectoriser() == 0 and index.vectoriser() == 0 and calculs == [1]


def test_question_deja_posee_sans_calcul(monkeypatch):
    calculs = []
    monkeypatch.setattr(index, "_questions", {})
    monkeypatch.setattr(index.local, "vecteurs", lambda textes: calculs.append(textes) or [[0.5]])
    assert index.vecteur_question("C'est quoi un VLAN ?") == [0.5]
    assert index.vecteur_question("c'est quoi  un vlan ?") == [0.5] and len(calculs) == 1
    monkeypatch.setattr(index.local, "vecteurs", lambda textes: None)   # Ollama absent : rien de gardé
    assert index.vecteur_question("autre") is None and "autre" not in index._questions
