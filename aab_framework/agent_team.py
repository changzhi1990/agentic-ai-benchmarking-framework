from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ReviewVerdict = Literal["PASS", "PASS_WITH_WARNINGS", "BLOCKED"]


@dataclass(frozen=True)
class AgentRoleSpec:
    name: str
    responsibility: str
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentTeamSpec:
    name: str
    roles: tuple[AgentRoleSpec, ...]
    challenge_role: str | None = None

    @property
    def role_names(self) -> tuple[str, ...]:
        return tuple(role.name for role in self.roles)


@dataclass(frozen=True)
class WorkloadSpec:
    name: str
    description: str
    default_team: AgentTeamSpec
    base_metrics: tuple[str, ...]
    business_metrics: tuple[str, ...] = ()
    executor_specific_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutorSpec:
    name: str
    isolation: str
    artifacts: tuple[str, ...]
    result_files: tuple[str, ...]
    supports_cpu_pinning: bool = False
    supports_numa_binding: bool = False


@dataclass(frozen=True)
class ChallengeFinding:
    severity: Literal["BLOCKER", "WARNING"]
    message: str


@dataclass(frozen=True)
class ChallengeReview:
    verdict: ReviewVerdict
    findings: tuple[ChallengeFinding, ...]

    @property
    def blockers(self) -> tuple[ChallengeFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == "BLOCKER")

    @property
    def warnings(self) -> tuple[ChallengeFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == "WARNING")

    @property
    def blocked(self) -> bool:
        return bool(self.blockers)


class PluginRegistry:
    def __init__(self) -> None:
        self._workloads: dict[str, WorkloadSpec] = {}
        self._executors: dict[str, ExecutorSpec] = {}

    def register_workload(self, workload: WorkloadSpec) -> None:
        self._workloads[workload.name] = workload

    def register_executor(self, executor: ExecutorSpec) -> None:
        self._executors[executor.name] = executor

    def workload(self, name: str) -> WorkloadSpec:
        return self._workloads[name]

    def executor(self, name: str) -> ExecutorSpec:
        return self._executors[name]

    def workload_names(self) -> list[str]:
        return sorted(self._workloads)

    def executor_names(self) -> list[str]:
        return sorted(self._executors)


class ChallengeAgent:
    def review_workload(self, workload: WorkloadSpec) -> ChallengeReview:
        findings: list[ChallengeFinding] = []
        if workload.executor_specific_fields:
            fields = ", ".join(workload.executor_specific_fields)
            findings.append(
                ChallengeFinding(
                    "BLOCKER",
                    f"Workload exposes executor-specific fields: {fields}. Keep sandbox details in executors.",
                )
            )
        if "success_rate_pct" not in workload.base_metrics:
            findings.append(ChallengeFinding("BLOCKER", "Workload must expose success_rate_pct as a base metric."))
        if workload.default_team.challenge_role and workload.default_team.challenge_role not in workload.default_team.role_names:
            findings.append(ChallengeFinding("BLOCKER", "Challenge role must be declared in the agent team roles."))
        if not workload.default_team.challenge_role:
            findings.append(ChallengeFinding("WARNING", "Workload has no challenge role in its default team."))
        return _review_from_findings(findings)

    def review_executor(self, executor: ExecutorSpec) -> ChallengeReview:
        findings: list[ChallengeFinding] = []
        if not executor.isolation:
            findings.append(ChallengeFinding("BLOCKER", "Executor must declare its isolation boundary."))
        if "trace.jsonl" not in executor.result_files:
            findings.append(ChallengeFinding("BLOCKER", "Executor must collect trace.jsonl for agent observability."))
        if "result.json" not in executor.result_files:
            findings.append(ChallengeFinding("BLOCKER", "Executor must collect result.json for task outcomes."))
        return _review_from_findings(findings)


def _review_from_findings(findings: list[ChallengeFinding]) -> ChallengeReview:
    if any(finding.severity == "BLOCKER" for finding in findings):
        verdict: ReviewVerdict = "BLOCKED"
    elif findings:
        verdict = "PASS_WITH_WARNINGS"
    else:
        verdict = "PASS"
    return ChallengeReview(verdict, tuple(findings))
