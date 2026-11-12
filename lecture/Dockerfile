# Service lecture : Docling (PDF), pix2tex (formules), FlashRank (tri des passages), sur processeur seul.
FROM python:3.15-slim
# OpenCV (pix2tex) a besoin de ces bibliothèques système
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
# PyTorch sans CUDA : ~200 Mo au lieu de plusieurs Go
# Cache de téléchargement gardé entre deux constructions : PyTorch n'est téléchargé qu'une fois
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch==2.7.1 torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cpu
WORKDIR /srv
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
# Poids de pix2tex téléchargés à la construction (GitHub) ; sinon, au premier usage
RUN python -c "from pix2tex.cli import LatexOCR; LatexOCR()" || echo "pix2tex : modèle téléchargé au premier usage"
ENV HF_HOME=/modeles/hf LECTURE_MODELES=/modeles
COPY serveur.py .
CMD ["uvicorn", "serveur:app", "--host", "0.0.0.0", "--port", "8000"]
