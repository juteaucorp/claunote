import datetime as dt
import json

import pytest

from claunote import actualites as ac, agent, db, main, notifications
from tests.test_site import anonyme, client  # noqa: F401  (fixtures du site)

MAINTENANT = dt.datetime(2026, 11, 12, 10, 0, tzinfo=dt.timezone.utc)
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Veille SIO</title><link>https://ex.fr</link>
<item><title>Faille &lt;b&gt;critique&lt;/b&gt; dans Windows Server</title><link>https://www.ex.fr/a/?utm_source=rss&amp;id=3#haut</link>
  <description>&lt;p&gt;Une &lt;b&gt;CVE&lt;/b&gt; exploitée&lt;/p&gt;&lt;script&gt;x()&lt;/script&gt;</description>
  <pubDate>Wed, 11 Nov 2026 08:00:00 +0000</pubDate></item>
<item><title>Même article relayé</title><link>https://ex.fr/a?id=3</link></item>
<item><title>Lien piégé</title><link>javascript:alert(1)</link></item>
</channel></rss>"""


def test_lire_flux_nettoie_et_deduplique():
    infos, items = ac.lire_flux(RSS.encode(), 1)
    assert infos == {"titre": "Veille SIO", "site": "https://ex.fr"}
    assert [i["cle"] for i in items][:1] == ["ex.fr/a?id=3"]   # sans utm, sans www, sans ancre ni / final
    assert len(items) == 2   # le relais du même lien n'est gardé qu'une fois
    assert items[0]["titre"] == "Faille critique dans Windows Server"
    assert items[0]["extrait"] == "Une CVE exploitée"
    assert items[0]["publie_le"] == dt.datetime(2026, 11, 11, 8, tzinfo=dt.timezone.utc)
    assert items[1]["lien"] == "" and items[1]["cle"].startswith("sans-lien:")   # jamais de javascript:


def test_lire_flux_refuse_une_page_html():
    with pytest.raises(ac.FluxIllisible):
        ac.lire_flux(b"<html><body>Bonjour</body></html>")


def test_mots_cles_sans_accents_ni_casse():
    assert ac.correspond(["vulnérabilité"], "Deux VULNERABILITES corrigées")
    assert ac.correspond(["CVE"], "La CVE-2026-1234")
    assert ac.correspond(["faille"], "Trois failles corrigées")   # début de mot : le pluriel est trouvé
    assert not ac.correspond(["Java"], "Un café à Jakarta")
    assert not ac.correspond(["SQL"], "Base MySQL")                  # jamais au milieu d'un mot
    assert ac.correspond(["Active Directory"], "Attaque sur un active directory")


class Base:
    def __init__(self, sujets, articles, abonnes=True):
        self.sujets_, self.articles_, self.abonnes = sujets, articles, abonnes
        self.tries, self.notifs, self.annonces = [], [], []

    def brancher(self, monkeypatch):
        monkeypatch.setattr(db, "sujets", lambda: self.sujets_)
        monkeypatch.setattr(db, "articles_a_trier", lambda limite=200: [a for a in self.articles_ if a["tri"] == "attente"])
        monkeypatch.setattr(db, "articles_trier", lambda r: self.tries.extend(r))
        monkeypatch.setattr(db, "articles_a_annoncer", lambda: self.articles_)
        monkeypatch.setattr(db, "articles_annonces", lambda ids: self.annonces.extend(ids))
        monkeypatch.setattr(db, "abonnements", lambda: [{"endpoint": "x"}] if self.abonnes else [])
        monkeypatch.setattr(db, "notifier", lambda *a, **k: self.notifs.append(a) or True)
        monkeypatch.setattr(db, "etat_valeur", lambda k: None)
        monkeypatch.setattr(ac.local, "reglages", lambda: {"mode": "local_d_abord"})


SUJETS = [{"id": 7, "nom": "Cybersécurité", "description": "Failles", "mots_cles": ["faille", "CVE"], "notifier": True},
          {"id": 9, "nom": "Dev", "description": "", "mots_cles": ["Python"], "notifier": False}]


def article(i, titre, **k):
    return {"id": i, "cle": f"ex.fr/{i}", "titre": titre, "extrait": "", "source": "Ex", "lien": f"https://ex.fr/{i}",
            "publie_le": MAINTENANT - dt.timedelta(hours=1), "cree_le": MAINTENANT, "tri": "attente", "sujets": [],
            "score": None, "pourquoi": None, "flux_notifier": False, **k}


def test_tri_par_l_ia(monkeypatch):
    b = Base(SUJETS, [article(1, "Faille dans Exchange"), article(2, "Promo casques")])
    b.brancher(monkeypatch)
    vus = {}

    def demander(tache, prompt, cwd, entree=None, timeout=0, lire=None, **k):
        vus.update(tache=tache, prompt=prompt, ids=[json.loads(l)["id"] for l in entree.splitlines()])
        return lire('Voilà : [{"id": 1, "sujets": ["S1", "S9"], "score": 0.9, "pourquoi": "À corriger"},'
                    ' {"id": 2, "sujets": [], "score": 0.05}, {"id": 99, "sujets": ["S1"], "score": 1}]'), "haiku"
    monkeypatch.setattr(agent, "demander", demander)
    assert ac.trier(lambda: True) == 2
    assert vus["tache"] == "actualites" and vus["ids"] == [1, 2] and "S1 — Cybersécurité : Failles" in vus["prompt"]
    assert b.tries == [{"id": 1, "tri": "ia", "sujets": [7], "score": 0.9, "pourquoi": "À corriger"},
                       {"id": 2, "tri": "ia", "sujets": [], "score": 0.05, "pourquoi": None}]


def test_tri_sans_claude_attend_puis_mots_cles(monkeypatch):
    recent = article(1, "Faille dans Exchange", cree_le=dt.datetime.now(dt.timezone.utc))
    ancien = article(2, "Python 4 et faille", cree_le=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=4))
    b = Base(SUJETS, [recent, ancien])
    b.brancher(monkeypatch)
    monkeypatch.setattr(agent, "demander", lambda *a, **k: pytest.fail("Claude en pause : pas d'appel"))
    ac.trier(lambda: False)
    # le récent attend le retour de Claude ; celui qui attend depuis 4 h passe aux mots-clés
    assert b.tries == [{"id": 2, "tri": "mots_cles", "sujets": [7, 9], "score": None, "pourquoi": None}]


def test_tri_reponse_illisible_repli_mots_cles(monkeypatch):
    b = Base(SUJETS, [article(1, "Faille dans Exchange")])
    b.brancher(monkeypatch)

    def demander(tache, prompt, cwd, entree=None, timeout=0, lire=None, **k):
        return lire("désolé, je ne peux pas"), "haiku"
    monkeypatch.setattr(agent, "demander", demander)
    ac.trier(lambda: True)
    assert b.tries[0]["tri"] == "mots_cles" and b.tries[0]["sujets"] == [7]


def test_limite_claude_laisse_en_attente(monkeypatch):
    b = Base(SUJETS, [article(1, "Faille")])
    b.brancher(monkeypatch)
    monkeypatch.setattr(agent, "demander", lambda *a, **k: (_ for _ in ()).throw(agent.LimiteAtteinte("limit")))
    with pytest.raises(agent.LimiteAtteinte):   # le worker met Claude en pause
        ac.trier(lambda: True)
    assert b.tries == []


def test_annonces_regroupees_et_sans_spam(monkeypatch):
    b = Base(SUJETS, [
        article(1, "Faille A", tri="ia", sujets=[7], score=0.9, pourquoi="Patch urgent"),
        article(2, "Faille B", tri="ia", sujets=[7], score=0.8),
        article(3, "Faille mineure", tri="ia", sujets=[7], score=0.4),            # pas assez important
        article(4, "Python 4", tri="ia", sujets=[9], score=0.95),                  # sujet non notifié
        article(5, "Vieille faille", tri="mots_cles", sujets=[7],
                publie_le=MAINTENANT - dt.timedelta(days=5)),                        # trop vieux
        article(6, "Alerte CERT", tri="ia", sujets=[], score=0.5, source="CERT-FR", flux_notifier=True),
    ])
    b.brancher(monkeypatch)
    assert ac.annoncer(MAINTENANT) == 2
    titres = sorted(n[1] for n in b.notifs)
    assert titres == ["2 actualités : Cybersécurité", "Alerte CERT"]
    groupe = next(n for n in b.notifs if n[1].startswith("2"))
    assert groupe[2] == "Faille A · Faille B" and groupe[3] == "/actualites"
    assert sorted(b.annonces) == [1, 2, 3, 4, 5, 6]   # tout est décidé une fois pour toutes


def test_une_seule_annonce_avec_raison(monkeypatch):
    b = Base(SUJETS, [article(1, "Faille A", tri="ia", sujets=[7], score=0.9, pourquoi="Patch urgent")])
    b.brancher(monkeypatch)
    ac.annoncer(MAINTENANT)
    assert b.notifs == [("actu:ex.fr/1", "Faille A", "Cybersécurité · Ex : Patch urgent", "/actualites?article=1#a1")]


def test_sans_appareil_rien_ne_sonne_mais_tout_est_note(monkeypatch):
    b = Base(SUJETS, [article(1, "Faille A", tri="ia", sujets=[7], score=0.9)], abonnes=False)
    b.brancher(monkeypatch)
    assert ac.annoncer(MAINTENANT) == 0 and b.notifs == [] and b.annonces == [1]


def test_decouvrir_le_flux_d_un_site(monkeypatch):
    import httpx

    def repondre(requete):
        if requete.url.path == "/feed":
            return httpx.Response(200, content=RSS.encode(), headers={"content-type": "application/rss+xml"})
        return httpx.Response(200, text='<html><head><link rel="alternate" type="application/rss+xml" '
                                        'href="/feed" title="RSS"></head></html>')
    client_http = httpx.Client(transport=httpx.MockTransport(repondre))
    url, infos, items = ac.decouvrir("ex.fr", client_http)
    assert url == "https://ex.fr/feed" and infos["titre"] == "Veille SIO" and len(items) == 2


def test_releve_premier_passage_sans_notification(monkeypatch):
    import httpx
    ajouts, maj = [], {}
    monkeypatch.setattr(db, "articles_ajouter", lambda f, items, annonce: ajouts.append((f, len(items), annonce)) or len(items))
    monkeypatch.setattr(db, "flux_maj", lambda i, **k: maj.update(k))
    client_http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=RSS.encode(), headers={"etag": '"v1"'})))
    flux = {"id": 3, "url": "https://ex.fr/feed", "etag": None, "modifie": None, "releve_le": None, "echecs": 0,
            "titre": "", "site": ""}
    assert ac.relever_un(flux, SUJETS, client_http) == 2
    assert ajouts == [(3, 2, True)] and maj["etag"] == '"v1"' and maj["titre"] == "Veille SIO" and maj["erreur"] is None
    # Erreur du site : notée sur le flux, jamais levée
    client_http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert ac.relever_un({**flux, "releve_le": MAINTENANT}, SUJETS, client_http) == 0
    assert maj["erreur"] == "le site répond 404" and maj["echecs"] == 1


def test_page_actualites(client, monkeypatch):
    a = {**article(1, "Faille A", tri="ia", sujets=[7], score=0.9, pourquoi="Patch urgent"),
         "lu": False, "favori": False, "noms_sujets": ["Cybersécurité"], "total": 1}
    monkeypatch.setattr(db, "articles", lambda *x, **k: ([a], 1))
    monkeypatch.setattr(db, "sujets", lambda: [{**SUJETS[0], "n": 1, "recherche_id": None, "recherche_erreur": None}])
    monkeypatch.setattr(db, "flux_liste", lambda: [{"id": 1, "titre": "Ex", "url": "https://ex.fr/feed",
                                                     "releve_le": MAINTENANT, "sujet": None, "sujet_id": None,
                                                     "notifier": False, "n": 1, "dernier": MAINTENANT,
                                                     "erreur": "le site répond 404", "echecs": 2, "site": ""}])
    r = client.get("/actualites")
    assert r.status_code == 200
    assert "Faille A" in r.text and "Patch urgent" in r.text and "Important" in r.text and 'href="https://ex.fr/1"' in r.text
    r = client.get("/actualites/sources")
    assert r.status_code == 200 and "Cybersécurité" in r.text and "le site répond 404 (2 fois de suite)" in r.text


def test_ajouter_un_sujet_avec_recherche(client, monkeypatch):
    sujets, flux, appels = [], [], []
    monkeypatch.setattr(db, "sujets", lambda: sujets)

    def ajouter(nom, description, mots, notifier):
        sujets.append({"id": 4, "nom": nom, "description": description, "mots_cles": mots, "notifier": notifier,
                       "recherche_id": None})
        return 4
    monkeypatch.setattr(db, "sujet_ajouter", ajouter)
    monkeypatch.setattr(db, "flux_ajouter", lambda url, titre, site, sujet_id=None, notifier=False:
                        flux.append((url, sujet_id)) or 1)
    monkeypatch.setattr(db, "articles_retrier", lambda jours=7: appels.append("retri") or 0)
    r = client.post("/actualites/sujets", data={"nom": "Apple", "mots_cles": "iPhone, iOS ; iphone",
                                                 "recherche": "true", "notifier": "true"}, follow_redirects=False)
    assert r.status_code == 303
    assert sujets[0]["mots_cles"] == ["iPhone", "iOS"]
    assert flux[0][1] == 4 and flux[0][0].startswith("https://news.google.com/rss/search?q=iPhone+OR+iOS")
    assert appels == ["retri"]


def test_ajouter_un_flux_illisible(client, monkeypatch):
    monkeypatch.setattr(ac, "decouvrir", lambda url: (_ for _ in ()).throw(ac.FluxIllisible("aucun flux RSS trouvé")))
    monkeypatch.setattr(db, "sujets", lambda: [])
    monkeypatch.setattr(db, "flux_liste", lambda: [])
    r = client.post("/actualites/flux", data={"url": "exemple.fr"})
    assert "Flux non ajouté : aucun flux RSS trouvé." in r.text and 'value="exemple.fr"' in r.text


def test_preferences_notifications_gardent_les_actualites(client):
    client.post("/notifications/reglages", data={"heure": "18:00", "silence_debut": "22:00", "silence_fin": "07:30",
                                                 "rappel": "true"}, follow_redirects=False)
    assert json.loads(client.etat["notifications"])["actualites"] is False
    assert notifications.DEFAUT["actualites"] is True


def test_passage_en_sisr_garde_les_sujets_modifies(monkeypatch):
    etat = {"actualites_init": "1"}
    sujets = [{"id": 1, "nom": "Cybersécurité", "description": ac.SUJETS_DEFAUT[0][1]},
              {"id": 2, "nom": "Développement", "description": ac.ANCIENS_DEFAUT["Développement"]},
              {"id": 3, "nom": "Réseaux et systèmes", "description": "Ma version à moi"}]
    retires, ajoutes = [], []
    monkeypatch.setattr(db, "etat_valeur", etat.get)
    monkeypatch.setattr(db, "etat_ecrire", lambda k, v: etat.update({k: v}))
    monkeypatch.setattr(db, "sujets", lambda: sujets)
    monkeypatch.setattr(db, "sujet_supprimer", retires.append)
    monkeypatch.setattr(db, "sujet_ajouter", lambda nom, d, m: ajoutes.append(nom))
    monkeypatch.setattr(db, "articles_retrier", lambda: 0)
    ac.initialiser()
    assert retires == [2]   # « Réseaux et systèmes » a été modifié : gardé
    assert ajoutes == ["Cybersécurité", "Réseaux", "Systèmes et virtualisation"]
    ac.initialiser()        # une seule fois
    assert retires == [2]
