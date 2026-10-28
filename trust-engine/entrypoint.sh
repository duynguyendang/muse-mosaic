#!/bin/sh
# Seed the shared taxonomy volume from the baked copy on first start,
# then run the trust-engine as the app user. The engine hot-reloads
# TAXONOMY_DIR, so steward-accepted versions written by mosaic-api
# need no restart.
set -e
chown -R app:app /app/taxonomy 2>/dev/null || true
if [ -z "$(ls -A /app/taxonomy 2>/dev/null)" ]; then
  cp /app/taxonomy-baked/*.json /app/taxonomy/
  chown app:app /app/taxonomy/*.json
fi
exec su app -s /bin/sh -c 'exec trust-engine'
