#!/usr/bin/env python3
"""Fabrique worker.js (un seul fichier, à coller tel quel dans Cloudflare) à partir de worker.src.js et
de maintenance.html. À relancer après chaque modification de l'un des deux : python3 infra/maintenance/assembler.py
"""
import json
from pathlib import Path

ICI = Path(__file__).resolve().parent
ENTETE = ("// FICHIER FABRIQUÉ par infra/maintenance/assembler.py à partir de worker.src.js et maintenance.html :\n"
          "// ne pas le modifier à la main. C'est lui qu'on colle dans Cloudflare (ou que wrangler déploie).\n")


def assembler() -> str:
    source = (ICI / "worker.src.js").read_text(encoding="utf-8")
    page = (ICI / "maintenance.html").read_text(encoding="utf-8")
    assert source.count('"__PAGE__"') == 1, "worker.src.js doit contenir une fois \"__PAGE__\""
    # JSON = chaîne JavaScript valide ; « </ » échappé par prudence
    return ENTETE + source.replace('"__PAGE__"', json.dumps(page, ensure_ascii=False).replace("</", "<\\/"))


if __name__ == "__main__":
    (ICI / "worker.js").write_text(assembler(), encoding="utf-8")
    print("worker.js fabriqué")
