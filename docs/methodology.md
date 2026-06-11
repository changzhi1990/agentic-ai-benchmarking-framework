# Agentic AI Benchmarking Framework Methodology

## 1. Purpose

This framework evaluates agentic AI systems on PCIe GPU servers. It is designed to benchmark not only LLM serving performance, but also agent task completion, tool execution, sandbox overhead, multi-tenant isolation, and infrastructure stability.

It is a framework, not a single workload.

```text
workload = one concrete task family
framework = a unified way to run and compare many workloads
```

Examples of workloads include:

- coding bug fixing
- code review
- SRE incident analysis
- security alert triage
- customer support
- data analysis
- compliance and privacy workflows
- finance auditing
- RAG-based enterprise knowledge agents

## 2. Architecture

The core architecture separates LLM serving from agent execution.

```text
PCIe GPU Server
├── LLM Serving Layer
│   └── vLLM / SGLang / TensorRT-LLM / TGI
│       └── GPU inference
│
└── Agent Execution Layer
    └── Agent workers
        ├── tool execution
        ├── data/code analysis
        ├── LLM API calls
        └── task verification
```

The LLM service should run on the host, often in Docker, with direct access to the GPUs. Agent execution should run in an isolated environment, such as a process, container, or microVM.

MicroVMs, such as Firecracker, are an implementation option for stronger isolation. They are not the methodology itself.

## 3. Core Abstractions

The framework consists of six core abstractions:

```text
Executor
Workload
Agent
Tool Sandbox
Verifier
Metrics Collector
```

### Executor

The executor defines where and how agents run.

Supported executor types may include:

- process executor
- Docker executor
- microVM executor

The same workload should be runnable across different executors to compare isolation overhead, resource efficiency, and failure boundaries.

### Workload

A workload is a concrete task family.

Each workload defines:

- task input format
- available tools
- expected output
- verifier
- business metrics
- resource profile

### Agent

An agent is the logic that performs the task.

It may:

- read files or data
- run tools
- build context
- call the LLM service
- take actions
- verify results
- write traces and metrics

### Tool Sandbox

The tool sandbox provides scenario-specific capabilities.

Examples:

```text
Coding: filesystem, git, pytest, patch apply
SRE: logs, metrics, deployment history, runbooks
Security: alerts, event logs, process trees
Data analysis: CSV, SQLite, SQL executor, pandas
Customer support: tickets, CRM, policies, response drafts
```

### Verifier

The verifier determines whether the task was actually completed.

Examples:

```text
Coding: tests pass, patch applies, no regression
SRE: correct root cause and mitigation
Security: correct severity and evidence
Customer support: policy-compliant response
Data analysis: correct numeric answer
Compliance: correct deletion and audit behavior
```

### Metrics Collector

The metrics collector records system, LLM, business, and isolation metrics.

## 4. Workload Model

Every agent task follows the same high-level lifecycle:

```text
input -> tool execution -> LLM reasoning -> action -> verification -> output
```

For a coding workload:

```text
bug report
repo snapshot
failing test
agent scans repo
agent calls LLM for diagnosis
agent generates patch
agent runs tests
agent revises if needed
verifier checks result
```

For non-coding workloads, replace the task input, tools, and verifier while keeping the same execution framework.

## 5. Request Concurrency Is Not Agent Count

A key rule is:

```text
agents != LLM request concurrency
```

Do not map:

```text
agents=256 -> 256 concurrent LLM requests
```

Instead, separate:

```text
agents                 business agent count
request_workers        bounded LLM request concurrency
memory_workers         host-side data/tool pressure
compute_workers        local compute pressure
vm_count               number of sandboxes
agents_per_vm          agent tasks per sandbox
```

This prevents the LLM serving engine from being overloaded by uncontrolled request concurrency while still allowing business load and tool pressure to scale.

## 6. Resource Isolation

The benchmark should reserve resources for the LLM serving control path.

Example CPU layout:

```text
CPU 0-31:   LLM serving, Docker, host control path
CPU 32-127: agent sandboxes and tool workers
```

Or by NUMA:

```text
NUMA node 0: LLM serving
NUMA node 1: agent execution
```

This avoids starving the LLM engine, NCCL coordination, shared-memory queues, and API server threads.

## 7. Metrics

The framework reports four classes of metrics.

### Business Metrics

- task success rate
- verified success rate
- successful tasks per hour
- task latency p50/p95/p99
- timeout rate
- retry success rate
- tokens per successful task
- cost per verified task

For coding:

- patch success rate
- first patch pass rate
- regression pass rate
- time to first patch
- time to verified success

### LLM Serving Metrics

- prompt tokens per second
- completion tokens per second
- total tokens per second
- LLM request latency p50/p95
- TTFT
- inter-token latency
- LLM error rate
- queue depth
- engine restart or crash count

### System Metrics

- CPU utilization
- host memory bandwidth
- GPU utilization
- GPU memory usage
- GPU memory bandwidth or memory-controller utilization
- power
- temperature
- storage I/O
- network I/O

Metric collectors should be platform-specific:

```text
NVIDIA: nvidia-smi, NVML, DCGM
AMD GPU: rocm-smi, amd-smi, rocprof
AMD CPU memory bandwidth: AMDuProfPcm
Intel CPU memory bandwidth: Intel PCM
```

### Isolation Metrics

- sandbox startup time
- agent ready time
- sandbox crash count
- OOM count
- sandbox teardown time
- noisy-neighbor impact
- sandbox overhead ratio

## 8. Experiment Flow

Each benchmark run should follow this flow:

```text
1. Record hardware topology
2. Start host LLM service
3. Healthcheck the LLM endpoint
4. Start executor pool
5. Dispatch tasks
6. Collect host and guest metrics
7. Collect agent traces
8. Run verifiers
9. Stop executor pool
10. Generate final report
```

Each sweep point should include pre-run and post-run health checks:

- LLM service health
- GPU health
- executor health
- sandbox health

If the LLM service becomes unhealthy, the sweep should stop. Later rows would otherwise be invalid.

## 9. Sweep Design

Recommended sweep dimensions:

- vm_count
- agents_per_vm
- request_workers_per_vm
- vcpu_per_vm
- memory_per_vm
- context size
- max output tokens

Recommended staged experiments:

```text
Stage 1: fixed request concurrency, increase business agents
Stage 2: fixed agents, increase request workers
Stage 3: fixed VM count, increase agents per VM
Stage 4: noisy-neighbor isolation test
```

Avoid increasing all pressure dimensions at once.

## 10. Report Format

Each run should produce a structured report:

```json
{
  "config": {
    "executor": "microvm",
    "vm_count": 8,
    "vcpu_per_vm": 8,
    "mem_mib_per_vm": 8192,
    "agents_per_vm": 4,
    "request_workers_per_vm": 1,
    "serving_backend": "vllm",
    "model": "Qwen2.5-Coder-32B-Instruct"
  },
  "business_metrics": {
    "verified_success_rate": 0.82,
    "successful_tasks_per_hour": 184.3,
    "task_latency_p95": 118.4
  },
  "llm_metrics": {
    "prompt_tokens_per_second": 12000,
    "completion_tokens_per_second": 640,
    "llm_error_rate": 0.01
  },
  "system_metrics": {
    "cpu_util_p95": 78.2,
    "memory_bandwidth_p95_gbps": 164.0,
    "gpu_util_p95": 96.5,
    "gpu_memory_used_p95_mib": 31200
  },
  "isolation_metrics": {
    "sandbox_startup_p95": 180,
    "sandbox_oom_count": 0,
    "noisy_neighbor_impact": 1.12
  }
}
```

## 11. Core Evaluation Questions

The framework is designed to answer:

- How many agentic tasks can the server complete?
- What is the verified success rate?
- What is p95 end-to-end task latency?
- What is the cost per verified task?
- Which resource is the bottleneck?
- What is the overhead of isolation?
- How much do noisy neighbors affect other agents?
- How stable is the LLM serving layer under multi-agent load?

## 12. Summary

This framework combines:

```text
host GPU LLM serving
isolated agent execution
pluggable workloads
task verifiers
business metrics
system metrics
isolation metrics
```

It is suitable for coding workloads and generalizes to other agentic AI scenarios. The central idea is to evaluate the full agent platform, not only the model or token throughput.

