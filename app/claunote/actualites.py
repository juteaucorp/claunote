"""Actualités : flux RSS que tu choisis, résumés et triés par l'IA selon tes sujets, alertes pour le rare et le grave.

    flux RSS / Atom ──┐                          ┌─► page Actualités : résumé en une phrase (« Pour toi », tout…)
    (sites ajoutés)   ├─► relevé toutes les ─► articles ─► tri par l'IA (Haiku, par lots de 40) ─┤
    Google Actualités ┘   30 min (ETag)      (dédup par lien)  sinon mots-clés                    ├─► notification si
    (recherche d'un sujet)                                                                        │   ALERTE (rare, grave)
                                                                                                  └─► « L'essentiel » :
                                                                         le point du jour en 3 à 5 phrases (Opus)
                                                                         + une notification par jour, à l'heure
                                                                           où tu lis d'habitude tes actualités

- Plus intéressant à lire : l'IA donne à chaque article une étiquette d'événement (la même info chez trois sites
  = une seule carte « aussi chez… ») et la notion de ta formation qu'il illustre (VLAN, RGPD…), reliée à ton
  cours sur ce sujet s'il y en a un ; l'image de l'article passe par le site (vignette réduite, jamais de
  chargement direct depuis un site tiers).
- Pour toutes les formations : rien n'est propre au BTS dans les consignes de l'IA ; elle s'appuie sur la formation
  renseignée (page Matières, formations.py) et sur tes sujets suivis.

- Dédup : un article = son lien nettoyé (sans utm_…, sans #) ; le même dans deux flux n'apparaît qu'une fois.
- Pas de spam : à son premier relevé, un flux n'annonce rien ; seule une « alerte » (jugée par l'IA : grosse
  fuite, faille critique exploitée dans un produit répandu, panne majeure) d'un sujet « à notifier » sonne, une
  seule fois par événement (étiquette commune aux articles qui en parlent) ; ou chaque nouveauté d'un flux
  « tout m'intéresse » ; puis la file commune (notifications.py) : silence, même texte < 20 h, plus de 3 = résumé.
- Sans Claude (limite, panne, réglage) : repérage par mots-clés (rien ne sonne), refait par l'IA à son retour.
- À chaque compte son fil : ses sources, ses sujets, ses articles lus ou favoris, son « Essentiel », ses réglages
  et ses notifications, sur ses appareils, selon ses heures de silence (le worker fait le tour compte par compte,
  sous la séparation de la base). Un compte sans forfait Claude reprend ce que l'IA a dit du même article chez
  un autre compte (résumé, alerte) et le range dans ses sujets par mots-clés : ses alertes sonnent quand même.
- Fil par défaut : tant qu'un compte n'a rien changé, son fil suit celui que l'admin a choisi pour tout le monde
  (sinon la sélection de départ), alertes activées sur chaque sujet. Il change une source ou un sujet : son fil
  devient le sien (« personnalisé ») ; un bouton le remet sur le fil par défaut.
"""
import datetime as dt
import hashlib
import html
import json
import re
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import httpx

from claunote import agent, db, local, notifications

RELEVE = 30 * 60          # secondes entre deux relevés des flux
LOT = 40                  # articles par appel à l'IA
FRAIS = 14                # jours : au-delà, un article n'est pas envoyé à l'IA (mots-clés seulement)
SANS_IA_APRES = 3 * 3600  # Claude absent plus longtemps : tri par mots-clés pour ne pas bloquer
SEUIL_IMPORTANT = 0.75   # badge « Important » sur la page (les notifications, elles, ne partent que pour une alerte)
AGE_NOTIF = 2             # jours : un article plus vieux ne sonne jamais (flux qui republie ses archives)
POINT_TOUTES = 3 * 3600   # « L'essentiel » refait au plus toutes les 3 h (ou tout de suite pour une alerte)
POINT_FENETRE = 24        # heures d'actualité prises en compte pour le point du jour
POINT_SEUIL = 0.6         # importance minimale d'un article pour entrer dans le point
POINT_MAX = 14            # articles proposés à l'IA pour le point
UA = "Mozilla/5.0 (compatible; Claunote/1.0; lecteur RSS personnel)"
DEFAUT = {"ia": True, "quotidien": "auto"}   # quotidien : "auto" (ton heure de lecture), "HH:MM" ou "non"
QUOTIDIEN_DEFAUT = dt.time(7, 30)   # heure de la notification du jour tant qu'on ne sait pas quand tu lis
QUOTIDIEN_JOURS = 28                # jours d'habitudes de lecture gardés
QUOTIDIEN_MIN = 5                   # jours de lecture qu'il faut pour choisir l'heure toute seule

# (adresse, nom, catégorie, page d'accueil du site). Une adresse « podcast:Nom » ou « youtube:Nom » est retrouvée au
# premier relevé (annuaire Apple Podcasts, page de la chaîne) ; un flux qui ne répond plus est recherché sur la page
# d'accueil du site (la plupart l'annoncent), il se répare tout seul.
FLUX_DEFAUT = [
    # Sécurité
    ("https://www.cert.ssi.gouv.fr/alerte/feed/", "CERT-FR : alertes de sécurité", "Sécurité", "https://www.cert.ssi.gouv.fr/"),
    ("https://www.cert.ssi.gouv.fr/actualite/feed/", "CERT-FR : actualités", "Sécurité", "https://www.cert.ssi.gouv.fr/"),
    ("https://www.bleepingcomputer.com/feed/", "BleepingComputer", "Sécurité", "https://www.bleepingcomputer.com/"),
    ("https://cyberguerre.numerama.com/feed/", "Cyberguerre", "Sécurité", "https://cyberguerre.numerama.com/"),
    ("https://feeds.feedburner.com/TheHackersNews", "The Hacker News", "Sécurité", "https://thehackernews.com/"),
    ("https://www.undernews.fr/feed", "UnderNews", "Sécurité", "https://www.undernews.fr/"),
    ("https://www.welivesecurity.com/fr/rss/feed/", "WeLiveSecurity", "Sécurité", "https://www.welivesecurity.com/fr/"),
    # Tech généraliste
    ("https://next.ink/feed/", "Next", "Tech généraliste", "https://next.ink/"),
    ("https://korben.info/feed", "Korben", "Tech généraliste", "https://korben.info/"),
    ("https://www.numerama.com/feed/", "Numerama", "Tech généraliste", "https://www.numerama.com/"),
    ("https://www.ginjfo.com/feed", "GinjFo", "Tech généraliste", "https://www.ginjfo.com/"),
    ("https://www.tomshardware.fr/feed/", "Tom's Hardware", "Tech généraliste", "https://www.tomshardware.fr/"),
    ("https://www.lemondeinformatique.fr/flux-rss/thematique/toutes-les-actualites/rss.xml", "Le Monde Informatique",
     "Tech généraliste", "https://www.lemondeinformatique.fr/"),
    ("https://www.01net.com/feed/", "01net", "Tech généraliste", "https://www.01net.com/"),
    ("https://www.clubic.com/feed/news.rss", "Clubic", "Tech généraliste", "https://www.clubic.com/"),
    # IT (admin systèmes et réseaux)
    ("https://www.it-connect.fr/feed/", "IT-Connect", "IT", "https://www.it-connect.fr/"),
    ("https://4sysops.com/feed/", "4sysops", "IT", "https://4sysops.com/"),
    ("https://blogmotion.fr/feed", "Blogmotion", "IT", "https://blogmotion.fr/"),
    ("https://lecrabeinfo.net/feed", "Le Crabe Info", "IT", "https://lecrabeinfo.net/"),
    ("https://www.malekal.com/feed/", "malekal.com", "IT", "https://www.malekal.com/"),
    ("https://www.tech2tech.fr/feed/", "Tech2Tech", "IT", "https://www.tech2tech.fr/"),
    # Linux
    ("https://www.linuxtricks.fr/rss.xml", "Linuxtricks", "Linux", "https://www.linuxtricks.fr/"),
    # Microsoft
    ("https://www.monwindows.com/feed", "MonWindows", "Microsoft", "https://www.monwindows.com/"),
    ("https://www.nextofwindows.com/feed", "Next of Windows", "Microsoft", "https://www.nextofwindows.com/"),
    ("https://www.windowscentral.com/feeds/all", "Windows Central", "Microsoft", "https://www.windowscentral.com/"),
    # NAS
    ("https://www.cachem.fr/feed/", "Cachem", "NAS", "https://www.cachem.fr/"),
    ("https://nascompares.com/feed/", "NAS Compares", "NAS", "https://nascompares.com/"),
    # Podcast (épisodes écoutables sur la page)
    ("podcast:De quoi j'me mail", "De quoi j'me mail", "Podcast", ""),
    ("podcast:Le rendez-vous Tech", "Le rendez-vous Tech", "Podcast", ""),
    ("podcast:Tech Café", "Tech Café", "Podcast", ""),
    # YouTube (une chaîne = un flux de ses vidéos)
    ("youtube:Tech2Tech Mikaël", "Tech2Tech (YouTube)", "YouTube", ""),
]
# Ajoutés après coup aux installations existantes (une fois), par nom
FLUX_AJOUTES = {"actualites_flux_2": ["Numerama", "01net"], "actualites_flux_3": ["Clubic"],
                # les sources de ton lecteur RSS (2026-11), rangées par catégories comme chez toi
                "actualites_flux_4": [t for _, t, _, _ in FLUX_DEFAUT
                                      if not t.startswith("CERT-FR") and t not in ("Le Monde Informatique", "01net", "Clubic")]}
SUJETS_DEFAUT = [   # sélection de départ (informatique), à changer sur la page Sources selon ta formation
    ("Cybersécurité", "Failles critiques à corriger, cyberattaques marquantes, rançongiciels, fuites de données, "
     "recommandations de l'ANSSI.",
     ["faille", "vulnérabilité", "CVE", "rançongiciel", "ransomware", "cyberattaque", "phishing", "hameçonnage",
      "ANSSI", "CERT-FR", "fuite de données", "zero-day", "correctif"]),
    ("Réseaux", "Équipements et protocoles réseau (Cisco, Fortinet, pfSense, routage, VLAN, Wi-Fi, IPv6, VPN, "
     "pare-feu) : nouveautés et failles qui comptent.",
     ["Cisco", "Fortinet", "FortiGate", "pfSense", "VLAN", "routeur", "commutateur", "switch", "Wi-Fi", "IPv6",
      "VPN", "pare-feu", "firewall", "BGP", "DNS", "DHCP"]),
    ("Systèmes et virtualisation", "Administration Windows Server, Linux, Active Directory, virtualisation "
     "(Proxmox, VMware, Hyper-V), conteneurs, sauvegarde, supervision et cloud : ce qui sert à un admin "
     "système.",
     ["Windows Server", "Linux", "Debian", "Ubuntu", "Active Directory", "Proxmox", "VMware", "Hyper-V", "Docker",
      "Kubernetes", "Veeam", "sauvegarde", "Zabbix", "GLPI", "Ansible", "Azure", "Microsoft 365"]),
]
# Sujets de départ d'avant (PR #52) : remplacés par ceux de l'option SISR s'ils n'ont pas été modifiés
# Descriptions de départ qui parlaient du BTS SIO (PR #53) : rendues neutres si elles n'ont pas été modifiées
DESCRIPTIONS_BTS = {
    "Réseaux": "Équipements et protocoles réseau (Cisco, Fortinet, pfSense, routage, VLAN, Wi-Fi, IPv6, VPN, "
               "pare-feu) : nouveautés et failles utiles à un étudiant en BTS SIO option SISR.",
    "Systèmes et virtualisation": "Administration Windows Server, Linux, Active Directory, virtualisation "
                                  "(Proxmox, VMware, Hyper-V), conteneurs, sauvegarde, supervision et cloud : ce qui "
                                  "sert à un admin système débutant (BTS SIO option SISR).",
}
ANCIENS_DEFAUT = {
    "Réseaux et systèmes": "Administration Windows Server, Linux, Active Directory, réseau (Cisco, VLAN, Wi-Fi), "
                           "virtualisation et cloud : nouveautés utiles à un étudiant de BTS SIO.",
    "Développement": "Langages, frameworks et outils de développement (Python, PHP, Java, JavaScript, SQL, Git), "
                     "IA pour développer.",
}

PROMPT = """Tu fais la veille d'actualités d'un élève ou d'un étudiant, en France. Il veut du condensé, rapide à
lire, et n'être alerté que pour le rare et le grave.
{formation}Ses sujets suivis :
{sujets}
{deja}
Sur l'entrée : des articles, un par ligne en JSON (id, source, titre, extrait).
Pour chaque article :
- "sujets" : les codes des sujets qu'il concerne VRAIMENT (pas un simple mot en commun ; [] si aucun) ;
- "score" : son importance pour lui, d'après ses sujets et sa formation, de 0 à 1 (0,9 : événement majeur ;
  0,6 : intéressant ; 0,3 : anecdotique ; 0 : hors sujet, pub, promo, bon plan, test de produit) ;
- "titre" : un titre neutre et informatif en français, 12 mots au plus, qui dit le fait (« Fuite chez France
  Travail : 43 millions de personnes touchées ») : pas de racolage, pas de question, pas de « vous ne devinerez
  jamais » ; le titre d'origine s'il est déjà clair ; vide si hors sujet ;
- "resume" : en français, style dépêche, 20 mots au plus : ce qui s'est passé, qui est touché, l'ampleur, et
  s'il y a quelque chose à faire (« Mettre à jour FortiOS en 7.4.5 »). Chiffres concrets, pas de « cet article »
  ni de formule creuse. Vide si hors sujet ;
- "alerte" : true SEULEMENT pour un événement rare et grave, dans ses sujets, qu'il doit connaître le jour même :
  par exemple piratage ou fuite massive d'une grande organisation (des millions de personnes), faille critique
  activement exploitée dans un produit très répandu, panne mondiale majeure, décision officielle qui change ses
  examens. Quelques-unes par semaine au plus ; dans le doute, false. Un tutoriel, une mise à jour courante, une
  faille non exploitée : false ;
- "evenement" : une étiquette courte de l'information en minuscules avec tirets (« fuite-france-travail »,
  « fortios-cve-2026-24858 », « windows-server-2025-maj-novembre »), LA MÊME pour tous les articles qui
  racontent la même information (même sur des sites différents) ; "" si hors sujet ;
- "notion" : la notion de cours que l'article illustre, en 1 à 4 mots, comme dans un cours de sa formation
  (« VLAN », « chiffrement TLS », « RGPD », « inflation », « photosynthèse ») ; "" si aucune.
Réponds seulement en JSON, un objet par article, tous les articles :
[{{"id": 12, "sujets": ["S1"], "score": 0.9, "titre": "...", "resume": "...", "alerte": true, "evenement": "...", "notion": "..."}}]"""

PROMPT_POINT = """Tu fais « L'essentiel » de la veille d'un élève ou d'un étudiant : ce qu'il doit savoir de
l'actualité des dernières 24 heures sur ses sujets, en 30 secondes, d'un seul coup d'œil.
{formation}Sur l'entrée : les articles importants, un par ligne en JSON (id, source, titre, resume, alerte, notion,
sources = nombre de sites qui en parlent).
{precedent}Écris :
- "en_bref" : UNE phrase de 20 mots au plus, ce qu'il faut retenir de la journée s'il ne lit que ça (le fait le
  plus important d'abord) ; si rien de marquant : « Journée calme : … » ;
- "points" : de 3 à 5 points, du plus important au moins important (une alerte ou une info reprise par beaucoup
  de sources passe devant). Un point = UNE information (regroupe les articles qui parlent de la même chose) :
  - "titre" : 2 à 5 mots qui disent de quoi il s'agit (« Fuite France Travail », « Faille FortiOS ») ;
  - "texte" : 25 mots au plus, en français, style dépêche : le fait, qui est touché, l'ampleur ; pas
    d'introduction, pas de « cet article », pas de point sans fait concret ;
  - "action" : ce qu'il doit faire, 10 mots au plus (« Mettre à jour FortiOS en 7.4.5 »), "" si rien ;
  - "suite" : true si c'est la suite d'un point de l'essentiel précédent (du nouveau sur la même histoire) ;
  - "ids" : les articles dont vient le point (au moins un, uniquement ceux de l'entrée) ;
- "a_retenir" : si une de ces infos illustre bien une notion de cours de sa formation, {{"notion": "1 à 4 mots",
  "texte": "20 mots au plus : le lien entre l'info et la notion, pour réviser"}} ; sinon null.
Réponds seulement en JSON :
{{"en_bref": "...", "points": [{{"titre": "...", "texte": "...", "action": "", "suite": false, "ids": [12, 15]}}],
  "a_retenir": {{"notion": "...", "texte": "..."}}}}"""


def formation_texte() -> str:
    """La formation suivie cette année (page Matières), pour que l'IA juge l'importance ; rien si non renseignée."""
    try:
        noms = db.formations_actuelles()
    except Exception as e:   # base d'avant les formations, panne : l'IA se fie aux sujets suivis
        log(f"formation illisible : {e}")
        noms = []
    if not noms:
        return ""
    return f"Sa formation : {' ; '.join(noms[:3])}.\n"


def log(msg: str):
    c = db.compte_actuel()
    print(f"actualités{f' (compte {c})' if c not in (None, 1) else ''} : {msg}", flush=True)


def _origine() -> bool:
    """Le fil de l'admin d'origine (ou le travail interne) : ses clés d'avant les comptes restent les mêmes."""
    return db.compte_actuel() in (None, 1)


def cle(nom: str) -> str:
    """Clé d'état propre au compte en cours (réglages, point du jour, heures de lecture…)."""
    return nom if _origine() else f"{nom}:{int(db.compte_actuel())}"


def destinataire() -> int | None:
    """À qui vont les notifications du fil en cours : ce compte, ou les admins (None) pour l'admin d'origine,
    comme les autres notifications du worker (ses appareils d'avant les comptes compris)."""
    return None if _origine() else db.compte_actuel()


def _cle_notif(base: str) -> str:
    return base if _origine() else f"{base}@{int(db.compte_actuel())}"[:400]


def notifier(base: str, titre: str, corps: str, url: str) -> bool:
    """Dans la file du compte du fil, avec une clé à lui : la même alerte sonne chez chacun, une fois."""
    cle_n = _cle_notif(base)
    with db.pour_compte(destinataire()):
        return db.notifier(cle_n, titre, corps, url)


def reglages() -> dict:
    try:
        return {**DEFAUT, **json.loads(db.etat_valeur(cle("actualites")) or "{}")}
    except ValueError:
        return dict(DEFAUT)


def enregistrer_reglages(r: dict):
    db.etat_ecrire(cle("actualites"), json.dumps({k: r[k] for k in DEFAUT if k in r}))


def descriptions_neutres():
    """Une fois : les descriptions de départ qui citaient le BTS SIO, si elles sont intactes, deviennent neutres."""
    if db.etat_valeur("actualites_neutre"):
        return
    db.etat_ecrire("actualites_neutre", "1")
    nouvelles = {nom: d for nom, d, _ in SUJETS_DEFAUT}
    for s in db.sujets():
        if DESCRIPTIONS_BTS.get(s["nom"]) == s["description"]:
            db.sujet_maj(s["id"], description=nouvelles[s["nom"]])


def passer_en_sisr():
    """Une seule fois : les anciens sujets de départ, s'ils sont intacts, laissent place à ceux de l'option SISR."""
    if db.etat_valeur("actualites_sisr"):
        return
    db.etat_ecrire("actualites_sisr", "1")
    remplaces = [s for s in db.sujets() if ANCIENS_DEFAUT.get(s["nom"]) == s["description"]]
    if not remplaces:
        return
    for s in remplaces:
        db.sujet_supprimer(s["id"])
    for nom, description, mots in SUJETS_DEFAUT:
        db.sujet_ajouter(nom, description, mots)   # ignoré s'il existe déjà
    db.articles_retrier()
    log(f"sujets de départ passés en option SISR ({', '.join(s['nom'] for s in remplaces)} remplacés)")


def _hote(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.") if url.startswith("http") else ""


def ajouter_nouveaux_flux():
    """Flux ajoutés à la sélection de départ après coup : proposés une fois aux installations existantes
    (un site que tu suis déjà, même sous une autre adresse, n'est pas ajouté deux fois)."""
    par_titre = {t: (u, c, site) for u, t, c, site in FLUX_DEFAUT}
    for cle, titres in FLUX_AJOUTES.items():
        if db.etat_valeur(cle):
            continue
        db.etat_ecrire(cle, "1")
        deja = {_hote(f["url"]) for f in db.flux_liste()} | {_hote(f["site"] or "") for f in db.flux_liste()}
        ajoutes = []
        for t in titres:
            u, c, site = par_titre[t]
            if _hote(u) and _hote(u) in deja - {"feeds.feedburner.com"}:
                continue
            db.flux_ajouter(u, t, site, categorie=c)
            ajoutes.append(t)
        if ajoutes:
            log(f"flux ajoutés : {', '.join(ajoutes)}")
    ranger_par_categories()


def categorie_connue(url: str, site: str = "") -> str:
    """La catégorie de départ d'un site connu (même adresse de flux ou même site), sinon ""."""
    for u, _, c, s in FLUX_DEFAUT:
        if url == u or (_hote(url) and _hote(url) in {_hote(u), _hote(s)} - {"feeds.feedburner.com"}) \
                or (_hote(site) and _hote(site) in {_hote(u), _hote(s)} - {"feeds.feedburner.com"}):
            return c
    return ""


def ranger_par_categories():
    """Une fois : les sources déjà suivies prennent la catégorie de départ de leur site (Sécurité, IT…)."""
    if db.etat_valeur(cle("actualites_categories")):
        return
    db.etat_ecrire(cle("actualites_categories"), "1")
    for f in db.flux_liste():
        if not f["sujet_id"] and not f["categorie"] and (c := categorie_connue(f["url"], f["site"] or "")):
            db.flux_maj(f["id"], categorie=c)


def resumes_condenses():
    """Une fois : les articles de la semaine sont relus pour avoir le résumé condensé (sans renotifier)."""
    if not db.etat_valeur("actualites_resumes"):
        db.etat_ecrire("actualites_resumes", "1")
        db.articles_retrier()


# --- Fil par défaut : celui que reçoit tout compte qui n'a rien changé -------------------------

def _depart() -> dict:
    return {"sujets": [{"nom": n, "description": d, "mots_cles": list(m), "notifier": True, "recherche": False}
                       for n, d, m in SUJETS_DEFAUT],
            "flux": [{"url": u, "titre": t, "site": site, "notifier": False, "categorie": c} for u, t, c, site in FLUX_DEFAUT]}


def _empreinte_depart() -> str:
    """Change quand la sélection de départ change : les comptes qui la suivent reçoivent les nouvelles sources."""
    return hashlib.sha1(json.dumps([FLUX_DEFAUT, SUJETS_DEFAUT], ensure_ascii=False).encode()).hexdigest()[:8]


def profil_defaut() -> dict:
    """{version, depart (sélection de départ ?), le, sujets: [...], flux: [...]} : le fil des comptes non personnalisés."""
    try:
        p = json.loads(db.etat_valeur("actualites_defaut") or "null")
    except ValueError:
        p = None
    if not isinstance(p, dict) or p.get("depart") or not isinstance(p.get("sujets"), list):
        return {**_depart(), "version": f"{(p or {}).get('version') or 'depart'}:{_empreinte_depart()}", "depart": True,
                "le": (p or {}).get("le")}
    return p


def profil_du_fil() -> dict:
    """Le fil du compte en cours, tel qu'on le copierait (les recherches Google sont une case du sujet)."""
    sujets = db.sujets()
    return {"sujets": [{"nom": s["nom"], "description": s["description"], "mots_cles": list(s["mots_cles"]),
                        "notifier": bool(s["notifier"]), "recherche": bool(s["recherche_id"])} for s in sujets],
            "flux": [{"url": f["url"], "titre": f["titre"], "site": f["site"], "notifier": bool(f["notifier"]),
                      "categorie": f.get("categorie") or ""} for f in db.flux_liste() if not f["sujet_id"]]}


def definir_defaut(depuis_mon_fil: bool = True) -> dict:
    """L'admin : son fil (ou la sélection de départ) devient celui de tous les comptes non personnalisés, au
    prochain tour du worker. Les alertes y sont activées sur chaque sujet : un nouveau compte est prévenu du grave."""
    version = f"{time.time():.3f}"
    if depuis_mon_fil:
        p = profil_du_fil()
        p["sujets"] = [{**x, "notifier": True} for x in p["sujets"]]
        p.update(version=version, depart=False, le=time.time())
    else:
        p = {"version": version, "depart": True, "le": time.time()}
    db.etat_ecrire("actualites_defaut", json.dumps(p, ensure_ascii=False))
    return profil_defaut()


def personnalise() -> bool:
    """Le compte en cours a-t-il changé son fil ? L'admin d'origine : toujours (son fil ne bouge jamais tout seul)."""
    return _origine() or db.etat_valeur(cle("actualites_perso")) == "1"


def marquer_personnalise():
    """Une source ou un sujet changé par le compte : son fil n'est plus remplacé par le fil par défaut."""
    if not _origine():
        db.etat_ecrire(cle("actualites_perso"), "1")


def _cle_fil(p: dict):
    return (sorted((x["nom"], x["description"], tuple(x["mots_cles"]), bool(x.get("recherche"))) for x in p["sujets"]),
            sorted(x["url"] for x in p["flux"]))


def appliquer_defaut() -> bool:
    """Met le fil du compte en cours sur le fil par défaut (sujets, flux, alertes). Ses articles des sources
    gardées restent (lus, favoris) ; ceux des sources retirées partent avec elles. Vrai si quelque chose a changé."""
    p = profil_defaut()
    sujets = {s["nom"]: s for s in db.sujets()}
    change = False
    for nom, s in sujets.items():
        if nom not in {x["nom"] for x in p["sujets"]}:
            db.sujet_supprimer(s["id"])
            change = True
    for x in p["sujets"]:
        s = sujets.get(x["nom"])
        if s is None:
            sujet_id = db.sujet_ajouter(x["nom"], x["description"], x["mots_cles"], x["notifier"])
            change = True
        else:
            sujet_id = s["id"]
            if (s["description"], list(s["mots_cles"]), bool(s["notifier"])) != (x["description"], x["mots_cles"], x["notifier"]):
                db.sujet_maj(sujet_id, description=x["description"], mots_cles=x["mots_cles"], notifier=x["notifier"])
                change = True
        s = next((y for y in db.sujets() if y["id"] == sujet_id), None)
        if s and x.get("recherche") and not s["recherche_id"]:
            db.flux_ajouter(recherche_google(s), f"Google Actualités : {s['nom']}", "news.google.com", sujet_id=sujet_id)
        elif s and not x.get("recherche") and s["recherche_id"]:
            db.flux_supprimer(s["recherche_id"])
    voulus = {x["url"]: x for x in p["flux"]}
    for f in db.flux_liste():
        if f["sujet_id"]:
            continue
        if f["url"] not in voulus:
            db.flux_supprimer(f["id"])
            continue
        x = voulus.pop(f["url"])
        if bool(f["notifier"]) != x["notifier"]:
            db.flux_maj(f["id"], notifier=not f["notifier"])
        if f["categorie"] != x.get("categorie", ""):
            db.flux_maj(f["id"], categorie=x.get("categorie", ""))
    for x in voulus.values():   # nouveaux : leur premier relevé ne sonne pas (articles déjà en ligne)
        db.flux_ajouter(x["url"], x["titre"], x["site"], notifier=x["notifier"], categorie=x.get("categorie", ""))
    if change:
        db.articles_retrier()
    db.etat_ecrire(cle("actualites_defaut_vu"), p["version"])
    return change


def comptes_suivant_defaut() -> tuple[list[dict], list[dict]]:
    """(comptes qui suivent le fil par défaut, comptes qui l'ont personnalisé), hors admin d'origine."""
    suivent, perso = [], []
    for compte_id in db.comptes_actualites():
        if compte_id == 1:
            continue
        k = db.compte(compte_id)
        (perso if db.etat_valeur(f"actualites_perso:{compte_id}") == "1" else suivent).append(
            {"id": compte_id, "nom": k["nom"] or k["identifiant"]})
    return suivent, perso


def suivre_defaut():
    """Le compte revient au fil par défaut (bouton de la page Sources) : tout de suite, puis à chaque changement."""
    if not _origine():
        db.etat_ecrire(cle("actualites_perso"), "")
        appliquer_defaut()


def initialiser():
    """Au démarrage de chaque compte puis à chaque tour : un compte qui n'a rien changé suit le fil par défaut.
    Les mises à jour de la sélection d'avant ne concernent que l'installation d'origine (admin)."""
    if not _origine():
        if personnalise():
            return
        vu = db.etat_valeur(cle("actualites_defaut_vu"))
        if not vu and (db.sujets() or db.flux_liste()) and _cle_fil(profil_du_fil()) != _cle_fil(_depart()):
            marquer_personnalise()   # fil d'avant le fil par défaut, déjà changé par lui : on n'y touche pas
            return
        if vu != profil_defaut()["version"]:
            appliquer_defaut()
            log("fil mis sur le fil par défaut")
        return
    if db.etat_valeur("actualites_init"):
        passer_en_sisr()
        descriptions_neutres()
        ajouter_nouveaux_flux()
        resumes_condenses()
        return
    db.etat_ecrire("actualites_sisr", "1")
    db.etat_ecrire("actualites_neutre", "1")
    db.etat_ecrire("actualites_resumes", "1")
    for k in FLUX_AJOUTES:
        db.etat_ecrire(k, "1")
    db.etat_ecrire("actualites_init", "1")
    for nom, description, mots in SUJETS_DEFAUT:
        db.sujet_ajouter(nom, description, mots)
    db.etat_ecrire(cle("actualites_categories"), "1")
    for url, titre, categorie, site in FLUX_DEFAUT:
        db.flux_ajouter(url, titre, site, categorie=categorie)
    log(f"sélection de départ : {len(FLUX_DEFAUT)} flux, {len(SUJETS_DEFAUT)} sujets")


# --- Texte et liens -------------------------------------------------------------

def texte_simple(brut: str | None, longueur: int = 400) -> str:
    """HTML d'un flux → texte court (balises retirées, entités décodées)."""
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", brut or "", flags=re.S | re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= longueur else t[:longueur].rsplit(" ", 1)[0] + "…"


def lien_sur(url: str | None) -> str:
    """Seulement http(s) : jamais un « javascript: » venu d'un flux."""
    url = (url or "").strip()
    return url if urlsplit(url).scheme in ("http", "https") else ""


def cle_article(lien: str, titre: str, flux_id: int) -> str:
    """Le lien sans traceurs ni ancre (le même article relayé par deux flux = une seule clé)."""
    if lien:
        u = urlsplit(lien)
        requete = urlencode([(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                             if not k.lower().startswith(("utm_", "xtor", "at_", "fbclid", "gclid"))])
        return urlunsplit(("", (u.hostname or "").removeprefix("www."), u.path.rstrip("/"), requete, ""))[2:]
    return "sans-lien:" + hashlib.sha256(f"{flux_id}:{titre}".encode()).hexdigest()[:32]


def _sans_accents(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


def correspond(mots_cles: list[str], texte: str) -> bool:
    """Un mot-clé présent comme mot entier (début de mot : « faille » trouve « failles »), sans accents ni casse."""
    t = _sans_accents(texte)
    return any(re.search(r"(?<![a-z0-9])" + re.escape(_sans_accents(m.strip())), t) for m in mots_cles if m.strip())


def par_mots_cles(sujets: list[dict], article: dict) -> list[int]:
    texte = f"{article['titre']} {article.get('extrait') or ''}"
    return [s["id"] for s in sujets if correspond(s["mots_cles"] or [s["nom"]], texte)]


def recherche_google(sujet: dict) -> str:
    """Flux Google Actualités d'un sujet : ses mots-clés (ou son nom), en français, la dernière semaine."""
    mots = sujet["mots_cles"][:6] or [sujet["nom"]]
    q = " OR ".join(f'"{m}"' if " " in m else m for m in mots) + " when:7d"
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=fr&gl=FR&ceid=FR:fr"


# --- Relevé des flux --------------------------------------------------------------

class FluxIllisible(Exception):
    pass


class AdresseLocale(httpx.RequestError):
    """Flux qui mène au réseau local (la VM, ses services Docker, le routeur…) : jamais relevé."""


def _seulement_internet(request: httpx.Request):
    # Vérifié à chaque requête, redirections comprises : un flux public ne peut pas renvoyer vers le réseau local
    if not _hote_public(str(request.url)):
        raise AdresseLocale("adresse du réseau local refusée : seulement des sites sur Internet", request=request)


def _client() -> httpx.Client:
    return httpx.Client(timeout=20, follow_redirects=True, event_hooks={"request": [_seulement_internet]},
                        headers={"User-Agent": UA, "Accept":
                        "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.8, */*;q=0.5"})


def _injoignable(e: httpx.HTTPError) -> str:
    return str(e) if isinstance(e, AdresseLocale) else f"site injoignable ({e.__class__.__name__})"


def _date(entree) -> dt.datetime:
    t = entree.get("published_parsed") or entree.get("updated_parsed")
    maintenant = dt.datetime.now(dt.timezone.utc)
    if not t:
        return maintenant
    try:
        return min(dt.datetime(*t[:6], tzinfo=dt.timezone.utc), maintenant)  # date future (fuseau mal donné)
    except (ValueError, TypeError):
        return maintenant


IMG = re.compile(r"<img\b[^>]*?\bsrc=[\"']([^\"']+)", re.I)


def image_article(e, lien: str = "") -> str:
    """L'illustration d'un article : media:thumbnail / media:content, pièce jointe image, ou 1re <img> du texte."""
    candidats = []
    for m in list(e.get("media_thumbnail") or []) + list(e.get("media_content") or []):
        genre = (m.get("type") or "").lower()
        if m.get("medium") in (None, "", "image") and not genre.startswith(("video", "audio")):
            candidats.append(m.get("url"))
    for l in e.get("links") or []:
        if l.get("rel") == "enclosure" and (l.get("type") or "").lower().startswith("image/"):
            candidats.append(l.get("href"))
    for brut in [e.get("summary") or ""] + [c.get("value", "") for c in (e.get("content") or [])]:
        candidats += IMG.findall(brut)
    for c in candidats:
        url = lien_sur(urljoin(lien, html.unescape(c or "").strip())) if c else ""
        # pas les pixels espions ni les émojis : un nom de fichier qui ressemble à une vraie image
        if url and len(url) <= 1000 and not re.search(r"(pixel|tracking|feedburner|emoji|gravatar|1x1)", url, re.I):
            return url
    return ""


def duree(texte) -> int | None:
    """itunes:duration (« 3600 », « 59:07 », « 1:02:03 ») → secondes."""
    try:
        parts = [int(float(x)) for x in str(texte or "").strip().split(":")]
    except ValueError:
        return None
    if not parts or len(parts) > 3:
        return None
    total = 0
    for x in parts:
        total = total * 60 + x
    return total if 0 < total < 24 * 3600 else None


def media_article(e, lien: str = "") -> dict:
    """Épisode de podcast (pièce jointe audio : écoutable sur la page) ou vidéo YouTube."""
    for l in e.get("links") or []:
        if l.get("rel") == "enclosure" and (l.get("type") or "").lower().startswith("audio/") and lien_sur(l.get("href")):
            return {"media": "audio", "media_url": lien_sur(l.get("href"))[:1000],
                    "duree": duree(e.get("itunes_duration"))}
    hote = (urlsplit(lien).hostname or "").removeprefix("www.").removeprefix("m.")
    if e.get("yt_videoid") or hote in ("youtube.com", "youtu.be"):
        return {"media": "video", "media_url": "", "duree": None}
    return {"media": "", "media_url": "", "duree": None}


def lire_flux(contenu: bytes, flux_id: int = 0) -> tuple[dict, list[dict]]:
    """Contenu d'un flux → (infos du flux, articles prêts pour la base)."""
    f = feedparser.parse(contenu)
    if not f.entries and (f.bozo or not f.feed.get("title")):
        raise FluxIllisible("ce n'est pas un flux RSS ou Atom")
    items, vus = [], set()
    for e in f.entries:
        titre = texte_simple(e.get("title"), 300)
        lien = lien_sur(e.get("link"))
        if not titre and not lien:
            continue
        cle = cle_article(lien, titre, flux_id)
        if cle in vus:
            continue
        vus.add(cle)
        resume = e.get("summary") or ""
        if not resume and e.get("content"):
            resume = e.content[0].get("value", "")
        items.append({"cle": cle, "lien": lien, "titre": titre or lien, "extrait": texte_simple(resume),
                      "publie_le": _date(e), "image": image_article(e, lien), **media_article(e, lien)})
    infos = {"titre": texte_simple(f.feed.get("title"), 120), "site": lien_sur(f.feed.get("link"))}
    return infos, items


ALTERNATE = re.compile(r"<link\b[^>]*>", re.I)


YOUTUBE_FLUX = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
CHAINE = re.compile(r'(?:"channelId"|"externalId"|"browseId")\s*:\s*"(UC[\w-]{22})"|youtube\.com/channel/(UC[\w-]{22})')
YOUTUBE_COOKIES = {"Cookie": "SOCS=CAI; CONSENT=YES+"}   # sans la page « Avant d'accéder à YouTube » (Europe)


def _est_youtube(url: str) -> bool:
    return (urlsplit(url).hostname or "").removeprefix("www.").removeprefix("m.") in ("youtube.com", "youtu.be")


def _chaine_youtube(page: str) -> str | None:
    m = CHAINE.search(page)
    return (m.group(1) or m.group(2)) if m else None


def flux_youtube(entree: str, c: httpx.Client) -> str:
    """Une chaîne YouTube (lien de la chaîne, @pseudo, ou son nom) → l'adresse du flux de ses vidéos."""
    entree = entree.strip()
    if m := re.search(r"youtube\.com/channel/(UC[\w-]{22})", entree):
        return YOUTUBE_FLUX.format(m.group(1))
    if entree.startswith("@"):
        page = f"https://www.youtube.com/{entree}"
    elif _est_youtube(entree if "://" in entree else "https://" + entree):
        u = urlsplit(entree if "://" in entree else "https://" + entree)
        page = urlunsplit(("https", "www.youtube.com", u.path, u.query, ""))
    else:   # un nom : la première chaîne trouvée par la recherche YouTube (filtre « Chaînes »)
        page = "https://www.youtube.com/results?" + urlencode({"search_query": entree, "sp": "EgIQAg=="})
    r = c.get(page, headers=YOUTUBE_COOKIES)
    r.raise_for_status()
    chaine = _chaine_youtube(r.text)
    if not chaine:
        raise FluxIllisible("chaîne YouTube introuvable : colle le lien de la chaîne (youtube.com/@…)")
    return YOUTUBE_FLUX.format(chaine)


def flux_podcast(entree: str, c: httpx.Client) -> str:
    """Un podcast (lien Apple Podcasts ou son nom) → l'adresse de son flux RSS, par l'annuaire d'Apple."""
    if m := re.search(r"podcasts\.apple\.com/.*?id(\d+)", entree):
        r = c.get("https://itunes.apple.com/lookup", params={"id": m.group(1), "entity": "podcast"})
    else:
        r = c.get("https://itunes.apple.com/search", params={"term": entree.strip(), "media": "podcast", "country": "fr",
                                                            "limit": 5})
    r.raise_for_status()
    try:
        resultats = r.json().get("results") or []
    except ValueError:
        resultats = []
    if not m:   # recherche par nom : le plus ressemblant d'abord (tous les mots du nom), sinon le premier
        voulu = _sans_accents(re.sub(r"[^\w ]", " ", entree)).split()
        resultats.sort(key=lambda x: not all(v in _sans_accents(x.get("collectionName") or "") for v in voulu))
    for x in resultats:
        if lien_sur(x.get("feedUrl")):
            return x["feedUrl"]
    raise FluxIllisible("podcast introuvable dans l'annuaire Apple Podcasts : colle l'adresse de son flux RSS")


def _source_speciale(url: str) -> tuple[str, str] | None:
    """« podcast:Nom », « youtube:Nom », un lien Apple Podcasts ou une chaîne YouTube → (genre, ce qu'on cherche)."""
    if m := re.match(r"(podcast|youtube)\s*:\s*(.+)", url, re.I):
        return m.group(1).lower(), m.group(2)
    if re.search(r"podcasts\.apple\.com/", url):
        return "podcast", url
    if _est_youtube(url if "://" in url else "https://" + url) and "/feeds/videos.xml" not in url and "watch?" not in url:
        return "youtube", url
    if url.startswith("@") and " " not in url:
        return "youtube", url
    return None


def resoudre(url: str, c: httpx.Client) -> str:
    """Adresse d'un flux telle quelle, ou celle d'un podcast / d'une chaîne YouTube retrouvée."""
    special = _source_speciale(url.strip())
    if not special:
        return url
    genre, quoi = special
    return flux_podcast(quoi, c) if genre == "podcast" else flux_youtube(quoi, c)


def decouvrir(url: str, client: httpx.Client | None = None) -> tuple[str, dict, list[dict]]:
    """Adresse d'un flux, d'un site qui en annonce un (<link rel="alternate">), d'une chaîne YouTube, d'un podcast
    (lien Apple Podcasts ou « podcast:Nom ») → (url du flux, infos, articles)."""
    url = url.strip()
    c = client or _client()
    try:
        try:
            url = resoudre(url, c)
        except httpx.HTTPStatusError as e:
            raise FluxIllisible(f"le site répond {e.response.status_code}") from e
        except httpx.HTTPError as e:
            raise FluxIllisible(_injoignable(e)) from e
        if not re.match(r"https?://", url, re.I):
            url = "https://" + url
        if not lien_sur(url):
            raise FluxIllisible("adresse invalide")
        r = c.get(url, headers=YOUTUBE_COOKIES if _est_youtube(url) else None)
        r.raise_for_status()
        try:
            infos, items = lire_flux(r.content)
            return str(r.url), infos, items
        except FluxIllisible:
            pass
        for balise in ALTERNATE.findall(r.text[:300_000]):
            if re.search(r"rel=[\"']?alternate", balise, re.I) and re.search(r"(rss|atom)\+xml", balise, re.I):
                href = re.search(r"href=[\"']([^\"']+)", balise, re.I)
                if href:
                    lien = urljoin(str(r.url), html.unescape(href.group(1)))
                    r2 = c.get(lien)
                    r2.raise_for_status()
                    infos, items = lire_flux(r2.content)
                    return str(r2.url), infos, items
        if _est_youtube(str(r.url)) and (chaine := _chaine_youtube(r.text)):   # page d'une vidéo : sa chaîne
            r2 = c.get(YOUTUBE_FLUX.format(chaine))
            r2.raise_for_status()
            infos, items = lire_flux(r2.content)
            return str(r2.url), infos, items
        raise FluxIllisible("aucun flux RSS trouvé sur cette page : colle l'adresse du flux lui-même")
    except httpx.HTTPStatusError as e:
        raise FluxIllisible(f"le site répond {e.response.status_code}") from e
    except httpx.HTTPError as e:
        raise FluxIllisible(_injoignable(e)) from e
    finally:
        if client is None:
            c.close()


def relever_un(flux: dict, sujets: list[dict], client: httpx.Client) -> int:
    """Nouveaux articles d'un flux ; une erreur est notée sur le flux (page Sources), jamais levée."""
    entetes = {}
    if flux["etag"]:
        entetes["If-None-Match"] = flux["etag"]
    if flux["modifie"]:
        entetes["If-Modified-Since"] = flux["modifie"]
    maintenant = dt.datetime.now(dt.timezone.utc)
    try:
        if _source_speciale(flux["url"]):   # « podcast:Nom », « youtube:Nom » : l'adresse du flux est retrouvée une fois
            adresse = resoudre(flux["url"], client)
            flux = {**flux, "url": adresse}
            if not _changer_adresse(flux["id"], adresse):
                raise FluxIllisible("ce flux est déjà dans ta liste")
        r = client.get(flux["url"], headers=entetes)
        if r.status_code == 304:
            db.flux_maj(flux["id"], releve_le=maintenant, erreur=None, echecs=0)
            return 0
        r.raise_for_status()
        infos, items = lire_flux(r.content, flux["id"])
    except (httpx.HTTPError, FluxIllisible) as e:
        message = (f"le site répond {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError)
                   else str(e) if isinstance(e, FluxIllisible) else _injoignable(e))
        reparation = _reparer(flux, e, client)
        if reparation is None:
            db.flux_maj(flux["id"], releve_le=maintenant, erreur=message, echecs=flux["echecs"] + 1)
            return 0
        r, infos, items = reparation
    premier = flux["releve_le"] is None
    for i in items:
        i["sujets"] = par_mots_cles(sujets, i)
        if not sujets:
            i["tri"] = "mots_cles"   # rien à trier : l'IA n'est pas appelée
    # Premier relevé : les articles déjà en ligne sont rangés sans notification
    n = db.articles_ajouter(flux["id"], items, annonce=premier)
    db.flux_maj(flux["id"], releve_le=maintenant, erreur=None, echecs=0,
                etag=r.headers.get("etag"), modifie=r.headers.get("last-modified"),
                **({"titre": infos["titre"]} if infos["titre"] and not flux["titre"] else {}),
                **({"site": infos["site"]} if infos["site"] and not flux["site"] else {}))
    return n


def _changer_adresse(flux_id: int, adresse: str) -> bool:
    """Nouvelle adresse d'un flux (retrouvée ou réparée) ; faux si tu suis déjà cette adresse dans un autre flux."""
    if any(f["url"] == adresse and f["id"] != flux_id for f in db.flux_liste()):
        return False
    db.flux_maj(flux_id, url=adresse, etag=None, modifie=None)
    return True


def _reparer(flux: dict, erreur: Exception, client: httpx.Client):
    """Flux disparu (404, page qui n'est plus un flux) : son site annonce peut-être sa nouvelle adresse. Essayé au
    premier échec puis tous les 6 relevés (3 h), jamais pour une recherche Google ni un site injoignable.
    → (réponse, infos, articles) avec la nouvelle adresse déjà enregistrée, ou None."""
    if flux.get("sujet_id") or not flux.get("site") or flux["echecs"] % 6:
        return None
    if not (isinstance(erreur, FluxIllisible) or (isinstance(erreur, httpx.HTTPStatusError)
                                                  and erreur.response.status_code in (404, 410, 403))):
        return None
    try:
        adresse, infos, items = decouvrir(flux["site"], client)
    except FluxIllisible:
        return None
    if adresse == flux["url"] or not _changer_adresse(flux["id"], adresse):
        return None
    log(f"flux « {flux['titre'] or flux['url']} » réparé : {flux['url']} → {adresse}")
    for i in items:
        i["cle"] = cle_article(i["lien"], i["titre"], flux["id"])
    return httpx.Response(200, request=httpx.Request("GET", adresse)), infos, items


def synchroniser_recherches(sujets: list[dict]):
    """Un flux Google Actualités par sujet qui le demande (mots-clés changés = nouvelle adresse)."""
    par_sujet = {f["sujet_id"]: f for f in db.flux_liste() if f["sujet_id"]}
    for s in sujets:
        f = par_sujet.get(s["id"])
        if f and f["url"] != recherche_google(s):
            db.flux_maj(f["id"], url=recherche_google(s), etag=None, modifie=None)


def relever() -> int:
    sujets = db.sujets()
    synchroniser_recherches(sujets)
    flux = db.flux_liste()
    if not flux:
        return 0
    with _client() as client, ThreadPoolExecutor(4, thread_name_prefix="rss") as ex:
        n = sum(ex.map(lambda f: relever_un(f, sujets, client), flux))
    db.articles_nettoyer()
    if n:
        log(f"{n} nouvel(s) article(s) sur {len(flux)} flux")
    return n


# --- Tri par l'IA --------------------------------------------------------------------

def _codes(sujets: list[dict]) -> dict[str, int]:
    return {f"S{i + 1}": s["id"] for i, s in enumerate(sujets)}


def prompt(sujets: list[dict], deja: list[str] | None = None) -> str:
    lignes = [f"{code} — {s['nom']} : {s['description'] or s['nom']}"
              + (f" (mots-clés : {', '.join(s['mots_cles'])})" if s["mots_cles"] else "")
              for code, s in zip(_codes(sujets), sujets)]
    connus = (f"\nÉtiquettes déjà données ces derniers jours (réutilise la même si c'est la même information) : "
              f"{', '.join(deja)}\n" if deja else "")
    return PROMPT.format(sujets="\n".join(lignes), deja=connus, formation=formation_texte())


def _etiquette(texte) -> str | None:
    e = re.sub(r"[^a-z0-9]+", "-", _sans_accents(str(texte or ""))).strip("-")[:80]
    return e or None


def lire_reponse(texte: str, ids: set[int], codes: dict[str, int]) -> dict[int, dict]:
    debut, fin = texte.find("["), texte.rfind("]")
    try:
        donnees = json.loads(texte[debut:fin + 1]) if debut >= 0 else None
    except ValueError:
        donnees = None
    if not isinstance(donnees, list):
        raise agent.ErreurAgent("réponse illisible (pas de liste JSON)")
    resultats = {}
    for d in donnees:
        if not isinstance(d, dict):
            continue
        try:
            i, score = int(d.get("id")), float(d.get("score") or 0)
        except (TypeError, ValueError):
            continue
        if i not in ids:
            continue
        sujets = [codes[c] for c in (d.get("sujets") or []) if isinstance(c, str) and c in codes]
        alerte = d.get("alerte") is True and bool(sujets)
        hors_sujet = not sujets and score < 0.3
        notion = re.sub(r"\s+", " ", str(d.get("notion") or "")).strip(" .«»\"'")[:60]
        titre = re.sub(r"\s+", " ", str(d.get("titre") or "")).strip(" «»\"'")[:140]
        resultats[i] = {"id": i, "tri": "ia", "sujets": sujets, "score": max(0.0, min(score, 1.0)),
                        "titre_ia": None if hors_sujet else titre or None,
                        "pourquoi": str(d.get("resume") or d.get("pourquoi") or "").strip()[:220] or None,
                        "alerte": alerte, "evenement": None if hors_sujet else _etiquette(d.get("evenement")),
                        "notion": None if hors_sujet else notion or None}
    if not resultats:
        raise agent.ErreurAgent("réponse sans aucun article reconnu")
    return resultats


def _sans_ia(a: dict, sujets: list[dict], verdict: dict | None = None) -> dict:
    """Rangé par mots-clés. `verdict` : ce que l'IA a dit du même article chez un autre compte (compte sans
    forfait) : son résumé et son alerte, si l'article tombe dans un de ses sujets."""
    r = {"id": a["id"], "tri": "mots_cles", "sujets": par_mots_cles(sujets, a), "score": None, "pourquoi": None,
         "alerte": False, "evenement": None, "notion": None, "titre_ia": None}
    if verdict:
        r.update(pourquoi=verdict["pourquoi"], evenement=verdict["evenement"], notion=verdict["notion"],
                 titre_ia=verdict.get("titre_ia"),
                 alerte=bool(verdict["alerte"]) and bool(r["sujets"]))
    return r


def _trier_sans_forfait(a_ia: list[dict], sujets: list[dict], vieux_attente: float) -> int:
    """Compte sans forfait Claude : l'IA n'est pas appelée pour lui. Le verdict du même article chez un autre
    compte (l'admin le trie à chaque tour, avant lui) est repris ; sans verdict, l'article attend un peu, puis
    les mots-clés seuls."""
    verdicts = db.verdicts_ia([a["cle"] for a in a_ia], db.compte_actuel())
    faits = [_sans_ia(a, sujets, verdicts.get(a["cle"])) for a in a_ia
             if a["cle"] in verdicts or a["cree_le"].timestamp() < vieux_attente]
    db.articles_trier(faits)
    return len(faits)


def trier(claude_ok) -> int:
    """Trie les articles en attente ; lève LimiteAtteinte/ClaudeIndisponible (le worker met Claude en pause)."""
    attente = db.articles_a_trier(LOT * 5)
    if not attente:
        return 0
    sujets = db.sujets()
    limite_fraicheur = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=FRAIS)
    vieux_attente = time.time() - SANS_IA_APRES
    ia_possible = bool(sujets) and reglages()["ia"] and local.reglages()["mode"] != "toujours"
    a_ia, sans = [], []
    for a in attente:
        if ia_possible and a["publie_le"] >= limite_fraicheur:
            a_ia.append(a)
        else:
            sans.append(_sans_ia(a, sujets))
    if sans:
        db.articles_trier(sans)
    if not a_ia:
        return len(sans)
    if not agent.forfait_autorise():
        return len(sans) + _trier_sans_forfait(a_ia, sujets, vieux_attente)
    if not claude_ok():
        # Claude en pause : on attend un peu, puis les mots-clés prennent le relais
        trop_vieux = [_sans_ia(a, sujets) for a in a_ia if a["cree_le"].timestamp() < vieux_attente]
        db.articles_trier(trop_vieux)
        return len(sans) + len(trop_vieux)
    codes = _codes(sujets)
    alertes = db.alertes_recentes()
    consigne = prompt(sujets, alertes + [e for e in db.evenements_recents() if e not in alertes])
    faits = len(sans)
    # Même article déjà trié avec exactement les mêmes sujets et la même formation (un autre compte qui suit la
    # même veille) : son verdict est repris tel quel, Claude n'est pas rappelé pour rien
    empreinte = hashlib.sha256(prompt(sujets).encode()).hexdigest()
    repris = {a["id"]: v for a in a_ia if (v := _verdict_repris(a, empreinte, codes))}
    if repris:
        db.articles_trier(list(repris.values()))
        faits += len(repris)
        a_ia = [a for a in a_ia if a["id"] not in repris]
    code_de = {i: c for c, i in codes.items()}
    for debut in range(0, len(a_ia), LOT):
        lot = a_ia[debut:debut + LOT]
        entree = "\n".join(json.dumps({"id": a["id"], "source": a["source"], "titre": a["titre"],
                                       "extrait": a["extrait"][:300]}, ensure_ascii=False) for a in lot)
        ids = {a["id"] for a in lot}
        try:
            resultats, _ = agent.demander("actualites", consigne, Path(tempfile.gettempdir()), entree=entree,
                                          timeout=300, lire=lambda t: lire_reponse(t, ids, codes))
        except agent.ErreurAgent as e:
            log(f"tri par l'IA impossible pour {len(lot)} articles ({e}) : mots-clés")
            resultats = {}
        db.articles_trier([resultats.get(a["id"]) or _sans_ia(a, sujets) for a in lot])
        for a in lot:
            if a["id"] in resultats:
                _retenir(empreinte, a["cle"], {**resultats[a["id"]], "sujets": [code_de[i] for i in resultats[a["id"]]["sujets"]]})
        faits += len(lot)
    return faits


# Verdicts récents de l'IA, par (empreinte de la consigne, article) : en mémoire du worker, 2 jours au plus
_VERDICTS: dict[tuple[str, str], tuple[float, dict]] = {}
VERDICTS_MAX, VERDICTS_DUREE = 5000, 2 * 86400


def _retenir(empreinte: str, cle_article: str, verdict: dict, maintenant: float | None = None):
    maintenant = maintenant or time.time()
    if len(_VERDICTS) >= VERDICTS_MAX:
        for k in [k for k, (le, _) in _VERDICTS.items() if maintenant - le > VERDICTS_DUREE] or list(_VERDICTS)[:VERDICTS_MAX // 5]:
            del _VERDICTS[k]
    _VERDICTS[(empreinte, cle_article)] = (maintenant, verdict)


def _verdict_repris(a: dict, empreinte: str, codes: dict[str, int], maintenant: float | None = None) -> dict | None:
    """Le verdict déjà donné pour cet article et cette consigne, avec les sujets de ce compte, ou None."""
    le, v = _VERDICTS.get((empreinte, a["cle"]), (0.0, None))
    if v is None or (maintenant or time.time()) - le > VERDICTS_DUREE:
        return None
    return {**v, "id": a["id"], "sujets": [codes[c] for c in v["sujets"] if c in codes]}


# --- « L'essentiel » : le point du jour -------------------------------------------------

def point() -> dict | None:
    """Le dernier point du jour : {le, ids, points: [{texte, ids}], modele} (points vide : rien à montrer)."""
    try:
        p = json.loads(db.etat_valeur(cle("actualites_point")) or "null")
    except ValueError:
        return None
    return p if isinstance(p, dict) else None


def lire_point(texte: str, ids: set[int]) -> dict:
    debut, fin = texte.find("{"), texte.rfind("}")
    try:
        d = json.loads(texte[debut:fin + 1]) if debut >= 0 else None
    except ValueError:
        d = None
    if not isinstance(d, dict) or not isinstance(d.get("points"), list):
        raise agent.ErreurAgent("point du jour illisible")
    points = []
    for p in d["points"][:5]:
        if not isinstance(p, dict) or not str(p.get("texte") or "").strip():
            continue
        cites = [int(i) for i in (p.get("ids") or []) if str(i).isdigit() and int(i) in ids]
        if cites:
            points.append({"texte": _ligne(p["texte"], 240), "ids": cites[:6], "titre": _ligne(p.get("titre"), 60),
                           "action": _ligne(p.get("action"), 100), "suite": p.get("suite") is True})
    if not points:
        raise agent.ErreurAgent("point du jour sans aucun point relié aux articles")
    r = d.get("a_retenir")
    a_retenir = None
    if isinstance(r, dict) and _ligne(r.get("notion"), 60) and _ligne(r.get("texte"), 200):
        a_retenir = {"notion": _ligne(r.get("notion"), 60).strip(" .«»"), "texte": _ligne(r.get("texte"), 200)}
    return {"points": points, "en_bref": _ligne(d.get("en_bref"), 200), "a_retenir": a_retenir}


def _ligne(texte, longueur: int) -> str:
    return re.sub(r"\s+", " ", str(texte or "")).strip()[:longueur]


POINT_AFFICHE = 36 * 3600   # au-delà, « L'essentiel » n'est plus montré (ni copié)


def essentiel_permis() -> bool:
    """« L'essentiel du jour » pour le compte en cours : l'admin, ou le droit « essentiel » (forfait de l'admin
    pour ce seul point, même sans autonomie)."""
    if _origine():
        return True
    from claunote import comptes
    return comptes.peut(comptes.trouver(db.compte_actuel()), "essentiel")


def _signature_fil():
    return _cle_fil(profil_du_fil())


def copier_point_admin(maintenant: float | None = None) -> bool | None:
    """Même fil que l'admin (le fil par défaut, le plus souvent) : son « Essentiel » est repris, relié aux mêmes
    articles de ce compte ; Claude n'est pas rappelé. None : rien à reprendre (fil différent, pas de point frais)."""
    maintenant = maintenant or time.time()
    mien = _signature_fil()
    with db.pour_compte(1):
        if _signature_fil() != mien:
            return None
        p = point()
    if not p or not p.get("points") or maintenant - float(p.get("le") or 0) > POINT_AFFICHE:
        return None
    if (point() or {}).get("source_le") == p["le"]:
        return False
    cles = db.cles_articles(sorted({i for x in p["points"] for i in x["ids"]}))
    miens = db.ids_par_cles(list(cles.values()))
    points = [{**x, "ids": ids} for x in p["points"]
              if (ids := [miens[cles[i]] for i in x["ids"] if cles.get(i) in miens])]
    if not points:
        return None
    db.etat_ecrire(cle("actualites_point"), json.dumps(
        {"points": points, "le": p["le"], "ids": sorted({i for x in points for i in x["ids"]}),
         "en_bref": p.get("en_bref") or "", "a_retenir": p.get("a_retenir"),
         "modele": p.get("modele"), "source_le": p["le"], "copie": True}, ensure_ascii=False))
    log(f"« L'essentiel » de l'admin repris ({len(points)} points)")
    return True


def faire_point(claude_ok, maintenant: float | None = None) -> bool:
    """Refait « L'essentiel » quand de nouveaux articles importants sont arrivés (au plus toutes les 3 h,
    tout de suite pour une alerte). Sans Claude, l'ancien point reste affiché : rien n'est inventé.
    Un autre compte : seulement avec le droit « essentiel » ; même fil que l'admin = son point repris."""
    if not reglages()["ia"] or local.reglages()["mode"] == "toujours":
        return False
    if _origine():
        return _faire_point(claude_ok, maintenant)
    if not essentiel_permis():
        return False   # la notification du jour compte alors les articles pour toi
    copie = copier_point_admin(maintenant)
    if copie is not None:
        return copie
    with agent.forfait(True):   # le droit « essentiel » : ce point passe par le forfait de l'admin
        return _faire_point(claude_ok, maintenant)


def _faire_point(claude_ok, maintenant: float | None = None) -> bool:
    maintenant = maintenant or time.time()
    candidats = db.articles_pour_point(POINT_FENETRE, POINT_SEUIL, POINT_MAX)
    if len(candidats) < 2:
        return False
    ancien = point() or {}
    ids = sorted(a["id"] for a in candidats)
    nouveaux = set(ids) - set(ancien.get("ids") or [])
    if not nouveaux:
        return False
    alerte_neuve = any(a["alerte"] for a in candidats if a["id"] in nouveaux)
    if not alerte_neuve and maintenant - float(ancien.get("le") or 0) < POINT_TOUTES:
        return False
    if not claude_ok():
        return False
    entree = "\n".join(json.dumps({"id": a["id"], "source": a["source"], "titre": a.get("titre_ia") or a["titre"],
                                   "resume": a["pourquoi"] or a["extrait"][:200], "alerte": bool(a["alerte"]),
                                   "notion": a.get("notion") or "", "sources": a.get("sources") or 1},
                                  ensure_ascii=False) for a in candidats)
    avant = [x.get("titre") or x["texte"] for x in (ancien.get("points") or [])
             if maintenant - float(ancien.get("le") or 0) < POINT_AFFICHE]
    precedent = ("L'essentiel précédent disait : " + " ; ".join(avant) + ".\n") if avant else ""
    try:
        consigne = PROMPT_POINT.format(formation=formation_texte(), precedent=precedent)
        lu, modele = agent.demander("actualites_point", consigne, Path(tempfile.gettempdir()), entree=entree,
                                    timeout=300, lire=lambda t: lire_point(t, set(ids)))
    except agent.ErreurAgent as e:
        log(f"point du jour impossible ({e}) : l'ancien reste affiché")
        # pas de nouvel essai à chaque tour : on attend la prochaine fenêtre de 3 h
        db.etat_ecrire(cle("actualites_point"), json.dumps({**ancien, "le": maintenant}, ensure_ascii=False))
        return False
    db.etat_ecrire(cle("actualites_point"), json.dumps({**lu, "le": maintenant, "ids": ids, "modele": modele},
                                                  ensure_ascii=False))
    log(f"point du jour refait ({len(lu['points'])} points, {modele})")
    return True


# --- Affichage : même info regroupée, lien avec les cours ---------------------------------

def regrouper(liste: list[dict], depuis: dt.datetime | None = None) -> list[dict]:
    """La même information chez plusieurs sites = une seule carte (la première : la plus récente, ou la plus
    importante en tri par importance), les autres en « aussi chez » ; « nouveau » : arrivé depuis ta visite."""
    cartes, par_evenement = [], {}
    for a in liste:
        a = {**a, "aussi": [], "image_id": a["id"], "ids": [a["id"]],
             "nouveau": bool(depuis and a.get("cree_le") and a["cree_le"] > depuis)}
        a["sources"] = 1
        cle = a.get("evenement")
        if cle and cle in par_evenement:
            tete = par_evenement[cle]
            tete["aussi"].append({"id": a["id"], "source": a["source"], "lien": a["lien"], "titre": a["titre"]})
            tete["ids"].append(a["id"])
            tete["sources"] = len({tete["source"], *(x["source"] for x in tete["aussi"])})
            tete["nouveau"] = tete["nouveau"] or a["nouveau"]
            tete["lu"] = tete["lu"] and a["lu"]
            tete["alerte"] = bool(tete.get("alerte") or a.get("alerte"))
            tete["score"] = max(tete.get("score") or 0, a.get("score") or 0) if tete.get("tri") == "ia" else tete["score"]
            tete["noms_sujets"] = list(dict.fromkeys([*(tete.get("noms_sujets") or []), *(a.get("noms_sujets") or [])]))
            tete["notion"] = tete.get("notion") or a.get("notion")
            if not tete.get("image") and a.get("image"):
                tete["image"], tete["image_id"] = a["image"], a["id"]
            continue
        if cle:
            par_evenement[cle] = a
        cartes.append(a)
    return cartes


def cours_lies(notions: list[str], limite: int = 12) -> dict[str, dict]:
    """Notion de cours → ton cours le plus proche (recherche plein texte), pour « Revoir ton cours »."""
    trouves = {}
    for n in list(dict.fromkeys(n for n in notions if n))[:limite]:
        try:
            r = db.recherche(n, 1)
        except Exception as e:   # recherche impossible : la page s'affiche sans le lien
            log(f"cours lié à « {n} » introuvable : {e}")
            continue
        if r:
            trouves[n] = r[0]
    return trouves


# --- Vignettes : l'image de l'article, passée par le site ---------------------------------

VIGNETTE_MAX = 4 * 1024 * 1024   # octets téléchargés au plus
VIGNETTE_COTE = 480              # pixels (côté le plus long) de la vignette gardée
VIGNETTES = Path(tempfile.gettempdir()) / "claunote-vignettes"
VIGNETTES_MAX = 300 * 1024 * 1024   # cache des vignettes borné : les plus anciennes effacées (retéléchargées au besoin)
_vignettes_ecrites = {"n": 0}


def elaguer_vignettes(maximum: int = VIGNETTES_MAX) -> int:
    """Cache des vignettes au-delà de `maximum` : les plus anciennes effacées jusqu'à 80 %. Renvoie le nombre effacé."""
    try:
        fichiers = sorted(((f.stat().st_mtime, f.stat().st_size, f) for f in VIGNETTES.iterdir()), key=lambda x: x[0])
    except OSError:
        return 0
    total, effaces = sum(t for _, t, _ in fichiers), 0
    for _, taille, f in fichiers:
        if total <= maximum * 0.8 or (effaces == 0 and total <= maximum):
            break
        f.unlink(missing_ok=True)
        total -= taille
        effaces += 1
    return effaces


def _hote_public(url: str) -> bool:
    """Jamais une adresse du réseau local (la VM, le routeur…) : seulement des sites sur Internet."""
    import ipaddress
    import socket
    hote = urlsplit(url).hostname
    if not hote:
        return False
    try:
        adresses = {i[4][0] for i in socket.getaddrinfo(hote, None)}
    except OSError:
        return False
    return bool(adresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in adresses)


def telecharger_image(url: str, client: httpx.Client | None = None, verifier_hote=_hote_public) -> bytes | None:
    """Image d'un site tiers → vignette JPEG réduite (ou None) : redirections vérifiées une à une, taille bornée,
    contenu décodé par Pillow (un SVG, une page HTML ou un fichier piégé ne passent pas)."""
    from io import BytesIO

    from PIL import Image
    c = client or httpx.Client(timeout=10, follow_redirects=False, headers={"User-Agent": UA, "Accept": "image/*"})
    try:
        for _ in range(4):
            if not lien_sur(url) or not verifier_hote(url):
                return None
            with c.stream("GET", url) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers.get("location", ""))
                    continue
                if r.status_code != 200 or not r.headers.get("content-type", "").lower().startswith("image/"):
                    return None
                donnees = bytearray()
                for morceau in r.iter_bytes():
                    donnees += morceau
                    if len(donnees) > VIGNETTE_MAX:
                        return None
            break
        else:
            return None
        Image.MAX_IMAGE_PIXELS = 40_000_000
        with Image.open(BytesIO(bytes(donnees))) as im:
            if im.format not in ("JPEG", "PNG", "WEBP", "GIF", "AVIF"):
                return None
            im.seek(0)
            im = im.convert("RGB")
            if min(im.size) < 40:          # pixel espion, icône
                return None
            im.thumbnail((VIGNETTE_COTE, VIGNETTE_COTE))
            sortie = BytesIO()
            im.save(sortie, "JPEG", quality=78, optimize=True)
            return sortie.getvalue()
    except (httpx.HTTPError, OSError, ValueError, Image.DecompressionBombError):
        return None
    finally:
        if client is None:
            c.close()


def vignette(url: str) -> bytes | None:
    """Vignette en cache (fichier vide = image refusée, pas retéléchargée)."""
    fichier = VIGNETTES / (hashlib.sha256(url.encode()).hexdigest() + ".jpg")
    if fichier.exists():
        return fichier.read_bytes() or None
    image = telecharger_image(url)
    try:
        VIGNETTES.mkdir(parents=True, exist_ok=True)
        fichier.write_bytes(image or b"")
        _vignettes_ecrites["n"] += 1
        if _vignettes_ecrites["n"] % 200 == 0:
            elaguer_vignettes()
    except OSError:
        pass
    return image


# --- Notification du jour : à l'heure où tu lis tes actualités ------------------------------

def _paris(maintenant: dt.datetime | None = None) -> dt.datetime:
    return (maintenant or dt.datetime.now(dt.timezone.utc)).astimezone(notifications.PARIS)


def _habitudes() -> dict[str, list[int]]:
    try:
        h = json.loads(db.etat_valeur(cle("actualites_lectures")) or "{}")
    except ValueError:
        return {}
    return h if isinstance(h, dict) else {}


def noter_lecture(maintenant: dt.datetime | None = None):
    """Tu ouvres les Actualités (ou un article) : l'heure est notée, une fois par heure et par jour, 28 jours."""
    m = _paris(maintenant)
    h = _habitudes()
    jour = m.date().isoformat()
    if m.hour in h.get(jour, []):
        return
    h[jour] = sorted({*h.get(jour, []), m.hour})
    limite = (m.date() - dt.timedelta(days=QUOTIDIEN_JOURS)).isoformat()
    db.etat_ecrire(cle("actualites_lectures"), json.dumps({j: v for j, v in h.items() if j > limite}))


VISITE_PAUSE = 20 * 60   # secondes sans venir : la visite suivante est une nouvelle visite


def visite(noter: bool = True, maintenant: float | None = None) -> dt.datetime | None:
    """L'heure de ta visite précédente (avant celle-ci), pour « Depuis ta visite » et les pastilles « Nouveau ».
    Plusieurs pages vues à moins de 20 minutes d'écart = la même visite : ce qui était nouveau le reste.
    noter=False (rafraîchissement automatique de la page) : rien n'est changé."""
    maintenant = maintenant or time.time()
    try:
        v = json.loads(db.etat_valeur(cle("actualites_visite")) or "{}")
    except ValueError:
        v = {}
    derniere, precedente = float(v.get("derniere") or 0), v.get("precedente")
    if noter:
        if derniere and maintenant - derniere > VISITE_PAUSE:
            precedente = derniere
        try:
            db.etat_ecrire(cle("actualites_visite"), json.dumps({"derniere": maintenant, "precedente": precedente}))
        except Exception as e:   # « Voir en tant que » (base en lecture seule), panne : la page s'affiche quand même
            log(f"visite non notée : {e}")
    return dt.datetime.fromtimestamp(float(precedente), dt.timezone.utc) if precedente else None


def heure_quotidienne() -> tuple[dt.time | None, str]:
    """(heure de la notification du jour, pourquoi) ; None : réglée sur « non »."""
    choix = reglages().get("quotidien") or "auto"
    if choix == "non":
        return None, "désactivée"
    if choix != "auto":
        try:
            return dt.time.fromisoformat(choix), "heure choisie"
        except ValueError:
            pass
    h = _habitudes()
    if len(h) < QUOTIDIEN_MIN:
        return QUOTIDIEN_DEFAUT, f"le temps d'apprendre quand tu lis : {len(h)}/{QUOTIDIEN_MIN} jours"
    # L'heure où tu lis le plus souvent (en nombre de jours) ; à égalité, la plus tôt : prévenu avant de lire
    compte = {}
    for heures in h.values():
        for x in heures:
            compte[x] = compte.get(x, 0) + 1
    meilleure = min(compte, key=lambda x: (-compte[x], x))
    return dt.time(meilleure, 0), f"tu lis tes actualités vers {meilleure} h ({compte[meilleure]} jours sur {len(h)})"


def quotidien(maintenant: dt.datetime | None = None) -> bool:
    """Une fois par jour, à l'heure choisie : « L'essentiel » (ou les articles pour toi pas encore lus).
    Rien à dire : rien n'est envoyé. S'ajoute aux alertes, qui partent toujours tout de suite."""
    heure, _ = heure_quotidienne()
    m = _paris(maintenant)
    if heure is None or m.time() < heure or not notifications.preferences().get("actualites", True):
        return False
    jour = f"actu-jour:{m.date().isoformat()}"
    if db.etat_valeur(cle("actualites_quotidien")) == jour:
        return False
    db.etat_ecrire(cle("actualites_quotidien"), jour)
    if m - m.replace(hour=heure.hour, minute=heure.minute, second=0, microsecond=0) > dt.timedelta(hours=3):
        return False   # worker arrêté pendant l'heure prévue : pas de notification « du matin » le soir
    p = point() or {}
    frais = p.get("points") and m.timestamp() - float(p.get("le") or 0) < 20 * 3600
    if frais:
        titre = f"📰 L'essentiel : {len(p['points'])} info{'s' if len(p['points']) > 1 else ''} du jour"
        corps = p.get("en_bref") or " · ".join(x["texte"] for x in p["points"][:2])
    else:
        n = db.nb_actualites_nouvelles()
        if not n:
            return False
        titre = f"📰 {n} article{'s' if n > 1 else ''} pour toi"
        corps = "Les nouveautés de tes sujets suivis t'attendent."
    return notifier(jour, titre[:120], corps[:300], "/actualites")


# --- Notifications ------------------------------------------------------------------

CVE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.I)


def _cles_alerte(a: dict) -> tuple[list[str], list[str]]:
    """(clés de notification, CVE citées) d'une alerte. Une CVE identifie mieux la faille que l'étiquette de
    l'IA : deux sites qui donnent deux étiquettes à la même CVE ne sonnent qu'une fois, et une étiquette
    réutilisée pour une autre faille (« cisco-faille-critique ») ne bloque pas la nouvelle."""
    texte = " ".join(str(a.get(k) or "") for k in ("titre", "pourquoi", "extrait", "evenement"))
    cves = sorted({c.upper() for c in CVE.findall(texte)})
    if cves:
        return [f"actu-alerte:{c.lower()}" for c in cves], cves
    return [f"actu-alerte:{a.get('evenement') or a['cle']}"[:400]], []


def annoncer(maintenant: dt.datetime | None = None) -> int:
    """Décide, pour chaque article trié, s'il sonne.

    Sonne : une alerte (rare et grave, jugée par l'IA) d'un sujet « à notifier », une seule fois par événement
    même si plusieurs sites en parlent ; ou chaque nouveauté d'un flux marqué « tout m'intéresse ».
    Une alerte de moins de 2 jours reste candidate même déjà décidée (relue après un changement de sujet,
    venue d'un flux à son premier relevé) : seule une notification déjà partie pour elle l'arrête.
    Sans l'IA, rien ne sonne pour les sujets : les mots-clés ne savent pas juger de la gravité.
    """
    articles = db.articles_a_annoncer()
    if not articles:
        return 0
    maintenant = maintenant or dt.datetime.now(dt.timezone.utc)
    pref = notifications.preferences().get("actualites", True)   # les réglages du compte du fil
    appareils = bool(db.abonnements_de(destinataire()))
    actif = pref and appareils
    sujets = {s["id"]: s for s in db.sujets()}
    notifiants = {i for i, s in sujets.items() if s["notifier"]}
    n, vus = 0, set()
    for a in sorted(articles, key=lambda a: -(a["score"] or 0)):
        neuf = not a.get("annonce")
        recent = a["publie_le"] >= maintenant - dt.timedelta(days=AGE_NOTIF)
        resume = a["pourquoi"] or a["extrait"][:200]
        url = f"/actualites?article={a['id']}#a{a['id']}"
        if a.get("alerte") and a["sujets"]:
            pourquoi_pas = ("notifications Actualités coupées" if not pref else "aucun appareil abonné" if not appareils
                            else "trop ancienne" if not recent
                            else "sujet sans « Me notifier »" if not any(s in notifiants for s in a["sujets"]) else None)
            if pourquoi_pas:
                if neuf:
                    log(f"alerte non notifiée ({pourquoi_pas}) : {a['titre'][:80]}")
                continue
            cles, cves = _cles_alerte(a)
            if vus & set(cles) or db.alerte_deja_envoyee([_cle_notif(k) for k in cles], cves, compte_id=destinataire()):
                vus.update(cles)
                continue
            vus.update(cles)
            if notifier(cles[0], f"🚨 {a['titre']}"[:120], f"{resume} ({a['source']})"[:300], url):
                n += 1
                log(f"alerte notifiée : {a['titre'][:80]}")
        elif neuf and actif and recent and a["flux_notifier"] and (
                a["tri"] != "ia" or (a["score"] or 0) >= 0.3 or not a["sujets"]):
            n += notifier(f"actu:{a['cle']}"[:380], a["titre"][:120],
                             (f"{a['source']} : {resume}" if resume else a["source"])[:300], url)
    db.articles_annonces([a["id"] for a in articles if not a.get("annonce")])
    return n


# --- Boucle du worker -----------------------------------------------------------------

def _chaque_compte(faire, comptes_ids: list[int]) -> list[int]:
    """`faire()` dans le fil de chaque compte (sa partie de la base, son droit au forfait Claude). La panne d'un
    fil ne bloque pas les autres ; une limite ou une panne de Claude remonte au worker (pause pour tous).
    Renvoie les comptes pour lesquels tout s'est bien passé."""
    from claunote import comptes   # comptes → agent, db : pas d'import circulaire au chargement
    reussis = []
    for compte_id in comptes_ids:
        try:
            with comptes.pour(compte_id):
                faire()
            reussis.append(compte_id)
        except (agent.LimiteAtteinte, agent.ClaudeIndisponible) as e:
            if isinstance(e, agent.SansForfait):
                continue
            raise
        except Exception as e:
            log(f"compte {compte_id} : {e}")
    return reussis


def suit_l_admin() -> bool:
    """Ce compte a exactement le fil de l'admin (le fil par défaut, sans rien changé) : il reprend ses articles,
    son tri et son « Essentiel » au lieu de tout refaire ; seules ses notifications et ses lectures sont à lui."""
    if _origine() or personnalise():
        return False
    mien = _signature_fil()
    with db.pour_compte(1):
        return _signature_fil() == mien


def miroir() -> int:
    """Recopie les articles triés de l'admin dans le fil du compte (voir db.articles_miroir)."""
    depuis = db.etat_valeur(cle("actualites_miroir_le"))
    maintenant = dt.datetime.now(dt.timezone.utc)
    n = db.articles_miroir(db.compte_actuel(), dt.datetime.fromisoformat(depuis) if depuis else None)
    db.etat_ecrire(cle("actualites_miroir_le"), (maintenant - dt.timedelta(minutes=5)).isoformat())
    db.articles_nettoyer()
    return n


def defaut_en_permanence() -> bool:
    """Réglage de l'admin : son fil est le fil par défaut, toujours à jour (activé tant qu'il ne l'a pas coupé)."""
    return db.etat_valeur("actualites_defaut_permanent") != "non"


def suivre_le_fil_admin():
    """Fil par défaut « en permanence » : un changement du fil de l'admin devient tout de suite le fil par défaut."""
    if not defaut_en_permanence():
        return
    p = profil_defaut()
    mien = profil_du_fil()
    mien["sujets"] = [{**x, "notifier": True} for x in mien["sujets"]]
    reglages_flux = lambda q: sorted((x["url"], x["notifier"], x.get("categorie", "")) for x in q["flux"])  # noqa: E731
    if p.get("depart") or _cle_fil(p) != _cle_fil(mien) or reglages_flux(p) != reglages_flux(mien):
        definir_defaut()
        log("fil par défaut mis à jour (le fil de l'admin, en permanence)")


def _tour_compte(claude_ok):
    if _origine():
        suivre_le_fil_admin()
    elif suit_l_admin():
        miroir()   # articles, tri et alertes de l'admin : ni relevé ni IA pour ce compte
    trier(claude_ok)
    annoncer()
    faire_point(claude_ok)
    quotidien()


def tour(claude_ok, maintenant: float | None = None):
    """Relevé de tous les fils (sans Claude), puis tri, alertes et point du jour de chacun, l'admin d'abord."""
    maintenant = maintenant or time.time()
    comptes_ids = db.comptes_actualites()
    _chaque_compte(initialiser, [c for c in comptes_ids if c != 1])   # l'admin d'origine : au démarrage du worker
    demande = db.etat_consommer("actualites_relever")
    dernier = float(db.etat_valeur("actualites_releve") or 0)
    if demande or maintenant - dernier >= RELEVE:
        db.etat_ecrire("actualites_releve", str(maintenant))
        _chaque_compte(lambda: None if suit_l_admin() else relever(), comptes_ids)
    _chaque_compte(lambda: _tour_compte(claude_ok), comptes_ids)
