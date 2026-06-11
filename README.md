# Agentic AI Benchmarking Framework

This repository defines a general benchmarking framework for agentic AI systems on PCIe GPU servers.

The framework is not a single workload. It is a methodology and architecture for running many agentic workloads, such as coding agents, SRE incident analysis, security triage, customer support, data analysis, compliance workflows, finance auditing, and RAG-based knowledge agents.

## Core Idea

Separate GPU LLM serving from agent execution:

```text
PCIe GPU Server
├── LLM Serving Layer
│   └── vLLM / SGLang / TensorRT-LLM / TGI
│       └── GPU inference
│
└── Agent Execution Layer
    └── Isolated agent workers
        ├── tool execution
        ├── data or code analysis
        ├── LLM API calls
        └── task verification
```

The LLM service runs on the host, usually in Docker, and directly uses the GPUs. Agent workers run in isolated execution environments, such as processes, containers, or microVMs, and call the host LLM service through an OpenAI-compatible API.

## What It Measures

The framework measures both system performance and business effectiveness.

System metrics:

- CPU utilization
- host memory bandwidth
- GPU utilization
- GPU memory usage
- GPU memory bandwidth or memory-controller utilization
- power, temperature, network, and storage I/O

Business metrics:

- task success rate
- verified success rate
- tasks per hour
- end-to-end task latency
- LLM request latency
- tokens per successful task
- failure rate

Isolation metrics:

- sandbox startup time
- agent ready time
- sandbox crash rate
- OOM count
- execution overhead
- noisy-neighbor impact

## Key Principles

- Keep GPU inference in a dedicated host serving layer.
- Run agents in isolated execution environments.
- Treat `agents` as business load, not raw LLM request concurrency.
- Control LLM request workers independently from total agent count.
- Use verifiers to measure real task completion.
- Collect host, guest, LLM, and business metrics.
- Support multiple workload plugins.
- Compare multiple executors, such as process, Docker, and microVM.

## Why This Matters

Traditional LLM benchmarks mainly measure model output quality or token throughput. Agentic AI systems also depend on tool execution, sandboxing, data access, task verification, multi-tenant isolation, and system stability.

This framework is designed to answer:

```text
How many real agentic tasks can a PCIe GPU server complete,
with what success rate, latency, resource cost, and isolation overhead?
```

See [docs/methodology.md](docs/methodology.md) for the full methodology.

## First Scaffold

This repository also includes an initial implementation scaffold:

```text
aab_framework/
  vllm.py          Docker vLLM command builder
  firecracker.py   Firecracker agent VM planning and config generation
  guest_agent.py   no-op guest agent placeholder
  cli.py           command line interface

bin/
  start_vllm_8x5090.sh
  plan_firecracker_agents.sh
```

Example commands:

```bash
python3 -m aab_framework.cli vllm-docker-command \
  --model /home/user/models/Qwen2.5-Coder-32B-Instruct \
  --tp 8

python3 -m aab_framework.cli firecracker-preflight \
  --kernel-image /opt/firecracker/vmlinux \
  --rootfs-image /opt/firecracker/rootfs.ext4

python3 -m aab_framework.cli plan-firecracker-agents \
  --vm-count 4 \
  --host-vllm-url http://172.16.0.1:8000/v1 \
  --kernel-image /opt/firecracker/vmlinux \
  --rootfs-image /opt/firecracker/rootfs.ext4 \
  --out-dir runs/firecracker-plan
```

The first scaffold does not implement real agent task logic yet. The guest agent is a no-op readiness placeholder, intended to validate executor wiring before adding coding, SRE, security, or data-analysis workloads.
