from __future__ import annotations

from aab_framework.agent_team import AgentRoleSpec, AgentTeamSpec, WorkloadSpec
from aab_framework.team_v2 import TeamOrchestrator, TeamRunConfig


def build_mini_swe_agent_team_v2_workload_spec() -> WorkloadSpec:
    return WorkloadSpec(
        name="mini_swe_agent_team_v2",
        description="Production-like mini-swe-agent Agent Team v2 workload with coordinator, workers, review, verification, repair, metrics, and UI reporting.",
        default_team=AgentTeamSpec(
            name="mini-swe-agent-team-v2",
            roles=(
                AgentRoleSpec("coordinator", "Create run, schedule issues, manage lifecycle.", produces=("team_run",)),
                AgentRoleSpec("planner", "Plan worker and issue assignments.", consumes=("tasks",), produces=("issue_plan",)),
                AgentRoleSpec("worker", "Run mini-swe-agent issue lifecycle.", consumes=("issue_plan",), produces=("candidate",)),
                AgentRoleSpec("reviewer", "Review patches and logs.", consumes=("candidate",), produces=("review_result",)),
                AgentRoleSpec("verifier", "Verify synthetic or pytest result.", consumes=("candidate",), produces=("verifier_result",)),
                AgentRoleSpec("repair_loop", "Decide if another round is needed.", consumes=("review_result", "verifier_result")),
                AgentRoleSpec("metrics_observer", "Wrap existing metrics outputs.", produces=("metrics_summary",)),
                AgentRoleSpec("aggregator", "Aggregate team, issue, and agent results.", produces=("result_json",)),
            ),
            challenge_role="reviewer",
        ),
        base_metrics=(
            "completed_tasks",
            "failed_tasks",
            "success_rate_pct",
            "verified_success_rate",
            "issue_latency_p95_sec",
        ),
        business_metrics=("verified_success_issues", "total_rounds", "review_pass_rate"),
        executor_specific_fields=(),
    )


def run_mini_swe_agent_team_v2(config: TeamRunConfig) -> dict:
    return TeamOrchestrator(config).run()
