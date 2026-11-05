FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" app

COPY mosaic-api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app mosaic-api/ ./
COPY --chown=app:app shared/ ./shared/
COPY --chown=app:app web ./web
COPY --chown=app:app data/mock ./data/mock
COPY --chown=app:app data/schemas ./data/schemas
COPY --chown=app:app trust-engine/policies ./policies
COPY --chown=app:app mosaic-api/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN mkdir -p /app/state /app/taxonomy && chown -R app:app /app/state /app/taxonomy

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
