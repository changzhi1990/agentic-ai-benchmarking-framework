from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aab_framework.firecracker import FirecrackerAgentSpec, FirecrackerPaths, build_vm_config, plan_agents
from aab_framework.firecracker_sweep import prepare_firecracker_run
from aab_framework.guest_agent import run_noop_agent
from aab_framework.rootfs import build_guest_agent_script, build_guest_systemd_unit
from aab_framework.sweep import plan_role_separated_sweep
from aab_framework.vllm import (
    VllmDockerConfig,
    build_vllm_container_command,
    build_vllm_serve_command,
)


class ScaffoldTests(unittest.TestCase):
    def test_build_vllm_docker_command_for_8x5090(self) -> None:
        command = build_vllm_container_command(
            VllmDockerConfig(
                model="/home/user/models/Qwen2.5-Coder-32B-Instruct",
                image="vllm/vllm-openai:latest",
                served_model_name="agentic-model",
                api_key="token-abc123",
                tensor_parallel_size=8,
                port=8000,
            )
        )

        self.assertIn("docker run -itd", command)
        self.assertIn("--gpus all", command)
        self.assertIn("--runtime=nvidia", command)
        self.assertIn("--network host", command)
        self.assertIn("--ipc=host", command)
        self.assertIn("--shm-size 128G", command)
        self.assertIn("--entrypoint /usr/bin/bash", command)
        self.assertIn("-v /home/user/models:/workspace/models", command)
        self.assertIn("vllm/vllm-openai:latest", command)

    def test_build_vllm_serve_command_uses_container_model_path(self) -> None:
        command = build_vllm_serve_command(
            VllmDockerConfig(
                model="/home/user/models/Qwen2.5-Coder-32B-Instruct",
                tensor_parallel_size=8,
            )
        )

        self.assertIn("docker exec -d aab-vllm", command)
        self.assertIn("vllm serve /workspace/models/Qwen2.5-Coder-32B-Instruct/", command)
        self.assertIn("--dtype half", command)
        self.assertIn("--kv-cache-dtype auto", command)
        self.assertIn("-tp 8", command)
        self.assertIn("-pp 1", command)
        self.assertIn("--max-num-seqs 128", command)
        self.assertIn("--gpu-memory-utilization 0.9", command)
        self.assertIn("--disable-log-requests", command)

    def test_plan_agents_assigns_unique_vm_ids_and_guest_ips(self) -> None:
        specs = plan_agents(
            vm_count=3,
            base_id="agent",
            host_vllm_url="http://172.16.0.1:8000/v1",
            guest_ip_prefix="172.16.0",
        )

        self.assertEqual([spec.vm_id for spec in specs], ["agent-000", "agent-001", "agent-002"])
        self.assertEqual([spec.tap_name for spec in specs], ["tap-agent-000", "tap-agent-001", "tap-agent-002"])
        self.assertEqual([spec.guest_ip for spec in specs], ["172.16.0.10", "172.16.0.11", "172.16.0.12"])

    def test_build_vm_config_contains_boot_disk_network_and_metadata(self) -> None:
        spec = FirecrackerAgentSpec(
            vm_id="agent-000",
            tap_name="tap-agent-000",
            guest_ip="172.16.0.10",
            host_ip="172.16.0.1",
            host_vllm_url="http://172.16.0.1:8000/v1",
            vcpu_count=2,
            mem_mib=1024,
            tasks_per_vm=3,
            request_workers=1,
        )
        paths = FirecrackerPaths(
            kernel_image="/opt/firecracker/vmlinux",
            rootfs_image="/opt/firecracker/rootfs.ext4",
            socket_path="/tmp/agent-000.socket",
        )

        config = build_vm_config(spec, paths)

        self.assertEqual(config["machine-config"]["vcpu_count"], 2)
        self.assertEqual(config["machine-config"]["mem_size_mib"], 1024)
        self.assertEqual(config["boot-source"]["kernel_image_path"], "/opt/firecracker/vmlinux")
        self.assertEqual(config["drives"][0]["path_on_host"], "/opt/firecracker/rootfs.ext4")
        self.assertEqual(config["network-interfaces"][0]["host_dev_name"], "tap-agent-000")
        self.assertIn("agent.vm_id=agent-000", config["boot-source"]["boot_args"])
        self.assertIn("agent.host_vllm_url=http://172.16.0.1:8000/v1", config["boot-source"]["boot_args"])
        self.assertIn("ip=172.16.0.10::172.16.0.1:255.255.255.0::eth0:off", config["boot-source"]["boot_args"])
        self.assertIn("agent.tasks_per_vm=3", config["boot-source"]["boot_args"])

    def test_noop_guest_agent_writes_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "result.json"

            result = run_noop_agent(
                vm_id="agent-000",
                host_vllm_url="http://172.16.0.1:8000/v1",
                output_path=output_path,
            )

            written = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "ready")
            self.assertEqual(written["vm_id"], "agent-000")
            self.assertEqual(written["host_vllm_url"], "http://172.16.0.1:8000/v1")

    def test_guest_agent_script_contains_kernel_arg_parsing_and_output_path(self) -> None:
        script = build_guest_agent_script()

        self.assertIn("agent.vm_id", script)
        self.assertIn("agent.host_vllm_url", script)
        self.assertIn("/var/lib/aab/result.json", script)
        self.assertIn("/models", script)
        self.assertIn("/chat/completions", script)
        self.assertIn("coding_bugfix", script)
        self.assertIn("tasks_per_vm", script)
        self.assertIn("vllm_health", script)
        self.assertIn("status", script)

    def test_guest_systemd_unit_runs_agent_script(self) -> None:
        unit = build_guest_systemd_unit()

        self.assertIn("[Unit]", unit)
        self.assertIn("After=network-online.target", unit)
        self.assertIn("ExecStart=/usr/local/bin/aab-guest-agent", unit)
        self.assertIn("WantedBy=multi-user.target", unit)

    def test_role_separated_sweep_caps_request_workers(self) -> None:
        rows = plan_role_separated_sweep([2, 4, 8, 16, 32, 64, 128, 192, 256])

        self.assertEqual(rows[0].agents, 2)
        self.assertEqual(rows[0].request_workers, 2)
        self.assertEqual(rows[-1].agents, 256)
        self.assertEqual(rows[-1].request_workers, 2)
        self.assertGreater(rows[-1].vm_count, rows[0].vm_count)
        self.assertGreater(rows[-1].total_tasks, rows[0].total_tasks)

    def test_prepare_firecracker_run_creates_per_vm_rootfs_and_configs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kernel = root / "vmlinux"
            rootfs = root / "rootfs.ext4"
            kernel.write_text("kernel", encoding="utf-8")
            rootfs.write_bytes(b"rootfs")

            manifest = prepare_firecracker_run(
                out_dir=root / "run",
                vm_count=2,
                kernel_image=kernel,
                base_rootfs_image=rootfs,
                host_vllm_url="http://172.16.0.1:8000/v1",
                tasks_per_vm=3,
                request_workers=1,
            )

            self.assertEqual(len(manifest["agents"]), 2)
            self.assertTrue((root / "run" / "agent-000.rootfs.ext4").exists())
            self.assertTrue((root / "run" / "agent-001.rootfs.ext4").exists())
            self.assertTrue((root / "run" / "agent-000.json").exists())
            self.assertEqual(manifest["agents"][0]["tasks_per_vm"], 3)


if __name__ == "__main__":
    unittest.main()
