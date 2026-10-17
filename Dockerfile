# Flowmentor CRM — imagem única (API + frontend estático). O worker usa a mesma imagem com outro comando.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /srv

COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

COPY backend backend
COPY frontend frontend

RUN useradd --system --uid 10001 --home /srv app && mkdir -p /srv/storage && chown -R app /srv
USER app
WORKDIR /srv/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fs http://localhost:8000/api/health || exit 1

# Aplica migrações e sobe a API. TLS é terminado no proxy reverso (Caddy/Nginx/ALB).
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips='*'"]
