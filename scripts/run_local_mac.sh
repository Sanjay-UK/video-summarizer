#!/usr/bin/env bash
# Run the web app on localhost with the validated Mac environment (venv-mac).
# RunPod is blocked for this run: an empty RUNPOD_API_KEY makes _api_key() raise before any
# request is sent, and the empty value takes precedence over .env (load_env_file uses setdefault).
# Nothing in .env is modified.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ ! -x venv-mac/bin/python ]; then
  echo "venv-mac not found. Create it from requirements/mac-cpu.lock first." >&2
  exit 1
fi
export RUNPOD_API_KEY=""
exec venv-mac/bin/python app.py
