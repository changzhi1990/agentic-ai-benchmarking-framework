from __future__ import annotations

from dataclasses import dataclass
from shlex import quote


@dataclass(frozen=True)
class VllmDockerConfig:
    model: str
    image: str = "vllm/vllm-openai:latest"
    served_model_name: str = "agentic-model"
    api_key: str = "token-abc123"
    tensor_parallel_size: int = 8
    port: int = 8000
    container_name: str = "aab-vllm"
    max_model_len: int = 16384
    gpu_memory_utilization: float = 0.9
    max_num_seqs: int = 128
    max_num_batched_tokens: int = 65536
    host_models_dir: str = "/home/user/models"
    container_models_dir: str = "/workspace/models"
    workdir: str = "/workspace/"
    shm_size: str = "128G"
    dtype: str = "half"
    kv_cache_dtype: str = "auto"
    pipeline_parallel_size: int = 1


def build_vllm_container_command(config: VllmDockerConfig) -> str:
    return (
        "docker run -itd "
        "-u 10861:10861 "
        "--network host "
        "-u root "
        "--cap-add=SYS_ADMIN "
        "--ipc=host "
        f"--shm-size {quote(config.shm_size)} "
        "--runtime=nvidia "
        "--gpus all "
        "--entrypoint /usr/bin/bash "
        "--security-opt seccomp=unconfined "
        "--security-opt apparmor=unconfined "
        f"-v {quote(config.host_models_dir)}:{quote(config.container_models_dir)} "
        f"-w {quote(config.workdir)} "
        f"--name {quote(config.container_name)} "
        f"{quote(config.image)}"
    )


def build_vllm_serve_command(config: VllmDockerConfig) -> str:
    model_path = _container_model_path(config)
    return (
        f"docker exec -d {quote(config.container_name)} "
        f"vllm serve {quote(model_path)} "
        f"--dtype {quote(config.dtype)} "
        f"--kv-cache-dtype {quote(config.kv_cache_dtype)} "
        f"-tp {config.tensor_parallel_size} "
        f"-pp {config.pipeline_parallel_size} "
        f"--max-num-seqs {config.max_num_seqs} "
        f"--gpu-memory-utilization {config.gpu_memory_utilization} "
        "--disable-log-requests"
    )


def build_vllm_docker_command(config: VllmDockerConfig) -> str:
    """Backward-compatible alias for the container creation command."""

    return build_vllm_container_command(config)


def build_vllm_healthcheck_command(port: int = 8000, api_key: str = "token-abc123") -> str:
    return (
        f"curl -fsS http://127.0.0.1:{port}/v1/models "
        f"-H {quote(f'Authorization: Bearer {api_key}')}"
    )


def _container_model_path(config: VllmDockerConfig) -> str:
    model = config.model.rstrip("/")
    host_root = config.host_models_dir.rstrip("/")
    if model.startswith(host_root + "/"):
        suffix = model[len(host_root) + 1 :]
        return f"{config.container_models_dir.rstrip('/')}/{suffix}/"
    return model + "/"
