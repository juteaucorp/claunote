FROM python:3.14-slim
# Pango + polices : WeasyPrint, pour les PDF des révisions ; catdoc : texte des vieux .doc/.xls/.ppt ;
# poppler + Tesseract : lecture locale (sans IA) des scans, pour le garde-fou et le découpage des gros scans
# Graphviz : schémas des fiches et épreuves (blocs ```dot), dessinés en SVG pour le site et le PDF
# Robuste : 5 essais par téléchargement, délai de 30 s, aucune question ; paquets .deb gardés en cache entre
# deux constructions (BuildKit), donc un nouvel essai ne retélécharge rien
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' \
       > /etc/apt/apt.conf.d/80-reseau \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl ca-certificates \
       libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
       catdoc poppler-utils tesseract-ocr tesseract-ocr-fra graphviz
# Claude Code : l'agent IA, authentifié par CLAUDE_CODE_OAUTH_TOKEN (forfait Pro)
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.local/bin:${PATH}"
WORKDIR /srv
COPY requirements.txt .
# Cache de téléchargement gardé entre deux constructions (BuildKit) : une dépendance ajoutée ne fait pas
# retélécharger toutes les autres
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
COPY claunote ./claunote
# Pas d'en-tête « server: uvicorn » : rien qui renseigne un curieux sur ce qui tourne
CMD ["uvicorn", "claunote.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
