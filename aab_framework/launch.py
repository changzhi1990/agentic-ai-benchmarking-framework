from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VmPlacement:
    cpu_set: str
    numa_node: int | None


def expand_cpu_list(value: str) -> list[int]:
    cpus: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError(f"invalid CPU range: {item}")
            cpus.extend(range(start, end + 1))
        else:
            cpus.append(int(item))
    return cpus


def format_cpu_list(cpus: list[int]) -> str:
    if not cpus:
        return ""
    ranges: list[str] = []
    start = cpus[0]
    previous = cpus[0]
    for cpu in cpus[1:]:
        if cpu == previous + 1:
            previous = cpu
            continue
        ranges.append(_format_range(start, previous))
        start = previous = cpu
    ranges.append(_format_range(start, previous))
    return ",".join(ranges)


def discover_numa_cpu_lists(sys_root: str | Path = "/sys/devices/system/node") -> dict[int, list[int]]:
    root = Path(sys_root)
    nodes: dict[int, list[int]] = {}
    for path in sorted(root.glob("node[0-9]*/cpulist")):
        node_id = int(path.parent.name.removeprefix("node"))
        cpus = expand_cpu_list(path.read_text(encoding="utf-8").strip())
        if cpus:
            nodes[node_id] = cpus
    return nodes


def plan_vm_placement(
    *,
    vm_index: int,
    vcpu_count: int,
    host_cpu_count: int | None = None,
    cpu_pinning: bool = False,
    numa_policy: str = "none",
    numa_cpu_lists: dict[int, list[int]] | None = None,
) -> VmPlacement:
    if vm_index < 0:
        raise ValueError("vm_index must be non-negative")
    if vcpu_count < 1:
        raise ValueError("vcpu_count must be positive")
    if not cpu_pinning:
        return VmPlacement(cpu_set="", numa_node=None)

    if host_cpu_count is None:
        host_cpu_count = os.cpu_count() or 1
    if host_cpu_count < 1:
        raise ValueError("host_cpu_count must be positive")

    if numa_policy == "bind-by-agent":
        nodes = numa_cpu_lists if numa_cpu_lists is not None else discover_numa_cpu_lists()
        if nodes:
            node_ids = sorted(nodes)
            numa_node = node_ids[vm_index % len(node_ids)]
            node_cpus = nodes[numa_node]
            slot = vm_index // len(node_ids)
            return VmPlacement(
                cpu_set=format_cpu_list(_round_robin_slice(node_cpus, slot * vcpu_count, vcpu_count)),
                numa_node=numa_node,
            )

    all_cpus = list(range(host_cpu_count))
    return VmPlacement(
        cpu_set=format_cpu_list(_round_robin_slice(all_cpus, vm_index * vcpu_count, vcpu_count)),
        numa_node=None,
    )


def _round_robin_slice(cpus: list[int], start: int, count: int) -> list[int]:
    if not cpus:
        raise ValueError("cpus must not be empty")
    return [cpus[(start + offset) % len(cpus)] for offset in range(count)]


def _format_range(start: int, end: int) -> str:
    if start == end:
        return str(start)
    return f"{start}-{end}"
