from __future__ import annotations

import ipaddress
import json
import tempfile
import unittest
from pathlib import Path

from aab_framework.firecracker import FirecrackerAgentSpec, FirecrackerPaths, build_vm_config, plan_agents
from aab_framework.firecracker_sweep import prepare_firecracker_run
from aab_framework.guest_agent import run_noop_agent
from aab_framework.launch import expand_cpu_list, plan_vm_placement
from aab_framework.metrics import summarize_firecracker_sweep
from aab_framework.nvtop import GPU_CSV_FIELDS, format_gpu_csv_rows, parse_nvtop_snapshot
from aab_framework.dcgm import DCGM_CSV_FIELDS, format_dcgm_csv_rows, parse_dcgm_dmon_lines
from aab_framework.rootfs import build_guest_agent_script, build_guest_systemd_unit, build_memory_burner_source
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
        self.assertIn("-e NCCL_P2P_LEVEL=SYS", command)
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
        self.assertIn("--served-model-name agentic-model", command)
        self.assertIn("--dtype half", command)
        self.assertIn("--kv-cache-dtype auto", command)
        self.assertIn("-tp 8", command)
        self.assertIn("-pp 1", command)
        self.assertNotIn("--max-model-len", command)
        self.assertIn("--max-num-seqs 128", command)
        self.assertNotIn("--max-num-batched-tokens", command)
        self.assertIn("--gpu-memory-utilization 0.9", command)
        self.assertIn("--disable-log-requests", command)

    def test_build_vllm_serve_command_exposes_gpu_memory_tuning_knobs(self) -> None:
        command = build_vllm_serve_command(
            VllmDockerConfig(
                model="/home/user/models/Qwen2.5-Coder-32B-Instruct",
                gpu_memory_utilization=0.95,
                max_model_len=32768,
                max_num_seqs=64,
                max_num_batched_tokens=131072,
            )
        )

        self.assertIn("--gpu-memory-utilization 0.95", command)
        self.assertIn("--max-model-len 32768", command)
        self.assertIn("--max-num-seqs 64", command)
        self.assertIn("--max-num-batched-tokens 131072", command)
        command_with_tools = build_vllm_serve_command(
            VllmDockerConfig(
                model="/home/user/models/Qwen2.5-Coder-32B-Instruct",
                enable_auto_tool_choice=True,
                tool_call_parser="hermes",
            )
        )
        self.assertIn("--enable-auto-tool-choice", command_with_tools)
        self.assertIn("--tool-call-parser hermes", command_with_tools)

    def test_start_vllm_script_defaults_nccl_p2p_level_to_sys(self) -> None:
        script = Path("bin/start_vllm_8x5090.sh").read_text(encoding="utf-8")

        self.assertIn('NCCL_P2P_LEVEL="${NCCL_P2P_LEVEL:-SYS}"', script)
        self.assertIn('--nccl-p2p-level "${NCCL_P2P_LEVEL}"', script)
        self.assertIn('GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.9}"', script)
        self.assertIn('MAX_MODEL_LEN="${MAX_MODEL_LEN:-}"', script)
        self.assertIn('MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-}"', script)
        self.assertIn('ENABLE_AUTO_TOOL_CHOICE="${ENABLE_AUTO_TOOL_CHOICE:-0}"', script)
        self.assertIn('TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-}"', script)
        self.assertIn('--gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"', script)
        self.assertIn('extra_vllm_args+=(--max-model-len "${MAX_MODEL_LEN}")', script)
        self.assertIn('extra_vllm_args+=(--max-num-batched-tokens "${MAX_NUM_BATCHED_TOKENS}")', script)
        self.assertIn('extra_vllm_args+=(--enable-auto-tool-choice)', script)
        self.assertIn('extra_vllm_args+=(--tool-call-parser "${TOOL_CALL_PARSER}")', script)

    def test_cli_exposes_vllm_gpu_memory_tuning_options(self) -> None:
        script = Path("aab_framework/cli.py").read_text(encoding="utf-8")

        self.assertIn('vllm_parser.add_argument("--gpu-memory-utilization"', script)
        self.assertIn('vllm_parser.add_argument("--max-model-len"', script)
        self.assertIn('vllm_parser.add_argument("--max-num-seqs"', script)
        self.assertIn('vllm_parser.add_argument("--max-num-batched-tokens"', script)
        self.assertIn('vllm_parser.add_argument("--enable-auto-tool-choice"', script)
        self.assertIn('vllm_parser.add_argument("--tool-call-parser"', script)

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

    def test_plan_agents_rolls_guest_ips_past_one_24_subnet(self) -> None:
        specs = plan_agents(
            vm_count=256,
            base_id="agent",
            host_vllm_url="http://172.16.0.1:8000/v1",
            guest_ip_prefix="172.16.0",
        )

        guest_ips = [spec.guest_ip for spec in specs]
        self.assertEqual(len(set(guest_ips)), 256)
        self.assertEqual(guest_ips[244], "172.16.0.254")
        self.assertEqual(guest_ips[245], "172.16.1.10")
        self.assertEqual(guest_ips[-1], "172.16.1.20")
        for guest_ip in guest_ips:
            ipaddress.ip_address(guest_ip)

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
            workload_seconds=165,
            memory_workers=8,
            memory_mb=1024,
            memory_rounds=4,
            memory_mode="read",
            llm_context_kb=16,
            llm_load_mode="sustained_prefill",
            llm_request_timeout_seconds=300,
            llm_inter_task_sleep_ms=50,
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
        self.assertIn("ip=172.16.0.10::172.16.0.1:255.255.0.0::eth0:off", config["boot-source"]["boot_args"])
        self.assertIn("agent.tasks_per_vm=3", config["boot-source"]["boot_args"])
        self.assertIn("agent.workload_seconds=165", config["boot-source"]["boot_args"])
        self.assertIn("agent.memory_workers=8", config["boot-source"]["boot_args"])
        self.assertIn("agent.memory_mb=1024", config["boot-source"]["boot_args"])
        self.assertIn("agent.memory_rounds=4", config["boot-source"]["boot_args"])
        self.assertIn("agent.memory_mode=read", config["boot-source"]["boot_args"])
        self.assertIn("agent.llm_context_kb=16", config["boot-source"]["boot_args"])
        self.assertIn("agent.llm_load_mode=sustained_prefill", config["boot-source"]["boot_args"])
        self.assertIn("agent.llm_request_timeout_seconds=300", config["boot-source"]["boot_args"])
        self.assertIn("agent.llm_inter_task_sleep_ms=50", config["boot-source"]["boot_args"])
        self.assertEqual(config["metadata"]["llm_context_kb"], 16)
        self.assertEqual(config["metadata"]["llm_load_mode"], "sustained_prefill")
        self.assertEqual(config["metadata"]["llm_request_timeout_seconds"], 300)
        self.assertEqual(config["metadata"]["llm_inter_task_sleep_ms"], 50)

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
        self.assertIn("memory_rounds", script)
        self.assertIn("memory_workers", script)
        self.assertIn("memory_mode", script)
        self.assertIn("task_memory_workers", script)
        self.assertIn("AAB_TASK_MEMORY_WORKERS:-0", script)
        self.assertIn("agent.workload_seconds", script)
        self.assertIn("agent.memory_workers", script)
        self.assertIn("agent.memory_mode", script)
        self.assertIn("agent.llm_context_kb", script)
        self.assertIn("agent.llm_prompt_repeat", script)
        self.assertIn("agent.llm_max_tokens", script)
        self.assertIn("agent.llm_load_mode", script)
        self.assertIn("agent.llm_request_timeout_seconds", script)
        self.assertIn("agent.llm_inter_task_sleep_ms", script)
        self.assertIn("AAB_LLM_CONTEXT_KB:-0", script)
        self.assertIn("AAB_LLM_PROMPT_REPEAT:-1", script)
        self.assertIn("AAB_LLM_MAX_TOKENS:-512", script)
        self.assertIn("AAB_LLM_LOAD_MODE:-single_task", script)
        self.assertIn("build_coding_prompt", script)
        self.assertIn("build_coding_payload", script)
        self.assertIn("write_stage_event", script)
        self.assertIn('payload_path="/tmp/${task_id}.payload.json"', script)
        self.assertIn('--data-binary @"${payload_path}"', script)
        self.assertIn('--max-time "${llm_request_timeout_seconds}"', script)
        self.assertIn("sleep_between_sustained_prefill_tasks", script)
        self.assertIn("run_sustained_prefill", script)
        self.assertIn("task_worker_pids", script)
        self.assertIn('wait "${pid}"', script)
        self.assertIn("run_memory_worker", script)
        self.assertIn("start_background_memory_workers", script)
        self.assertIn("stop_background_memory_workers", script)
        self.assertIn("aab-memory-burner", script)
        self.assertIn("aab-memory-burner-selftest.log", script)
        self.assertIn("AAB_MEMORY_WORKERS:-4", script)
        self.assertIn("AAB_MEMORY_MB:-512", script)
        self.assertIn("dd if=/dev/zero", script)
        self.assertIn("vllm_health", script)
        self.assertIn("status", script)
        self.assertIn("write_result", script)
        self.assertIn("write_result\nwait_for_background_memory_workers", script)

    def test_guest_systemd_unit_runs_agent_script(self) -> None:
        unit = build_guest_systemd_unit()

        self.assertIn("[Unit]", unit)
        self.assertIn("After=network-online.target", unit)
        self.assertIn("ExecStart=/usr/local/bin/aab-guest-agent", unit)
        self.assertIn("WantedBy=multi-user.target", unit)

    def test_memory_burner_source_contains_stream_triad_loop(self) -> None:
        source = build_memory_burner_source()

        self.assertIn("pthread_create", source)
        self.assertIn("mb_per_thread", source)
        self.assertIn("seconds", source)
        self.assertIn("a[i] = b[i] + scalar * c[i]", source)
        self.assertIn('"--mode"', source)
        self.assertIn("MODE_READ", source)
        self.assertIn("MODE_READ8", source)
        self.assertIn('"read8"', source)
        self.assertIn("MODE_NT_WRITE", source)

    def test_rootfs_customization_builds_static_memory_burner(self) -> None:
        script = Path("bin/customize_firecracker_rootfs.sh").read_text(encoding="utf-8")

        self.assertIn("gcc -O3 -static -pthread", script)

    def test_expand_cpu_list_parses_ranges_and_singletons(self) -> None:
        self.assertEqual(expand_cpu_list("0-3,8,10-11"), [0, 1, 2, 3, 8, 10, 11])

    def test_plan_vm_placement_binds_agents_across_numa_nodes(self) -> None:
        numa_cpu_lists = {
            0: list(range(0, 16)),
            1: list(range(64, 80)),
        }

        placements = [
            plan_vm_placement(
                vm_index=index,
                vcpu_count=4,
                host_cpu_count=128,
                cpu_pinning=True,
                numa_policy="bind-by-agent",
                numa_cpu_lists=numa_cpu_lists,
            )
            for index in range(6)
        ]

        self.assertEqual([item.numa_node for item in placements], [0, 1, 0, 1, 0, 1])
        self.assertEqual([item.cpu_set for item in placements], ["0-3", "64-67", "4-7", "68-71", "8-11", "72-75"])

    def test_role_separated_sweep_defaults_to_one_vm_per_agent(self) -> None:
        rows = plan_role_separated_sweep([2, 4, 8, 16, 32, 64, 128, 164])

        self.assertEqual(rows[0].agents, 2)
        self.assertEqual(rows[0].vm_count, 2)
        self.assertEqual(rows[0].tasks_per_vm, 1)
        self.assertEqual(rows[0].total_tasks, 2)
        self.assertEqual(rows[-1].agents, 164)
        self.assertEqual(rows[-1].vm_count, 164)
        self.assertEqual(rows[-1].tasks_per_vm, 1)
        self.assertEqual(rows[-1].total_tasks, 164)
        self.assertGreater(rows[-1].total_tasks, rows[0].total_tasks)

    def test_coding_sweep_script_uses_one_firecracker_vm_per_agent(self) -> None:
        script = Path("bin/run_coding_firecracker_sweep.sh").read_text(encoding="utf-8")

        self.assertIn('AAB_AGENTS_PER_VM="${AAB_AGENTS_PER_VM:-1}"', script)
        self.assertIn("plan_point", script)
        self.assertIn("tasks_per_vm", script)
        self.assertIn("--workload-seconds", script)
        self.assertIn('AAB_MEMORY_WORKERS="${AAB_MEMORY_WORKERS:-8}"', script)
        self.assertIn('AAB_MEMORY_WORKERS_PROFILE="${AAB_MEMORY_WORKERS_PROFILE:-fixed}"', script)
        self.assertIn('AAB_MEMORY_WORKERS_PER_AGENT="${AAB_MEMORY_WORKERS_PER_AGENT:-8}"', script)
        self.assertIn("AAB_VCPUS_PER_AGENT", script)
        self.assertIn("memory_workers_for_agents", script)
        self.assertIn('AAB_MEMORY_MB="${AAB_MEMORY_MB:-256}"', script)
        self.assertIn('AAB_CPU_PINNING="${AAB_CPU_PINNING:-1}"', script)
        self.assertIn('AAB_NUMA_POLICY="${AAB_NUMA_POLICY:-bind-by-agent}"', script)
        self.assertIn('AAB_MEMORY_MODE="${AAB_MEMORY_MODE:-read}"', script)
        self.assertIn('AAB_LLM_CONTEXT_KB="${AAB_LLM_CONTEXT_KB:-32}"', script)
        self.assertIn('AAB_LLM_PROMPT_REPEAT="${AAB_LLM_PROMPT_REPEAT:-1}"', script)
        self.assertIn('AAB_LLM_MAX_TOKENS="${AAB_LLM_MAX_TOKENS:-512}"', script)
        self.assertIn('AAB_LLM_LOAD_MODE="${AAB_LLM_LOAD_MODE:-single_task}"', script)
        self.assertIn('AAB_LLM_REQUEST_TIMEOUT_SECONDS="${AAB_LLM_REQUEST_TIMEOUT_SECONDS:-120}"', script)
        self.assertIn('AAB_LLM_INTER_TASK_SLEEP_MS="${AAB_LLM_INTER_TASK_SLEEP_MS:-0}"', script)
        self.assertIn("--llm-context-kb", script)
        self.assertIn("--llm-prompt-repeat", script)
        self.assertIn("--llm-max-tokens", script)
        self.assertIn("--llm-load-mode", script)
        self.assertIn("--llm-request-timeout-seconds", script)
        self.assertIn("--llm-inter-task-sleep-ms", script)
        self.assertIn('AAB_DCGMI_BIN="${AAB_DCGMI_BIN:-dcgmi}"', script)
        self.assertIn("start_gpu_metrics_dcgm", script)
        self.assertIn("python3 -m aab_framework.dcgm", script)
        self.assertIn("gpu_metrics_backend.log", script)
        self.assertNotIn("nvidia-smi", script)
        self.assertNotIn("start_gpu_metrics_nvidia_smi", script)
        self.assertNotIn("start_gpu_metrics_nvtop", script)
        self.assertIn('WORKLOAD_GRACE_SECONDS="${WORKLOAD_GRACE_SECONDS:-60}"', script)
        self.assertIn('top -r', script)
        self.assertNotIn('elif [[ "${agents}" -le 32 ]]', script)
        self.assertNotIn('echo "16 $(((agents + 15) / 16))"', script)

    def test_nvtop_snapshot_parser_outputs_compatible_gpu_csv_rows(self) -> None:
        snapshot = json.dumps(
            [
                {
                    "device_name": "NVIDIA GeForce RTX 5090",
                    "gpu_util": "87%",
                    "mem_util": "95%",
                    "mem_used": str(31 * 1024 * 1024 * 1024),
                    "mem_total": str(32 * 1024 * 1024 * 1024),
                    "power_draw": "251W",
                    "temp": "42C",
                }
            ]
        )

        rows = parse_nvtop_snapshot(snapshot, timestamp=123)
        csv_line = format_gpu_csv_rows(rows)

        self.assertEqual(GPU_CSV_FIELDS[0], "timestamp")
        self.assertEqual(rows[0]["timestamp"], "123")
        self.assertEqual(rows[0]["index"], "0")
        self.assertEqual(rows[0]["utilization_gpu_pct"], "87")
        self.assertEqual(rows[0]["utilization_memory_pct"], "")
        self.assertEqual(rows[0]["memory_used_mib"], "31744.0")
        self.assertEqual(rows[0]["memory_total_mib"], "32768.0")
        self.assertEqual(rows[0]["power_draw_w"], "251")
        self.assertEqual(rows[0]["temperature_c"], "42")
        self.assertEqual(rows[0]["memory_used_pct"], "95")
        self.assertEqual(rows[0]["gpu_metrics_backend"], "nvtop")
        self.assertEqual(len(csv_line.split(",")), len(GPU_CSV_FIELDS))

    def test_dcgm_dmon_parser_outputs_normalized_compute_metric_rows(self) -> None:
        lines = [
            "# gpu gputl mcutl fbused fbtotal power temp sm_active sm_occupancy tensor_active dram_active fp32_active fp16_active",
            "GPU 0 0.42 0.24 29305 32607 125.5 42 0.31 0.18 0.21 0.11 0.07 0.09",
            "GPU 1 42 24 29305 32607 125.5 42 31 18 21 11 7 9",
        ]

        rows = parse_dcgm_dmon_lines(
            lines,
            field_ids="203,204,252,250,155,150,1002,1003,1004,1005,1007,1008",
            timestamp=123,
        )
        csv_line = format_dcgm_csv_rows(rows)

        self.assertEqual(DCGM_CSV_FIELDS[0], "timestamp")
        self.assertEqual(rows[0]["timestamp"], "123")
        self.assertEqual(rows[0]["index"], "0")
        self.assertEqual(rows[0]["utilization_gpu_pct"], "42.0")
        self.assertEqual(rows[0]["utilization_memory_pct"], "24.0")
        self.assertEqual(rows[0]["memory_used_mib"], "29305.0")
        self.assertEqual(rows[0]["memory_total_mib"], "32607.0")
        self.assertEqual(rows[0]["memory_used_pct"], "89.8733")
        self.assertEqual(rows[0]["power_draw_w"], "125.5")
        self.assertEqual(rows[0]["temperature_c"], "42.0")
        self.assertEqual(rows[0]["sm_active_pct"], "31.0")
        self.assertEqual(rows[0]["sm_occupancy_pct"], "18.0")
        self.assertEqual(rows[0]["tensor_active_pct"], "21.0")
        self.assertEqual(rows[0]["dram_active_pct"], "11.0")
        self.assertEqual(rows[1]["utilization_gpu_pct"], "42.0")
        self.assertEqual(rows[1]["sm_active_pct"], "31.0")
        self.assertEqual(rows[1]["fp32_active_pct"], "7.0")
        self.assertEqual(rows[1]["fp16_active_pct"], "9.0")
        self.assertEqual(len(csv_line.splitlines()[0].split(",")), len(DCGM_CSV_FIELDS))

    def test_prepared_firecracker_runner_supports_pinning_and_numa(self) -> None:
        script = Path("bin/run_prepared_firecracker_agents.sh").read_text(encoding="utf-8")

        self.assertIn("plan_vm_placement", script)
        self.assertIn("AAB_CPU_PINNING", script)
        self.assertIn("AAB_NUMA_POLICY", script)
        self.assertIn("taskset", script)
        self.assertIn("numactl", script)

    def test_prepared_firecracker_runner_collects_guest_output_directory(self) -> None:
        script = Path("bin/run_prepared_firecracker_agents.sh").read_text(encoding="utf-8")

        self.assertIn("/output", script)
        self.assertIn("trajectory.jsonl", script)
        self.assertIn("patch.diff", script)
        self.assertIn("test.log", script)
        self.assertIn("stdout.log", script)
        self.assertIn("stderr.log", script)
        self.assertIn("task_spec_host_path", script)

    def test_mini_swe_team_metrics_wrapper_starts_existing_collectors(self) -> None:
        script = Path("bin/run_mini_swe_agent_team_v2_sweep_with_metrics.sh").read_text(encoding="utf-8")

        self.assertIn("start_cpu_metrics", script)
        self.assertIn("start_gpu_metrics", script)
        self.assertIn("start_pcm", script)
        self.assertIn("metrics/cpu.csv", script)
        self.assertIn("metrics/gpu.csv", script)
        self.assertIn("amd_pcm_memory.csv", script)
        self.assertIn("refresh_run_metrics", script)
        self.assertIn('ADAPTER_MODE="${ADAPTER_MODE:-mock}"', script)
        self.assertIn('--adapter-mode "${ADAPTER_MODE}"', script)
        self.assertIn('RUNTIME_TYPE="${RUNTIME_TYPE:-process}"', script)
        self.assertIn('--runtime-type "${RUNTIME_TYPE}"', script)
        self.assertIn('AAB_REPO_CONTEXT_ENABLED="${AAB_REPO_CONTEXT_ENABLED:-0}"', script)
        self.assertIn("--repo-context-enabled", script)
        self.assertIn("--repo-source", script)
        self.assertIn('AAB_REPO_WORKSPACE_MODE="${AAB_REPO_WORKSPACE_MODE:-worktree}"', script)
        self.assertIn("--repo-workspace-mode", script)
        self.assertIn('AAB_REPO_CONTEXT_INCLUDE_GIT_HISTORY="${AAB_REPO_CONTEXT_INCLUDE_GIT_HISTORY:-0}"', script)
        self.assertIn("--repo-context-include-git-history", script)
        self.assertIn('AAB_REPO_CONTEXT_PYTEST_COLLECT="${AAB_REPO_CONTEXT_PYTEST_COLLECT:-0}"', script)
        self.assertIn("--repo-context-pytest-collect", script)
        self.assertIn('METRICS_MIN_SECONDS="${METRICS_MIN_SECONDS:-2}"', script)
        self.assertIn('sleep "${METRICS_MIN_SECONDS}"', script)

    def test_mini_swe_firecracker_sweep_wrapper_uses_vm_runner_and_collectors(self) -> None:
        script = Path("bin/run_mini_swe_agent_team_v2_firecracker_sweep_with_metrics.sh").read_text(encoding="utf-8")

        self.assertIn("setup_firecracker_network.sh", script)
        self.assertIn("--use-firecracker", script)
        self.assertIn("--fc-rootfs", script)
        self.assertIn("--fc-kernel", script)
        self.assertIn("--guest-vllm-base-url", script)
        self.assertIn("start_cpu_metrics", script)
        self.assertIn("start_gpu_metrics", script)
        self.assertIn("start_pcm", script)
        self.assertIn("refresh_run_metrics", script)
        self.assertIn("build_sweep_from_child_runs", script)

    def test_mini_swe_guest_worker_uses_python_api_runner(self) -> None:
        from aab_framework.rootfs import build_mini_swe_guest_worker_script

        script = build_mini_swe_guest_worker_script()

        self.assertIn("OpenAITextModel", script)
        self.assertIn("LocalEnvironment", script)
        self.assertIn("DefaultAgent", script)
        self.assertIn("api_imports_done", script)
        self.assertIn("import_pydantic_start", script)
        self.assertIn("import_msa_default_done", script)
        self.assertIn("urllib.request.urlopen", script)
        self.assertIn("agent_run_start", script)
        self.assertIn("agent_run_done", script)

    def test_firecracker_network_setup_replaces_existing_bridge_cidr(self) -> None:
        script = Path("bin/setup_firecracker_network.sh").read_text(encoding="utf-8")

        self.assertIn('CIDR="${CIDR:-16}"', script)
        self.assertIn('ip addr replace "${HOST_IP}/${CIDR}" dev "${BRIDGE_NAME}"', script)

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
                workload_seconds=165,
                memory_workers=8,
                memory_mb=1024,
                memory_rounds=4,
                memory_mode="read",
                llm_context_kb=16,
                llm_load_mode="sustained_prefill",
                llm_request_timeout_seconds=300,
                llm_inter_task_sleep_ms=50,
            )

            self.assertEqual(len(manifest["agents"]), 2)
            self.assertTrue((root / "run" / "agent-000.rootfs.ext4").exists())
            self.assertTrue((root / "run" / "agent-001.rootfs.ext4").exists())
            self.assertTrue((root / "run" / "agent-000.json").exists())
            self.assertEqual(manifest["agents"][0]["tasks_per_vm"], 3)
            self.assertEqual(manifest["agents"][0]["workload_seconds"], 165)
            self.assertEqual(manifest["agents"][0]["memory_workers"], 8)
            self.assertEqual(manifest["agents"][0]["memory_mb"], 1024)
            self.assertEqual(manifest["agents"][0]["memory_rounds"], 4)
            self.assertEqual(manifest["agents"][0]["memory_mode"], "read")
            self.assertEqual(manifest["agents"][0]["llm_context_kb"], 16)
            self.assertEqual(manifest["agents"][0]["llm_load_mode"], "sustained_prefill")
            self.assertEqual(manifest["agents"][0]["llm_request_timeout_seconds"], 300)
            self.assertEqual(manifest["agents"][0]["llm_inter_task_sleep_ms"], 50)
            config = json.loads((root / "run" / "agent-000.json").read_text(encoding="utf-8"))
            self.assertIn("agent.llm_context_kb=16", config["boot-source"]["boot_args"])
            self.assertIn("agent.llm_load_mode=sustained_prefill", config["boot-source"]["boot_args"])

    def test_summarize_firecracker_sweep_writes_aligned_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            point = root / "agents_2"
            metrics = point / "metrics"
            results = point / "results"
            metrics.mkdir(parents=True)
            results.mkdir(parents=True)
            (results / "summary.json").write_text(
                json.dumps({"vm_results": 2, "completed_tasks": 2, "failed_tasks": 0, "vllm_ok": 2}),
                encoding="utf-8",
            )
            (results / "agent-000.trace.jsonl").write_text(
                json.dumps({"task_id": "agent-000-task-0", "status": "ok", "latency_ms": 100}) + "\n",
                encoding="utf-8",
            )
            (results / "agent-000.result.json").write_text(
                json.dumps({"memory_workers": 8, "memory_mode": "read"}),
                encoding="utf-8",
            )
            (metrics / "cpu.csv").write_text(
                "timestamp,cpu_util_pct,user_pct,system_pct,iowait_pct,idle_pct,load1,load5,load15\n"
                "1,10,8,2,0,90,1,1,1\n"
                "2,20,16,4,0,80,2,2,2\n",
                encoding="utf-8",
            )
            (metrics / "gpu.csv").write_text(
                "timestamp,index,utilization_gpu_pct,utilization_memory_pct,memory_used_mib,memory_total_mib,power_draw_w,temperature_c\n"
                "1,0,0,0,100,1000,50,30\n"
                "2,0,50,20,200,1000,100,35\n",
                encoding="utf-8",
            )
            (metrics / "amd_pcm_memory.csv").write_text(
                "Total Mem Bw (GB/s)    |            10.00 |            5.00 |            5.00 |\n"
                "Total Mem RdBw (GB/s)  |             7.00 |            4.00 |            3.00 |\n"
                "Total Mem WrBw (GB/s)  |             3.00 |            1.00 |            2.00 |\n"
                "Total Mem Bw (GB/s)    |            20.00 |           10.00 |           10.00 |\n"
                "Total Mem RdBw (GB/s)  |            14.00 |            8.00 |            6.00 |\n"
                "Total Mem WrBw (GB/s)  |             6.00 |            2.00 |            4.00 |\n",
                encoding="utf-8",
            )
            (metrics / "dcgm.csv").write_text(
                "timestamp,index,sm_active_pct,sm_occupancy_pct,tensor_active_pct,dram_active_pct,fp32_active_pct,fp16_active_pct,dcgm_metrics_backend\n"
                "1,0,10,20,30,40,5,6,dcgmi\n"
                "2,0,50,60,70,80,15,16,dcgmi\n",
                encoding="utf-8",
            )

            rows = summarize_firecracker_sweep(root, run_seconds=60, workload_seconds=30)

            self.assertEqual(rows[0]["agents"], 2)
            self.assertEqual(rows[0]["completed"], 2)
            self.assertEqual(rows[0]["dram_bw_p95_gbps"], 19.5)
            self.assertEqual(rows[0]["dram_bw_max_pct_of_peak"], 3.45)
            self.assertEqual(rows[0]["gpu_util_p95_pct"], 50.0)
            self.assertEqual(rows[0]["gpu_memctrl_p95_pct"], 20.0)
            self.assertEqual(rows[0]["gpu_util_max_pct"], 50.0)
            self.assertEqual(rows[0]["gpu_mem_used_pct_p95"], 19.5)
            self.assertEqual(rows[0]["gpu_mem_used_pct_max"], 20.0)
            self.assertEqual(rows[0]["sm_active_p95_pct"], 48.0)
            self.assertEqual(rows[0]["sm_active_max_pct"], 50.0)
            self.assertEqual(rows[0]["tensor_active_p95_pct"], 68.0)
            self.assertEqual(rows[0]["dram_active_p95_pct"], 78.0)
            self.assertEqual(rows[0]["sm_active_avg_pct"], 30.0)
            self.assertEqual(rows[0]["sm_active_sample_pct"], 100.0)
            self.assertEqual(rows[0]["sm_active_area"], 60.0)
            self.assertEqual(rows[0]["tensor_active_avg_pct"], 50.0)
            self.assertEqual(rows[0]["tensor_active_sample_pct"], 100.0)
            self.assertEqual(rows[0]["tensor_active_area"], 100.0)
            self.assertEqual(rows[0]["dram_active_avg_pct"], 60.0)
            self.assertEqual(rows[0]["dram_active_sample_pct"], 100.0)
            self.assertEqual(rows[0]["dram_active_area"], 120.0)
            self.assertEqual(rows[0]["dcgm_samples"], 2)
            self.assertTrue((root / "aligned_metrics.csv").exists())
            self.assertTrue((root / "aligned_metrics.json").exists())
            self.assertTrue((root / "aligned_metrics.md").exists())


if __name__ == "__main__":
    unittest.main()
