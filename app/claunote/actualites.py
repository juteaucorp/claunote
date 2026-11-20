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

FLUX_DEFAUT = [
    ("https://www.cert.ssi.gouv.fr/alerte/feed/", "CERT-FR : alertes de sécurité"),
    ("https://www.cert.ssi.gouv.fr/actualite/feed/", "CERT-FR : actualités"),
    ("https://www.it-connect.fr/feed/", "IT-Connect"),
    ("https://next.ink/feed/", "Next"),
    ("https://korben.info/feed", "Korben"),
    ("https://www.lemondeinformatique.fr/flux-rss/thematique/toutes-les-actualites/rss.xml", "Le Monde Informatique"),
    ("https://www.numerama.com/feed/", "Numerama"),
    ("https://www.01net.com/feed/", "01net"),
    ("https://www.clubic.com/feed/news.rss", "Clubic"),
]
FLUX_AJOUTES = {"actualites_flux_2": ["Numerama", "01net"], "actualites_flux_3": ["Clubic"]}   # ajoutés après coup aux installations existantes
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
[{{"id": 12, "sujets": ["S1"], "score": 0.9, "resume": "...", "alerte": true, "evenement": "...", "notion": "..."}}]"""

PROMPT_POINT = """Tu fais « L'essentiel » de la veille d'un élève ou d'un étudiant : ce qu'il doit savoir de
l'actualité des dernières 24 heures sur ses sujets, en 30 secondes.
{formation}Sur l'entrée : les articles importants, un par ligne en JSON (id, source, titre, resume, alerte, notion).
Écris de 3 à 5 points, du plus important au moins important :
- un point = UNE information (regroupe les articles qui parlent de la même chose), 25 mots au plus, en français,
  style dépêche : le fait, qui est touché, l'ampleur, et l'action à faire s'il y en a une ;
- pas de titre, pas d'introduction, pas de « cet article », pas de point sans fait concret ;
- "ids" : les articles dont vient le point (au moins un, uniquement ceux de l'entrée).
Réponds seulement en JSON :
{{"points": [{{"texte": "...", "ids": [12, 15]}}]}}"""


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
    print(f"actualités : {msg}", flush=True)


def reglages() -> dict:
    try:
        return {**DEFAUT, **json.loads(db.etat_valeur("actualites") or "{}")}
    except ValueError:
        return dict(DEFAUT)


def enregistrer_reglages(r: dict):
    db.etat_ecrire("actualites", json.dumps({k: r[k] for k in DEFAUT if k in r}))


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


def ajouter_nouveaux_flux():
    """Flux ajoutés à la sélection de départ après coup : proposés une fois aux installations existantes."""
    par_titre = {t: u for u, t in FLUX_DEFAUT}
    for cle, titres in FLUX_AJOUTES.items():
        if db.etat_valeur(cle):
            continue
        db.etat_ecrire(cle, "1")
        for t in titres:
            db.flux_ajouter(par_titre[t], t, urlsplit(par_titre[t]).hostname or "")
        log(f"flux ajoutés : {', '.join(titres)}")


def resumes_condenses():
    """Une fois : les articles de la semaine sont relus pour avoir le résumé condensé (sans renotifier)."""
    if not db.etat_valeur("actualites_resumes"):
        db.etat_ecrire("actualites_resumes", "1")
        db.articles_retrier()


def initialiser():
    """Au tout premier démarrage : une sélection de départ (modifiable sur la page Sources)."""
    if db.etat_valeur("actualites_init"):
        passer_en_sisr()
        descriptions_neutres()
        ajouter_nouveaux_flux()
        resumes_condenses()
        return
    db.etat_ecrire("actualites_sisr", "1")
    db.etat_ecrire("actualites_neutre", "1")
    db.etat_ecrire("actualites_resumes", "1")
    for cle in FLUX_AJOUTES:
        db.etat_ecrire(cle, "1")
    db.etat_ecrire("actualites_init", "1")
    for nom, description, mots in SUJETS_DEFAUT:
        db.sujet_ajouter(nom, description, mots)
    for url, titre in FLUX_DEFAUT:
        db.flux_ajouter(url, titre, urlsplit(url).hostname or "")
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


def _client() -> httpx.Client:
    return httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": UA, "Accept":
                        "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.8, */*;q=0.5"})


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
                      "publie_le": _date(e), "image": image_article(e, lien)})
    infos = {"titre": texte_simple(f.feed.get("title"), 120), "site": lien_sur(f.feed.get("link"))}
    return infos, items


ALTERNATE = re.compile(r"<link\b[^>]*>", re.I)


def decouvrir(url: str, client: httpx.Client | None = None) -> tuple[str, dict, list[dict]]:
    """Adresse d'un flux, ou d'un site qui en annonce un (<link rel="alternate">) → (url du flux, infos, articles)."""
    url = url.strip()
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    if not lien_sur(url):
        raise FluxIllisible("adresse invalide")
    c = client or _client()
    try:
        r = c.get(url)
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
        raise FluxIllisible("aucun flux RSS trouvé sur cette page : colle l'adresse du flux lui-même")
    except httpx.HTTPStatusError as e:
        raise FluxIllisible(f"le site répond {e.response.status_code}") from e
    except httpx.HTTPError as e:
        raise FluxIllisible(f"site injoignable ({e.__class__.__name__})") from e
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
        r = client.get(flux["url"], headers=entetes)
        if r.status_code == 304:
            db.flux_maj(flux["id"], releve_le=maintenant, erreur=None, echecs=0)
            return 0
        r.raise_for_status()
        infos, items = lire_flux(r.content, flux["id"])
    except (httpx.HTTPError, FluxIllisible) as e:
        message = (f"le site répond {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError)
                   else str(e) if isinstance(e, FluxIllisible) else f"site injoignable ({e.__class__.__name__})")
        db.flux_maj(flux["id"], releve_le=maintenant, erreur=message, echecs=flux["echecs"] + 1)
        return 0
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
        resultats[i] = {"id": i, "tri": "ia", "sujets": sujets, "score": max(0.0, min(score, 1.0)),
                        "pourquoi": str(d.get("resume") or d.get("pourquoi") or "").strip()[:220] or None,
                        "alerte": alerte, "evenement": None if hors_sujet else _etiquette(d.get("evenement")),
                        "notion": None if hors_sujet else notion or None}
    if not resultats:
        raise agent.ErreurAgent("réponse sans aucun article reconnu")
    return resultats


def _sans_ia(a: dict, sujets: list[dict]) -> dict:
    return {"id": a["id"], "tri": "mots_cles", "sujets": par_mots_cles(sujets, a), "score": None, "pourquoi": None,
            "alerte": False, "evenement": None, "notion": None}


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
    if not claude_ok():
        # Claude en pause : on attend un peu, puis les mots-clés prennent le relais
        trop_vieux = [_sans_ia(a, sujets) for a in a_ia if a["cree_le"].timestamp() < vieux_attente]
        db.articles_trier(trop_vieux)
        return len(sans) + len(trop_vieux)
    codes = _codes(sujets)
    alertes = db.alertes_recentes()
    consigne = prompt(sujets, alertes + [e for e in db.evenements_recents() if e not in alertes])
    faits = len(sans)
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
        faits += len(lot)
    return faits


# --- « L'essentiel » : le point du jour -------------------------------------------------

def point() -> dict | None:
    """Le dernier point du jour : {le, ids, points: [{texte, ids}], modele} (points vide : rien à montrer)."""
    try:
        p = json.loads(db.etat_valeur("actualites_point") or "null")
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
            points.append({"texte": re.sub(r"\s+", " ", str(p["texte"])).strip()[:240], "ids": cites[:4]})
    if not points:
        raise agent.ErreurAgent("point du jour sans aucun point relié aux articles")
    return {"points": points}


def faire_point(claude_ok, maintenant: float | None = None) -> bool:
    """Refait « L'essentiel » quand de nouveaux articles importants sont arrivés (au plus toutes les 3 h,
    tout de suite pour une alerte). Sans Claude, l'ancien point reste affiché : rien n'est inventé."""
    maintenant = maintenant or time.time()
    if not reglages()["ia"] or local.reglages()["mode"] == "toujours":
        return False
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
    entree = "\n".join(json.dumps({"id": a["id"], "source": a["source"], "titre": a["titre"],
                                   "resume": a["pourquoi"] or a["extrait"][:200], "alerte": bool(a["alerte"]),
                                   "notion": a.get("notion") or ""}, ensure_ascii=False) for a in candidats)
    try:
        consigne = PROMPT_POINT.format(formation=formation_texte())
        lu, modele = agent.demander("actualites_point", consigne, Path(tempfile.gettempdir()), entree=entree,
                                    timeout=300, lire=lambda t: lire_point(t, set(ids)))
    except agent.ErreurAgent as e:
        log(f"point du jour impossible ({e}) : l'ancien reste affiché")
        # pas de nouvel essai à chaque tour : on attend la prochaine fenêtre de 3 h
        db.etat_ecrire("actualites_point", json.dumps({**ancien, "le": maintenant}, ensure_ascii=False))
        return False
    db.etat_ecrire("actualites_point", json.dumps({**lu, "le": maintenant, "ids": ids, "modele": modele},
                                                  ensure_ascii=False))
    log(f"point du jour refait ({len(lu['points'])} points, {modele})")
    return True


# --- Affichage : même info regroupée, lien avec les cours ---------------------------------

def regrouper(liste: list[dict]) -> list[dict]:
    """La même information chez plusieurs sites = une seule carte (la plus récente), les autres en « aussi chez »."""
    cartes, par_evenement = [], {}
    for a in liste:
        a = {**a, "aussi": [], "image_id": a["id"]}
        cle = a.get("evenement")
        if cle and cle in par_evenement:
            tete = par_evenement[cle]   # la plus récente (la liste est triée du plus récent au plus ancien)
            tete["aussi"].append({"id": a["id"], "source": a["source"], "lien": a["lien"], "titre": a["titre"]})
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
    except OSError:
        pass
    return image


# --- Notification du jour : à l'heure où tu lis tes actualités ------------------------------

def _paris(maintenant: dt.datetime | None = None) -> dt.datetime:
    return (maintenant or dt.datetime.now(dt.timezone.utc)).astimezone(notifications.PARIS)


def _habitudes() -> dict[str, list[int]]:
    try:
        h = json.loads(db.etat_valeur("actualites_lectures") or "{}")
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
    db.etat_ecrire("actualites_lectures", json.dumps({j: v for j, v in h.items() if j > limite}))


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
    cle = f"actu-jour:{m.date().isoformat()}"
    if db.etat_valeur("actualites_quotidien") == cle:
        return False
    db.etat_ecrire("actualites_quotidien", cle)
    if m - m.replace(hour=heure.hour, minute=heure.minute, second=0, microsecond=0) > dt.timedelta(hours=3):
        return False   # worker arrêté pendant l'heure prévue : pas de notification « du matin » le soir
    p = point() or {}
    frais = p.get("points") and m.timestamp() - float(p.get("le") or 0) < 20 * 3600
    if frais:
        titre = f"📰 L'essentiel : {len(p['points'])} info{'s' if len(p['points']) > 1 else ''} du jour"
        corps = " · ".join(x["texte"] for x in p["points"][:2])
    else:
        n = db.nb_actualites_nouvelles()
        if not n:
            return False
        titre = f"📰 {n} article{'s' if n > 1 else ''} pour toi"
        corps = "Les nouveautés de tes sujets suivis t'attendent."
    return db.notifier(cle, titre[:120], corps[:300], "/actualites")


# --- Notifications ------------------------------------------------------------------

def annoncer(maintenant: dt.datetime | None = None) -> int:
    """Décide une fois pour toutes, pour chaque article trié, s'il sonne.

    Sonne : une alerte (rare et grave, jugée par l'IA) d'un sujet « à notifier », une seule fois par événement
    même si plusieurs sites en parlent ; ou chaque nouveauté d'un flux marqué « tout m'intéresse ».
    Sans l'IA, rien ne sonne pour les sujets : les mots-clés ne savent pas juger de la gravité.
    """
    articles = db.articles_a_annoncer()
    if not articles:
        return 0
    maintenant = maintenant or dt.datetime.now(dt.timezone.utc)
    actif = notifications.preferences().get("actualites", True) and bool(db.abonnements())
    sujets = {s["id"]: s for s in db.sujets()}
    notifiants = {i for i, s in sujets.items() if s["notifier"]}
    n, vus = 0, set()
    for a in sorted(articles, key=lambda a: -(a["score"] or 0)):
        if not actif or a["publie_le"] < maintenant - dt.timedelta(days=AGE_NOTIF):
            continue
        resume = a["pourquoi"] or a["extrait"][:200]
        url = f"/actualites?article={a['id']}#a{a['id']}"
        if a.get("alerte") and any(s in notifiants for s in a["sujets"]):
            evenement = a.get("evenement") or a["cle"]
            if evenement in vus:
                continue
            vus.add(evenement)
            # La clé de l'événement : la même fuite relayée par trois sites ne sonne qu'une fois
            n += db.notifier(f"actu-alerte:{evenement}"[:400], f"🚨 {a['titre']}"[:120],
                             f"{resume} ({a['source']})"[:300], url)
        elif a["flux_notifier"] and (a["tri"] != "ia" or (a["score"] or 0) >= 0.3 or not a["sujets"]):
            n += db.notifier(f"actu:{a['cle']}"[:400], a["titre"][:120], (f"{a['source']} : {resume}" if resume else a["source"])[:300], url)
    db.articles_annonces([a["id"] for a in articles])
    return n


# --- Boucle du worker -----------------------------------------------------------------

def tour(claude_ok, maintenant: float | None = None):
    maintenant = maintenant or time.time()
    demande = db.etat_consommer("actualites_relever")
    dernier = float(db.etat_valeur("actualites_releve") or 0)
    if demande or maintenant - dernier >= RELEVE:
        db.etat_ecrire("actualites_releve", str(maintenant))
        relever()
    trier(claude_ok)
    annoncer()
    faire_point(claude_ok)
    quotidien()
