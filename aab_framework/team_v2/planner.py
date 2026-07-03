from __future__ import annotations

import json
from pathlib import Path

from .schemas import AgentWorkerSpec, IssueExecutionPlan, TeamRunConfig


class TaskPlanner:
    def build_workers(self, config: TeamRunConfig) -> list[AgentWorkerSpec]:
        return [
            AgentWorkerSpec(
                agent_id=f"agent-{index}",
                requested_context_length=config.context_length,
                effective_context_length=config.context_length,
            )
            for index in range(config.num_agents)
        ]

    def build_issue_plans(self, config: TeamRunConfig, workers: list[AgentWorkerSpec]) -> list[IssueExecutionPlan]:
        tasks = self._load_tasks(config)
        plans: list[IssueExecutionPlan] = []
        for index, task in enumerate(tasks):
            worker = workers[index % len(workers)]
            issue_id = str(task.get("issue_id") or f"synthetic-{index}")
            plans.append(
                IssueExecutionPlan(
                    issue_id=issue_id,
                    task_id=str(task.get("task_id") or issue_id),
                    repo=str(task.get("repo") or "synthetic"),
                    prompt=str(task.get("prompt") or _default_synthetic_prompt(issue_id)),
                    agent_id=worker.agent_id,
                    max_rounds=config.max_rounds_per_issue,
                    candidate_per_issue=config.candidate_per_issue,
                    synthetic_expected_file=task.get("expected_file"),
                    workdir=task.get("workdir"),
                    verify_command=task.get("verify_command"),
                )
            )
        return plans

    def _load_tasks(self, config: TeamRunConfig) -> list[dict]:
        if config.instances_file:
            return _read_instances_file(Path(config.instances_file))
        if config.task_source == "local":
            task_count = max(config.num_agents, 1)
            return [
                {
                    "issue_id": f"local-{index}",
                    "task_id": f"local-task-{index}",
                    "repo": str(config.repo_source),
                    "prompt": str(config.task),
                    "workdir": str(config.repo_source),
                    "verify_command": config.verify_command,
                }
                for index in range(task_count)
            ]
        task_count = max(config.num_agents, 1)
        return [
            {
                "issue_id": f"synthetic-{index}",
                "task_id": f"synthetic-task-{index}",
                "repo": "synthetic",
                "prompt": _default_synthetic_prompt(str(index)),
                "expected_file": "/tmp/aab_mini_swe_agent_team_v2_ok.txt",
            }
            for index in range(task_count)
        ]


def _read_instances_file(path: Path) -> list[dict]:
    tasks: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("{"):
            tasks.append(json.loads(line))
        else:
            tasks.append({"issue_id": line, "task_id": line, "repo": "swebench", "prompt": line})
    return tasks or [{"issue_id": "empty-input", "task_id": "empty-input", "repo": "synthetic"}]


def _default_synthetic_prompt(issue_id: str) -> str:
    return (
        f"Synthetic issue {issue_id}: create /tmp/aab_mini_swe_agent_team_v2_ok.txt "
        "with content ok, verify it, and finish cleanly."
    )
