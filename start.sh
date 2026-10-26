#!/bin/bash
# Starts the Yes Deputy app server.
# Reads credentials from .env in the same directory.
set -e

cd "$(dirname "$0")"
exec .venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  --workers 2
