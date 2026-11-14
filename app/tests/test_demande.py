import datetime as dt

from claunote import demande, pronote

CHOIX = [{"niveau": "BTS1", "matiere": "CEJM", "n": 12}, {"niveau": "BTS2", "matiere": "CEJM", "n": 9},
         {"niveau": "BTS1", "matiere": "Mathématiques", "n": 8}, {"niveau": "BTS2", "matiere": "Mathématiques", "n": 4},
         {"niveau": "BTS2", "matiere": "Réseaux", "n": 20}, {"niveau": "Terminale", "matiere": "Anglais", "n": 5},
         {"niveau": "BTS1", "matiere": "Culture générale et expression", "n": 3}]
SAMEDI = dt.date(2026, 11, 14)


def lire(texte, **k):
    return demande.comprendre(texte, CHOIX, aujourdhui=SAMEDI, **k)


def test_demande_complete():
    c = lire("Fais-moi une fiche de CEJM sur le contrat et la responsabilité civile pour jeudi")
    assert c["genres"] == ["fiche"] and c["matieres"] == [{"niveau": "BTS2", "matiere": "CEJM"}]
    assert c["sujet"] == "contrat et la responsabilité civile"
    assert c["date"] == dt.date(2026, 11, 19) and c["source_date"] == "texte"
    assert c["incertain"] == {"genres": False, "matieres": False}
    assert demande.phrase(c) == "Fiche en CEJM (BTS2) sur « contrat et la responsabilité civile », pour le jeudi 19/11"


def test_supports_et_dates():
    c = lire("quiz et flashcards en maths bts1 sur les suites, contrôle le 20/11")
    assert c["genres"] == ["quiz", "cartes"] and c["matieres"] == [{"niveau": "BTS1", "matiere": "Mathématiques"}]
    assert c["sujet"] == "suites" and c["date"] == dt.date(2026, 11, 20)
    assert lire("épreuve blanche de réseaux demain")["genres"] == ["examen"]
    assert lire("épreuve blanche de réseaux demain")["date"] == dt.date(2026, 11, 15)
    assert lire("fiche anglais dans 3 jours")["date"] == dt.date(2026, 11, 17)
    assert lire("fiche anglais le 5 janvier")["date"] == dt.date(2027, 1, 5)
    assert lire("fiche de reseaux")["date"] is None and lire("fiche de reseaux")["sujet"] == ""


def test_matieres_sigles_fautes_et_annees():
    assert lire("fiche de mathematique")["matieres"][0]["matiere"] == "Mathématiques"
    assert lire("fiche en CGE")["matieres"][0]["matiere"] == "Culture générale et expression"
    c = lire("fiche de CEJM sur toutes les années")
    assert c["matieres"] == [{"niveau": "BTS2", "matiere": "CEJM"}, {"niveau": "BTS1", "matiere": "CEJM"}]


def test_plusieurs_matieres_et_contexte():
    c = lire("Prépare-moi le BTS blanc de la semaine prochaine en maths, CEJM et réseaux")
    assert [m["matiere"] for m in c["matieres"]] == ["Mathématiques", "CEJM", "Réseaux"]
    assert c["titre"] == "BTS blanc" and c["date"] == dt.date(2026, 11, 16) and c["fin"] == dt.date(2026, 11, 20)
    assert c["incertain"]["genres"] and c["genres"] == ["fiche"] and c["sujet"] == ""


def test_matiere_devinee_ou_demandee():
    docs = [{"matiere": "Réseaux", "statut": "classe"}] * 3 + [{"matiere": "CEJM", "statut": "classe"}]
    c = lire("je comprends rien aux masques de sous-réseau", chercher=lambda q: docs)
    assert c["matieres"] == [{"niveau": "BTS2", "matiere": "Réseaux"}] and c["sujet"] == "masques de sous-réseau"
    assert c["incertain"] == {"genres": True, "matieres": False} and "Réseaux" in c["pourquoi"][0]
    c = lire("fiche sur la mondialisation", chercher=lambda q: [])
    assert c["matieres"] == [] and c["incertain"]["matieres"] and c["sujet"] == "mondialisation"
    assert "?" in demande.phrase(c)


def test_date_du_controle_pronote():
    evs = [{"matiere": "CULTURE ECONOMIQUE JURIDIQUE ET MANAGERIALE", "le": dt.datetime(2026, 11, 24, 8, tzinfo=pronote.PARIS)}]
    c = lire("quiz de cejm", evaluations=evs)
    assert c["date"] == dt.date(2026, 11, 24) and c["source_date"] == "pronote"
