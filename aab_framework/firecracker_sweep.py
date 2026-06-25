from __future__ import annotations

from pathlib import Path
from typing import Any

from .executors.firecracker import prepare_firecracker_executor_run


def prepare_firecracker_run(
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
) -> dict[str, Any]:
    return prepare_firecracker_executor_run(
        out_dir=out_dir,
        vm_count=vm_count,
        kernel_image=kernel_image,
        base_rootfs_image=base_rootfs_image,
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
        workload_name="coding",
    )
