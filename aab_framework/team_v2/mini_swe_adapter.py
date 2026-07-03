from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .docker_runtime import DockerRuntime
from .schemas import IssueExecutionPlan, MiniSweAgentResult, TeamRunConfig


class MiniSweAgentAdapter:
    def __init__(self, config: TeamRunConfig) -> None:
        self.config = config

    def run_issue_round(self, plan: IssueExecutionPlan, *, round_id: int, output_dir: Path) -> MiniSweAgentResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        if self.config.adapter_mode == "mock":
            return self._run_mock(plan, round_id=round_id, output_dir=output_dir)
        return self._run_cli(plan, round_id=round_id, output_dir=output_dir)

    def _run_mock(self, plan: IssueExecutionPlan, *, round_id: int, output_dir: Path) -> MiniSweAgentResult:
        started = time.time()
        diagnosis_path = output_dir / "diagnosis.txt"
        patch_path = output_dir / "patch.diff"
        diff_path = output_dir / "diff.patch"
        test_log_path = output_dir / "test.log"
        diagnosis_path.write_text(f"diagnosis for {plan.issue_id} round {round_id}\n", encoding="utf-8")
        patch_text = (
            "diff --git a/synthetic.py b/synthetic.py\n"
            "--- a/synthetic.py\n"
            "+++ b/synthetic.py\n"
            "@@ -0,0 +1 @@\n"
            "+ok\n"
        )
        patch_path.write_text(patch_text, encoding="utf-8")
        diff_path.write_text(patch_text, encoding="utf-8")
        test_log_path.write_text("1 passed\n", encoding="utf-8")
        return MiniSweAgentResult(
            status="completed",
            stdout="mock mini-swe-agent completed\n",
            stderr="",
            returncode=0,
            output_dir=output_dir,
            diagnosis_path=diagnosis_path,
            patch_path=patch_path,
            diff_path=diff_path,
            test_log_path=test_log_path,
            number_of_llm_calls=1,
            generation_latency_sec=time.time() - started,
        )

    def _run_cli(self, plan: IssueExecutionPlan, *, round_id: int, output_dir: Path) -> MiniSweAgentResult:
        started = time.time()
        trajectory_path = output_dir / "trajectory.json"
        stdout_path = output_dir / "mini_stdout.log"
        stderr_path = output_dir / "mini_stderr.log"
        diagnosis_path = output_dir / "diagnosis.txt"
        patch_path = output_dir / "patch.diff"
        diff_path = output_dir / "diff.patch"
        test_log_path = output_dir / "test.log"
        command_trajectory = str(trajectory_path)
        if self.config.runtime_type == "docker":
            command_trajectory = f"{self.config.runtime_workdir.rstrip('/')}/output/trajectory.json"
        command = [
            self.config.mini_command,
            "-y",
            "--exit-immediately",
            "--task",
            plan.prompt,
            "--model",
            self.config.model,
            "--output",
            command_trajectory,
            "--config",
            "mini_textbased.yaml",
            "--model-class",
            "minisweagent.models.litellm_textbased_model.LitellmTextbasedModel",
            "--config",
            f"model.model_kwargs.api_base={_openai_base_url(self.config.vllm_base_url)}",
            "--config",
            "model.model_kwargs.drop_params=true",
            "--config",
            "model.cost_tracking=ignore_errors",
            "--config",
            "agent.cost_limit=0",
        ]
        if self.config.runtime_type == "docker":
            runtime = DockerRuntime(self.config)
            host_workdir = Path(plan.workdir).expanduser() if plan.workdir else None
            command = runtime.build_one_shot_command(
                container_name=runtime.container_name(
                    run_id="team-v2-cli",
                    issue_id=f"{plan.issue_id}-round-{round_id}",
                    agent_id=plan.agent_id,
                ),
                host_output_dir=output_dir,
                host_workdir=host_workdir,
                command=command,
            )
        cwd = Path(plan.workdir).expanduser() if plan.workdir and self.config.runtime_type != "docker" else None
        result = subprocess.run(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=self.config.request_timeout_sec,
            check=False,
        )
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        diagnosis_path.write_text(result.stdout[-4000:], encoding="utf-8")
        diff_text = _git_diff(Path(plan.workdir).expanduser()) if plan.workdir else ""
        patch_path.write_text(diff_text, encoding="utf-8")
        diff_path.write_text(diff_text, encoding="utf-8")
        test_log_path.write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
        status = "completed" if result.returncode == 0 and "RepeatedFormatError" not in result.stdout else "failed"
        return MiniSweAgentResult(
            status=status,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            output_dir=output_dir,
            diagnosis_path=diagnosis_path,
            patch_path=patch_path,
            diff_path=diff_path,
            test_log_path=test_log_path,
            number_of_llm_calls=max(1, result.stdout.count("mini-swe-agent (step")),
            generation_latency_sec=time.time() - started,
            error=None if status == "completed" else "mini-swe-agent did not complete cleanly",
        )


def _openai_base_url(base_url: str) -> str:
    stripped = base_url.rstrip("/")
    return stripped if stripped.endswith("/v1") else f"{stripped}/v1"


def _git_diff(workdir: Path) -> str:
    if not workdir.exists():
        return ""
    result = subprocess.run(
        ["git", "diff", "--no-ext-diff"],
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""
