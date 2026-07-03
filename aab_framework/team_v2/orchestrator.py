from __future__ import annotations

import json
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .aggregator import ResultAggregator
from .docker_runtime import DockerRuntime
from .firecracker_runner import (
    collect_firecracker_team_results,
    prepare_firecracker_team_plan,
    run_prepared_firecracker_team,
)
from .metrics_observer import MetricsObserver
from .mini_swe_adapter import MiniSweAgentAdapter
from .planner import TaskPlanner
from .resource_governor import ResourceGovernor
from .schemas import TeamRunConfig, WORKLOAD_TYPE
from .worker import SWEWorkerAgent


class TeamOrchestrator:
    def __init__(self, config: TeamRunConfig) -> None:
        self.config = config
        self.planner = TaskPlanner()
        self.aggregator = ResultAggregator()

    def run(self) -> dict[str, Any]:
        run_id = f"team-v2-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        run_dir = Path(self.config.out_dir) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        workers = self.planner.build_workers(self.config)
        plans = self.planner.build_issue_plans(self.config, workers)
        metrics = MetricsObserver(run_dir)
        metrics.start()
        runtime = DockerRuntime(self.config)
        issues: list[dict[str, Any]] = []
        errors: list[str] = []
        governor = ResourceGovernor(
            max_active_agents=self.config.max_active_agents or self.config.parallelism,
            max_active_llm_requests=self.config.max_active_llm_requests,
        )
        vllm_ok = True
        if self.config.adapter_mode != "mock":
            vllm_ok = governor.vllm_healthy(self.config.vllm_base_url)
            if self.config.runtime_type == "docker" and not runtime.available():
                errors.append("docker runtime is unavailable")
        adapter = MiniSweAgentAdapter(self.config)

        firecracker_manifest: dict[str, Any] | None = None
        if self.config.use_firecracker:
            firecracker_manifest = prepare_firecracker_team_plan(self.config, run_id, run_dir, workers, plans)
            if not self.config.firecracker_dry_run:
                completed = run_prepared_firecracker_team(self.config, run_dir)
                (run_dir / "firecracker-run.stdout.log").write_text(completed.stdout, encoding="utf-8")
                (run_dir / "firecracker-run.stderr.log").write_text(completed.stderr, encoding="utf-8")
                if completed.returncode != 0:
                    errors.append(f"firecracker runner exited with {completed.returncode}")
            issues, firecracker_errors = collect_firecracker_team_results(run_dir, firecracker_manifest, plans)
            errors.extend(firecracker_errors)
        else:
            def run_plan(plan):
                return SWEWorkerAgent(self.config, adapter).run_issue(plan, run_dir)

            with ThreadPoolExecutor(max_workers=self.config.parallelism) as executor:
                futures = {executor.submit(run_plan, plan): plan for plan in plans}
                for future in as_completed(futures):
                    plan = futures[future]
                    try:
                        issues.append(future.result())
                    except Exception as exc:  # keep result.json even for failed agents
                        errors.append(f"{plan.issue_id}/{plan.agent_id}: {type(exc).__name__}: {exc}")
                        now = time.time()
                        issues.append(
                            {
                                "issue_id": plan.issue_id,
                                "task_id": plan.task_id,
                                "repo": plan.repo,
                                "agent_id": plan.agent_id,
                                "status": "failed",
                                "started_at": _iso(now),
                                "ended_at": _iso(now),
                                "latency_sec": 0,
                                "rounds": [],
                                "final_patch_path": "",
                                "final_test_log_path": "",
                                "verified": False,
                                "error": str(exc),
                            }
                        )

        ended = time.time()
        metrics_summary = metrics.stop()
        agent_rows = _agent_rows(workers, issues, self.config, metrics_summary)
        summary = self.aggregator.aggregate(issues, agent_rows, metrics_summary)
        ui = _ui_status(self.config)
        result = {
            "run_id": run_id,
            "workload_type": WORKLOAD_TYPE,
            "started_at": _iso(started),
            "ended_at": _iso(ended),
            "duration_sec": round(ended - started, 3),
            "config": {
                **self.config.to_dict(),
                "runtime": runtime.to_result_config(),
                "vllm": _vllm_config(self.config),
                "sweep": self.config.sweep,
                "ui": {
                    "host": self.config.ui_host,
                    "port": self.config.ui_port,
                    "fallback_port": self.config.ui_fallback_port,
                    "actual_port": ui["actual_port"],
                    "fallback_used": ui["fallback_used"],
                },
            },
            "team": {
                "coordinator": "CoordinatorAgent",
                "runtime": "DockerRuntime",
                "planner": "TaskPlanner",
                "worker": "SWEWorkerAgent",
                "adapter": "MiniSweAgentAdapter",
                "reviewer": "PatchReviewerAgent",
                "verifier": "TestVerifierAgent",
                "repair_loop": "RepairLoopController",
                "metrics_observer": "MetricsObserver",
                "resource_governor": "ResourceGovernor",
                "aggregator": "ResultAggregator",
            },
            "summary": summary,
            "issues": sorted(issues, key=lambda item: item["issue_id"]),
            "agents": agent_rows,
            "overall_metrics_summary": metrics_summary,
            "metrics_summary": metrics_summary.get("metrics_summary", {}),
            "metrics_timeline": metrics_summary.get("metrics_timeline", {}),
            "metrics_field_mapping": metrics_summary.get("metrics_field_mapping", {}),
            "metrics_window": metrics_summary.get("metrics_window", {}),
            "ui": ui,
            "firecracker": {
                "enabled": self.config.use_firecracker,
                "runner": self.config.fc_runner if self.config.use_firecracker else None,
                "kernel": self.config.fc_kernel,
                "rootfs": self.config.fc_rootfs,
                "guest_vllm_base_url": self.config.guest_vllm_base_url,
                "manifest_path": str(run_dir / "firecracker-run.json") if firecracker_manifest else None,
            },
            "vllm": {
                "ok": vllm_ok,
                "base_url": self.config.vllm_base_url,
                "model": self.config.model,
                **_vllm_config(self.config),
            },
            "errors": errors,
        }
        (run_dir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        response = dict(result)
        response["run_dir"] = str(run_dir)
        response["result_path"] = str(run_dir / "result.json")
        return response


def _agent_rows(workers, issues: list[dict[str, Any]], config: TeamRunConfig, metrics: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for worker in workers:
        assigned = [item for item in issues if item.get("agent_id") == worker.agent_id]
        rows.append(
            {
                "agent_id": worker.agent_id,
                "role": worker.role,
                "status": "succeeded" if all(item.get("verified") for item in assigned) else "failed",
                "assigned_issues": [item["issue_id"] for item in assigned],
                "completed_issues": len(assigned),
                "verified_success_issues": sum(1 for item in assigned if item.get("verified")),
                "latency_sec": round(sum(float(item.get("latency_sec", 0) or 0) for item in assigned), 3),
                "requested_context_length": config.context_length,
                "effective_context_length": config.context_length,
                "context_source": "TeamRunConfig.context_length",
                "verified_context_length": False,
                "verification_method": "assumed",
                "metrics_summary": metrics,
                "metrics_timeline_ref": f"metrics/agent_{worker.agent_id}_metrics.jsonl",
            }
        )
    return rows


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _ui_url(port: int) -> str:
    host = socket.gethostname()
    if port == 80:
        return f"http://{host}/"
    return f"http://{host}:{port}/"


def _ui_status(config: TeamRunConfig) -> dict[str, Any]:
    actual_port = config.ui_port
    fallback_used = False
    fallback_reason = None
    if config.ui_enable_port_fallback:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((config.ui_host, config.ui_port))
        except PermissionError:
            actual_port = config.ui_fallback_port
            fallback_used = True
            fallback_reason = "Port 80 requires root permission" if config.ui_port == 80 else "Port requires elevated permission"
        except OSError as exc:
            actual_port = config.ui_fallback_port
            fallback_used = True
            fallback_reason = str(exc)
    return {
        "url": _ui_url(actual_port),
        "requested_port": config.ui_port,
        "actual_port": actual_port,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
    }


def _vllm_config(config: TeamRunConfig) -> dict[str, Any]:
    return {
        "model": config.model,
        "max_model_len": max(config.vllm_max_model_len, config.context_length),
        "max_num_seqs": config.vllm_max_num_seqs,
        "max_num_batched_tokens": config.vllm_max_num_batched_tokens,
        "gpu_memory_utilization": config.vllm_gpu_memory_utilization,
        "tensor_parallel_size": config.vllm_tensor_parallel_size,
        "dtype": config.vllm_dtype,
        "prefix_caching": config.vllm_prefix_caching,
    }
