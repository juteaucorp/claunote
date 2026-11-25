"""Actualités sur une vraie base PostgreSQL (lancé seulement si CLAUNOTE_TEST_DB est défini) : alertes, et un fil
par compte (sources, sujets, articles, réglages, notifications)."""
import datetime as dt
import json

from claunote import actualites, agent, comptes, db, notifications
from tests.test_base_comptes import URL, base, pytestmark  # noqa: F401  (même base jetable)


def _article(c, flux_id, cle, titre, annonce, alerte, heures):
    return c.execute(
        "INSERT INTO articles (flux_id, cle, lien, titre, extrait, publie_le, sujets, tri, annonce, alerte, score) "
        "VALUES (%s, %s, '', %s, '', now() - make_interval(hours => %s), '{1}', 'ia', %s, %s, 0.9) RETURNING id",
        (flux_id, cle, titre, heures, annonce, alerte)).fetchone()["id"]


def test_alerte_deja_decidee_reste_candidate_jusqu_a_sa_notification(base):
    flux_id = db.flux_ajouter("https://news.google.com/rss/search?q=cisco", "Google Actualités : Réseaux", "google")
    with db.connect() as c:
        cisco = _article(c, flux_id, "a1", "Cisco Expressway CVE-2026-76466", True, True, 2)
        _article(c, flux_id, "a2", "Article déjà décidé", True, False, 2)
        _article(c, flux_id, "a3", "Vieille alerte", True, True, 72)
        neuf = _article(c, flux_id, "a4", "Neuf", False, False, 1)
    assert sorted(a["id"] for a in db.articles_a_annoncer()) == [cisco, neuf]
    assert not db.alerte_deja_envoyee(["actu-alerte:cve-2026-76466"], ["CVE-2026-76466"])
    # Envoyée autrefois sous l'étiquette de l'IA : la CVE citée suffit à la reconnaître
    db.notifier("actu-alerte:cisco-expressway", "🚨 Cisco : faille CVE-2026-76466", "Patcher")
    assert db.alerte_deja_envoyee(["actu-alerte:cve-2026-76466"], ["CVE-2026-76466"])
    assert not db.alerte_deja_envoyee(["actu-alerte:cve-2026-99999"], ["CVE-2026-99999"])
    assert db.alerte_deja_envoyee(["actu-alerte:cisco-expressway"], [])


# --- À chaque compte son fil d'actualités, ses alertes sur ses appareils ----------------------------------

FAILLE = "Faille critique FortiOS CVE-2026-11111 exploitée"


def _fil(compte_id, sujet="Cybersécurité", mots=("faille", "CVE"), notifier=True, url="https://ex.fr/feed"):
    """Un flux et un sujet pour ce compte, et le même article (même lien) chez chacun."""
    with db.pour_compte(compte_id):
        db.sujet_ajouter(sujet, "Failles", list(mots), notifier)
        flux_id = db.flux_ajouter(url, "Ex", "ex.fr")
        db.articles_ajouter(flux_id, [{"cle": "ex.fr/fortios", "lien": "https://ex.fr/fortios", "titre": FAILLE,
                                       "extrait": "", "publie_le": dt.datetime.now(dt.timezone.utc)}], annonce=False)
        db.enregistrer_abonnement(f"https://push.example/{compte_id}", "p", "a", f"tel-{compte_id}")


def _notifs():
    with db.sans_compte(), db.connect() as c:
        return [dict(r) for r in c.execute("SELECT cle, titre, compte_id FROM notifications ORDER BY id").fetchall()]


def test_chaque_compte_a_son_propre_fil(base):
    invite = db.creer_compte("camille", "h", "invite", ["actualites"])
    _fil(1)
    _fil(invite, sujet="Réseaux", mots=("VLAN",), url="https://autre.fr/feed")
    with db.pour_compte(invite):
        assert [s["nom"] for s in db.sujets()] == ["Réseaux"]
        assert [f["url"] for f in db.flux_liste()] == ["https://autre.fr/feed"]
        art = db.articles("tout")[0][0]
        db.article_maj(art["id"], lu=True, favori=True)
        # Ses changements ne touchent pas le fil de l'admin, même avec le bon identifiant
        with db.pour_compte(1):
            admin_flux = db.flux_liste()[0]["id"]
        db.flux_supprimer(admin_flux)
        db.sujet_ajouter("Cybersécurité", "", [])   # même nom que chez l'admin : permis
        db.flux_ajouter("https://ex.fr/feed")        # même adresse que chez l'admin : permis
    with db.pour_compte(1):
        assert [s["nom"] for s in db.sujets()] == ["Cybersécurité"]
        assert [f["url"] for f in db.flux_liste()] == ["https://ex.fr/feed"]
        a = db.articles("tout")[0][0]
        assert not a["lu"] and not a["favori"]
    assert not comptes.tout_voir({"role": "admin", "tout_voir": True}, "/actualites")   # « Tout voir » : pas de mélange


def test_alerte_sonne_chez_chaque_compte_selon_son_fil(base, monkeypatch):
    sans_forfait = db.creer_compte("camille", "h", "invite", ["actualites"])
    autonome = db.creer_compte("dominique", "h", "invite", ["actualites", "claude"])
    sans_notif = db.creer_compte("eli", "h", "invite", ["actualites"])
    coupe = db.creer_compte("farah", "h", "invite", ["actualites"])
    hors_sujet = db.creer_compte("gael", "h", "invite", ["actualites"])
    db.creer_compte("hugo", "h", "invite", ["bibliotheque"])   # sans le droit Actualités : pas de fil
    _fil(1)
    _fil(sans_forfait)
    _fil(autonome)
    _fil(sans_notif, notifier=False)
    _fil(coupe)
    _fil(hors_sujet, sujet="Cuisine", mots=("recette",))
    notifications.enregistrer_preferences({**notifications.DEFAUT, "actualites": False}, coupe)
    db.etat_ecrire("actualites_releve", str(9e12))   # pas de relevé réseau dans le test
    appels = []

    def demander(tache, prompt, cwd, entree=None, timeout=0, lire=None, **k):
        if tache != "actualites":
            raise agent.ErreurAgent("pas de point du jour ici")
        appels.append(db.compte_actuel())
        ids = [json.loads(l)["id"] for l in entree.splitlines()]
        return lire(json.dumps([{"id": i, "sujets": ["S1"], "score": 0.95, "resume": "Patcher FortiOS",
                                 "alerte": True, "evenement": "fortios"} for i in ids])), "haiku"
    monkeypatch.setattr(agent, "demander", demander)
    monkeypatch.setattr(notifications, "en_silence", lambda m, p: False)
    # Pas de notification du jour (« L'essentiel ») : sinon le test dépend de l'heure où la CI tourne
    monkeypatch.setattr(actualites, "heure_quotidienne", lambda: (None, "test"))
    actualites.tour(lambda: True)

    # l'IA pour l'admin ; le compte en autonomie suit le même fil : le même verdict est repris, sans rappeler Claude
    assert appels == [1]
    n = _notifs()
    par = {x["compte_id"]: x for x in n}
    assert sorted(par, key=lambda c: c or 0) == [None, sans_forfait, autonome]
    assert par[None]["cle"] == "actu-alerte:cve-2026-11111"                     # l'admin : comme avant
    assert par[sans_forfait]["cle"] == f"actu-alerte:cve-2026-11111@{sans_forfait}"
    assert all(x["titre"] == f"🚨 {FAILLE}" for x in n)
    # Sans forfait : le verdict de l'IA chez l'admin est repris, rangé dans son sujet par mots-clés
    with db.pour_compte(sans_forfait):
        a = db.articles("tout")[0][0]
        assert a["tri"] == "mots_cles" and a["alerte"] and a["pourquoi"] == "Patcher FortiOS" and a["sujets"]
    with db.pour_compte(hors_sujet):
        assert not db.articles("tout")[0][0]["alerte"]   # pas dans ses sujets : pas d'alerte pour lui

    # Un tour de plus : rien ne sonne deux fois ; chaque compte reçoit les siennes sur ses appareils
    actualites.tour(lambda: True)
    assert len(_notifs()) == 3
    envois = []
    monkeypatch.setattr(notifications, "envoyer", lambda abo, charge: envois.append(abo["appareil"]))
    monkeypatch.setattr(notifications, "badge", lambda: 0)
    notifications.vider_file(dt.datetime(2026, 11, 24, 12, tzinfo=notifications.PARIS))
    assert sorted(envois) == sorted(["tel-1", f"tel-{sans_forfait}", f"tel-{autonome}"])


def test_reglages_et_point_du_jour_propres_au_compte(base):
    invite = db.creer_compte("camille", "h", "invite", ["actualites"])
    with db.pour_compte(invite):
        actualites.enregistrer_reglages({"ia": True, "quotidien": "non"})
        actualites.noter_lecture(dt.datetime(2026, 11, 24, 9, tzinfo=dt.timezone.utc))
        assert actualites.heure_quotidienne()[0] is None
    with db.pour_compte(1):
        assert actualites.reglages()["quotidien"] == "auto"
        assert db.etat_valeur("actualites_lectures") is None
    assert db.etat_valeur(f"actualites:{invite}") and db.etat_valeur(f"actualites_lectures:{invite}")


def test_nouveau_compte_recoit_la_selection_de_depart(base):
    invite = db.creer_compte("camille", "h", "invite", ["actualites"])
    actualites.initialiser()   # l'admin, au démarrage du worker
    db.etat_ecrire("actualites_releve", str(9e12))
    actualites.tour(lambda: False)
    with db.pour_compte(invite):
        assert len(db.flux_liste()) == len(actualites.FLUX_DEFAUT)
        assert [s["nom"] for s in db.sujets()] == sorted(n for n, _, _ in actualites.SUJETS_DEFAUT)
        actualites.marquer_personnalise()   # comme la page Sources quand il retire un sujet
        db.sujet_supprimer(db.sujets()[0]["id"])
    actualites.tour(lambda: False)   # ses changements restent : la sélection n'est donnée qu'une fois
    with db.pour_compte(invite):
        assert len(db.sujets()) == len(actualites.SUJETS_DEFAUT) - 1
    with db.pour_compte(1):
        assert len(db.sujets()) == len(actualites.SUJETS_DEFAUT)


def test_installation_d_avant_les_fils_par_compte(base):
    """Base d'avant : un seul fil commun (sans compte_id, noms et adresses uniques) → tout va à l'admin d'origine."""
    with db.connect() as c:
        for t, col in (("articles", "cle"), ("flux", "url"), ("sujets", "nom")):
            c.execute(f"DROP TRIGGER proprietaire ON {t}")
            c.execute(f"ALTER TABLE {t} DROP COLUMN compte_id CASCADE")
            c.execute(f"ALTER TABLE {t} ADD CONSTRAINT {t}_{col}_key UNIQUE ({col})")
        c.execute("INSERT INTO sujets (nom) VALUES ('Cybersécurité')")
        f = c.execute("INSERT INTO flux (url) VALUES ('https://ex.fr/feed') RETURNING id").fetchone()["id"]
        c.execute("INSERT INTO articles (flux_id, cle, titre) VALUES (%s, 'ex.fr/1', 'Vieux')", (f,))
    db.init()
    invite = db.creer_compte("camille", "h", "invite", ["actualites"])
    with db.pour_compte(1):
        assert len(db.sujets()) == 1 and len(db.flux_liste()) == 1 and db.articles("tout")[1] == 1
    with db.pour_compte(invite):
        assert db.sujets() == [] and db.articles("tout")[1] == 0
        assert db.sujet_ajouter("Cybersécurité", "", []) is not None


# --- Fil par défaut : celui de tout compte qui n'a rien changé, choisi par l'admin -------------------------

def _noms():
    return sorted(s["nom"] for s in db.sujets()), sorted(f["url"] for f in db.flux_liste() if not f["sujet_id"])


def test_fil_par_defaut_choisi_par_l_admin(base):
    suit = db.creer_compte("camille", "h", "invite", ["actualites"])
    change = db.creer_compte("dominique", "h", "invite", ["actualites"])
    actualites.initialiser()
    db.etat_ecrire("actualites_releve", str(9e12))
    actualites.tour(lambda: False)
    with db.pour_compte(change):   # il change un sujet : son fil devient le sien
        actualites.marquer_personnalise()
        db.sujet_ajouter("Jeux vidéo", "", ["jeu"])
        assert actualites.personnalise()
    with db.pour_compte(1):        # l'admin fait son fil, avec un sujet sans alerte, puis le met par défaut
        db.sujet_supprimer(next(s["id"] for s in db.sujets() if s["nom"] == "Réseaux"))
        sujet = db.sujet_ajouter("Cuisine", "Recettes", ["recette"], notifier=False)
        db.flux_ajouter("https://cuisine.fr/feed", "Cuisine", "cuisine.fr", notifier=True)
        db.flux_supprimer(next(f["id"] for f in db.flux_liste() if "korben" in f["url"]))
        db.flux_ajouter(actualites.recherche_google(next(s for s in db.sujets() if s["id"] == sujet)),
                        "Google Actualités : Cuisine", "news.google.com", sujet_id=sujet)
        actualites.definir_defaut()
        attendu = _noms()
    assert actualites.comptes_suivant_defaut() == ([{"id": suit, "nom": "camille"}], [{"id": change, "nom": "dominique"}])
    actualites.tour(lambda: False)
    with db.pour_compte(suit):
        assert _noms() == attendu
        sujets = {s["nom"]: s for s in db.sujets()}
        assert sujets["Cuisine"]["notifier"] and sujets["Cuisine"]["recherche_id"]   # alertes activées par défaut
        assert next(f for f in db.flux_liste() if f["url"] == "https://cuisine.fr/feed")["notifier"]
        assert not actualites.personnalise()
    with db.pour_compte(change):   # personnalisé : jamais touché
        assert "Jeux vidéo" in _noms()[0] and "Réseaux" in _noms()[0]
        actualites.suivre_defaut()  # « Revenir au fil par défaut »
        assert _noms() == attendu and not actualites.personnalise()
    with db.pour_compte(1):
        actualites.definir_defaut(depuis_mon_fil=False)   # retour à la sélection de départ
    actualites.tour(lambda: False)
    with db.pour_compte(suit):
        assert _noms()[0] == sorted(n for n, _, _ in actualites.SUJETS_DEFAUT)
        assert len(_noms()[1]) == len(actualites.FLUX_DEFAUT)
    with db.pour_compte(1):        # le fil de l'admin ne bouge jamais tout seul
        assert "Cuisine" in _noms()[0]


def test_fil_deja_change_avant_le_fil_par_defaut_reste_le_sien(base):
    invite = db.creer_compte("camille", "h", "invite", ["actualites"])
    with db.pour_compte(invite):
        db.sujet_ajouter("Jeux vidéo", "", ["jeu"])
        actualites.initialiser()
        assert actualites.personnalise() and _noms()[0] == ["Jeux vidéo"]


# --- « L'essentiel du jour » pour les autres comptes (droit « essentiel ») -----------------------------------

def _fil_essentiel(compte_id, sujet="Cybersécurité"):
    _fil(compte_id, sujet=sujet)
    with db.pour_compte(compte_id):
        flux_id = db.flux_liste()[0]["id"]
        db.articles_ajouter(flux_id, [{"cle": "ex.fr/chrome", "lien": "https://ex.fr/chrome", "extrait": "",
                                       "titre": "Faille Chrome 9,6/10 corrigée", "publie_le": dt.datetime.now(dt.timezone.utc)}],
                            annonce=False)


def test_essentiel_pour_les_comptes_avec_le_droit(base, monkeypatch):
    meme_fil = db.creer_compte("camille", "h", "invite", ["actualites", "essentiel"])
    sans_droit = db.creer_compte("dominique", "h", "invite", ["actualites"])
    autre_fil = db.creer_compte("eli", "h", "invite", ["actualites", "essentiel"])
    for c in (1, meme_fil, sans_droit):
        _fil_essentiel(c)
    _fil_essentiel(autre_fil, sujet="Failles")   # un autre sujet : fil différent de celui de l'admin
    db.etat_ecrire("actualites_releve", str(9e12))
    points = []

    def demander(tache, prompt, cwd, entree=None, timeout=0, lire=None, **k):
        ids = [json.loads(l)["id"] for l in entree.splitlines()]
        if tache == "actualites_point":
            points.append((db.compte_actuel(), agent.forfait_autorise()))
            return lire(json.dumps({"points": [{"texte": f"Point {i}", "ids": [i]} for i in ids]})), "opus"
        return lire(json.dumps([{"id": i, "sujets": ["S1"], "score": 0.9, "resume": f"Résumé {i}",
                                 "alerte": False, "evenement": f"e{i}"} for i in ids])), "haiku"
    monkeypatch.setattr(agent, "demander", demander)
    monkeypatch.setattr(notifications, "en_silence", lambda m, p: False)
    actualites.tour(lambda: True)

    # Claude : l'admin, et le compte au fil différent (sur le forfait de l'admin, sans autonomie) ; pas les autres
    assert points == [(1, True), (autre_fil, True)]
    with db.pour_compte(1):
        admin = actualites.point()
    with db.pour_compte(meme_fil):   # même fil : le point de l'admin, relié à SES articles
        p = actualites.point()
        a_moi = {a["id"] for a in db.articles("tout")[0]}
        assert p["copie"] and [x["texte"] for x in p["points"]] == [x["texte"] for x in admin["points"]]
        assert {i for x in p["points"] for i in x["ids"]} == a_moi
    with db.pour_compte(sans_droit):
        assert actualites.point() is None
    with db.pour_compte(autre_fil):
        assert len(actualites.point()["points"]) == 2

    actualites.tour(lambda: True)    # rien de nouveau : ni Claude, ni nouvelle copie
    assert len(points) == 2


# --- Fil de l'admin par défaut en permanence : les comptes qui le suivent reprennent tout de lui -------------

def test_comptes_sur_le_fil_admin_reprennent_ses_articles_et_ses_alertes(base, monkeypatch):
    suit = db.creer_compte("camille", "h", "invite", ["actualites", "claude", "essentiel"])
    perso = db.creer_compte("dominique", "h", "invite", ["actualites", "claude"])
    _fil(1)
    with db.pour_compte(suit):
        db.enregistrer_abonnement("https://push.example/suit", "p", "a", "tel-suit")
    _fil(perso, sujet="Réseaux", mots=("faille",), url="https://autre.fr/feed")
    with db.pour_compte(perso):
        actualites.marquer_personnalise()
    db.etat_ecrire("actualites_releve", str(9e12))
    appels = []

    def demander(tache, prompt, cwd, entree=None, timeout=0, lire=None, **k):
        if tache != "actualites":
            raise agent.ErreurAgent("pas de point ici")
        appels.append(db.compte_actuel())
        ids = [json.loads(l)["id"] for l in entree.splitlines()]
        return lire(json.dumps([{"id": i, "sujets": ["S1"], "score": 0.95, "resume": "Patcher FortiOS",
                                 "alerte": True, "evenement": "fortios"} for i in ids])), "haiku"
    monkeypatch.setattr(agent, "demander", demander)
    monkeypatch.setattr(actualites, "heure_quotidienne", lambda: (None, "test"))   # indépendant de l'heure de la CI
    actualites.tour(lambda: True)   # le fil de l'admin devient le fil par défaut (en permanence, réglage de départ)
    actualites.tour(lambda: True)   # le compte qui suit le reçoit, puis reprend ses articles
    assert actualites.profil_defaut()["depart"] is False
    with db.pour_compte(suit):
        assert actualites.suit_l_admin()
        a = db.articles("tout")[0][0]
        assert a["titre"] == FAILLE and a["tri"] == "ia" and a["alerte"] and a["pourquoi"] == "Patcher FortiOS"
        assert a["sujets"] == [s["id"] for s in db.sujets() if s["nom"] == "Cybersécurité"]
    with db.pour_compte(perso):
        assert not actualites.suit_l_admin()
    assert suit not in appels and appels.count(1) == 1 and perso in appels   # rien n'est refait pour celui qui suit
    cles = {n["compte_id"]: n["cle"] for n in _notifs()}
    assert cles[None] == "actu-alerte:cve-2026-11111" and cles[suit] == f"actu-alerte:cve-2026-11111@{suit}"

    # L'admin change son fil : le fil par défaut suit, et le compte aussi
    with db.pour_compte(1):
        db.sujet_ajouter("Cloud", "Azure, AWS", ["Azure"])
    actualites.tour(lambda: True)
    actualites.tour(lambda: True)
    with db.pour_compte(suit):
        assert "Cloud" in [s["nom"] for s in db.sujets()] and actualites.suit_l_admin()
    with db.pour_compte(perso):
        assert "Cloud" not in [s["nom"] for s in db.sujets()]


def test_alerte_disque_en_file_retiree_quand_la_place_revient(base):
    db.notifier("disque:attention:2026-11-24", "💽 Peu de place sur le disque", "4.8 Go libres")
    db.notifier("jour:controle", "Contrôle ce matin", "Maths")
    assert db.oublier_notifications("disque:", "disque de nouveau libre") == 1
    with db.connect() as c:
        statuts = {r["cle"]: r["statut"] for r in c.execute("SELECT cle, statut FROM notifications")}
    assert statuts["disque:attention:2026-11-24"] == "ignore" and statuts["jour:controle"] == "attente"
