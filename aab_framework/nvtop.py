from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any


GPU_CSV_FIELDS = [
    "timestamp",
    "index",
    "utilization_gpu_pct",
    "utilization_memory_pct",
    "memory_used_mib",
    "memory_total_mib",
    "power_draw_w",
    "temperature_c",
    "memory_used_pct",
    "gpu_metrics_backend",
]


def parse_nvtop_snapshot(snapshot: str, *, timestamp: int | str) -> list[dict[str, str]]:
    devices = json.loads(snapshot)
    if not isinstance(devices, list):
        raise ValueError("nvtop snapshot must be a JSON list")

    rows = []
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            continue
        rows.append(
            {
                "timestamp": str(timestamp),
                "index": str(index),
                "utilization_gpu_pct": _unit_number(device.get("gpu_util"), "%"),
                # nvtop's NVIDIA mem_util is VRAM occupancy, not NVML utilization.memory.
                "utilization_memory_pct": "",
                "memory_used_mib": _bytes_to_mib(device.get("mem_used")),
                "memory_total_mib": _bytes_to_mib(device.get("mem_total")),
                "power_draw_w": _unit_number(device.get("power_draw"), "W"),
                "temperature_c": _unit_number(device.get("temp"), "C"),
                "memory_used_pct": _unit_number(device.get("mem_util"), "%"),
                "gpu_metrics_backend": "nvtop",
            }
        )
    return rows


def format_gpu_csv_rows(rows: list[dict[str, str]]) -> str:
    lines = []
    for row in rows:
        lines.append(",".join(str(row.get(field, "")) for field in GPU_CSV_FIELDS))
    return "\n".join(lines)


def _unit_number(value: Any, suffix: str) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() == "null" or not text:
        return ""
    if text.endswith(suffix):
        text = text[: -len(suffix)]
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return "" if not match else match.group(0)


def _bytes_to_mib(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() == "null" or not text:
        return ""
    try:
        return str(round(int(text) / 1024 / 1024, 2))
    except ValueError:
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m aab_framework.nvtop")
    parser.add_argument("--timestamp", required=True)
    args = parser.parse_args(argv)

    snapshot = sys.stdin.read()
    print(format_gpu_csv_rows(parse_nvtop_snapshot(snapshot, timestamp=args.timestamp)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
