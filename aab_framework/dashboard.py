from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .agent_team import ChallengeAgent, PluginRegistry
from .executors.firecracker import build_firecracker_executor_spec
from .metrics import summarize_firecracker_sweep
from .workloads.coding import build_coding_workload_spec
from .workloads.mini_swe_agent_team_v2 import build_mini_swe_agent_team_v2_workload_spec
from .team_v2.sweep import SWEEP_WORKLOAD_TYPE


MAX_AGENT_COUNT = 512
STATIC_DIR = Path(__file__).with_name("dashboard_static")
SUPPORTED_DASHBOARD_COMBO = ("coding", "firecracker")
TEAM_V2_WORKLOAD = "mini_swe_agent_team_v2"
VLLM_MODELS_URL = "http://127.0.0.1:8000/v1/models"


@dataclass(frozen=True)
class SweepLaunchConfig:
    agents: list[int]
    run_seconds: int = 180
    workload_grace_seconds: int = 60
    memory_workers: int = 8
    memory_mb: int = 256
    memory_rounds: int = 16
    vcpus_per_agent: int = 8
    llm_context_kb: int = 2
    llm_prompt_repeat: int = 1
    llm_max_tokens: int = 512
    llm_load_mode: str = "single_task"
    llm_request_timeout_seconds: int = 120
    llm_inter_task_sleep_ms: int = 0
    sudo_password: str = ""
    run_name: str = "manual"
    workload: str = "coding"
    executor: str = "firecracker"


def parse_agents_list(value: str) -> list[int]:
    if not value.strip():
        raise ValueError("agents list is required")
    if not re.fullmatch(r"[0-9,\s]+", value):
        raise ValueError("agents list may only contain numbers, commas, and whitespace")
    seen: set[int] = set()
    agents: list[int] = []
    for item in re.split(r"[\s,]+", value.strip()):
        if not item:
            continue
        agent_count = int(item)
        if agent_count < 1:
            raise ValueError("agent counts must be positive")
        if agent_count > MAX_AGENT_COUNT:
            raise ValueError(f"agent counts must be <= {MAX_AGENT_COUNT}")
        if agent_count not in seen:
            seen.add(agent_count)
            agents.append(agent_count)
    if not agents:
        raise ValueError("agents list is required")
    return agents


def build_sweep_environment(config: SweepLaunchConfig, *, timestamp: str | None = None) -> dict[str, str]:
    _validate_dashboard_combo(config.workload, config.executor)
    label = _sanitize_label(config.run_name)
    stamp = timestamp or time.strftime("%Y%m%d-%H%M%S")
    return {
        "AGENTS_LIST": " ".join(str(item) for item in config.agents),
        "RUN_SECONDS": str(_positive_int(config.run_seconds, "run_seconds")),
        "WORKLOAD_GRACE_SECONDS": str(
            _non_negative_int(config.workload_grace_seconds, "workload_grace_seconds")
        ),
        "AAB_MEMORY_WORKERS": str(_positive_int(config.memory_workers, "memory_workers")),
        "AAB_MEMORY_MB": str(_positive_int(config.memory_mb, "memory_mb")),
        "AAB_MEMORY_ROUNDS": str(_positive_int(config.memory_rounds, "memory_rounds")),
        "AAB_VCPUS_PER_AGENT": str(_positive_int(config.vcpus_per_agent, "vcpus_per_agent")),
        "AAB_LLM_CONTEXT_KB": str(_non_negative_int(config.llm_context_kb, "llm_context_kb")),
        "AAB_LLM_PROMPT_REPEAT": str(_positive_int(config.llm_prompt_repeat, "llm_prompt_repeat")),
        "AAB_LLM_MAX_TOKENS": str(_positive_int(config.llm_max_tokens, "llm_max_tokens")),
        "AAB_LLM_LOAD_MODE": _validate_llm_load_mode(config.llm_load_mode),
        "AAB_LLM_REQUEST_TIMEOUT_SECONDS": str(
            _positive_int(config.llm_request_timeout_seconds, "llm_request_timeout_seconds")
        ),
        "AAB_LLM_INTER_TASK_SLEEP_MS": str(
            _non_negative_int(config.llm_inter_task_sleep_ms, "llm_inter_task_sleep_ms")
        ),
        "AAB_WORKLOAD": config.workload,
        "AAB_EXECUTOR": config.executor,
        "SUDO_PASSWORD": config.sudo_password,
        "SWEEP_ROOT": f"runs/dashboard-{label}-{stamp}",
    }


def dashboard_plugins_payload() -> dict[str, Any]:
    registry = PluginRegistry()
    workload = build_coding_workload_spec()
    team_workload = build_mini_swe_agent_team_v2_workload_spec()
    executor = build_firecracker_executor_spec()
    registry.register_workload(workload)
    registry.register_workload(team_workload)
    registry.register_executor(executor)
    challenge = ChallengeAgent()
    return {
        "workloads": [
            {
                "name": item.name,
                "description": item.description,
                "team_name": item.default_team.name,
                "challenge_role": item.default_team.challenge_role,
                "roles": list(item.default_team.role_names),
            }
            for item in (registry.workload(name) for name in registry.workload_names())
        ],
        "executors": [
            {
                "name": item.name,
                "isolation": item.isolation,
                "supports_cpu_pinning": item.supports_cpu_pinning,
                "supports_numa_binding": item.supports_numa_binding,
            }
            for item in (registry.executor(name) for name in registry.executor_names())
        ],
        "supported_combinations": [
            {"workload": SUPPORTED_DASHBOARD_COMBO[0], "executor": SUPPORTED_DASHBOARD_COMBO[1]},
            {"workload": TEAM_V2_WORKLOAD, "executor": "process"},
        ],
        "challenge_reviews": {
            "workloads": {
                workload.name: _review_to_dict(challenge.review_workload(workload)),
                team_workload.name: _review_to_dict(challenge.review_workload(team_workload)),
            },
            "executors": {executor.name: _review_to_dict(challenge.review_executor(executor))},
        },
    }


def list_runs(runs_dir: str | Path) -> list[dict[str, Any]]:
    root = Path(runs_dir)
    if not root.exists():
        return []
    runs = []
    for path in sorted(root.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not path.is_dir():
            continue
        if not _looks_like_run(path):
            continue
        metadata = _read_run_metadata(path)
        runs.append(
            {
                "name": path.name,
                "display_name": metadata.get("display_name") or path.name,
                "path": str(path),
                "mtime": path.stat().st_mtime,
                "has_aligned_metrics": (path / "aligned_metrics.csv").exists(),
                "has_sweep_summary": (path / "sweep_summary.csv").exists(),
                "has_team_result": _is_team_v2_run(path) or _is_team_v2_sweep_run(path),
            }
        )
    return runs


def load_run_report(run_dir: str | Path) -> dict[str, Any]:
    run_path = Path(run_dir)
    if _is_team_v2_sweep_run(run_path):
        return _load_team_v2_sweep_report(run_path)
    if _is_team_v2_run(run_path):
        return _load_team_v2_run_report(run_path)
    ensure_aligned_metrics(run_path)
    metadata = _read_run_metadata(run_path)
    aligned_rows = _read_csv_rows(run_path / "aligned_metrics.csv")
    sweep_rows = _read_csv_rows(run_path / "sweep_summary.csv")
    point_rows = aligned_rows or _summary_rows_as_points(sweep_rows)
    points = [_build_point(run_path, row) for row in point_rows]
    points.sort(key=lambda item: item["agents"])
    return {
        "name": run_path.name,
        "display_name": metadata.get("display_name") or run_path.name,
        "metadata": metadata,
        "path": str(run_path),
        "mtime": run_path.stat().st_mtime if run_path.exists() else None,
        "overview": _build_overview(points),
        "points": points,
        "files": {
            "aligned_metrics_csv": (run_path / "aligned_metrics.csv").exists(),
            "aligned_metrics_json": (run_path / "aligned_metrics.json").exists(),
            "aligned_metrics_md": (run_path / "aligned_metrics.md").exists(),
            "sweep_summary_csv": (run_path / "sweep_summary.csv").exists(),
        },
    }


def ensure_aligned_metrics(run_dir: str | Path) -> bool:
    run_path = Path(run_dir)
    if (run_path / "aligned_metrics.csv").exists():
        return False
    if not (run_path / "sweep_summary.csv").exists():
        return False
    metadata = _read_run_metadata(run_path)
    run_seconds = float(metadata.get("run_seconds") or 180)
    grace_seconds = float(metadata.get("workload_grace_seconds") or 60)
    workload_seconds = max(run_seconds - grace_seconds, 1)
    summarize_firecracker_sweep(
        run_path,
        run_seconds=run_seconds,
        workload_seconds=workload_seconds,
    )
    return True


def tail_file(path: str | Path, *, max_lines: int = 250) -> dict[str, Any]:
    target = Path(path)
    if max_lines < 1:
        max_lines = 1
    if max_lines > 2000:
        max_lines = 2000
    if not target.exists() or not target.is_file():
        return {"path": str(target), "exists": False, "lines": []}
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"path": str(target), "exists": True, "lines": lines[-max_lines:]}


def serve(project_root: str | Path, *, host: str, port: int) -> None:
    root = Path(project_root).resolve()
    state = DashboardState(root)
    handler = _make_handler(state)
    server = ThreadingHTTPServer((host, port), handler)
    print(json.dumps({"url": f"http://{host}:{port}", "project_root": str(root)}, sort_keys=True))
    server.serve_forever()


def serve_with_fallback(
    project_root: str | Path,
    *,
    host: str,
    port: int,
    fallback_port: int,
    enable_fallback: bool,
) -> dict[str, Any]:
    try:
        serve(project_root, host=host, port=port)
    except OSError as exc:
        if not enable_fallback:
            raise
        reason = str(exc)
        if port == 80 and "permission" in reason.lower():
            reason = "Port 80 requires root permission"
            print(f"Port 80 requires root. Falling back to {fallback_port}.")
        else:
            print(f"Port {port} is unavailable ({exc}). Falling back to {fallback_port}.")
        serve(project_root, host=host, port=fallback_port)
        return {
            "ui_host": host,
            "requested_ui_port": port,
            "actual_ui_port": fallback_port,
            "port_fallback_used": True,
            "fallback_reason": reason,
        }
    return {
        "ui_host": host,
        "requested_ui_port": port,
        "actual_ui_port": port,
        "port_fallback_used": False,
        "fallback_reason": None,
    }


class DashboardState:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.current_sweep_root: str | None = None
        self.current_stdout_log: str | None = None
        self.started_at: float | None = None

    @property
    def runs_dir(self) -> Path:
        return self.project_root / "runs"

    def status(self) -> dict[str, Any]:
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            return {
                "running": running,
                "returncode": None if self.process is None else self.process.poll(),
                "sweep_root": self.current_sweep_root,
                "stdout_log": self.current_stdout_log,
                "started_at": self.started_at,
            }

    def start_sweep(self, config: SweepLaunchConfig) -> dict[str, Any]:
        require_vllm_ready()
        env_updates = build_sweep_environment(config)
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise RuntimeError("a sweep is already running")
            sweep_root = self.project_root / env_updates["SWEEP_ROOT"]
            sweep_root.mkdir(parents=True, exist_ok=True)
            _write_run_metadata(sweep_root, config, env_updates)
            stdout_log = sweep_root / "dashboard-sweep.log"
            env = os.environ.copy()
            env.update(env_updates)
            script = self.project_root / "bin" / "run_coding_firecracker_sweep.sh"
            handle = stdout_log.open("w", encoding="utf-8")
            process = subprocess.Popen(
                ["bash", str(script)],
                cwd=self.project_root,
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                preexec_fn=os.setsid,
            )
            handle.close()
            self.process = process
            self.current_sweep_root = env_updates["SWEEP_ROOT"]
            self.current_stdout_log = str(stdout_log)
            self.started_at = time.time()
        return self.status()

    def stop_sweep(self) -> dict[str, Any]:
        with self.lock:
            process = self.process
            sweep_root = self.current_sweep_root
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        if sweep_root:
            subprocess.run(
                ["pkill", "-TERM", "-f", sweep_root],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        return self.status()


def _make_handler(state: DashboardState) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path in {"/", "/index.html"}:
                    self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
                elif parsed.path == "/app.js":
                    self._send_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
                elif parsed.path == "/style.css":
                    self._send_file(STATIC_DIR / "style.css", "text/css; charset=utf-8")
                elif parsed.path == "/api/runs":
                    self._send_json({"runs": list_runs(state.runs_dir), "status": state.status()})
                elif parsed.path == "/api/plugins":
                    self._send_json(dashboard_plugins_payload())
                elif parsed.path == "/api/status":
                    self._send_json({"status": state.status(), "vllm": _probe_vllm()})
                elif parsed.path == "/api/run":
                    query = parse_qs(parsed.query)
                    self._send_json(load_run_report(_resolve_run(state, query.get("name", ["latest"])[0])))
                elif parsed.path == "/api/log":
                    query = parse_qs(parsed.query)
                    self._send_json(tail_file(_resolve_log(state, query), max_lines=_query_int(query, "lines", 250)))
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/sweep":
                    payload = self._read_json_body()
                    config = SweepLaunchConfig(
                        agents=parse_agents_list(str(payload.get("agents", ""))),
                        run_seconds=int(payload.get("run_seconds", 180)),
                        workload_grace_seconds=int(payload.get("workload_grace_seconds", 60)),
                        memory_workers=int(payload.get("memory_workers", 8)),
                        memory_mb=int(payload.get("memory_mb", 256)),
                        memory_rounds=int(payload.get("memory_rounds", 16)),
                        vcpus_per_agent=int(payload.get("vcpus_per_agent", 8)),
                        llm_context_kb=int(payload.get("llm_context_kb", 2)),
                        llm_prompt_repeat=int(payload.get("llm_prompt_repeat", 1)),
                        llm_max_tokens=int(payload.get("llm_max_tokens", 512)),
                        llm_load_mode=str(payload.get("llm_load_mode") or "single_task"),
                        llm_request_timeout_seconds=int(payload.get("llm_request_timeout_seconds", 120)),
                        llm_inter_task_sleep_ms=int(payload.get("llm_inter_task_sleep_ms", 0)),
                        sudo_password=str(
                            payload.get("sudo_password")
                            or os.environ.get("AAB_DASHBOARD_SUDO_PASSWORD", "")
                        ),
                        run_name=str(payload.get("run_name") or payload.get("run_label") or "manual"),
                        workload=str(payload.get("workload") or "coding"),
                        executor=str(payload.get("executor") or "firecracker"),
                    )
                    self._send_json({"status": state.start_sweep(config)}, status=HTTPStatus.ACCEPTED)
                elif parsed.path == "/api/sweep/stop":
                    self._send_json({"status": state.stop_sweep()})
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def log_message(self, format: str, *args: object) -> None:
            print(f"{self.address_string()} - {format % args}")

        def _send_file(self, path: Path, content_type: str) -> None:
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

    return DashboardHandler


def _resolve_run(state: DashboardState, name: str) -> Path:
    if name == "latest":
        runs = list_runs(state.runs_dir)
        if runs:
            return Path(runs[0]["path"])
        latest_file = state.runs_dir / "latest_coding_firecracker_sweep_dir.txt"
        if latest_file.exists():
            value = latest_file.read_text(encoding="utf-8").strip()
            if value:
                path = state.project_root / value
                if path.exists():
                    return path
        raise ValueError("no runs found")
    safe_name = Path(name).name
    path = state.runs_dir / safe_name
    if not path.exists():
        raise ValueError(f"run not found: {safe_name}")
    return path


def _resolve_log(state: DashboardState, query: dict[str, list[str]]) -> Path:
    log_type = query.get("type", ["run"])[0]
    if log_type == "dashboard":
        status = state.status()
        if not status.get("stdout_log"):
            raise ValueError("no dashboard sweep log available")
        return Path(str(status["stdout_log"]))
    if log_type == "vllm":
        return state.runs_dir / "vllm-start-20260612-171551.log"
    run_path = _resolve_run(state, query.get("run", ["latest"])[0])
    if _is_team_v2_run(run_path):
        return run_path / "result.json"
    point = query.get("point", [""])[0]
    if point:
        return run_path / f"agents_{int(point)}" / "run.log"
    return run_path / "sweep_summary.csv"


def _query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int(query.get(key, [str(default)])[0])
    except ValueError:
        return default


def _probe_vllm() -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "curl",
                "-fsS",
                "--max-time",
                "2",
                VLLM_MODELS_URL,
                "-H",
                "Authorization: Bearer token-abc123",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stderr": result.stderr.strip(),
        }
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def require_vllm_ready() -> None:
    status = _probe_vllm()
    if status.get("ok"):
        return
    detail = str(status.get("stderr") or status.get("error") or f"curl return code {status.get('returncode')}")
    raise RuntimeError(f"vLLM is unavailable at {VLLM_MODELS_URL}; start vLLM before launching a sweep. {detail}")


def _build_point(run_path: Path, row: dict[str, str]) -> dict[str, Any]:
    agents = int(float(row.get("agents", "0") or 0))
    point_path = run_path / f"agents_{agents}"
    result_summary = _read_json(point_path / "results" / "summary.json")
    manifest = _read_json(point_path / "firecracker-run.json")
    failed_vm_ids = [
        item.get("vm_id", "")
        for item in result_summary.get("results", [])
        if int(item.get("failed_tasks", 0) or 0) > 0
    ]
    dram_bw_max_gbps = _number(row.get("dram_bw_max_gbps", 0))
    return {
        "agents": agents,
        "vm_results": _number(row.get("vm_results", result_summary.get("vm_results", 0))),
        "completed": _number(row.get("completed", row.get("completed_tasks", result_summary.get("completed_tasks", 0)))),
        "failed": _number(row.get("failed", row.get("failed_tasks", result_summary.get("failed_tasks", 0)))),
        "success_rate_pct": _number(row.get("success_rate_pct", 0)),
        "throughput_task_per_min_run": _number(row.get("throughput_task_per_min_run", 0)),
        "throughput_task_per_min_workload": _number(row.get("throughput_task_per_min_workload", 0)),
        "lat_all_p50_ms": _number(row.get("lat_all_p50_ms", 0)),
        "lat_all_p95_ms": _number(row.get("lat_all_p95_ms", 0)),
        "lat_ok_p50_ms": _number(row.get("lat_ok_p50_ms", 0)),
        "lat_ok_p95_ms": _number(row.get("lat_ok_p95_ms", 0)),
        "cpu_p50_pct": _number(row.get("cpu_p50_pct", 0)),
        "cpu_p95_pct": _number(row.get("cpu_p95_pct", 0)),
        "cpu_max_pct": _number(row.get("cpu_max_pct", row.get("cpu_p95_pct", 0))),
        "dram_bw_p50_gbps": _number(row.get("dram_bw_p50_gbps", 0)),
        "dram_bw_p95_gbps": _number(row.get("dram_bw_p95_gbps", 0)),
        "dram_bw_max_gbps": dram_bw_max_gbps,
        "dram_bw_max_pct_of_peak": _dram_peak_pct(row.get("dram_bw_max_pct_of_peak"), dram_bw_max_gbps),
        "gpu_util_p95_pct": _number(row.get("gpu_util_p95_pct", 0)),
        "gpu_util_max_pct": _number(row.get("gpu_util_max_pct", 0)),
        "gpu_active_sample_pct": _number(row.get("gpu_active_sample_pct", 0)),
        "gpu_memctrl_p95_pct": _number(row.get("gpu_memctrl_p95_pct", 0)),
        "gpu_memctrl_max_pct": _number(row.get("gpu_memctrl_max_pct", 0)),
        "gpu_memctrl_active_sample_pct": _number(row.get("gpu_memctrl_active_sample_pct", 0)),
        "gpu_power_p95_w": _number(row.get("gpu_power_p95_w", 0)),
        "gpu_mem_used_p95_mib": _number(row.get("gpu_mem_used_p95_mib", 0)),
        "gpu_mem_used_pct_p95": _number(row.get("gpu_mem_used_pct_p95", 0)),
        "gpu_mem_used_pct_max": _number(row.get("gpu_mem_used_pct_max", 0)),
        "gr_engine_active_p95_pct": _number(row.get("gr_engine_active_p95_pct", 0)),
        "gr_engine_active_max_pct": _number(row.get("gr_engine_active_max_pct", 0)),
        "sm_active_p95_pct": _number(row.get("sm_active_p95_pct", 0)),
        "sm_active_max_pct": _number(row.get("sm_active_max_pct", 0)),
        "sm_occupancy_p95_pct": _number(row.get("sm_occupancy_p95_pct", 0)),
        "sm_occupancy_max_pct": _number(row.get("sm_occupancy_max_pct", 0)),
        "tensor_active_p95_pct": _number(row.get("tensor_active_p95_pct", 0)),
        "tensor_active_max_pct": _number(row.get("tensor_active_max_pct", 0)),
        "dram_active_p95_pct": _number(row.get("dram_active_p95_pct", 0)),
        "dram_active_max_pct": _number(row.get("dram_active_max_pct", 0)),
        "fp16_active_p95_pct": _number(row.get("fp16_active_p95_pct", 0)),
        "fp16_active_max_pct": _number(row.get("fp16_active_max_pct", 0)),
        "fp32_active_p95_pct": _number(row.get("fp32_active_p95_pct", 0)),
        "fp32_active_max_pct": _number(row.get("fp32_active_max_pct", 0)),
        "dcgm_samples": _number(row.get("dcgm_samples", 0)),
        "vllm_ok": _number(row.get("vllm_ok", result_summary.get("vllm_ok", 0))),
        "failed_vm_ids": failed_vm_ids,
        "config": _point_config(manifest),
    }


def _load_team_v2_run_report(run_path: Path) -> dict[str, Any]:
    result = _read_json(run_path / "result.json")
    summary = result.get("summary", {})
    config = result.get("config", {})
    agents = result.get("agents", [])
    issues = result.get("issues", [])
    metrics = result.get("metrics_summary", {}) or {}
    point = {
        "agents": _number(config.get("num_agents", len(agents))),
        "vm_results": len(agents),
        "completed": _number(summary.get("completed_issues", 0)),
        "failed": _number(summary.get("failed_issues", 0)),
        "success_rate_pct": _number(summary.get("verified_success_rate", 0)),
        "throughput_task_per_min_run": _number(summary.get("candidate_per_min", 0)),
        "throughput_task_per_min_workload": _number(summary.get("candidate_per_min", 0)),
        "lat_all_p50_ms": _number(summary.get("issue_latency_p50_sec", 0)) * 1000,
        "lat_all_p95_ms": _number(summary.get("issue_latency_p95_sec", 0)) * 1000,
        "lat_ok_p50_ms": _number(summary.get("issue_latency_p50_sec", 0)) * 1000,
        "lat_ok_p95_ms": _number(summary.get("issue_latency_p95_sec", 0)) * 1000,
        "cpu_p50_pct": _metric(metrics, "cpu", "p50"),
        "cpu_p95_pct": _metric(metrics, "cpu", "p95"),
        "cpu_max_pct": _metric(metrics, "cpu", "max"),
        "dram_bw_p50_gbps": _metric(metrics, "dram_bw", "avg"),
        "dram_bw_p95_gbps": _metric(metrics, "dram_bw", "p95"),
        "dram_bw_max_gbps": _metric(metrics, "dram_bw", "max"),
        "dram_bw_max_pct_of_peak": _dram_peak_pct("", _metric(metrics, "dram_bw", "max")),
        "gpu_util_p95_pct": _metric(metrics, "gpu", "p95"),
        "gpu_util_max_pct": _metric(metrics, "gpu", "max"),
        "gpu_active_sample_pct": 0,
        "gpu_memctrl_p95_pct": 0,
        "gpu_memctrl_max_pct": 0,
        "gpu_memctrl_active_sample_pct": 0,
        "gpu_power_p95_w": 0,
        "gpu_mem_used_p95_mib": _metric(metrics, "gpu_memory", "p95"),
        "gpu_mem_used_pct_p95": 0,
        "gpu_mem_used_pct_max": 0,
        "sm_active_p95_pct": 0,
        "sm_active_max_pct": 0,
        "sm_occupancy_p95_pct": 0,
        "tensor_active_p95_pct": 0,
        "tensor_active_max_pct": 0,
        "dram_active_p95_pct": 0,
        "dram_active_max_pct": 0,
        "fp16_active_p95_pct": 0,
        "fp16_active_max_pct": 0,
        "fp32_active_p95_pct": 0,
        "fp32_active_max_pct": 0,
        "dcgm_samples": 0,
        "vllm_ok": 1 if result.get("vllm", {}).get("ok", True) else 0,
        "failed_vm_ids": [item.get("issue_id", "") for item in issues if not item.get("verified")],
        "config": {
            "vm_count": len(agents),
            "tasks_per_vm": 1,
            "request_workers": config.get("parallelism"),
            "workload_seconds": result.get("duration_sec"),
            "memory_workers": "-",
            "memory_mb": "-",
            "memory_rounds": "-",
            "memory_mode": "time_window",
            "llm_context_kb": int(config.get("context_length", 0) or 0) // 1024,
            "llm_prompt_repeat": 1,
            "llm_max_tokens": "-",
            "llm_load_mode": TEAM_V2_WORKLOAD,
            "llm_request_timeout_seconds": config.get("request_timeout_sec"),
            "llm_inter_task_sleep_ms": 0,
            "vcpu_count": "-",
            "mem_mib": "-",
        },
    }
    return {
        "name": run_path.name,
        "display_name": result.get("run_id") or run_path.name,
        "metadata": result.get("config", {}),
        "path": str(run_path),
        "mtime": run_path.stat().st_mtime if run_path.exists() else None,
        "overview": _build_overview([point]),
        "points": [point],
        "team": result.get("team", {}),
        "issues": issues,
        "agents": agents,
        "summary": summary,
        "files": {
            "result_json": True,
            "aligned_metrics_csv": False,
            "aligned_metrics_json": False,
            "aligned_metrics_md": False,
            "sweep_summary_csv": False,
        },
    }


def _load_team_v2_sweep_report(run_path: Path) -> dict[str, Any]:
    result = _read_json(run_path / "result.json")
    points = [_team_v2_sweep_point(item) for item in result.get("points", [])]
    return {
        "name": run_path.name,
        "display_name": result.get("run_id") or run_path.name,
        "metadata": result.get("config", {}),
        "path": str(run_path),
        "mtime": run_path.stat().st_mtime if run_path.exists() else None,
        "overview": _build_overview(points),
        "points": sorted(points, key=lambda item: item["agents"]),
        "team": result.get("team", {}),
        "issues": [],
        "agents": [],
        "summary": result.get("summary", {}),
        "files": {
            "result_json": True,
            "team_sweep_summary_csv": (run_path / "team_sweep_summary.csv").exists(),
            "aligned_metrics_csv": False,
            "aligned_metrics_json": False,
            "aligned_metrics_md": False,
            "sweep_summary_csv": False,
        },
    }


def _team_v2_sweep_point(point: dict[str, Any]) -> dict[str, Any]:
    metrics = point.get("metrics_summary", {}) or {}
    return {
        "agents": _number(point.get("agents", 0)),
        "case_id": point.get("case_id", ""),
        "context_length": _number(point.get("context_length", 0)),
        "experiment_mode": point.get("experiment_mode", ""),
        "repeat": _number(point.get("repeat", 1)),
        "max_active_llm_requests": _number(point.get("max_active_llm_requests", 0)),
        "max_active_prefill_tokens": _number(point.get("max_active_prefill_tokens", 0)),
        "vm_results": _number(point.get("total_issues", 0)),
        "completed": _number(point.get("total_issues", 0)) - _number(point.get("failed_issues", 0)),
        "failed": _number(point.get("failed_issues", 0)),
        "success_rate_pct": _number(point.get("verified_success_rate", 0)),
        "throughput_task_per_min_run": _number(point.get("candidate_per_min", 0)),
        "throughput_task_per_min_workload": _number(point.get("candidate_per_min", 0)),
        "lat_all_p50_ms": _number(point.get("issue_latency_p95_sec", 0)) * 1000,
        "lat_all_p95_ms": _number(point.get("issue_latency_p95_sec", 0)) * 1000,
        "lat_ok_p50_ms": _number(point.get("issue_latency_p95_sec", 0)) * 1000,
        "lat_ok_p95_ms": _number(point.get("issue_latency_p95_sec", 0)) * 1000,
        "cpu_p50_pct": _metric(metrics, "cpu", "p50"),
        "cpu_p95_pct": _metric(metrics, "cpu", "p95"),
        "cpu_max_pct": _metric(metrics, "cpu", "max"),
        "dram_bw_p50_gbps": _metric(metrics, "dram_bw", "avg"),
        "dram_bw_p95_gbps": _metric(metrics, "dram_bw", "p95"),
        "dram_bw_max_gbps": _metric(metrics, "dram_bw", "max"),
        "dram_bw_max_pct_of_peak": _dram_peak_pct("", _metric(metrics, "dram_bw", "max")),
        "gpu_util_p95_pct": _metric(metrics, "gpu", "p95"),
        "gpu_util_max_pct": _metric(metrics, "gpu", "max"),
        "gpu_active_sample_pct": 0,
        "gpu_memctrl_p95_pct": 0,
        "gpu_memctrl_max_pct": 0,
        "gpu_memctrl_active_sample_pct": 0,
        "gpu_power_p95_w": 0,
        "gpu_mem_used_p95_mib": _metric(metrics, "gpu_memory", "p95"),
        "gpu_mem_used_pct_p95": 0,
        "gpu_mem_used_pct_max": 0,
        "sm_active_p95_pct": 0,
        "sm_active_max_pct": 0,
        "sm_occupancy_p95_pct": 0,
        "tensor_active_p95_pct": 0,
        "tensor_active_max_pct": 0,
        "dram_active_p95_pct": 0,
        "dram_active_max_pct": 0,
        "fp16_active_p95_pct": 0,
        "fp16_active_max_pct": 0,
        "fp32_active_p95_pct": 0,
        "fp32_active_max_pct": 0,
        "dcgm_samples": _number(metrics.get("gpu_samples", 0)),
        "vllm_ok": 1,
        "failed_vm_ids": [],
        "config": {
            "vm_count": _number(point.get("agents", 0)),
            "tasks_per_vm": 1,
            "request_workers": _number(point.get("parallelism", 0)),
            "workload_seconds": _number(point.get("duration_sec", 0)),
            "memory_workers": "-",
            "memory_mb": "-",
            "memory_rounds": "-",
            "memory_mode": "time_window",
            "llm_context_kb": int(_number(point.get("context_length", 0))) // 1024,
            "experiment_mode": point.get("experiment_mode", ""),
            "case_id": point.get("case_id", ""),
            "max_active_llm_requests": _number(point.get("max_active_llm_requests", 0)),
            "max_active_prefill_tokens": _number(point.get("max_active_prefill_tokens", 0)),
            "llm_prompt_repeat": 1,
            "llm_max_tokens": "-",
            "llm_load_mode": SWEEP_WORKLOAD_TYPE,
            "llm_request_timeout_seconds": "-",
            "llm_inter_task_sleep_ms": 0,
            "vcpu_count": "-",
            "mem_mib": "-",
        },
    }


def _metric(metrics: dict[str, Any], group: str, field: str) -> int | float:
    try:
        return _number(metrics.get(group, {}).get(field, 0))
    except AttributeError:
        return 0


def _point_config(manifest: dict[str, Any]) -> dict[str, Any]:
    first_agent = (manifest.get("agents") or [{}])[0]
    return {
        "vm_count": manifest.get("vm_count"),
        "tasks_per_vm": manifest.get("tasks_per_vm"),
        "request_workers": manifest.get("request_workers"),
        "workload_seconds": manifest.get("workload_seconds"),
        "memory_workers": manifest.get("memory_workers"),
        "memory_mb": manifest.get("memory_mb"),
        "memory_rounds": manifest.get("memory_rounds"),
        "memory_mode": manifest.get("memory_mode"),
        "llm_context_kb": manifest.get("llm_context_kb"),
        "llm_prompt_repeat": manifest.get("llm_prompt_repeat"),
        "llm_max_tokens": manifest.get("llm_max_tokens"),
        "llm_load_mode": manifest.get("llm_load_mode"),
        "llm_request_timeout_seconds": manifest.get("llm_request_timeout_seconds"),
        "llm_inter_task_sleep_ms": manifest.get("llm_inter_task_sleep_ms"),
        "vcpu_count": first_agent.get("vcpu_count"),
        "mem_mib": first_agent.get("mem_mib"),
    }


def _build_overview(points: list[dict[str, Any]]) -> dict[str, Any]:
    if not points:
        return {
            "max_agents": 0,
            "total_completed": 0,
            "total_failed": 0,
            "best_stable_agents": 0,
            "max_throughput_task_per_min_workload": 0,
        }
    stable = [item["agents"] for item in points if item.get("failed") == 0]
    return {
        "max_agents": max(item["agents"] for item in points),
        "total_completed": sum(int(item.get("completed", 0) or 0) for item in points),
        "total_failed": sum(int(item.get("failed", 0) or 0) for item in points),
        "best_stable_agents": max(stable) if stable else 0,
        "max_throughput_task_per_min_workload": max(
            float(item.get("throughput_task_per_min_workload", 0) or 0) for item in points
        ),
    }


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _summary_rows_as_points(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "agents": row.get("agents", "0"),
            "vm_results": row.get("vm_results", "0"),
            "completed": row.get("completed_tasks", "0"),
            "failed": row.get("failed_tasks", "0"),
            "vllm_ok": row.get("vllm_ok", "0"),
        }
        for row in rows
    ]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_run_metadata(run_path: Path) -> dict[str, Any]:
    return _read_json(run_path / "dashboard-run.json")


def _write_run_metadata(run_path: Path, config: SweepLaunchConfig, env_updates: dict[str, str]) -> None:
    metadata = {
        "display_name": config.run_name.strip() or "manual",
        "created_at_unix": time.time(),
        "sweep_root": env_updates["SWEEP_ROOT"],
        "agents": config.agents,
        "run_seconds": config.run_seconds,
        "workload_grace_seconds": config.workload_grace_seconds,
        "memory_workers": config.memory_workers,
        "memory_mb": config.memory_mb,
        "memory_rounds": config.memory_rounds,
        "vcpus_per_agent": config.vcpus_per_agent,
        "llm_context_kb": config.llm_context_kb,
        "llm_prompt_repeat": config.llm_prompt_repeat,
        "llm_max_tokens": config.llm_max_tokens,
        "llm_load_mode": config.llm_load_mode,
        "llm_request_timeout_seconds": config.llm_request_timeout_seconds,
        "llm_inter_task_sleep_ms": config.llm_inter_task_sleep_ms,
        "workload": config.workload,
        "executor": config.executor,
    }
    (run_path / "dashboard-run.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _number(value: object) -> int | float:
    if value is None or value == "":
        return 0
    if isinstance(value, int | float):
        return value
    number = float(str(value))
    if number.is_integer():
        return int(number)
    return number


def _dram_peak_pct(value: object, dram_bw_max_gbps: int | float) -> int | float:
    if value not in {None, ""}:
        return _number(value)
    if not dram_bw_max_gbps:
        return 0
    pct = round(float(dram_bw_max_gbps) / 580.0 * 100, 2)
    return int(pct) if pct.is_integer() else pct


def _looks_like_run(path: Path) -> bool:
    return (
        (path / "dashboard-run.json").exists()
        or
        _is_team_v2_sweep_run(path)
        or
        _is_team_v2_run(path)
        or
        (path / "aligned_metrics.csv").exists()
        or (path / "sweep_summary.csv").exists()
        or any(path.glob("agents_*/results/summary.json"))
    )


def _is_team_v2_run(path: Path) -> bool:
    result_path = path / "result.json"
    if not result_path.exists():
        return False
    return _read_json(result_path).get("workload_type") == TEAM_V2_WORKLOAD


def _is_team_v2_sweep_run(path: Path) -> bool:
    result_path = path / "result.json"
    if not result_path.exists():
        return False
    return _read_json(result_path).get("workload_type") == SWEEP_WORKLOAD_TYPE


def _sanitize_label(value: str) -> str:
    label = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower()
    return label or "manual"


def _validate_dashboard_combo(workload: str, executor: str) -> None:
    if (workload, executor) != SUPPORTED_DASHBOARD_COMBO:
        raise ValueError(
            f"unsupported dashboard sweep combination: workload={workload}, executor={executor}"
        )


def _review_to_dict(review) -> dict[str, Any]:
    return {
        "verdict": review.verdict,
        "blocked": review.blocked,
        "blockers": [finding.message for finding in review.blockers],
        "warnings": [finding.message for finding in review.warnings],
    }


def _positive_int(value: int, name: str) -> int:
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def _non_negative_int(value: int, name: str) -> int:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _validate_llm_load_mode(value: str) -> str:
    if value not in {"single_task", "sustained_prefill"}:
        raise ValueError(f"unsupported llm_load_mode: {value}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aab-dashboard")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--host", default=os.environ.get("AAB_UI_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AAB_UI_PORT", "80")))
    parser.add_argument("--fallback-port", type=int, default=int(os.environ.get("AAB_UI_FALLBACK_PORT", "8080")))
    parser.add_argument(
        "--disable-port-fallback",
        action="store_true",
        default=os.environ.get("AAB_UI_ENABLE_PORT_FALLBACK", "1") in {"0", "false", "False"},
    )
    args = parser.parse_args(argv)
    serve_with_fallback(
        args.project_root,
        host=args.host,
        port=args.port,
        fallback_port=args.fallback_port,
        enable_fallback=not args.disable_port_fallback,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
