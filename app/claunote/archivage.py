"""Ménage proposé au fil du temps : révisions, flashcards et campagnes qui ne servent plus.

Rien n'est supprimé ni archivé sans toi : Claunote propose, tu choisis.

    candidats (db) ──► règles ci-dessous ──► propositions ──► /archives : « Archiver » ou « Garder »
                                                     └──────► le dimanche, une notification s'il y en a assez

    Archivé = sorti de l'historique, des campagnes et des flashcards du jour (plus de rappels),
              mais toujours là : « Restaurer » le remet comme avant, « Supprimer » l'efface pour de bon.
    Garder  = plus reproposé pendant GARDER_JOURS.

Jamais proposé : ce qui est en préparation, lié à un contrôle à venir, d'une campagne en cours,
ou gardé récemment.
"""
import datetime as dt

from claunote import db, pronote

PARIS = pronote.PARIS
GARDER_JOURS = 60
EVAL_PASSEE = 7          # jours après la date d'évaluation d'un support
PAS_OUVERTE = 60         # fiche, quiz, plan ou épreuve blanche pas ouvert depuis
CARTES_ABANDON = 30      # paquet de flashcards plus révisé depuis
CARTES_JAMAIS = 21       # paquet jamais commencé depuis sa création
ERREUR = 14              # support jamais généré (en erreur) depuis
CAMPAGNE_FINIE = 7       # jours après la fin d'une campagne
MIN_NOTIFICATION = 3     # en dessous, pas de notification (la proposition reste visible sur le site)


def _jours(depuis: dt.datetime, maintenant: dt.datetime) -> int:
    return (maintenant - depuis).days


def _date(d) -> str:
    return d.strftime("%d/%m/%Y")


def raison_revision(r: dict, maintenant: dt.datetime) -> str | None:
    """Pourquoi proposer d'archiver cette révision, ou None si elle sert encore."""
    aujourdhui = maintenant.astimezone(PARIS).date()
    if r.get("garder_jusqu_au") and r["garder_jusqu_au"] > maintenant:
        return None
    if r.get("evaluation_a_venir") or (r.get("date_eval") and r["date_eval"] >= aujourdhui):
        return None
    if r["statut"] == "erreur":
        if _jours(r["cree_le"], maintenant) >= ERREUR:
            return f"Jamais générée (en erreur depuis le {_date(r['cree_le'].astimezone(PARIS))})"
        return None
    if r.get("date_eval") and (aujourdhui - r["date_eval"]).days >= EVAL_PASSEE:
        return f"Évaluation du {_date(r['date_eval'])} passée"
    if r["genre"] == "cartes":
        if not r.get("nb_cartes"):
            return None
        derniere = r.get("derniere_revue")
        if derniere is None:
            n = _jours(r["cree_le"], maintenant)
            return f"Jamais commencé, créé il y a {n} jours" if n >= CARTES_JAMAIS else None
        n = _jours(derniere, maintenant)
        if n >= CARTES_ABANDON:
            dues = r.get("cartes_dues") or 0
            retard = f" · {dues} carte{'s' if dues > 1 else ''} en retard" if dues else ""
            return f"Plus révisé depuis {n} jours{retard}"
        return None
    vu = max(x for x in (r.get("ouverte_le"), r.get("fait_le"), r["cree_le"]) if x)
    n = _jours(vu, maintenant)
    return f"Pas ouverte depuis {n} jours" if n >= PAS_OUVERTE else None


def raison_campagne(k: dict, maintenant: dt.datetime) -> str | None:
    aujourdhui = maintenant.astimezone(PARIS).date()
    if k.get("garder_jusqu_au") and k["garder_jusqu_au"] > maintenant:
        return None
    if (aujourdhui - k["fin"]).days < CAMPAGNE_FINIE:
        return None
    faits = f" · {k['faites']}/{k['nb']} supports faits" if k.get("nb") else ""
    return f"Terminée le {_date(k['fin'])}{faits}"


def propositions(maintenant: dt.datetime | None = None) -> list[dict]:
    """Ce qu'il est proposé d'archiver : campagnes d'abord, puis les plus anciennes révisions."""
    maintenant = maintenant or dt.datetime.now(PARIS)
    revisions, campagnes = db.candidats_archivage()
    out = []
    for k in campagnes:
        raison = raison_campagne(k, maintenant)
        if raison:
            out.append({"type": "campagne", "id": k["id"], "titre": k["titre"], "genre": "campagne",
                        "raison": raison, "nb": k.get("nb") or 0})
    for r in revisions:
        raison = raison_revision(r, maintenant)
        if raison:
            out.append({"type": "revision", "id": r["id"], "titre": r["sujet"], "genre": r["genre"],
                        "matiere": r.get("matiere"), "raison": raison, "nb": r.get("nb_cartes") or 0})
    return out


def nb_propositions(maintenant: dt.datetime | None = None) -> int:
    try:
        return len(propositions(maintenant))
    except Exception:   # jamais une page cassée pour une suggestion
        return 0


def separer(cles: list[str]) -> tuple[list[int], list[int]]:
    """Cases cochées « revision:12 », « campagne:3 » → (ids de révisions, ids de campagnes)."""
    revisions, campagnes = [], []
    for cle in cles:
        genre, _, ident = (cle or "").partition(":")
        if ident.isdigit():
            if genre == "revision":
                revisions.append(int(ident))
            elif genre == "campagne":
                campagnes.append(int(ident))
    return revisions, campagnes


def resume(props: list[dict]) -> str:
    """Texte court de la notification : « 2 campagnes terminées, 3 paquets de flashcards, 4 fiches… »."""
    campagnes = sum(p["type"] == "campagne" for p in props)
    cartes = sum(p["genre"] == "cartes" for p in props)
    autres = len(props) - campagnes - cartes
    morceaux = []
    if campagnes:
        morceaux.append(f"{campagnes} campagne{'s' if campagnes > 1 else ''} terminée{'s' if campagnes > 1 else ''}")
    if cartes:
        morceaux.append(f"{cartes} paquet{'s' if cartes > 1 else ''} de flashcards délaissé{'s' if cartes > 1 else ''}")
    if autres:
        morceaux.append(f"{autres} support{'s' if autres > 1 else ''} plus ouvert{'s' if autres > 1 else ''}")
    return ", ".join(morceaux) + " : je les archive ? Rien n'est supprimé."
