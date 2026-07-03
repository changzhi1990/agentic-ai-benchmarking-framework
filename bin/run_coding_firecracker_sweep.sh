#!/usr/bin/env bash
set -euo pipefail

AGENTS_LIST="${AGENTS_LIST:-1 2 4 8 16 32 64 128}"
SWEEP_ROOT="${SWEEP_ROOT:-runs/coding-firecracker-sweep-$(date +%Y%m%d-%H%M%S)}"
KERNEL_IMAGE="${KERNEL_IMAGE:-/opt/firecracker/vmlinux}"
BASE_ROOTFS_IMAGE="${BASE_ROOTFS_IMAGE:-/opt/firecracker/rootfs.ext4}"
HOST_VLLM_URL="${HOST_VLLM_URL:-http://172.16.0.1:8000/v1}"
SUDO_PASSWORD="${SUDO_PASSWORD:-}"
PCM_BIN="${PCM_BIN:-/home/user/zhi/AMDuProf_Nda_Linux_x64_5.0.1479/bin/AMDuProfPcm}"
RUN_SECONDS="${RUN_SECONDS:-180}"
WORKLOAD_GRACE_SECONDS="${WORKLOAD_GRACE_SECONDS:-60}"
AAB_MEMORY_WORKERS="${AAB_MEMORY_WORKERS:-8}"
AAB_MEMORY_WORKERS_PROFILE="${AAB_MEMORY_WORKERS_PROFILE:-fixed}"
AAB_MEMORY_MB="${AAB_MEMORY_MB:-256}"
AAB_MEMORY_ROUNDS="${AAB_MEMORY_ROUNDS:-16}"
AAB_MEMORY_MODE="${AAB_MEMORY_MODE:-read}"
AAB_LLM_CONTEXT_KB="${AAB_LLM_CONTEXT_KB:-32}"
AAB_LLM_PROMPT_REPEAT="${AAB_LLM_PROMPT_REPEAT:-1}"
AAB_LLM_MAX_TOKENS="${AAB_LLM_MAX_TOKENS:-512}"
AAB_LLM_LOAD_MODE="${AAB_LLM_LOAD_MODE:-single_task}"
AAB_LLM_REQUEST_TIMEOUT_SECONDS="${AAB_LLM_REQUEST_TIMEOUT_SECONDS:-120}"
AAB_LLM_INTER_TASK_SLEEP_MS="${AAB_LLM_INTER_TASK_SLEEP_MS:-0}"
AAB_DCGMI_BIN="${AAB_DCGMI_BIN:-dcgmi}"
AAB_DCGM_INTERVAL_MS="${AAB_DCGM_INTERVAL_MS:-1000}"
AAB_DCGM_FIELD_IDS="${AAB_DCGM_FIELD_IDS:-203,204,252,250,155,150,1002,1003,1004,1005,1007,1008}"
AAB_CPU_PINNING="${AAB_CPU_PINNING:-1}"
AAB_NUMA_POLICY="${AAB_NUMA_POLICY:-bind-by-agent}"
AAB_AGENTS_PER_VM="${AAB_AGENTS_PER_VM:-1}"
AAB_MEMORY_WORKERS_PER_AGENT="${AAB_MEMORY_WORKERS_PER_AGENT:-8}"
AAB_VCPUS_PER_AGENT="${AAB_VCPUS_PER_AGENT:-8}"

mkdir -p "${SWEEP_ROOT}"
echo "${SWEEP_ROOT}" > runs/latest_coding_firecracker_sweep_dir.txt

plan_point() {
  local agents="$1"
  local limit="${AAB_AGENTS_PER_VM}"
  local tasks_per_vm
  if [[ "${limit}" -gt "${agents}" ]]; then
    limit="${agents}"
  fi
  for tasks_per_vm in $(seq "${limit}" -1 1); do
    if [[ $((agents % tasks_per_vm)) -eq 0 ]]; then
      echo "$((agents / tasks_per_vm)) ${tasks_per_vm}"
      return 0
    fi
  done
  echo "${agents} 1"
}

workload_seconds_for_run() {
  if [[ "${RUN_SECONDS}" -gt "${WORKLOAD_GRACE_SECONDS}" ]]; then
    echo "$((RUN_SECONDS - WORKLOAD_GRACE_SECONDS))"
  else
    echo "${RUN_SECONDS}"
  fi
}

memory_workers_for_agents() {
  local agents="$1"
  local tasks_per_vm="$2"
  if [[ "${AAB_MEMORY_WORKERS_PROFILE}" == "per-agent" ]]; then
    echo "$((tasks_per_vm * AAB_MEMORY_WORKERS_PER_AGENT))"
  elif [[ "${AAB_MEMORY_WORKERS_PROFILE}" != "bandwidth" ]]; then
    echo "${AAB_MEMORY_WORKERS}"
  elif [[ "${agents}" -le 4 ]]; then
    echo 32
  elif [[ "${agents}" -le 16 ]]; then
    echo 24
  elif [[ "${agents}" -le 64 ]]; then
    echo 16
  else
    echo "${AAB_MEMORY_WORKERS}"
  fi
}

vcpu_count_for_tasks_per_vm() {
  local tasks_per_vm="$1"
  echo "$((tasks_per_vm * AAB_VCPUS_PER_AGENT))"
}

start_gpu_metrics() {
  start_gpu_metrics_dcgm "$1"
}

start_gpu_metrics_dcgm() {
  local run_dir="$1"
  mkdir -p "${run_dir}/metrics"
  if ! command -v "${AAB_DCGMI_BIN}" >/dev/null 2>&1; then
    echo "dcgmi unavailable; GPU metrics disabled" > "${run_dir}/metrics/gpu_metrics_backend.log"
    echo "timestamp,index,utilization_gpu_pct,utilization_memory_pct,memory_used_mib,memory_total_mib,power_draw_w,temperature_c,memory_used_pct,gr_engine_active_pct,sm_active_pct,sm_occupancy_pct,tensor_active_pct,dram_active_pct,fp32_active_pct,fp16_active_pct,gpu_metrics_backend,dcgm_metrics_backend" > "${run_dir}/metrics/gpu.csv"
    return 0
  fi
  echo "dcgmi" > "${run_dir}/metrics/gpu_metrics_backend.log"
  (
    echo "timestamp,index,utilization_gpu_pct,utilization_memory_pct,memory_used_mib,memory_total_mib,power_draw_w,temperature_c,memory_used_pct,gr_engine_active_pct,sm_active_pct,sm_occupancy_pct,tensor_active_pct,dram_active_pct,fp32_active_pct,fp16_active_pct,gpu_metrics_backend,dcgm_metrics_backend"
    "${AAB_DCGMI_BIN}" dmon -e "${AAB_DCGM_FIELD_IDS}" -d "${AAB_DCGM_INTERVAL_MS}" 2>>"${run_dir}/metrics/dcgm.stderr" \
      | while IFS= read -r line; do
          ts="$(date +%s)"
          printf '%s\n' "${line}" | python3 -m aab_framework.dcgm --field-ids "${AAB_DCGM_FIELD_IDS}" --timestamp "${ts}" | tail -n +2 || true
        done
  ) > "${run_dir}/metrics/gpu.csv" &
  GPU_METRICS_PID="$!"
}

start_cpu_metrics() {
  local run_dir="$1"
  mkdir -p "${run_dir}/metrics"
  (
    echo "timestamp,cpu_util_pct,user_pct,system_pct,iowait_pct,idle_pct,load1,load5,load15"
    prev_total=""
    prev_idle=""
    while true; do
      read -r _ user nice system idle iowait irq softirq steal guest guest_nice < /proc/stat
      total=$((user + nice + system + idle + iowait + irq + softirq + steal))
      idle_all=$((idle + iowait))
      if [[ -n "${prev_total}" ]]; then
        total_delta=$((total - prev_total))
        idle_delta=$((idle_all - prev_idle))
        user_delta=$((user - prev_user))
        system_delta=$((system - prev_system))
        iowait_delta=$((iowait - prev_iowait))
        if [[ "${total_delta}" -gt 0 ]]; then
          cpu_util="$(awk -v t="${total_delta}" -v i="${idle_delta}" 'BEGIN { printf "%.4f", 100*(1-i/t) }')"
          user_pct="$(awk -v t="${total_delta}" -v v="${user_delta}" 'BEGIN { printf "%.4f", 100*v/t }')"
          system_pct="$(awk -v t="${total_delta}" -v v="${system_delta}" 'BEGIN { printf "%.4f", 100*v/t }')"
          iowait_pct="$(awk -v t="${total_delta}" -v v="${iowait_delta}" 'BEGIN { printf "%.4f", 100*v/t }')"
          idle_pct="$(awk -v t="${total_delta}" -v v="${idle_delta}" 'BEGIN { printf "%.4f", 100*v/t }')"
          read -r load1 load5 load15 _ < /proc/loadavg
          echo "$(date +%s),${cpu_util},${user_pct},${system_pct},${iowait_pct},${idle_pct},${load1},${load5},${load15}"
        fi
      fi
      prev_total="${total}"
      prev_idle="${idle_all}"
      prev_user="${user}"
      prev_system="${system}"
      prev_iowait="${iowait}"
      sleep 1
    done
  ) > "${run_dir}/metrics/cpu.csv" &
  CPU_METRICS_PID="$!"
}

stop_cpu_metrics() {
  if [[ -n "${CPU_METRICS_PID:-}" ]]; then
    kill "${CPU_METRICS_PID}" >/dev/null 2>&1 || true
    wait "${CPU_METRICS_PID}" >/dev/null 2>&1 || true
  fi
}

stop_gpu_metrics() {
  if [[ -n "${GPU_METRICS_PID:-}" ]]; then
    pkill -TERM -P "${GPU_METRICS_PID}" >/dev/null 2>&1 || true
    kill "${GPU_METRICS_PID}" >/dev/null 2>&1 || true
    wait "${GPU_METRICS_PID}" >/dev/null 2>&1 || true
  fi
}

start_pcm() {
  local run_dir="$1"
  mkdir -p "${run_dir}/metrics"
  PCM_PID=""
  if [[ -x "${PCM_BIN}" && -n "${SUDO_PASSWORD}" ]]; then
    printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' "${PCM_BIN}" top -r \
      -m memory -a -A system --msr -d 1 -I 1200 \
      -o "${run_dir}/metrics/amd_pcm_reset.csv" \
      > "${run_dir}/metrics/amd_pcm_reset.stdout" 2> "${run_dir}/metrics/amd_pcm_reset.stderr" || true
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
  workload_seconds="$(workload_seconds_for_run)"
  memory_workers="$(memory_workers_for_agents "${agents}" "${tasks_per_vm}")"
  vcpu_count="$(vcpu_count_for_tasks_per_vm "${tasks_per_vm}")"
  run_dir="${SWEEP_ROOT}/agents_${agents}"
  echo "===== START agents=${agents} vm_count=${vm_count} tasks_per_vm=${tasks_per_vm} workload_seconds=${workload_seconds} memory_workers=${memory_workers} vcpu_count=${vcpu_count} llm_context_kb=${AAB_LLM_CONTEXT_KB} llm_prompt_repeat=${AAB_LLM_PROMPT_REPEAT} llm_max_tokens=${AAB_LLM_MAX_TOKENS} llm_load_mode=${AAB_LLM_LOAD_MODE} llm_request_timeout_seconds=${AAB_LLM_REQUEST_TIMEOUT_SECONDS} llm_inter_task_sleep_ms=${AAB_LLM_INTER_TASK_SLEEP_MS} ====="
  mkdir -p "${run_dir}"
  printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' env VM_COUNT="${vm_count}" TAP_OWNER="${USER}" CIDR=16 ./bin/setup_firecracker_network.sh >/dev/null
  python3 -m aab_framework.cli prepare-firecracker-run \
    --out-dir "${run_dir}" \
    --vm-count "${vm_count}" \
    --kernel-image "${KERNEL_IMAGE}" \
    --base-rootfs-image "${BASE_ROOTFS_IMAGE}" \
    --host-vllm-url "${HOST_VLLM_URL}" \
    --tasks-per-vm "${tasks_per_vm}" \
    --request-workers 1 \
    --workload-seconds "${workload_seconds}" \
    --memory-workers "${memory_workers}" \
    --memory-mb "${AAB_MEMORY_MB}" \
    --memory-rounds "${AAB_MEMORY_ROUNDS}" \
    --memory-mode "${AAB_MEMORY_MODE}" \
    --llm-context-kb "${AAB_LLM_CONTEXT_KB}" \
    --llm-prompt-repeat "${AAB_LLM_PROMPT_REPEAT}" \
    --llm-max-tokens "${AAB_LLM_MAX_TOKENS}" \
    --llm-load-mode "${AAB_LLM_LOAD_MODE}" \
    --llm-request-timeout-seconds "${AAB_LLM_REQUEST_TIMEOUT_SECONDS}" \
    --llm-inter-task-sleep-ms "${AAB_LLM_INTER_TASK_SLEEP_MS}" \
    --vcpu-count "${vcpu_count}" \
    --mem-mib 16384 >/dev/null
  start_gpu_metrics "${run_dir}"
  start_cpu_metrics "${run_dir}"
  start_pcm "${run_dir}"
  RUN_DIR="${run_dir}" RUN_SECONDS="${RUN_SECONDS}" SUDO_PASSWORD="${SUDO_PASSWORD}" AAB_CPU_PINNING="${AAB_CPU_PINNING}" AAB_NUMA_POLICY="${AAB_NUMA_POLICY}" ./bin/run_prepared_firecracker_agents.sh | tee "${run_dir}/run.log"
  stop_pcm
  stop_cpu_metrics
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
