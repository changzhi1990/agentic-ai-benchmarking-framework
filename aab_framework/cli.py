from __future__ import annotations

import argparse
import json
from pathlib import Path

from .firecracker import (
    FirecrackerPaths,
    build_firecracker_command,
    discover_firecracker,
    plan_agents,
    spec_to_dict,
    write_vm_config,
)
from .guest_agent import run_noop_agent
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

    plan_parser = subparsers.add_parser("plan-firecracker-agents")
    plan_parser.add_argument("--vm-count", type=int, default=1)
    plan_parser.add_argument("--base-id", default="agent")
    plan_parser.add_argument("--host-vllm-url", default="http://172.16.0.1:8000/v1")
    plan_parser.add_argument("--guest-ip-prefix", default="172.16.0")
    plan_parser.add_argument("--kernel-image", required=True)
    plan_parser.add_argument("--rootfs-image", required=True)
    plan_parser.add_argument("--out-dir", required=True)
    plan_parser.add_argument("--vcpu-count", type=int, default=2)
    plan_parser.add_argument("--mem-mib", type=int, default=1024)

    preflight_parser = subparsers.add_parser("firecracker-preflight")
    preflight_parser.add_argument("--firecracker-bin", default=None)
    preflight_parser.add_argument("--kernel-image", required=True)
    preflight_parser.add_argument("--rootfs-image", required=True)

    guest_parser = subparsers.add_parser("guest-noop")
    guest_parser.add_argument("--vm-id", required=True)
    guest_parser.add_argument("--host-vllm-url", required=True)
    guest_parser.add_argument("--output", required=True)

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
            vcpu_count=args.vcpu_count,
            mem_mib=args.mem_mib,
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

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
