from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any


DRAM_PEAK_BW_GBPS = 580.0


ALIGNED_FIELDS = [
    "agents",
    "vm_results",
    "completed",
    "failed",
    "success_rate_pct",
    "vllm_ok",
    "throughput_task_per_min_run",
    "throughput_task_per_min_workload",
    "lat_all_p50_ms",
    "lat_all_p95_ms",
    "lat_ok_p50_ms",
    "lat_ok_p95_ms",
    "cpu_p50_pct",
    "cpu_p95_pct",
    "cpu_user_p95_pct",
    "cpu_sys_p95_pct",
    "load1_p95",
    "dram_bw_p50_gbps",
    "dram_bw_p95_gbps",
    "dram_bw_max_gbps",
    "dram_bw_max_pct_of_peak",
    "dram_read_bw_p95_gbps",
    "dram_write_bw_p95_gbps",
    "gpu_util_p50_pct",
    "gpu_util_p95_pct",
    "gpu_util_max_pct",
    "gpu_active_sample_pct",
    "gpu_memctrl_p50_pct",
    "gpu_memctrl_p95_pct",
    "gpu_memctrl_max_pct",
    "gpu_memctrl_active_sample_pct",
    "gpu_power_p95_w",
    "gpu_mem_used_p95_mib",
    "gpu_mem_used_pct_p95",
    "gpu_mem_used_pct_max",
    "gr_engine_active_p95_pct",
    "gr_engine_active_max_pct",
    "sm_active_p95_pct",
    "sm_active_max_pct",
    "sm_active_avg_pct",
    "sm_active_sample_pct",
    "sm_active_area",
    "sm_occupancy_p95_pct",
    "sm_occupancy_max_pct",
    "tensor_active_p95_pct",
    "tensor_active_max_pct",
    "tensor_active_avg_pct",
    "tensor_active_sample_pct",
    "tensor_active_area",
    "dram_active_p95_pct",
    "dram_active_max_pct",
    "dram_active_avg_pct",
    "dram_active_sample_pct",
    "dram_active_area",
    "fp32_active_p95_pct",
    "fp32_active_max_pct",
    "fp16_active_p95_pct",
    "fp16_active_max_pct",
    "memory_workers",
    "memory_mode",
    "cpu_samples",
    "pcm_samples",
    "gpu_samples",
    "dcgm_samples",
]


def summarize_firecracker_sweep(
    run_root: str | Path,
    *,
    run_seconds: float,
    workload_seconds: float,
    write_outputs: bool = True,
) -> list[dict[str, Any]]:
    root = Path(run_root)
    rows = [_summarize_point(path, run_seconds, workload_seconds) for path in _agent_dirs(root)]
    if write_outputs:
        _write_csv(root / "aligned_metrics.csv", rows)
        (root / "aligned_metrics.json").write_text(
            json.dumps(rows, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (root / "aligned_metrics.md").write_text(_markdown_table(rows), encoding="utf-8")
    return rows


def _summarize_point(path: Path, run_seconds: float, workload_seconds: float) -> dict[str, Any]:
    agents = int(path.name.split("_")[1])
    summary = json.loads((path / "results" / "summary.json").read_text(encoding="utf-8"))
    traces = _read_traces(path / "results")
    lat_all = [float(item.get("latency_ms", 0)) for item in traces if item.get("latency_ms") is not None]
    lat_ok = [
        float(item.get("latency_ms", 0))
        for item in traces
        if item.get("status") == "ok" and item.get("latency_ms") is not None
    ]

    cpu = _read_float_col(path / "metrics" / "cpu.csv", "cpu_util_pct")
    user = _read_float_col(path / "metrics" / "cpu.csv", "user_pct")
    system = _read_float_col(path / "metrics" / "cpu.csv", "system_pct")
    load1 = _read_float_col(path / "metrics" / "cpu.csv", "load1")
    gpu = _read_float_col(path / "metrics" / "gpu.csv", "utilization_gpu_pct")
    gpu_memctrl = _read_float_col(path / "metrics" / "gpu.csv", "utilization_memory_pct")
    gpu_power = _read_float_col(path / "metrics" / "gpu.csv", "power_draw_w")
    gpu_mem_used = _read_float_col(path / "metrics" / "gpu.csv", "memory_used_mib")
    gpu_mem_used_pct = _read_gpu_memory_used_pct(path / "metrics" / "gpu.csv")
    dcgm_source = _dcgm_metrics_path(path)
    gr_engine_active = _read_float_col(dcgm_source, "gr_engine_active_pct")
    sm_active = _read_float_col(dcgm_source, "sm_active_pct")
    sm_occupancy = _read_float_col(dcgm_source, "sm_occupancy_pct")
    tensor_active = _read_float_col(dcgm_source, "tensor_active_pct")
    dram_active = _read_float_col(dcgm_source, "dram_active_pct")
    fp32_active = _read_float_col(dcgm_source, "fp32_active_pct")
    fp16_active = _read_float_col(dcgm_source, "fp16_active_pct")
    dram_bw = _read_pcm_metric(path / "metrics" / "amd_pcm_memory.csv", "Total Mem Bw (GB/s)")
    dram_rd = _read_pcm_metric(path / "metrics" / "amd_pcm_memory.csv", "Total Mem RdBw (GB/s)")
    dram_wr = _read_pcm_metric(path / "metrics" / "amd_pcm_memory.csv", "Total Mem WrBw (GB/s)")
    result_records = _read_result_records(path / "results")
    completed = int(summary.get("completed_tasks", 0))
    failed = int(summary.get("failed_tasks", 0))
    total = completed + failed

    dram_bw_max = max(dram_bw) if dram_bw else None

    return {
        "agents": agents,
        "vm_results": summary.get("vm_results", 0),
        "completed": completed,
        "failed": failed,
        "success_rate_pct": _round(100 * completed / total if total else None),
        "vllm_ok": summary.get("vllm_ok", 0),
        "throughput_task_per_min_run": _round(completed / run_seconds * 60),
        "throughput_task_per_min_workload": _round(completed / workload_seconds * 60),
        "lat_all_p50_ms": _round(_percentile(lat_all, 50), 1),
        "lat_all_p95_ms": _round(_percentile(lat_all, 95), 1),
        "lat_ok_p50_ms": _round(_percentile(lat_ok, 50), 1),
        "lat_ok_p95_ms": _round(_percentile(lat_ok, 95), 1),
        "cpu_p50_pct": _round(_percentile(cpu, 50)),
        "cpu_p95_pct": _round(_percentile(cpu, 95)),
        "cpu_user_p95_pct": _round(_percentile(user, 95)),
        "cpu_sys_p95_pct": _round(_percentile(system, 95)),
        "load1_p95": _round(_percentile(load1, 95)),
        "dram_bw_p50_gbps": _round(_percentile(dram_bw, 50)),
        "dram_bw_p95_gbps": _round(_percentile(dram_bw, 95)),
        "dram_bw_max_gbps": _round(dram_bw_max),
        "dram_bw_max_pct_of_peak": _round(
            100 * dram_bw_max / DRAM_PEAK_BW_GBPS if dram_bw_max is not None else None
        ),
        "dram_read_bw_p95_gbps": _round(_percentile(dram_rd, 95)),
        "dram_write_bw_p95_gbps": _round(_percentile(dram_wr, 95)),
        "gpu_util_p50_pct": _round(_percentile(gpu, 50)),
        "gpu_util_p95_pct": _round(_positive_percentile(gpu, 95)),
        "gpu_util_max_pct": _round(max(gpu) if gpu else None),
        "gpu_active_sample_pct": _round(_active_pct(gpu)),
        "gpu_memctrl_p50_pct": _round(_percentile(gpu_memctrl, 50)),
        "gpu_memctrl_p95_pct": _round(_positive_percentile(gpu_memctrl, 95)),
        "gpu_memctrl_max_pct": _round(max(gpu_memctrl) if gpu_memctrl else None),
        "gpu_memctrl_active_sample_pct": _round(_active_pct(gpu_memctrl)),
        "gpu_power_p95_w": _round(_positive_percentile(gpu_power, 95)),
        "gpu_mem_used_p95_mib": _round(_positive_percentile(gpu_mem_used, 95)),
        "gpu_mem_used_pct_p95": _round(_positive_percentile(gpu_mem_used_pct, 95)),
        "gpu_mem_used_pct_max": _round(max(gpu_mem_used_pct) if gpu_mem_used_pct else None),
        "gr_engine_active_p95_pct": _round(_positive_percentile(gr_engine_active, 95)),
        "gr_engine_active_max_pct": _round(max(gr_engine_active) if gr_engine_active else None),
        "sm_active_p95_pct": _round(_positive_percentile(sm_active, 95)),
        "sm_active_max_pct": _round(max(sm_active) if sm_active else None),
        "sm_active_avg_pct": _round(_average(sm_active)),
        "sm_active_sample_pct": _round(_active_pct(sm_active)),
        "sm_active_area": _round(_area(sm_active)),
        "sm_occupancy_p95_pct": _round(_positive_percentile(sm_occupancy, 95)),
        "sm_occupancy_max_pct": _round(max(sm_occupancy) if sm_occupancy else None),
        "tensor_active_p95_pct": _round(_positive_percentile(tensor_active, 95)),
        "tensor_active_max_pct": _round(max(tensor_active) if tensor_active else None),
        "tensor_active_avg_pct": _round(_average(tensor_active)),
        "tensor_active_sample_pct": _round(_active_pct(tensor_active)),
        "tensor_active_area": _round(_area(tensor_active)),
        "dram_active_p95_pct": _round(_positive_percentile(dram_active, 95)),
        "dram_active_max_pct": _round(max(dram_active) if dram_active else None),
        "dram_active_avg_pct": _round(_average(dram_active)),
        "dram_active_sample_pct": _round(_active_pct(dram_active)),
        "dram_active_area": _round(_area(dram_active)),
        "fp32_active_p95_pct": _round(_positive_percentile(fp32_active, 95)),
        "fp32_active_max_pct": _round(max(fp32_active) if fp32_active else None),
        "fp16_active_p95_pct": _round(_positive_percentile(fp16_active, 95)),
        "fp16_active_max_pct": _round(max(fp16_active) if fp16_active else None),
        "memory_workers": _unique_join(item.get("memory_workers") for item in result_records),
        "memory_mode": _unique_join(item.get("memory_mode") for item in result_records),
        "cpu_samples": len(cpu),
        "pcm_samples": len(dram_bw),
        "gpu_samples": len(gpu),
        "dcgm_samples": len(sm_active or tensor_active or dram_active or gr_engine_active),
    }


def _agent_dirs(root: Path) -> list[Path]:
    return sorted(root.glob("agents_*"), key=lambda path: int(path.name.split("_")[1]))


def _dcgm_metrics_path(point_path: Path) -> Path:
    gpu_path = point_path / "metrics" / "gpu.csv"
    if gpu_path.exists():
        try:
            header = gpu_path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        except IndexError:
            header = ""
        if "sm_active_pct" in header or "dcgm_metrics_backend" in header:
            return gpu_path
    return point_path / "metrics" / "dcgm.csv"


def _read_float_col(path: Path, column: str) -> list[float]:
    values: list[float] = []
    if not path.exists():
        return values
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                values.append(float(str(row[column]).strip()))
            except (KeyError, TypeError, ValueError):
                pass
    return values


def _read_gpu_memory_used_pct(path: Path) -> list[float]:
    values: list[float] = []
    if not path.exists():
        return values
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            explicit = str(row.get("memory_used_pct", "")).strip()
            if explicit:
                try:
                    values.append(float(explicit))
                    continue
                except ValueError:
                    pass
            try:
                used = float(str(row["memory_used_mib"]).strip())
                total = float(str(row["memory_total_mib"]).strip())
            except (KeyError, TypeError, ValueError):
                continue
            if total > 0:
                values.append(100 * used / total)
    return values


def _read_pcm_metric(path: Path, metric: str) -> list[float]:
    values: list[float] = []
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if metric not in line or "|" not in line:
            continue
        try:
            values.append(float(line.split("|")[1].strip()))
        except (IndexError, ValueError):
            pass
    return values


def _read_traces(results_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(results_dir.glob("*.trace.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _read_result_records(results_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(results_dir.glob("*.result.json")):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return rows


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _positive_percentile(values: list[float], percentile: float) -> float | None:
    return _percentile([value for value in values if value > 0], percentile)


def _active_pct(values: list[float]) -> float | None:
    if not values:
        return None
    return 100 * sum(1 for value in values if value > 0) / len(values)


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _area(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values)


def _round(value: float | None, digits: int = 2) -> float | str:
    if value is None:
        return ""
    return round(float(value), digits)


def _unique_join(values: object) -> str:
    return ",".join(str(value) for value in sorted({value for value in values if value is not None}))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ALIGNED_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _markdown_table(rows: list[dict[str, Any]]) -> str:
    fields = [
        "agents",
        "vm_results",
        "completed",
        "failed",
        "success_rate_pct",
        "throughput_task_per_min_run",
        "lat_all_p50_ms",
        "lat_all_p95_ms",
        "cpu_p95_pct",
        "dram_bw_p95_gbps",
        "gpu_util_p95_pct",
        "gpu_util_max_pct",
        "gpu_active_sample_pct",
        "gpu_memctrl_p95_pct",
        "gpu_memctrl_max_pct",
        "gpu_memctrl_active_sample_pct",
        "gpu_power_p95_w",
        "gpu_mem_used_pct_p95",
        "sm_active_p95_pct",
        "tensor_active_p95_pct",
        "dram_active_p95_pct",
        "memory_workers",
        "memory_mode",
    ]
    lines = [
        "| " + " | ".join(fields) + " |",
        "| " + " | ".join(["---"] * len(fields)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(field, "")) for field in fields) + " |")
    return "\n".join(lines) + "\n"
