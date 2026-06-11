#!/usr/bin/env bash
set -euo pipefail

PLAN_DIR="${PLAN_DIR:-runs/firecracker-plan}"
FIRECRACKER_BIN="${FIRECRACKER_BIN:-firecracker}"
VM_ID="${VM_ID:-agent-000}"
RUN_SECONDS="${RUN_SECONDS:-10}"
USE_SUDO="${USE_SUDO:-0}"

config_path="${PLAN_DIR}/${VM_ID}.json"
socket_path="${PLAN_DIR}/${VM_ID}.socket"

if [[ ! -f "${config_path}" ]]; then
  echo "Missing config: ${config_path}" >&2
  exit 1
fi

rm -f "${socket_path}"
cmd=("${FIRECRACKER_BIN}" --api-sock "${socket_path}" --config-file "${config_path}")
if [[ "${USE_SUDO}" == "1" ]]; then
  sudo "${cmd[@]}" &
else
  "${cmd[@]}" &
fi
pid="$!"

sleep "${RUN_SECONDS}"
kill "${pid}" >/dev/null 2>&1 || true
wait "${pid}" >/dev/null 2>&1 || true
echo "Firecracker ${VM_ID} launched for ${RUN_SECONDS}s and was stopped."
