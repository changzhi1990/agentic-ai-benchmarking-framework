from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from shlex import quote
from typing import Any


@dataclass(frozen=True)
class FirecrackerAgentSpec:
    vm_id: str
    tap_name: str
    guest_ip: str
    host_ip: str
    host_vllm_url: str
    vcpu_count: int = 2
    mem_mib: int = 1024
    tasks_per_vm: int = 1
    request_workers: int = 1


@dataclass(frozen=True)
class FirecrackerPaths:
    kernel_image: str
    rootfs_image: str
    socket_path: str


@dataclass(frozen=True)
class FirecrackerPreflight:
    firecracker_bin: str | None
    kernel_image: str
    rootfs_image: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "firecracker_bin": self.firecracker_bin,
            "firecracker_available": bool(self.firecracker_bin),
            "kernel_image": self.kernel_image,
            "kernel_exists": Path(self.kernel_image).exists(),
            "rootfs_image": self.rootfs_image,
            "rootfs_exists": Path(self.rootfs_image).exists(),
            "ready": bool(self.firecracker_bin)
            and Path(self.kernel_image).exists()
            and Path(self.rootfs_image).exists(),
        }


def plan_agents(
    *,
    vm_count: int,
    base_id: str,
    host_vllm_url: str,
    guest_ip_prefix: str,
    host_ip: str = "172.16.0.1",
    vcpu_count: int = 2,
    mem_mib: int = 1024,
    tasks_per_vm: int = 1,
    request_workers: int = 1,
) -> list[FirecrackerAgentSpec]:
    if vm_count < 1:
        raise ValueError("vm_count must be >= 1")
    specs = []
    for index in range(vm_count):
        vm_id = f"{base_id}-{index:03d}"
        specs.append(
            FirecrackerAgentSpec(
                vm_id=vm_id,
                tap_name=f"tap-{vm_id}",
                guest_ip=f"{guest_ip_prefix}.{10 + index}",
                host_ip=host_ip,
                host_vllm_url=host_vllm_url,
                vcpu_count=vcpu_count,
                mem_mib=mem_mib,
                tasks_per_vm=tasks_per_vm,
                request_workers=request_workers,
            )
        )
    return specs


def build_vm_config(spec: FirecrackerAgentSpec, paths: FirecrackerPaths) -> dict[str, Any]:
    boot_args = " ".join(
        [
            "console=ttyS0",
            "reboot=k",
            "panic=1",
            "pci=off",
            f"ip={spec.guest_ip}::{spec.host_ip}:255.255.255.0::eth0:off",
            f"agent.vm_id={spec.vm_id}",
            f"agent.guest_ip={spec.guest_ip}",
            f"agent.host_ip={spec.host_ip}",
            f"agent.host_vllm_url={spec.host_vllm_url}",
            f"agent.tasks_per_vm={spec.tasks_per_vm}",
            f"agent.request_workers={spec.request_workers}",
        ]
    )
    return {
        "boot-source": {
            "kernel_image_path": paths.kernel_image,
            "boot_args": boot_args,
        },
        "drives": [
            {
                "drive_id": "rootfs",
                "path_on_host": paths.rootfs_image,
                "is_root_device": True,
                "is_read_only": False,
            }
        ],
        "machine-config": {
            "vcpu_count": spec.vcpu_count,
            "mem_size_mib": spec.mem_mib,
            "smt": False,
        },
        "network-interfaces": [
            {
                "iface_id": "eth0",
                "host_dev_name": spec.tap_name,
                "guest_mac": _stable_mac(spec.vm_id),
            }
        ],
        "metadata": {
            "vm_id": spec.vm_id,
            "guest_ip": spec.guest_ip,
            "host_ip": spec.host_ip,
            "host_vllm_url": spec.host_vllm_url,
            "tasks_per_vm": spec.tasks_per_vm,
            "request_workers": spec.request_workers,
        },
    }


def write_vm_config(path: str | Path, spec: FirecrackerAgentSpec, paths: FirecrackerPaths) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(build_vm_config(spec, paths), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def discover_firecracker(
    *,
    firecracker_bin: str | None,
    kernel_image: str,
    rootfs_image: str,
) -> FirecrackerPreflight:
    discovered = firecracker_bin or shutil.which("firecracker")
    return FirecrackerPreflight(
        firecracker_bin=discovered,
        kernel_image=kernel_image,
        rootfs_image=rootfs_image,
    )


def build_firecracker_command(firecracker_bin: str, socket_path: str, config_path: str) -> str:
    return (
        f"{quote(firecracker_bin)} "
        f"--api-sock {quote(socket_path)} "
        f"--config-file {quote(config_path)}"
    )


def spec_to_dict(spec: FirecrackerAgentSpec) -> dict[str, Any]:
    return asdict(spec)


def _stable_mac(vm_id: str) -> str:
    value = sum((index + 1) * ord(char) for index, char in enumerate(vm_id))
    return "02:FC:%02x:%02x:%02x:%02x" % (
        (value >> 24) & 0xFF,
        (value >> 16) & 0xFF,
        (value >> 8) & 0xFF,
        value & 0xFF,
    )
