"""Supervision Cloudflare : ce que Cloudflare bloque AVANT que ça n'arrive sur la VM (offre gratuite, sans IA).

    toutes les 15 min (worker) ──► API GraphQL Analytics (jeton en LECTURE SEULE) ──► état « cloudflare » en base
                                         │                                              │
                                         │                                              ├─► page Réglages → Sécurité
                                         └─ 2 requêtes HTTP, rien d'autre              └─► alerte push si pic anormal

- Éteint tant que le jeton API et l'identifiant de zone ne sont pas saisis (Réglages → Installation → Accès par
  Internet). Le jeton est chiffré comme les autres secrets ; il ne peut rien modifier chez Cloudflare.
- Identifiant de compte facultatif : seulement pour les chiffres du captcha Turnstile (données « compte »).
- Pic : les événements bloqués des 15 dernières minutes comparés à la médiane des 24 dernières heures
  (au moins PIC_MIN et PIC_FOIS × la médiane). Une alerte au plus toutes les 3 h, sauf si le pic double.
- Offre gratuite : événements de sécurité gardés 31 jours, requêtes sur 3 jours au plus ; on ne lit que les 24 h.
"""
import datetime as dt
import json
import os
import re
import statistics
import time

import httpx

from claunote import db

GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
DELAI = 20                       # secondes pour la réponse de Cloudflare
INTERVALLE = 15 * 60             # une relève toutes les 15 min
RETARD = dt.timedelta(minutes=5)  # Cloudflare met quelques minutes à compter : on ne lit pas les toutes dernières
FENETRE = dt.timedelta(minutes=15)
HISTORIQUE = 96                  # 24 h de fenêtres de 15 min
PIC_MIN = int(os.environ.get("CLOUDFLARE_PIC_MIN") or 60)
PIC_FOIS = 5
ALERTE_ESPACEE = 3 * 3600
CLE_ETAT = "cloudflare"
ID = re.compile(r"^[0-9a-f]{32}$")

SOURCES = {
    "firewallCustom": "Règle personnalisée", "firewallrules": "Règle de pare-feu", "ratelimit": "Limite de débit",
    "firewallManaged": "Règles gérées (WAF)", "waf": "Règles gérées (WAF)", "securitylevel": "Niveau de sécurité",
    "bic": "Contrôle d'intégrité du navigateur", "hot": "Protection hotlink", "uablock": "Blocage d'agent utilisateur",
    "zonelockdown": "Verrouillage de zone", "ip": "Règle d'accès IP", "country": "Règle d'accès pays",
    "asn": "Règle d'accès ASN", "botFight": "Bot Fight Mode", "botfight": "Bot Fight Mode",
    "l7ddos": "Protection DDoS", "validation": "Requête invalide", "apiShield": "API Shield",
}
ACTIONS = {
    "block": "bloqué", "managed_challenge": "défi géré", "challenge": "défi", "jschallenge": "défi JavaScript",
    "challenge_failed": "défi raté", "jschallenge_failed": "défi raté", "managed_challenge_failed": "défi raté",
    "challenge_solved": "défi réussi", "jschallenge_solved": "défi réussi", "managed_challenge_solved": "défi réussi",
    "managed_challenge_skipped": "défi évité", "challenge_bypassed": "défi évité",
    "log": "noté", "skip": "laissé passer", "allow": "autorisé", "connection_close": "connexion fermée",
    "drop": "abandonné",
}
DEFIS = {"managed_challenge", "challenge", "jschallenge"}
TURNSTILE = {"challenge": "captchas affichés", "solved": "réussis", "failed": "ratés", "pass": "réussis",
             "fail": "ratés"}


class Refus(Exception):
    """Jeton refusé ou permission manquante : rien ne sert de réessayer avant qu'il soit changé."""


def log(msg: str):
    print(f"cloudflare : {msg}", flush=True)


def _jeton() -> str:
    return (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()


def _zone() -> str:
    return (os.environ.get("CLOUDFLARE_ZONE_ID") or "").strip().lower()


def _compte() -> str:
    return (os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip().lower()


def actif() -> bool:
    return bool(_jeton() and ID.match(_zone()))


def id_ok(valeur: str) -> bool:
    return bool(ID.match((valeur or "").strip().lower()))


# --- Appels -------------------------------------------------------------------------

def _appel(jeton: str, requete: str) -> dict:
    """Une requête GraphQL ; lève Refus (jeton, permission) ou httpx.HTTPError (réseau, panne)."""
    r = httpx.post(GRAPHQL, json={"query": requete}, timeout=DELAI,
                   headers={"Authorization": f"Bearer {jeton}", "Accept": "application/json"})
    if r.status_code in (400, 401, 403):
        try:
            erreurs = r.json().get("errors") or []
        except ValueError:
            erreurs = []
        raise Refus("; ".join(str(e.get("message", e)) for e in erreurs)[:300] or f"HTTP {r.status_code}")
    r.raise_for_status()
    corps = r.json()
    if corps.get("errors"):
        messages = "; ".join(str(e.get("message", e)) for e in corps["errors"])[:300]
        if re.search(r"auth|permission|not authorized|access", messages, re.I):
            raise Refus(messages)
        raise ValueError(messages)
    return corps.get("data") or {}


def _t(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _filtre(debut: dt.datetime, fin: dt.datetime) -> str:
    return f'{{datetime_geq: "{_t(debut)}", datetime_lt: "{_t(fin)}"}}'


def _requete_zone(zone: str, fin: dt.datetime, regle: str = "description") -> str:
    jour, fenetre = _filtre(fin - dt.timedelta(hours=24), fin), _filtre(fin - FENETRE, fin)
    heures = f'{{datetime_geq: "{_t(fin - dt.timedelta(hours=24))}", datetime_lt: "{_t(fin)}"}}'
    groupes = "firewallEventsAdaptiveGroups"
    return f"""{{ viewer {{ zones(filter: {{zoneTag: "{zone}"}}) {{
      regles: {groupes}(limit: 25, filter: {jour}, orderBy: [count_DESC]) {{ count dimensions {{ action source {regle} }} }}
      pays: {groupes}(limit: 10, filter: {jour}, orderBy: [count_DESC]) {{ count dimensions {{ clientCountryName }} }}
      chemins: {groupes}(limit: 8, filter: {jour}, orderBy: [count_DESC]) {{ count dimensions {{ clientRequestPath }} }}
      fenetre: {groupes}(limit: 1, filter: {fenetre}) {{ count }}
      trafic: httpRequests1hGroups(limit: 24, filter: {heures}, orderBy: [datetime_ASC]) {{
        dimensions {{ datetime }} sum {{ requests threats }} }}
    }} }} }}"""


def _requete_turnstile(compte: str, fin: dt.datetime) -> str:
    return f"""{{ viewer {{ accounts(filter: {{accountTag: "{compte}"}}) {{
      turnstileAdaptiveGroups(limit: 20, filter: {_filtre(fin - dt.timedelta(hours=24), fin)}, orderBy: [count_DESC]) {{
        count dimensions {{ eventType }} }}
    }} }} }}"""


def _zone_brute(jeton: str, zone: str, fin: dt.datetime) -> dict:
    try:
        data = _appel(jeton, _requete_zone(zone, fin))
    except ValueError as e:   # champ « description » inconnu de cette offre : on se rabat sur l'identifiant de règle
        if "description" not in str(e):
            raise
        data = _appel(jeton, _requete_zone(zone, fin, "ruleId"))
    zones = (data.get("viewer") or {}).get("zones") or []
    if not zones:
        raise Refus("zone introuvable avec ce jeton : vérifie l'identifiant de zone et la ressource du jeton")
    return zones[0]


# --- Mise en forme -------------------------------------------------------------------

def resumer(z: dict) -> dict:
    """Réponse GraphQL → ce qu'affiche la page (petits nombres, libellés en français)."""
    regles = []
    for g in z.get("regles") or []:
        d = g.get("dimensions") or {}
        source = d.get("source") or ""
        nom = (d.get("description") or "").strip() or (d.get("ruleId") or "")
        regles.append({"n": g.get("count", 0), "source": SOURCES.get(source, source or "?"),
                       "regle": nom[:80], "action": ACTIONS.get(d.get("action") or "", d.get("action") or "?"),
                       "defi": (d.get("action") or "") in DEFIS})
    pays = [{"n": g.get("count", 0), "pays": (g.get("dimensions") or {}).get("clientCountryName") or "?"}
            for g in z.get("pays") or []]
    chemins = [{"n": g.get("count", 0), "chemin": ((g.get("dimensions") or {}).get("clientRequestPath") or "/")[:80]}
               for g in z.get("chemins") or []]
    trafic = z.get("trafic") or []
    fenetre = sum(g.get("count", 0) for g in z.get("fenetre") or [])
    return {
        "evenements_24h": sum(r["n"] for r in regles), "defis_24h": sum(r["n"] for r in regles if r["defi"]),
        "bloques_24h": sum(r["n"] for r in regles if r["action"] == "bloqué"),
        "requetes_24h": sum((g.get("sum") or {}).get("requests", 0) for g in trafic),
        "menaces_24h": sum((g.get("sum") or {}).get("threats", 0) for g in trafic),
        "regles": regles, "pays": pays, "chemins": chemins, "fenetre": fenetre}


def resumer_turnstile(data: dict) -> list[dict]:
    comptes = (data.get("viewer") or {}).get("accounts") or []
    groupes = comptes[0].get("turnstileAdaptiveGroups") or [] if comptes else []
    return [{"n": g.get("count", 0), "type": TURNSTILE.get((g.get("dimensions") or {}).get("eventType") or "",
                                                           (g.get("dimensions") or {}).get("eventType") or "?")}
            for g in groupes]


def pic(n: int, historique: list[int]) -> int | None:
    """Seuil dépassé par n (ou None) : au moins PIC_MIN, et PIC_FOIS fois la médiane des 24 h."""
    base = statistics.median(historique) if historique else 0
    seuil = max(PIC_MIN, int(PIC_FOIS * base) + 1)
    return seuil if n >= seuil else None


# --- État ----------------------------------------------------------------------------

def etat() -> dict:
    try:
        return json.loads(db.etat_valeur(CLE_ETAT) or "{}")
    except ValueError:
        return {}


def _ecrire(e: dict):
    db.etat_ecrire(CLE_ETAT, json.dumps(e, ensure_ascii=False))


def due(e: dict, maintenant: float | None = None) -> bool:
    return actif() and (maintenant or time.time()) - e.get("tick", 0) >= INTERVALLE


def tour(compter: bool = True, maintenant: dt.datetime | None = None) -> dict:
    """Une relève. `compter` : la fenêtre de 15 min entre dans l'historique et peut déclencher l'alerte de pic
    (faux pour le bouton « Relever maintenant », qui ne doit ni fausser la médiane ni alerter deux fois)."""
    from claunote import securite   # import tardif : securite → notifications → beaucoup de modules
    e = etat()
    if not actif():
        return e
    maintenant = maintenant or dt.datetime.now(dt.timezone.utc)
    fin = (maintenant - RETARD).replace(second=0, microsecond=0)
    if compter:
        e["tick"] = time.time()
    avant_ok = not e.get("erreur")
    try:
        resume = resumer(_zone_brute(_jeton(), _zone(), fin))
    except (Refus, httpx.HTTPError, ValueError) as err:
        refus = isinstance(err, Refus)
        e["erreur"] = (("Jeton ou permissions refusés : " if refus else "Cloudflare injoignable : ")
                       + (str(err) or type(err).__name__))[:300]
        e["erreur_le"] = maintenant.isoformat()
        _ecrire(e)
        log(e["erreur"])
        if avant_ok and refus:   # noté une fois quand ça casse, pas à chaque relève
            securite.noter("cloudflare_erreur", None, e["erreur"])
        return e
    turnstile, turnstile_erreur = None, None
    if id_ok(_compte()):
        try:
            turnstile = resumer_turnstile(_appel(_jeton(), _requete_turnstile(_compte(), fin)))
        except (Refus, httpx.HTTPError, ValueError) as err:
            turnstile_erreur = (str(err) or type(err).__name__)[:200]
    e.update({"le": maintenant.isoformat(), "fin": fin.isoformat(), "resume": resume, "turnstile": turnstile,
              "turnstile_erreur": turnstile_erreur, "erreur": None})
    if compter:
        historique = [n for _, n in e.get("fenetres", [])]
        seuil = pic(resume["fenetre"], historique)
        e["fenetres"] = (e.get("fenetres", []) + [[fin.isoformat(), resume["fenetre"]]])[-HISTORIQUE:]
        if seuil:
            _alerte_pic(e, resume, seuil, historique, securite)
    _ecrire(e)
    return e


def _alerte_pic(e: dict, resume: dict, seuil: int, historique: list[int], securite):
    n = resume["fenetre"]
    derniere = e.get("alerte") or {}
    if time.time() - derniere.get("t", 0) < ALERTE_ESPACEE and n < 2 * derniere.get("n", 0):
        return   # même vague d'attaque : déjà signalée
    e["alerte"] = {"t": time.time(), "n": n}
    base = int(statistics.median(historique)) if historique else 0
    top = resume["regles"][0] if resume["regles"] else None
    pays = ", ".join(p["pays"] for p in resume["pays"][:3])
    detail = (f"{n} événements en 15 min (d'habitude {base}, seuil {seuil})"
              + (f" ; surtout {top['source'].lower()}{' « ' + top['regle'] + ' »' if top['regle'] else ''}" if top else "")
              + (f" ; pays : {pays}" if pays else ""))
    securite.noter("cloudflare_pic", None, detail,
                   alerte=f"{detail}. Cloudflare les arrête avant ta VM : rien à faire si le site marche.",
                   titre="Pic d'attaques arrêté par Cloudflare")


# --- Bouton « Tester » ---------------------------------------------------------------

def tester(jeton: str, zone: str, compte: str = "") -> tuple[str, str]:
    """(« ok », « refuse » ou « injoignable », message) : une vraie petite requête, en lecture seule."""
    fin = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    try:
        z = resumer(_zone_brute(jeton, zone.lower(), fin))
    except Refus as e:
        return "refuse", (f"Cloudflare refuse : {e}. Il faut Zone → Analytics → Lecture sur cette zone "
                          "(voir docs/cloudflare-tunnel.md, étape 11).")
    except (httpx.HTTPError, ValueError) as e:
        return "injoignable", f"Cloudflare ne répond pas comme prévu ({str(e)[:150] or type(e).__name__})."
    message = (f"Jeton accepté : {z['evenements_24h']} événement(s) de sécurité et {z['requetes_24h']} requête(s) "
               "en 24 h.")
    if compte:
        try:
            _appel(jeton, _requete_turnstile(compte.lower(), fin))
            message += " Chiffres Turnstile lisibles."
        except (Refus, httpx.HTTPError, ValueError):
            message += (" Turnstile illisible : ajoute Compte → Account Analytics → Lecture au jeton "
                        "(ou vide l'identifiant de compte).")
    return "ok", message
