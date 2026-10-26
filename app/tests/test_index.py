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
