#!/usr/bin/env bash
# Run the hub from the project venv (no Docker, no root).
#   scripts/run.sh            -> http://127.0.0.1:8000
#   HOST=0.0.0.0 scripts/run.sh  to reach it from other devices on the LAN
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/uvicorn ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements-dev.txt
fi

exec .venv/bin/uvicorn --factory app.main:create_app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --proxy-headers
