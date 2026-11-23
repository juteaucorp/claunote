"""Page de maintenance (infra/maintenance) : servie par Caddy et par le Worker Cloudflare quand le site ne répond pas."""
import importlib.util
import re
from pathlib import Path

from tests.test_site import anonyme, client  # noqa: F401 (fixtures)

MAINTENANCE = Path(__file__).resolve().parents[2] / "infra" / "maintenance"


def _assembleur():
    spec = importlib.util.spec_from_file_location("assembler", MAINTENANCE / "assembler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_a_jour_avec_la_page():
    """worker.js (collé dans Cloudflare) doit contenir la page actuelle : relancer assembler.py après une modification."""
    assert (MAINTENANCE / "worker.js").read_text(encoding="utf-8") == _assembleur().assembler(), \
        "worker.js pas à jour : python3 infra/maintenance/assembler.py"


def test_page_autonome():
    """Site arrêté : la page ne peut charger aucun fichier, ni du site ni d'ailleurs."""
    html = (MAINTENANCE / "maintenance.html").read_text(encoding="utf-8")
    assert not re.search(r'\b(src|href)=["\'](?!#)', html), "la page doit tout contenir (styles, logo, script)"
    assert '<p class="doux" id="cause">' in html          # remplacé par le Worker selon la panne
    assert "prefers-color-scheme: dark" in html and 'data-theme="sombre"' in html
    assert "#d97757" in html                              # le papillon


def test_caddy_sert_la_page_si_le_site_ne_repond_pas():
    caddyfile = (MAINTENANCE.parent / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    assert "handle_errors 502 503 504" in caddyfile and "rewrite * /maintenance.html" in caddyfile
    compose = (MAINTENANCE.parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./infra/maintenance:/srv/maintenance:ro" in compose


def test_reponses_du_site_marquees(client):  # noqa: F811
    """Le Worker ne remplace pas un 503 venu de Claunote : il le reconnaît à cet en-tête."""
    assert client.get("/connexion").headers["x-claunote-app"] == "1"
    assert client.get("/hors-ligne").headers["x-claunote-app"] == "1"
