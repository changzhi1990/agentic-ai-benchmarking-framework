#!/usr/bin/env bash
set -euo pipefail

HOST="${HOST:-0.0.0.0}"
AAB_UI_PORT="${AAB_UI_PORT:-${PORT:-80}}"
AAB_UI_FALLBACK_PORT="${AAB_UI_FALLBACK_PORT:-8080}"
PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"

cd "${PROJECT_ROOT}"
exec python3 -m aab_framework.dashboard \
  --project-root "${PROJECT_ROOT}" \
  --host "${HOST}" \
  --port "${AAB_UI_PORT}" \
  --fallback-port "${AAB_UI_FALLBACK_PORT}"
