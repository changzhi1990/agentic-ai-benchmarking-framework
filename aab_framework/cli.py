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
from .team_v2 import DEFAULT_CONTEXT_LENGTH, TeamRunConfig
from .team_v2.sweep import TeamSweepOrchestrator
from .workloads.coding import build_coding_workload_spec
from .workloads.mini_swe_agent_team_v2 import (
    build_mini_swe_agent_team_v2_workload_spec,
    run_mini_swe_agent_team_v2,
)
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
    vllm_parser.add_argument("--enable-auto-tool-choice", action="store_true")
    vllm_parser.add_argument("--tool-call-parser", default=None)

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

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--workload", required=True)
    run_parser.add_argument("--num-agents", type=int, default=1)
    run_parser.add_argument("--agent-counts", default="")
    run_parser.add_argument("--agent-sweep", default="")
    run_parser.add_argument("--parallelism", type=int, default=1)
    run_parser.add_argument("--max-rounds-per-issue", type=int, default=2)
    run_parser.add_argument("--candidate-per-issue", type=int, default=1)
    run_parser.add_argument("--context-length", type=int, default=DEFAULT_CONTEXT_LENGTH)
    run_parser.add_argument("--context-lengths", default="")
    run_parser.add_argument("--experiment-name", default="agent_scaling_test")
    run_parser.add_argument("--experiment-mode", default="fixed_llm", choices=("fixed_llm", "unlimited_llm"))
    run_parser.add_argument("--repeats", type=int, default=1)
    run_parser.add_argument("--fixed-llm-requests", type=int, default=8)
    run_parser.add_argument("--max-active-llm-requests", type=int, default=None)
    run_parser.add_argument("--max-active-prefill-tokens", type=int, default=None)
    run_parser.add_argument("--vllm-max-model-len", type=int, default=4096)
    run_parser.add_argument("--vllm-max-num-seqs", type=int, default=128)
    run_parser.add_argument("--vllm-max-num-batched-tokens", type=int, default=16384)
    run_parser.add_argument("--vllm-gpu-memory-utilization", type=float, default=0.9)
    run_parser.add_argument("--vllm-tensor-parallel-size", type=int, default=8)
    run_parser.add_argument("--vllm-dtype", default="bfloat16")
    run_parser.add_argument("--disable-vllm-prefix-caching", action="store_true")
    run_parser.add_argument("--vllm-base-url", default="http://127.0.0.1:8000/v1")
    run_parser.add_argument("--model", default="agentic-model")
    run_parser.add_argument("--task-source", default="synthetic")
    run_parser.add_argument("--task", default=None)
    run_parser.add_argument("--verify-command", default=None)
    run_parser.add_argument("--verify-timeout-sec", type=int, default=120)
    run_parser.add_argument("--instances-file", default=None)
    run_parser.add_argument("--out-dir", default="runs")
    run_parser.add_argument("--adapter-mode", default="mock", choices=("mock", "cli"))
    run_parser.add_argument("--mini-command", default="mini")
    run_parser.add_argument("--mini-swe-agent-repo", default="third_party/mini-swe-agent")
    run_parser.add_argument("--runtime-type", default="docker", choices=("docker", "process", "firecracker"))
    run_parser.add_argument("--repo-context-enabled", action="store_true")
    run_parser.add_argument("--repo-source", default=None)
    run_parser.add_argument("--repo-context-max-files", type=int, default=20000)
    run_parser.add_argument("--repo-context-max-bytes", type=int, default=1024 * 1024 * 1024)
    run_parser.add_argument("--repo-context-bundle-max-bytes", type=int, default=4 * 1024 * 1024)
    run_parser.add_argument("--repo-context-prompt-max-chars", type=int, default=8192)
    run_parser.add_argument("--repo-context-extensions", default=".py,.js,.jsx,.ts,.tsx,.go,.rs,.c,.cc,.cpp,.h,.hpp,.java,.md,.toml,.yaml,.yml,.json")
    run_parser.add_argument("--repo-workspace-mode", default="source", choices=("source", "copy", "worktree"))
    run_parser.add_argument("--repo-workspace-cleanup", action="store_true")
    run_parser.add_argument("--repo-context-include-git-history", action="store_true")
    run_parser.add_argument("--repo-context-git-history-max-bytes", type=int, default=512 * 1024 * 1024)
    run_parser.add_argument("--repo-context-git-log-limit", type=int, default=1000)
    run_parser.add_argument("--repo-context-pytest-collect", action="store_true")
    run_parser.add_argument("--repo-context-pytest-command", default="python -m pytest --collect-only -q")
    run_parser.add_argument("--repo-context-pytest-timeout-sec", type=int, default=120)
    run_parser.add_argument("--ui-host", default="0.0.0.0")
    run_parser.add_argument("--ui-port", type=int, default=80)
    run_parser.add_argument("--ui-fallback-port", type=int, default=8080)
    run_parser.add_argument("--disable-ui-port-fallback", action="store_true")
    run_parser.add_argument("--use-firecracker", nargs="?", const=True, default=False, type=_parse_bool)
    run_parser.add_argument("--fc-rootfs", default=None)
    run_parser.add_argument("--fc-kernel", default=None)
    run_parser.add_argument("--fc-runner", default="bin/run_prepared_firecracker_agents.sh")
    run_parser.add_argument("--guest-vllm-base-url", default="http://172.16.0.1:8000/v1")
    run_parser.add_argument("--firecracker-run-seconds", type=int, default=120)
    run_parser.add_argument("--firecracker-vcpu-count", type=int, default=2)
    run_parser.add_argument("--firecracker-mem-mib", type=int, default=4096)

    sweep_parser = subparsers.add_parser("sweep")
    sweep_parser.add_argument("--workload", required=True)
    sweep_parser.add_argument("--agent-counts", default="1,2,4")
    sweep_parser.add_argument("--context-lengths", default=str(DEFAULT_CONTEXT_LENGTH))
    sweep_parser.add_argument("--parallelism", type=int, default=1)
    sweep_parser.add_argument("--max-rounds-per-issue", type=int, default=2)
    sweep_parser.add_argument("--candidate-per-issue", type=int, default=1)
    sweep_parser.add_argument("--experiment-name", default="agent_scaling_test")
    sweep_parser.add_argument("--experiment-mode", default="fixed_llm", choices=("fixed_llm", "unlimited_llm"))
    sweep_parser.add_argument("--repeats", type=int, default=1)
    sweep_parser.add_argument("--fixed-llm-requests", type=int, default=8)
    sweep_parser.add_argument("--max-active-llm-requests", type=int, default=None)
    sweep_parser.add_argument("--max-active-prefill-tokens", type=int, default=None)
    sweep_parser.add_argument("--vllm-base-url", default="http://127.0.0.1:8000/v1")
    sweep_parser.add_argument("--model", default="agentic-model")
    sweep_parser.add_argument("--task-source", default="synthetic")
    sweep_parser.add_argument("--task", default=None)
    sweep_parser.add_argument("--verify-command", default=None)
    sweep_parser.add_argument("--verify-timeout-sec", type=int, default=120)
    sweep_parser.add_argument("--instances-file", default=None)
    sweep_parser.add_argument("--out-dir", default="runs")
    sweep_parser.add_argument("--adapter-mode", default="mock", choices=("mock", "cli"))
    sweep_parser.add_argument("--mini-command", default="mini")
    sweep_parser.add_argument("--mini-swe-agent-repo", default="third_party/mini-swe-agent")
    sweep_parser.add_argument("--runtime-type", default="docker", choices=("docker", "process", "firecracker"))
    sweep_parser.add_argument("--repo-context-enabled", action="store_true")
    sweep_parser.add_argument("--repo-source", default=None)
    sweep_parser.add_argument("--repo-context-max-files", type=int, default=20000)
    sweep_parser.add_argument("--repo-context-max-bytes", type=int, default=1024 * 1024 * 1024)
    sweep_parser.add_argument("--repo-context-bundle-max-bytes", type=int, default=4 * 1024 * 1024)
    sweep_parser.add_argument("--repo-context-prompt-max-chars", type=int, default=8192)
    sweep_parser.add_argument("--repo-context-extensions", default=".py,.js,.jsx,.ts,.tsx,.go,.rs,.c,.cc,.cpp,.h,.hpp,.java,.md,.toml,.yaml,.yml,.json")
    sweep_parser.add_argument("--repo-workspace-mode", default="source", choices=("source", "copy", "worktree"))
    sweep_parser.add_argument("--repo-workspace-cleanup", action="store_true")
    sweep_parser.add_argument("--repo-context-include-git-history", action="store_true")
    sweep_parser.add_argument("--repo-context-git-history-max-bytes", type=int, default=512 * 1024 * 1024)
    sweep_parser.add_argument("--repo-context-git-log-limit", type=int, default=1000)
    sweep_parser.add_argument("--repo-context-pytest-collect", action="store_true")
    sweep_parser.add_argument("--repo-context-pytest-command", default="python -m pytest --collect-only -q")
    sweep_parser.add_argument("--repo-context-pytest-timeout-sec", type=int, default=120)
    sweep_parser.add_argument("--ui-host", default="0.0.0.0")
    sweep_parser.add_argument("--ui-port", type=int, default=80)
    sweep_parser.add_argument("--ui-fallback-port", type=int, default=8080)
    sweep_parser.add_argument("--disable-ui-port-fallback", action="store_true")
    sweep_parser.add_argument("--vllm-max-model-len", type=int, default=4096)
    sweep_parser.add_argument("--vllm-max-num-seqs", type=int, default=128)
    sweep_parser.add_argument("--vllm-max-num-batched-tokens", type=int, default=16384)
    sweep_parser.add_argument("--vllm-gpu-memory-utilization", type=float, default=0.9)
    sweep_parser.add_argument("--vllm-tensor-parallel-size", type=int, default=8)
    sweep_parser.add_argument("--vllm-dtype", default="bfloat16")
    sweep_parser.add_argument("--disable-vllm-prefix-caching", action="store_true")

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
                    enable_auto_tool_choice=args.enable_auto_tool_choice,
                    tool_call_parser=args.tool_call_parser,
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
                    enable_auto_tool_choice=args.enable_auto_tool_choice,
                    tool_call_parser=args.tool_call_parser,
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

    if args.command == "run":
        if args.workload not in {"mini_swe_agent_team_v2", "mini_swe_agent_team"}:
            raise SystemExit(f"unsupported workload for run command: {args.workload}")
        config = _team_run_config_from_args(args, context_length=args.context_length)
        agent_counts = _parse_agent_counts(args.agent_sweep or args.agent_counts)
        context_lengths = _parse_context_lengths(args.context_lengths) if args.context_lengths else [args.context_length]
        result = (
            TeamSweepOrchestrator(
                config,
                agent_counts,
                context_lengths=context_lengths,
                repeats=args.repeats,
                experiment_mode=args.experiment_mode,
                max_active_llm_requests=args.max_active_llm_requests,
                max_active_prefill_tokens=args.max_active_prefill_tokens,
            ).run()
            if agent_counts
            else run_mini_swe_agent_team_v2(config)
        )
        summary = result["summary"]
        if args.agent_sweep or agent_counts:
            print(f"Sweep Group: {result['run_id']}")
            for item in result.get("runs", []):
                print(
                    "Run: "
                    f"case_id={item.get('case_id')} "
                    f"num_agents={item['num_agents']} "
                    f"context_length={item.get('context_length')} "
                    f"repeat={item.get('repeat')} "
                    f"run_id={item['run_id']} "
                    f"result={item['result_path']}"
                )
        _print_team_result(result, summary)
        return 0

    if args.command == "sweep":
        if args.workload not in {"mini_swe_agent_team_v2", "mini_swe_agent_team"}:
            raise SystemExit(f"unsupported workload for sweep command: {args.workload}")
        agent_counts = _parse_agent_counts(args.agent_counts)
        context_lengths = _parse_context_lengths(args.context_lengths)
        config = _team_run_config_from_args(args, context_length=context_lengths[0])
        result = TeamSweepOrchestrator(
            config,
            agent_counts,
            context_lengths=context_lengths,
            repeats=args.repeats,
            experiment_mode=args.experiment_mode,
            max_active_llm_requests=args.max_active_llm_requests,
            max_active_prefill_tokens=args.max_active_prefill_tokens,
        ).run()
        summary = result["summary"]
        print(f"Sweep Group: {result['run_id']}")
        print(f"Experiment name: {args.experiment_name}")
        print(f"Experiment mode: {args.experiment_mode}")
        for item in result.get("runs", []):
            print(
                "Run: "
                f"case_id={item.get('case_id')} "
                f"num_agents={item['num_agents']} "
                f"context_length={item.get('context_length')} "
                f"repeat={item.get('repeat')} "
                f"run_id={item['run_id']} "
                f"result={item['result_path']}"
            )
        _print_team_result(result, summary)
        return 0

    if args.command == "inspect-agent-team":
        registry = PluginRegistry()
        workload = build_coding_workload_spec()
        mini_workload = build_mini_swe_agent_team_v2_workload_spec()
        executor = build_firecracker_executor_spec()
        registry.register_workload(workload)
        registry.register_workload(mini_workload)
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
                    workload.name: _review_to_dict(challenge.review_workload(workload)),
                    mini_workload.name: _review_to_dict(challenge.review_workload(mini_workload)),
                },
                "executors": {
                    executor.name: _review_to_dict(challenge.review_executor(executor))
                },
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    return 1


def _team_run_config_from_args(args, *, context_length: int) -> TeamRunConfig:
    return TeamRunConfig(
        num_agents=getattr(args, "num_agents", 1),
        parallelism=args.parallelism,
        max_rounds_per_issue=args.max_rounds_per_issue,
        candidate_per_issue=args.candidate_per_issue,
        context_length=context_length,
        vllm_base_url=args.vllm_base_url,
        model=args.model,
        vllm_max_model_len=args.vllm_max_model_len,
        vllm_max_num_seqs=args.vllm_max_num_seqs,
        vllm_max_num_batched_tokens=args.vllm_max_num_batched_tokens,
        vllm_gpu_memory_utilization=args.vllm_gpu_memory_utilization,
        vllm_tensor_parallel_size=args.vllm_tensor_parallel_size,
        vllm_dtype=args.vllm_dtype,
        vllm_prefix_caching=not args.disable_vllm_prefix_caching,
        task_source=args.task_source,
        task=args.task,
        verify_command=args.verify_command,
        verify_timeout_sec=args.verify_timeout_sec,
        out_dir=args.out_dir,
        adapter_mode=args.adapter_mode,
        mini_command=args.mini_command,
        mini_swe_agent_repo=args.mini_swe_agent_repo,
        runtime_type=args.runtime_type,
        repo_context_enabled=args.repo_context_enabled,
        repo_source=args.repo_source,
        repo_context_max_files=args.repo_context_max_files,
        repo_context_max_bytes=args.repo_context_max_bytes,
        repo_context_bundle_max_bytes=args.repo_context_bundle_max_bytes,
        repo_context_prompt_max_chars=args.repo_context_prompt_max_chars,
        repo_context_extensions=tuple(item.strip() for item in args.repo_context_extensions.split(",") if item.strip()),
        repo_workspace_mode=args.repo_workspace_mode,
        repo_workspace_cleanup=args.repo_workspace_cleanup,
        repo_context_include_git_history=args.repo_context_include_git_history,
        repo_context_git_history_max_bytes=args.repo_context_git_history_max_bytes,
        repo_context_git_log_limit=args.repo_context_git_log_limit,
        repo_context_pytest_collect=args.repo_context_pytest_collect,
        repo_context_pytest_command=args.repo_context_pytest_command,
        repo_context_pytest_timeout_sec=args.repo_context_pytest_timeout_sec,
        experiment_name=args.experiment_name,
        experiment_mode=args.experiment_mode,
        repeats=args.repeats,
        fixed_llm_requests=args.fixed_llm_requests,
        max_active_llm_requests=args.max_active_llm_requests,
        max_active_prefill_tokens=args.max_active_prefill_tokens,
        ui_host=args.ui_host,
        ui_port=args.ui_port,
        ui_fallback_port=args.ui_fallback_port,
        ui_enable_port_fallback=not args.disable_ui_port_fallback,
        instances_file=args.instances_file,
        use_firecracker=getattr(args, "use_firecracker", False),
        fc_rootfs=getattr(args, "fc_rootfs", None),
        fc_kernel=getattr(args, "fc_kernel", None),
        fc_runner=getattr(args, "fc_runner", "bin/run_prepared_firecracker_agents.sh"),
        guest_vllm_base_url=getattr(args, "guest_vllm_base_url", "http://172.16.0.1:8000/v1"),
        firecracker_run_seconds=getattr(args, "firecracker_run_seconds", 120),
        firecracker_vcpu_count=getattr(args, "firecracker_vcpu_count", 2),
        firecracker_mem_mib=getattr(args, "firecracker_mem_mib", 4096),
    )


def _print_team_result(result: dict, summary: dict) -> None:
    print(f"Run ID: {result['run_id']}")
    print(f"Run dir: {result['run_dir']}")
    print(f"Result: {result['result_path']}")
    print(f"Total issues: {summary.get('total_issues', 0)}")
    print(f"Verified success rate: {summary.get('verified_success_rate', summary.get('best_verified_success_rate', 0))}")
    print(f"Failed issues: {summary.get('failed_issues', 0)}")
    print(f"Timeout issues: {summary.get('timeout_issues', 0)}")
    print(f"Requested UI port: {result['ui']['requested_port']}")
    print(f"Actual UI port: {result['ui']['actual_port']}")
    print(f"Fallback used: {str(result['ui']['fallback_used']).lower()}")
    print(f"UI available at: {result['ui']['url']}")


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


def _parse_agent_counts(value: str) -> list[int]:
    if not value.strip():
        return []
    counts = []
    for item in value.replace(",", " ").split():
        count = int(item)
        if count <= 0:
            raise ValueError("agent counts must be positive")
        counts.append(count)
    return counts


def _parse_context_lengths(value: str) -> list[int]:
    lengths = []
    for item in value.replace(",", " ").split():
        length = int(item)
        if length <= 0:
            raise ValueError("context lengths must be positive")
        lengths.append(length)
    if not lengths:
        raise ValueError("context_lengths must not be empty")
    return lengths


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean value: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
