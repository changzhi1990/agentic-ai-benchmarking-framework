# Firecracker Coding Agent Sweep

This benchmark simulates many isolated AI coding agents running on one GPU
server.

## What It Uses

- `vLLM` serves the model through an OpenAI-compatible API on the host.
- `Firecracker` runs isolated microVMs for agent execution.
- The guest agent runs a synthetic coding bugfix workflow.
- `aab-memory-burner` simulates code/context scanning pressure inside each VM.
- `AMDuProfPcm` records host-level DRAM bandwidth.
- `nvidia-smi` records GPU utilization, GPU memory-controller utilization,
  GPU memory use, power, and temperature.
- A host sampler records CPU utilization from `/proc/stat`.

## Scenario

The workload models a PCIe GPU server running many concurrent coding agents.
Each agent:

1. Boots in an isolated Firecracker VM.
2. Probes the host vLLM endpoint.
3. Builds memory pressure that represents code/context scanning.
4. Sends a synthetic coding bugfix prompt to vLLM.
5. Writes `trace.jsonl` and `result.json`.

The default execution mode is one business agent per Firecracker VM:

```text
agent_count = firecracker_vm_count
tasks_per_vm = 1
```

The scripts still support packing multiple logical tasks into one VM via
`AAB_AGENTS_PER_VM`, but the default remains `1` because it is the simplest
isolation model.

## Guest Agent Workflow

Each Firecracker VM runs `/usr/local/bin/aab-guest-agent` through systemd. The
guest agent is a synthetic coding-agent workflow, not a full repository repair
benchmark.

The VM-local workflow is:

```text
Firecracker VM boots
  -> aab-guest-agent starts
  -> reads kernel boot args
  -> probes host vLLM /v1/models
  -> starts the memory workflow
  -> builds a synthetic coding bugfix prompt
  -> calls host vLLM /v1/chat/completions
  -> writes trace.jsonl
  -> writes result.json
  -> waits for the memory workflow
  -> writes final result.json
```

The coding task is a synthetic bugfix diagnosis. It asks the model to diagnose
a fixed issue, such as retry state not being persisted after a timeout, and to
return concise `diagnosis`, `patch_plan`, and `verification` fields.

The memory workflow represents the expensive context-building work that real
coding agents do before calling an LLM, for example:

- scanning source files
- scanning logs and failing test output
- ranking candidate files
- reading context chunks into memory
- compacting a context bundle for the prompt

Today this pressure is implemented by the static `aab-memory-burner` helper.
The default mode is `read`, which creates sustained host DRAM read pressure from
inside each VM. Other modes, such as `triad`, `copy`, `scale`, `write`,
`nt-write`, and `read8`, are available for experiments.

The workflow therefore models a multi-tenant coding-agent server where many
isolated agents simultaneously perform context construction and make short LLM
calls to a shared host vLLM service.

## Key Metrics

System metrics:

- CPU utilization p50/p95.
- Host DRAM bandwidth p50/p95/max from AMDuProfPcm.
- GPU utilization p50/p95/max from nvidia-smi.
- GPU memory-controller utilization p50/p95/max from nvidia-smi.
- GPU power and memory usage.

Business metrics:

- VM result count.
- Completed tasks.
- Failed tasks.
- Success rate.
- Throughput per minute.
- Latency p50/p95.

DRAM bandwidth is a host-level system aggregate. It includes all processes on
the server during the sample window. In these sweeps, the dominant DRAM pressure
is expected to come from the Firecracker guest memory workflow.

## Preparing the Server

Start vLLM:

```bash
MODEL=/home/user/models/Qwen2.5-Coder-32B-Instruct \
TP=8 \
PORT=8000 \
GPU_MEMORY_UTILIZATION=0.93 \
MAX_MODEL_LEN=32768 \
MAX_NUM_BATCHED_TOKENS=131072 \
CONTAINER_NAME=aab-vllm \
./bin/start_vllm_8x5090.sh
```

Customize the Firecracker rootfs after changing guest code:

```bash
ROOTFS_IMAGE=/opt/firecracker/rootfs.ext4 \
./bin/customize_firecracker_rootfs.sh
```

The customization installs:

- `/usr/local/bin/aab-guest-agent`
- `/usr/local/bin/aab-memory-burner`
- `aab-guest-agent.service`

## Running a Sweep

Fast sweep:

```bash
SUDO_PASSWORD=000000 \
RUN_SECONDS=90 \
WORKLOAD_GRACE_SECONDS=30 \
AGENTS_LIST='2 4 8 16 32 64 128 164' \
SWEEP_ROOT="runs/coding-firecracker-fast-aligned-sweep-$(date +%Y%m%d-%H%M%S)" \
./bin/run_coding_firecracker_sweep.sh
```

Longer sweep:

```bash
SUDO_PASSWORD=000000 \
RUN_SECONDS=240 \
WORKLOAD_GRACE_SECONDS=60 \
AGENTS_LIST='2 4 8 16 32 64 128 164' \
SWEEP_ROOT="runs/coding-firecracker-aligned-sweep-$(date +%Y%m%d-%H%M%S)" \
./bin/run_coding_firecracker_sweep.sh
```

Useful tuning knobs:

```bash
AAB_MEMORY_MODE=read
AAB_MEMORY_WORKERS=8
AAB_MEMORY_MB=256
AAB_LLM_LOAD_MODE=single_task
AAB_LLM_CONTEXT_KB=128
AAB_LLM_REQUEST_TIMEOUT_SECONDS=300
AAB_LLM_INTER_TASK_SLEEP_MS=100
AAB_CPU_PINNING=1
AAB_NUMA_POLICY=bind-by-agent
AAB_AGENTS_PER_VM=1
```

For raising GPU memory use without sustained prefill pressure, keep
`AAB_LLM_LOAD_MODE=single_task`, increase `AAB_LLM_CONTEXT_KB` gradually, and
start vLLM with a slightly higher `GPU_MEMORY_UTILIZATION`. Rebuild the
Firecracker rootfs after changing guest-agent code so the generated
`/usr/local/bin/aab-guest-agent` includes the latest request-generation logic.

## Output Layout

Each point writes:

```text
runs/<run>/agents_<N>/
  metrics/
    cpu.csv
    gpu.csv
    amd_pcm_memory.csv
  results/
    agent-*.result.json
    agent-*.trace.jsonl
    summary.json
  firecracker-run.json
  agent-list.tsv
  run.log
```

## Creating the Aligned Table

Use the built-in summary command:

```bash
python3 -m aab_framework.cli summarize-firecracker-sweep \
  --run-root runs/<run> \
  --run-seconds 90 \
  --workload-seconds 60
```

This writes:

```text
aligned_metrics.csv
aligned_metrics.json
aligned_metrics.md
```

The aligned table joins system metrics and business metrics by `agents`.

## Validation

Before committing changes, run:

```bash
python3 -m unittest discover -s tests -p 'test*.py'
bash -n bin/run_coding_firecracker_sweep.sh \
  bin/run_prepared_firecracker_agents.sh \
  bin/customize_firecracker_rootfs.sh \
  bin/setup_firecracker_network.sh
python3 -m compileall aab_framework tests
```
