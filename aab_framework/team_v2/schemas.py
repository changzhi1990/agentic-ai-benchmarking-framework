from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONTEXT_LENGTH = 1024
SUPPORTED_CONTEXT_LENGTHS = (1024, 2048, 4096, 8192)
WORKLOAD_TYPE = "mini_swe_agent_team_v2"


@dataclass(frozen=True)
class ContextMetadata:
    requested_context_length: int = DEFAULT_CONTEXT_LENGTH
    effective_context_length: int = DEFAULT_CONTEXT_LENGTH
    context_source: str = "TeamRunConfig.context_length"
    verified_context_length: bool = False
    verification_method: str = "assumed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TeamRunConfig:
    num_agents: int = 1
    parallelism: int = 1
    max_rounds_per_issue: int = 2
    candidate_per_issue: int = 1
    context_length: int = DEFAULT_CONTEXT_LENGTH
    vllm_base_url: str = "http://127.0.0.1:8000/v1"
    model: str = "agentic-model"
    vllm_max_model_len: int = 4096
    vllm_max_num_seqs: int = 128
    vllm_max_num_batched_tokens: int = 16384
    vllm_gpu_memory_utilization: float = 0.9
    vllm_tensor_parallel_size: int = 8
    vllm_dtype: str = "bfloat16"
    vllm_prefix_caching: bool = True
    task_source: str = "synthetic"
    task: str | None = None
    verify_command: str | None = None
    verify_timeout_sec: int = 120
    out_dir: str = "runs"
    mini_swe_agent_repo: str = "third_party/mini-swe-agent"
    mini_command: str = "mini"
    adapter_mode: str = "mock"
    runtime_type: str = "docker"
    runtime_image: str = "aab-mini-swe-agent:latest"
    runtime_workdir: str = "/workspace"
    runtime_network: str = "host"
    runtime_cleanup: bool = True
    runtime_cpu_limit: str | None = None
    runtime_memory_limit: str | None = None
    runtime_container_name_prefix: str = "aab-team-v2"
    repo_context_enabled: bool = False
    repo_source: str | None = None
    repo_context_max_files: int = 20000
    repo_context_max_bytes: int = 1024 * 1024 * 1024
    repo_context_bundle_max_bytes: int = 4 * 1024 * 1024
    repo_context_prompt_max_chars: int = 8192
    repo_workspace_mode: str = "source"
    repo_workspace_cleanup: bool = False
    repo_context_include_git_history: bool = False
    repo_context_git_history_max_bytes: int = 512 * 1024 * 1024
    repo_context_git_log_limit: int = 1000
    repo_context_pytest_collect: bool = False
    repo_context_pytest_command: str = "python -m pytest --collect-only -q"
    repo_context_pytest_timeout_sec: int = 120
    repo_context_extensions: tuple[str, ...] = (
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".java",
        ".md",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
    )
    experiment_name: str = "agent_scaling_test"
    experiment_mode: str = "fixed_llm"
    repeats: int = 1
    fixed_llm_requests: int = 8
    max_active_agents: int | None = None
    max_active_llm_requests: int | None = None
    max_active_prefill_tokens: int | None = None
    sweep: dict[str, Any] = field(default_factory=dict)
    retry_on_test_failure: bool = True
    retry_on_review_reject: bool = True
    request_timeout_sec: int = 300
    ui_host: str = "0.0.0.0"
    ui_port: int = 80
    ui_fallback_port: int = 8080
    ui_enable_port_fallback: bool = True
    instances_file: str | None = None
    use_firecracker: bool = False
    fc_rootfs: str | None = None
    fc_kernel: str | None = None
    fc_runner: str = "bin/run_prepared_firecracker_agents.sh"
    guest_vllm_base_url: str = "http://172.16.0.1:8000/v1"
    firecracker_run_seconds: int = 120
    firecracker_vcpu_count: int = 2
    firecracker_mem_mib: int = 4096
    firecracker_dry_run: bool = False

    def __post_init__(self) -> None:
        if self.num_agents < 1:
            raise ValueError("num_agents must be positive")
        if self.parallelism < 1:
            raise ValueError("parallelism must be positive")
        if self.max_rounds_per_issue < 1:
            raise ValueError("max_rounds_per_issue must be positive")
        if self.candidate_per_issue < 1:
            raise ValueError("candidate_per_issue must be positive")
        if self.context_length not in SUPPORTED_CONTEXT_LENGTHS:
            raise ValueError(f"context_length must be one of {SUPPORTED_CONTEXT_LENGTHS}")
        if self.runtime_type not in {"docker", "firecracker", "process"}:
            raise ValueError("runtime_type must be docker, firecracker, or process")
        if self.repo_context_enabled and not self.repo_source:
            raise ValueError("repo_source is required when repo_context_enabled is true")
        if self.task_source == "local" and not self.repo_source:
            raise ValueError("repo_source is required when task_source is local")
        if self.task_source == "local" and not self.task:
            raise ValueError("task is required when task_source is local")
        if self.verify_timeout_sec < 1:
            raise ValueError("verify_timeout_sec must be positive")
        if self.repo_context_max_files < 1:
            raise ValueError("repo_context_max_files must be positive")
        if self.repo_context_max_bytes < 1:
            raise ValueError("repo_context_max_bytes must be positive")
        if self.repo_context_bundle_max_bytes < 1:
            raise ValueError("repo_context_bundle_max_bytes must be positive")
        if self.repo_context_prompt_max_chars < 0:
            raise ValueError("repo_context_prompt_max_chars must be non-negative")
        if self.repo_workspace_mode not in {"source", "copy", "worktree"}:
            raise ValueError("repo_workspace_mode must be source, copy, or worktree")
        if self.repo_context_git_history_max_bytes < 1:
            raise ValueError("repo_context_git_history_max_bytes must be positive")
        if self.repo_context_git_log_limit < 1:
            raise ValueError("repo_context_git_log_limit must be positive")
        if self.repo_context_pytest_timeout_sec < 1:
            raise ValueError("repo_context_pytest_timeout_sec must be positive")
        if self.experiment_mode not in {"fixed_llm", "unlimited_llm"}:
            raise ValueError("experiment_mode must be fixed_llm or unlimited_llm")
        if self.repeats < 1:
            raise ValueError("repeats must be positive")
        if self.fixed_llm_requests < 1:
            raise ValueError("fixed_llm_requests must be positive")
        if self.use_firecracker and (not self.fc_rootfs or not self.fc_kernel):
            raise ValueError("fc_rootfs and fc_kernel are required when use_firecracker is true")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentWorkerSpec:
    agent_id: str
    role: str = "SWEWorkerAgent"
    requested_context_length: int = DEFAULT_CONTEXT_LENGTH
    effective_context_length: int = DEFAULT_CONTEXT_LENGTH

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IssueExecutionPlan:
    issue_id: str
    task_id: str
    repo: str
    prompt: str
    agent_id: str
    max_rounds: int
    candidate_per_issue: int
    synthetic_expected_file: str | None = None
    workdir: str | None = None
    verify_command: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MiniSweAgentResult:
    status: str
    stdout: str
    stderr: str
    returncode: int
    output_dir: Path
    diagnosis_path: Path
    patch_path: Path
    diff_path: Path
    test_log_path: Path
    number_of_llm_calls: int = 1
    generation_latency_sec: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class ReviewResult:
    review_status: str
    review_score: float
    issues: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VerifierResult:
    verified: bool
    test_status: str
    passed_tests: int = 0
    failed_tests: int = 0
    error_tests: int = 0
    verifier_score: float = 0.0
    test_log_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
