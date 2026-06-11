#!/usr/bin/env bash
set -euo pipefail

RUN_DIR="${RUN_DIR:-runs/firecracker-run}"
RUN_SECONDS="${RUN_SECONDS:-120}"
SUDO_PASSWORD="${SUDO_PASSWORD:-}"
RESULTS_DIR="${RESULTS_DIR:-${RUN_DIR}/results}"
mkdir -p "${RESULTS_DIR}"

manifest="${RUN_DIR}/firecracker-run.json"
if [[ ! -f "${manifest}" ]]; then
  echo "Missing manifest: ${manifest}" >&2
  exit 1
fi

python3 - "$manifest" <<'PY' > "${RUN_DIR}/agent-list.tsv"
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for agent in manifest["agents"]:
    print("\t".join([
        agent["vm_id"],
        agent["config_path"],
        agent["socket_path"],
        agent["log_path"],
        agent["rootfs_image"],
    ]))
PY

pids=()
while IFS=$'\t' read -r vm_id config_path socket_path log_path rootfs_image; do
  rm -f "${socket_path}" "${log_path}"
  if [[ -n "${SUDO_PASSWORD}" ]]; then
    { printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' firecracker --api-sock "${socket_path}" --config-file "${config_path}"; } > "${log_path}" 2>&1 &
  else
    firecracker --api-sock "${socket_path}" --config-file "${config_path}" > "${log_path}" 2>&1 &
  fi
  pids+=("$!")
done < "${RUN_DIR}/agent-list.tsv"

sleep "${RUN_SECONDS}"

while IFS=$'\t' read -r vm_id config_path socket_path log_path rootfs_image; do
  if [[ -n "${SUDO_PASSWORD}" ]]; then
    printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' pkill -TERM -f "${socket_path}" >/dev/null 2>&1 || true
  else
    pkill -TERM -f "${socket_path}" >/dev/null 2>&1 || true
  fi
done < "${RUN_DIR}/agent-list.tsv"

sleep 1
for pid in "${pids[@]}"; do
  wait "${pid}" >/dev/null 2>&1 || true
done

mount_base="$(mktemp -d)"
trap 'rm -rf "${mount_base}"' EXIT
while IFS=$'\t' read -r vm_id config_path socket_path log_path rootfs_image; do
  mount_dir="${mount_base}/${vm_id}"
  mkdir -p "${mount_dir}"
  if [[ -n "${SUDO_PASSWORD}" ]]; then
    printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' mount -o loop "${rootfs_image}" "${mount_dir}"
    if [[ -f "${mount_dir}/var/lib/aab/result.json" ]]; then
      printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' cp "${mount_dir}/var/lib/aab/result.json" "${RESULTS_DIR}/${vm_id}.result.json"
    fi
    if [[ -f "${mount_dir}/var/lib/aab/trace.jsonl" ]]; then
      printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' cp "${mount_dir}/var/lib/aab/trace.jsonl" "${RESULTS_DIR}/${vm_id}.trace.jsonl"
    fi
    printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' umount "${mount_dir}"
  else
    sudo mount -o loop "${rootfs_image}" "${mount_dir}"
    sudo cp "${mount_dir}/var/lib/aab/result.json" "${RESULTS_DIR}/${vm_id}.result.json" 2>/dev/null || true
    sudo cp "${mount_dir}/var/lib/aab/trace.jsonl" "${RESULTS_DIR}/${vm_id}.trace.jsonl" 2>/dev/null || true
    sudo umount "${mount_dir}"
  fi
done < "${RUN_DIR}/agent-list.tsv"

python3 - "$RESULTS_DIR" <<'PY'
import json
import sys
from pathlib import Path

results_dir = Path(sys.argv[1])
records = []
for path in sorted(results_dir.glob("*.result.json")):
    try:
        records.append(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        pass
summary = {
    "vm_results": len(records),
    "completed_tasks": sum(item.get("completed_tasks", 0) for item in records),
    "failed_tasks": sum(item.get("failed_tasks", 0) for item in records),
    "vllm_ok": sum(1 for item in records if item.get("vllm_health") == "ok"),
    "results": records,
}
(results_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(summary, indent=2, sort_keys=True))
PY
