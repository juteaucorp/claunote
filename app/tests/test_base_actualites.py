"""Alertes d'actualités sur une vraie base PostgreSQL (lancé seulement si CLAUNOTE_TEST_DB est défini)."""
from claunote import db
from tests.test_base_comptes import URL, base, pytestmark  # noqa: F401  (même base jetable)


def _article(c, flux_id, cle, titre, annonce, alerte, heures):
    return c.execute(
        "INSERT INTO articles (flux_id, cle, lien, titre, extrait, publie_le, sujets, tri, annonce, alerte, score) "
        "VALUES (%s, %s, '', %s, '', now() - make_interval(hours => %s), '{1}', 'ia', %s, %s, 0.9) RETURNING id",
        (flux_id, cle, titre, heures, annonce, alerte)).fetchone()["id"]


def test_alerte_deja_decidee_reste_candidate_jusqu_a_sa_notification(base):
    flux_id = db.flux_ajouter("https://news.google.com/rss/search?q=cisco", "Google Actualités : Réseaux", "google")
    with db.connect() as c:
        cisco = _article(c, flux_id, "a1", "Cisco Expressway CVE-2026-76466", True, True, 2)
        _article(c, flux_id, "a2", "Article déjà décidé", True, False, 2)
        _article(c, flux_id, "a3", "Vieille alerte", True, True, 72)
        neuf = _article(c, flux_id, "a4", "Neuf", False, False, 1)
    assert sorted(a["id"] for a in db.articles_a_annoncer()) == [cisco, neuf]
    assert not db.alerte_deja_envoyee(["actu-alerte:cve-2026-76466"], ["CVE-2026-76466"])
    # Envoyée autrefois sous l'étiquette de l'IA : la CVE citée suffit à la reconnaître
    db.notifier("actu-alerte:cisco-expressway", "🚨 Cisco : faille CVE-2026-76466", "Patcher")
    assert db.alerte_deja_envoyee(["actu-alerte:cve-2026-76466"], ["CVE-2026-76466"])
    assert not db.alerte_deja_envoyee(["actu-alerte:cve-2026-99999"], ["CVE-2026-99999"])
    assert db.alerte_deja_envoyee(["actu-alerte:cisco-expressway"], [])
