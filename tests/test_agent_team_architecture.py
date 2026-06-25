from __future__ import annotations

import unittest
import tempfile
from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path

from aab_framework.agent_team import (
    AgentRoleSpec,
    AgentTeamSpec,
    ChallengeAgent,
    ChallengeFinding,
    ExecutorSpec,
    PluginRegistry,
    WorkloadSpec,
)
from aab_framework.cli import main
from aab_framework.executors.firecracker import (
    build_firecracker_executor_spec,
    prepare_firecracker_executor_run,
)
from aab_framework.rootfs import build_guest_agent_script
from aab_framework.workloads.coding import (
    build_coding_chat_payload_template,
    build_coding_prompt_template,
    build_coding_result_schema,
    build_coding_trace_schema,
    build_coding_workload_spec,
)


class AgentTeamArchitectureTests(unittest.TestCase):
    def test_coding_workload_declares_agent_team_without_executor_details(self) -> None:
        workload = build_coding_workload_spec()

        self.assertEqual(workload.name, "coding")
        self.assertEqual(workload.default_team.name, "coding-bugfix-team")
        self.assertIn("planner", workload.default_team.role_names)
        self.assertIn("challenge", workload.default_team.role_names)
        self.assertEqual(workload.default_team.challenge_role, "challenge")
        self.assertNotIn("tap_name", workload.executor_specific_fields)
        self.assertIn("success_rate_pct", workload.base_metrics)
        self.assertIn("patch_plan", workload.business_metrics)

    def test_firecracker_executor_declares_isolation_and_resource_capabilities(self) -> None:
        executor = build_firecracker_executor_spec()

        self.assertEqual(executor.name, "firecracker")
        self.assertEqual(executor.isolation, "microvm")
        self.assertTrue(executor.supports_cpu_pinning)
        self.assertTrue(executor.supports_numa_binding)
        self.assertIn("rootfs", executor.artifacts)
        self.assertIn("trace.jsonl", executor.result_files)

    def test_firecracker_executor_prepares_run_manifest_without_workload_logic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kernel = root / "vmlinux"
            rootfs = root / "rootfs.ext4"
            kernel.write_text("kernel", encoding="utf-8")
            rootfs.write_bytes(b"rootfs")

            manifest = prepare_firecracker_executor_run(
                out_dir=root / "run",
                vm_count=2,
                kernel_image=kernel,
                base_rootfs_image=rootfs,
                host_vllm_url="http://172.16.0.1:8000/v1",
                tasks_per_vm=1,
                request_workers=1,
                workload_seconds=120,
                memory_workers=8,
                memory_mb=256,
                memory_rounds=16,
                memory_mode="read",
                llm_context_kb=64,
                llm_prompt_repeat=2,
                llm_max_tokens=768,
                llm_load_mode="sustained_prefill",
                llm_request_timeout_seconds=180,
                workload_name="coding",
            )

            self.assertEqual(manifest["executor"], "firecracker")
            self.assertEqual(manifest["workload"], "coding")
            self.assertEqual(manifest["vm_count"], 2)
            self.assertEqual(len(manifest["agents"]), 2)
            self.assertTrue((root / "run" / "agent-000.rootfs.ext4").exists())
            self.assertTrue((root / "run" / "agent-000.json").exists())
            self.assertIn("firecracker_command", manifest["agents"][0])
            self.assertEqual(manifest["llm_context_kb"], 64)
            self.assertEqual(manifest["llm_prompt_repeat"], 2)
            self.assertEqual(manifest["llm_max_tokens"], 768)
            self.assertEqual(manifest["llm_load_mode"], "sustained_prefill")
            self.assertEqual(manifest["llm_request_timeout_seconds"], 180)
            self.assertEqual(manifest["agents"][0]["llm_context_kb"], 64)
            self.assertNotIn("prompt", manifest["agents"][0])

    def test_plugin_registry_keeps_workloads_and_executors_separate(self) -> None:
        registry = PluginRegistry()
        registry.register_workload(build_coding_workload_spec())
        registry.register_executor(build_firecracker_executor_spec())

        self.assertEqual(registry.workload("coding").name, "coding")
        self.assertEqual(registry.executor("firecracker").name, "firecracker")
        self.assertEqual(registry.workload_names(), ["coding"])
        self.assertEqual(registry.executor_names(), ["firecracker"])

    def test_challenge_agent_blocks_workload_with_executor_coupling(self) -> None:
        workload = WorkloadSpec(
            name="bad-workload",
            description="Invalid workload with executor leakage.",
            default_team=AgentTeamSpec(
                name="bad-team",
                roles=(AgentRoleSpec(name="solver", responsibility="Solve the task."),),
                challenge_role=None,
            ),
            base_metrics=("success_rate_pct",),
            business_metrics=("task_quality",),
            executor_specific_fields=("tap_name",),
        )

        review = ChallengeAgent().review_workload(workload)

        self.assertTrue(review.blocked)
        self.assertEqual(review.verdict, "BLOCKED")
        self.assertIn("executor-specific fields", review.blockers[0].message)

    def test_challenge_agent_passes_current_coding_workload_and_firecracker_executor(self) -> None:
        challenge = ChallengeAgent()

        workload_review = challenge.review_workload(build_coding_workload_spec())
        executor_review = challenge.review_executor(build_firecracker_executor_spec())

        self.assertFalse(workload_review.blocked)
        self.assertEqual(workload_review.verdict, "PASS")
        self.assertFalse(executor_review.blocked)
        self.assertEqual(executor_review.verdict, "PASS")

    def test_cli_inspects_registered_agent_team_plugins(self) -> None:
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(["inspect-agent-team"])

        self.assertEqual(exit_code, 0)
        payload = output.getvalue()
        self.assertIn('"workloads"', payload)
        self.assertIn('"coding"', payload)
        self.assertIn('"executors"', payload)
        self.assertIn('"firecracker"', payload)
        self.assertIn('"challenge_reviews"', payload)
        self.assertIn('"PASS"', payload)

    def test_coding_workload_owns_prompt_payload_and_result_contract(self) -> None:
        prompt = build_coding_prompt_template()
        payload = build_coding_chat_payload_template()
        trace_schema = build_coding_trace_schema()
        result_schema = build_coding_result_schema()

        self.assertIn("retry_state is not persisted after timeout", prompt)
        self.assertIn("${task_id}", prompt)
        self.assertIn("${prompt}", payload)
        self.assertIn("${llm_max_tokens}", payload)
        self.assertIn("/workspace/models/Qwen2.5-Coder-32B-Instruct/", payload)
        self.assertIn("diagnosis", payload)
        self.assertIn("task_id", trace_schema.required_fields)
        self.assertIn("event_type", trace_schema.required_fields)
        self.assertIn("stage", trace_schema.required_fields)
        self.assertIn("role", trace_schema.required_fields)
        self.assertIn("latency_ms", trace_schema.required_fields)
        self.assertIn("vllm_health", result_schema.required_fields)
        self.assertIn("completed_tasks", result_schema.required_fields)

    def test_guest_agent_script_uses_coding_workload_contract_markers(self) -> None:
        script = build_guest_agent_script()

        self.assertIn("AAB_WORKLOAD_NAME=\"coding\"", script)
        self.assertIn("BEGIN coding workload contract", script)
        self.assertIn("END coding workload contract", script)
        self.assertIn("build_coding_prompt", script)
        self.assertIn("build_coding_payload", script)

    def test_guest_agent_script_records_agent_team_stage_trace_events(self) -> None:
        script = build_guest_agent_script()

        self.assertIn("write_stage_event", script)
        self.assertIn('"event_type":"stage"', script)
        self.assertIn('write_stage_event "${task_id}" "planner" "planner"', script)
        self.assertIn('write_stage_event "${task_id}" "context_builder" "context_builder"', script)
        self.assertIn('write_stage_event "${task_id}" "solver" "solver"', script)
        self.assertIn('write_stage_event "${task_id}" "verifier" "verifier"', script)
        self.assertIn('write_stage_event "${task_id}" "challenge" "challenge"', script)


if __name__ == "__main__":
    unittest.main()
