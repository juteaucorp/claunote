import base64
import datetime as dt
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from claunote import db, notifications as nt

PARIS = nt.PARIS
LUNDI = dt.datetime(2026, 10, 26, 18, 30, tzinfo=PARIS)


class Base:
    """Fausse base : état, file de notifications, abonnements, évaluations."""

    def __init__(self):
        self.etat, self.file, self.abos = {}, {}, []
        self.evals, self.dues, self.nouveaux, self.campagne = [], 0, None, None

    def notifier(self, cle, titre, corps="", url="/", statut="attente"):
        if cle in self.file:
            return False
        if statut == "attente" and any(n["titre"] == titre and n["corps"] == corps
                                       and n["statut"] in ("attente", "envoi", "envoye") for n in self.file.values()):
            statut = "ignore"  # comme la base : même texte récent = pas renvoyé
        self.file[cle] = {"id": len(self.file) + 1, "cle": cle, "titre": titre, "corps": corps, "url": url,
                          "statut": statut}
        return True

    def reserver(self, seulement=None):
        pris = [n for n in self.file.values() if n["statut"] == "attente"
                and (seulement is None or n["cle"].split(":")[0] + ":" in seulement)]
        for n in pris:
            n["statut"] = "envoi"
        return pris

    def maj(self, i, statut, erreur=None):
        for n in self.file.values():
            if n["id"] == i:
                n.update(statut=statut, erreur=erreur)


@pytest.fixture
def base(monkeypatch):
    b = Base()
    monkeypatch.setattr(db, "etat_valeur", lambda k: b.etat.get(k))
    monkeypatch.setattr(db, "etat_ecrire", lambda k, v: b.etat.update({k: v}))
    monkeypatch.setattr(db, "etat_initialiser", lambda k, v: b.etat.setdefault(k, v))
    monkeypatch.setattr(db, "notifier", b.notifier)
    monkeypatch.setattr(db, "notification_connue", lambda cle: cle in b.file)
    monkeypatch.setattr(db, "reserver_notifications", b.reserver)
    monkeypatch.setattr(db, "maj_notification", b.maj)
    monkeypatch.setattr(db, "abonnements", lambda: list(b.abos))
    monkeypatch.setattr(db, "retirer_abonnement", lambda e: b.abos.remove(next(a for a in b.abos if a["endpoint"] == e)))
    monkeypatch.setattr(db, "evenements_a_venir", lambda genre=None, jours=60: [
        e for e in b.evals if e["le"] < LUNDI + dt.timedelta(days=jours)])
    monkeypatch.setattr(db, "nb_cartes_a_revoir", lambda rev_id=None: b.dues)
    monkeypatch.setattr(db, "nb_evaluations_proches", lambda jours=7: len(b.evals))
    monkeypatch.setattr(db, "nouveaux_cours_sans_revision", lambda: b.nouveaux)
    monkeypatch.setattr(db, "campagne_du_jour", lambda jour: b.campagne)
    return b


def evaluation(jours, revision_id=None, statut=None, heure=10, **k):
    le = (LUNDI + dt.timedelta(days=jours)).replace(hour=heure, minute=0)
    return {"id": f"cours:{jours}", "matiere": "MATHEMATIQUES", "titre": "DS", "le": le,
            "revision_id": revision_id, "revision_statut": statut, **k}


def abonnement():
    """Un vrai couple de clés d'appareil, pour que le chiffrement soit réellement fait."""
    prive = ec.generate_private_key(ec.SECP256R1())
    pub = prive.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda x: base64.urlsafe_b64encode(x).rstrip(b"=").decode()
    return {"endpoint": "https://web.push.apple.com/abc", "p256dh": b64(pub), "auth": b64(b"0123456789abcdef"),
            "appareil": "iPhone (appli)"}


def test_cle_vapid_creee_une_fois(base):
    cle = nt.cle_publique()
    assert len(base64.urlsafe_b64decode(cle + "==")) == 65 and cle == nt.cle_publique()
    assert base.etat["vapid_prive"].startswith("-----BEGIN PRIVATE KEY-----")


def test_heures_de_silence():
    p = nt.DEFAUT
    assert nt.en_silence(LUNDI.replace(hour=23), p) and nt.en_silence(LUNDI.replace(hour=6), p)
    assert not nt.en_silence(LUNDI.replace(hour=7, minute=30), p) and not nt.en_silence(LUNDI, p)
    assert nt.en_silence(LUNDI.replace(hour=13), {**p, "silence_debut": "12:00", "silence_fin": "14:00"})


def test_rappel_du_jour_par_priorite(base):
    jour = LUNDI.date()
    assert nt.rappel_du_jour(jour) is None
    base.nouveaux = {"niveau": "BTS2", "matiere": "Cybersécurité", "n": 4}
    titre, corps, url = nt.rappel_du_jour(jour)
    assert titre == "Nouveaux cours en Cybersécurité" and "4 cours" in corps and "genre=cartes" in url
    base.evals = [evaluation(1, revision_id=7, statut="pret")]
    assert nt.rappel_du_jour(jour) == ("Contrôle de MATHEMATIQUES demain", "Relis ta fiche, 5 minutes suffisent.",
                                       "/revision/7")
    base.dues = 12
    titre, corps, url = nt.rappel_du_jour(jour)
    assert titre == "Réviser maintenant" and "12 flashcards" in corps and "fiche est prête" in corps
    assert url == "/cartes/session"
    base.evals.append(evaluation(4))  # contrôle sans fiche : passe avant tout
    titre, corps, url = nt.rappel_du_jour(jour)
    assert titre == "Contrôle de MATHEMATIQUES dans 4 jours" and "je la prépare" in corps and "12 flashcards" in corps


def test_planifier_une_fois_par_jour(base):
    base.dues = 3
    nt.planifier(LUNDI.replace(hour=17))
    assert base.file == {}  # pas encore l'heure
    nt.planifier(LUNDI)
    nt.planifier(LUNDI + dt.timedelta(minutes=5))
    assert list(base.file) == ["rappel:2026-10-26"] and base.file["rappel:2026-10-26"]["titre"] == "Réviser maintenant"
    base.dues = 0
    nt.planifier(LUNDI + dt.timedelta(days=1))
    assert base.file["rappel:2026-10-27"]["statut"] == "ignore"  # rien à proposer : rien n'est envoyé


def test_matin_du_controle(base):
    base.evals = [evaluation(0, heure=14, revision_id=3)]
    nt.planifier(LUNDI.replace(hour=8))
    n = base.file["jour:MATHEMATIQUES:2026-10-26T14:00"]
    assert n["titre"] == "MATHEMATIQUES aujourd'hui à 14:00" and n["url"] == "/revision/3"


def test_envoi_chiffre_et_appareil_expire(base, monkeypatch):
    envois = []

    class Reponse:
        def __init__(self, code):
            self.status_code, self.reason, self.text, self.headers = code, "", "", {}

    def post(url, data=None, headers=None, timeout=None):
        envois.append((url, data, headers))
        return Reponse(410 if url.endswith("/expire") else 201)

    monkeypatch.setattr("pywebpush.requests.post", post)
    base.abos = [abonnement(), {**abonnement(), "endpoint": "https://web.push.apple.com/expire"}]
    base.dues = 2
    base.notifier("revision:4", "Fiche prête", "VLAN", "/revision/4")
    nt.vider_file(LUNDI.replace(hour=23))
    assert envois == []  # heures de silence : ça attend
    nt.vider_file(LUNDI)
    assert len(envois) == 2 and base.file["revision:4"]["statut"] == "envoye"
    url, corps, entetes = envois[0]
    assert entetes["content-encoding"] == "aes128gcm" and b"VLAN" not in corps  # chiffré pour l'appareil
    assert entetes["ttl"] == str(nt.DUREE_VIE) and entetes["Authorization"].startswith("vapid t=")
    assert [a["endpoint"] for a in base.abos] == ["https://web.push.apple.com/abc"]  # l'appareil désabonné est oublié


def test_echec_d_envoi_garde_l_erreur(base, monkeypatch):
    monkeypatch.setattr(nt, "envoyer", lambda abo, charge: "403 BadJwtToken")
    base.abos = [abonnement()]
    base.notifier("test", "Titre")
    nt.vider_file(LUNDI)
    assert base.file["test"]["statut"] == "erreur" and "BadJwtToken" in base.file["test"]["erreur"]


def test_pronote_sans_appareil_n_annonce_pas_le_passe(base):
    base.etat["notifications_cles_stables"] = "1"
    base.evals = [evaluation(5)]
    nt.apres_synchro_pronote(3)
    assert base.file["nouvelle:MATHEMATIQUES:2026-10-31T10:00"]["statut"] == "ignore" and len(base.file) == 1
    base.abos = [abonnement()]
    base.evals.append({**evaluation(9), "id": "cours:9", "titre": "Oral"})
    nt.apres_synchro_pronote(2)
    nouvelle = base.file["nouvelle:MATHEMATIQUES:2026-11-04T10:00"]
    assert nouvelle["statut"] == "attente" and nouvelle["corps"] == "mer. 04/11 · Oral"
    assert any(c.startswith("documents:") and "2 documents" in n["corps"] for c, n in base.file.items())


def test_pronote_ids_qui_changent_ne_reannoncent_rien(base):
    """Le bug : Pronote donne de nouveaux ids à chaque connexion, chaque synchro ré-annonçait tout."""
    base.etat["notifications_cles_stables"] = "1"
    base.abos = [abonnement()]
    base.evals = [{**evaluation(5), "id": "cours:25#abc"}]
    nt.apres_synchro_pronote()
    for nouvel_id in ("cours:25#xyz", "cours:25#def", "cours:25#ghi"):
        base.evals = [{**evaluation(5), "id": nouvel_id}]
        nt.apres_synchro_pronote()
    assert len(base.file) == 1


def test_premier_passage_apres_mise_a_jour_n_annonce_rien(base):
    base.abos = [abonnement()]
    base.evals = [evaluation(5), evaluation(6)]
    nt.apres_synchro_pronote()
    assert {n["statut"] for n in base.file.values()} == {"ignore"} and base.etat["notifications_cles_stables"]
    base.evals.append(evaluation(7))
    nt.apres_synchro_pronote()
    assert base.file["nouvelle:MATHEMATIQUES:2026-11-02T10:00"]["statut"] == "attente"


def test_meme_texte_pas_renvoye_et_rafale_resumee(base, monkeypatch):
    envois = []
    monkeypatch.setattr(nt, "envoyer", lambda abo, charge: envois.append(charge))
    monkeypatch.setattr(nt, "badge", lambda: 0)
    base.abos = [abonnement()]
    base.notifier("documents:1", "Nouveaux documents des profs", "2 documents")
    base.notifier("documents:2", "Nouveaux documents des profs", "2 documents")  # autre clé, même texte
    nt.vider_file(LUNDI)
    nt.vider_file(LUNDI)  # un second tour ne renvoie rien
    assert len(envois) == 1 and base.file["documents:2"]["statut"] == "ignore"
    for i in range(5):
        base.notifier(f"x:{i}", f"Truc {i}")
    nt.vider_file(LUNDI)
    assert len(envois) == 2 and envois[1]["titre"] == "5 nouveautés sur Claunote" and "Truc 4" in envois[1]["corps"]
    assert all(base.file[f"x:{i}"]["statut"] == "envoye" for i in range(5))


def test_revision_prete(base):
    rev = {"id": 4, "genre": "cartes", "sujet": "VLAN"}
    nt.revision_prete(rev)
    assert base.file == {}  # personne n'est abonné
    base.abos = [abonnement()]
    nt.revision_prete(rev)
    assert base.file["revision:4"]["titre"] == "Flashcards prêtes" and base.file["revision:4"]["url"] == "/revision/4"


def test_reglages(base):
    nt.enregistrer_preferences({"heure": "19:15", "rappel": False, "inconnu": 1})
    p = nt.preferences()
    assert p["heure"] == "19:15" and p["rappel"] is False and p["pronote"] is True and "inconnu" not in p
    assert json.loads(base.etat["notifications"]) == {"heure": "19:15", "rappel": False}


def test_rappel_du_jour_campagne(base):
    jour = LUNDI.date()
    base.dues = 5
    base.campagne = {"id": 3, "titre": "BTS blanc", "faits": [f"{jour}:1"], "programme": {"jours": [
        {"date": jour.isoformat(), "titre": "", "taches": [
            {"matiere": "Maths", "quoi": "Épreuve", "duree": 120, "support": "examen"},
            {"matiere": "CEJM", "quoi": "Fiche", "duree": 30, "support": "fiche"}]}]}}
    titre, corps, url = nt.rappel_du_jour(jour)
    assert titre == "BTS blanc : programme du jour" and url == "/campagne/3"
    assert "Maths (120 min)" in corps and "CEJM" not in corps and "5 flashcards" in corps
    base.campagne["faits"].append(f"{jour}:0")  # tout est fait : on passe au rappel suivant
    assert nt.rappel_du_jour(jour)[0] == "Réviser maintenant"


def test_campagne_prete_une_seule_fois(base, monkeypatch):
    base.abos = [abonnement()]
    revs = [{"statut": "pret"}, {"statut": "en_attente"}]
    monkeypatch.setattr(db, "campagne", lambda i: {"id": i, "titre": "BTS blanc", "statut": "pret"})
    monkeypatch.setattr(db, "revisions_campagne", lambda i: revs)
    nt.campagne_prete(3)
    assert base.file == {}  # un support est encore en préparation
    revs[1]["statut"] = "erreur"
    nt.campagne_prete(3)
    nt.campagne_prete(3)
    assert list(base.file) == ["campagne:3"] and "1 en erreur" in base.file["campagne:3"]["corps"]


def test_menage_propose_le_dimanche(base, monkeypatch):
    from claunote import archivage
    dimanche = dt.datetime(2026, 11, 22, 18, 30, tzinfo=PARIS)
    props = [{"type": "revision", "id": i, "genre": "fiche", "titre": "x", "raison": "r"} for i in range(3)]
    monkeypatch.setattr(archivage, "propositions", lambda maintenant=None: props)
    nt.planifier(dimanche - dt.timedelta(days=6))           # lundi : rien
    assert not any(c.startswith("archivage:") for c in base.file)
    nt.planifier(dimanche)
    nt.planifier(dimanche + dt.timedelta(minutes=30))       # une fois par semaine
    menage = [n for c, n in base.file.items() if c.startswith("archivage:")]
    assert len(menage) == 1 and menage[0]["url"] == "/archives" and menage[0]["statut"] == "attente"
    props.pop()                                              # trop peu la semaine suivante : rien n'est envoyé
    nt.planifier(dimanche + dt.timedelta(days=7))
    assert [n["statut"] for c, n in base.file.items() if c.startswith("archivage:")] == ["attente", "ignore"]
