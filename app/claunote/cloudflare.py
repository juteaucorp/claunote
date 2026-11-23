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
import collections
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


class Indisponible(Exception):
    """Jeu de données ou champ fermé sur cette offre Cloudflare (« does not have access to the path »)."""


AUTH = re.compile(r"authenticat|not authorized|unauthori[sz]ed|invalid (api )?token|permission", re.I)
ACCES_OFFRE = re.compile(r"does not have access to|not available (on|for) (your|this) plan|plan does not", re.I)
BRUTS = 1000   # repli événements un par un : au plus 1000 sur 24 h (échantillon de Cloudflare)


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

def _classer(messages: str, statut: int = 200) -> Exception:
    """Erreur GraphQL → Refus (vrai problème de jeton), Indisponible (jeu de données ou champ hors de l'offre)
    ou ValueError (autre). « does not have access to the path » = offre, PAS permission du jeton."""
    if ACCES_OFFRE.search(messages):
        return Indisponible(messages)
    if statut in (401, 403) or AUTH.search(messages):
        return Refus(messages)
    return ValueError(messages)


def _appel(jeton: str, requete: str) -> dict:
    """Une requête GraphQL ; lève Refus, Indisponible, ValueError ou httpx.HTTPError (réseau, panne)."""
    r = httpx.post(GRAPHQL, json={"query": requete}, timeout=DELAI,
                   headers={"Authorization": f"Bearer {jeton}", "Accept": "application/json"})
    try:
        corps = r.json()
    except ValueError:
        corps = {}
    erreurs = corps.get("errors") or [] if isinstance(corps, dict) else []
    messages = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in erreurs)[:300]
    if r.status_code in (400, 401, 403):
        raise _classer(messages or f"HTTP {r.status_code}", r.status_code)
    r.raise_for_status()
    if erreurs:
        raise _classer(messages)
    return corps.get("data") or {}


def _t(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _filtre(debut: dt.datetime, fin: dt.datetime) -> str:
    return f'{{datetime_geq: "{_t(debut)}", datetime_lt: "{_t(fin)}"}}'


def _zones(jeton: str, zone: str, corps: str) -> dict:
    data = _appel(jeton, f'{{ viewer {{ zones(filter: {{zoneTag: "{zone}"}}) {{ {corps} }} }} }}')
    zones = (data.get("viewer") or {}).get("zones") or []
    if not zones:
        raise Refus("zone introuvable avec ce jeton : vérifie l'identifiant de zone et la ressource « Zone » du jeton")
    return zones[0]


def _requete_turnstile(compte: str, fin: dt.datetime) -> str:
    return f"""{{ viewer {{ accounts(filter: {{accountTag: "{compte}"}}) {{
      turnstileAdaptiveGroups(limit: 20, filter: {_filtre(fin - dt.timedelta(hours=24), fin)}, orderBy: [count_DESC]) {{
        count dimensions {{ eventType }} }}
    }} }} }}"""


def _premier(essais):
    """Essaie chaque variante (de la plus riche à la plus simple) ; renvoie la première qui passe, sinon relève
    la dernière erreur. Un jeton refusé coupe court : inutile d'essayer plus simple."""
    derniere = None
    for faire in essais:
        try:
            return faire()
        except (Indisponible, ValueError) as e:
            derniere = e
    raise derniere


def _evenements_groupes(jeton, zone, jour, fenetre) -> dict:
    """firewallEventsAdaptiveGroups : déjà comptés par Cloudflare (une requête par tableau, chacun peut manquer)."""
    g = "firewallEventsAdaptiveGroups"
    sortie = {"fenetre": _zones(jeton, zone, f"fenetre: {g}(limit: 1, filter: {fenetre}) {{ count }}")["fenetre"]}

    def regles(champs):
        return lambda: _zones(jeton, zone, f"r: {g}(limit: 25, filter: {jour}, orderBy: [count_DESC]) "
                                           f"{{ count dimensions {{ {champs} }} }}")["r"]
    sortie["regles"] = _premier([regles("action source description"), regles("action source ruleId"),
                                 regles("action source")])
    for cle, champ, n in (("pays", "clientCountryName", 10), ("chemins", "clientRequestPath", 8)):
        try:
            sortie[cle] = _zones(jeton, zone, f"r: {g}(limit: {n}, filter: {jour}, orderBy: [count_DESC]) "
                                              f"{{ count dimensions {{ {champ} }} }}")["r"]
        except (Indisponible, ValueError):
            sortie[cle] = []
    return sortie


def _evenements_bruts(jeton, zone, jour, debut_fenetre: str) -> dict:
    """Repli : firewallEventsAdaptive (événements un par un, échantillon) comptés ici, si les groupes sont fermés."""
    def lignes(champs):
        return lambda: _zones(jeton, zone, f"r: firewallEventsAdaptive(limit: {BRUTS}, filter: {jour}, "
                                           f"orderBy: [datetime_DESC]) {{ datetime {champs} }}")["r"]
    rows = _premier([lignes("action source description clientCountryName clientRequestPath"),
                     lignes("action source ruleId clientCountryName clientRequestPath"),
                     lignes("action source clientCountryName"), lignes("action source")])

    def compter(champs, n):
        c = collections.Counter(tuple(r.get(k) for k in champs) for r in rows if any(r.get(k) for k in champs))
        return [{"count": nb, "dimensions": dict(zip(champs, cle))} for cle, nb in c.most_common(n)]
    regle = "description" if any("description" in r for r in rows) else "ruleId"
    return {"regles": compter(("action", "source", regle), 25), "pays": compter(("clientCountryName",), 10),
            "chemins": compter(("clientRequestPath",), 8),
            "fenetre": [{"count": sum(1 for r in rows if (r.get("datetime") or "") >= debut_fenetre)}],
            "echantillon": len(rows) >= BRUTS}


def _zone_brute(jeton: str, zone: str, fin: dt.datetime) -> dict:
    """Chaque jeu de données est lu à part : celui que l'offre gratuite ferme manque, les autres s'affichent."""
    jour, fenetre = _filtre(fin - dt.timedelta(hours=24), fin), _filtre(fin - FENETRE, fin)
    sortie, manques, details = {}, [], []
    try:
        sortie["trafic"] = _zones(jeton, zone, f"t: httpRequests1hGroups(limit: 24, filter: {jour}, "
                                               "orderBy: [datetime_ASC]) { dimensions { datetime } sum { requests threats } }")["t"]
    except (Indisponible, ValueError) as e:
        manques.append("trafic")
        details.append(f"trafic : {str(e)[:120]}")
    try:
        try:
            sortie.update(_evenements_groupes(jeton, zone, jour, fenetre))
        except (Indisponible, ValueError):
            sortie.update(_evenements_bruts(jeton, zone, jour, _t(fin - FENETRE)))
    except (Indisponible, ValueError) as e:
        manques.append("événements de sécurité")
        details.append(f"événements : {str(e)[:120]}")
        sortie["sans_evenements"] = True
    if len(manques) == 2:
        raise Indisponible(" ; ".join(details))
    if details:
        log("pas disponible avec cette offre : " + " ; ".join(details))
    sortie["manques"] = manques
    return sortie


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
        "regles": regles, "pays": pays, "chemins": chemins, "fenetre": fenetre,
        "manques": z.get("manques") or [], "echantillon": bool(z.get("echantillon")),
        "sans_evenements": bool(z.get("sans_evenements"))}


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
    except (Refus, Indisponible, httpx.HTTPError, ValueError) as err:
        refus = isinstance(err, Refus)
        prefixe = ("Jeton refusé : " if refus else "Rien de lisible avec ton offre Cloudflare : "
                   if isinstance(err, Indisponible) else "Cloudflare injoignable : ")
        e["erreur"] = (prefixe + (str(err) or type(err).__name__))[:300]
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
        except (Refus, Indisponible, httpx.HTTPError, ValueError) as err:
            turnstile_erreur = (str(err) or type(err).__name__)[:200]
    e.update({"le": maintenant.isoformat(), "fin": fin.isoformat(), "resume": resume, "turnstile": turnstile,
              "turnstile_erreur": turnstile_erreur, "erreur": None})
    if compter and not resume["sans_evenements"]:
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
    """(« ok », « refuse », « indisponible » ou « injoignable », message) : de vraies petites requêtes, en lecture."""
    fin = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    try:
        z = resumer(_zone_brute(jeton, zone.lower(), fin))
    except Refus as e:
        return "refuse", (f"Cloudflare refuse le jeton : {e}. Vérifie Zone → Analytics → Lecture sur ce domaine, "
                          "et que le filtre d'adresses IP du jeton autorise l'adresse publique de ta VM.")
    except Indisponible as e:
        return "indisponible", (f"Jeton accepté, mais Cloudflare ne donne aucune de ces données avec ton offre ({e}).")
    except (httpx.HTTPError, ValueError) as e:
        return "injoignable", f"Cloudflare ne répond pas comme prévu ({str(e)[:150] or type(e).__name__})."
    parts = []
    if not z["sans_evenements"]:
        parts.append(f"{'au moins ' if z['echantillon'] else ''}{z['evenements_24h']} événement(s) de sécurité")
    if not "trafic" in z["manques"]:
        parts.append(f"{z['requetes_24h']} requête(s)")
    message = f"Jeton accepté : {' et '.join(parts)} en 24 h."
    if z["manques"]:
        message += " Pas disponible avec ton offre (ignoré) : " + " ; ".join(z["manques"]) + "."
    if compte:
        try:
            _appel(jeton, _requete_turnstile(compte.lower(), fin))
            message += " Chiffres Turnstile lisibles."
        except Refus:
            message += (" Turnstile illisible : ajoute Compte → Account Analytics → Lecture au jeton "
                        "(ou vide l'identifiant de compte).")
        except (Indisponible, httpx.HTTPError, ValueError):
            message += " Chiffres Turnstile pas disponibles avec ton offre (ignorés)."
    return "ok", message
