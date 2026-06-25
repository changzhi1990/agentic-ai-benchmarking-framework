from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from aab_framework.agent_team import ExecutorSpec
from aab_framework.firecracker import (
    FirecrackerPaths,
    build_firecracker_command,
    plan_agents,
    spec_to_dict,
    write_vm_config,
)


def build_firecracker_executor_spec() -> ExecutorSpec:
    return ExecutorSpec(
        name="firecracker",
        isolation="microvm",
        artifacts=(
            "kernel",
            "rootfs",
            "firecracker_config",
            "api_socket",
            "tap_device",
        ),
        result_files=(
            "result.json",
            "trace.jsonl",
        ),
        supports_cpu_pinning=True,
        supports_numa_binding=True,
    )


def prepare_firecracker_executor_run(
    *,
    out_dir: str | Path,
    vm_count: int,
    kernel_image: str | Path,
    base_rootfs_image: str | Path,
    host_vllm_url: str,
    guest_ip_prefix: str = "172.16.0",
    host_ip: str = "172.16.0.1",
    guest_netmask: str = "255.255.0.0",
    vcpu_count: int = 2,
    mem_mib: int = 1024,
    tasks_per_vm: int = 1,
    request_workers: int = 1,
    workload_seconds: int = 60,
    memory_workers: int = 4,
    memory_mb: int = 512,
    memory_rounds: int = 16,
    memory_mode: str = "read",
    llm_context_kb: int = 0,
    llm_prompt_repeat: int = 1,
    llm_max_tokens: int = 512,
    llm_load_mode: str = "single_task",
    llm_request_timeout_seconds: int = 120,
    llm_inter_task_sleep_ms: int = 0,
    workload_name: str = "coding",
) -> dict[str, Any]:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    kernel_path = Path(kernel_image)
    base_rootfs_path = Path(base_rootfs_image)

    specs = plan_agents(
        vm_count=vm_count,
        base_id="agent",
        host_vllm_url=host_vllm_url,
        guest_ip_prefix=guest_ip_prefix,
        host_ip=host_ip,
        guest_netmask=guest_netmask,
        vcpu_count=vcpu_count,
        mem_mib=mem_mib,
        tasks_per_vm=tasks_per_vm,
        request_workers=request_workers,
        workload_seconds=workload_seconds,
        memory_workers=memory_workers,
        memory_mb=memory_mb,
        memory_rounds=memory_rounds,
        memory_mode=memory_mode,
        llm_context_kb=llm_context_kb,
        llm_prompt_repeat=llm_prompt_repeat,
        llm_max_tokens=llm_max_tokens,
        llm_load_mode=llm_load_mode,
        llm_request_timeout_seconds=llm_request_timeout_seconds,
        llm_inter_task_sleep_ms=llm_inter_task_sleep_ms,
    )

    agents = []
    for spec in specs:
        rootfs_path = output_dir / f"{spec.vm_id}.rootfs.ext4"
        shutil.copyfile(base_rootfs_path, rootfs_path)
        socket_path = output_dir / f"{spec.vm_id}.socket"
        config_path = output_dir / f"{spec.vm_id}.json"
        paths = FirecrackerPaths(
            kernel_image=str(kernel_path),
            rootfs_image=str(rootfs_path),
            socket_path=str(socket_path),
        )
        write_vm_config(config_path, spec, paths)
        item = spec_to_dict(spec)
        item.update(
            {
                "kernel_image": str(kernel_path),
                "rootfs_image": str(rootfs_path),
                "socket_path": str(socket_path),
                "config_path": str(config_path),
                "log_path": str(output_dir / f"{spec.vm_id}.log"),
                "firecracker_command": build_firecracker_command(
                    "firecracker",
                    str(socket_path),
                    str(config_path),
                ),
            }
        )
        agents.append(item)

    manifest = {
        "executor": "firecracker",
        "workload": workload_name,
        "vm_count": vm_count,
        "host_vllm_url": host_vllm_url,
        "guest_netmask": guest_netmask,
        "kernel_image": str(kernel_path),
        "base_rootfs_image": str(base_rootfs_path),
        "tasks_per_vm": tasks_per_vm,
        "request_workers": request_workers,
        "workload_seconds": workload_seconds,
        "memory_workers": memory_workers,
        "memory_mb": memory_mb,
        "memory_rounds": memory_rounds,
        "memory_mode": memory_mode,
        "llm_context_kb": llm_context_kb,
        "llm_prompt_repeat": llm_prompt_repeat,
        "llm_max_tokens": llm_max_tokens,
        "llm_load_mode": llm_load_mode,
        "llm_request_timeout_seconds": llm_request_timeout_seconds,
        "llm_inter_task_sleep_ms": llm_inter_task_sleep_ms,
        "agents": agents,
    }
    (output_dir / "firecracker-run.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
