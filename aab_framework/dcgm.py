from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from io import StringIO
from typing import Iterable


DCGM_FIELD_MAP = {
    "150": "temperature_c",
    "155": "power_draw_w",
    "203": "utilization_gpu_pct",
    "204": "utilization_memory_pct",
    "250": "memory_total_mib",
    "252": "memory_used_mib",
    "1001": "gr_engine_active_pct",
    "1002": "sm_active_pct",
    "1003": "sm_occupancy_pct",
    "1004": "tensor_active_pct",
    "1005": "dram_active_pct",
    "1007": "fp32_active_pct",
    "1008": "fp16_active_pct",
}

DCGM_CSV_FIELDS = [
    "timestamp",
    "index",
    "utilization_gpu_pct",
    "utilization_memory_pct",
    "memory_used_mib",
    "memory_total_mib",
    "power_draw_w",
    "temperature_c",
    "memory_used_pct",
    "gr_engine_active_pct",
    "sm_active_pct",
    "sm_occupancy_pct",
    "tensor_active_pct",
    "dram_active_pct",
    "fp32_active_pct",
    "fp16_active_pct",
    "gpu_metrics_backend",
    "dcgm_metrics_backend",
]

PCT_FIELDS = {
    "utilization_gpu_pct",
    "utilization_memory_pct",
    "gr_engine_active_pct",
    "sm_active_pct",
    "sm_occupancy_pct",
    "tensor_active_pct",
    "dram_active_pct",
    "fp32_active_pct",
    "fp16_active_pct",
}


def parse_dcgm_dmon_lines(
    lines: Iterable[str],
    *,
    field_ids: str,
    timestamp: int | None = None,
) -> list[dict[str, str]]:
    fields = _fields_for_ids(field_ids)
    rows: list[dict[str, str]] = []
    ts = str(int(time.time() if timestamp is None else timestamp))
    for line in lines:
        parsed = _parse_data_line(line)
        if parsed is None:
            continue
        index, values = parsed
        row = {field: "" for field in DCGM_CSV_FIELDS}
        row["timestamp"] = ts
        row["index"] = str(index)
        row["gpu_metrics_backend"] = "dcgmi"
        row["dcgm_metrics_backend"] = "dcgmi"
        for name, value in zip(fields, values, strict=False):
            if name in row:
                row[name] = _format_value(name, value)
        _fill_memory_used_pct(row)
        rows.append(row)
    return rows


def format_dcgm_csv_rows(rows: list[dict[str, str]]) -> str:
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=DCGM_CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _fields_for_ids(field_ids: str) -> list[str]:
    fields: list[str] = []
    for item in re.split(r"[\s,]+", field_ids.strip()):
        if not item:
            continue
        fields.append(DCGM_FIELD_MAP.get(item, f"field_{item}"))
    return fields


def _parse_data_line(line: str) -> tuple[int, list[float]] | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    parts = text.split()
    if parts[0].upper() == "GPU" and len(parts) >= 3 and parts[1].isdigit():
        value_parts = parts[2:]
        index = int(parts[1])
    elif parts[0].isdigit() and len(parts) >= 2:
        value_parts = parts[1:]
        index = int(parts[0])
    else:
        return None
    values: list[float] = []
    for item in value_parts:
        try:
            values.append(float(item))
        except ValueError:
            values.append(0.0)
    return index, values


def _format_value(name: str, value: float) -> str:
    if name in PCT_FIELDS:
        value = value * 100 if 0 <= value <= 1 else value
    return _format_number(value)


def _format_number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") if value % 1 else f"{value:.1f}"


def _fill_memory_used_pct(row: dict[str, str]) -> None:
    if row.get("memory_used_pct"):
        return
    try:
        used = float(row.get("memory_used_mib") or "")
        total = float(row.get("memory_total_mib") or "")
    except ValueError:
        return
    if total > 0:
        row["memory_used_pct"] = _format_number(100 * used / total)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aab-dcgm")
    parser.add_argument("--field-ids", required=True)
    parser.add_argument("--timestamp", type=int, default=None)
    args = parser.parse_args(argv)
    rows = parse_dcgm_dmon_lines(sys.stdin, field_ids=args.field_ids, timestamp=args.timestamp)
    if rows:
        sys.stdout.write(format_dcgm_csv_rows(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
