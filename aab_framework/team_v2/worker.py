from __future__ import annotations

from dataclasses import replace
import time
from pathlib import Path
from typing import Any

from .mini_swe_adapter import MiniSweAgentAdapter
from .repo_context import RepoContextBuilder, RepoWorkspaceManager
from .repair_loop import RepairLoopController
from .reviewer import PatchReviewerAgent
from .schemas import ContextMetadata, IssueExecutionPlan, TeamRunConfig
from .verifier import TestVerifierAgent


class SWEWorkerAgent:
    def __init__(self, config: TeamRunConfig, adapter: MiniSweAgentAdapter) -> None:
        self.config = config
        self.adapter = adapter
        self.reviewer = PatchReviewerAgent()
        self.verifier = TestVerifierAgent()
        self.repair_loop = RepairLoopController()

    def run_issue(self, plan: IssueExecutionPlan, run_dir: Path) -> dict[str, Any]:
        issue_dir = run_dir / "issues" / plan.issue_id
        issue_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        rounds: list[dict[str, Any]] = []
        status = "failed"
        verified = False
        error = None
        final_patch_path = ""
        final_test_log_path = ""
        repo_workspace = RepoWorkspaceManager(self.config).prepare(plan, issue_dir)
        workspace_plan = replace(plan, workdir=repo_workspace.path if repo_workspace.prepared else plan.workdir)
        repo_context = RepoContextBuilder(self.config).build(workspace_plan, issue_dir)
        execution_plan = replace(
            workspace_plan,
            prompt=repo_context.augment_prompt(workspace_plan.prompt, max_chars=self.config.repo_context_prompt_max_chars),
        )

        for round_id in range(plan.max_rounds):
            round_dir = issue_dir / f"round-{round_id}"
            round_started = time.time()
            adapter_result = self.adapter.run_issue_round(execution_plan, round_id=round_id, output_dir=round_dir)
            verify_started = time.time()
            if execution_plan.verify_command and execution_plan.workdir:
                verifier = self.verifier.verify_command(
                    ["bash", "-lc", execution_plan.verify_command],
                    cwd=Path(execution_plan.workdir).expanduser(),
                    test_log_path=round_dir / "verify.log",
                    timeout=self.config.verify_timeout_sec,
                )
                review_log_path = Path(verifier.test_log_path)
            else:
                verifier = self.verifier.verify_synthetic(adapter_result.test_log_path)
                review_log_path = adapter_result.test_log_path
            verify_ended = time.time()
            review_started = time.time()
            review = self.reviewer.review(adapter_result.patch_path, review_log_path)
            review_ended = time.time()
            context = ContextMetadata(
                requested_context_length=self.config.context_length,
                effective_context_length=self.config.context_length,
            )
            retry, retry_reason = self.repair_loop.should_retry(
                round_id=round_id,
                review=review,
                verifier=verifier,
                config=self.config,
            )
            rounds.append(
                {
                    "round_id": round_id,
                    "candidate_id": f"{plan.issue_id}-candidate-{round_id}",
                    "diagnosis_path": str(adapter_result.diagnosis_path),
                    "patch_path": str(adapter_result.patch_path),
                    "diff_path": str(adapter_result.diff_path),
                    "apply_status": "applied" if adapter_result.returncode == 0 else "failed",
                    "test_status": verifier.test_status,
                    "review_status": review.review_status,
                    "review_result": review.to_dict(),
                    "verifier_result": verifier.to_dict(),
                    "verifier_score": verifier.verifier_score,
                    "retry_reason": retry_reason,
                    "number_of_llm_calls": adapter_result.number_of_llm_calls,
                    "generation_latency_sec": adapter_result.generation_latency_sec,
                    "stage_timings": {
                        "prepare_repo_sec": 0,
                        "prepare_workspace_sec": repo_workspace.prepare_repo_sec if round_id == 0 else 0,
                        "scan_repo_sec": repo_context.scan_repo_sec if round_id == 0 else 0,
                        "build_context_sec": repo_context.build_context_sec if round_id == 0 else 0,
                        "llm_wait_sec": 0,
                        "llm_generation_sec": round(adapter_result.generation_latency_sec, 3),
                        "patch_apply_sec": 0,
                        "test_sec": 0,
                        "review_sec": round(review_ended - review_started, 3),
                        "verify_sec": round(verify_ended - verify_started, 3),
                        "result_write_sec": round(time.time() - round_started, 3),
                    },
                    "repo_workspace": repo_workspace.to_dict(),
                    "repo_context": repo_context.to_dict(),
                    **context.to_dict(),
                    "logs": {
                        "stdout": adapter_result.stdout[-2000:],
                        "stderr": adapter_result.stderr[-2000:],
                        "test_log": str(adapter_result.test_log_path),
                    },
                }
            )
            final_patch_path = str(adapter_result.patch_path)
            final_test_log_path = str(adapter_result.test_log_path)
            if verifier.verified and review.review_status in {"approved", "warning"}:
                status = "verified_success"
                verified = True
                break
            if not retry:
                status = "review_rejected" if review.review_status == "rejected" else "test_failed"
                error = adapter_result.error
                break

        ended = time.time()
        return {
            "issue_id": plan.issue_id,
            "task_id": plan.task_id,
            "repo": plan.repo,
            "agent_id": plan.agent_id,
            "status": status,
            "started_at": _iso(started),
            "ended_at": _iso(ended),
            "latency_sec": round(ended - started, 3),
            "rounds": rounds,
            "repo_workspace": repo_workspace.to_dict(),
            "repo_context": repo_context.to_dict(),
            "final_patch_path": final_patch_path,
            "final_test_log_path": final_test_log_path,
            "verified": verified,
            "error": error,
        }


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
