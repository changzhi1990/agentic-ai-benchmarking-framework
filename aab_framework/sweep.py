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


def plan_role_separated_sweep(agent_counts: list[int], *, agents_per_vm: int = 1) -> list[SweepPoint]:
    if agents_per_vm <= 0:
        raise ValueError("agents_per_vm must be positive")
    points = []
    for agents in agent_counts:
        if agents <= 0:
            raise ValueError("agents must be positive")
        tasks_per_vm = _tasks_per_vm_for_agents(agents, agents_per_vm)
        vm_count = agents // tasks_per_vm
        points.append(
            SweepPoint(
                agents=agents,
                vm_count=vm_count,
                tasks_per_vm=tasks_per_vm,
                request_workers=1,
                total_tasks=agents,
            )
        )
    return points


def _tasks_per_vm_for_agents(agents: int, agents_per_vm: int) -> int:
    limit = min(agents, agents_per_vm)
    for value in range(limit, 0, -1):
        if agents % value == 0:
            return value
    return 1
