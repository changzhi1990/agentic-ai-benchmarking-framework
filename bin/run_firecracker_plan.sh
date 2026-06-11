#!/usr/bin/env bash
set -euo pipefail

PLAN_DIR="${PLAN_DIR:-runs/firecracker-plan}"
FIRECRACKER_BIN="${FIRECRACKER_BIN:-firecracker}"
VM_ID="${VM_ID:-agent-000}"
RUN_SECONDS="${RUN_SECONDS:-10}"
USE_SUDO="${USE_SUDO:-0}"
SUDO_PASSWORD="${SUDO_PASSWORD:-}"
LOG_PATH="${LOG_PATH:-${PLAN_DIR}/${VM_ID}.log}"

config_path="${PLAN_DIR}/${VM_ID}.json"
socket_path="${PLAN_DIR}/${VM_ID}.socket"

if [[ ! -f "${config_path}" ]]; then
  echo "Missing config: ${config_path}" >&2
  exit 1
fi

rm -f "${socket_path}"
cmd=("${FIRECRACKER_BIN}" --api-sock "${socket_path}" --config-file "${config_path}")
if [[ "${USE_SUDO}" == "1" ]]; then
  if [[ -n "${SUDO_PASSWORD}" ]]; then
    { printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' "${cmd[@]}"; } > "${LOG_PATH}" 2>&1 &
  else
    sudo "${cmd[@]}" > "${LOG_PATH}" 2>&1 &
  fi
else
  "${cmd[@]}" > "${LOG_PATH}" 2>&1 &
fi
pid="$!"

sleep "${RUN_SECONDS}"
kill "${pid}" >/dev/null 2>&1 || true
if [[ "${USE_SUDO}" == "1" && -n "${SUDO_PASSWORD}" ]]; then
  printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' pkill -TERM -f "${socket_path}" >/dev/null 2>&1 || true
else
  pkill -TERM -f "${socket_path}" >/dev/null 2>&1 || true
fi
sleep 1
if [[ "${USE_SUDO}" == "1" && -n "${SUDO_PASSWORD}" ]]; then
  printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' pkill -KILL -f "${socket_path}" >/dev/null 2>&1 || true
else
  pkill -KILL -f "${socket_path}" >/dev/null 2>&1 || true
fi
wait "${pid}" >/dev/null 2>&1 || true
echo "Firecracker ${VM_ID} launched for ${RUN_SECONDS}s and was stopped."
echo "Log: ${LOG_PATH}"
