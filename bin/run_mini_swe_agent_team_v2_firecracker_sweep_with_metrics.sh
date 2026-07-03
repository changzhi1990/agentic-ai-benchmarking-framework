#!/usr/bin/env bash
set -euo pipefail

AGENT_SWEEP="${AGENT_SWEEP:-1 2 4}"
SWEEP_ROOT="${SWEEP_ROOT:-runs/team-v2-firecracker-sweep-metrics-$(date +%Y%m%d-%H%M%S)}"
PARALLELISM="${PARALLELISM:-4}"
CONTEXT_LENGTH="${CONTEXT_LENGTH:-1024}"
MINI_COMMAND="${MINI_COMMAND:-mini}"
MODEL="${MODEL:-openai/agentic-model}"
HOST_VLLM_BASE_URL="${HOST_VLLM_BASE_URL:-http://127.0.0.1:8000/v1}"
GUEST_VLLM_BASE_URL="${GUEST_VLLM_BASE_URL:-http://172.16.0.1:8000/v1}"
FC_ROOTFS="${FC_ROOTFS:-/home/user/agentic-ai-benchmarking-framework/rootfs-mini-swe-agent.ext4}"
FC_KERNEL="${FC_KERNEL:-/opt/firecracker/vmlinux}"
FIRECRACKER_RUN_SECONDS="${FIRECRACKER_RUN_SECONDS:-240}"
FIRECRACKER_VCPU_COUNT="${FIRECRACKER_VCPU_COUNT:-2}"
FIRECRACKER_MEM_MIB="${FIRECRACKER_MEM_MIB:-4096}"
PCM_BIN="${PCM_BIN:-/home/user/zhi/AMDuProf_Nda_Linux_x64_5.0.1479/bin/AMDuProfPcm}"
AAB_DCGMI_BIN="${AAB_DCGMI_BIN:-dcgmi}"
AAB_DCGM_INTERVAL_MS="${AAB_DCGM_INTERVAL_MS:-1000}"
AAB_DCGM_FIELD_IDS="${AAB_DCGM_FIELD_IDS:-203,204,252,250,155,150,1002,1003,1004,1005,1007,1008}"
SUDO_PASSWORD="${SUDO_PASSWORD:-}"
AAB_CPU_PINNING="${AAB_CPU_PINNING:-1}"
AAB_NUMA_POLICY="${AAB_NUMA_POLICY:-bind-by-agent}"

mkdir -p "${SWEEP_ROOT}"

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

start_gpu_metrics() {
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
    (
      printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' "${PCM_BIN}" top \
        -m memory -a -A system --msr -d 3600 -I 1200 \
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

refresh_run_metrics() {
  local run_dir="$1"
  python3 - "${run_dir}" <<'PY'
import sys
from pathlib import Path
from aab_framework.team_v2.metrics_wrapper import refresh_run_metrics

refresh_run_metrics(Path(sys.argv[1]))
PY
}

child_results=()
for agents in ${AGENT_SWEEP//,/ }; do
  point_tmp="${SWEEP_ROOT}/metrics_agents_${agents}"
  mkdir -p "${point_tmp}"
  echo "===== START firecracker mini_swe_agent_team_v2 agents=${agents} ====="
  if [[ -n "${SUDO_PASSWORD}" ]]; then
    printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' env VM_COUNT="${agents}" TAP_OWNER="${USER}" CIDR=16 ./bin/setup_firecracker_network.sh >/dev/null
  else
    sudo env VM_COUNT="${agents}" TAP_OWNER="${USER}" CIDR=16 ./bin/setup_firecracker_network.sh >/dev/null
  fi
  start_cpu_metrics "${point_tmp}"
  start_gpu_metrics "${point_tmp}"
  start_pcm "${point_tmp}"
  set +e
  run_output="$(
    SUDO_PASSWORD="${SUDO_PASSWORD}" AAB_CPU_PINNING="${AAB_CPU_PINNING}" AAB_NUMA_POLICY="${AAB_NUMA_POLICY}" \
    python3 -m aab_framework.cli run \
      --workload mini_swe_agent_team_v2 \
      --num-agents "${agents}" \
      --parallelism "${agents}" \
      --context-length "${CONTEXT_LENGTH}" \
      --adapter-mode cli \
      --mini-command "${MINI_COMMAND}" \
      --model "${MODEL}" \
      --vllm-base-url "${HOST_VLLM_BASE_URL}" \
      --use-firecracker true \
      --fc-rootfs "${FC_ROOTFS}" \
      --fc-kernel "${FC_KERNEL}" \
      --guest-vllm-base-url "${GUEST_VLLM_BASE_URL}" \
      --firecracker-run-seconds "${FIRECRACKER_RUN_SECONDS}" \
      --firecracker-vcpu-count "${FIRECRACKER_VCPU_COUNT}" \
      --firecracker-mem-mib "${FIRECRACKER_MEM_MIB}" \
      --out-dir "${SWEEP_ROOT}" 2>&1
  )"
  rc="$?"
  set -e
  stop_pcm
  stop_cpu_metrics
  stop_gpu_metrics
  printf '%s\n' "${run_output}" | tee "${point_tmp}/run.log"
  if [[ "${rc}" -ne 0 ]]; then
    echo "run failed for agents=${agents} rc=${rc}" >&2
    exit "${rc}"
  fi
  run_dir="$(printf '%s\n' "${run_output}" | awk -F': ' '/^Run dir:/ {print $2}' | tail -1)"
  result_path="$(printf '%s\n' "${run_output}" | awk -F': ' '/^Result:/ {print $2}' | tail -1)"
  if [[ -z "${run_dir}" || -z "${result_path}" ]]; then
    echo "unable to parse run output for agents=${agents}" >&2
    exit 1
  fi
  rm -rf "${run_dir}/metrics"
  cp -a "${point_tmp}/metrics" "${run_dir}/metrics"
  refresh_run_metrics "${run_dir}"
  child_results+=("${result_path}")
  echo "===== END firecracker mini_swe_agent_team_v2 agents=${agents} result=${result_path} ====="
done

python3 - "${SWEEP_ROOT}" "${child_results[@]}" <<'PY'
import sys
from pathlib import Path
from aab_framework.team_v2.metrics_wrapper import build_sweep_from_child_runs

sweep_root = Path(sys.argv[1])
child_results = [Path(item) for item in sys.argv[2:]]
result = build_sweep_from_child_runs(sweep_root, child_results)
print(f"Sweep Group: {result['run_id']}")
print(f"Run dir: {sweep_root}")
print(f"Result: {sweep_root / 'result.json'}")
for item in result["runs"]:
    print(f"Run: num_agents={item['num_agents']} run_id={item['run_id']} result={item['result_path']}")
PY
