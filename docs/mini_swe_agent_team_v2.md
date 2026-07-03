# mini_swe_agent_team_v2

`mini_swe_agent_team_v2` is the Agent Team v2 workload for integrating
mini-swe-agent into the existing benchmarking framework. It keeps the current
CLI, vLLM, metrics, Firecracker, and dashboard paths intact while adding a
Docker-first runtime contract and configurable context-length sweeps.

## Architecture

Runtime roles:

- `CoordinatorAgent` / `TeamOrchestrator`: creates `run_id`, run directory,
  task queue, workers, metrics observer, aggregation, and `result.json`.
- `TaskPlanner`: loads synthetic tasks or SWE-Bench-like instances and assigns
  issues round-robin to workers.
- `SWEWorkerAgent`: runs one issue lifecycle across diagnosis, patch, review,
  verify, and repair rounds.
- `MiniSweAgentAdapter`: wraps mini-swe-agent. `mock` mode is used for smoke
  tests; `cli` mode calls the configured mini-swe-agent command.
- `DockerRuntime`: default runtime config and Docker command encapsulation.
- `PatchReviewerAgent`: rule-based patch and log review.
- `TestVerifierAgent`: synthetic verifier and command-based verifier hook.
- `RepairLoopController`: decides whether to retry a failed round.
- `MetricsObserver`: wraps existing metrics outputs. It does not collect new
  metrics.
- `ResourceGovernor`: stores active-agent and active-LLM-request limits and
  performs vLLM health checks.
- `ResultAggregator`: computes team, issue, agent, throughput, and latency KPIs.

## Data Flow

```text
CLI/config
  -> TeamRunConfig
  -> TaskPlanner
  -> SWEWorkerAgent
  -> MiniSweAgentAdapter
  -> PatchReviewerAgent
  -> TestVerifierAgent
  -> RepairLoopController
  -> MetricsObserver
  -> ResultAggregator
  -> result.json
  -> dashboard /api/run
```

## Docker Runtime

The default runtime schema is:

```json
{
  "type": "docker",
  "image": "aab-mini-swe-agent:latest",
  "workdir": "/workspace",
  "network": "host",
  "cleanup": true,
  "cpu_limit": null,
  "memory_limit": null,
  "container_name_prefix": "aab-team-v2"
}
```

Docker command construction belongs to `aab_framework/team_v2/docker_runtime.py`.
Workers and CLI code should not build Docker commands directly. Mock runs do not
start Docker, which keeps unit and smoke tests independent from the host daemon.
For real `adapter_mode=cli` runs, Docker availability is recorded as an error if
the daemon is unavailable, and the run still writes `result.json`.

The existing Firecracker path remains optional through `--use-firecracker true`.
It reuses `aab_framework/team_v2/firecracker_runner.py`,
`bin/run_prepared_firecracker_agents.sh`, and the existing rootfs customization
flow.

## Issue Lifecycle

Each issue records:

```text
repo preparation
optional real repository context scan
issue loading
context metadata
diagnosis
patch candidate
patch review
test verification
repair decision
result aggregation
```

Each round includes `review_result`, `verifier_result`, `stage_timings`, context
metadata, patch paths, logs, and retry reason. If verification or review fails
and `max_rounds_per_issue` allows another round, the worker records the retry
reason and runs another candidate.

## Real Repository Context

To model real SWE-agent work instead of synthetic memory pressure, enable the
repo context builder. Each agent scans a real repository before calling
mini-swe-agent:

```text
git/source tree -> file selection -> file reads -> symbol/import extraction -> context_bundle.txt -> mini-swe-agent prompt
```

This is intended to increase host CPU and DRAM bandwidth through realistic code
understanding work. It does not run memory burners, STREAM, stress-ng, or random
byte loops.

CLI example:

```bash
python3 -m aab_framework.cli run \
  --workload mini_swe_agent_team_v2 \
  --num-agents 8 \
  --parallelism 8 \
  --context-length 4096 \
  --adapter-mode cli \
  --runtime-type docker \
  --repo-context-enabled \
  --repo-source /repos/linux \
  --repo-context-max-files 20000 \
  --repo-context-max-bytes 1073741824 \
  --repo-context-bundle-max-bytes 4194304
```

Metrics wrapper example:

```bash
AAB_REPO_CONTEXT_ENABLED=1 \
AAB_REPO_SOURCE=/repos/linux \
AAB_REPO_CONTEXT_MAX_FILES=20000 \
AAB_REPO_CONTEXT_MAX_BYTES=1073741824 \
AAB_REPO_CONTEXT_BUNDLE_MAX_BYTES=4194304 \
AGENT_SWEEP="1 2 4 8 16 32 64 128" \
CONTEXT_LENGTH=4096 \
ADAPTER_MODE=cli \
RUNTIME_TYPE=docker \
bin/run_mini_swe_agent_team_v2_sweep_with_metrics.sh
```

Each issue result includes:

```json
{
  "repo_context": {
    "enabled": true,
    "repo_source": "/repos/linux",
    "files_scanned": 20000,
    "bytes_scanned": 1073741824,
    "symbols_extracted": 100000,
    "imports_extracted": 50000,
    "context_bundle_path": "issues/.../repo_context/context_bundle.txt",
    "index_path": "issues/.../repo_context/repo_index.json",
    "scan_repo_sec": 1.2,
    "build_context_sec": 8.4
  }
}
```

## Verifier Scoring

Synthetic smoke tests use `TestVerifierAgent.verify_synthetic`, which treats a
passing test log or `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` as verified. Real
repository verification can use `verify_command` with pytest or a task-specific
command and writes the combined test log path into the result.

## Metrics

`MetricsObserver` reuses existing framework outputs:

- `metrics/cpu.csv`
- `metrics/gpu.csv`
- `metrics/amd_pcm_memory.csv`
- `aligned_metrics.json`

It derives `metrics_summary`, `metrics_timeline`, `metrics_field_mapping`, and a
time-window attribution marker. If no collector output exists, it writes an
empty structured summary with `attribution_method = "time_window"`. This avoids
duplicating the existing CPU, GPU, DCGM, and AMDuProfPcm collection pipeline.

## Context Length

The default context length is `1024`. Supported sweep values are:

```text
1024, 2048, 4096, 8192
```

Every worker, issue round, and agent row records:

```json
{
  "requested_context_length": 1024,
  "effective_context_length": 1024,
  "context_source": "TeamRunConfig.context_length",
  "verified_context_length": false,
  "verification_method": "assumed"
}
```

If vLLM metadata is not queried, the result must remain `assumed` rather than
claiming that the server setting was verified.

## Agent Count Sweep

Default smoke values:

```text
agent_counts = 1,2,4
context_lengths = 1024
```

Recommended scaling values:

```text
agent_counts = 1,2,4,8,16,32,64,128
context_lengths = 1024,2048,4096,8192
```

Each case writes a child run with:

```json
{
  "sweep": {
    "agent_count": 4,
    "context_length": 2048,
    "case_id": "agents4_ctx2048_r1",
    "experiment_mode": "fixed_llm",
    "repeat": 1,
    "max_active_llm_requests": 4,
    "max_active_prefill_tokens": 8192
  }
}
```

## Experiment Modes

`fixed_llm` limits LLM concurrency:

```text
max_active_llm_requests = min(agent_count, fixed_llm_requests)
max_active_prefill_tokens = max_active_llm_requests * context_length
```

`unlimited_llm` lets LLM concurrency scale with agents:

```text
max_active_llm_requests = agent_count
max_active_prefill_tokens = agent_count * context_length
```

Keep model, vLLM parameters, task set, Docker image, metrics interval,
`max_rounds_per_issue`, and verification timeout fixed inside one sweep.

## CLI

Synthetic smoke run:

```bash
python3 -m aab_framework.cli run \
  --workload mini_swe_agent_team_v2 \
  --num-agents 2 \
  --parallelism 2 \
  --max-rounds-per-issue 2 \
  --candidate-per-issue 1 \
  --context-length 1024 \
  --task-source synthetic \
  --adapter-mode mock \
  --out-dir runs
```

Context and agent sweep:

```bash
python3 -m aab_framework.cli sweep \
  --workload mini_swe_agent_team_v2 \
  --agent-counts 1,2,4 \
  --context-lengths 1024,2048,4096,8192 \
  --experiment-mode fixed_llm \
  --max-active-llm-requests 2 \
  --adapter-mode mock \
  --out-dir runs
```

SWE-Bench-like input file:

```bash
python3 -m aab_framework.cli run \
  --workload mini_swe_agent_team_v2 \
  --num-agents 8 \
  --parallelism 8 \
  --context-length 1024 \
  --task-source swebench \
  --instances-file configs/swebench_instances.txt \
  --adapter-mode cli \
  --mini-command mini \
  --vllm-base-url http://127.0.0.1:8000/v1 \
  --model hosted_vllm/agentic-model \
  --out-dir runs
```

`instances_file` accepts plain instance IDs or JSON lines with `issue_id`,
`task_id`, `repo`, and `prompt`.

## UI

The UI is the existing Python static dashboard in `aab_framework/dashboard.py`
and `aab_framework/dashboard_static/`. Start it with:

```bash
bin/start_dashboard.sh
```

Default host and port:

```text
host: 0.0.0.0
port: 80
fallback_port: 8080
```

If the current user cannot bind port 80, the dashboard falls back to 8080 and
prints the reason. To force port 80, configure permissions outside the project
with root, systemd, a reverse proxy, or a file capability. The project does not
automatically sudo, install nginx, or modify firewall rules.

The dashboard recognizes `mini_swe_agent_team_v2` run and sweep `result.json`
files and displays team overview, issue table, agent table, and metrics summary.

## Smoke Test

```bash
scripts/test_mini_swe_agent_team_v2.sh
```

Environment overrides:

```bash
AAB_AGENT_COUNTS=1,2,4 \
AAB_CONTEXT_LENGTHS=1024,2048,4096,8192 \
scripts/test_mini_swe_agent_team_v2.sh
```

Equivalent CLI options:

```bash
scripts/test_mini_swe_agent_team_v2.sh \
  --agent-counts 1,2,4 \
  --context-lengths 1024,2048,4096,8192 \
  --experiment-mode fixed_llm \
  --max-active-llm-requests 2
```

## Extending

- Replace `PatchReviewerAgent` with an LLM reviewer while preserving its JSON
  contract.
- Extend `TestVerifierAgent` to run per-repository pytest commands.
- Add SRE, security triage, RAG, or support workflows by reusing
  `TeamOrchestrator`, `ResourceGovernor`, `DockerRuntime`, and
  `ResultAggregator`.

## Secret Handling

Do not write SSH passwords, API keys, tokens, or private keys to configs, docs,
logs, command history, or git diffs. Pass secrets through environment variables
or operator-managed mechanisms.
