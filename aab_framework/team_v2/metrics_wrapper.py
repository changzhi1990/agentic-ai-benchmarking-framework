from __future__ import annotations

import csv
import json
import socket
import time
from pathlib import Path
from typing import Any

from .metrics_observer import MetricsObserver
from .schemas import WORKLOAD_TYPE
from .sweep import SWEEP_WORKLOAD_TYPE


def refresh_run_metrics(run_dir: str | Path) -> dict[str, Any]:
    run_path = Path(run_dir)
    result_path = run_path / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))

    observer = MetricsObserver(run_path)
    metrics = observer.stop()
    metrics["metrics_window"] = {
        "started_at": result.get("started_at"),
        "ended_at": result.get("ended_at"),
        "source": "run_mini_swe_agent_team_v2_sweep_with_metrics.sh",
        "attribution_method": "run_time_window",
    }

    result["overall_metrics_summary"] = metrics
    result["metrics_summary"] = metrics.get("metrics_summary", {})
    result["metrics_timeline"] = metrics.get("metrics_timeline", {})
    result["metrics_field_mapping"] = metrics.get("metrics_field_mapping", {})
    result["metrics_window"] = metrics.get("metrics_window", {})
    for agent in result.get("agents", []):
        agent["metrics_summary"] = metrics
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def build_sweep_from_child_runs(sweep_root: str | Path, child_result_paths: list[str | Path]) -> dict[str, Any]:
    root = Path(sweep_root)
    root.mkdir(parents=True, exist_ok=True)
    children = [json.loads(Path(path).read_text(encoding="utf-8")) for path in child_result_paths]
    points = [_point_from_child(child, Path(path)) for child, path in zip(children, child_result_paths, strict=True)]
    runs = [
        {
            "num_agents": int(child.get("config", {}).get("num_agents", 0) or 0),
            "run_id": child.get("run_id", ""),
            "result_path": str(path),
            "run_dir": str(Path(path).parent),
        }
        for child, path in zip(children, child_result_paths, strict=True)
    ]
    run_id = root.name
    started_at = min((child.get("started_at", "") for child in children if child.get("started_at")), default=_iso(time.time()))
    ended_at = max((child.get("ended_at", "") for child in children if child.get("ended_at")), default=started_at)
    result = {
        "run_id": run_id,
        "sweep_group_id": run_id,
        "workload_type": SWEEP_WORKLOAD_TYPE,
        "child_workload_type": WORKLOAD_TYPE,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_sec": round(sum(float(child.get("duration_sec", 0) or 0) for child in children), 3),
        "config": (children[0].get("config", {}) if children else {}) | {
            "agent_counts": [point["agents"] for point in points],
        },
        "summary": _summary_from_points(points),
        "points": sorted(points, key=lambda item: item["agents"]),
        "child_runs": runs,
        "runs": runs,
        "scaling_metrics": [
            _scaling_metrics_from_child(child, str(path))
            for child, path in zip(children, child_result_paths, strict=True)
        ],
        "team": children[0].get("team", {}) if children else {},
        "ui": {
            "url": _ui_url(80),
            "requested_port": 80,
            "actual_port": 80,
            "fallback_used": False,
            "fallback_reason": None,
        },
        "errors": [error for child in children for error in child.get("errors", [])],
    }
    (root / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sweep = {
        "sweep_group_id": run_id,
        "experiment_name": "agent_scaling_test",
        "parameter": "num_agents",
        "values": [point["agents"] for point in result["points"]],
        "runs": runs,
        "scaling_metrics": result["scaling_metrics"],
        "created_at": started_at,
        "notes": "mini_swe_agent_team_v2 agent-count sweep with external metrics collectors",
    }
    (root / "sweep.json").write_text(json.dumps(sweep, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_csv(root / "team_sweep_summary.csv", result["points"])
    return result


def _point_from_child(child: dict[str, Any], result_path: Path) -> dict[str, Any]:
    summary = child.get("summary", {}) or {}
    config = child.get("config", {}) or {}
    return {
        "agents": int(config.get("num_agents", 0) or 0),
        "parallelism": int(config.get("parallelism", 0) or 0),
        "context_length": int(config.get("context_length", 0) or 0),
        "duration_sec": float(child.get("duration_sec", 0) or 0),
        "total_issues": int(summary.get("total_issues", 0) or 0),
        "verified_success_rate": float(summary.get("verified_success_rate", 0) or 0),
        "failed_issues": int(summary.get("failed_issues", 0) or 0),
        "issue_latency_p95_sec": float(summary.get("issue_latency_p95_sec", 0) or 0),
        "candidate_per_min": float(summary.get("candidate_per_min", 0) or 0),
        "issue_per_hour": float(summary.get("issue_per_hour", 0) or 0),
        "latency_p95": float(summary.get("issue_latency_p95_sec", 0) or 0),
        "metrics_summary": child.get("metrics_summary", {}),
        "metrics_timeline": child.get("metrics_timeline", {}),
        "result_path": str(result_path),
        "run_id": child.get("run_id", ""),
    }


def _scaling_metrics_from_child(child: dict[str, Any], result_path: str) -> dict[str, Any]:
    metrics = child.get("metrics_summary", {}) or {}
    summary = child.get("summary", {}) or {}
    agents = int(child.get("config", {}).get("num_agents", 0) or 0)
    return {
        "num_agents": agents,
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


def _summary_from_points(points: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "sweep_points": len(points),
        "max_agents": max((point["agents"] for point in points), default=0),
        "total_issues": sum(point["total_issues"] for point in points),
        "failed_issues": sum(point["failed_issues"] for point in points),
        "best_verified_success_rate": max((point["verified_success_rate"] for point in points), default=0),
    }


def _metric(metrics: dict[str, Any], group: str, field: str) -> float:
    try:
        return float(metrics.get(group, {}).get(field, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _write_csv(path: Path, points: list[dict[str, Any]]) -> None:
    if not points:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(points[0].keys()))
        writer.writeheader()
        writer.writerows(points)


def _ui_url(port: int) -> str:
    host = socket.gethostname()
    if port == 80:
        return f"http://{host}/"
    return f"http://{host}:{port}/"


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
