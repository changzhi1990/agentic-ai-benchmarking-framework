from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SweepPoint:
    agents: int
    vm_count: int
    tasks_per_vm: int
    request_workers: int
    total_tasks: int
    vcpu_per_vm: int = 2
    mem_mib_per_vm: int = 1024


def plan_role_separated_sweep(agent_counts: list[int]) -> list[SweepPoint]:
    points = []
    for agents in agent_counts:
        vm_count = _vm_count_for_agents(agents)
        tasks_per_vm = max(1, (agents + vm_count - 1) // vm_count)
        points.append(
            SweepPoint(
                agents=agents,
                vm_count=vm_count,
                tasks_per_vm=tasks_per_vm,
                request_workers=min(2, agents),
                total_tasks=vm_count * tasks_per_vm,
            )
        )
    return points


def _vm_count_for_agents(agents: int) -> int:
    if agents <= 0:
        raise ValueError("agents must be positive")
    if agents <= 8:
        return agents
    if agents <= 32:
        return 8
    if agents <= 128:
        return 16
    return 32
