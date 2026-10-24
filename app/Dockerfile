FROM python:3.12-slim
# Pango + polices : WeasyPrint, pour les PDF des révisions ; catdoc : texte des vieux .doc/.xls/.ppt ;
# poppler + Tesseract : lecture locale (sans IA) des scans, pour le garde-fou et le découpage des gros scans
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
    catdoc poppler-utils tesseract-ocr tesseract-ocr-fra \
    && rm -rf /var/lib/apt/lists/*
# Claude Code : l'agent IA, authentifié par CLAUDE_CODE_OAUTH_TOKEN (forfait Pro)
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.local/bin:${PATH}"
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY claunote ./claunote
CMD ["uvicorn", "claunote.main:app", "--host", "0.0.0.0", "--port", "8000"]
