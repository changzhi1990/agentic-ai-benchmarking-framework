from __future__ import annotations

import json
import socket
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .orchestrator import TeamOrchestrator
from .schemas import WORKLOAD_TYPE, TeamRunConfig


SWEEP_WORKLOAD_TYPE = "mini_swe_agent_team_v2_sweep"


class TeamSweepOrchestrator:
    def __init__(
        self,
        base_config: TeamRunConfig,
        agent_counts: list[int],
        *,
        context_lengths: list[int] | None = None,
        repeats: int | None = None,
        experiment_mode: str | None = None,
        max_active_llm_requests: int | None = None,
        max_active_prefill_tokens: int | None = None,
    ) -> None:
        if not agent_counts:
            raise ValueError("agent_counts must not be empty")
        self.base_config = base_config
        self.agent_counts = agent_counts
        self.context_lengths = context_lengths or [base_config.context_length]
        self.repeats = repeats or base_config.repeats
        self.experiment_mode = experiment_mode or base_config.experiment_mode
        self.max_active_llm_requests = max_active_llm_requests
        self.max_active_prefill_tokens = max_active_prefill_tokens

    def run(self) -> dict[str, Any]:
        run_id = f"team-v2-sweep-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        run_dir = Path(self.base_config.out_dir) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        points: list[dict[str, Any]] = []
        child_runs: list[dict[str, Any]] = []
        errors: list[str] = []

        for context_length in self.context_lengths:
            for agents in self.agent_counts:
                for repeat in range(1, self.repeats + 1):
                    sweep_meta = _sweep_metadata(
                        agents=agents,
                        context_length=context_length,
                        repeat=repeat,
                        experiment_mode=self.experiment_mode,
                        fixed_llm_requests=self.max_active_llm_requests
                        or self.base_config.max_active_llm_requests
                        or self.base_config.fixed_llm_requests,
                        max_active_prefill_tokens=self.max_active_prefill_tokens,
                    )
                    child_config = replace(
                        self.base_config,
                        num_agents=agents,
                        parallelism=agents,
                        context_length=context_length,
                        out_dir=str(run_dir),
                        experiment_mode=self.experiment_mode,
                        max_active_llm_requests=sweep_meta["max_active_llm_requests"],
                        max_active_prefill_tokens=sweep_meta["max_active_prefill_tokens"],
                        sweep=sweep_meta,
                    )
                    child = TeamOrchestrator(child_config).run()
                    child_data = _read_json(Path(child["result_path"]))
                    child_runs.append(
                        {
                            "agents": agents,
                            "num_agents": agents,
                            "context_length": context_length,
                            "repeat": repeat,
                            "case_id": sweep_meta["case_id"],
                            "experiment_mode": self.experiment_mode,
                            "run_id": child_data.get("run_id", child["run_id"]),
                            "run_dir": child["run_dir"],
                            "result_path": child["result_path"],
                            "summary": child_data.get("summary", {}),
                        }
                    )
                    points.append(_point_from_child(child_data, child["run_dir"]))
                    errors.extend(child_data.get("errors", []))

        ended = time.time()
        scaling_metrics = [
            _scaling_metrics_from_child(_read_json(Path(child["result_path"])), child["agents"], child["result_path"])
            for child in child_runs
        ]
        result = {
            "run_id": run_id,
            "sweep_group_id": run_id,
            "workload_type": SWEEP_WORKLOAD_TYPE,
            "child_workload_type": WORKLOAD_TYPE,
            "started_at": _iso(started),
            "ended_at": _iso(ended),
            "duration_sec": round(ended - started, 3),
            "config": self.base_config.to_dict()
            | {
                "agent_counts": self.agent_counts,
                "context_lengths": self.context_lengths,
                "repeats": self.repeats,
                "experiment_mode": self.experiment_mode,
            },
            "summary": _summary_from_points(points),
            "points": sorted(points, key=lambda item: item["agents"]),
            "child_runs": child_runs,
            "runs": [
                {
                    "num_agents": child["agents"],
                    "context_length": child["context_length"],
                    "repeat": child["repeat"],
                    "case_id": child["case_id"],
                    "experiment_mode": child["experiment_mode"],
                    "run_id": child["run_id"],
                    "result_path": child["result_path"],
                    "run_dir": child["run_dir"],
                }
                for child in child_runs
            ],
            "scaling_metrics": scaling_metrics,
            "team": {
                "coordinator": "CoordinatorAgent",
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
            "ui": {
                "url": _ui_url(self.base_config.ui_port),
                "requested_port": self.base_config.ui_port,
                "actual_port": self.base_config.ui_port,
                "fallback_used": False,
                "fallback_reason": None,
            },
            "errors": errors,
        }
        (run_dir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        sweep = {
            "sweep_group_id": run_id,
            "experiment_name": "agent_scaling_test",
            "parameters": {"agent_counts": self.agent_counts, "context_lengths": self.context_lengths, "repeats": self.repeats},
            "parameter": "agent_counts,context_lengths",
            "values": {"agent_counts": self.agent_counts, "context_lengths": self.context_lengths},
            "runs": result["runs"],
            "scaling_metrics": scaling_metrics,
            "created_at": result["started_at"],
            "notes": "mini_swe_agent_team_v2 agent-count sweep; each value is an independent run",
        }
        (run_dir / "sweep.json").write_text(json.dumps(sweep, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _write_csv(run_dir / "team_sweep_summary.csv", result["points"])
        response = dict(result)
        response["run_dir"] = str(run_dir)
        response["result_path"] = str(run_dir / "result.json")
        return response


def _point_from_child(child: dict[str, Any], run_dir: str) -> dict[str, Any]:
    summary = child.get("summary", {})
    config = child.get("config", {})
    sweep = config.get("sweep", {}) or {}
    metrics_summary = child.get("metrics_summary", {})
    return {
        "agents": int(config.get("num_agents", 0) or 0),
        "parallelism": int(config.get("parallelism", 0) or 0),
        "context_length": int(config.get("context_length", 0) or 0),
        "case_id": str(sweep.get("case_id", "")),
        "experiment_mode": str(sweep.get("experiment_mode", config.get("experiment_mode", ""))),
        "repeat": int(sweep.get("repeat", 1) or 1),
        "max_active_llm_requests": int(sweep.get("max_active_llm_requests", config.get("max_active_llm_requests", 0)) or 0),
        "max_active_prefill_tokens": int(sweep.get("max_active_prefill_tokens", config.get("max_active_prefill_tokens", 0)) or 0),
        "duration_sec": float(child.get("duration_sec", 0) or 0),
        "total_issues": int(summary.get("total_issues", 0) or 0),
        "verified_success_rate": float(summary.get("verified_success_rate", 0) or 0),
        "failed_issues": int(summary.get("failed_issues", 0) or 0),
        "issue_latency_p95_sec": float(summary.get("issue_latency_p95_sec", 0) or 0),
        "candidate_per_min": float(summary.get("candidate_per_min", 0) or 0),
        "issue_per_hour": float(summary.get("issue_per_hour", 0) or 0),
        "latency_p95": float(summary.get("issue_latency_p95_sec", 0) or 0),
        "metrics_summary": metrics_summary,
        "metrics_timeline": child.get("metrics_timeline", {}),
        "result_path": str(Path(run_dir) / "result.json"),
        "run_id": child.get("run_id", ""),
    }


def _scaling_metrics_from_child(child: dict[str, Any], agents: int, result_path: str) -> dict[str, Any]:
    metrics = child.get("metrics_summary", {}) or {}
    summary = child.get("summary", {}) or {}
    config = child.get("config", {}) or {}
    sweep = config.get("sweep", {}) or {}
    return {
        "num_agents": agents,
        "context_length": int(config.get("context_length", 0) or 0),
        "case_id": str(sweep.get("case_id", "")),
        "experiment_mode": str(sweep.get("experiment_mode", config.get("experiment_mode", ""))),
        "repeat": int(sweep.get("repeat", 1) or 1),
        "run_id": child.get("run_id", ""),
        "cpu_avg": _metric(metrics, "cpu", "avg"),
        "cpu_p95": _metric(metrics, "cpu", "p95"),
        "gpu_avg": _metric(metrics, "gpu", "avg"),
        "gpu_p95": _metric(metrics, "gpu", "p95"),
        "gpu_memory_avg": _metric(metrics, "gpu_memory", "avg"),
        "gpu_memory_p95": _metric(metrics, "gpu_memory", "p95"),
        "memory_avg": _metric(metrics, "memory", "avg"),
        "memory_p95": _metric(metrics, "memory", "p95"),
        "dram_bw_avg": _metric(metrics, "dram_bw", "avg"),
        "dram_bw_p95": _metric(metrics, "dram_bw", "p95"),
        "issue_per_hour": float(summary.get("issue_per_hour", 0) or 0),
        "latency_p95": float(summary.get("issue_latency_p95_sec", 0) or 0),
        "source_result_json": result_path,
    }


def _metric(metrics: dict[str, Any], group: str, field: str) -> float:
    try:
        return float(metrics.get(group, {}).get(field, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _summary_from_points(points: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "sweep_points": len(points),
        "max_agents": max((point["agents"] for point in points), default=0),
        "total_issues": sum(point["total_issues"] for point in points),
        "failed_issues": sum(point["failed_issues"] for point in points),
        "best_verified_success_rate": max((point["verified_success_rate"] for point in points), default=0),
    }


def _sweep_metadata(
    *,
    agents: int,
    context_length: int,
    repeat: int,
    experiment_mode: str,
    fixed_llm_requests: int,
    max_active_prefill_tokens: int | None,
) -> dict[str, Any]:
    if experiment_mode == "unlimited_llm":
        llm_requests = agents
    else:
        llm_requests = min(agents, fixed_llm_requests)
    prefill_tokens = max_active_prefill_tokens or llm_requests * context_length
    return {
        "agent_count": agents,
        "context_length": context_length,
        "case_id": f"agents{agents}_ctx{context_length}_r{repeat}",
        "experiment_mode": experiment_mode,
        "repeat": repeat,
        "max_active_llm_requests": llm_requests,
        "max_active_prefill_tokens": prefill_tokens,
    }


def _write_csv(path: Path, points: list[dict[str, Any]]) -> None:
    if not points:
        path.write_text("", encoding="utf-8")
        return
    import csv

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(points[0].keys()))
        writer.writeheader()
        writer.writerows(points)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _ui_url(port: int) -> str:
    host = socket.gethostname()
    if port == 80:
        return f"http://{host}/"
    return f"http://{host}:{port}/"
