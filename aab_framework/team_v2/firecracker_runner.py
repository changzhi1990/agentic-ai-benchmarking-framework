from __future__ import annotations

import json
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

from aab_framework.executors.firecracker import prepare_firecracker_executor_run

from .schemas import AgentWorkerSpec, IssueExecutionPlan, TeamRunConfig


def prepare_firecracker_team_plan(
    config: TeamRunConfig,
    run_id: str,
    run_dir: Path,
    workers: list[AgentWorkerSpec],
    plans: list[IssueExecutionPlan],
) -> dict[str, Any]:
    if not config.fc_kernel or not config.fc_rootfs:
        raise ValueError("fc_kernel and fc_rootfs are required for Firecracker runs")
    manifest = prepare_firecracker_executor_run(
        out_dir=run_dir,
        vm_count=len(workers),
        kernel_image=config.fc_kernel,
        base_rootfs_image=config.fc_rootfs,
        host_vllm_url=config.guest_vllm_base_url,
        vcpu_count=config.firecracker_vcpu_count,
        mem_mib=config.firecracker_mem_mib,
        tasks_per_vm=1,
        request_workers=1,
        workload_seconds=config.firecracker_run_seconds,
        llm_context_kb=config.context_length // 1024,
        llm_load_mode="single_task",
        llm_request_timeout_seconds=config.request_timeout_sec,
        workload_name="mini_swe_agent_team_v2",
    )
    task_root = run_dir / "tasks"
    indexed_plans = {index: plan for index, plan in enumerate(plans)}
    for index, agent in enumerate(manifest["agents"]):
        short_socket = _short_socket_path(run_id, agent["vm_id"])
        agent["socket_path"] = short_socket
        agent["firecracker_command"] = f"firecracker --api-sock {short_socket} --config-file {agent['config_path']}"
        plan = indexed_plans[index]
        task_dir = task_root / agent["vm_id"]
        task_dir.mkdir(parents=True, exist_ok=True)
        task_spec_path = task_dir / "task.json"
        task_spec = _task_spec(config, run_id, agent["vm_id"], plan)
        task_spec_path.write_text(
            json.dumps(task_spec, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        agent.update(
            {
                "agent_id": plan.agent_id,
                "issue_id": plan.issue_id,
                "task_id": plan.task_id,
                "task_spec_host_path": str(task_spec_path),
                "task_spec_guest_path": "/task/task.json",
                "output_guest_dir": "/output",
                "collected_output_dir": str(run_dir / "agents" / agent["vm_id"]),
            }
        )
    manifest.update(
        {
            "run_id": run_id,
            "workload": "mini_swe_agent_team_v2",
            "runner": config.fc_runner,
            "task_spec_transport": "rootfs_task_json",
            "guest_output_dir": "/output",
            "guest_vllm_base_url": config.guest_vllm_base_url,
            "host_vllm_base_url": config.vllm_base_url,
        }
    )
    (run_dir / "firecracker-run.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _short_socket_path(run_id: str, vm_id: str) -> str:
    digest = hashlib.sha1(f"{run_id}:{vm_id}".encode("utf-8")).hexdigest()[:12]
    return f"/tmp/aab-{digest}-{vm_id}.sock"


def run_prepared_firecracker_team(config: TeamRunConfig, run_dir: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "RUN_DIR": str(run_dir),
            "RUN_SECONDS": str(config.firecracker_run_seconds),
            "RESULTS_DIR": str(run_dir / "results"),
            "AGENTS_OUTPUT_DIR": str(run_dir / "agents"),
        }
    )
    runner = Path(config.fc_runner)
    command = [str(runner if runner.is_absolute() else Path.cwd() / runner)]
    return subprocess.run(
        command,
        cwd=Path.cwd(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def collect_firecracker_team_results(
    run_dir: Path,
    manifest: dict[str, Any],
    plans: list[IssueExecutionPlan],
) -> tuple[list[dict[str, Any]], list[str]]:
    issues: list[dict[str, Any]] = []
    errors: list[str] = []
    by_vm = {agent["vm_id"]: agent for agent in manifest.get("agents", [])}
    by_agent_id = {plan.agent_id: plan for plan in plans}
    for vm_id, agent in sorted(by_vm.items()):
        plan = by_agent_id.get(agent.get("agent_id", ""))
        if plan is None:
            continue
        output_dir = run_dir / "agents" / vm_id
        result_path = output_dir / "result.json"
        if not result_path.exists():
            errors.append(f"{vm_id}/{plan.issue_id}: missing guest result.json")
            issues.append(_failed_issue(plan, output_dir, "missing guest result.json"))
            continue
        try:
            guest = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{vm_id}/{plan.issue_id}: invalid guest result.json: {exc}")
            issues.append(_failed_issue(plan, output_dir, "invalid guest result.json"))
            continue
        issues.append(_issue_from_guest(plan, output_dir, guest))
    return issues, errors


def _task_spec(config: TeamRunConfig, run_id: str, vm_id: str, plan: IssueExecutionPlan) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "agent_id": plan.agent_id,
        "vm_id": vm_id,
        "issue_id": plan.issue_id,
        "task_id": plan.task_id,
        "repo": plan.repo,
        "prompt": plan.prompt,
        "model": config.model,
        "vllm_base_url": config.guest_vllm_base_url,
        "host_vllm_base_url": config.vllm_base_url,
        "context_length": config.context_length,
        "max_rounds": config.max_rounds_per_issue,
        "candidate_per_issue": config.candidate_per_issue,
        "output_dir": "/output",
        "work_dir": "/work",
        "task_source": config.task_source,
        "swebench_instance": None,
        "mini_command": config.mini_command,
        "adapter_mode": config.adapter_mode,
    }


def _issue_from_guest(plan: IssueExecutionPlan, output_dir: Path, guest: dict[str, Any]) -> dict[str, Any]:
    verified = bool(guest.get("verified") or guest.get("status") in {"verified_success", "passed", "ok"})
    status = "verified_success" if verified else "failed"
    test_status = str(guest.get("test_status") or ("passed" if verified else "failed"))
    return {
        "issue_id": plan.issue_id,
        "task_id": plan.task_id,
        "repo": plan.repo,
        "agent_id": plan.agent_id,
        "status": status,
        "started_at": guest.get("started_at", ""),
        "ended_at": guest.get("ended_at", ""),
        "latency_sec": float(guest.get("latency_sec", 0) or 0),
        "rounds": [
            {
                "round_id": 0,
                "candidate_id": "candidate-0",
                "diagnosis_path": str(output_dir / "diagnosis.txt"),
                "patch_path": str(output_dir / "patch.diff"),
                "diff_path": str(output_dir / "patch.diff"),
                "apply_status": guest.get("apply_status", "applied"),
                "test_status": test_status,
                "review_status": guest.get("review_status", "approved" if verified else "warning"),
                "review_result": guest.get("review_result", {}),
                "verifier_result": guest.get("verifier_result", {}),
                "verifier_score": float(guest.get("verifier_score", 1.0 if verified else 0.0) or 0),
                "retry_reason": None,
                "requested_context_length": int(guest.get("context_length", 0) or 0) or None,
                "effective_context_length": int(guest.get("context_length", 0) or 0) or None,
                "context_source": "TeamRunConfig.context_length",
                "verified_context_length": False,
                "verification_method": "assumed",
                "stage_timings": {
                    "prepare_repo_sec": 0,
                    "build_context_sec": 0,
                    "llm_wait_sec": 0,
                    "llm_generation_sec": float(guest.get("generation_latency_sec", 0) or 0),
                    "patch_apply_sec": 0,
                    "test_sec": 0,
                    "review_sec": 0,
                    "verify_sec": 0,
                    "result_write_sec": 0,
                },
                "logs": {
                    "stdout": str(output_dir / "stdout.log"),
                    "stderr": str(output_dir / "stderr.log"),
                    "test_log": str(output_dir / "test.log"),
                },
            }
        ],
        "final_patch_path": str(output_dir / "patch.diff"),
        "final_test_log_path": str(output_dir / "test.log"),
        "verified": verified,
        "error": guest.get("error"),
    }


def _failed_issue(plan: IssueExecutionPlan, output_dir: Path, error: str) -> dict[str, Any]:
    return {
        "issue_id": plan.issue_id,
        "task_id": plan.task_id,
        "repo": plan.repo,
        "agent_id": plan.agent_id,
        "status": "failed",
        "started_at": "",
        "ended_at": "",
        "latency_sec": 0,
        "rounds": [],
        "final_patch_path": str(output_dir / "patch.diff"),
        "final_test_log_path": str(output_dir / "test.log"),
        "verified": False,
        "error": error,
    }
