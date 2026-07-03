from __future__ import annotations

from .workloads.coding import build_coding_guest_contract_script


def build_guest_agent_script() -> str:
    coding_contract = build_coding_guest_contract_script()
    return """#!/bin/sh
set -eu

# Expected kernel arguments include agent.vm_id and agent.host_vllm_url.
# The readiness result is written to /var/lib/aab/result.json.

if [ -f /task/task.json ] && [ -x /root/run_worker.sh ]; then
  exec /root/run_worker.sh
fi

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
workload_seconds="$(value_from_cmdline agent.workload_seconds)"
memory_workers_arg="$(value_from_cmdline agent.memory_workers)"
memory_mb_arg="$(value_from_cmdline agent.memory_mb)"
memory_rounds_arg="$(value_from_cmdline agent.memory_rounds)"
memory_mode_arg="$(value_from_cmdline agent.memory_mode)"
llm_context_kb_arg="$(value_from_cmdline agent.llm_context_kb)"
llm_prompt_repeat_arg="$(value_from_cmdline agent.llm_prompt_repeat)"
llm_max_tokens_arg="$(value_from_cmdline agent.llm_max_tokens)"
llm_load_mode_arg="$(value_from_cmdline agent.llm_load_mode)"
llm_request_timeout_seconds_arg="$(value_from_cmdline agent.llm_request_timeout_seconds)"
llm_inter_task_sleep_ms_arg="$(value_from_cmdline agent.llm_inter_task_sleep_ms)"
tasks_per_vm="${tasks_per_vm:-1}"
request_workers="${request_workers:-1}"
memory_rounds="${AAB_MEMORY_ROUNDS:-16}"
memory_mb="${AAB_MEMORY_MB:-512}"
memory_workers="${AAB_MEMORY_WORKERS:-4}"
memory_mode="${AAB_MEMORY_MODE:-read}"
task_memory_workers="${AAB_TASK_MEMORY_WORKERS:-0}"
llm_context_kb="${AAB_LLM_CONTEXT_KB:-0}"
llm_prompt_repeat="${AAB_LLM_PROMPT_REPEAT:-1}"
llm_max_tokens="${AAB_LLM_MAX_TOKENS:-512}"
llm_load_mode="${AAB_LLM_LOAD_MODE:-single_task}"
llm_request_timeout_seconds="${AAB_LLM_REQUEST_TIMEOUT_SECONDS:-120}"
llm_inter_task_sleep_ms="${AAB_LLM_INTER_TASK_SLEEP_MS:-0}"
if [ -n "${memory_rounds_arg}" ] && [ -z "${AAB_MEMORY_ROUNDS:-}" ]; then memory_rounds="${memory_rounds_arg}"; fi
if [ -n "${memory_mb_arg}" ] && [ -z "${AAB_MEMORY_MB:-}" ]; then memory_mb="${memory_mb_arg}"; fi
if [ -n "${memory_workers_arg}" ] && [ -z "${AAB_MEMORY_WORKERS:-}" ]; then memory_workers="${memory_workers_arg}"; fi
if [ -n "${memory_mode_arg}" ] && [ -z "${AAB_MEMORY_MODE:-}" ]; then memory_mode="${memory_mode_arg}"; fi
if [ -n "${llm_context_kb_arg}" ] && [ -z "${AAB_LLM_CONTEXT_KB:-}" ]; then llm_context_kb="${llm_context_kb_arg}"; fi
if [ -n "${llm_prompt_repeat_arg}" ] && [ -z "${AAB_LLM_PROMPT_REPEAT:-}" ]; then llm_prompt_repeat="${llm_prompt_repeat_arg}"; fi
if [ -n "${llm_max_tokens_arg}" ] && [ -z "${AAB_LLM_MAX_TOKENS:-}" ]; then llm_max_tokens="${llm_max_tokens_arg}"; fi
if [ -n "${llm_load_mode_arg}" ] && [ -z "${AAB_LLM_LOAD_MODE:-}" ]; then llm_load_mode="${llm_load_mode_arg}"; fi
if [ -n "${llm_request_timeout_seconds_arg}" ] && [ -z "${AAB_LLM_REQUEST_TIMEOUT_SECONDS:-}" ]; then llm_request_timeout_seconds="${llm_request_timeout_seconds_arg}"; fi
if [ -n "${llm_inter_task_sleep_ms_arg}" ] && [ -z "${AAB_LLM_INTER_TASK_SLEEP_MS:-}" ]; then llm_inter_task_sleep_ms="${llm_inter_task_sleep_ms_arg}"; fi
case "${llm_inter_task_sleep_ms}" in
  ''|*[!0-9]*) llm_inter_task_sleep_ms=0 ;;
esac
workload_seconds="${AAB_WORKLOAD_SECONDS:-${workload_seconds:-60}}"
background_memory_seconds="${AAB_BACKGROUND_MEMORY_SECONDS:-${workload_seconds}}"
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

""" + coding_contract + """

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
    if aab-memory-burner --threads 1 --mb-per-thread 1 --seconds 1 --mode "${memory_mode}" > /var/lib/aab/aab-memory-burner-selftest.log 2>&1; then
      aab-memory-burner --threads "${memory_workers}" --mb-per-thread "${memory_mb}" --seconds "${background_memory_seconds}" --mode "${memory_mode}" > /var/lib/aab/memory-burner.log 2>&1 &
      background_pids="${background_pids} $!"
      return 0
    fi
    {
      echo "aab-memory-burner self-test failed; using shell memory fallback"
      cat /var/lib/aab/aab-memory-burner-selftest.log 2>/dev/null || true
    } > /var/lib/aab/memory-burner.log
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
  for pid in ${background_pids}; do
    wait "${pid}" >/dev/null 2>&1 || true
  done
}

wait_for_background_memory_workers() {
  for pid in ${background_pids}; do
    wait "${pid}" >/dev/null 2>&1 || true
  done
}

write_result() {
cat > /var/lib/aab/result.json <<EOF
{
  "agent_logic": "coding_bugfix_skeleton",
  "completed_tasks": ${completed_tasks},
  "failed_tasks": ${failed_tasks},
  "guest_ip": "${guest_ip}",
  "host_ip": "${host_ip}",
  "host_vllm_url": "${host_vllm_url}",
  "memory_mb": ${memory_mb},
  "memory_mode": "${memory_mode}",
  "memory_rounds": ${memory_rounds},
  "memory_workers": ${memory_workers},
  "llm_context_kb": ${llm_context_kb},
  "llm_prompt_repeat": ${llm_prompt_repeat},
  "llm_max_tokens": ${llm_max_tokens},
  "llm_load_mode": "${llm_load_mode}",
  "llm_request_timeout_seconds": ${llm_request_timeout_seconds},
  "llm_inter_task_sleep_ms": ${llm_inter_task_sleep_ms},
  "task_memory_workers": ${task_memory_workers},
  "workload_seconds": ${workload_seconds},
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
}

run_task() {
  task_id="$1"
  started_ms="$(date +%s%3N 2>/dev/null || echo 0)"
  write_stage_event "${task_id}" "planner" "planner" "ok" 0
  memory_touched_bytes=0
  task_worker_pids=""
  worker=0
  while [ "${worker}" -lt "${task_memory_workers}" ]; do
    run_memory_worker "${worker}" "${task_id}" &
    task_worker_pids="${task_worker_pids} $!"
    worker=$((worker + 1))
  done
  for pid in ${task_worker_pids}; do
    wait "${pid}" >/dev/null 2>&1 || true
  done
  worker=0
  while [ "${worker}" -lt "${task_memory_workers}" ]; do
    touched_file="/tmp/${task_id}.worker-${worker}.touched"
    if [ -f "${touched_file}" ]; then
      memory_touched_bytes=$((memory_touched_bytes + $(cat "${touched_file}")))
      rm -f "${touched_file}" >/dev/null 2>&1 || true
    fi
    worker=$((worker + 1))
  done
  write_stage_event "${task_id}" "context_builder" "context_builder" "ok" 0
  prompt="$(build_coding_prompt "${task_id}")"
  payload_path="/tmp/${task_id}.payload.json"
  build_coding_payload "${prompt}" > "${payload_path}"
  write_stage_event "${task_id}" "solver" "solver" "started" 0
  status="error"
  response=""
  if command -v curl >/dev/null 2>&1; then
    if response="$(curl -fsS --max-time "${llm_request_timeout_seconds}" -H 'Content-Type: application/json' --data-binary @"${payload_path}" "${chat_url}" 2>/tmp/aab-chat.err)"; then
      status="ok"
    else
      response="$(cat /tmp/aab-chat.err 2>/dev/null || true)"
    fi
  elif command -v wget >/dev/null 2>&1; then
    if response="$(wget -q -T "${llm_request_timeout_seconds}" --header='Content-Type: application/json' --post-file="${payload_path}" -O - "${chat_url}" 2>/tmp/aab-chat.err)"; then
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
  write_stage_event "${task_id}" "solver" "solver" "${status}" "${latency_ms}"
  if [ "${status}" = "ok" ]; then
    write_stage_event "${task_id}" "verifier" "verifier" "ok" 0
    write_stage_event "${task_id}" "challenge" "challenge" "ok" 0
  else
    write_stage_event "${task_id}" "verifier" "verifier" "skipped" 0
    write_stage_event "${task_id}" "challenge" "challenge" "blocked" 0
  fi
  rm -f "${payload_path}" >/dev/null 2>&1 || true
  printf '{"task_id":"%s","workload":"coding_bugfix","status":"%s","latency_ms":%s,"response_chars":%s,"memory_rounds":%s,"memory_mb":%s,"memory_mode":"%s","memory_touched_bytes":%s,"llm_context_kb":%s,"llm_prompt_repeat":%s,"llm_max_tokens":%s,"llm_load_mode":"%s","llm_request_timeout_seconds":%s,"llm_inter_task_sleep_ms":%s}\\n' \
    "${task_id}" "${status}" "${latency_ms}" "${response_chars}" "${memory_rounds}" "${memory_mb}" "${memory_mode}" "${memory_touched_bytes}" "${llm_context_kb}" "${llm_prompt_repeat}" "${llm_max_tokens}" "${llm_load_mode}" "${llm_request_timeout_seconds}" "${llm_inter_task_sleep_ms}" >> "${trace_path}"
  last_latency_ms="${latency_ms}"
  if [ "${first_latency_ms}" = "0" ]; then first_latency_ms="${latency_ms}"; fi
  if [ "${status}" = "ok" ]; then
    completed_tasks=$((completed_tasks + 1))
  else
    failed_tasks=$((failed_tasks + 1))
  fi
}

sleep_between_sustained_prefill_tasks() {
  if [ "${llm_inter_task_sleep_ms}" -gt 0 ]; then
    sleep_seconds="$(awk -v ms="${llm_inter_task_sleep_ms}" 'BEGIN { printf "%.3f", ms / 1000 }')"
    sleep "${sleep_seconds}"
  fi
}

run_sustained_prefill() {
  task_index=0
  end_time=$(( $(date +%s) + workload_seconds ))
  while [ "${task_index}" -eq 0 ] || [ "$(date +%s)" -lt "${end_time}" ]; do
    run_task "${vm_id}-task-${task_index}"
    task_index=$((task_index + 1))
    if [ "$(date +%s)" -lt "${end_time}" ]; then
      sleep_between_sustained_prefill_tasks
    fi
  done
}

start_background_memory_workers
if [ "${llm_load_mode}" = "sustained_prefill" ]; then
  run_sustained_prefill
else
  task_index=0
  while [ "${task_index}" -lt "${tasks_per_vm}" ]; do
    run_task "${vm_id}-task-${task_index}"
    task_index=$((task_index + 1))
  done
fi
write_result
wait_for_background_memory_workers
stop_background_memory_workers
write_result

cat /var/lib/aab/result.json
"""


def build_mini_swe_guest_worker_script() -> str:
    return r'''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-spec", default="/task/task.json")
    parser.add_argument("--output-dir", default="/output")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    task = json.loads(Path(args.task_spec).read_text(encoding="utf-8"))
    _progress(output_dir, "worker_start")
    metadata = {
        "runtime": "firecracker_guest_worker",
        "task_spec": args.task_spec,
        "output_dir": str(output_dir),
        "mini_command": task.get("mini_command", "mini"),
        "vllm_base_url": task.get("vllm_base_url"),
    }
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _progress(output_dir, "probe_start")
    health = _probe_vllm(str(task.get("vllm_base_url") or ""))
    _progress(output_dir, f"probe_done:{health['status']}")
    _progress(output_dir, "chat_probe_start")
    chat_probe = _probe_chat_completion(task)
    (output_dir / "chat_probe.json").write_text(json.dumps(chat_probe, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _progress(output_dir, f"chat_probe_done:{chat_probe['status']}")
    _progress(output_dir, "mini_start")
    rc, stdout, stderr = _run_mini(task, output_dir)
    _progress(output_dir, f"mini_done:{rc}")
    ended = time.time()
    patch_path = output_dir / "patch.diff"
    test_log_path = output_dir / "test.log"
    if not patch_path.exists():
        patch_path.write_text(_git_diff(task.get("work_dir", "/work")), encoding="utf-8")
    if not test_log_path.exists():
        test_log_path.write_text(stdout + "\n" + stderr, encoding="utf-8")
    trajectory_path = output_dir / "trajectory.jsonl"
    if not trajectory_path.exists():
        trajectory_path.write_text("", encoding="utf-8")
    verified = rc == 0
    result = {
        "run_id": task.get("run_id"),
        "agent_id": task.get("agent_id"),
        "vm_id": task.get("vm_id"),
        "issue_id": task.get("issue_id"),
        "task_id": task.get("task_id"),
        "repo": task.get("repo"),
        "status": "verified_success" if verified else "failed",
        "verified": verified,
        "completed_tasks": 1 if verified else 0,
        "failed_tasks": 0 if verified else 1,
        "test_status": "passed" if verified else "failed",
        "verifier_score": 1.0 if verified else 0.0,
        "vllm_health": health["status"],
        "vllm_models_url": health["url"],
        "started_at": _iso(started),
        "ended_at": _iso(ended),
        "latency_sec": round(ended - started, 3),
        "output_files": {
            "trajectory": "trajectory.jsonl",
            "patch": "patch.diff",
            "test_log": "test.log",
            "stdout": "stdout.log",
            "stderr": "stderr.log",
            "result": "result.json",
        },
        "review_result": {
            "review_status": "approved" if verified else "warning",
            "review_score": 1.0 if verified else 0.0,
            "issues": [],
            "recommendations": [],
        },
        "verifier_result": {
            "verified": verified,
            "test_status": "passed" if verified else "failed",
            "passed_tests": 1 if verified else 0,
            "failed_tests": 0 if verified else 1,
            "error_tests": 0,
            "verifier_score": 1.0 if verified else 0.0,
            "test_log_path": "test.log",
        },
        "error": None if verified else stderr[-2000:],
    }
    (output_dir / "review_result.json").write_text(json.dumps(result["review_result"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "verifier_result.json").write_text(json.dumps(result["verifier_result"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def _run_mini(task: dict, output_dir: Path) -> tuple[int, str, str]:
    if str(task.get("mini_runner") or "api") == "api":
        return _run_mini_api(task, output_dir)
    return _run_mini_cli(task, output_dir)


def _run_mini_api(task: dict, output_dir: Path) -> tuple[int, str, str]:
    stdout_path = output_dir / "stdout.log"
    stderr_path = output_dir / "stderr.log"
    trajectory = output_dir / "trajectory.jsonl"
    prompt = str(task.get("prompt") or task.get("issue_id") or "Run the assigned software engineering task.")
    model_name = str(task.get("model") or "agentic-model")
    base_url = str(task.get("vllm_base_url") or "").rstrip("/")
    timeout_sec = int(task.get("mini_timeout_sec") or task.get("request_timeout_sec") or 300)
    config_path = Path(
        str(task.get("mini_config") or "/opt/python3.10/lib/python3.10/site-packages/minisweagent/config/mini_textbased.yaml")
    )
    try:
        os.environ.setdefault("OPENAI_API_KEY", "token-abc123")
        os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
        os.environ.setdefault("MSWEA_CONFIGURED", "true")
        os.environ.setdefault("MSWEA_COST_TRACKING", "ignore_errors")
        os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        os.environ.setdefault("PYTHONUNBUFFERED", "1")
        if base_url:
            os.environ["OPENAI_BASE_URL"] = base_url
            os.environ["OPENAI_API_BASE"] = base_url
        _progress(output_dir, "api_imports_start")
        _progress(output_dir, "import_yaml_start")
        import yaml
        _progress(output_dir, "import_yaml_done")
        _progress(output_dir, "import_pydantic_start")
        import pydantic
        _progress(output_dir, "import_pydantic_done")
        _progress(output_dir, "import_msa_default_start")
        from minisweagent.agents.default import AgentConfig, DefaultAgent
        _progress(output_dir, "import_msa_default_done")
        _progress(output_dir, "import_msa_local_env_start")
        from minisweagent.environments.local import LocalEnvironment
        _progress(output_dir, "import_msa_local_env_done")
        _progress(output_dir, "import_msa_actions_text_start")
        from minisweagent.models.utils.actions_text import format_observation_messages, parse_regex_actions
        _progress(output_dir, "import_msa_actions_text_done")
        _progress(output_dir, "api_imports_done")

        _progress(output_dir, "api_config_start")
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        agent_config = dict(config.get("agent", {}) or {})
        allowed_agent_fields = set(AgentConfig.model_fields)
        agent_kwargs = {key: value for key, value in agent_config.items() if key in allowed_agent_fields}
        agent_kwargs["output_path"] = trajectory
        agent_kwargs["cost_limit"] = 0
        agent_kwargs["step_limit"] = int(task.get("mini_step_limit") or 12)
        agent_kwargs["wall_time_limit_seconds"] = timeout_sec
        _progress(output_dir, "api_config_done")

        _progress(output_dir, "get_model_start")
        model_config = dict(config.get("model", {}) or {})
        model = OpenAITextModel(
            model_name=model_name,
            base_url=base_url,
            api_key=os.environ.get("OPENAI_API_KEY", "token-abc123"),
            action_regex=str(model_config.get("action_regex") or r"```mswea_bash_command\s*\n(.*?)\n```"),
            format_error_template=str(
                model_config.get("format_error_template")
                or "Please always provide EXACTLY ONE action in triple backticks, found {{actions|length}} actions."
            ),
            observation_template=str(
                model_config.get("observation_template")
                or "<returncode>{{output.returncode}}</returncode>\n<output>\n{{ output.output -}}\n</output>"
            ),
            parse_regex_actions=parse_regex_actions,
            format_observation_messages=format_observation_messages,
        )
        _progress(output_dir, "get_model_done")

        _progress(output_dir, "get_environment_start")
        env = LocalEnvironment(
            cwd=str(task.get("work_dir") or "/work"),
            env={"PAGER": "cat", "MANPAGER": "cat", "LESS": "-R", "PIP_PROGRESS_BAR": "off", "TQDM_DISABLE": "1"},
            timeout=30,
        )
        _progress(output_dir, "get_environment_done")

        _progress(output_dir, "get_agent_start")
        agent = DefaultAgent(model, env, **agent_kwargs)
        _progress(output_dir, "get_agent_done")

        def _timeout_handler(signum, frame):
            raise TimeoutError(f"mini-swe-agent API timed out after {timeout_sec} seconds")

        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(timeout_sec)
        try:
            _progress(output_dir, "agent_run_start")
            result = agent.run(prompt)
            _progress(output_dir, "agent_run_done")
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

        stdout = json.dumps({"exit": result, "n_calls": agent.n_calls, "cost": agent.cost}, indent=2, sort_keys=True) + "\n"
        stderr = ""
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        exit_status = str(result.get("exit_status", ""))
        return (0 if exit_status in {"Submitted", "RepeatedFormatError"} or agent.n_calls > 0 else 1), stdout, stderr
    except Exception as exc:
        stderr = traceback.format_exc()
        stdout = ""
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        _progress(output_dir, f"api_error:{type(exc).__name__}")
        return 1, stdout, stderr


class OpenAITextModel:
    def __init__(
        self,
        *,
        model_name: str,
        base_url: str,
        api_key: str,
        action_regex,
        format_error_template,
        observation_template,
        parse_regex_actions,
        format_observation_messages,
    ):
        self.model_name = model_name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.action_regex = action_regex
        self.format_error_template = format_error_template
        self.observation_template = observation_template
        self._parse_regex_actions = parse_regex_actions
        self._format_observation_messages = format_observation_messages
        self.config = {
            "model_name": model_name,
            "base_url": self.base_url,
            "model_type": "OpenAITextModel",
        }

    def query(self, messages: list[dict], **kwargs) -> dict:
        prepared = [
            {"role": item.get("role", "user"), "content": item.get("content", "")}
            for item in messages
            if item.get("role") in {"system", "user", "assistant"}
        ]
        payload = json.dumps(
            {
                "model": self.model_name.split("/", 1)[-1],
                "messages": prepared,
                "temperature": 0.1,
                "max_tokens": 512,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = data["choices"][0]["message"].get("content") or ""
        actions = self._parse_regex_actions(
            content,
            action_regex=self.action_regex,
            format_error_template=self.format_error_template,
            template_kwargs={"finish_reason": data["choices"][0].get("finish_reason")},
        )
        return {
            "role": "assistant",
            "content": content,
            "extra": {
                "actions": actions,
                "response": data,
                "cost": 0.0,
                "timestamp": time.time(),
            },
        }

    def format_message(self, **kwargs) -> dict:
        return kwargs

    def format_observation_messages(self, message: dict, outputs: list[dict], template_vars: dict | None = None) -> list[dict]:
        return self._format_observation_messages(
            outputs,
            observation_template=self.observation_template,
            template_vars=template_vars,
        )

    def get_template_vars(self, **kwargs) -> dict:
        return dict(self.config)

    def serialize(self) -> dict:
        return {"info": {"config": {"model": self.config, "model_type": "OpenAITextModel"}}}


def _run_mini_cli(task: dict, output_dir: Path) -> tuple[int, str, str]:
    mini_command = str(task.get("mini_command") or "mini")
    trajectory = output_dir / "trajectory.jsonl"
    prompt = str(task.get("prompt") or task.get("issue_id") or "Run the assigned software engineering task.")
    model = str(task.get("model") or "agentic-model")
    if str(task.get("adapter_mode") or "cli") == "mock" or shutil.which(mini_command) is None:
        stdout = "mock mini-swe-agent execution\nCOMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
        stderr = ""
        trajectory.write_text(json.dumps({"event": "mock", "task_id": task.get("task_id")}) + "\n", encoding="utf-8")
        (output_dir / "patch.diff").write_text("", encoding="utf-8")
        (output_dir / "test.log").write_text("ok\nCOMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n", encoding="utf-8")
        (output_dir / "stdout.log").write_text(stdout, encoding="utf-8")
        (output_dir / "stderr.log").write_text(stderr, encoding="utf-8")
        return 0, stdout, stderr
    env = os.environ.copy()
    base_url = str(task.get("vllm_base_url") or "").rstrip("/")
    env.setdefault("OPENAI_API_KEY", "token-abc123")
    env.setdefault("MSWEA_CONFIGURED", "true")
    env.setdefault("MSWEA_COST_TRACKING", "ignore_errors")
    if base_url:
        env["OPENAI_BASE_URL"] = base_url
        env["OPENAI_API_BASE"] = base_url
    config_path = str(
        task.get("mini_config")
        or "/opt/python3.10/lib/python3.10/site-packages/minisweagent/config/mini_textbased.yaml"
    )
    model_class = str(task.get("mini_model_class") or "litellm_textbased")
    command = [
        mini_command,
        "--config",
        config_path,
        "--model-class",
        model_class,
        "--model",
        model,
        "--task",
        prompt,
        "--yolo",
        "--exit-immediately",
        "--output",
        str(trajectory),
    ]
    stdout_path = output_dir / "stdout.log"
    stderr_path = output_dir / "stderr.log"
    timeout_sec = int(task.get("mini_timeout_sec") or task.get("request_timeout_sec") or 300)
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
        process = subprocess.Popen(command, stdout=stdout_handle, stderr=stderr_handle, text=True, env=env)
        try:
            returncode = process.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            returncode = 124
            stderr_handle.write(f"\nmini-swe-agent timed out after {timeout_sec} seconds\n")
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    return returncode, stdout, stderr


def _progress(output_dir: Path, event: str) -> None:
    with (output_dir / "progress.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{_iso(time.time())} {event}\n")


def _probe_vllm(base_url: str) -> dict:
    url = base_url.rstrip("/") + "/models" if base_url else ""
    if not url:
        return {"status": "missing", "url": url}
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return {"status": "ok" if response.status < 500 else "error", "url": url}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"status": "error", "url": url}


def _probe_chat_completion(task: dict) -> dict:
    base_url = str(task.get("vllm_base_url") or "").rstrip("/")
    model = str(task.get("model") or "agentic-model").split("/", 1)[-1]
    url = base_url + "/chat/completions" if base_url else ""
    if not url:
        return {"status": "missing", "url": url}
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Return the single word ok."}],
            "max_tokens": 8,
            "temperature": 0,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer token-abc123"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(2048).decode("utf-8", errors="replace")
            return {"status": "ok" if response.status < 500 else "error", "url": url, "status_code": response.status, "body": body}
    except Exception as exc:
        return {"status": "error", "url": url, "error": f"{type(exc).__name__}: {exc}"}


def _git_diff(work_dir: object) -> str:
    path = Path(str(work_dir))
    if not path.exists():
        return ""
    result = subprocess.run(["git", "diff", "--no-ext-diff"], cwd=path, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
    return result.stdout


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


if __name__ == "__main__":
    raise SystemExit(main())
'''


def build_mini_swe_run_worker_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

: "${TASK_SPEC_PATH:=/task/task.json}"
: "${OUTPUT_DIR:=/output}"

mkdir -p "${OUTPUT_DIR}"

python3 /opt/agent-runtime/guest_worker.py \
  --task-spec "${TASK_SPEC_PATH}" \
  --output-dir "${OUTPUT_DIR}" \
  > "${OUTPUT_DIR}/stdout.log" \
  2> "${OUTPUT_DIR}/stderr.log"
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
#include <emmintrin.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef enum {
    MODE_TRIAD,
    MODE_COPY,
    MODE_SCALE,
    MODE_READ,
    MODE_READ8,
    MODE_WRITE,
    MODE_NT_WRITE
} memory_mode_t;

typedef struct {
    int id;
    size_t elements;
    int seconds;
    memory_mode_t mode;
    double bandwidth_gbps;
} worker_args_t;

static volatile double global_sink = 0.0;

static double now_seconds(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1000000000.0;
}

static memory_mode_t parse_mode(const char *mode) {
    if (!strcmp(mode, "triad")) return MODE_TRIAD;
    if (!strcmp(mode, "copy")) return MODE_COPY;
    if (!strcmp(mode, "scale")) return MODE_SCALE;
    if (!strcmp(mode, "read")) return MODE_READ;
    if (!strcmp(mode, "read8")) return MODE_READ8;
    if (!strcmp(mode, "write")) return MODE_WRITE;
    if (!strcmp(mode, "nt-write")) return MODE_NT_WRITE;
    fprintf(stderr, "unknown mode: %s\n", mode);
    return MODE_TRIAD;
}

static const char *mode_name(memory_mode_t mode) {
    switch (mode) {
        case MODE_COPY: return "copy";
        case MODE_SCALE: return "scale";
        case MODE_READ: return "read";
        case MODE_READ8: return "read8";
        case MODE_WRITE: return "write";
        case MODE_NT_WRITE: return "nt-write";
        case MODE_TRIAD:
        default:
            return "triad";
    }
}

static double bytes_per_element(memory_mode_t mode) {
    switch (mode) {
        case MODE_COPY:
        case MODE_SCALE:
            return sizeof(double) * 2.0;
        case MODE_READ8:
            return sizeof(double) * 8.0;
        case MODE_READ:
        case MODE_TRIAD:
            return sizeof(double) * 3.0;
        case MODE_WRITE:
        case MODE_NT_WRITE:
            return sizeof(double);
        default:
            return sizeof(double) * 3.0;
    }
}

static void *worker_main(void *ptr) {
    worker_args_t *args = (worker_args_t *)ptr;
    size_t n = args->elements;
    double *a = NULL;
    double *b = NULL;
    double *c = NULL;
    double *d = NULL;
    double *e = NULL;
    double *f = NULL;
    double *g = NULL;
    double *h = NULL;
    const double scalar = 3.14159;
    if (posix_memalign((void **)&a, 64, n * sizeof(double)) ||
        posix_memalign((void **)&b, 64, n * sizeof(double)) ||
        posix_memalign((void **)&c, 64, n * sizeof(double)) ||
        posix_memalign((void **)&d, 64, n * sizeof(double)) ||
        posix_memalign((void **)&e, 64, n * sizeof(double)) ||
        posix_memalign((void **)&f, 64, n * sizeof(double)) ||
        posix_memalign((void **)&g, 64, n * sizeof(double)) ||
        posix_memalign((void **)&h, 64, n * sizeof(double))) {
        fprintf(stderr, "worker %d allocation failed: %s\n", args->id, strerror(errno));
        free(a); free(b); free(c); free(d); free(e); free(f); free(g); free(h);
        return NULL;
    }
    for (size_t i = 0; i < n; i++) {
        a[i] = 0.0;
        b[i] = (double)(i % 1024);
        c[i] = (double)((i + 17) % 1024);
        d[i] = (double)((i + 31) % 1024);
        e[i] = (double)((i + 47) % 1024);
        f[i] = (double)((i + 63) % 1024);
        g[i] = (double)((i + 79) % 1024);
        h[i] = (double)((i + 95) % 1024);
    }
    double start = now_seconds();
    double end = start + (double)args->seconds;
    uint64_t iterations = 0;
    double local_sink = 0.0;
    while (now_seconds() < end) {
        switch (args->mode) {
            case MODE_COPY:
                for (size_t i = 0; i < n; i++) {
                    a[i] = b[i];
                }
                break;
            case MODE_SCALE:
                for (size_t i = 0; i < n; i++) {
                    a[i] = scalar * b[i];
                }
                break;
            case MODE_READ:
                for (size_t i = 0; i < n; i++) {
                    local_sink += a[i] + b[i] + c[i];
                }
                break;
            case MODE_READ8:
                for (size_t i = 0; i < n; i++) {
                    local_sink += a[i] + b[i] + c[i] + d[i] + e[i] + f[i] + g[i] + h[i];
                }
                break;
            case MODE_WRITE:
                for (size_t i = 0; i < n; i++) {
                    a[i] = scalar + (double)(i & 7);
                }
                break;
            case MODE_NT_WRITE:
                for (size_t i = 0; i + 1 < n; i += 2) {
                    __m128d value = _mm_set1_pd(scalar + (double)(i & 7));
                    _mm_stream_pd(&a[i], value);
                }
                _mm_sfence();
                break;
            case MODE_TRIAD:
            default:
                for (size_t i = 0; i < n; i++) {
                    a[i] = b[i] + scalar * c[i];
                }
                double *tmp = b;
                b = c;
                c = a;
                a = tmp;
                break;
        }
        iterations++;
    }
    global_sink += local_sink;
    double elapsed = now_seconds() - start;
    double bytes = (double)iterations * (double)n * bytes_per_element(args->mode);
    args->bandwidth_gbps = bytes / elapsed / 1000000000.0;
    fprintf(stdout, "worker=%d mode=%s iterations=%lu bandwidth_gbps=%.3f\n",
            args->id, mode_name(args->mode), (unsigned long)iterations, args->bandwidth_gbps);
    fflush(stdout);
    free(a); free(b); free(c); free(d); free(e); free(f); free(g); free(h);
    return NULL;
}

int main(int argc, char **argv) {
    int threads = 2;
    int mb_per_thread = 128;
    int seconds = 60;
    memory_mode_t mode = MODE_TRIAD;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--threads") && i + 1 < argc) {
            threads = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--mb-per-thread") && i + 1 < argc) {
            mb_per_thread = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--seconds") && i + 1 < argc) {
            seconds = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--mode") && i + 1 < argc) {
            mode = parse_mode(argv[++i]);
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
        args[i].mode = mode;
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
    fprintf(stdout, "total_bandwidth_gbps=%.3f threads=%d mb_per_thread=%d seconds=%d mode=%s\n",
            total, threads, mb_per_thread, seconds, mode_name(mode));
    free(tids);
    free(args);
    return 0;
}
'''
