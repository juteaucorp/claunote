"""Emploi du temps Pronote : savoir quand tu es libre pour recevoir une notification.

Trois états à un instant donné :
- « cours » : pendant un cours (ou un interclasse trop court pour regarder son téléphone) ;
- « entreprise » : heures marquées « Dispensé » sur Pronote, c'est-à-dire en alternance au travail.
  Comme les horaires de l'entreprise ne sont pas sur Pronote, on compte toute la plage, de la première
  heure dispensée à la dernière (plus le trajet), sauf le trou de midi ;
- libre : avant les cours, aux vraies pauses, à midi, après la journée, le week-end.

Sans emploi du temps connu pour ce jour-là (Pronote pas relié, vacances au-delà de la synchro),
on ne sait rien : tout est « libre » et les notifications partent comme avant.
"""
import datetime as dt
import json
from zoneinfo import ZoneInfo

from claunote import db

PARIS = ZoneInfo("Europe/Paris")

PAUSE_MIN = dt.timedelta(minutes=15)       # un trou plus court entre deux cours : pas le temps
SORTIE_COURS = dt.timedelta(minutes=10)    # le temps de sortir de classe
TRAJET_ENTREPRISE = dt.timedelta(minutes=30)
MIDI = (dt.time(11, 30), dt.time(14, 0))   # un trou dans cette plage, en entreprise : la pause déjeuner


def _paris(d: dt.datetime) -> dt.datetime:
    return d.astimezone(PARIS)


def fusionner_fenetre(ancien: dict, nouveaux: list[dict], du: str | None, au: str | None, cle: str) -> dict:
    """{du, au, <cle>: [...]} : la fenêtre reçue remplace ce qu'on avait, les jours d'après sont gardés.

    La vérification rapide (8 jours) ne doit pas effacer les semaines suivantes que la synchro (28 jours) a vues.
    Les jours d'avant la fenêtre reçue (le passé) sont oubliés."""
    d_du, d_au = _date(du), _date(au)
    a_du, a_au = _date(ancien.get("du")), _date(ancien.get("au"))
    if not (d_du and d_au and a_du and a_au) or a_au < d_du or d_au < a_du:   # rien en commun : on repart de zéro
        return {"du": du, "au": au, cle: nouveaux}
    gardes = [c for c in ancien.get(cle) or [] if (_date(c.get("debut")) or d_du) >= d_au]
    return {"du": du, "au": max(a_au, d_au).isoformat(),
            cle: sorted(gardes + nouveaux, key=lambda c: _date(c.get("debut")) or d_du)}


def enregistrer(creneaux: list[dict] | None, du: str | None, au: str | None):
    """Synchro Pronote → créneaux gardés en base (les 28 prochains jours)."""
    if creneaux is None:   # la section a échoué : on garde l'ancien plutôt que d'effacer
        return
    nouveaux = [{k: c.get(k) for k in ("debut", "fin", "matiere", "dispense", "genre", "salle", "prof", "statut")}
                for c in creneaux]
    db.etat_ecrire(db.cle_du_compte("pronote_edt"), json.dumps(fusionner_fenetre(_charger(), nouveaux, du, au, "creneaux")))


def _charger(compte_id: int | None = None) -> dict:
    """Emploi du temps du compte (celui de la requête ou du worker, ou `compte_id`) ; vide s'il n'a pas Pronote."""
    try:
        d = json.loads(db.etat_valeur(db.cle_du_compte("pronote_edt", compte_id)) or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _date(iso: str | None) -> dt.datetime | None:
    try:
        return _paris(dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))) if iso else None
    except ValueError:
        return None


def connu(jour: dt.date, donnees: dict | None = None) -> bool:
    """L'emploi du temps de ce jour a-t-il été récupéré ?"""
    d = _charger() if donnees is None else donnees
    du, au = _date(d.get("du")), _date(d.get("au"))
    return bool(du and au and du.date() <= jour < au.date())


def journee(jour: dt.date, donnees: dict | None = None) -> dict | None:
    """{"cours": [(début, fin)], "entreprise": [(début, fin)]} triés, ou None si le jour est inconnu."""
    d = _charger() if donnees is None else donnees
    if not connu(jour, d):
        return None
    cours, entreprise = [], []
    for c in d.get("creneaux") or []:
        debut, fin = _date(c.get("debut")), _date(c.get("fin"))
        if not debut or not fin or fin <= debut or debut.date() != jour:
            continue
        (entreprise if c.get("dispense") else cours).append((debut, fin))
    return {"cours": _fusionner(sorted(cours), PAUSE_MIN), "entreprise": _plage_entreprise(sorted(entreprise))}


def _fusionner(plages: list[tuple], trou_max: dt.timedelta) -> list[tuple]:
    """Plages qui se touchent (ou séparées de moins de `trou_max`) fusionnées."""
    res = []
    for debut, fin in plages:
        if res and debut - res[-1][1] < trou_max:
            res[-1] = (res[-1][0], max(res[-1][1], fin))
        else:
            res.append((debut, fin))
    return res


def _midi(debut: dt.datetime, fin: dt.datetime) -> bool:
    return debut.time() < MIDI[1] and fin.time() > MIDI[0]


def _plage_entreprise(plages: list[tuple]) -> list[tuple]:
    """Au travail de la première à la dernière heure dispensée, trajet compris ; seul le trou de midi est libre."""
    if not plages:
        return []
    res = [plages[0]]
    for debut, fin in plages[1:]:
        if debut > res[-1][1] and _midi(res[-1][1], debut):
            res.append((debut, fin))          # la pause déjeuner
        else:
            res[-1] = (res[-1][0], max(res[-1][1], fin))
    return [(d, f + TRAJET_ENTREPRISE) if i == len(res) - 1 else (d, f) for i, (d, f) in enumerate(res)]


def occupation(maintenant: dt.datetime, donnees: dict | None = None) -> tuple[str | None, dt.datetime | None]:
    """(« cours » | « entreprise » | None, libre à partir de) à cet instant."""
    m = _paris(maintenant)
    j = journee(m.date(), donnees)
    if not j:
        return None, None
    for genre, plages, marge in (("cours", j["cours"], SORTIE_COURS), ("entreprise", j["entreprise"], dt.timedelta())):
        for debut, fin in plages:
            if debut <= m < fin + marge:
                return genre, fin + marge
    return None, None


def libre_a_partir(maintenant: dt.datetime, donnees: dict | None = None) -> dt.datetime:
    """Le premier instant libre (maintenant s'il l'est déjà)."""
    d = _charger() if donnees is None else donnees
    m = _paris(maintenant)
    for _ in range(20):   # cours puis entreprise qui s'enchaînent : quelques sauts suffisent
        genre, fin = occupation(m, d)
        if not genre:
            return m
        m = fin
    return m


def fin_de_journee(jour: dt.date, donnees: dict | None = None) -> tuple[dt.datetime, str] | None:
    """(moment où la journée est finie, « cours » | « entreprise »), ou None : rien ce jour-là / inconnu."""
    j = journee(jour, donnees)
    if not j:
        return None
    fins = [(f + SORTIE_COURS, "cours") for _, f in j["cours"][-1:]] + [(f, "entreprise") for _, f in j["entreprise"][-1:]]
    return max(fins) if fins else None


def heure_rappel(jour: dt.date, heure_choisie: dt.time, donnees: dict | None = None) -> tuple[dt.datetime, str]:
    """Quand envoyer le rappel du jour : à la sortie des cours si c'est avant l'heure choisie, sinon à l'heure
    choisie (repoussée par la file si tu es encore en cours ou au travail). Renvoie (moment, pourquoi)."""
    choisie = dt.datetime.combine(jour, heure_choisie, PARIS)
    fin = fin_de_journee(jour, donnees)
    if not fin:
        return choisie, "heure choisie"
    moment, genre = fin
    if moment < choisie:
        return moment, "à la sortie des cours" if genre == "cours" else "en rentrant du travail"
    occupe, _ = occupation(choisie, donnees)
    if occupe:
        return libre_a_partir(choisie, donnees), "après les cours" if occupe == "cours" else "après le travail"
    return choisie, "heure choisie"


def resume(maintenant: dt.datetime, heure_choisie: dt.time) -> str | None:
    """Une phrase pour la page Notifications : ce que l'emploi du temps change aujourd'hui."""
    d = _charger()
    if not d.get("creneaux") and not d.get("du"):
        return None
    m = _paris(maintenant)
    j = journee(m.date(), d)
    if j is None:
        return "Emploi du temps pas encore récupéré pour aujourd'hui : notifications aux heures habituelles."
    bouts = []
    if j["cours"]:
        bouts.append(f"cours de {j['cours'][0][0]:%H:%M} à {j['cours'][-1][1]:%H:%M}")
    if j["entreprise"]:
        bouts.append(f"en entreprise (dispensé) de {j['entreprise'][0][0]:%H:%M} à "
                     f"{j['entreprise'][-1][1] - TRAJET_ENTREPRISE:%H:%M}")
    if not bouts:
        return "Pas de cours aujourd'hui : rappel à l'heure choisie."
    moment, pourquoi = heure_rappel(m.date(), heure_choisie, d)
    return f"Aujourd'hui : {', '.join(bouts)}. Rappel du jour vers {moment:%H:%M} ({pourquoi})."


# --- Affichage (page Agenda) et révisions proposées avant un cours -----------------------

def jours(depuis: dt.date, nb: int = 14, donnees: dict | None = None) -> list[dict]:
    """Les jours qui ont des créneaux, à partir de `depuis` : [{date, creneaux, entreprise}].

    Les heures « Dispensé » qui se suivent ne font qu'un bloc « En entreprise » (sauf la pause de midi)."""
    d = _charger() if donnees is None else donnees
    par_jour: dict = {}
    for c in d.get("creneaux") or []:
        debut, fin = _date(c.get("debut")), _date(c.get("fin"))
        if debut and fin and depuis <= debut.date() < depuis + dt.timedelta(days=nb):
            par_jour.setdefault(debut.date(), []).append({**c, "debut": debut, "fin": fin})
    res = []
    for jour in sorted(par_jour):
        creneaux = []
        for c in sorted(par_jour[jour], key=lambda c: c["debut"]):
            prec = creneaux[-1] if creneaux else None
            if c.get("dispense") and prec and prec.get("dispense") and c["debut"] <= prec["fin"] + PAUSE_MIN:
                prec["fin"] = max(prec["fin"], c["fin"])
                continue
            creneaux.append(dict(c))
        res.append({"date": jour, "creneaux": creneaux,
                     "entreprise": all(c.get("dispense") for c in creneaux)})
    return res


def prochain_jour_de_cours(apres: dt.date, donnees: dict | None = None, max_jours: int = 4):
    """(date, cours non dispensés) du prochain jour au lycée après `apres`, ou None."""
    d = _charger() if donnees is None else donnees
    for j in jours(apres + dt.timedelta(days=1), max_jours, d):
        cours = [c for c in j["creneaux"] if not c.get("dispense") and c.get("genre", "lesson") == "lesson"]
        if cours:
            return j["date"], cours
    return None


FICHE_RELUE = dt.timedelta(days=4)   # fiche ouverte il y a moins longtemps : pas la peine de la reproposer


def avant_les_cours(aujourd_hui: dt.date, donnees: dict | None = None) -> dict | None:
    """Pour le prochain jour au lycée : quoi revoir avant chaque matière (flashcards dues, fiche pas relue).

    {date, suggestions: [{matiere, pronote, debut, cartes, paquet, fiche}]}, ou None sans cours à venir."""
    from claunote.pronote import trouver_matiere   # pronote importe ce module
    d = _charger() if donnees is None else donnees
    prochain = prochain_jour_de_cours(aujourd_hui, d)
    if not prochain:
        return None
    jour, cours = prochain
    stock = db.a_revoir_par_matiere()
    noms, vues, suggestions = sorted(stock), set(), []
    maintenant = dt.datetime.now(PARIS)
    for c in cours:
        m = trouver_matiere(c["matiere"], noms)
        if not m or m in vues:
            continue
        vues.add(m)
        s = stock[m]
        fiche = s.get("fiche")
        if fiche and fiche.get("ouverte_le") and maintenant - fiche["ouverte_le"] < FICHE_RELUE:
            fiche = None
        if s.get("cartes") or fiche:
            suggestions.append({"matiere": m, "pronote": c["matiere"], "debut": c["debut"], "cartes": s.get("cartes", 0),
                                "paquet": s.get("paquet"), "fiche": fiche})
    return {"date": jour, "suggestions": suggestions}
