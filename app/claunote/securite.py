"""Sécurité du site ouvert sur Internet : journal, alertes push, blocages automatiques.

    requête ──► pays autorisé ? ──► adresse bannie ? ──► appareil approuvé ? ──► session ? ──► page
                    │ non               │ oui                │ non                  │ non
                    ▼                   ▼                    ▼                      ▼
                  refus 403          refus 403          page « code »         page de connexion
                    └──────────────┬────┴────────────────────┴──────────────────────┘
                                   ▼
                     journal (table securite, page Réglages → Sécurité)
                                   └─► alerte push tout de suite (même la nuit) si c'est suspect

Règles (par Internet = arrivé par Cloudflare Tunnel ; le VPN n'est jamais bloqué, pour ne pas t'enfermer dehors) :
- mot de passe, Face ID ou code d'appareil refusé : noté ; alerte au 1er code refusé, au 3e échec de connexion,
  puis à 10, 30, 100 en 15 min ;
- 20 échecs d'une même adresse en 24 h : adresse bannie 24 h (par Internet) ;
- 30 échecs par Internet en 15 min, toutes adresses confondues : connexions par Internet fermées 30 min ;
- pays hors de CLAUNOTE_PAYS (défaut FR ; « * » pour tout accepter) : refusé, même si la règle Cloudflare saute ;
- appareil non approuvé : noté une fois par heure et par adresse ; alerte dès 5 adresses différentes en 1 h ;
  une adresse qui insiste (100 requêtes en 10 min) est bannie 24 h ;
- nouvel appareil, clé Face ID ajoutée, mot de passe changé, « déconnecter partout » : alerte à chaque fois ;
- connexion réussie par Internet : alerte (rare : une session dure 30 jours) ; par le VPN : seulement si
  l'adresse est nouvelle ;
- requête venue d'un autre site (CSRF), en-tête Cloudflare falsifié sur le réseau local : refus + alerte ;
- 5 erreurs serveur (500) en 5 min : alerte.
Une alerte du même genre ne repart pas avant 10 min (sauf connexion réussie, nouvel appareil, mot de passe, clé : toujours).
"""
import collections
import datetime as dt
import os
import threading
import time

from claunote import appareils, db

FENETRE_ECHECS = 15 * 60
PALIERS_ECHECS = (3, 10, 30, 100)
BAN_ECHECS, BAN_FENETRE, BAN_DUREE = 20, 24 * 3600, 24 * 3600
VERROU_ECHECS, VERROU_DUREE = 30, 30 * 60
INCONNUS_FENETRE, PALIERS_INCONNUS = 3600, (5, 20, 100)
INSISTANCE, INSISTANCE_FENETRE = 100, 10 * 60
ERREURS_FENETRE, ERREURS_SEUIL = 5 * 60, 5
ALERTE_ESPACEE = 10 * 60
ECHECS = {"connexion_echec", "faceid_echec", "code_echec"}

# type → (gravité, libellé) ; gravité : info, attention, alerte
TYPES = {
    "connexion_ok": ("info", "Connexion réussie"),
    "connexion_echec": ("attention", "Mot de passe refusé"),
    "faceid_echec": ("attention", "Face ID refusé"),
    "code_echec": ("attention", "Code d'appareil refusé"),
    "captcha": ("attention", "Captcha Turnstile refusé"),
    "code_cree": ("info", "Code d'appareil créé"),
    "appareil_ajoute": ("alerte", "Nouvel appareil approuvé"),
    "appareil_retire": ("attention", "Appareil retiré"),
    "appareil_deplace": ("attention", "Appareil donné à un autre compte"),
    "appareil_inconnu": ("info", "Appareil non approuvé"),
    "appareil_revoque": ("alerte", "Cookie d'appareil inconnu ou retiré"),
    "cle_ajoutee": ("alerte", "Clé Face ID ajoutée"),
    "cle_supprimee": ("attention", "Clé Face ID supprimée"),
    "mot_de_passe": ("alerte", "Mot de passe changé"),
    "deconnexion_partout": ("alerte", "Tous les appareils déconnectés"),
    "mot_de_passe_defaut": ("alerte", "Connexion par Internet refusée : mot de passe par défaut"),
    "pays": ("alerte", "Accès depuis un pays non autorisé"),
    "banni": ("alerte", "Adresse bannie 24 h"),
    "verrou": ("alerte", "Connexions par Internet fermées 30 min"),
    "csrf": ("alerte", "Requête venue d'un autre site refusée"),
    "entete_falsifie": ("alerte", "En-tête Cloudflare falsifié"),
    "erreurs_serveur": ("attention", "Erreurs du serveur en rafale"),
    "erreur_serveur": ("info", "Erreur du serveur"),
    "deblocage": ("info", "Blocages levés"),
    "mise_a_jour": ("info", "Mise à jour demandée"),
    "invitation": ("attention", "Jeton d'invitation créé"),
    "invitation_annulee": ("info", "Jeton d'invitation annulé"),
    "invitation_echec": ("attention", "Jeton d'invitation refusé"),
    "compte_cree": ("alerte", "Nouveau compte créé"),
    "compte_modifie": ("attention", "Compte modifié (droits, rôle, accès)"),
    "notification_test": ("info", "Notification de test envoyée à des comptes"),
    "compte_deconnecte": ("info", "Compte déconnecté de ses appareils"),
    "compte_supprime": ("alerte", "Compte supprimé"),
    "reglage": ("alerte", "Réglage de l'installation changé"),
    "lien_mot_de_passe": ("attention", "Lien de nouveau mot de passe créé"),
    "tout_voir": ("attention", "Admin : « Tout voir »"),
    "apercu": ("attention", "Admin : « Voir en tant que »"),
    "conditions": ("info", "Conditions d'utilisation acceptées"),
    "cloudflare_pic": ("alerte", "Pic d'attaques arrêté par Cloudflare"),
    "cloudflare_erreur": ("attention", "Supervision Cloudflare en panne"),
}
TOUJOURS = {"appareil_ajoute", "cle_ajoutee", "mot_de_passe", "deconnexion_partout", "connexion_ok", "compte_cree",
            "compte_supprime"}

_verrou_etat = threading.Lock()
_echecs: collections.deque = collections.deque()                  # (instant, ip, par Internet)
_echecs_ip: dict[str, collections.deque] = collections.defaultdict(collections.deque)
_bannis: dict[str, float] = {}                                    # ip → fin du ban
_verrou_internet = {"jusqu_a": 0.0}
_inconnus: dict[str, float] = {}                                  # ip → dernière fois notée
_inconnus_hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
_erreurs: collections.deque = collections.deque()
_derniere_alerte: dict[str, float] = {}
_paliers_faits: dict[str, set] = collections.defaultdict(set)


def log(msg: str):
    print(f"sécurité : {msg}", flush=True)


def pays_autorises() -> set[str] | None:
    """None = tous les pays acceptés."""
    brut = os.environ.get("CLAUNOTE_PAYS", "FR").strip().upper()
    if brut in ("*", ""):
        return None
    return {p.strip() for p in brut.split(",") if p.strip()}


def pays(request) -> str | None:
    return request.headers.get("cf-ipcountry") if appareils.par_tunnel(request.headers) else None


def _vider(file: collections.deque, fenetre: float, maintenant: float, cle=lambda e: e):
    while file and maintenant - cle(file[0]) > fenetre:
        file.popleft()


# --- Journal et alertes ---------------------------------------------------------

def noter(type: str, request=None, detail: str = "", ip: str | None = None, alerte: bool | str = False,
          titre: str | None = None):
    """Note l'événement ; `alerte` (vrai ou texte du message) envoie aussi une notification push.

    Ne lève jamais : un journal en panne ne doit pas empêcher de se connecter."""
    gravite, libelle = TYPES.get(type, ("info", type))
    internet = bool(request is not None and appareils.par_tunnel(request.headers))
    ip = ip or (appareils.ip_client(request) if request is not None else None)
    pays_ = pays(request) if request is not None else None
    chemin = request.url.path[:200] if request is not None else None
    navigateur = (request.headers.get("user-agent") or "")[:300] if request is not None else None
    try:
        evenement = db.securite_noter(type, gravite, ip, pays_, internet, chemin, navigateur, detail[:1000], False)
    except Exception as e:
        log(f"journal indisponible ({e}) : {type} {ip} {detail}")
        evenement = None
    log(f"{type} {ip or ''} {pays_ or ''} {detail}".strip())
    if alerte:
        corps = alerte if isinstance(alerte, str) else detail
        lieu = " · ".join(x for x in (("Internet" if internet else "VPN/réseau local") if request is not None else "",
                                       ip or "", pays_ or "") if x)
        alerter(type, titre or libelle, f"{corps} ({lieu})" if lieu else corps, evenement)


def alerter(type: str, titre: str, corps: str, evenement: int | None = None) -> bool:
    """Notification tout de suite, sur chaque appareil abonné, même pendant les heures de silence."""
    maintenant = time.monotonic()
    with _verrou_etat:
        if type not in TOUJOURS and maintenant - _derniere_alerte.get(type, -1e9) < ALERTE_ESPACEE:
            return False
        _derniere_alerte[type] = maintenant
    from claunote import notifications   # import tardif : notifications importe pronote et revision
    if not notifications.preferences(None).get("securite", True):   # réglage des admins
        return False
    try:
        notif = db.notification_directe(f"securite:{type}:{time.time_ns()}", f"🔒 {titre}", corps[:300], "/securite")
    except Exception as e:
        log(f"alerte non enregistrée : {e}")
        notif = None

    def envoyer():
        try:
            envoyes, erreurs = notifications.envoyer_a_tous(f"🔒 {titre}", corps[:300], "/securite", f"securite-{type}",
                                                             compte_id=None)
            if notif:
                db.maj_notification(notif, "envoye" if envoyes or not erreurs else "erreur",
                                    "; ".join(erreurs) or None)
            if evenement:
                db.securite_alerte_envoyee(evenement)
        except Exception as e:
            log(f"alerte non envoyée : {e}")

    threading.Thread(target=envoyer, daemon=True).start()
    return True


# --- Échecs de connexion, bans, verrou ------------------------------------------

def bannie(ip: str, maintenant: float | None = None) -> int:
    """Secondes de ban restantes pour cette adresse (accès par Internet)."""
    maintenant = maintenant or time.time()
    fin = _bannis.get(ip, 0)
    if fin and fin <= maintenant:
        _bannis.pop(ip, None)
        return 0
    return int(fin - maintenant) if fin else 0


def verrouille(maintenant: float | None = None) -> int:
    """Secondes pendant lesquelles les connexions par Internet restent fermées."""
    reste = _verrou_internet["jusqu_a"] - (maintenant or time.time())
    return int(reste) if reste > 0 else 0


def bannir(ip: str, raison: str, request=None):
    _bannis[ip] = time.time() + BAN_DUREE
    noter("banni", request, f"{ip} : {raison}", ip=ip, alerte=f"{ip} bannie 24 h : {raison}.")


def echec(type: str, request, detail: str = ""):
    """Mot de passe, Face ID ou code refusé : noté, compté, alerte et blocage au besoin."""
    maintenant = time.time()
    ip = appareils.ip_client(request)
    internet = appareils.par_tunnel(request.headers)
    with _verrou_etat:
        _echecs.append((maintenant, ip, internet))
        _vider(_echecs, FENETRE_ECHECS, maintenant, lambda e: e[0])
        n = len(_echecs)
        n_internet = sum(1 for e in _echecs if e[2])
        file_ip = _echecs_ip[ip]
        file_ip.append(maintenant)
        _vider(file_ip, BAN_FENETRE, maintenant)
        n_ip = len(file_ip)
        for vieille in [k for k, f in _echecs_ip.items() if not f or maintenant - f[-1] > BAN_FENETRE]:
            _echecs_ip.pop(vieille, None)   # adresses sans échec depuis 24 h : oubliées (mémoire bornée)
        if n == 1:
            _paliers_faits["echecs"].clear()
        palier = next((p for p in PALIERS_ECHECS if n >= p and p not in _paliers_faits["echecs"]), None)
        if palier:
            _paliers_faits["echecs"].add(palier)
    noter(type, request, detail, ip=ip)
    if type == "code_echec":   # au plus une alerte par 10 min
        alerter("code_echec", TYPES[type][1], "Quelqu'un a tapé un mauvais code d'appareil. Si ce n'est pas toi, "
                                              f"quelqu'un connaît l'adresse du site ({ip}).")
    if palier:
        _derniere_alerte.pop("echecs", None)   # chaque palier part, même rapproché
        alerter("echecs", "Échecs de connexion",
                f"{n} échecs de connexion en 15 min. Dernier : {TYPES[type][1].lower()} ({ip}).")
    if internet and n_ip >= BAN_ECHECS and not bannie(ip):
        bannir(ip, f"{n_ip} échecs en 24 h", request)
    if internet and n_internet >= VERROU_ECHECS and not verrouille():
        _verrou_internet["jusqu_a"] = maintenant + VERROU_DUREE
        noter("verrou", request, f"{n_internet} échecs par Internet en 15 min",
              alerte=f"{n_internet} échecs par Internet en 15 min : connexions par Internet fermées 30 min. "
                     "Le VPN marche toujours.")


def reussite(request, comment: str):
    """Connexion réussie : notée ; alerte si par Internet ou depuis une adresse jamais vue."""
    ip = appareils.ip_client(request)
    internet = appareils.par_tunnel(request.headers)
    try:
        nouvelle = not db.securite_ip_connue(ip)
    except Exception:
        nouvelle = False
    appareil = appareils.reconnu(request.cookies.get(appareils.COOKIE)) if internet else None
    detail = f"{comment}{' depuis ' + appareil['nom'] if appareil else ''}{' · nouvelle adresse' if nouvelle else ''}"
    noter("connexion_ok", request, detail, ip=ip,
          alerte=(f"Connexion par Internet ({detail}). Si ce n'est pas toi : Réglages → Sécurité → "
                  "« Déconnecter partout » et change le mot de passe.") if internet or nouvelle else False,
          titre="Connexion par Internet" if internet else "Connexion depuis une nouvelle adresse")
    with _verrou_etat:
        _echecs_ip.pop(ip, None)


def debloquer(request=None):
    """Bouton de la page Sécurité : lève bans et verrou (les compteurs repartent de zéro)."""
    with _verrou_etat:
        _bannis.clear()
        _verrou_internet["jusqu_a"] = 0.0
        _echecs.clear()
        _echecs_ip.clear()
        _paliers_faits.clear()
    noter("deblocage", request, "bans et verrou levés à la main")


def reinitialiser():
    """Tout oublier (tests)."""
    with _verrou_etat:
        for d in (_echecs, _echecs_ip, _bannis, _inconnus, _inconnus_hits, _erreurs, _derniere_alerte, _paliers_faits):
            d.clear()
        _verrou_internet["jusqu_a"] = 0.0


def etat() -> dict:
    maintenant = time.time()
    return {"bannis": sorted(((ip, int(fin - maintenant)) for ip, fin in _bannis.items() if fin > maintenant),
                             key=lambda b: -b[1]),
            "verrou": verrouille(maintenant), "pays": pays_autorises(),
            "echecs_15min": len([e for e in _echecs if maintenant - e[0] <= FENETRE_ECHECS])}


# --- Filtre des requêtes par Internet -------------------------------------------

def refus_internet(request) -> str | None:
    """Raison de refuser d'emblée une requête par Internet (pays, adresse bannie), ou None."""
    if not appareils.par_tunnel(request.headers):
        return None
    ip = appareils.ip_client(request)
    if bannie(ip):
        return "adresse bloquée"
    autorises = pays_autorises()
    code = (request.headers.get("cf-ipcountry") or "").upper()
    if autorises is not None and code not in autorises:
        noter("pays", request, f"pays {code or 'inconnu'}, page {request.url.path}", ip=ip,
              alerte=f"Accès refusé depuis le pays {code or 'inconnu'} : la règle Cloudflare « France seulement » "
                     "est-elle toujours active ?")
        return "pays non autorisé"
    return None


def appareil_inconnu(request):
    """Appareil non approuvé par Internet : noté une fois par heure et par adresse, alerte si ça se multiplie."""
    maintenant = time.time()
    ip = appareils.ip_client(request)
    cookie = request.cookies.get(appareils.COOKIE)
    with _verrou_etat:
        hits = _inconnus_hits[ip]
        hits.append(maintenant)
        _vider(hits, INSISTANCE_FENETRE, maintenant)
        insiste = len(hits) >= INSISTANCE
        nouveau = maintenant - _inconnus.get(ip, -1e9) > INCONNUS_FENETRE
        if nouveau:
            _inconnus[ip] = maintenant
        for vieux in [k for k, t in _inconnus.items() if maintenant - t > INCONNUS_FENETRE]:
            _inconnus.pop(vieux, None)
            _inconnus_hits.pop(vieux, None)
        n = len(_inconnus)
        palier = next((p for p in PALIERS_INCONNUS if n >= p and p not in _paliers_faits["inconnus"]), None)
        if palier:
            _paliers_faits["inconnus"].add(palier)
        if n < PALIERS_INCONNUS[0]:
            _paliers_faits["inconnus"].clear()
    if cookie and nouveau:
        noter("appareil_revoque", request, f"cookie d'appareil refusé, page {request.url.path}", ip=ip,
              alerte="Un appareil retiré (ou un cookie falsifié) essaie d'ouvrir le site.")
    elif nouveau:
        noter("appareil_inconnu", request, f"page {request.url.path}", ip=ip)
    if palier:
        alerter("inconnus", "Visites d'appareils inconnus",
                f"{n} adresses différentes sans appareil approuvé en 1 h (dernière : {ip}). "
                "Elles ne voient que la page « code », mais l'adresse du site circule.")
    if insiste and not bannie(ip):
        bannir(ip, f"{len(hits)} requêtes en 10 min sans appareil approuvé", request)


def erreur_serveur(request, detail: str):
    maintenant = time.time()
    with _verrou_etat:
        _erreurs.append(maintenant)
        _vider(_erreurs, ERREURS_FENETRE, maintenant)
        n = len(_erreurs)
    noter("erreur_serveur", request, detail)
    if n >= ERREURS_SEUIL:
        noter("erreurs_serveur", request, f"{n} erreurs en 5 min, dernière : {request.url.path}",
              alerte=f"{n} erreurs du serveur en 5 min (dernière sur {request.url.path}). "
                     "Bug, panne ou quelqu'un qui teste le site : voir Activité → État.")


def nb_nouvelles_alertes() -> int:
    """Pastille du menu : alertes depuis la dernière visite de la page Sécurité."""
    try:
        return db.securite_nb_alertes_depuis(db.etat_valeur("securite_vu"))
    except Exception:
        return 0


def marquer_vu():
    db.etat_ecrire("securite_vu", dt.datetime.now(dt.timezone.utc).isoformat())
