#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "[ERROR] .venv not found. See README setup steps."
  exit 1
fi

echo "[1/2] Starting backend on http://127.0.0.1:8000 ..."
.venv/bin/python -m uvicorn backend:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!
trap 'kill $BACKEND_PID 2>/dev/null' EXIT INT TERM

sleep 3
echo "[2/2] Starting Ananya worker..."
.venv/bin/python main.py