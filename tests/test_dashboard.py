from __future__ import annotations

import csv
import json
import multiprocessing
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aab_framework.dashboard import (
    DashboardState,
    SweepLaunchConfig,
    build_sweep_environment,
    ensure_aligned_metrics,
    list_runs,
    load_run_report,
    parse_agents_list,
    require_vllm_ready,
)


class _FakeProcess:
    pid = 12345

    def poll(self) -> None:
        return None


def _start_sweep_worker(project_root: str, result_queue: multiprocessing.Queue) -> None:
    with (
        patch("aab_framework.dashboard.require_vllm_ready"),
        patch("aab_framework.dashboard.subprocess.Popen", return_value=_FakeProcess()),
    ):
        state = DashboardState(Path(project_root))
        status = state.start_sweep(SweepLaunchConfig(agents=[1], run_name="Deadlock Test"))
        result_queue.put(status)


class DashboardTests(unittest.TestCase):
    def test_load_run_report_combines_aligned_metrics_manifest_and_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "runs" / "coding-firecracker-sweep-demo"
            point = run / "agents_128"
            results = point / "results"
            results.mkdir(parents=True)
            (run / "aligned_metrics.csv").write_text(
                "\n".join(
                    [
                        "agents,vm_results,completed,failed,success_rate_pct,throughput_task_per_min_run,throughput_task_per_min_workload,lat_ok_p95_ms,cpu_p95_pct,cpu_max_pct,dram_bw_p95_gbps,dram_bw_max_gbps,dram_bw_max_pct_of_peak,gpu_util_p95_pct,gpu_util_max_pct,gpu_memctrl_p95_pct,gpu_memctrl_max_pct,gpu_mem_used_p95_mib",
                        "128,128,125,3,97.66,41.67,62.5,58231.0,100.0,100.0,446.52,543.09,93.64,2.4,95.0,3.5,24.0,32039.0",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (run / "sweep_summary.csv").write_text(
                "agents,vm_results,completed_tasks,failed_tasks,vllm_ok\n128,128,125,3,128\n",
                encoding="utf-8",
            )
            (point / "firecracker-run.json").write_text(
                json.dumps(
                    {
                        "vm_count": 128,
                        "tasks_per_vm": 1,
                        "request_workers": 1,
                        "workload_seconds": 120,
                        "memory_workers": 8,
                        "memory_mb": 256,
                        "memory_rounds": 16,
                        "memory_mode": "read",
                        "agents": [
                            {"vm_id": "agent-000", "vcpu_count": 8, "mem_mib": 16384},
                            {"vm_id": "agent-091", "vcpu_count": 8, "mem_mib": 16384},
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (results / "summary.json").write_text(
                json.dumps(
                    {
                        "vm_results": 128,
                        "completed_tasks": 125,
                        "failed_tasks": 3,
                        "vllm_ok": 128,
                        "results": [
                            {"vm_id": "agent-000", "completed_tasks": 1, "failed_tasks": 0},
                            {"vm_id": "agent-091", "completed_tasks": 0, "failed_tasks": 1},
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = load_run_report(run)

            self.assertEqual(report["name"], "coding-firecracker-sweep-demo")
            self.assertEqual(report["points"][0]["agents"], 128)
            self.assertEqual(report["points"][0]["completed"], 125)
            self.assertEqual(report["points"][0]["failed_vm_ids"], ["agent-091"])
            self.assertEqual(report["points"][0]["config"]["vm_count"], 128)
            self.assertEqual(report["points"][0]["config"]["vcpu_count"], 8)
            self.assertEqual(report["points"][0]["config"]["mem_mib"], 16384)
            self.assertEqual(report["points"][0]["gpu_util_p95_pct"], 2.4)
            self.assertEqual(report["points"][0]["gpu_util_max_pct"], 95)
            self.assertEqual(report["points"][0]["gpu_memctrl_p95_pct"], 3.5)
            self.assertEqual(report["points"][0]["gpu_memctrl_max_pct"], 24)
            self.assertEqual(report["points"][0]["cpu_max_pct"], 100)
            self.assertEqual(report["points"][0]["dram_bw_max_gbps"], 543.09)
            self.assertEqual(report["points"][0]["dram_bw_max_pct_of_peak"], 93.64)
            self.assertEqual(report["overview"]["max_agents"], 128)
            self.assertEqual(report["overview"]["best_stable_agents"], 0)

    def test_load_run_report_exposes_dcgm_compute_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "runs" / "dcgm-demo"
            point = run / "agents_16"
            results = point / "results"
            results.mkdir(parents=True)
            (run / "aligned_metrics.csv").write_text(
                "\n".join(
                    [
                        "agents,vm_results,completed,failed,success_rate_pct,sm_active_p95_pct,sm_active_max_pct,sm_occupancy_p95_pct,tensor_active_p95_pct,tensor_active_max_pct,dram_active_p95_pct,dram_active_max_pct,fp16_active_p95_pct,fp32_active_p95_pct,dcgm_samples",
                        "16,16,16,0,100,72.5,80,44,61.5,70,38.5,40,12.5,8.5,30",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (results / "summary.json").write_text(
                json.dumps({"vm_results": 16, "completed_tasks": 16, "failed_tasks": 0, "vllm_ok": 16}),
                encoding="utf-8",
            )

            report = load_run_report(run)
            point_report = report["points"][0]

            self.assertEqual(point_report["sm_active_p95_pct"], 72.5)
            self.assertEqual(point_report["sm_active_max_pct"], 80)
            self.assertEqual(point_report["sm_occupancy_p95_pct"], 44)
            self.assertEqual(point_report["tensor_active_p95_pct"], 61.5)
            self.assertEqual(point_report["tensor_active_max_pct"], 70)
            self.assertEqual(point_report["dram_active_p95_pct"], 38.5)
            self.assertEqual(point_report["dram_active_max_pct"], 40)
            self.assertEqual(point_report["fp16_active_p95_pct"], 12.5)
            self.assertEqual(point_report["fp32_active_p95_pct"], 8.5)
            self.assertEqual(point_report["dcgm_samples"], 30)

    def test_parse_agents_list_accepts_spaces_commas_and_deduplicates(self) -> None:
        self.assertEqual(parse_agents_list("2, 4 8\n16 16"), [2, 4, 8, 16])

    def test_parse_agents_list_rejects_unsafe_values(self) -> None:
        with self.assertRaises(ValueError):
            parse_agents_list("2 && rm -rf /")
        with self.assertRaises(ValueError):
            parse_agents_list("0 2")
        with self.assertRaises(ValueError):
            parse_agents_list("1025")

    def test_build_sweep_environment_sets_run_root_and_knobs(self) -> None:
        config = SweepLaunchConfig(
            agents=[2, 4],
            run_seconds=90,
            workload_grace_seconds=30,
            memory_workers=4,
            memory_mb=128,
            vcpus_per_agent=2,
            llm_context_kb=128,
            llm_prompt_repeat=4,
            llm_max_tokens=1024,
            llm_load_mode="sustained_prefill",
            llm_request_timeout_seconds=180,
            llm_inter_task_sleep_ms=750,
            sudo_password="secret",
            run_name="Nightly Smoke",
            workload="coding",
            executor="firecracker",
        )

        env = build_sweep_environment(config, timestamp="20260613-120000")

        self.assertEqual(env["AGENTS_LIST"], "2 4")
        self.assertEqual(env["RUN_SECONDS"], "90")
        self.assertEqual(env["WORKLOAD_GRACE_SECONDS"], "30")
        self.assertEqual(env["AAB_MEMORY_WORKERS"], "4")
        self.assertEqual(env["AAB_MEMORY_MB"], "128")
        self.assertEqual(env["AAB_VCPUS_PER_AGENT"], "2")
        self.assertEqual(env["AAB_LLM_CONTEXT_KB"], "128")
        self.assertEqual(env["AAB_LLM_PROMPT_REPEAT"], "4")
        self.assertEqual(env["AAB_LLM_MAX_TOKENS"], "1024")
        self.assertEqual(env["AAB_LLM_LOAD_MODE"], "sustained_prefill")
        self.assertEqual(env["AAB_LLM_REQUEST_TIMEOUT_SECONDS"], "180")
        self.assertEqual(env["AAB_LLM_INTER_TASK_SLEEP_MS"], "750")
        self.assertEqual(env["AAB_WORKLOAD"], "coding")
        self.assertEqual(env["AAB_EXECUTOR"], "firecracker")
        self.assertEqual(env["SUDO_PASSWORD"], "secret")
        self.assertEqual(env["SWEEP_ROOT"], "runs/dashboard-nightly-smoke-20260613-120000")

    def test_build_sweep_environment_defaults_to_requested_vm_and_context_shape(self) -> None:
        env = build_sweep_environment(SweepLaunchConfig(agents=[1, 2, 4]), timestamp="20260613-120000")

        self.assertEqual(env["AGENTS_LIST"], "1 2 4")
        self.assertEqual(env["AAB_VCPUS_PER_AGENT"], "8")
        self.assertEqual(env["AAB_LLM_CONTEXT_KB"], "2")

    def test_start_sweep_returns_status_without_deadlocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_queue: multiprocessing.Queue = multiprocessing.Queue()
            process = multiprocessing.Process(target=_start_sweep_worker, args=(tmp, result_queue))
            process.start()
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
                self.fail("start_sweep did not return; likely lock re-entry deadlock")
            self.assertEqual(process.exitcode, 0)
            try:
                status = result_queue.get_nowait()
            except queue.Empty as exc:
                raise AssertionError("start_sweep returned no status") from exc
            self.assertTrue(status["running"])
            self.assertIn("dashboard-deadlock-test-", status["sweep_root"])

    def test_start_sweep_requires_vllm_ready_before_launching_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = DashboardState(Path(tmp))

            with (
                patch(
                    "aab_framework.dashboard.subprocess.run",
                    return_value=SimpleNamespace(returncode=7, stderr="curl: connection refused"),
                ),
                patch("aab_framework.dashboard.subprocess.Popen") as popen,
            ):
                with self.assertRaisesRegex(RuntimeError, "vLLM is unavailable"):
                    state.start_sweep(SweepLaunchConfig(agents=[1]))

            popen.assert_not_called()

    def test_require_vllm_ready_includes_probe_error_details(self) -> None:
        with patch(
            "aab_framework.dashboard.subprocess.run",
            return_value=SimpleNamespace(returncode=7, stderr="curl: connection refused"),
        ):
            with self.assertRaisesRegex(RuntimeError, "curl: connection refused"):
                require_vllm_ready()

    def test_list_runs_uses_dashboard_run_name_for_display(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            run_dir = runs_dir / "dashboard-nightly-smoke-20260613-120000"
            run_dir.mkdir(parents=True)
            (run_dir / "dashboard-run.json").write_text(
                json.dumps(
                    {
                        "display_name": "Nightly Smoke",
                        "sweep_root": "runs/dashboard-nightly-smoke-20260613-120000",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            runs = list_runs(runs_dir)

            self.assertEqual(runs[0]["name"], "dashboard-nightly-smoke-20260613-120000")
            self.assertEqual(runs[0]["display_name"], "Nightly Smoke")

    def test_ensure_aligned_metrics_generates_missing_dashboard_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "runs" / "dashboard-manual"
            point = run / "agents_1"
            metrics = point / "metrics"
            results = point / "results"
            metrics.mkdir(parents=True)
            results.mkdir(parents=True)
            (run / "dashboard-run.json").write_text(
                json.dumps(
                    {
                        "display_name": "manual",
                        "run_seconds": 180,
                        "workload_grace_seconds": 60,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (run / "sweep_summary.csv").write_text(
                "agents,vm_results,completed_tasks,failed_tasks,vllm_ok\n1,1,1,0,1\n",
                encoding="utf-8",
            )
            (results / "summary.json").write_text(
                json.dumps({"vm_results": 1, "completed_tasks": 1, "failed_tasks": 0, "vllm_ok": 1}),
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
                "1,10,8,2,0,90,1,1,1\n",
                encoding="utf-8",
            )
            (metrics / "gpu.csv").write_text(
                "timestamp,index,utilization_gpu_pct,utilization_memory_pct,memory_used_mib,memory_total_mib,power_draw_w,temperature_c\n"
                "1,0,50,20,200,1000,100,35\n",
                encoding="utf-8",
            )

            generated = ensure_aligned_metrics(run)

            self.assertTrue(generated)
            self.assertTrue((run / "aligned_metrics.csv").exists())
            report = load_run_report(run)
            self.assertEqual(report["points"][0]["success_rate_pct"], 100)
            self.assertEqual(report["points"][0]["lat_ok_p95_ms"], 100)

    def test_gpu_p95_metrics_ignore_zero_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "runs" / "dashboard-gpu-p95"
            point = run / "agents_1"
            metrics = point / "metrics"
            results = point / "results"
            metrics.mkdir(parents=True)
            results.mkdir(parents=True)
            (run / "dashboard-run.json").write_text(
                json.dumps({"display_name": "gpu p95", "run_seconds": 60, "workload_grace_seconds": 0}) + "\n",
                encoding="utf-8",
            )
            (run / "sweep_summary.csv").write_text(
                "agents,vm_results,completed_tasks,failed_tasks,vllm_ok\n1,1,1,0,1\n",
                encoding="utf-8",
            )
            (results / "summary.json").write_text(
                json.dumps({"vm_results": 1, "completed_tasks": 1, "failed_tasks": 0, "vllm_ok": 1}),
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
                "1,10,8,2,0,90,1,1,1\n",
                encoding="utf-8",
            )
            (metrics / "gpu.csv").write_text(
                "timestamp,index,utilization_gpu_pct,utilization_memory_pct,memory_used_mib,memory_total_mib,power_draw_w,temperature_c,memory_used_pct,sm_active_pct,tensor_active_pct,dram_active_pct,gpu_metrics_backend,dcgm_metrics_backend\n"
                "1,0,0,0,0,1000,0,30,0,0,0,0,dcgmi,dcgmi\n"
                "2,0,0,0,0,1000,0,30,0,0,0,0,dcgmi,dcgmi\n"
                "3,0,0,0,0,1000,0,30,0,0,0,0,dcgmi,dcgmi\n"
                "4,0,100,40,1000,1000,200,35,50,60,70,80,dcgmi,dcgmi\n",
                encoding="utf-8",
            )

            ensure_aligned_metrics(run)
            report = load_run_report(run)
            point_report = report["points"][0]

            self.assertEqual(point_report["gpu_util_p95_pct"], 100)
            self.assertEqual(point_report["gpu_memctrl_p95_pct"], 40)
            self.assertEqual(point_report["gpu_power_p95_w"], 200)
            self.assertEqual(point_report["gpu_mem_used_p95_mib"], 1000)
            self.assertEqual(point_report["sm_active_p95_pct"], 60)
            self.assertEqual(point_report["tensor_active_p95_pct"], 70)
            self.assertEqual(point_report["dram_active_p95_pct"], 80)
            self.assertEqual(point_report["gpu_mem_used_pct_p95"], 50)

    def test_dram_max_percentage_uses_580_gbps_peak_bandwidth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "runs" / "dashboard-dram-pct"
            point = run / "agents_1"
            metrics = point / "metrics"
            results = point / "results"
            metrics.mkdir(parents=True)
            results.mkdir(parents=True)
            (run / "dashboard-run.json").write_text(
                json.dumps({"display_name": "dram pct", "run_seconds": 60, "workload_grace_seconds": 0}) + "\n",
                encoding="utf-8",
            )
            (run / "sweep_summary.csv").write_text(
                "agents,vm_results,completed_tasks,failed_tasks,vllm_ok\n1,1,1,0,1\n",
                encoding="utf-8",
            )
            (results / "summary.json").write_text(
                json.dumps({"vm_results": 1, "completed_tasks": 1, "failed_tasks": 0, "vllm_ok": 1}),
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
                "1,10,8,2,0,90,1,1,1\n",
                encoding="utf-8",
            )
            (metrics / "gpu.csv").write_text(
                "timestamp,index,utilization_gpu_pct,utilization_memory_pct,memory_used_mib,memory_total_mib,power_draw_w,temperature_c\n"
                "1,0,50,20,200,1000,100,35\n",
                encoding="utf-8",
            )
            (metrics / "amd_pcm_memory.csv").write_text(
                "Total Mem Bw (GB/s)    |           290.00 |          145.00 |          145.00 |\n",
                encoding="utf-8",
            )

            ensure_aligned_metrics(run)
            report = load_run_report(run)

            self.assertEqual(report["points"][0]["dram_bw_max_gbps"], 290)
            self.assertEqual(report["points"][0]["dram_bw_max_pct_of_peak"], 50)

    def test_load_run_report_backfills_dram_peak_percentage_for_existing_aligned_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "runs" / "dashboard-old-dram"
            point = run / "agents_1"
            results = point / "results"
            results.mkdir(parents=True)
            (run / "aligned_metrics.csv").write_text(
                "agents,vm_results,completed,failed,success_rate_pct,dram_bw_max_gbps\n"
                "1,1,1,0,100,348.28\n",
                encoding="utf-8",
            )
            (run / "sweep_summary.csv").write_text(
                "agents,vm_results,completed_tasks,failed_tasks,vllm_ok\n1,1,1,0,1\n",
                encoding="utf-8",
            )
            (point / "firecracker-run.json").write_text(
                json.dumps({"vm_count": 1, "agents": [{"vm_id": "agent-000"}]}) + "\n",
                encoding="utf-8",
            )
            (results / "summary.json").write_text(
                json.dumps({"vm_results": 1, "completed_tasks": 1, "failed_tasks": 0, "vllm_ok": 1}),
                encoding="utf-8",
            )

            report = load_run_report(run)

            self.assertEqual(report["points"][0]["dram_bw_max_gbps"], 348.28)
            self.assertEqual(report["points"][0]["dram_bw_max_pct_of_peak"], 60.05)

    def test_dashboard_charts_render_y_axis_ticks(self) -> None:
        script = Path("aab_framework/dashboard_static/app.js").read_text(encoding="utf-8")

        self.assertIn("drawYAxis", script)
        self.assertIn("formatAxisTick", script)
        self.assertIn("ctx.textAlign = \"right\"", script)
        self.assertIn("maxTickCount", script)

    def test_dashboard_start_sweep_uses_name_field_for_runs(self) -> None:
        html = Path("aab_framework/dashboard_static/index.html").read_text(encoding="utf-8")
        script = Path("aab_framework/dashboard_static/app.js").read_text(encoding="utf-8")

        self.assertIn('id="agentsInput" class="control" value="1 2 4 8 16 32 64 128"', html)
        self.assertIn('id="vcpuInput" class="control" type="number" value="8"', html)
        self.assertIn('id="llmContextKbInput" class="control" type="number" value="2"', html)
        self.assertIn("Name<input id=\"runNameInput\"", html)
        self.assertIn("Context KiB<input id=\"llmContextKbInput\"", html)
        self.assertIn("Prompt repeat<input id=\"llmPromptRepeatInput\"", html)
        self.assertIn("Max tokens", html)
        self.assertIn("id=\"llmMaxTokensInput\"", html)
        self.assertIn("Load mode<select id=\"llmLoadModeSelect\"", html)
        self.assertIn("Request timeout<input id=\"llmRequestTimeoutInput\"", html)
        self.assertIn("Inter-task sleep<input id=\"llmInterTaskSleepInput\"", html)
        self.assertIn("Workload<select id=\"workloadSelect\"", html)
        self.assertIn("Executor<select id=\"executorSelect\"", html)
        self.assertNotIn("Label<input id=\"labelInput\"", html)
        self.assertIn("run_name: $(\"runNameInput\").value", script)
        self.assertIn("llm_context_kb: Number($(\"llmContextKbInput\").value)", script)
        self.assertIn("llm_prompt_repeat: Number($(\"llmPromptRepeatInput\").value)", script)
        self.assertIn("llm_max_tokens: Number($(\"llmMaxTokensInput\").value)", script)
        self.assertIn("llm_load_mode: $(\"llmLoadModeSelect\").value", script)
        self.assertIn("llm_request_timeout_seconds: Number($(\"llmRequestTimeoutInput\").value)", script)
        self.assertIn("llm_inter_task_sleep_ms: Number($(\"llmInterTaskSleepInput\").value)", script)
        self.assertIn("workload: $(\"workloadSelect\").value", script)
        self.assertIn("executor: $(\"executorSelect\").value", script)
        self.assertIn("run.display_name", script)

    def test_start_dashboard_script_defaults_to_port_80(self) -> None:
        script = Path("bin/start_dashboard.sh").read_text(encoding="utf-8")

        self.assertIn('PORT="${PORT:-80}"', script)

    def test_dashboard_exposes_registered_workload_and_executor_plugins(self) -> None:
        from aab_framework.dashboard import dashboard_plugins_payload

        payload = dashboard_plugins_payload()

        self.assertEqual(payload["workloads"][0]["name"], "coding")
        self.assertEqual(payload["executors"][0]["name"], "firecracker")
        self.assertEqual(payload["challenge_reviews"]["workloads"]["coding"]["verdict"], "PASS")
        self.assertEqual(payload["challenge_reviews"]["executors"]["firecracker"]["verdict"], "PASS")

    def test_sweep_results_table_includes_gpu_utilization_and_memory_bandwidth(self) -> None:
        script = Path("aab_framework/dashboard_static/app.js").read_text(encoding="utf-8")
        html = Path("aab_framework/dashboard_static/index.html").read_text(encoding="utf-8")

        self.assertIn("GPU Metrics", html)
        self.assertIn("gpuUtilChart", html)
        self.assertIn("gpuMemoryChart", html)
        self.assertIn("gpuActivityChart", html)
        self.assertIn('drawChart("gpuUtilChart"', script)
        self.assertIn('drawChart("gpuMemoryChart"', script)
        self.assertIn('drawChart("gpuActivityChart"', script)
        for key in [
            "gpu_util_p95_pct",
            "gpu_util_max_pct",
            "gpu_active_sample_pct",
            "gpu_power_p95_w",
        ]:
            self.assertIn(f'key: "{key}"', script)
        for key in [
            "gpu_memctrl_p95_pct",
            "gpu_memctrl_max_pct",
            "gpu_memctrl_active_sample_pct",
            "gpu_mem_used_p95_mib",
            "gpu_mem_used_pct_p95",
            "gpu_mem_used_pct_max",
        ]:
            self.assertIn(f'key: "{key}"', script)
        for key in [
            "sm_active_p95_pct",
            "sm_occupancy_p95_pct",
            "tensor_active_p95_pct",
            "dram_active_p95_pct",
            "fp16_active_p95_pct",
            "fp32_active_p95_pct",
        ]:
            self.assertIn(f'key: "{key}"', script)

        self.assertIn("GPU Active Max", script)
        self.assertNotIn("GPU Mem BW Max", script)
        self.assertIn("GPU MemCtrl Max", script)

    def test_dashboard_uses_business_cpu_and_three_gpu_charts(self) -> None:
        script = Path("aab_framework/dashboard_static/app.js").read_text(encoding="utf-8")
        html = Path("aab_framework/dashboard_static/index.html").read_text(encoding="utf-8")

        self.assertIn("Business Performance", html)
        self.assertIn("businessChart", html)
        self.assertIn("CPU Metrics", html)
        self.assertIn("cpuChart", html)
        self.assertIn("GPU Metrics", html)
        self.assertIn("GPU Utilization", html)
        self.assertIn("GPU Memory", html)
        self.assertIn("GPU Engine Activity", html)
        self.assertIn("gpuUtilChart", html)
        self.assertIn("gpuMemoryChart", html)
        self.assertIn("gpuActivityChart", html)
        self.assertIn('drawChart("businessChart"', script)
        self.assertIn('drawChart("cpuChart"', script)
        self.assertIn('drawChart("gpuUtilChart"', script)
        self.assertIn('drawChart("gpuMemoryChart"', script)
        self.assertIn('drawChart("gpuActivityChart"', script)
        self.assertNotIn('drawChart("gpuChart"', script)
        for removed in [
            "successChart",
            "throughputChart",
            "systemChart",
            "gpuScalingChart",
            "gpuComputeChart",
            "GPU Scaling",
            "GPU Compute",
            "CPU and DRAM",
        ]:
            self.assertNotIn(removed, html)
        for removed_chart in [
            "successChart",
            "throughputChart",
            "systemChart",
            "gpuScalingChart",
            "gpuComputeChart",
        ]:
            self.assertNotIn(f'drawChart("{removed_chart}"', script)
        self.assertIn("tasks/min", script)
        self.assertIn("latency p95 s", script)
        self.assertIn("cpu max", script)
        self.assertIn("dram max", script)
        self.assertIn("SM Active P95", script)
        self.assertIn("Tensor Active P95", script)
        self.assertIn("DRAM Active P95", script)
        for key in [
            "sm_active_p95_pct",
            "sm_occupancy_p95_pct",
            "tensor_active_p95_pct",
            "dram_active_p95_pct",
            "fp16_active_p95_pct",
            "fp32_active_p95_pct",
        ]:
            self.assertIn(f'key: "{key}"', script)

    def test_sweep_results_table_removes_fail_column_and_title_cases_headers(self) -> None:
        script = Path("aab_framework/dashboard_static/app.js").read_text(encoding="utf-8")

        self.assertIn("\"Agents\"", script)
        self.assertIn("\"Success\"", script)
        self.assertNotIn("\"CPU P95\"", script)
        self.assertNotIn("\"VM\"", script)
        self.assertNotIn("\"Done\"", script)
        self.assertNotIn("\"fail\"", script)
        self.assertNotIn("p.cpu_p95_pct", script)
        self.assertNotIn("p.failed,\n", script)
        self.assertNotIn("p.vm_results,\n", script)
        self.assertNotIn("p.completed,\n", script)

    def test_cpu_metrics_chart_includes_cpu_and_dram_series(self) -> None:
        script = Path("aab_framework/dashboard_static/app.js").read_text(encoding="utf-8")

        self.assertIn('drawChart("cpuChart"', script)
        self.assertIn('key: "cpu_max_pct"', script)
        self.assertIn('label: "cpu max"', script)
        self.assertIn('key: "cpu_p95_pct"', script)
        self.assertIn('label: "cpu p95"', script)
        self.assertIn('key: "dram_bw_max_gbps"', script)
        self.assertIn('label: "dram max"', script)
        self.assertIn('key: "dram_bw_max_pct_of_peak"', script)
        self.assertIn('label: "dram %peak"', script)
        self.assertNotIn('key: "dram_bw_p95_gbps", label: "dram p95"', script)

    def test_coding_sweep_uses_dcgm_for_gpu_metrics(self) -> None:
        script = Path("bin/run_coding_firecracker_sweep.sh").read_text(encoding="utf-8")

        self.assertIn('AAB_DCGMI_BIN="${AAB_DCGMI_BIN:-dcgmi}"', script)
        self.assertIn("start_gpu_metrics_dcgm", script)
        self.assertIn("python3 -m aab_framework.dcgm", script)
        self.assertNotIn("nvidia-smi", script)
        self.assertNotIn("start_gpu_metrics_nvidia_smi", script)
        self.assertNotIn("start_gpu_metrics_nvtop", script)

    def test_coding_sweep_stops_dcgm_child_processes_before_parent(self) -> None:
        script = Path("bin/run_coding_firecracker_sweep.sh").read_text(encoding="utf-8")
        stop_body = script.split("stop_gpu_metrics() {", 1)[1].split("\n}\n", 1)[0]

        child_stop = 'pkill -TERM -P "${GPU_METRICS_PID}"'
        parent_stop = 'kill "${GPU_METRICS_PID}"'
        self.assertIn(child_stop, stop_body)
        self.assertIn(parent_stop, stop_body)
        self.assertLess(stop_body.index(child_stop), stop_body.index(parent_stop))

    def test_dashboard_line_charts_use_polished_rendering_helpers(self) -> None:
        script = Path("aab_framework/dashboard_static/app.js").read_text(encoding="utf-8")
        style = Path("aab_framework/dashboard_static/style.css").read_text(encoding="utf-8")

        self.assertIn("drawSeriesArea", script)
        self.assertIn("drawSmoothLine", script)
        self.assertIn("drawDataPoints", script)
        self.assertIn("bindChartHover", script)
        self.assertIn("handleChartPointerMove", script)
        self.assertIn("drawHoverTooltip", script)
        self.assertIn("mousemove", script)
        self.assertIn("mouseleave", script)
        self.assertNotIn("drawEndValueLabel", script)
        self.assertIn("createLinearGradient", script)
        self.assertIn("quadraticCurveTo", script)
        self.assertIn("ctx.arc", script)
        self.assertIn("box-shadow", style)

    def test_dashboard_hover_tooltip_avoids_canvas_resize_and_roundrect_compatibility_bugs(self) -> None:
        script = Path("aab_framework/dashboard_static/app.js").read_text(encoding="utf-8")

        self.assertIn("const ratio = window.devicePixelRatio || 1", script)
        self.assertIn("canvas.dataset.logicalHeight", script)
        self.assertIn("const height = Number(canvas.dataset.logicalHeight)", script)
        self.assertNotIn("const height = Number(canvas.getAttribute(\"height\"))", script)
        self.assertIn("ctx.setTransform(ratio, 0, 0, ratio, 0, 0)", script)
        self.assertIn("canvas.dataset.pixelWidth", script)
        self.assertIn("hoverPoint", script)
        self.assertIn("placeTooltip", script)
        self.assertIn("clamp(", script)
        self.assertNotIn("Math.min(Math.max(tooltipAnchor", script)
        self.assertIn("drawRoundedRect", script)
        self.assertIn("quadraticCurveTo(x + rectWidth", script)
        self.assertNotIn("ctx.roundRect", script)


if __name__ == "__main__":
    unittest.main()
