from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from aab_framework.cli import main
from aab_framework.dashboard import load_run_report, serve_with_fallback
from aab_framework.team_v2 import DEFAULT_CONTEXT_LENGTH, SUPPORTED_CONTEXT_LENGTHS, TeamOrchestrator, TeamRunConfig
from aab_framework.team_v2.docker_runtime import DockerRuntime
from aab_framework.team_v2.firecracker_runner import prepare_firecracker_team_plan
from aab_framework.team_v2.metrics_wrapper import build_sweep_from_child_runs, refresh_run_metrics
from aab_framework.team_v2.planner import TaskPlanner
from aab_framework.team_v2.repo_context import RepoContextBuilder, RepoWorkspaceManager
from aab_framework.team_v2.repair_loop import RepairLoopController
from aab_framework.team_v2.reviewer import PatchReviewerAgent
from aab_framework.team_v2.schemas import ReviewResult, VerifierResult
from aab_framework.team_v2.sweep import SWEEP_WORKLOAD_TYPE, TeamSweepOrchestrator
from aab_framework.team_v2.verifier import TestVerifierAgent
from aab_framework.workloads.mini_swe_agent_team_v2 import build_mini_swe_agent_team_v2_workload_spec


class MiniSweAgentTeamV2Tests(unittest.TestCase):
    def test_config_and_planner_propagate_default_1k_context(self) -> None:
        config = TeamRunConfig(num_agents=2, parallelism=2)
        workers = TaskPlanner().build_workers(config)
        plans = TaskPlanner().build_issue_plans(config, workers)

        self.assertEqual(DEFAULT_CONTEXT_LENGTH, 1024)
        self.assertEqual(config.context_length, DEFAULT_CONTEXT_LENGTH)
        self.assertEqual(len(workers), 2)
        self.assertEqual(len(plans), 2)
        self.assertTrue(all(worker.effective_context_length == DEFAULT_CONTEXT_LENGTH for worker in workers))
        self.assertEqual({plan.agent_id for plan in plans}, {"agent-0", "agent-1"})

    def test_context_length_sweep_values_are_supported(self) -> None:
        self.assertEqual(SUPPORTED_CONTEXT_LENGTHS, (1024, 2048, 4096, 8192))
        for context_length in SUPPORTED_CONTEXT_LENGTHS:
            config = TeamRunConfig(num_agents=1, parallelism=1, context_length=context_length)
            workers = TaskPlanner().build_workers(config)
            plans = TaskPlanner().build_issue_plans(config, workers)

            self.assertEqual(config.context_length, context_length)
            self.assertEqual(workers[0].effective_context_length, context_length)
            self.assertEqual(plans[0].max_rounds, config.max_rounds_per_issue)

    def test_docker_runtime_default_config_and_command_encapsulation(self) -> None:
        config = TeamRunConfig()
        runtime = DockerRuntime(config)

        self.assertEqual(config.runtime_type, "docker")
        self.assertEqual(runtime.to_result_config()["type"], "docker")
        self.assertEqual(runtime.to_result_config()["image"], "aab-mini-swe-agent:latest")
        self.assertEqual(runtime.build_exec_command("container-1", ["python3", "-V"])[:3], ["docker", "exec", "container-1"])

    def test_reviewer_verifier_and_repair_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patch = root / "patch.diff"
            log = root / "test.log"
            patch.write_text("diff --git a/a.py b/a.py\n+ok\n", encoding="utf-8")
            log.write_text("1 passed\n", encoding="utf-8")

            review = PatchReviewerAgent().review(patch, log)
            verify = TestVerifierAgent().verify_synthetic(log)
            retry, reason = RepairLoopController().should_retry(
                round_id=0,
                review=review,
                verifier=verify,
                config=TeamRunConfig(max_rounds_per_issue=2),
            )

        self.assertEqual(review.review_status, "approved")
        self.assertTrue(verify.verified)
        self.assertFalse(retry)
        self.assertIsNone(reason)

    def test_reviewer_allows_empty_patch_for_synthetic_verified_task_as_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patch = root / "patch.diff"
            log = root / "test.log"
            patch.write_text("", encoding="utf-8")
            log.write_text("ok\nCOMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n", encoding="utf-8")

            review = PatchReviewerAgent().review(patch, log)

        self.assertEqual(review.review_status, "warning")

    def test_repo_context_builder_scans_real_repo_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "app.py").write_text("import os\n\ndef handle_issue():\n    return os.getcwd()\n", encoding="utf-8")
            (repo / "README.md").write_text("# Large service\nThis repository has payment and scheduler code.\n", encoding="utf-8")
            issue_dir = root / "issue"
            config = TeamRunConfig(
                repo_context_enabled=True,
                repo_source=str(repo),
                repo_context_max_files=10,
                repo_context_max_bytes=10_000,
                repo_context_bundle_max_bytes=4096,
            )
            plan = TaskPlanner().build_issue_plans(config, TaskPlanner().build_workers(config))[0]

            result = RepoContextBuilder(config).build(plan, issue_dir)
            bundle_text = Path(result.context_bundle_path).read_text(encoding="utf-8")
            bundle_exists = Path(result.context_bundle_path).exists()
            index_exists = Path(result.index_path).exists()
            augmented_prompt = result.augment_prompt("Fix the bug.")

        self.assertTrue(result.enabled)
        self.assertEqual(result.files_scanned, 2)
        self.assertGreater(result.bytes_scanned, 40)
        self.assertGreaterEqual(result.symbols_extracted, 1)
        self.assertTrue(bundle_exists)
        self.assertTrue(index_exists)
        self.assertIn("handle_issue", bundle_text)
        self.assertIn("Repo context bundle", augmented_prompt)

    def test_repo_context_builder_reads_git_history_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess_run = __import__("subprocess").run
            subprocess_run(["git", "init"], cwd=repo, check=True, stdout=__import__("subprocess").DEVNULL)
            subprocess_run(["git", "config", "user.email", "aab@example.com"], cwd=repo, check=True)
            subprocess_run(["git", "config", "user.name", "AAB"], cwd=repo, check=True)
            for index in range(3):
                (repo / "module.py").write_text(f"def value():\n    return {index}\n", encoding="utf-8")
                subprocess_run(["git", "add", "module.py"], cwd=repo, check=True)
                subprocess_run(["git", "commit", "-m", f"change {index}"], cwd=repo, check=True, stdout=__import__("subprocess").DEVNULL)
            subprocess_run(["git", "gc"], cwd=repo, check=True, stdout=__import__("subprocess").DEVNULL)
            config = TeamRunConfig(
                repo_context_enabled=True,
                repo_source=str(repo),
                repo_context_include_git_history=True,
                repo_context_git_history_max_bytes=1_000_000,
            )
            plan = TaskPlanner().build_issue_plans(config, TaskPlanner().build_workers(config))[0]

            result = RepoContextBuilder(config).build(plan, root / "issue")
            index = json.loads(Path(result.index_path).read_text(encoding="utf-8"))

        self.assertGreater(result.git_history_bytes_scanned, 0)
        self.assertGreater(result.git_history_files_scanned, 0)
        self.assertGreaterEqual(result.git_history_sec, 0)
        self.assertTrue(index["git_history"]["files"])

    def test_repo_context_builder_runs_pytest_collect_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
            config = TeamRunConfig(
                repo_context_enabled=True,
                repo_source=str(repo),
                repo_context_pytest_collect=True,
                repo_context_pytest_command="python3 -c \"print('collected 1 item')\"",
                repo_context_pytest_timeout_sec=10,
            )
            plan = TaskPlanner().build_issue_plans(config, TaskPlanner().build_workers(config))[0]

            result = RepoContextBuilder(config).build(plan, root / "issue")
            collect_log = Path(result.pytest_collect_log_path).read_text(encoding="utf-8")

        self.assertEqual(result.pytest_collect_status, "passed")
        self.assertIn("collected 1 item", collect_log)
        self.assertGreaterEqual(result.pytest_collect_sec, 0)

    def test_team_run_records_repo_context_in_issue_and_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            for index in range(6):
                (repo / f"module_{index}.py").write_text(
                    f"import json\nclass Service{index}:\n    def method_{index}(self):\n        return {index}\n",
                    encoding="utf-8",
                )
            result = TeamOrchestrator(
                TeamRunConfig(
                    num_agents=1,
                    parallelism=1,
                    out_dir=str(root / "runs"),
                    repo_context_enabled=True,
                    repo_source=str(repo),
                    repo_context_max_files=20,
                    repo_context_max_bytes=100_000,
                    repo_context_bundle_max_bytes=8192,
                )
            ).run()
            data = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
            bundle_exists = Path(data["issues"][0]["repo_context"]["context_bundle_path"]).exists()

        issue = data["issues"][0]
        round0 = issue["rounds"][0]
        self.assertEqual(issue["repo_context"]["files_scanned"], 6)
        self.assertGreater(issue["repo_context"]["bytes_scanned"], 100)
        self.assertEqual(round0["repo_context"]["files_scanned"], 6)
        self.assertGreaterEqual(round0["stage_timings"]["scan_repo_sec"], 0)
        self.assertGreaterEqual(round0["stage_timings"]["build_context_sec"], 0)
        self.assertTrue(bundle_exists)

    def test_repo_workspace_manager_creates_independent_git_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "app.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")
            subprocess_run = __import__("subprocess").run
            subprocess_run(["git", "init"], cwd=repo, check=True, stdout=__import__("subprocess").DEVNULL)
            subprocess_run(["git", "config", "user.email", "aab@example.com"], cwd=repo, check=True)
            subprocess_run(["git", "config", "user.name", "AAB"], cwd=repo, check=True)
            subprocess_run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess_run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=__import__("subprocess").DEVNULL)
            config = TeamRunConfig(
                task_source="local",
                repo_source=str(repo),
                task="Inspect",
                repo_workspace_mode="worktree",
            )
            plans = TaskPlanner().build_issue_plans(config, TaskPlanner().build_workers(config))

            first = RepoWorkspaceManager(config).prepare(plans[0], root / "issue-a")
            second = RepoWorkspaceManager(config).prepare(plans[0], root / "issue-b")
            first_has_app = Path(first.path, "app.py").exists()
            second_has_app = Path(second.path, "app.py").exists()

        self.assertEqual(first.mode, "worktree")
        self.assertEqual(second.mode, "worktree")
        self.assertTrue(Path(first.path).is_absolute())
        self.assertTrue(Path(second.path).is_absolute())
        self.assertNotEqual(first.path, str(repo))
        self.assertNotEqual(first.path, second.path)
        self.assertTrue(first_has_app)
        self.assertTrue(second_has_app)

    def test_mock_team_run_writes_required_result_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = TeamOrchestrator(
                TeamRunConfig(
                    num_agents=2,
                    parallelism=2,
                    out_dir=tmp,
                    sweep={
                        "agent_count": 2,
                        "context_length": DEFAULT_CONTEXT_LENGTH,
                        "case_id": "agents2_ctx1024",
                        "experiment_mode": "fixed_llm",
                        "repeat": 1,
                    },
                )
            ).run()
            result_path = Path(result["result_path"])
            data = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(data["workload_type"], "mini_swe_agent_team_v2")
        self.assertIn("team", data)
        self.assertEqual(data["team"]["runtime"], "DockerRuntime")
        self.assertEqual(len(data["issues"]), 2)
        self.assertEqual(len(data["agents"]), 2)
        self.assertEqual(data["config"]["runtime"]["type"], "docker")
        self.assertEqual(data["config"]["sweep"]["case_id"], "agents2_ctx1024")
        self.assertEqual(data["config"]["vllm"]["max_model_len"], 4096)
        self.assertTrue(all(agent["effective_context_length"] == DEFAULT_CONTEXT_LENGTH for agent in data["agents"]))
        self.assertTrue(all(issue["rounds"][0]["verifier_result"] for issue in data["issues"]))
        self.assertTrue(all(issue["rounds"][0]["review_result"] for issue in data["issues"]))
        for issue in data["issues"]:
            round0 = issue["rounds"][0]
            self.assertEqual(round0["requested_context_length"], DEFAULT_CONTEXT_LENGTH)
            self.assertEqual(round0["effective_context_length"], DEFAULT_CONTEXT_LENGTH)
            self.assertEqual(round0["context_source"], "TeamRunConfig.context_length")
            self.assertIn("stage_timings", round0)
            self.assertIn("prepare_repo_sec", round0["stage_timings"])
            self.assertIn("llm_generation_sec", round0["stage_timings"])
            self.assertIn("verify_sec", round0["stage_timings"])
        self.assertIn("attribution_method", data["overall_metrics_summary"])
        self.assertIn("metrics_summary", data)
        self.assertIn("metrics_timeline", data)
        self.assertIn("metrics_window", data)
        self.assertEqual(data["metrics_summary"]["cpu"]["unit"], "percent")
        self.assertEqual(data["metrics_timeline"]["system"], "metrics/system_metrics.jsonl")

    def test_cli_run_outputs_result_and_ui_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "run",
                        "--workload",
                        "mini_swe_agent_team_v2",
                        "--num-agents",
                        "1",
                        "--parallelism",
                        "1",
                        "--out-dir",
                        tmp,
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = output.getvalue()
        self.assertIn("Run ID:", payload)
        self.assertIn("Result:", payload)
        self.assertIn("UI available at:", payload)

    def test_cli_run_with_agent_counts_outputs_single_sweep_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "run",
                        "--workload",
                        "mini_swe_agent_team_v2",
                        "--agent-counts",
                        "1 2",
                        "--parallelism",
                        "2",
                        "--out-dir",
                        tmp,
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertIn("Run ID: team-v2-sweep-", output.getvalue())

    def test_cli_agent_sweep_outputs_sweep_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "run",
                        "--workload",
                        "mini_swe_agent_team_v2",
                        "--agent-sweep",
                        "1,2",
                        "--parallelism",
                        "2",
                        "--out-dir",
                        tmp,
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = output.getvalue()
        self.assertIn("Sweep Group:", payload)
        self.assertIn("num_agents=1", payload)
        self.assertIn("num_agents=2", payload)

    def test_dashboard_loads_team_v2_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = TeamOrchestrator(TeamRunConfig(num_agents=1, out_dir=tmp)).run()
            report = load_run_report(Path(run["run_dir"]))

        self.assertEqual(report["points"][0]["agents"], 1)
        self.assertEqual(len(report["issues"]), 1)
        self.assertEqual(len(report["agents"]), 1)

    def test_sweep_run_writes_group_file_with_independent_child_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = TeamSweepOrchestrator(
                TeamRunConfig(num_agents=1, parallelism=4, out_dir=tmp),
                [1, 2, 4],
                context_lengths=[1024, 2048],
                repeats=1,
                experiment_mode="fixed_llm",
                max_active_llm_requests=2,
            ).run()
            data = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
            sweep = json.loads((Path(result["run_dir"]) / "sweep.json").read_text(encoding="utf-8"))
            report = load_run_report(Path(result["run_dir"]))
            summary_exists = (Path(result["run_dir"]) / "team_sweep_summary.csv").exists()

        self.assertEqual(data["workload_type"], SWEEP_WORKLOAD_TYPE)
        self.assertEqual(len(data["points"]), 6)
        self.assertEqual({point["context_length"] for point in data["points"]}, {1024, 2048})
        self.assertEqual({point["experiment_mode"] for point in data["points"]}, {"fixed_llm"})
        self.assertEqual({point["max_active_llm_requests"] for point in data["points"]}, {1, 2})
        self.assertEqual(len(report["points"]), 6)
        self.assertEqual(sweep["parameters"], {"agent_counts": [1, 2, 4], "context_lengths": [1024, 2048], "repeats": 1})
        self.assertEqual(len(sweep["runs"]), 6)
        self.assertEqual(len(sweep["scaling_metrics"]), 6)
        self.assertTrue(all((Path(item["result_path"]).name == "result.json") for item in sweep["runs"]))
        self.assertTrue(summary_exists)

    def test_cli_sweep_outputs_context_cases_and_experiment_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "sweep",
                        "--workload",
                        "mini_swe_agent_team_v2",
                        "--agent-counts",
                        "1,2",
                        "--context-lengths",
                        "1024,2048",
                        "--repeats",
                        "1",
                        "--experiment-mode",
                        "fixed_llm",
                        "--max-active-llm-requests",
                        "2",
                        "--out-dir",
                        tmp,
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = output.getvalue()
        self.assertIn("Sweep Group:", payload)
        self.assertIn("case_id=agents1_ctx1024_r1", payload)
        self.assertIn("case_id=agents2_ctx2048_r1", payload)

    def test_cli_accepts_process_runtime_for_host_mini_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "run",
                        "--workload",
                        "mini_swe_agent_team_v2",
                        "--num-agents",
                        "1",
                        "--parallelism",
                        "1",
                        "--runtime-type",
                        "process",
                        "--adapter-mode",
                        "mock",
                        "--out-dir",
                        tmp,
                    ]
                )
            result_path = Path([line for line in output.getvalue().splitlines() if line.startswith("Result: ")][0].split(": ", 1)[1])
            data = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(data["config"]["runtime"]["type"], "process")

    def test_cli_accepts_repo_context_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "service.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "run",
                        "--workload",
                        "mini_swe_agent_team_v2",
                        "--num-agents",
                        "1",
                        "--parallelism",
                        "1",
                        "--repo-context-enabled",
                        "--repo-source",
                        str(repo),
                        "--repo-context-max-bytes",
                        "100000",
                        "--repo-context-max-files",
                        "10",
                        "--adapter-mode",
                        "mock",
                        "--out-dir",
                        str(root / "runs"),
                    ]
                )
            result_path = Path([line for line in output.getvalue().splitlines() if line.startswith("Result: ")][0].split(": ", 1)[1])
            data = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(data["config"]["repo_context_enabled"])
        self.assertEqual(data["issues"][0]["repo_context"]["files_scanned"], 1)

    def test_local_task_planner_uses_repo_source_task_and_verify_command(self) -> None:
        config = TeamRunConfig(
            num_agents=2,
            parallelism=2,
            task_source="local",
            repo_source="/tmp/example-repo",
            task="Fix the parser bug",
            verify_command="python -m pytest tests/test_parser.py",
        )
        workers = TaskPlanner().build_workers(config)
        plans = TaskPlanner().build_issue_plans(config, workers)

        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[0].repo, "/tmp/example-repo")
        self.assertEqual(plans[0].workdir, "/tmp/example-repo")
        self.assertEqual(plans[0].prompt, "Fix the parser bug")
        self.assertEqual(plans[0].verify_command, "python -m pytest tests/test_parser.py")

    def test_process_local_task_runs_mini_and_verify_command_in_repo_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "marker.txt").write_text("ok\n", encoding="utf-8")
            mini = root / "fake-mini"
            mini.write_text(
                "#!/usr/bin/env bash\n"
                "pwd\n"
                "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n",
                encoding="utf-8",
            )
            mini.chmod(0o755)

            result = TeamOrchestrator(
                TeamRunConfig(
                    num_agents=1,
                    parallelism=1,
                    task_source="local",
                    repo_source=str(repo),
                    task="Inspect the marker file.",
                    verify_command="test -f marker.txt",
                    adapter_mode="cli",
                    runtime_type="process",
                    mini_command=str(mini),
                    out_dir=str(root / "runs"),
                )
            ).run()
            data = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))

        issue = data["issues"][0]
        round0 = issue["rounds"][0]
        self.assertTrue(issue["verified"])
        self.assertEqual(round0["verifier_result"]["test_status"], "passed")
        self.assertIn(str(repo), round0["logs"]["stdout"])

    def test_local_task_can_run_in_independent_copy_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "marker.txt").write_text("ok\n", encoding="utf-8")
            mini = root / "fake-mini"
            mini.write_text(
                "#!/usr/bin/env bash\n"
                "pwd\n"
                "echo changed > generated.txt\n"
                "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n",
                encoding="utf-8",
            )
            mini.chmod(0o755)

            result = TeamOrchestrator(
                TeamRunConfig(
                    num_agents=1,
                    parallelism=1,
                    task_source="local",
                    repo_source=str(repo),
                    task="Inspect the marker file.",
                    verify_command="test -f marker.txt && test -f generated.txt",
                    adapter_mode="cli",
                    runtime_type="process",
                    repo_workspace_mode="copy",
                    mini_command=str(mini),
                    out_dir=str(root / "runs"),
                )
            ).run()
            data = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
            issue = data["issues"][0]
            workspace = Path(issue["repo_workspace"]["path"])
            workspace_generated = (workspace / "generated.txt").exists()
            source_generated = (repo / "generated.txt").exists()

        self.assertTrue(issue["verified"])
        self.assertTrue(workspace.is_absolute())
        self.assertNotEqual(str(workspace), str(repo))
        self.assertTrue(workspace_generated)
        self.assertFalse(source_generated)
        self.assertIn(str(workspace), issue["rounds"][0]["logs"]["stdout"])

    def test_smoke_script_supports_agent_context_and_experiment_args(self) -> None:
        script = Path("scripts/test_mini_swe_agent_team_v2.sh").read_text(encoding="utf-8")

        self.assertIn("AAB_AGENT_COUNTS", script)
        self.assertIn("AAB_CONTEXT_LENGTHS", script)
        self.assertIn("--agent-counts", script)
        self.assertIn("--context-lengths", script)
        self.assertIn("--experiment-mode", script)
        self.assertIn("--repeats", script)
        self.assertIn("--max-active-llm-requests", script)
        self.assertIn("--max-active-prefill-tokens", script)

    def test_refresh_run_metrics_reads_existing_collector_csvs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "team-v2-run"
            metrics_dir = run_dir / "metrics"
            metrics_dir.mkdir(parents=True)
            (metrics_dir / "cpu.csv").write_text(
                "timestamp,cpu_util_pct,user_pct,system_pct,load1\n"
                "1,10,4,6,1\n"
                "2,30,12,18,2\n",
                encoding="utf-8",
            )
            (metrics_dir / "gpu.csv").write_text(
                "timestamp,index,utilization_gpu_pct,utilization_memory_pct,memory_used_mib,memory_total_mib,power_draw_w,temperature_c\n"
                "1,0,20,40,1000,2000,100,40\n"
                "2,0,80,60,1500,2000,120,42\n",
                encoding="utf-8",
            )
            (metrics_dir / "amd_pcm_memory.csv").write_text(
                "Total Mem Bw (GB/s)|50.0\n"
                "Total Mem Bw (GB/s)|70.0\n",
                encoding="utf-8",
            )
            (run_dir / "result.json").write_text(
                json.dumps(
                    {
                        "run_id": "team-v2-run",
                        "workload_type": "mini_swe_agent_team_v2",
                        "started_at": "2026-06-30T00:00:00Z",
                        "ended_at": "2026-06-30T00:00:10Z",
                        "duration_sec": 10,
                        "config": {"num_agents": 1, "parallelism": 1, "context_length": DEFAULT_CONTEXT_LENGTH},
                        "summary": {"total_issues": 1, "failed_issues": 0, "verified_success_rate": 100, "issue_per_hour": 360},
                        "agents": [{"agent_id": "agent-0"}],
                        "issues": [],
                        "team": {},
                        "errors": [],
                    }
                ),
                encoding="utf-8",
            )

            refreshed = refresh_run_metrics(run_dir)

        self.assertEqual(refreshed["metrics_summary"]["cpu"]["avg"], 20.0)
        self.assertEqual(refreshed["metrics_summary"]["gpu"]["p95"], 77.0)
        self.assertEqual(refreshed["metrics_summary"]["gpu_memory"]["max"], 1500.0)
        self.assertEqual(refreshed["metrics_summary"]["dram_bw"]["avg"], 60.0)
        self.assertEqual(refreshed["metrics_timeline"]["cpu"], "metrics/cpu.csv")
        self.assertEqual(refreshed["metrics_timeline"]["gpu"], "metrics/gpu.csv")

    def test_build_sweep_from_child_runs_uses_child_metrics_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_paths = []
            for agents, cpu_avg in [(1, 12), (2, 34)]:
                run_dir = root / f"team-v2-{agents}"
                run_dir.mkdir()
                result = {
                    "run_id": run_dir.name,
                    "workload_type": "mini_swe_agent_team_v2",
                    "started_at": "2026-06-30T00:00:00Z",
                    "ended_at": "2026-06-30T00:00:10Z",
                    "duration_sec": 10,
                    "config": {"num_agents": agents, "parallelism": agents, "context_length": DEFAULT_CONTEXT_LENGTH},
                    "summary": {"total_issues": agents, "failed_issues": 0, "verified_success_rate": 100, "issue_per_hour": 360},
                    "metrics_summary": {
                        "cpu": {"avg": cpu_avg, "p95": cpu_avg + 1, "max": cpu_avg + 2, "unit": "percent"},
                        "gpu": {"avg": 1, "p95": 2, "max": 3, "unit": "percent"},
                        "gpu_memory": {"avg": 100, "p95": 120, "max": 140, "unit": "MiB"},
                        "memory": {"avg": 0, "p95": 0, "max": 0, "unit": "MiB"},
                        "dram_bw": {"avg": 50, "p95": 60, "max": 70, "unit": "GB/s"},
                    },
                    "metrics_timeline": {"cpu": "metrics/cpu.csv"},
                    "team": {},
                    "agents": [],
                    "issues": [],
                    "errors": [],
                }
                (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
                child_paths.append(run_dir / "result.json")

            sweep = build_sweep_from_child_runs(root / "sweep-group", child_paths)
            sweep_file = json.loads(((root / "sweep-group") / "sweep.json").read_text(encoding="utf-8"))

        self.assertEqual([item["num_agents"] for item in sweep["scaling_metrics"]], [1, 2])
        self.assertEqual([item["cpu_avg"] for item in sweep_file["scaling_metrics"]], [12.0, 34.0])

    def test_firecracker_team_plan_writes_vm_task_specs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kernel = root / "vmlinux"
            rootfs = root / "rootfs.ext4"
            kernel.write_text("kernel", encoding="utf-8")
            rootfs.write_text("rootfs", encoding="utf-8")
            config = TeamRunConfig(
                num_agents=2,
                parallelism=2,
                use_firecracker=True,
                fc_kernel=str(kernel),
                fc_rootfs=str(rootfs),
                guest_vllm_base_url="http://172.16.0.1:8000/v1",
                out_dir=str(root),
            )
            workers = TaskPlanner().build_workers(config)
            plans = TaskPlanner().build_issue_plans(config, workers)

            manifest = prepare_firecracker_team_plan(config, "run-1", root / "run-1", workers, plans)

            task_specs = sorted((root / "run-1" / "tasks").glob("*/task.json"))
            self.assertEqual(len(task_specs), 2)
            first = json.loads(task_specs[0].read_text(encoding="utf-8"))
            self.assertEqual(first["context_length"], DEFAULT_CONTEXT_LENGTH)
            self.assertEqual(first["vllm_base_url"], "http://172.16.0.1:8000/v1")
            self.assertEqual(first["output_dir"], "/output")
            self.assertEqual(manifest["runner"], "bin/run_prepared_firecracker_agents.sh")
            self.assertTrue(all("task_spec_host_path" in agent for agent in manifest["agents"]))
            self.assertTrue(all(agent["socket_path"].startswith("/tmp/aab-") for agent in manifest["agents"]))
            self.assertTrue(all(len(agent["socket_path"]) < 100 for agent in manifest["agents"]))

    def test_ui_port_fallback_path_does_not_crash(self) -> None:
        calls = []

        def fake_serve(project_root, *, host, port):
            calls.append(port)
            if port == 80:
                raise OSError("permission denied")

        with patch("aab_framework.dashboard.serve", side_effect=fake_serve):
            output = StringIO()
            with redirect_stdout(output):
                result = serve_with_fallback(".", host="0.0.0.0", port=80, fallback_port=8080, enable_fallback=True)

        self.assertEqual(calls, [80, 8080])
        self.assertTrue(result["port_fallback_used"])
        self.assertIn("Port 80 requires root. Falling back to 8080.", output.getvalue())

    def test_workload_spec_registers_agent_team_roles(self) -> None:
        workload = build_mini_swe_agent_team_v2_workload_spec()

        self.assertEqual(workload.name, "mini_swe_agent_team_v2")
        self.assertIn("success_rate_pct", workload.base_metrics)
        self.assertIn("reviewer", workload.default_team.role_names)


if __name__ == "__main__":
    unittest.main()
