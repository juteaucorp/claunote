"""Actualités : flux RSS que tu choisis, triés par l'IA selon les sujets que tu suis, avec notifications.

    flux RSS / Atom ──┐                          ┌─► page Actualités (« Pour toi », tout, favoris)
    (sites ajoutés)   ├─► relevé toutes les ─► articles ─► tri par l'IA (Haiku, par lots de 40) ─┤
    Google Actualités ┘   30 min (ETag)      (dédup par lien)  sinon mots-clés                    └─► notification
    (recherche d'un sujet)                                                                           regroupée

- Dédup : un article = son lien nettoyé (sans utm_…, sans #) ; le même dans deux flux n'apparaît qu'une fois.
- Pas de spam : à son premier relevé, un flux n'annonce rien (ses anciens articles sont rangés sans bruit) ;
  seuls les articles jugés importants (score ≥ 0,75) d'un sujet « à notifier », ou d'un flux « tout
  m'intéresse », sonnent ; plusieurs d'un coup pour un même sujet = une seule notification ; puis la file
  commune (notifications.py) : heures de silence, même texte en moins de 20 h ignoré, plus de 3 = un résumé.
- Sans Claude (limite, panne, réglage) : repérage par mots-clés, refait par l'IA à son retour si possible.
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
SEUIL_NOTIF = 0.75
AGE_NOTIF = 2             # jours : un article plus vieux ne sonne jamais (flux qui republie ses archives)
UA = "Mozilla/5.0 (compatible; Claunote/1.0; lecteur RSS personnel)"
DEFAUT = {"ia": True}

FLUX_DEFAUT = [
    ("https://www.cert.ssi.gouv.fr/alerte/feed/", "CERT-FR : alertes de sécurité"),
    ("https://www.cert.ssi.gouv.fr/actualite/feed/", "CERT-FR : actualités"),
    ("https://www.it-connect.fr/feed/", "IT-Connect"),
    ("https://next.ink/feed/", "Next"),
    ("https://korben.info/feed", "Korben"),
    ("https://www.lemondeinformatique.fr/flux-rss/thematique/toutes-les-actualites/rss.xml", "Le Monde Informatique"),
]
SUJETS_DEFAUT = [   # option SISR du BTS SIO : infrastructure, réseau, sécurité
    ("Cybersécurité", "Failles critiques à corriger, cyberattaques marquantes, rançongiciels, fuites de données, "
     "recommandations de l'ANSSI.",
     ["faille", "vulnérabilité", "CVE", "rançongiciel", "ransomware", "cyberattaque", "phishing", "hameçonnage",
      "ANSSI", "CERT-FR", "fuite de données", "zero-day", "correctif"]),
    ("Réseaux", "Équipements et protocoles réseau (Cisco, Fortinet, pfSense, routage, VLAN, Wi-Fi, IPv6, VPN, "
     "pare-feu) : nouveautés et failles utiles à un étudiant en BTS SIO option SISR.",
     ["Cisco", "Fortinet", "FortiGate", "pfSense", "VLAN", "routeur", "commutateur", "switch", "Wi-Fi", "IPv6",
      "VPN", "pare-feu", "firewall", "BGP", "DNS", "DHCP"]),
    ("Systèmes et virtualisation", "Administration Windows Server, Linux, Active Directory, virtualisation "
     "(Proxmox, VMware, Hyper-V), conteneurs, sauvegarde, supervision et cloud : ce qui sert à un admin système "
     "débutant (BTS SIO option SISR).",
     ["Windows Server", "Linux", "Debian", "Ubuntu", "Active Directory", "Proxmox", "VMware", "Hyper-V", "Docker",
      "Kubernetes", "Veeam", "sauvegarde", "Zabbix", "GLPI", "Ansible", "Azure", "Microsoft 365"]),
]
# Sujets de départ d'avant (PR #52) : remplacés par ceux de l'option SISR s'ils n'ont pas été modifiés
ANCIENS_DEFAUT = {
    "Réseaux et systèmes": "Administration Windows Server, Linux, Active Directory, réseau (Cisco, VLAN, Wi-Fi), "
                           "virtualisation et cloud : nouveautés utiles à un étudiant de BTS SIO.",
    "Développement": "Langages, frameworks et outils de développement (Python, PHP, Java, JavaScript, SQL, Git), "
                     "IA pour développer.",
}

PROMPT = """Tu fais la veille d'actualités d'un étudiant en BTS SIO option SISR (infrastructures, réseaux,
systèmes et cybersécurité ; lycée en France).
Ses sujets suivis :
{sujets}

Sur l'entrée : des articles, un par ligne en JSON (id, source, titre, extrait).
Pour chaque article :
- "sujets" : les codes des sujets qu'il concerne VRAIMENT (pas un simple mot en commun ; [] si aucun) ;
- "score" : son importance pour cet étudiant, de 0 à 1 (0,9 : à ne surtout pas rater, faille critique
  massivement exploitée, annonce majeure ; 0,6 : intéressant ; 0,3 : anecdotique ; 0 : hors sujet, pub, promo) ;
- "pourquoi" : en français, une phrase de 15 mots au plus qui dit ce qu'il y a à retenir (vide si hors sujet).
Réponds seulement en JSON, un objet par article, tous les articles :
[{{"id": 12, "sujets": ["S1"], "score": 0.8, "pourquoi": "..."}}]"""


def log(msg: str):
    print(f"actualités : {msg}", flush=True)


def reglages() -> dict:
    try:
        return {**DEFAUT, **json.loads(db.etat_valeur("actualites") or "{}")}
    except ValueError:
        return dict(DEFAUT)


def enregistrer_reglages(r: dict):
    db.etat_ecrire("actualites", json.dumps({k: r[k] for k in DEFAUT if k in r}))


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


def initialiser():
    """Au tout premier démarrage : une sélection de départ (modifiable sur la page Sources)."""
    if db.etat_valeur("actualites_init"):
        passer_en_sisr()
        return
    db.etat_ecrire("actualites_sisr", "1")
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
                      "publie_le": _date(e)})
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


def prompt(sujets: list[dict]) -> str:
    lignes = [f"{code} — {s['nom']} : {s['description'] or s['nom']}"
              + (f" (mots-clés : {', '.join(s['mots_cles'])})" if s["mots_cles"] else "")
              for code, s in zip(_codes(sujets), sujets)]
    return PROMPT.format(sujets="\n".join(lignes))


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
        resultats[i] = {"id": i, "tri": "ia", "sujets": sujets, "score": max(0.0, min(score, 1.0)),
                        "pourquoi": str(d.get("pourquoi") or "").strip()[:200] or None}
    if not resultats:
        raise agent.ErreurAgent("réponse sans aucun article reconnu")
    return resultats


def _sans_ia(a: dict, sujets: list[dict]) -> dict:
    return {"id": a["id"], "tri": "mots_cles", "sujets": par_mots_cles(sujets, a), "score": None, "pourquoi": None}


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
    consigne = prompt(sujets)
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


# --- Notifications ------------------------------------------------------------------

def _important(a: dict, notifiants: set[int]) -> list[int]:
    """Sujets « à notifier » pour lesquels cet article mérite de sonner."""
    if a["tri"] == "ia" and (a["score"] or 0) < SEUIL_NOTIF:
        return []
    return [s for s in a["sujets"] if s in notifiants]


def annoncer(maintenant: dt.datetime | None = None) -> int:
    """Décide une fois pour toutes, pour chaque article trié, s'il sonne ; regroupe par sujet ou par flux."""
    articles = db.articles_a_annoncer()
    if not articles:
        return 0
    maintenant = maintenant or dt.datetime.now(dt.timezone.utc)
    actif = notifications.preferences().get("actualites", True) and bool(db.abonnements())
    sujets = {s["id"]: s for s in db.sujets()}
    notifiants = {i for i, s in sujets.items() if s["notifier"]}
    groupes: dict[str, list[dict]] = {}
    for a in articles:
        if not actif or a["publie_le"] < maintenant - dt.timedelta(days=AGE_NOTIF):
            continue
        importants = _important(a, notifiants)
        if importants:
            groupe = min((sujets[i]["nom"] for i in importants), key=str.lower)
        elif a["flux_notifier"] and (a["tri"] != "ia" or (a["score"] or 0) >= 0.3 or not a["sujets"]):
            groupe = a["source"]
        else:
            continue
        groupes.setdefault(groupe, []).append(a)
    n = 0
    for groupe, liste in groupes.items():
        liste.sort(key=lambda a: -(a["score"] if a["score"] is not None else 0.8))
        if len(liste) == 1:
            a = liste[0]
            corps = f"{groupe} · {a['source']}" if groupe != a["source"] else a["source"]
            if a["pourquoi"]:
                corps += f" : {a['pourquoi']}"
            n += db.notifier(f"actu:{a['cle']}"[:400], a["titre"][:120], corps[:300], f"/actualites?article={a['id']}#a{a['id']}")
        else:
            ids = sorted(a["id"] for a in liste)
            n += db.notifier(f"actu:{groupe}:{ids[0]}-{ids[-1]}:{len(ids)}"[:400], f"{len(liste)} actualités : {groupe}",
                             " · ".join(a["titre"] for a in liste)[:300], "/actualites")
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
