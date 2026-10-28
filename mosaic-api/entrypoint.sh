#!/bin/sh
# Seed the shared taxonomy volume from the baked copy on first start,
# then run the API as the app user. mosaic-api and trust-engine share
# this volume, so steward-accepted taxonomy versions are visible to the
# engine (hot-reload, no restart).
set -e
chown -R app:app /app/taxonomy /app/state 2>/dev/null || true
if [ -z "$(ls -A /app/taxonomy 2>/dev/null)" ]; then
  cp /app/data/mock/taxonomy/*.json /app/taxonomy/
  chown app:app /app/taxonomy/*.json
fi
exec su app -s /bin/sh -c 'exec uvicorn main:app --host 0.0.0.0 --port 8000'
