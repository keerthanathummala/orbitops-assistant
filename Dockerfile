# One-container build of OrbitOps Assistant for free hosting (Hugging Face
# Spaces, deployed from GitHub by .github/workflows/deploy-hf-space.yml).
# The backend runs as a single process (chat + auth + ops, SQLite, on-disk
# Chroma, in-memory rate limits) and also serves the built frontend, so the
# only outside service it needs is the Gemini API. See HOSTING.md.
#
# The Kubernetes deployment (k8s/, docker-compose.yml) still uses the
# separate backend/ and frontend/ images; this file doesn't change them.

# --- Frontend: build the React app -----------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Empty API URL -> the app calls /api/... on its own origin (see api.js).
ENV VITE_API_URL=""
RUN npm run build

# --- Backend: FastAPI app that also serves the frontend --------------------
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HF_HOME=/app/.cache/huggingface \
    STATIC_DIR=/app/static \
    APP_VERSION=hosted \
    PORT=7860

# CPU-only torch keeps the image ~2GB smaller than the default CUDA build.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
        torch -r requirements.txt

# Bake the embedding model in so the app starts without downloading it.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

COPY backend/app ./app
COPY --from=frontend /src/dist ./static

# Hugging Face Spaces runs the container as uid 1000; SQLite and the Chroma
# store are written inside /app, so that user must own it.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser /app
USER appuser

EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
