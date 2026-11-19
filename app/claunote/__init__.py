"""Claunote. Au chargement : les réglages choisis sur le site (table reglages, voir reglages.py) passent avant
le .env, avant que les modules ne lisent leurs variables (dossiers kDrive, jeton Claude…)."""
import os

if os.environ.get("DATABASE_URL") and not os.environ.get("CLAUNOTE_SANS_REGLAGES"):
    try:
        from claunote import reglages as _reglages
        _reglages.charger_dans_env()
    except Exception as _e:   # base pas encore prête : le worker les recharge dès qu'elle répond
        print(f"réglages du site pas encore chargés ({_e.__class__.__name__}) : le .env seul compte", flush=True)
