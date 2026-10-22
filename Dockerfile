# Assistant Pacewell : API FastAPI + page de démonstration. La base Postgres (pgvector) est un service à part.
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DOSSIER_MODELES=/modeles
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Le modèle d'embeddings est téléchargé une fois, à la construction de l'image (environ 220 Mo).
ARG MODELE_EMBEDDINGS=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
ENV MODELE_EMBEDDINGS=${MODELE_EMBEDDINGS}
RUN python -c "import os; from fastembed import TextEmbedding; TextEmbedding(os.environ['MODELE_EMBEDDINGS'], cache_dir='/modeles')"
COPY assistant ./assistant
COPY evaluation ./evaluation
COPY corpus ./corpus
COPY donnees ./donnees
COPY web ./web
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/sante', timeout=4)"
CMD ["uvicorn", "assistant.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
