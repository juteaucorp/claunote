"""Alertes Pronote : un cours de la semaine qui change, une nouvelle note.

À chaque vérification (toutes les 15 min en journée, plus la synchro complète), on compare l'emploi du
temps reçu à celui de la fois d'avant, cours par cours :

    avant (gardé en base)        maintenant (Pronote)          notification
    Réseaux mar. 10:00 B12   →   Réseaux mar. 10:00 C04    →   « Changement de salle »
    Maths  mer. 8:00         →   Maths mer. 8:00 annulé    →   « Cours annulé » (Prof. absent)
    CEJM   jeu. 14:00        →   (disparu) + CEJM ven. 9:00 →  « Cours déplacé »

Un cours est reconnu par sa matière et son heure de début, jamais par son id Pronote (il change à chaque
connexion). Seuls les cours des 7 prochains jours comptent, et jamais les heures « Dispensé » (en entreprise).
Ces alertes ont la clé « edt: » : elles passent même en cours ou au travail (notifications.URGENTES).

Nouvelles notes : chaque note a une clé tirée de son contenu (matière, jour, devoir, barème) ; celles jamais vues
partent en une notification (une seule pour plusieurs notes publiées d'un coup, vers 7 h 10 en général).
Au tout premier passage, rien n'est annoncé : on note seulement ce qui existe déjà.
"""
import datetime as dt
import hashlib
import json

from claunote import db, emploi_du_temps

PARIS = emploi_du_temps.PARIS
SEMAINE = dt.timedelta(days=7)
GROUPER = 3   # à partir de 3 changements le même jour (prof absent toute la journée) : une seule notification
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]


def _date(iso):
    return emploi_du_temps._date(iso)


def _cle(c: dict) -> str:
    return f"{c.get('matiere', '')}|{_date(c['debut']):%Y-%m-%dT%H:%M}"


def _consolider(cours: list[dict]) -> dict[str, dict]:
    """Un état par cours (matière + début). Un cours annulé et son remplaçant au même créneau : le remplaçant."""
    par_cle: dict[str, list[dict]] = {}
    for c in cours or []:
        if _date(c.get("debut")) and _date(c.get("fin")):
            par_cle.setdefault(_cle(c), []).append(c)
    res = {}
    for cle, liste in par_cle.items():
        actifs = [c for c in liste if not c.get("annule")]
        base = actifs or liste
        res[cle] = {
            "matiere": base[0].get("matiere", ""), "debut": base[0]["debut"],
            "fin": max(base, key=lambda c: _date(c["fin"]))["fin"],
            "salle": ", ".join(sorted({c.get("salle") or "" for c in base} - {""})),
            "prof": ", ".join(sorted({c.get("prof") or "" for c in base} - {""})),
            "statut": next((c.get("statut") for c in base if c.get("statut")), ""),
            "annule": not actifs,
            "dispense": any(c.get("dispense") for c in liste),
        }
    return res


def _quand(debut: dt.datetime, maintenant: dt.datetime) -> str:
    ecart = (debut.date() - maintenant.date()).days
    jour = "aujourd'hui" if ecart == 0 else "demain" if ecart == 1 else f"{JOURS[debut.weekday()]} {debut:%d/%m}"
    return f"{jour} {debut:%H:%M}"


def _ou(valeur: str) -> str:
    return valeur or "aucune"


def changements(ancien: dict, nouveau: dict, maintenant: dt.datetime) -> list[dict]:
    """Cours des 7 prochains jours qui ont changé entre deux relevés : [{cle, jour, debut, titre, corps}]."""
    m = maintenant.astimezone(PARIS)
    bornes = [_date(x) for x in (ancien.get("du"), ancien.get("au"), nouveau.get("du"), nouveau.get("au"))]
    if not all(bornes):
        return []
    debut_fenetre = max(m, bornes[0], bornes[2])
    fin_fenetre = min(m + SEMAINE, bornes[1], bornes[3])
    tout_avant, tout_apres = _consolider(ancien.get("cours")), _consolider(nouveau.get("cours"))
    # « Dispensé » d'un côté ou de l'autre : en entreprise, ce cours ne te concerne pas
    entreprise = {k for k, c in (*tout_avant.items(), *tout_apres.items()) if c["dispense"]}
    dans = lambda k, c: debut_fenetre <= _date(c["debut"]) < fin_fenetre and k not in entreprise
    avant = {k: c for k, c in tout_avant.items() if dans(k, c)}
    apres = {k: c for k, c in tout_apres.items() if dans(k, c)}
    res = []

    def ajouter(c, quoi, details, etat):
        debut = _date(c["debut"])
        statut = c.get("statut") or ""
        if statut and statut.lower() not in (quoi.lower(), *[d.lower() for d in details]):
            details = [*details, statut]
        empreinte = hashlib.sha1(json.dumps([quoi, etat], sort_keys=True).encode()).hexdigest()[:10]
        res.append({"cle": f"edt:{_cle(c)}:{empreinte}", "jour": debut.date(), "debut": debut,
                    "matiere": c["matiere"], "quoi": quoi,
                    "titre": f"{quoi} : {c['matiere']}",
                    "corps": f"{_quand(debut, m).capitalize()}" + (f" · {' · '.join(details)}" if details else "")})

    for cle, b in apres.items():
        a = avant.get(cle)
        if a is None:
            continue
        if b["annule"] and not a["annule"]:
            ajouter(b, "Cours annulé", [b["prof"]] if b["prof"] else [], b)
            continue
        if a["annule"] and not b["annule"]:
            ajouter(b, "Cours rétabli", [f"salle {b['salle']}"] if b["salle"] else [], b)
            continue
        if b["annule"]:
            continue
        details, quoi = [], []
        if a["salle"] != b["salle"]:
            quoi.append("Changement de salle")
            details.append(f"salle {_ou(a['salle'])} → {_ou(b['salle'])}")
        if a["prof"] != b["prof"]:
            quoi.append("Changement de prof")
            details.append(f"{_ou(a['prof'])} → {_ou(b['prof'])}")
        if a["fin"] != b["fin"]:
            quoi.append("Horaire modifié")
            details.append(f"fin à {_date(b['fin']):%H:%M} au lieu de {_date(a['fin']):%H:%M}")
        if not quoi and b["statut"] and b["statut"] != a["statut"]:
            quoi.append(b["statut"])   # « Prof. absent », « Cours modifié »… sans autre changement visible
        if quoi:
            ajouter(b, quoi[0] if len(quoi) == 1 else "Cours modifié", details, b)

    # Cours disparus et apparus : la même matière des deux côtés, c'est un cours déplacé
    partis = [c for k, c in avant.items() if k not in apres and not c["annule"]]
    arrives = [c for k, c in apres.items() if k not in avant]
    for p in partis:
        n = next((c for c in arrives if c["matiere"] == p["matiere"] and not c["annule"]), None)
        if n:
            arrives.remove(n)
            ajouter(n, "Cours déplacé", [f"au lieu de {_quand(_date(p['debut']), m)}"]
                    + ([f"salle {n['salle']}"] if n["salle"] else []), {"de": p, "vers": n})
        else:
            ajouter(p, "Cours retiré", ["n'apparaît plus sur Pronote"], {"retire": p})
    for n in arrives:
        if n["annule"]:
            ajouter(n, "Cours annulé", [n["prof"]] if n["prof"] else [], n)
        else:
            ajouter(n, "Cours ajouté", [f"salle {n['salle']}"] if n["salle"] else [], n)
    return sorted(res, key=lambda r: r["debut"])


def log(msg: str):
    print(f"alertes : {msg}", flush=True)


def _cours_dans(releve: dict, du: str | None, au: str | None) -> int:
    d, a = _date(du), _date(au)
    return sum(1 for c in releve.get("cours") or []
               if d and a and _date(c.get("debut")) and d <= _date(c["debut"]) < a and not c.get("annule"))


def _actif() -> bool:
    from claunote import notifications   # notifications importe pronote, qui importe ce module
    return bool(notifications.preferences(None).get("pronote", True)) and bool(db.abonnements())


def suivre_cours(cours: list[dict] | None, du: str | None, au: str | None,
                 maintenant: dt.datetime | None = None) -> list[dict]:
    """Relevé reçu → alertes des cours modifiés ; garde le relevé pour la prochaine fois. Renvoie les changements."""
    if cours is None:   # la section a échoué : on ne conclut rien
        return []
    maintenant = maintenant or dt.datetime.now(PARIS)
    try:
        ancien = json.loads(db.etat_valeur(db.cle_du_compte("pronote_cours")) or "null")
    except ValueError:
        ancien = None
    nouveau = {"du": du, "au": au, "cours": cours}
    if isinstance(ancien, dict) and not cours and _cours_dans(ancien, du, au) >= GROUPER:
        # Emploi du temps soudain vide alors qu'il y avait des cours : Pronote a mal répondu, on attend
        log("emploi du temps reçu vide, relevé ignoré")
        return []
    trouves = changements(ancien, nouveau, maintenant) if isinstance(ancien, dict) else []
    garde = emploi_du_temps.fusionner_fenetre(ancien if isinstance(ancien, dict) else {}, cours, du, au, "cours")
    # Le passé ne sert plus : seulement à partir d'aujourd'hui
    aujourd_hui = maintenant.astimezone(PARIS).replace(hour=0, minute=0, second=0, microsecond=0)
    garde["cours"] = [c for c in garde["cours"] if (_date(c.get("debut")) or aujourd_hui) >= aujourd_hui]
    db.etat_ecrire(db.cle_du_compte("pronote_cours"), json.dumps(garde))
    if trouves and _actif():
        for jour in sorted({c["jour"] for c in trouves}):
            du_jour = [c for c in trouves if c["jour"] == jour]
            if len(du_jour) < GROUPER:
                for c in du_jour:
                    db.notifier(c["cle"], c["titre"], c["corps"], "/agenda")
                continue
            empreinte = hashlib.sha1("".join(c["cle"] for c in du_jour).encode()).hexdigest()[:10]
            quand = _quand(du_jour[0]["debut"], maintenant.astimezone(PARIS)).rsplit(" ", 1)[0]
            annules = sum(c["quoi"] == "Cours annulé" for c in du_jour)
            titre = (f"{annules} cours annulés {quand}" if annules == len(du_jour)
                     else f"{len(du_jour)} cours modifiés {quand}")
            corps = " · ".join(f"{c['debut']:%H:%M} {c['matiere']} : {c['quoi'].lower()}" for c in du_jour)
            db.notifier(f"edt:{jour}:groupe:{empreinte}", titre, corps[:300], "/agenda")
    return trouves


# --- Nouvelles notes ---------------------------------------------------------------

def cles_notes(notes: list[dict]) -> list[tuple[str, dict]]:
    """(clé tirée du contenu, note) : l'id Pronote change à chaque connexion."""
    vus: dict[str, int] = {}
    res = []
    for n in notes or []:
        jour = (n.get("date") or "")[:10]
        base = json.dumps([n.get("matiere"), jour, n.get("commentaire") or "", n.get("sur"), n.get("coefficient")],
                          sort_keys=True)
        vus[base] = vus.get(base, 0) + 1
        res.append((f"note:{hashlib.sha1(f'{base}#{vus[base]}'.encode()).hexdigest()[:16]}", n))
    return res


def _libelle(n: dict) -> str:
    from claunote.pronote import libelle_note
    return libelle_note(n.get("valeur"), n.get("sur"))


def suivre_notes(notes: dict | None) -> list[dict]:
    """Notes reçues → une notification pour celles jamais vues. Renvoie les nouvelles notes."""
    if not notes:
        return []
    liste = cles_notes(notes.get("notes"))
    premiere_fois = not db.etat_valeur(db.cle_du_compte("pronote_notes_suivies"))
    actif = not premiere_fois and _actif()
    nouvelles = [(cle, n) for cle, n in liste if not db.notification_connue(cle)]
    if premiere_fois:
        db.etat_ecrire(db.cle_du_compte("pronote_notes_suivies"), "1")
    if not nouvelles:
        return []
    if not actif:   # premier passage, ou notifications Pronote coupées : on note sans annoncer
        for cle, n in nouvelles:
            db.notifier(cle, f"Note en {n.get('matiere')}", _libelle(n), "/agenda", statut="ignore")
        return [n for _, n in nouvelles]
    if len(nouvelles) == 1:
        cle, n = nouvelles[0]
        details = [_libelle(n)]
        from claunote.pronote import libelle_note
        if n.get("moyenne_classe"):
            details.append(f"classe {libelle_note(n['moyenne_classe'])}")
        if n.get("coefficient") not in (None, 1, 1.0):
            details.append(f"coef {n['coefficient']:g}")
        if n.get("commentaire"):
            details.append(n["commentaire"])
        db.notifier(cle, f"Nouvelle note en {n.get('matiere')}", " · ".join(details)[:300], "/agenda")
        return [n]
    corps = " · ".join(f"{n.get('matiere')} {_libelle(n)}" for _, n in nouvelles)
    empreinte = hashlib.sha1("".join(cle for cle, _ in nouvelles).encode()).hexdigest()[:16]
    db.notifier(f"note:groupe:{empreinte}", f"{len(nouvelles)} nouvelles notes", corps[:300], "/agenda")
    for cle, n in nouvelles:   # chaque note est connue : pas ré-annoncée seule ensuite
        db.notifier(cle, f"Note en {n.get('matiere')}", _libelle(n), "/agenda", statut="ignore")
    return [n for _, n in nouvelles]
