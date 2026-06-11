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
memory_mb="${AAB_MEMORY_MB:-512}"
memory_workers="${AAB_MEMORY_WORKERS:-4}"
background_memory_seconds="${AAB_BACKGROUND_MEMORY_SECONDS:-75}"
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

background_pids=""
start_background_memory_workers() {
  if command -v aab-memory-burner >/dev/null 2>&1; then
    aab-memory-burner --threads "${memory_workers}" --mb-per-thread "${memory_mb}" --seconds "${background_memory_seconds}" > /var/lib/aab/memory-burner.log 2>&1 &
    background_pids="${background_pids} $!"
    return 0
  fi
  worker=0
  while [ "${worker}" -lt "${memory_workers}" ]; do
    (
      end_time=$(( $(date +%s) + background_memory_seconds ))
      while [ "$(date +%s)" -lt "${end_time}" ]; do
        run_memory_worker "bg-${worker}" "background-${vm_id}-$(date +%s)"
      done
    ) &
    background_pids="${background_pids} $!"
    worker=$((worker + 1))
  done
}

stop_background_memory_workers() {
  for pid in ${background_pids}; do
    kill "${pid}" >/dev/null 2>&1 || true
  done
  wait >/dev/null 2>&1 || true
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

start_background_memory_workers
task_index=0
while [ "${task_index}" -lt "${tasks_per_vm}" ]; do
  run_task "${vm_id}-task-${task_index}"
  task_index=$((task_index + 1))
done
stop_background_memory_workers

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
  "background_memory_seconds": ${background_memory_seconds},
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


def build_memory_burner_source() -> str:
    return r'''#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct {
    int id;
    size_t elements;
    int seconds;
    double bandwidth_gbps;
} worker_args_t;

static double now_seconds(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1000000000.0;
}

static void *worker_main(void *ptr) {
    worker_args_t *args = (worker_args_t *)ptr;
    size_t n = args->elements;
    double *a = NULL;
    double *b = NULL;
    double *c = NULL;
    const double scalar = 3.14159;
    if (posix_memalign((void **)&a, 64, n * sizeof(double)) ||
        posix_memalign((void **)&b, 64, n * sizeof(double)) ||
        posix_memalign((void **)&c, 64, n * sizeof(double))) {
        fprintf(stderr, "worker %d allocation failed: %s\n", args->id, strerror(errno));
        free(a); free(b); free(c);
        return NULL;
    }
    for (size_t i = 0; i < n; i++) {
        a[i] = 0.0;
        b[i] = (double)(i % 1024);
        c[i] = (double)((i + 17) % 1024);
    }
    double start = now_seconds();
    double end = start + (double)args->seconds;
    uint64_t iterations = 0;
    while (now_seconds() < end) {
        for (size_t i = 0; i < n; i++) {
            a[i] = b[i] + scalar * c[i];
        }
        double *tmp = b;
        b = c;
        c = a;
        a = tmp;
        iterations++;
    }
    double elapsed = now_seconds() - start;
    double bytes = (double)iterations * (double)n * sizeof(double) * 3.0;
    args->bandwidth_gbps = bytes / elapsed / 1000000000.0;
    fprintf(stdout, "worker=%d iterations=%lu bandwidth_gbps=%.3f\n",
            args->id, (unsigned long)iterations, args->bandwidth_gbps);
    fflush(stdout);
    free(a); free(b); free(c);
    return NULL;
}

int main(int argc, char **argv) {
    int threads = 2;
    int mb_per_thread = 128;
    int seconds = 60;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--threads") && i + 1 < argc) {
            threads = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--mb-per-thread") && i + 1 < argc) {
            mb_per_thread = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--seconds") && i + 1 < argc) {
            seconds = atoi(argv[++i]);
        }
    }
    if (threads < 1) threads = 1;
    if (mb_per_thread < 1) mb_per_thread = 1;
    if (seconds < 1) seconds = 1;
    pthread_t * tids = calloc((size_t)threads, sizeof(pthread_t));
    worker_args_t * args = calloc((size_t)threads, sizeof(worker_args_t));
    if (!tids || !args) {
        fprintf(stderr, "metadata allocation failed\n");
        return 2;
    }
    size_t elements = ((size_t)mb_per_thread * 1024ULL * 1024ULL) / (sizeof(double) * 3ULL);
    for (int i = 0; i < threads; i++) {
        args[i].id = i;
        args[i].elements = elements;
        args[i].seconds = seconds;
        if (pthread_create(&tids[i], NULL, worker_main, &args[i])) {
            fprintf(stderr, "pthread_create failed for worker %d\n", i);
            return 3;
        }
    }
    double total = 0.0;
    for (int i = 0; i < threads; i++) {
        pthread_join(tids[i], NULL);
        total += args[i].bandwidth_gbps;
    }
    fprintf(stdout, "total_bandwidth_gbps=%.3f threads=%d mb_per_thread=%d seconds=%d\n",
            total, threads, mb_per_thread, seconds);
    free(tids);
    free(args);
    return 0;
}
'''
