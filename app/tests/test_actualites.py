import datetime as dt
import json

import httpx
import pytest

from claunote import actualites as ac, agent, db, main, notifications
from tests.test_site import anonyme, client  # noqa: F401  (fixtures du site)

# Relative à aujourd'hui : le code n'envoie à l'IA que les articles de moins de FRAIS jours (date figée = tests cassés 14 j plus tard)
MAINTENANT = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
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
        monkeypatch.setattr(db, "sujets", lambda **k: self.sujets_)
        monkeypatch.setattr(db, "articles_a_trier", lambda limite=200: [a for a in self.articles_ if a["tri"] == "attente"])
        monkeypatch.setattr(db, "articles_trier", lambda r: self.tries.extend(r))
        monkeypatch.setattr(db, "articles_a_annoncer", lambda: self.articles_)
        self.envoyees = set()
        monkeypatch.setattr(db, "alerte_deja_envoyee", lambda cles, cves, **k: bool(self.envoyees & set(cles)))
        monkeypatch.setattr(db, "articles_annonces", lambda ids: self.annonces.extend(ids))
        monkeypatch.setattr(db, "abonnements_de", lambda compte_id: [{"endpoint": "x"}] if self.abonnes else [])
        monkeypatch.setattr(db, "notifier", lambda *a, **k: self.notifs.append(a) or True)
        monkeypatch.setattr(db, "etat_valeur", lambda k: None)
        monkeypatch.setattr(db, "formations_actuelles", lambda: ["BTS Services informatiques aux organisations"])
        monkeypatch.setattr(db, "alertes_recentes", lambda jours=7: ["fuite-france-travail"])
        monkeypatch.setattr(db, "evenements_recents", lambda jours=3, limite=80: ["fuite-france-travail", "proxmox-9-1"])
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
        return lire('Voilà : [{"id": 1, "sujets": ["S1", "S9"], "score": 0.9, "resume": "Faille exploitée : patcher",'
                    ' "titre": " « Exchange : faille exploitée, patch disponible »",'
                    ' "alerte": true, "evenement": "Exchange CVE-2026-1"},'
                    ' {"id": 2, "sujets": [], "score": 0.05, "alerte": true, "titre": "Promo"}, {"id": 99, "sujets": ["S1"], "score": 1}]'), "haiku"
    monkeypatch.setattr(agent, "demander", demander)
    assert ac.trier(lambda: True) == 2
    assert vus["tache"] == "actualites" and vus["ids"] == [1, 2] and "S1 — Cybersécurité : Failles" in vus["prompt"]
    # l'IA réutilise l'étiquette d'une information déjà vue (alertes d'abord, sans doublon)
    assert "fuite-france-travail, proxmox-9-1" in vus["prompt"]
    assert b.tries == [{"id": 1, "tri": "ia", "sujets": [7], "score": 0.9, "pourquoi": "Faille exploitée : patcher",
                        "titre_ia": "Exchange : faille exploitée, patch disponible",
                        "alerte": True, "evenement": "exchange-cve-2026-1", "notion": None},
                       {"id": 2, "tri": "ia", "sujets": [], "score": 0.05, "pourquoi": None,   # hors sujet : jamais d'alerte
                        "titre_ia": None, "alerte": False, "evenement": None, "notion": None}]


def test_tri_etiquette_et_notion_meme_sans_alerte(monkeypatch):
    b = Base(SUJETS, [article(1, "Proxmox VE 9.1 est sorti")])
    b.brancher(monkeypatch)
    monkeypatch.setattr(agent, "demander", lambda tache, prompt, cwd, entree=None, timeout=0, lire=None, **k: (lire(
        '[{"id": 1, "sujets": ["S1"], "score": 0.6, "resume": "Nouvelle version", "alerte": false,'
        ' "evenement": "Proxmox 9.1", "notion": " « virtualisation »."}]'), "haiku"))
    ac.trier(lambda: True)
    assert b.tries[0]["evenement"] == "proxmox-9-1" and b.tries[0]["notion"] == "virtualisation"
    assert b.tries[0]["alerte"] is False


def test_tri_sans_claude_attend_puis_mots_cles(monkeypatch):
    recent = article(1, "Faille dans Exchange", cree_le=dt.datetime.now(dt.timezone.utc))
    ancien = article(2, "Python 4 et faille", cree_le=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=4))
    b = Base(SUJETS, [recent, ancien])
    b.brancher(monkeypatch)
    monkeypatch.setattr(agent, "demander", lambda *a, **k: pytest.fail("Claude en pause : pas d'appel"))
    ac.trier(lambda: False)
    # le récent attend le retour de Claude ; celui qui attend depuis 4 h passe aux mots-clés
    assert b.tries == [{"id": 2, "tri": "mots_cles", "sujets": [7, 9], "score": None, "pourquoi": None,
                        "alerte": False, "evenement": None, "notion": None, "titre_ia": None}]


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


def test_seules_les_alertes_sonnent_une_fois_par_evenement(monkeypatch):
    b = Base(SUJETS, [
        article(1, "Fuite chez France Travail", tri="ia", sujets=[7], score=0.95, alerte=True,
                evenement="fuite-france-travail", pourquoi="43 millions de comptes exposés"),
        article(2, "France Travail piraté", tri="ia", sujets=[7], score=0.9, alerte=True,
                evenement="fuite-france-travail", source="Numerama"),               # même événement, autre site
        article(3, "Faille Exchange", tri="ia", sujets=[7], score=0.85),             # important mais pas une alerte
        article(4, "Python 4", tri="ia", sujets=[9], score=0.95, alerte=True, evenement="python-4"),  # sujet muet
        article(5, "Faille mots-clés", tri="mots_cles", sujets=[7]),                 # sans IA : ne sonne pas
        article(6, "Vieille alerte", tri="ia", sujets=[7], alerte=True, evenement="vieux",
                publie_le=MAINTENANT - dt.timedelta(days=5)),                         # trop vieux
        article(7, "Alerte CERT", tri="ia", sujets=[], score=0.5, source="CERT-FR", flux_notifier=True),
    ])
    b.brancher(monkeypatch)
    assert ac.annoncer(MAINTENANT) == 2
    assert b.notifs == [
        ("actu-alerte:fuite-france-travail", "🚨 Fuite chez France Travail", "43 millions de comptes exposés (Ex)",
         "/actualites?article=1#a1"),
        ("actu:ex.fr/7", "Alerte CERT", "CERT-FR", "/actualites?article=7#a7")]
    assert sorted(b.annonces) == [1, 2, 3, 4, 5, 6, 7]   # tout est décidé une fois pour toutes


def test_alerte_apparue_apres_coup_sonne_quand_meme(monkeypatch):
    """Cas réel : faille Cisco dans « L'essentiel » sans notification (article déjà décidé avant que l'IA en
    fasse une alerte : relu après un changement de sujet, ou premier relevé d'un nouveau flux Google)."""
    cisco = article(1, "Cisco Expressway : faille critique CVE-2026-76466 exploitée", tri="ia", sujets=[7],
                    score=0.95, alerte=True, evenement="cisco-faille-critique", annonce=True,
                    source="Google Actualités : Réseaux")
    vieux_flux = article(2, "Nouveauté d'un flux suivi", tri="ia", sujets=[], score=0.5, flux_notifier=True,
                         annonce=True)   # déjà décidé : ne resonne pas
    b = Base(SUJETS, [cisco, vieux_flux])
    b.brancher(monkeypatch)
    assert ac.annoncer(MAINTENANT) == 1
    assert b.notifs[0][0] == "actu-alerte:cve-2026-76466" and b.notifs[0][1].startswith("🚨 Cisco")
    assert b.annonces == []   # rien de neuf à marquer
    # Une fois partie, elle ne repart plus, même relayée par un autre site sous une autre étiquette
    b.envoyees.add("actu-alerte:cve-2026-76466")
    b.articles_.append(article(3, "Cisco corrige la CVE-2026-76466", tri="ia", sujets=[7], score=0.9, alerte=True,
                               evenement="cisco-expressway-cve"))
    b.notifs.clear()
    assert ac.annoncer(MAINTENANT) == 0 and b.notifs == []


def test_cles_alerte_par_cve_sinon_etiquette():
    a = article(1, "Faille CVE-2026-1111 et cve-2026-2222", evenement="fortinet")
    assert ac._cles_alerte(a) == (["actu-alerte:cve-2026-1111", "actu-alerte:cve-2026-2222"],
                                  ["CVE-2026-1111", "CVE-2026-2222"])
    assert ac._cles_alerte(article(2, "Fuite géante", evenement="fuite-x")) == (["actu-alerte:fuite-x"], [])


def test_sans_appareil_rien_ne_sonne_mais_tout_est_note(monkeypatch):
    b = Base(SUJETS, [article(1, "Faille A", tri="ia", sujets=[7], score=0.9, alerte=True)], abonnes=False)
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
    monkeypatch.setattr(db, "sujets", lambda **k: [{**SUJETS[0], "n": 1, "recherche_id": None, "recherche_erreur": None}])
    monkeypatch.setattr(db, "flux_liste", lambda **k: [{"id": 1, "titre": "Ex", "url": "https://ex.fr/feed",
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
    monkeypatch.setattr(db, "sujets", lambda **k: sujets)

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
    monkeypatch.setattr(db, "sujets", lambda **k: [])
    monkeypatch.setattr(db, "flux_liste", lambda **k: [])
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
    monkeypatch.setattr(db, "sujets", lambda **k: sujets)
    monkeypatch.setattr(db, "sujet_supprimer", retires.append)
    monkeypatch.setattr(db, "sujet_ajouter", lambda nom, d, m: ajoutes.append(nom))
    monkeypatch.setattr(db, "articles_retrier", lambda: 0)
    monkeypatch.setattr(db, "sujet_maj", lambda i, **k: pytest.fail("descriptions déjà modifiées : gardées"))
    flux, suivis = [], [{"id": 9, "url": "https://korben.info/feed/", "site": "https://korben.info", "sujet_id": None,
                         "categorie": ""}]

    def flux_ajouter(url, titre, site, categorie=""):
        flux.append(titre)
        suivis.append({"id": len(suivis) + 10, "url": url, "site": site, "sujet_id": None, "categorie": categorie})
    monkeypatch.setattr(db, "flux_ajouter", flux_ajouter)
    monkeypatch.setattr(db, "flux_liste", lambda **k: suivis)
    ranges = {}
    monkeypatch.setattr(db, "flux_maj", lambda i, **k: ranges.update({i: k["categorie"]}))
    ac.initialiser()
    # nouveaux flux proposés une fois aux installations existantes ; un site déjà suivi (Korben) n'est pas doublé
    assert flux[:3] == ["Numerama", "01net", "Clubic"]
    assert "BleepingComputer" in flux and "Tech Café" in flux and "NAS Compares" in flux
    assert "Korben" not in flux and flux.count("Numerama") == 1 and "Le Monde Informatique" not in flux
    assert ranges == {9: "Tech généraliste"}   # les sources déjà suivies rangées dans leur catégorie
    assert retires == [2]   # « Réseaux et systèmes » a été modifié : gardé
    assert ajoutes == ["Cybersécurité", "Réseaux", "Systèmes et virtualisation"]
    ac.initialiser()        # une seule fois
    assert retires == [2]


# --- Plus intéressant : images, même info regroupée, point du jour, lien avec les cours ----------------

RSS_IMAGES = """<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>T</title>
<item><title>A</title><link>https://ex.fr/a</link><media:thumbnail url="https://cdn.ex.fr/a.jpg"/></item>
<item><title>B</title><link>https://ex.fr/b</link><enclosure url="https://cdn.ex.fr/b.png" type="image/png" length="1"/></item>
<item><title>C</title><link>https://ex.fr/dossier/c</link>
  <description>&lt;img src="https://feeds.feedburner.com/~r/pixel.gif"&gt;&lt;img src="../img/c.webp"&gt;Texte</description></item>
<item><title>D</title><link>https://ex.fr/d</link><enclosure url="https://cdn.ex.fr/d.mp3" type="audio/mpeg" length="1"/></item>
</channel></rss>"""


def test_image_des_articles():
    _, items = ac.lire_flux(RSS_IMAGES.encode())
    assert [i["image"] for i in items] == ["https://cdn.ex.fr/a.jpg", "https://cdn.ex.fr/b.png",
                                          "https://ex.fr/img/c.webp", ""]   # pixel espion et son : ignorés


def carte(i, **k):
    return {**article(i, f"Article {i}", tri="ia", score=0.6, source=f"Site{i}"), "lu": False, "noms_sujets": [],
            "image": "", "notion": None, "evenement": None, "alerte": False, **k}


def test_regrouper_la_meme_information():
    cartes = ac.regrouper([carte(1, evenement="fuite-x", noms_sujets=["Cybersécurité"]),
                           carte(2), carte(3, evenement="fuite-x", alerte=True, score=0.95, lu=True,
                                           image="https://cdn/x.jpg", notion="RGPD")])
    assert [c["id"] for c in cartes] == [1, 2]
    tete = cartes[0]
    assert [x["source"] for x in tete["aussi"]] == ["Site3"]
    assert tete["alerte"] and tete["score"] == 0.95 and not tete["lu"]       # une source pas lue : carte non lue
    assert tete["image_id"] == 3 and tete["notion"] == "RGPD"                 # l'image d'un autre site sert


class Point:
    def __init__(self, monkeypatch, candidats, etat=None):
        self.etat = dict(etat or {})
        self.appels = []
        monkeypatch.setattr(db, "etat_valeur", self.etat.get)
        monkeypatch.setattr(db, "etat_ecrire", lambda k, v: self.etat.update({k: v}))
        monkeypatch.setattr(db, "articles_pour_point", lambda h, s, n: candidats)
        monkeypatch.setattr(db, "formations_actuelles", lambda: [])
        monkeypatch.setattr(ac.local, "reglages", lambda: {"mode": "local_d_abord"})

        def demander(tache, prompt, cwd, entree=None, timeout=0, lire=None, **k):
            self.appels.append((tache, [json.loads(l)["id"] for l in entree.splitlines()]))
            return lire('{"points": [{"texte": "Fuite chez X : 2 M de comptes.", "ids": [1, 3, 99]},'
                        ' {"texte": "Sans source", "ids": [42]}], "a_retenir": "Révise le RGPD."}'), "opus"
        monkeypatch.setattr(agent, "demander", demander)


def test_point_du_jour(monkeypatch):
    c = [carte(1, alerte=True), carte(3)]
    p = Point(monkeypatch, c)
    assert ac.faire_point(lambda: True, 10_000)
    assert p.appels == [("actualites_point", [1, 3])]
    point = ac.point()
    assert point["points"] == [{"texte": "Fuite chez X : 2 M de comptes.", "ids": [1, 3], "titre": "", "action": "",
                                "suite": False}]   # ids inventés retirés
    assert point["a_retenir"] is None and point["en_bref"] == ""           # ancienne forme : ignorée
    assert point["ids"] == [1, 3] and point["modele"] == "opus"
    assert not ac.faire_point(lambda: True, 10_060)      # rien de neuf : pas de nouvel appel
    c.append(carte(5))
    assert not ac.faire_point(lambda: True, 10_060)      # du neuf, mais le dernier point date d'une minute
    assert ac.faire_point(lambda: True, 10_000 + ac.POINT_TOUTES)
    c.append(carte(6, alerte=True))
    assert ac.faire_point(lambda: True, 10_000 + ac.POINT_TOUTES + 60)   # une alerte : tout de suite
    assert len(p.appels) == 3


def test_point_du_jour_sans_claude_ni_assez_d_articles(monkeypatch):
    p = Point(monkeypatch, [carte(1), carte(2)])
    assert not ac.faire_point(lambda: False, 10_000) and p.appels == []
    p = Point(monkeypatch, [carte(1)])
    assert not ac.faire_point(lambda: True, 10_000) and p.appels == []


def test_point_du_jour_illisible_garde_l_ancien(monkeypatch):
    ancien = json.dumps({"le": 1, "ids": [1], "points": [{"texte": "Hier", "ids": [1]}], "a_retenir": ""})
    p = Point(monkeypatch, [carte(1), carte(2)], {"actualites_point": ancien})
    monkeypatch.setattr(agent, "demander", lambda *a, **k: (k["lire"]("pas de JSON"), "opus"))
    assert not ac.faire_point(lambda: True, 50_000)
    assert ac.point()["points"][0]["texte"] == "Hier" and ac.point()["le"] == 50_000   # pas de nouvel essai à chaque tour


def _png(cote=100):
    from io import BytesIO

    from PIL import Image
    b = BytesIO()
    Image.new("RGB", (cote * 2, cote), "orange").save(b, "PNG")
    return b.getvalue()


def test_vignette_reduite_et_protegee():
    import httpx

    def repondre(r):
        if r.url.host == "redirige.fr":
            return httpx.Response(302, headers={"location": "http://192.168.1.1/admin.png"})
        if r.url.path == "/svg":
            return httpx.Response(200, content=b"<svg onload='x()'/>", headers={"content-type": "image/svg+xml"})
        if r.url.path == "/html":
            return httpx.Response(200, content=b"<html>", headers={"content-type": "image/png"})
        return httpx.Response(200, content=_png(600), headers={"content-type": "image/png"})
    c = httpx.Client(transport=httpx.MockTransport(repondre))
    public = lambda url: not url.startswith("http://192.168.")   # noqa: E731
    from io import BytesIO

    from PIL import Image
    jpeg = ac.telecharger_image("https://cdn.ex.fr/a.png", c, public)
    with Image.open(BytesIO(jpeg)) as im:
        assert im.format == "JPEG" and max(im.size) == ac.VIGNETTE_COTE
    assert ac.telecharger_image("https://redirige.fr/x.png", c, public) is None   # jamais le réseau local
    assert ac.telecharger_image("https://cdn.ex.fr/svg", c, public) is None
    assert ac.telecharger_image("https://cdn.ex.fr/html", c, public) is None      # pas vraiment une image
    assert ac.telecharger_image("javascript:alert(1)", c, public) is None


def test_hote_public():
    assert not ac._hote_public("http://127.0.0.1/x.png")
    assert not ac._hote_public("http://10.0.0.5/x.png")
    assert not ac._hote_public("http://[::1]/x.png")
    assert not ac._hote_public("http://localhost/x.png")


def test_page_actualites_essentiel_regroupement_et_cours(client, monkeypatch):
    import time
    lignes = [{**carte(1, sujets=[7], score=0.9, pourquoi="2 M de comptes", evenement="fuite-x", notion="RGPD",
                       image="https://cdn/x.jpg", noms_sujets=["Cybersécurité"]), "favori": False, "total": 3},
              {**carte(2, evenement="fuite-x", source="Numerama"), "favori": False, "total": 3},
              {**carte(4, notion="VLAN", pourquoi="Nouveau switch"), "favori": False, "total": 3}]
    monkeypatch.setattr(db, "articles", lambda *x, **k: (lignes, 3))
    monkeypatch.setattr(db, "sujets", lambda **k: [{**SUJETS[0], "n": 1, "recherche_id": None, "recherche_erreur": None}])
    monkeypatch.setattr(db, "flux_liste", lambda **k: [{"id": 1, "titre": "Ex", "url": "https://ex.fr/feed",
                                                     "releve_le": MAINTENANT}])
    monkeypatch.setattr(db, "recherche", lambda q, limite=30: [{"id": 55, "titre": "TP VLAN", "nom": "tp.pdf"}]
                        if q == "VLAN" else [])
    monkeypatch.setattr(db, "articles_par_ids", lambda ids: [{"id": 1, "titre": "Fuite", "lien": "https://ex.fr/1",
                                                              "lu": False, "alerte": True, "source": "Ex"}])
    client.etat["actualites_point"] = json.dumps({"le": time.time(), "ids": [1], "a_retenir": "Révise le RGPD.",
                                                  "points": [{"texte": "Fuite chez X.", "ids": [1]}]})
    r = client.get("/actualites")
    assert r.status_code == 200
    assert "L'essentiel" in r.text and "Fuite chez X." in r.text
    assert "Pour ton BTS" not in r.text and "Révise le RGPD." not in r.text   # outil pour toutes les formations
    assert r.text.index("Fuite chez X.") < r.text.index("Tout lu") < r.text.index('id="a1"')
    assert "Aussi chez" in r.text and ">Numerama</a>" in r.text and r.text.count('id="a2"') == 0   # regroupé
    assert 'href="/doc/55"' in r.text and "VLAN · ton cours" in r.text     # lié à ton cours
    assert 'src="/actualites/1/image"' in r.text
    # Pas de point sur les autres vues
    assert "L'essentiel" not in client.get("/actualites?vue=tout").text


def test_image_d_un_article(client, monkeypatch):
    monkeypatch.setattr(db, "article", lambda i: {"id": i, "image": "https://cdn/x.jpg"} if i == 1 else None)
    monkeypatch.setattr(ac, "vignette", lambda url: b"\xff\xd8jpeg")
    r = client.get("/actualites/1/image")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == b"\xff\xd8jpeg"
    assert client.get("/actualites/2/image").status_code == 404


# --- Toutes formations, notification du jour ----------------------------------------------

def test_consignes_sans_rien_de_propre_au_bts(monkeypatch):
    for texte in (ac.PROMPT, ac.PROMPT_POINT, *(d for _, d, _ in ac.SUJETS_DEFAUT)):
        assert "BTS" not in texte and "SISR" not in texte
    monkeypatch.setattr(db, "formations_actuelles", lambda: ["Terminale générale"])
    assert "Sa formation : Terminale générale." in ac.prompt(SUJETS)
    monkeypatch.setattr(db, "formations_actuelles", lambda: [])
    assert "Sa formation" not in ac.prompt(SUJETS)   # non renseignée : l'IA se fie aux sujets suivis


def test_descriptions_bts_rendues_neutres_si_intactes(monkeypatch):
    etat, maj = {}, {}
    monkeypatch.setattr(db, "etat_valeur", etat.get)
    monkeypatch.setattr(db, "etat_ecrire", lambda k, v: etat.update({k: v}))
    monkeypatch.setattr(db, "sujets", lambda **k: [{"id": 1, "nom": "Réseaux", "description": ac.DESCRIPTIONS_BTS["Réseaux"]},
                                               {"id": 2, "nom": "Systèmes et virtualisation", "description": "À moi"}])
    monkeypatch.setattr(db, "sujet_maj", lambda i, **k: maj.update({i: k["description"]}))
    ac.descriptions_neutres()
    assert maj == {1: ac.SUJETS_DEFAUT[1][1]}   # celle qui a été modifiée est gardée
    ac.descriptions_neutres()
    assert len(maj) == 1


class Quotidien:
    def __init__(self, monkeypatch, etat=None, nouvelles=0):
        self.etat, self.notifs = dict(etat or {}), []
        monkeypatch.setattr(db, "etat_valeur", self.etat.get)
        monkeypatch.setattr(db, "etat_ecrire", lambda k, v: self.etat.update({k: v}))
        monkeypatch.setattr(db, "nb_actualites_nouvelles", lambda: nouvelles)
        monkeypatch.setattr(db, "notifier", lambda *a: self.notifs.append(a) or True)
        monkeypatch.setattr(notifications, "preferences", lambda: {"actualites": True})


def paris(jour, h, m=0):
    return dt.datetime(2026, 11, jour, h, m, tzinfo=notifications.PARIS)


def test_heure_du_jour_apprise_de_tes_lectures(monkeypatch):
    q = Quotidien(monkeypatch)
    assert ac.heure_quotidienne()[0] == ac.QUOTIDIEN_DEFAUT          # pas encore d'habitudes : 7 h 30
    for jour in range(1, 7):
        ac.noter_lecture(paris(jour, 12, 10))
        ac.noter_lecture(paris(jour, 12, 40))                       # même heure, même jour : compté une fois
        if jour % 2:
            ac.noter_lecture(paris(jour, 21))
    assert json.loads(q.etat["actualites_lectures"])["2026-11-01"] == [12, 21]
    heure, pourquoi = ac.heure_quotidienne()
    assert heure == dt.time(12, 0) and "vers 12 h (6 jours sur 6)" in pourquoi
    ac.noter_lecture(paris(30, 8))                                   # plus de 28 jours : oubliés
    assert "2026-11-01" not in json.loads(q.etat["actualites_lectures"])
    q.etat["actualites"] = json.dumps({"quotidien": "18:15"})
    assert ac.heure_quotidienne() == (dt.time(18, 15), "heure choisie")
    q.etat["actualites"] = json.dumps({"quotidien": "non"})
    assert ac.heure_quotidienne()[0] is None


def test_notification_du_jour(monkeypatch):
    point = {"le": paris(20, 6).timestamp(), "ids": [1, 2],
             "points": [{"texte": "Faille FortiOS exploitée.", "ids": [1]}, {"texte": "Fuite chez X.", "ids": [2]},
                        {"texte": "Troisième.", "ids": [2]}]}
    q = Quotidien(monkeypatch, {"actualites_point": json.dumps(point)})
    assert not ac.quotidien(paris(20, 7, 0))                         # avant 7 h 30
    assert ac.quotidien(paris(20, 7, 31))
    assert q.notifs == [("actu-jour:2026-11-20", "📰 L'essentiel : 3 infos du jour",
                         "Faille FortiOS exploitée. · Fuite chez X.", "/actualites")]
    assert not ac.quotidien(paris(20, 9))                            # une seule par jour
    assert not ac.quotidien(paris(21, 11)) and len(q.notifs) == 1    # worker arrêté à l'heure prévue : pas le soir


def test_notification_du_jour_sans_point(monkeypatch):
    q = Quotidien(monkeypatch, nouvelles=4)
    assert ac.quotidien(paris(20, 8))
    assert q.notifs[0][1] == "📰 4 articles pour toi"
    q = Quotidien(monkeypatch, nouvelles=0)
    assert not ac.quotidien(paris(20, 8)) and q.notifs == []          # rien à dire : rien n'est envoyé


def test_reglage_notification_du_jour(client, monkeypatch):
    client.post("/actualites/reglages", data={"ia": "true", "notifs": "true", "quotidien": "heure", "heure": "06:45"},
                follow_redirects=False)
    assert json.loads(client.etat["actualites"])["quotidien"] == "06:45"
    client.post("/actualites/reglages", data={"quotidien": "heure", "heure": "pas une heure"}, follow_redirects=False)
    assert json.loads(client.etat["actualites"])["quotidien"] == "auto"


def test_flux_jamais_vers_le_reseau_local(monkeypatch):
    """Un flux ajouté par un compte ne peut pas faire lire au serveur une adresse du réseau local, même redirigé."""
    with pytest.raises(ac.FluxIllisible, match="réseau local"):
        ac.decouvrir("http://127.0.0.1:11434/api/tags")
    monkeypatch.setattr(ac, "_hote_public", lambda url: "10.0.0.1" not in url)

    def site(req):
        if req.url.host == "ex.fr":
            return httpx.Response(302, headers={"location": "http://10.0.0.1/admin"})
        return httpx.Response(200, text=RSS)
    c = httpx.Client(transport=httpx.MockTransport(site), follow_redirects=True,
                     event_hooks={"request": [ac._seulement_internet]})
    with pytest.raises(ac.FluxIllisible, match="réseau local"):
        ac.decouvrir("https://ex.fr/flux", c)


def test_easter_egg_vigie(client, monkeypatch):
    a = {**article(1, "Faille A"), "lu": False, "favori": False, "noms_sujets": [], "total": 1}
    monkeypatch.setattr(db, "articles", lambda *x, **k: ([a], 1))
    monkeypatch.setattr(db, "sujets", lambda **k: [])
    monkeypatch.setattr(db, "flux_liste", lambda **k: [{"id": 1, "titre": "Ex", "url": "https://ex.fr/feed",
                                                     "releve_le": MAINTENANT, "sujet": None, "sujet_id": None,
                                                     "notifier": False, "n": 1, "dernier": MAINTENANT,
                                                     "erreur": None, "echecs": 0, "site": ""}])
    r = client.get("/actualites")
    assert '<script src="/static/geste.js" defer>' in r.text   # 10 appuis (fichier à part)
    assert "vigie" not in r.text.lower() and "easter" not in r.text.lower()   # rien d'explicite dans la page
    geste = client.get("/static/geste.js").text
    assert "vigie" not in geste and "L2FjdHVhbGl0ZXMvdmlnaWU=" in geste   # adresse encodée
    r = client.get("/actualites/vigie")
    assert r.status_code == 200 and 'href="https://vigie.dix-neuf.ovh"' in r.text and "noindex" in r.text
    assert "frame-src 'self' blob:" in r.headers["content-security-policy"]   # pas d'iframe, CSP inchangée


def test_vigie_debloque_le_style(client):
    from claunote import apparence
    r = client.get("/reglages/apparence")
    assert r.status_code == 200 and 'value="sombre"' in r.text and 'value="vigie"' not in r.text   # pas encore trouvé
    client.post("/reglages/apparence", data={"mode": "sombre", "style": "vigie"})
    r = client.get("/reglages")
    assert 'data-theme="sombre" data-style="claunote"' in r.text   # Vigie refusé tant que l'easter egg n'est pas trouvé
    r = client.get("/actualites/vigie")
    assert "Style Vigie débloqué" in r.text and "Appliquer le style Vigie" in r.text
    assert 'data-theme="sombre" data-style="vigie"' in r.text   # la page cachée est déjà en Vigie sombre
    assert "Style Vigie débloqué" not in client.get("/actualites/vigie").text   # une seule fois
    r = client.post("/reglages/apparence", data={"mode": "sombre", "style": "vigie", "suite": "/actualites/vigie"},
                    follow_redirects=False)
    assert r.headers["location"] == "/actualites/vigie"
    r = client.get("/reglages/apparence")
    assert 'data-theme="sombre" data-style="vigie"' in r.text and "/static/logo-vigie.svg" in r.text
    assert 'value="vigie" checked' in r.text and '<meta name="theme-color" content="#232220">' in r.text
    client.post("/reglages/apparence", data={"mode": "clair", "style": "vigie"})
    r = client.get("/reglages/apparence")
    assert 'data-theme="sombre" data-style="vigie"' in r.text and 'value="clair" checked' in r.text   # Vigie : sombre seulement
    client.post("/reglages/apparence", data={"mode": "clair", "style": "claunote"})
    assert 'data-theme="clair" data-style="claunote"' in client.get("/reglages").text   # le choix revient
    client.post("/reglages/apparence", data={"mode": "nimporte", "style": "claunote"})
    assert apparence.lire({"id": 1})["mode"] == "auto"


def test_logo_vigie_garde_le_papillon():
    import pathlib
    statique = pathlib.Path(main.__file__).parent / "static"
    papillon = lambda f: [l for l in (statique / f).read_text().splitlines() if "<path" in l][0].split(' d="')[1]
    assert papillon("logo.svg") == papillon("logo-vigie.svg")


def test_apparence_suit_le_compte_sur_tous_les_appareils(client, anonyme):
    from fastapi.testclient import TestClient
    client.post("/reglages/apparence", data={"mode": "sombre", "style": "claunote"})
    autre = TestClient(main.app)   # un deuxième appareil, même compte
    autre.post("/connexion", data={"utilisateur": "admin", "mot_de_passe": "claunote"})
    r = autre.get("/reglages")
    assert 'data-theme="sombre"' in r.text   # enregistré sur le compte, pas dans le navigateur
    assert "h.dataset.theme !== ici.dataset.theme" in r.text   # reprise sans recharger à la main


def test_admin_met_son_fil_par_defaut(client, monkeypatch):
    monkeypatch.setattr(db, "sujets", lambda **k: [{**SUJETS[1], "n": 0, "recherche_id": None, "recherche_erreur": None}])
    monkeypatch.setattr(db, "flux_liste", lambda **k: [{"id": 1, "titre": "Ex", "url": "https://ex.fr/feed", "releve_le": None,
                                                     "sujet": None, "sujet_id": None, "notifier": False, "n": 0,
                                                     "dernier": None, "erreur": None, "echecs": 0, "site": "ex.fr"}])
    r = client.get("/actualites/sources")
    assert "Fil par défaut pour tout le monde" in r.text and "la sélection de départ" in r.text
    r = client.post("/actualites/defaut", data={"depuis": "mon-fil"}, follow_redirects=False)
    assert r.status_code == 303
    p = json.loads(client.etat["actualites_defaut"])
    # Le sujet « Dev » n'avait pas « Me notifier » : activé dans le fil par défaut (alertes urgentes)
    assert p["sujets"][0]["nom"] == "Dev" and p["sujets"][0]["notifier"] is True
    assert [f["url"] for f in p["flux"]] == ["https://ex.fr/feed"] and not p["depart"]
    assert "ton fil, copié le" in client.get("/actualites/sources").text


def test_admin_coupe_le_fil_par_defaut_permanent(client, monkeypatch):
    monkeypatch.setattr(db, "sujets", lambda **k: [])
    monkeypatch.setattr(db, "flux_liste", lambda **k: [])
    assert "Mon fil est le fil par défaut, en permanence" in client.get("/actualites/sources").text
    assert ac.defaut_en_permanence()
    client.post("/actualites/defaut/permanent", data={}, follow_redirects=False)
    assert client.etat["actualites_defaut_permanent"] == "non" and not ac.defaut_en_permanence()
    client.post("/actualites/defaut/permanent", data={"permanent": "true"}, follow_redirects=False)
    assert ac.defaut_en_permanence() and not json.loads(client.etat["actualites_defaut"])["depart"]


# --- Sources de ton lecteur RSS : catégories, podcasts, YouTube, recherche ------------------------------

PODCAST = """<?xml version="1.0"?><rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel><title>Tech Café</title><link>https://techcafe.fr</link>
<item><title>Épisode 400 : Windows 12</title><link>https://techcafe.fr/400</link>
  <enclosure url="https://cdn.ex/400.mp3" type="audio/mpeg" length="1"/><itunes:duration>1:02:03</itunes:duration></item>
</channel></rss>"""
YOUTUBE = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"
 xmlns:media="http://search.yahoo.com/mrss/"><title>Tech2Tech</title><link rel="alternate" href="https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv"/>
<entry><yt:videoId>xyz</yt:videoId><title>Installer Proxmox 9</title><link rel="alternate" href="https://www.youtube.com/watch?v=xyz"/>
<published>2026-11-20T10:00:00+00:00</published><media:group><media:thumbnail url="https://i.ytimg.com/vi/xyz/hq.jpg"/></media:group></entry>
</feed>"""


def test_episodes_de_podcast_et_videos_youtube():
    _, items = ac.lire_flux(PODCAST.encode())
    assert items[0]["media"] == "audio" and items[0]["media_url"] == "https://cdn.ex/400.mp3" and items[0]["duree"] == 3723
    _, items = ac.lire_flux(YOUTUBE.encode())
    assert items[0]["media"] == "video" and items[0]["image"] == "https://i.ytimg.com/vi/xyz/hq.jpg"
    _, items = ac.lire_flux(RSS.encode())
    assert items[0]["media"] == ""
    assert ac.duree("59:07") == 3547 and ac.duree("3600") == 3600 and ac.duree("n'importe") is None
    assert main._duree_txt(3723) == "1 h 02" and main._duree_txt(1500) == "25 min"


def test_trouver_un_podcast_ou_une_chaine_youtube():
    vus = []

    def repondre(r):
        vus.append(str(r.url))
        if r.url.host == "itunes.apple.com" and r.url.path == "/lookup":
            return httpx.Response(200, json={"results": [{"collectionName": "Tech Café", "feedUrl": "https://feeds.ex/tc2"}]})
        if r.url.host == "itunes.apple.com":
            return httpx.Response(200, json={"results": [
                {"collectionName": "Autre chose", "feedUrl": "https://autre.ex/feed"},
                {"collectionName": "Tech Café", "feedUrl": "https://feeds.ex/techcafe"}]})
        if r.url.host == "feeds.ex":
            return httpx.Response(200, content=PODCAST.encode())
        if r.url.path == "/feeds/videos.xml":
            return httpx.Response(200, content=YOUTUBE.encode())
        if r.url.host == "www.youtube.com":
            assert "SOCS=CAI" in r.headers.get("cookie", "")   # sans la page de consentement
            return httpx.Response(200, text='<html>{"externalId":"UCabcdefghijklmnopqrstuv"}</html>')
        return httpx.Response(404)
    c = httpx.Client(transport=httpx.MockTransport(repondre))
    url, infos, items = ac.decouvrir("podcast:Tech Cafe", c)   # sans accent : retrouvé quand même
    assert url == "https://feeds.ex/techcafe" and items[0]["media"] == "audio"
    url, infos, items = ac.decouvrir("https://podcasts.apple.com/fr/podcast/tech-cafe/id123456", c)
    assert url == "https://feeds.ex/tc2" and "lookup" in vus[-2] and "id=123456" in vus[-2]
    url, infos, items = ac.decouvrir("youtube.com/@Tech2Tech", c)
    assert url == ac.YOUTUBE_FLUX.format("UCabcdefghijklmnopqrstuv") and items[0]["media"] == "video"
    url, *_ = ac.decouvrir("youtube:Tech2Tech Mikaël", c)
    assert "results?search_query=Tech2Tech" in vus[-2] and url.endswith("UCabcdefghijklmnopqrstuv")
    url, *_ = ac.decouvrir("https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv", c)
    assert url.endswith("UCabcdefghijklmnopqrstuv")


def test_releve_d_un_podcast_par_son_nom_puis_reparation(monkeypatch):
    maj, ajouts = {}, []
    monkeypatch.setattr(db, "flux_maj", lambda i, **k: maj.update(k))
    monkeypatch.setattr(db, "flux_liste", lambda **k: [])
    monkeypatch.setattr(db, "articles_ajouter", lambda f, items, annonce: ajouts.extend(items) or len(items))

    def repondre(r):
        if r.url.host == "itunes.apple.com":
            return httpx.Response(200, json={"results": [{"collectionName": "Tech Café", "feedUrl": "https://feeds.ex/tc"}]})
        if r.url.host == "feeds.ex":
            return httpx.Response(200, content=PODCAST.encode())
        if r.url.path == "/ancien-flux":
            return httpx.Response(404)
        if r.url.path == "/nouveau-flux":
            return httpx.Response(200, content=RSS.encode())
        return httpx.Response(200, text='<link rel="alternate" type="application/rss+xml" href="/nouveau-flux">')
    c = httpx.Client(transport=httpx.MockTransport(repondre))
    flux = {"id": 4, "url": "podcast:Tech Café", "etag": None, "modifie": None, "releve_le": None, "echecs": 0,
            "titre": "Tech Café", "site": "", "sujet_id": None}
    assert ac.relever_un(flux, SUJETS, c) == 1
    assert maj["url"] == "https://feeds.ex/tc" and ajouts[0]["media"] == "audio" and maj["erreur"] is None
    # Le site a changé l'adresse de son flux : retrouvée sur sa page d'accueil, sans erreur
    maj.clear(), ajouts.clear()
    flux = {**flux, "url": "https://ex.fr/ancien-flux", "site": "https://ex.fr/", "titre": "Ex"}
    assert ac.relever_un(flux, SUJETS, c) == 2
    assert maj["url"] == "https://ex.fr/nouveau-flux" and maj["erreur"] is None and maj["echecs"] == 0
    # Pas plus d'une tentative toutes les 6 erreurs
    maj.clear()
    assert ac.relever_un({**flux, "echecs": 1}, SUJETS, c) == 0 and maj["erreur"] == "le site répond 404"


def test_recherche_motifs_et_surlignage():
    assert db.motifs_recherche('Faille "Active Directory" -windows ou sécurité') == (
        ["%faille%", "%active directory%", "%securite%"], ["%windows%"])
    assert db.motifs_recherche("100%_x")[0] == ["%100\\%\\_x%"]
    assert str(main.marquer("La <b>Sécurité</b> des failles", "securite faille")) == \
        "La &lt;b&gt;<mark>Sécurité</mark>&lt;/b&gt; des <mark>faille</mark>s"
    assert str(main.marquer("<i>x</i>", "")) == "&lt;i&gt;x&lt;/i&gt;"


def test_page_recherche_filtres_et_categories(client, monkeypatch):
    a = {**article(1, "Faille critique Fortinet", tri="ia", sujets=[], score=0.9, pourquoi="Mettre à jour FortiOS"),
         "lu": False, "favori": False, "noms_sujets": [], "total": 1, "media": "audio", "media_url": "https://cdn.ex/1.mp3",
         "duree": 1500}
    appels = []
    monkeypatch.setattr(db, "articles", lambda *x, **k: appels.append((x, k)) or ([a], 1))
    monkeypatch.setattr(db, "sujets", lambda **k: [])
    monkeypatch.setattr(db, "flux_liste", lambda **k: [
        {"id": 1, "titre": "Tech Café", "url": "https://feeds.ex/tc", "releve_le": MAINTENANT, "sujet": None, "sujet_id": None,
         "notifier": False, "n": 3, "non_lus": 2, "dernier": MAINTENANT, "erreur": None, "echecs": 0, "site": "",
         "categorie": "Podcast", "media": "audio"},
        {"id": 2, "titre": "IT-Connect", "url": "https://www.it-connect.fr/feed/", "releve_le": MAINTENANT, "sujet": None,
         "sujet_id": None, "notifier": False, "n": 5, "non_lus": 1, "dernier": MAINTENANT, "erreur": None, "echecs": 0,
         "site": "", "categorie": "IT", "media": ""}])
    r = client.get("/actualites?q=fortinet&categorie=Podcast&type=audio&periode=7j&non_lus=1&tri=pertinence")
    assert r.status_code == 200
    (vue, *_), k = appels[-1]
    assert vue == "tout" and k == {"q": "fortinet", "categorie": "Podcast", "media": "audio", "jours": 7, "non_lus": True,
                                   "tri": "pertinence"}
    assert "<mark>Fortinet</mark>" in r.text and "1</strong> résultat pour « fortinet »" in r.text
    assert 'data-audio="https://cdn.ex/1.mp3"' in r.text and "25 min" in r.text
    assert 'href="/actualites?categorie=Podcast"' in r.text and "L'essentiel" not in r.text
    r = client.get("/actualites")
    (vue, *_), k = appels[-1]
    assert vue == "pour_toi" and k["q"] == "" and k["categorie"] is None
    # Le formulaire tel que le navigateur l'envoie sans rien choisir : champs vides = tous, pas d'erreur brute
    r = client.get("/actualites?q=fortinet&categorie=&flux=&type=&periode=&tri=&sujet=&page=&article=")
    assert r.status_code == 200 and "int_parsing" not in r.text
    (vue, sujet, flux, *_), k = appels[-1]
    assert vue == "tout" and sujet is None and flux is None and k["q"] == "fortinet" and k["categorie"] is None \
        and k["media"] is None and k["jours"] is None and k["non_lus"] is False
    r = client.get("/actualites?flux=2&page=abc&non_lus=on")
    assert r.status_code == 200
    (vue, sujet, flux, *_), k = appels[-1]
    assert flux == 2 and k["non_lus"] is True
    assert 'el.disabled = true' in r.text   # les filtres vides ne partent plus dans l'adresse
    r = client.get("/actualites/sources")
    assert r.status_code == 200
    assert r.text.index(">IT<") < r.text.index(">Podcast<") and "🎙 Tech Café" in r.text


def test_ranger_une_source_dans_une_categorie(client, monkeypatch):
    maj = {}
    monkeypatch.setattr(db, "flux_maj", lambda i, **k: maj.update({i: k}))
    r = client.post("/actualites/flux/3/categorie", data={"categorie": "  Sécurité   réseau "}, follow_redirects=False)
    assert r.status_code == 303 and maj == {3: {"categorie": "Sécurité réseau"}}


def test_selection_de_depart_rangee_par_categories():
    p = ac._depart()
    cats = {f["titre"]: f["categorie"] for f in p["flux"]}
    assert cats["BleepingComputer"] == "Sécurité" and cats["4sysops"] == "IT" and cats["Tech Café"] == "Podcast"
    assert cats["Tech2Tech (YouTube)"] == "YouTube" and cats["NAS Compares"] == "NAS" and cats["Linuxtricks"] == "Linux"
    assert len({f["url"] for f in p["flux"]}) == len(p["flux"]) and all(f["categorie"] for f in p["flux"])
    assert ac.categorie_connue("https://www.it-connect.fr/feed") == "IT"
    assert ac.categorie_connue("https://inconnu.ex/feed") == ""


def test_essentiel_renforce(monkeypatch):
    c = [carte(1, alerte=True, sources=3), carte(3)]
    p = Point(monkeypatch, c, {"actualites_point": json.dumps(
        {"le": 9_000, "ids": [1], "points": [{"titre": "Fuite X", "texte": "Hier", "ids": [1]}]})})
    vus = {}

    def demander(tache, prompt, cwd, entree=None, timeout=0, lire=None, **k):
        vus.update(prompt=prompt, entree=[json.loads(l) for l in entree.splitlines()])
        return lire('{"en_bref": "Fuite massive chez X : change ton mot de passe.", "points": [{"titre": "Fuite X",'
                    ' "texte": "2 M de comptes.", "action": "Changer son mot de passe", "suite": true, "ids": [1, 3]}],'
                    ' "a_retenir": {"notion": "RGPD", "texte": "Une fuite se déclare à la CNIL sous 72 h."}}'), "opus"
    monkeypatch.setattr(agent, "demander", demander)
    assert ac.faire_point(lambda: True, 9_000 + ac.POINT_TOUTES)
    assert "L'essentiel précédent disait : Fuite X." in vus["prompt"] and vus["entree"][0]["sources"] == 3
    point = ac.point()
    assert point["en_bref"] == "Fuite massive chez X : change ton mot de passe."
    assert point["points"] == [{"titre": "Fuite X", "texte": "2 M de comptes.", "action": "Changer son mot de passe",
                                "suite": True, "ids": [1, 3]}]
    assert point["a_retenir"] == {"notion": "RGPD", "texte": "Une fuite se déclare à la CNIL sous 72 h."}
    assert p.appels == []   # demander remplacé


def test_notification_du_jour_en_une_phrase(monkeypatch):
    etat = {"actualites_point": json.dumps({"le": dt.datetime(2026, 11, 25, 6, tzinfo=dt.timezone.utc).timestamp(),
                                            "en_bref": "Une phrase.", "points": [{"texte": "A", "ids": [1]}]})}
    monkeypatch.setattr(db, "etat_valeur", etat.get)
    monkeypatch.setattr(db, "etat_ecrire", lambda k, v: etat.update({k: v}))
    monkeypatch.setattr(ac.notifications, "preferences", lambda: {})
    envoyees = []
    monkeypatch.setattr(ac, "notifier", lambda base, titre, corps, url: envoyees.append(corps) or True)
    assert ac.quotidien(dt.datetime(2026, 11, 25, 8, tzinfo=notifications.PARIS))
    assert envoyees == ["Une phrase."]


def test_visite_precedente(monkeypatch):
    etat = {}
    monkeypatch.setattr(db, "etat_valeur", etat.get)
    monkeypatch.setattr(db, "etat_ecrire", lambda k, v: etat.update({k: v}))
    assert ac.visite(maintenant=1_000) is None                         # première visite
    assert ac.visite(maintenant=1_600) is None                         # 10 min après : même visite
    assert ac.visite(maintenant=1_600 + ac.VISITE_PAUSE + 1).timestamp() == 1_600   # nouvelle visite
    assert ac.visite(noter=False, maintenant=99_999).timestamp() == 1_600           # rafraîchissement : rien ne bouge
    assert ac.visite(maintenant=1_600 + ac.VISITE_PAUSE + 60).timestamp() == 1_600  # toujours la même visite


def test_regrouper_compte_les_sources_et_le_nouveau():
    depuis = MAINTENANT - dt.timedelta(minutes=5)
    cartes = ac.regrouper([carte(1, evenement="x", cree_le=MAINTENANT - dt.timedelta(hours=2)),
                           carte(2, evenement="x", cree_le=MAINTENANT), carte(3, evenement="x", source="Site1"),
                           carte(4, cree_le=MAINTENANT - dt.timedelta(hours=1))], depuis)
    assert [(c["id"], c["sources"], c["ids"], c["nouveau"]) for c in cartes] == [(1, 2, [1, 2, 3], True), (4, 1, [4], False)]


def test_page_coup_d_oeil(client, monkeypatch):
    import time
    appels = {}
    lignes = [{**carte(1, sujets=[7], score=0.9, pourquoi="2 M de comptes", evenement="fuite-x",
                       titre_ia="Fuite chez X : 2 millions de comptes", cree_le=dt.datetime.now(dt.timezone.utc)),
               "favori": False, "total": 2, "titre": "Vous ne devinerez jamais qui s'est fait pirater"},
              {**carte(2, evenement="fuite-x", source="Numerama"), "favori": False, "total": 2}]

    def articles(*x, **k):
        appels.update(k)
        return lignes, 2
    monkeypatch.setattr(db, "articles", articles)
    monkeypatch.setattr(db, "sujets", lambda **k: [])
    monkeypatch.setattr(db, "flux_liste", lambda **k: [{"id": 1, "titre": "Ex", "url": "https://ex.fr/feed", "releve_le": MAINTENANT}])
    monkeypatch.setattr(db, "recherche", lambda q, limite=30: [{"id": 55, "titre": "Cours RGPD", "nom": "rgpd.pdf"}]
                        if q == "RGPD" else [])
    monkeypatch.setattr(db, "articles_par_ids", lambda ids: [
        {"id": 1, "titre": "Fuite", "lien": "https://ex.fr/1", "lu": False, "alerte": True, "source": "Ex",
         "cree_le": dt.datetime.now(dt.timezone.utc)},
        {"id": 2, "titre": "Fuite", "lien": "https://nu.fr/2", "lu": False, "alerte": False, "source": "Numerama",
         "cree_le": MAINTENANT}])
    monkeypatch.setattr(db, "tendances", lambda: [{"notion": "RGPD", "n": 4, "sources": 3, "avant": 0, "alerte": False}])
    monkeypatch.setattr(db, "actualites_depuis", lambda depuis, limite=5: {
        "nouveaux": 5, "alertes": 1, "histoires": 3, "non_lus": 5,
        "a_la_une": [{"id": 1, "titre": "x", "titre_ia": "Fuite chez X", "lien": "https://ex.fr/1", "alerte": True,
                      "lu": False, "source": "Ex", "sources": 2},
                     {"id": 9, "titre": "y", "titre_ia": "Proxmox 9.1 est sorti", "lien": "https://ex.fr/9", "alerte": False,
                      "lu": False, "source": "Ex", "sources": 1, "evenement": "proxmox"}]})
    client.etat["actualites_visite"] = json.dumps({"derniere": time.time() - 3 * 86400, "precedente": None})
    client.etat["actualites_point"] = json.dumps({
        "le": time.time(), "ids": [1, 2], "en_bref": "Grosse fuite chez X.",
        "points": [{"titre": "Fuite X", "texte": "2 M de comptes.", "action": "Changer son mot de passe", "ids": [1, 2]}],
        "a_retenir": {"notion": "RGPD", "texte": "Déclarer à la CNIL sous 72 h."}})
    r = client.get("/actualites")
    assert r.status_code == 200 and appels["tri"] == "importance"
    t = r.text
    assert "<strong>+5</strong> depuis ta visite" in t and 'id="depuis"' not in t   # dans l'en-tête de L'essentiel
    assert "30 s de lecture" not in t and 'class="recherche-actu" method="get" action="/actualites" role="search" hidden' in t
    assert "Sujets et catégories" not in t or "panneau-filtres" in t
    assert "Grosse fuite chez X." in t and "<strong>Fuite X</strong> : 2 M de comptes." in t
    assert "<strong>À faire</strong> : Changer son mot de passe" in t and "2 sources" in t
    assert "Pour tes cours · RGPD" in t and 'href="/doc/55"' in t
    assert "Aussi pendant ton absence" in t and "Proxmox 9.1 est sorti" in t   # parti 3 jours
    assert 'data-lu="1" title="x"' not in t                                 # déjà dans L'essentiel : pas répété
    assert "Ça monte" in t and "q=RGPD&amp;periode=24h" in t
    assert "Fuite chez X : 2 millions de comptes" in t and "Titre d'origine : Vous ne devinerez jamais" in t
    assert 'data-histoire="1"' in t and "Le plus important" in t and 'data-affichage="titres"' in t
    assert t.index("Grosse fuite chez X.") < t.index("Aussi pendant ton absence") < t.index("Ça monte") < t.index('id="a1"')
    # « Le plus récent » choisi : gardé dans les pages suivantes
    client.get("/actualites?vue=pour_toi&tri=recents")
    assert appels["tri"] == "recents"
    client.get("/actualites?vue=tout")
    assert appels["tri"] == "recents"


def test_histoire_lue(client, monkeypatch):
    monkeypatch.setattr(db, "histoire_lue", lambda i: [i, 8])
    assert client.post("/actualites/3/histoire-lue").json() == {"ok": True, "ids": [3, 8]}


def test_alerte_lue_reste_marquee_dans_l_essentiel():
    from pathlib import Path
    css = Path(ac.__file__).with_name("templates").joinpath("actualites.html").read_text()
    assert ".essentiel li.lu::before" not in css and ".essentiel li.lu:not(.alerte)::before" in css


def test_haut_de_page_allege(client, monkeypatch):
    monkeypatch.setattr(db, "sujets", lambda **k: [{**SUJETS[0], "n": 1, "recherche_id": None, "recherche_erreur": None}])
    monkeypatch.setattr(db, "flux_liste", lambda **k: [{"id": 1, "titre": "Ex", "url": "https://ex.fr/feed", "releve_le": MAINTENANT,
                                                     "categorie": "Linux", "non_lus": 44, "sujet_id": None}])
    t = client.get("/actualites").text
    # une seule rangée : Pour toi / Tout / Favoris, sujets et catégories repliés, sans compteurs
    assert '<details class="panneau-filtres">' in t and ">Linux</a>" in t and ">Linux<span" not in t
    assert t.index("panneau-filtres") > t.index(">Favoris")
    assert "<button type=\"button\" class=\"petit\" data-ouvrir-recherche" in t   # la recherche se déplie à la demande
    t = client.get(f"/actualites?sujet={SUJETS[0]['id']}").text
    assert 'class="actif">Cybersécurité</a>' in t and "class=\"petit\" data-ouvrir-recherche" not in t


def test_cache_des_vignettes_borne(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(ac, "VIGNETTES", tmp_path)
    for n in range(5):
        f = tmp_path / f"{n}.jpg"
        f.write_bytes(b"x" * 100)
        os.utime(f, (1000 + n, 1000 + n))
    assert ac.elaguer_vignettes(1000) == 0
    assert ac.elaguer_vignettes(300) == 3     # les plus anciennes, jusqu'à 80 % de la limite
    assert sorted(p.name for p in tmp_path.iterdir()) == ["3.jpg", "4.jpg"]


def test_vignettes_preparees_apres_le_releve(tmp_path, monkeypatch):
    """Le worker prépare les vignettes des nouveaux articles : la page les sert du cache, sans attendre les sites."""
    monkeypatch.setattr(ac, "VIGNETTES", tmp_path)
    ac._fichier_vignette("https://img.fr/deja.jpg").write_bytes(b"x")
    monkeypatch.setattr(db, "images_recentes", lambda limite: ["https://img.fr/deja.jpg", "https://img.fr/1.jpg",
                                                               "https://img.fr/2.jpg"])
    vues = []
    monkeypatch.setattr(ac, "telecharger_image", lambda url: vues.append(url) or b"\xff\xd8jpeg")
    assert ac.prechauffer_vignettes() == 2
    assert sorted(vues) == ["https://img.fr/1.jpg", "https://img.fr/2.jpg"]
    assert ac.vignette("https://img.fr/1.jpg") == b"\xff\xd8jpeg" and len(vues) == 2   # ensuite : depuis le cache
