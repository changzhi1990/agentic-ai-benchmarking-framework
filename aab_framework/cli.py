from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent_team import ChallengeAgent, PluginRegistry
from .executors.firecracker import build_firecracker_executor_spec
from .firecracker import (
    FirecrackerPaths,
    build_firecracker_command,
    discover_firecracker,
    plan_agents,
    spec_to_dict,
    write_vm_config,
)
from .firecracker_sweep import prepare_firecracker_run
from .guest_agent import run_noop_agent
from .metrics import summarize_firecracker_sweep
from .workloads.coding import build_coding_workload_spec
from .vllm import (
    VllmDockerConfig,
    build_vllm_container_command,
    build_vllm_healthcheck_command,
    build_vllm_serve_command,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aab-framework")
    subparsers = parser.add_subparsers(dest="command", required=True)

    vllm_parser = subparsers.add_parser("vllm-docker-command")
    vllm_parser.add_argument("--model", required=True)
    vllm_parser.add_argument("--image", default="vllm/vllm-openai:latest")
    vllm_parser.add_argument("--served-model-name", default="agentic-model")
    vllm_parser.add_argument("--api-key", default="token-abc123")
    vllm_parser.add_argument("--tp", type=int, default=8)
    vllm_parser.add_argument("--port", type=int, default=8000)
    vllm_parser.add_argument("--nccl-p2p-level", default="SYS")
    vllm_parser.add_argument("--gpu-memory-utilization", type=float, default=0.9)
    vllm_parser.add_argument("--max-model-len", type=int, default=None)
    vllm_parser.add_argument("--max-num-seqs", type=int, default=128)
    vllm_parser.add_argument("--max-num-batched-tokens", type=int, default=None)

    plan_parser = subparsers.add_parser("plan-firecracker-agents")
    plan_parser.add_argument("--vm-count", type=int, default=1)
    plan_parser.add_argument("--base-id", default="agent")
    plan_parser.add_argument("--host-vllm-url", default="http://172.16.0.1:8000/v1")
    plan_parser.add_argument("--guest-ip-prefix", default="172.16.0")
    plan_parser.add_argument("--host-ip", default="172.16.0.1")
    plan_parser.add_argument("--guest-netmask", default="255.255.0.0")
    plan_parser.add_argument("--kernel-image", required=True)
    plan_parser.add_argument("--rootfs-image", required=True)
    plan_parser.add_argument("--out-dir", required=True)
    plan_parser.add_argument("--vcpu-count", type=int, default=2)
    plan_parser.add_argument("--mem-mib", type=int, default=1024)
    plan_parser.add_argument("--tasks-per-vm", type=int, default=1)
    plan_parser.add_argument("--request-workers", type=int, default=1)
    plan_parser.add_argument("--workload-seconds", type=int, default=60)
    plan_parser.add_argument("--memory-workers", type=int, default=4)
    plan_parser.add_argument("--memory-mb", type=int, default=512)
    plan_parser.add_argument("--memory-rounds", type=int, default=16)
    plan_parser.add_argument("--memory-mode", default="read")
    plan_parser.add_argument("--llm-context-kb", type=int, default=0)
    plan_parser.add_argument("--llm-prompt-repeat", type=int, default=1)
    plan_parser.add_argument("--llm-max-tokens", type=int, default=512)
    plan_parser.add_argument("--llm-load-mode", default="single_task")
    plan_parser.add_argument("--llm-request-timeout-seconds", type=int, default=120)
    plan_parser.add_argument("--llm-inter-task-sleep-ms", type=int, default=0)

    preflight_parser = subparsers.add_parser("firecracker-preflight")
    preflight_parser.add_argument("--firecracker-bin", default=None)
    preflight_parser.add_argument("--kernel-image", required=True)
    preflight_parser.add_argument("--rootfs-image", required=True)

    guest_parser = subparsers.add_parser("guest-noop")
    guest_parser.add_argument("--vm-id", required=True)
    guest_parser.add_argument("--host-vllm-url", required=True)
    guest_parser.add_argument("--output", required=True)

    prepare_parser = subparsers.add_parser("prepare-firecracker-run")
    prepare_parser.add_argument("--out-dir", required=True)
    prepare_parser.add_argument("--vm-count", type=int, required=True)
    prepare_parser.add_argument("--kernel-image", required=True)
    prepare_parser.add_argument("--base-rootfs-image", required=True)
    prepare_parser.add_argument("--host-vllm-url", default="http://172.16.0.1:8000/v1")
    prepare_parser.add_argument("--guest-netmask", default="255.255.0.0")
    prepare_parser.add_argument("--tasks-per-vm", type=int, default=1)
    prepare_parser.add_argument("--request-workers", type=int, default=1)
    prepare_parser.add_argument("--vcpu-count", type=int, default=2)
    prepare_parser.add_argument("--mem-mib", type=int, default=1024)
    prepare_parser.add_argument("--workload-seconds", type=int, default=60)
    prepare_parser.add_argument("--memory-workers", type=int, default=4)
    prepare_parser.add_argument("--memory-mb", type=int, default=512)
    prepare_parser.add_argument("--memory-rounds", type=int, default=16)
    prepare_parser.add_argument("--memory-mode", default="read")
    prepare_parser.add_argument("--llm-context-kb", type=int, default=0)
    prepare_parser.add_argument("--llm-prompt-repeat", type=int, default=1)
    prepare_parser.add_argument("--llm-max-tokens", type=int, default=512)
    prepare_parser.add_argument("--llm-load-mode", default="single_task")
    prepare_parser.add_argument("--llm-request-timeout-seconds", type=int, default=120)
    prepare_parser.add_argument("--llm-inter-task-sleep-ms", type=int, default=0)

    summarize_parser = subparsers.add_parser("summarize-firecracker-sweep")
    summarize_parser.add_argument("--run-root", required=True)
    summarize_parser.add_argument("--run-seconds", type=float, required=True)
    summarize_parser.add_argument("--workload-seconds", type=float, required=True)

    subparsers.add_parser("inspect-agent-team")

    args = parser.parse_args(argv)

    if args.command == "vllm-docker-command":
        print(
            build_vllm_container_command(
                VllmDockerConfig(
                    model=args.model,
                    image=args.image,
                    served_model_name=args.served_model_name,
                    api_key=args.api_key,
                    tensor_parallel_size=args.tp,
                    port=args.port,
                    nccl_p2p_level=args.nccl_p2p_level,
                    gpu_memory_utilization=args.gpu_memory_utilization,
                    max_model_len=args.max_model_len,
                    max_num_seqs=args.max_num_seqs,
                    max_num_batched_tokens=args.max_num_batched_tokens,
                )
            )
        )
        print(
            build_vllm_serve_command(
                VllmDockerConfig(
                    model=args.model,
                    image=args.image,
                    served_model_name=args.served_model_name,
                    api_key=args.api_key,
                    tensor_parallel_size=args.tp,
                    port=args.port,
                    nccl_p2p_level=args.nccl_p2p_level,
                    gpu_memory_utilization=args.gpu_memory_utilization,
                    max_model_len=args.max_model_len,
                    max_num_seqs=args.max_num_seqs,
                    max_num_batched_tokens=args.max_num_batched_tokens,
                )
            )
        )
        print(build_vllm_healthcheck_command(port=args.port, api_key=args.api_key))
        return 0

    if args.command == "plan-firecracker-agents":
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        specs = plan_agents(
            vm_count=args.vm_count,
            base_id=args.base_id,
            host_vllm_url=args.host_vllm_url,
            guest_ip_prefix=args.guest_ip_prefix,
            host_ip=args.host_ip,
            guest_netmask=args.guest_netmask,
            vcpu_count=args.vcpu_count,
            mem_mib=args.mem_mib,
            tasks_per_vm=args.tasks_per_vm,
            request_workers=args.request_workers,
            workload_seconds=args.workload_seconds,
            memory_workers=args.memory_workers,
            memory_mb=args.memory_mb,
            memory_rounds=args.memory_rounds,
            memory_mode=args.memory_mode,
            llm_context_kb=args.llm_context_kb,
            llm_prompt_repeat=args.llm_prompt_repeat,
            llm_max_tokens=args.llm_max_tokens,
            llm_load_mode=args.llm_load_mode,
            llm_request_timeout_seconds=args.llm_request_timeout_seconds,
            llm_inter_task_sleep_ms=args.llm_inter_task_sleep_ms,
        )
        manifest = []
        for spec in specs:
            socket_path = out_dir / f"{spec.vm_id}.socket"
            config_path = out_dir / f"{spec.vm_id}.json"
            paths = FirecrackerPaths(
                kernel_image=args.kernel_image,
                rootfs_image=args.rootfs_image,
                socket_path=str(socket_path),
            )
            write_vm_config(config_path, spec, paths)
            item = spec_to_dict(spec)
            item["config_path"] = str(config_path)
            item["socket_path"] = str(socket_path)
            item["firecracker_command"] = build_firecracker_command(
                "firecracker",
                str(socket_path),
                str(config_path),
            )
            manifest.append(item)
        (out_dir / "agents.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"agents": manifest}, indent=2, sort_keys=True))
        return 0

    if args.command == "firecracker-preflight":
        print(
            json.dumps(
                discover_firecracker(
                    firecracker_bin=args.firecracker_bin,
                    kernel_image=args.kernel_image,
                    rootfs_image=args.rootfs_image,
                ).to_dict(),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "guest-noop":
        print(
            json.dumps(
                run_noop_agent(
                    vm_id=args.vm_id,
                    host_vllm_url=args.host_vllm_url,
                    output_path=args.output,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "prepare-firecracker-run":
        print(
            json.dumps(
                prepare_firecracker_run(
                    out_dir=args.out_dir,
                    vm_count=args.vm_count,
                    kernel_image=args.kernel_image,
                    base_rootfs_image=args.base_rootfs_image,
                    host_vllm_url=args.host_vllm_url,
                    guest_netmask=args.guest_netmask,
                    tasks_per_vm=args.tasks_per_vm,
                    request_workers=args.request_workers,
                    vcpu_count=args.vcpu_count,
                    mem_mib=args.mem_mib,
                    workload_seconds=args.workload_seconds,
                    memory_workers=args.memory_workers,
                    memory_mb=args.memory_mb,
                    memory_rounds=args.memory_rounds,
                    memory_mode=args.memory_mode,
                    llm_context_kb=args.llm_context_kb,
                    llm_prompt_repeat=args.llm_prompt_repeat,
                    llm_max_tokens=args.llm_max_tokens,
                    llm_load_mode=args.llm_load_mode,
                    llm_request_timeout_seconds=args.llm_request_timeout_seconds,
                    llm_inter_task_sleep_ms=args.llm_inter_task_sleep_ms,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "summarize-firecracker-sweep":
        print(
            json.dumps(
                summarize_firecracker_sweep(
                    args.run_root,
                    run_seconds=args.run_seconds,
                    workload_seconds=args.workload_seconds,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "inspect-agent-team":
        registry = PluginRegistry()
        workload = build_coding_workload_spec()
        executor = build_firecracker_executor_spec()
        registry.register_workload(workload)
        registry.register_executor(executor)
        challenge = ChallengeAgent()
        payload = {
            "workloads": {
                name: _workload_to_dict(registry.workload(name))
                for name in registry.workload_names()
            },
            "executors": {
                name: _executor_to_dict(registry.executor(name))
                for name in registry.executor_names()
            },
            "challenge_reviews": {
                "workloads": {
                    workload.name: _review_to_dict(challenge.review_workload(workload))
                },
                "executors": {
                    executor.name: _review_to_dict(challenge.review_executor(executor))
                },
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    return 1


def _workload_to_dict(workload) -> dict:
    return {
        "name": workload.name,
        "description": workload.description,
        "team": {
            "name": workload.default_team.name,
            "challenge_role": workload.default_team.challenge_role,
            "roles": [
                {
                    "name": role.name,
                    "responsibility": role.responsibility,
                    "consumes": list(role.consumes),
                    "produces": list(role.produces),
                }
                for role in workload.default_team.roles
            ],
        },
        "base_metrics": list(workload.base_metrics),
        "business_metrics": list(workload.business_metrics),
    }


def _executor_to_dict(executor) -> dict:
    return {
        "name": executor.name,
        "isolation": executor.isolation,
        "artifacts": list(executor.artifacts),
        "result_files": list(executor.result_files),
        "supports_cpu_pinning": executor.supports_cpu_pinning,
        "supports_numa_binding": executor.supports_numa_binding,
    }


def _review_to_dict(review) -> dict:
    return {
        "verdict": review.verdict,
        "blocked": review.blocked,
        "blockers": [finding.message for finding in review.blockers],
        "warnings": [finding.message for finding in review.warnings],
    }


if __name__ == "__main__":
    raise SystemExit(main())
