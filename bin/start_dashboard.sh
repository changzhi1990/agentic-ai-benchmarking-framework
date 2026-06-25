#!/usr/bin/env bash
set -euo pipefail

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-80}"
PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"

cd "${PROJECT_ROOT}"
exec python3 -m aab_framework.dashboard \
  --project-root "${PROJECT_ROOT}" \
  --host "${HOST}" \
  --port "${PORT}"
