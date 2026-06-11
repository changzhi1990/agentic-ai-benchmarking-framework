from __future__ import annotations


def build_guest_agent_script() -> str:
    return """#!/bin/sh
set -eu

# Expected kernel arguments include agent.vm_id and agent.host_vllm_url.
# The readiness result is written to /var/lib/aab/result.json.

value_from_cmdline() {
  key="$1"
  for item in $(cat /proc/cmdline); do
    case "$item" in
      "$key="*) printf '%s' "${item#*=}"; return 0 ;;
    esac
  done
  return 0
}

vm_id="$(value_from_cmdline agent.vm_id)"
guest_ip="$(value_from_cmdline agent.guest_ip)"
host_ip="$(value_from_cmdline agent.host_ip)"
host_vllm_url="$(value_from_cmdline agent.host_vllm_url)"
tasks_per_vm="$(value_from_cmdline agent.tasks_per_vm)"
request_workers="$(value_from_cmdline agent.request_workers)"
tasks_per_vm="${tasks_per_vm:-1}"
request_workers="${request_workers:-1}"
memory_rounds="${AAB_MEMORY_ROUNDS:-16}"
memory_mb="${AAB_MEMORY_MB:-128}"
memory_workers="${AAB_MEMORY_WORKERS:-2}"
timestamp_unix="$(date +%s)"
models_url="${host_vllm_url%/}/models"
chat_url="${host_vllm_url%/}/chat/completions"
vllm_health="unavailable"
vllm_models_payload=""
if command -v curl >/dev/null 2>&1; then
  if vllm_models_payload="$(curl -fsS --max-time 5 "${models_url}" 2>/tmp/aab-vllm-probe.err)"; then
    vllm_health="ok"
  else
    vllm_health="error"
    vllm_models_payload="$(cat /tmp/aab-vllm-probe.err 2>/dev/null || true)"
  fi
elif command -v wget >/dev/null 2>&1; then
  if vllm_models_payload="$(wget -q -T 5 -O - "${models_url}" 2>/tmp/aab-vllm-probe.err)"; then
    vllm_health="ok"
  else
    vllm_health="error"
    vllm_models_payload="$(cat /tmp/aab-vllm-probe.err 2>/dev/null || true)"
  fi
else
  vllm_models_payload="curl_or_wget_not_found"
fi
vllm_models_payload_escaped="$(printf '%s' "${vllm_models_payload}" | tr '\\n' ' ' | sed 's/"/\\\\"/g' | cut -c 1-2048)"

completed_tasks=0
failed_tasks=0
first_latency_ms=0
last_latency_ms=0
trace_path="/var/lib/aab/trace.jsonl"
mkdir -p /var/lib/aab
: > "${trace_path}"

run_memory_worker() {
  worker_id="$1"
  task_id="$2"
  memory_dir="/dev/shm"
  if [ ! -d "${memory_dir}" ]; then memory_dir="/tmp"; fi
  memory_file="${memory_dir}/${task_id}.worker-${worker_id}.memory.bin"
  touched_file="/tmp/${task_id}.worker-${worker_id}.touched"
  memory_bytes=$((memory_mb * 1024 * 1024))
  touched=0
  dd if=/dev/zero of="${memory_file}" bs=1M count="${memory_mb}" conv=fsync >/dev/null 2>&1 || true
  round=0
  while [ "${round}" -lt "${memory_rounds}" ]; do
    cat "${memory_file}" >/dev/null 2>&1 || true
    cksum "${memory_file}" >/dev/null 2>&1 || true
    dd if=/dev/zero of="${memory_file}" bs=1M count="${memory_mb}" conv=notrunc >/dev/null 2>&1 || true
    touched=$((touched + memory_bytes * 3))
    round=$((round + 1))
  done
  rm -f "${memory_file}" >/dev/null 2>&1 || true
  printf '%s' "${touched}" > "${touched_file}"
}

run_task() {
  task_id="$1"
  started_ms="$(date +%s%3N 2>/dev/null || echo 0)"
  memory_touched_bytes=0
  worker=0
  while [ "${worker}" -lt "${memory_workers}" ]; do
    run_memory_worker "${worker}" "${task_id}" &
    worker=$((worker + 1))
  done
  wait
  worker=0
  while [ "${worker}" -lt "${memory_workers}" ]; do
    touched_file="/tmp/${task_id}.worker-${worker}.touched"
    if [ -f "${touched_file}" ]; then
      memory_touched_bytes=$((memory_touched_bytes + $(cat "${touched_file}")))
      rm -f "${touched_file}" >/dev/null 2>&1 || true
    fi
    worker=$((worker + 1))
  done
  prompt="You are a coding bugfix agent. Diagnose this synthetic bug and propose a minimal patch plan. Task ${task_id}: retry_state is not persisted after timeout. Return concise JSON fields diagnosis, patch_plan, verification."
  payload="$(cat <<EOF_PAYLOAD
{"model":"/workspace/models/Qwen2.5-Coder-32B-Instruct/","messages":[{"role":"system","content":"You are a coding bugfix agent."},{"role":"user","content":"${prompt}"}],"temperature":0.1,"max_tokens":128}
EOF_PAYLOAD
)"
  status="error"
  response=""
  if command -v curl >/dev/null 2>&1; then
    if response="$(curl -fsS --max-time 60 -H 'Content-Type: application/json' -d "${payload}" "${chat_url}" 2>/tmp/aab-chat.err)"; then
      status="ok"
    else
      response="$(cat /tmp/aab-chat.err 2>/dev/null || true)"
    fi
  elif command -v wget >/dev/null 2>&1; then
    if response="$(wget -q -T 60 --header='Content-Type: application/json' --post-data="${payload}" -O - "${chat_url}" 2>/tmp/aab-chat.err)"; then
      status="ok"
    else
      response="$(cat /tmp/aab-chat.err 2>/dev/null || true)"
    fi
  else
    response="curl_or_wget_not_found"
  fi
  ended_ms="$(date +%s%3N 2>/dev/null || echo 0)"
  latency_ms=0
  if [ "${started_ms}" != "0" ] && [ "${ended_ms}" != "0" ]; then
    latency_ms=$((ended_ms - started_ms))
  fi
  response_chars="$(printf '%s' "${response}" | wc -c | tr -d ' ')"
  printf '{"task_id":"%s","workload":"coding_bugfix","status":"%s","latency_ms":%s,"response_chars":%s,"memory_rounds":%s,"memory_mb":%s,"memory_touched_bytes":%s}\\n' \
    "${task_id}" "${status}" "${latency_ms}" "${response_chars}" "${memory_rounds}" "${memory_mb}" "${memory_touched_bytes}" >> "${trace_path}"
  last_latency_ms="${latency_ms}"
  if [ "${first_latency_ms}" = "0" ]; then first_latency_ms="${latency_ms}"; fi
  if [ "${status}" = "ok" ]; then
    completed_tasks=$((completed_tasks + 1))
  else
    failed_tasks=$((failed_tasks + 1))
  fi
}

task_index=0
while [ "${task_index}" -lt "${tasks_per_vm}" ]; do
  run_task "${vm_id}-task-${task_index}"
  task_index=$((task_index + 1))
done

cat > /var/lib/aab/result.json <<EOF
{
  "agent_logic": "coding_bugfix_skeleton",
  "completed_tasks": ${completed_tasks},
  "failed_tasks": ${failed_tasks},
  "guest_ip": "${guest_ip}",
  "host_ip": "${host_ip}",
  "host_vllm_url": "${host_vllm_url}",
  "memory_mb": ${memory_mb},
  "memory_rounds": ${memory_rounds},
  "memory_workers": ${memory_workers},
  "request_workers": ${request_workers},
  "status": "ready",
  "tasks_per_vm": ${tasks_per_vm},
  "timestamp_unix": ${timestamp_unix},
  "trace_path": "${trace_path}",
  "vllm_health": "${vllm_health}",
  "vllm_chat_url": "${chat_url}",
  "vllm_models_url": "${models_url}",
  "vllm_models_payload": "${vllm_models_payload_escaped}",
  "vm_id": "${vm_id}"
}
EOF

cat /var/lib/aab/result.json
"""


def build_guest_systemd_unit() -> str:
    return """[Unit]
Description=Agentic AI Benchmark Guest Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/aab-guest-agent
StandardOutput=journal+console
StandardError=journal+console
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
