#!/usr/bin/env bash
# Start the Shondhan AI server at http://127.0.0.1:8000
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3.12 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port "${PORT:-8000}"
