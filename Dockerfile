# PropertyOps AI web app (Cloud Run). Build: docker build -t propertyops-web .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    AUTH_MODE=google_id_token

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ app/
# Policy docs, seed data and reference files; eval cases and demo fixtures are excluded
# by .dockerignore (demo auth is refused in production anyway).
COPY data/property_docs/ data/property_docs/
COPY data/reference/ data/reference/

RUN useradd --system --uid 10001 app && chown -R app /srv
USER app

# Cloud Run provides $PORT. One worker per instance; scale with instances.
CMD ["sh", "-c", "exec uvicorn app.web.server:create_app --factory --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --no-access-log"]
