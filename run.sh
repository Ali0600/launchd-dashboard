#!/usr/bin/env bash
# Dev launcher: create the venv on first run, then serve the dashboard on localhost.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating venv + installing deps…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

PORT="${PORT:-8787}"
echo "launchd dashboard → http://127.0.0.1:${PORT}"
exec ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" "$@"
