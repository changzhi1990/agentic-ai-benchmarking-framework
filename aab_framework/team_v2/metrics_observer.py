from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any


class MetricsObserver:
    """Lifecycle wrapper around existing metrics outputs.

    This class intentionally does not collect new metrics. It reads existing
    framework summaries when present and records the attribution method.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.started = False
        self.started_at: float | None = None
        self.ended_at: float | None = None

    def start(self) -> None:
        self.started = True
        self.started_at = time.time()

    def stop(self) -> dict[str, Any]:
        self.ended_at = time.time()
        legacy = self._read_existing_summary()
        structured = self._structured_summary_from_legacy(legacy)
        timeline = self._timeline_refs()
        window = {
            "started_at": _iso(self.started_at or self.ended_at or time.time()),
            "ended_at": _iso(self.ended_at or time.time()),
            "source": "run_prepared_firecracker_agents.sh" if (self.run_dir / "firecracker-run.json").exists() else "existing_metrics_pipeline",
            "attribution_method": "run_time_window",
        }
        legacy.setdefault("attribution_method", "time_window")
        legacy["metrics_summary"] = structured
        legacy["metrics_timeline"] = timeline
        legacy["metrics_field_mapping"] = {
            "cpu": "cpu.util_pct",
            "gpu": "gpu.avg_util_pct",
            "gpu_memory": "gpu.memory_used_mib",
            "memory": "memory.used_mib",
            "dram_bw": "dram.total_bw_gbps",
        }
        legacy["metrics_window"] = window
        return legacy

    def _read_existing_summary(self) -> dict[str, Any]:
        csv_summary = self._read_metrics_csv_summary()
        if csv_summary:
            return csv_summary
        aligned = self.run_dir / "aligned_metrics.json"
        if aligned.exists():
            try:
                rows = json.loads(aligned.read_text(encoding="utf-8"))
                if rows:
                    return _legacy_from_aligned_row(rows[0]) | {"source": str(aligned), "rows": rows}
            except json.JSONDecodeError:
                pass
        return {
            "avg_cpu_util": 0,
            "max_cpu_util": 0,
            "avg_memory_used": 0,
            "max_memory_used": 0,
            "avg_dram_read_bw": 0,
            "max_dram_read_bw": 0,
            "avg_dram_write_bw": 0,
            "max_dram_write_bw": 0,
            "avg_gpu_util": 0,
            "max_gpu_util": 0,
            "avg_gpu_memory_used": 0,
            "max_gpu_memory_used": 0,
            "avg_gpu_memory_controller_util": 0,
            "max_gpu_memory_controller_util": 0,
        }

    def _read_metrics_csv_summary(self) -> dict[str, Any]:
        metrics_dir = self.run_dir / "metrics"
        cpu = _read_float_col(metrics_dir / "cpu.csv", "cpu_util_pct")
        memory = _read_float_col(metrics_dir / "memory.csv", "memory_used_mib")
        gpu = _read_float_col(metrics_dir / "gpu.csv", "utilization_gpu_pct")
        gpu_memory = _read_float_col(metrics_dir / "gpu.csv", "memory_used_mib")
        gpu_memctrl = _read_float_col(metrics_dir / "gpu.csv", "utilization_memory_pct")
        dram_bw = _read_pcm_metric(metrics_dir / "amd_pcm_memory.csv", "Total Mem Bw (GB/s)")
        if not any([cpu, memory, gpu, gpu_memory, gpu_memctrl, dram_bw]):
            return {}
        return {
            "avg_cpu_util": _avg(cpu),
            "max_cpu_util": _max(cpu),
            "avg_memory_used": _avg(memory),
            "max_memory_used": _max(memory),
            "avg_dram_read_bw": 0,
            "max_dram_read_bw": 0,
            "avg_dram_write_bw": 0,
            "max_dram_write_bw": 0,
            "avg_dram_bw": _avg(dram_bw),
            "max_dram_bw": _max(dram_bw),
            "avg_gpu_util": _avg(gpu),
            "max_gpu_util": _max(gpu),
            "avg_gpu_memory_used": _avg(gpu_memory),
            "max_gpu_memory_used": _max(gpu_memory),
            "avg_gpu_memory_controller_util": _avg(gpu_memctrl),
            "max_gpu_memory_controller_util": _max(gpu_memctrl),
            "cpu_p50": _percentile(cpu, 50),
            "cpu_p95": _percentile(cpu, 95),
            "gpu_p50": _percentile(gpu, 50),
            "gpu_p95": _percentile(gpu, 95),
            "gpu_memory_p50": _percentile(gpu_memory, 50),
            "gpu_memory_p95": _percentile(gpu_memory, 95),
            "memory_p50": _percentile(memory, 50),
            "memory_p95": _percentile(memory, 95),
            "dram_bw_p95": _percentile(dram_bw, 95),
        }

    def _structured_summary_from_legacy(self, legacy: dict[str, Any]) -> dict[str, Any]:
        return {
            "cpu": {
                "avg": _number(legacy.get("avg_cpu_util", legacy.get("cpu_p50_pct", 0))),
                "p50": _number(legacy.get("cpu_p50", legacy.get("cpu_p50_pct", legacy.get("avg_cpu_util", 0)))),
                "p95": _number(legacy.get("cpu_p95", legacy.get("cpu_p95_pct", legacy.get("max_cpu_util", 0)))),
                "max": _number(legacy.get("max_cpu_util", legacy.get("cpu_max_pct", legacy.get("cpu_p95_pct", 0)))),
                "unit": "percent",
            },
            "gpu": {
                "avg": _number(legacy.get("avg_gpu_util", legacy.get("gpu_util_p50_pct", 0))),
                "p50": _number(legacy.get("gpu_p50", legacy.get("gpu_util_p50_pct", legacy.get("avg_gpu_util", 0)))),
                "p95": _number(legacy.get("gpu_p95", legacy.get("gpu_util_p95_pct", legacy.get("max_gpu_util", 0)))),
                "max": _number(legacy.get("max_gpu_util", legacy.get("gpu_util_max_pct", 0))),
                "unit": "percent",
            },
            "gpu_memory": {
                "avg": _number(legacy.get("avg_gpu_memory_used", legacy.get("gpu_mem_used_p95_mib", 0))),
                "p50": _number(legacy.get("gpu_memory_p50", legacy.get("avg_gpu_memory_used", 0))),
                "p95": _number(legacy.get("gpu_memory_p95", legacy.get("gpu_mem_used_p95_mib", legacy.get("max_gpu_memory_used", 0)))),
                "max": _number(legacy.get("max_gpu_memory_used", legacy.get("gpu_mem_used_p95_mib", 0))),
                "unit": "MiB",
            },
            "memory": {
                "avg": _number(legacy.get("avg_memory_used", 0)),
                "p50": _number(legacy.get("memory_p50", legacy.get("avg_memory_used", 0))),
                "p95": _number(legacy.get("memory_p95", legacy.get("max_memory_used", 0))),
                "max": _number(legacy.get("max_memory_used", 0)),
                "unit": "MiB",
            },
            "dram_bw": {
                "avg": _number(legacy.get("avg_dram_bw", legacy.get("dram_bw_p50_gbps", 0))),
                "p95": _number(legacy.get("dram_bw_p95", legacy.get("dram_bw_p95_gbps", 0))),
                "max": _number(legacy.get("max_dram_bw", legacy.get("dram_bw_max_gbps", 0))),
                "unit": "GB/s",
            },
        }

    def _timeline_refs(self) -> dict[str, str]:
        metrics_dir = self.run_dir / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        system = metrics_dir / "system_metrics.jsonl"
        if not system.exists():
            system.write_text("", encoding="utf-8")
        refs = {"system": "metrics/system_metrics.jsonl"}
        if (metrics_dir / "cpu.csv").exists():
            refs["cpu"] = "metrics/cpu.csv"
        if (metrics_dir / "gpu.csv").exists():
            refs["gpu"] = "metrics/gpu.csv"
            refs["gpu_memory"] = "metrics/gpu.csv"
        if (metrics_dir / "memory.csv").exists():
            refs["memory"] = "metrics/memory.csv"
        if (metrics_dir / "amd_pcm_memory.csv").exists():
            refs["dram_bw"] = "metrics/amd_pcm_memory.csv"
        return refs


def _legacy_from_aligned_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "avg_cpu_util": _number(row.get("cpu_p50_pct", 0)),
        "max_cpu_util": _number(row.get("cpu_max_pct", row.get("cpu_p95_pct", 0))),
        "avg_memory_used": 0,
        "max_memory_used": 0,
        "avg_dram_bw": _number(row.get("dram_bw_p50_gbps", 0)),
        "max_dram_bw": _number(row.get("dram_bw_max_gbps", 0)),
        "avg_dram_read_bw": 0,
        "max_dram_read_bw": _number(row.get("dram_read_bw_p95_gbps", 0)),
        "avg_dram_write_bw": 0,
        "max_dram_write_bw": _number(row.get("dram_write_bw_p95_gbps", 0)),
        "avg_gpu_util": _number(row.get("gpu_util_p50_pct", 0)),
        "max_gpu_util": _number(row.get("gpu_util_max_pct", 0)),
        "avg_gpu_memory_used": _number(row.get("gpu_mem_used_p95_mib", 0)),
        "max_gpu_memory_used": _number(row.get("gpu_mem_used_p95_mib", 0)),
        "avg_gpu_memory_controller_util": _number(row.get("gpu_memctrl_p50_pct", 0)),
        "max_gpu_memory_controller_util": _number(row.get("gpu_memctrl_max_pct", 0)),
        "cpu_p50_pct": _number(row.get("cpu_p50_pct", 0)),
        "cpu_p95_pct": _number(row.get("cpu_p95_pct", 0)),
        "gpu_util_p50_pct": _number(row.get("gpu_util_p50_pct", 0)),
        "gpu_util_p95_pct": _number(row.get("gpu_util_p95_pct", 0)),
        "gpu_mem_used_p95_mib": _number(row.get("gpu_mem_used_p95_mib", 0)),
        "dram_bw_p50_gbps": _number(row.get("dram_bw_p50_gbps", 0)),
        "dram_bw_p95_gbps": _number(row.get("dram_bw_p95_gbps", 0)),
        "dram_bw_max_gbps": _number(row.get("dram_bw_max_gbps", 0)),
    }


def _read_float_col(path: Path, column: str) -> list[float]:
    if not path.exists():
        return []
    values: list[float] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                values.append(float(str(row.get(column, "")).strip()))
            except ValueError:
                pass
    return values


def _read_pcm_metric(path: Path, metric: str) -> list[float]:
    if not path.exists():
        return []
    values: list[float] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if metric not in line or "|" not in line:
            continue
        try:
            values.append(float(line.split("|")[1].strip()))
        except (IndexError, ValueError):
            pass
    return values


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0


def _max(values: list[float]) -> float:
    return round(max(values), 3) if values else 0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * percentile / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = position - lower
    return round(ordered[lower] * (1 - fraction) + ordered[upper] * fraction, 3)


def _number(value: object) -> float:
    if value in {None, ""}:
        return 0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
