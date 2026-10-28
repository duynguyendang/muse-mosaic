#!/bin/sh
# Stateless matching engine: no volumes to seed, just run as the app user.
set -e
exec su app -s /bin/sh -c 'exec uvicorn main:app --host 0.0.0.0 --port 8081'
