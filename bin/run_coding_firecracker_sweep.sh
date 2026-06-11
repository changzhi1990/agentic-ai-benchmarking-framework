#!/usr/bin/env bash
set -euo pipefail

AGENTS_LIST="${AGENTS_LIST:-2 4 8 16 32 64 128 192 256}"
SWEEP_ROOT="${SWEEP_ROOT:-runs/coding-firecracker-sweep-$(date +%Y%m%d-%H%M%S)}"
KERNEL_IMAGE="${KERNEL_IMAGE:-/opt/firecracker/vmlinux}"
BASE_ROOTFS_IMAGE="${BASE_ROOTFS_IMAGE:-/opt/firecracker/rootfs.ext4}"
HOST_VLLM_URL="${HOST_VLLM_URL:-http://172.16.0.1:8000/v1}"
SUDO_PASSWORD="${SUDO_PASSWORD:-}"
PCM_BIN="${PCM_BIN:-/home/user/zhi/AMDuProf_Nda_Linux_x64_5.0.1479/bin/AMDuProfPcm}"
RUN_SECONDS="${RUN_SECONDS:-180}"

mkdir -p "${SWEEP_ROOT}"
echo "${SWEEP_ROOT}" > runs/latest_coding_firecracker_sweep_dir.txt

plan_point() {
  local agents="$1"
  if [[ "${agents}" -le 8 ]]; then
    echo "${agents} 1"
  elif [[ "${agents}" -le 32 ]]; then
    echo "8 $(((agents + 7) / 8))"
  elif [[ "${agents}" -le 128 ]]; then
    echo "16 $(((agents + 15) / 16))"
  else
    echo "32 $(((agents + 31) / 32))"
  fi
}

start_gpu_metrics() {
  local run_dir="$1"
  mkdir -p "${run_dir}/metrics"
  (
    echo "timestamp,index,utilization_gpu_pct,utilization_memory_pct,memory_used_mib,memory_total_mib,power_draw_w,temperature_c"
    while true; do
      ts="$(date +%s)"
      nvidia-smi --query-gpu=index,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,temperature.gpu --format=csv,noheader,nounits 2>/dev/null \
        | awk -v ts="${ts}" -F', ' '{print ts "," $0}'
      sleep 1
    done
  ) > "${run_dir}/metrics/gpu.csv" &
  GPU_METRICS_PID="$!"
}

stop_gpu_metrics() {
  if [[ -n "${GPU_METRICS_PID:-}" ]]; then
    kill "${GPU_METRICS_PID}" >/dev/null 2>&1 || true
    wait "${GPU_METRICS_PID}" >/dev/null 2>&1 || true
  fi
}

start_pcm() {
  local run_dir="$1"
  mkdir -p "${run_dir}/metrics"
  PCM_PID=""
  if [[ -x "${PCM_BIN}" && -n "${SUDO_PASSWORD}" ]]; then
    (
      printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' "${PCM_BIN}" top \
        -m memory -a -A system --msr -d "$((RUN_SECONDS + 120))" -I 1200 \
        -o "${run_dir}/metrics/amd_pcm_memory.csv"
    ) > "${run_dir}/metrics/amd_pcm.stdout" 2> "${run_dir}/metrics/amd_pcm.stderr" &
    PCM_PID="$!"
    sleep 2
  fi
}

stop_pcm() {
  if [[ -n "${PCM_PID:-}" ]]; then
    printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' pkill -INT -f "${PCM_BIN}" >/dev/null 2>&1 || true
    sleep 1
    wait "${PCM_PID}" >/dev/null 2>&1 || true
  fi
}

for agents in ${AGENTS_LIST}; do
  read -r vm_count tasks_per_vm < <(plan_point "${agents}")
  run_dir="${SWEEP_ROOT}/agents_${agents}"
  echo "===== START agents=${agents} vm_count=${vm_count} tasks_per_vm=${tasks_per_vm} ====="
  mkdir -p "${run_dir}"
  printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' env VM_COUNT="${vm_count}" TAP_OWNER="${USER}" ./bin/setup_firecracker_network.sh >/dev/null
  python3 -m aab_framework.cli prepare-firecracker-run \
    --out-dir "${run_dir}" \
    --vm-count "${vm_count}" \
    --kernel-image "${KERNEL_IMAGE}" \
    --base-rootfs-image "${BASE_ROOTFS_IMAGE}" \
    --host-vllm-url "${HOST_VLLM_URL}" \
    --tasks-per-vm "${tasks_per_vm}" \
    --request-workers 1 \
    --vcpu-count 2 \
    --mem-mib 1024 >/dev/null
  start_gpu_metrics "${run_dir}"
  start_pcm "${run_dir}"
  RUN_DIR="${run_dir}" RUN_SECONDS="${RUN_SECONDS}" SUDO_PASSWORD="${SUDO_PASSWORD}" ./bin/run_prepared_firecracker_agents.sh | tee "${run_dir}/run.log"
  stop_pcm
  stop_gpu_metrics
  echo "===== END agents=${agents} ====="
done

python3 - "${SWEEP_ROOT}" <<'PY'
import csv
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
rows = []
for path in sorted(root.glob("agents_*/results/summary.json"), key=lambda p: int(p.parts[-3].split("_")[1])):
    agents = int(path.parts[-3].split("_")[1])
    summary = json.loads(path.read_text(encoding="utf-8"))
    rows.append({
        "agents": agents,
        "vm_results": summary.get("vm_results", 0),
        "completed_tasks": summary.get("completed_tasks", 0),
        "failed_tasks": summary.get("failed_tasks", 0),
        "vllm_ok": summary.get("vllm_ok", 0),
    })
out = root / "sweep_summary.csv"
with out.open("w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=["agents", "vm_results", "completed_tasks", "failed_tasks", "vllm_ok"])
    writer.writeheader()
    writer.writerows(rows)
print(json.dumps(rows, indent=2, sort_keys=True))
PY
