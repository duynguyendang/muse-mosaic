FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" app

COPY matching-engine/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app matching-engine/ ./
COPY --chown=app:app shared/ ./shared/
COPY --chown=app:app matching-engine/entrypoint.sh /usr/local/bin/entrypoint.sh

EXPOSE 8081

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
