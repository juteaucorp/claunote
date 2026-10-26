"""Page Progression : régularité, mémoire des flashcards (FSRS), réussite par matière, notes Pronote.

    revues_cartes ─┐
    revisions ─────┼─► calculer() ─► série, activité des 30 jours, retenue estimée, tableau par matière
    cartes (FSRS) ─┤
    pronote_notes ─┘                 notes /20, moyenne de classe, tendance
"""
import datetime as dt

import fsrs

from claunote import revision

MAITRISEE = 21      # jours de stabilité FSRS : au-delà, la carte est considérée comme sue
SANS_MATIERE = "Sans matière"


def sur_20(valeur: dict | None, sur: dict | None) -> float | None:
    """Note Pronote ramenée sur 20 (None pour « Absent », « Non noté »…)."""
    if not isinstance(valeur, dict) or valeur.get("genre", 0) != 0 or not isinstance(valeur.get("points"), (int, float)):
        return None
    bareme = sur.get("points") if isinstance(sur, dict) and sur.get("genre", 0) == 0 else 20
    return round(valeur["points"] * 20 / bareme, 2) if isinstance(bareme, (int, float)) and bareme > 0 else None


def serie(jours_actifs: list[dt.date], aujourdhui: dt.date) -> int:
    """Jours de travail d'affilée jusqu'à aujourd'hui (ou hier : la journée n'est pas finie)."""
    actifs = set(jours_actifs)
    jour = aujourdhui if aujourdhui in actifs else aujourdhui - dt.timedelta(days=1)
    n = 0
    while jour in actifs:
        n, jour = n + 1, jour - dt.timedelta(days=1)
    return n


def _pourcent(a: int | float, b: int | float) -> int | None:
    return round(100 * a / b) if b else None


def memoire(cartes: list[dict], maintenant: dt.datetime) -> tuple[dict, dict[str, dict]]:
    """État des cartes (nouvelles, en apprentissage, en cours, sues) et retenue estimée, au total et par matière."""
    p = revision.planificateur(hasard=False)

    def vide():
        return {"total": 0, "nouvelles": 0, "apprentissage": 0, "en_cours": 0, "sues": 0, "a_revoir": 0,
                "_retenues": []}
    total, par_matiere = vide(), {}
    for k in cartes:
        carte = revision.carte_fsrs(k)
        cases = [total, par_matiere.setdefault(k.get("matiere") or SANS_MATIERE, vide())]
        if not k.get("fsrs") and not k.get("repetitions"):
            etat = "nouvelles"
        elif carte.state in (fsrs.State.Learning, fsrs.State.Relearning):
            etat = "apprentissage"
        else:
            etat = "sues" if (carte.stability or 0) >= MAITRISEE else "en_cours"
        for c in cases:
            c["total"] += 1
            c[etat] += 1
            c["a_revoir"] += carte.due <= maintenant + dt.timedelta(minutes=20)
            if etat != "nouvelles" and carte.last_review:
                c["_retenues"].append(p.get_card_retrievability(carte, maintenant))
    for c in [total, *par_matiere.values()]:
        r = c.pop("_retenues")
        c["retenue"] = round(100 * sum(r) / len(r)) if r else None
    return total, par_matiere


def notes_par_matiere(notes: list[dict]) -> list[dict]:
    """Moyenne /20 (coefficients compris), moyenne de classe et tendance des dernières notes."""
    groupes: dict[str, list[dict]] = {}
    for n in notes:
        note = sur_20(n.get("valeur"), n.get("sur"))
        if note is not None:
            groupes.setdefault(n["matiere"], []).append(
                {"le": n.get("le"), "note": note, "classe": sur_20(n.get("moyenne_classe"), n.get("sur")),
                 "coef": n.get("coefficient") or 1})
    lignes = []
    for matiere, liste in groupes.items():
        poids = sum(x["coef"] for x in liste)
        classes = [x for x in liste if x["classe"] is not None]
        tendance = None
        if len(liste) >= 4:  # 3 dernières notes contre les 3 d'avant
            recentes, avant = liste[-3:], liste[-6:-3]
            tendance = round(sum(x["note"] for x in recentes) / 3 - sum(x["note"] for x in avant) / len(avant), 1)
        lignes.append({
            "matiere": matiere, "n": len(liste), "notes": [x["note"] for x in liste[-10:]],
            "moyenne": round(sum(x["note"] * x["coef"] for x in liste) / poids, 1) if poids else None,
            "classe": round(sum(x["classe"] * x["coef"] for x in classes) / sum(x["coef"] for x in classes), 1)
            if classes and sum(x["coef"] for x in classes) else None,
            "tendance": tendance})
    return sorted(lignes, key=lambda l: l["matiere"])


def calculer(d: dict, aujourdhui: dt.date, maintenant: dt.datetime, jours: int = 30) -> dict:
    revues = {r["jour"]: r for r in d["revues"]}
    faites = {r["jour"]: r["n"] for r in d["revisions"]}
    activite = []
    for i in range(jours - 1, -1, -1):
        jour = aujourdhui - dt.timedelta(days=i)
        r = revues.get(jour) or {}
        activite.append({"jour": jour, "reponses": r.get("reponses", 0), "reussies": r.get("reussies", 0),
                         "revisions": faites.get(jour, 0)})
    reponses = sum(a["reponses"] for a in activite)
    reussies = sum(a["reussies"] for a in activite)
    total, cartes_matiere = memoire(d["cartes"], maintenant)

    reussite = {r["matiere"] or SANS_MATIERE: r for r in d["reussite"]}
    quiz = {r["matiere"] or SANS_MATIERE: r for r in d["quiz"]}
    nb_faites = {r["matiere"] or SANS_MATIERE: r["n"] for r in d["faites"]}
    matieres = []
    for m in sorted(set(cartes_matiere) | set(reussite) | set(quiz) | set(nb_faites)):
        c, r, q = cartes_matiere.get(m) or {}, reussite.get(m) or {}, quiz.get(m) or {}
        matieres.append({
            "matiere": m, "cartes": c.get("total", 0), "sues": c.get("sues", 0), "a_revoir": c.get("a_revoir", 0),
            "retenue": c.get("retenue"), "reussite": _pourcent(r.get("reussies", 0), r.get("reponses", 0)),
            "reponses": r.get("reponses", 0), "quiz": _pourcent(q.get("bonnes") or 0, q.get("total") or 0),
            "nb_quiz": q.get("n", 0), "faites": nb_faites.get(m, 0)})
    return {
        "activite": activite, "max_jour": max([a["reponses"] for a in activite] + [1]),
        "reponses": reponses, "reussite": _pourcent(reussies, reponses),
        "revisions": sum(a["revisions"] for a in activite),
        "jours_actifs": sum(1 for a in activite if a["reponses"] or a["revisions"]),
        "serie": serie(d["jours_actifs"], aujourdhui), "cartes": total, "matieres": matieres,
        "notes": notes_par_matiere(d["notes"]), "jours": jours}
