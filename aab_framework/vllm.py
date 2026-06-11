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
    gpu_memory_utilization: float = 0.92
    max_num_seqs: int = 128
    max_num_batched_tokens: int = 65536


def build_vllm_docker_command(config: VllmDockerConfig) -> str:
    mount = ""
    if config.model.startswith("/"):
        mount = f"-v {quote(config.model)}:{quote(config.model)}:ro "

    return (
        "docker run -d --rm "
        f"--name {quote(config.container_name)} "
        "--gpus all "
        "--ipc=host "
        "--network host "
        "-e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True "
        f"{mount}"
        f"{quote(config.image)} "
        f"--model {quote(config.model)} "
        f"--served-model-name {quote(config.served_model_name)} "
        "--host 0.0.0.0 "
        f"--port {config.port} "
        f"--api-key {quote(config.api_key)} "
        f"--tensor-parallel-size {config.tensor_parallel_size} "
        "--dtype bfloat16 "
        f"--max-model-len {config.max_model_len} "
        f"--gpu-memory-utilization {config.gpu_memory_utilization} "
        f"--max-num-seqs {config.max_num_seqs} "
        f"--max-num-batched-tokens {config.max_num_batched_tokens} "
        "--enable-chunked-prefill"
    )


def build_vllm_healthcheck_command(port: int = 8000, api_key: str = "token-abc123") -> str:
    return (
        f"curl -fsS http://127.0.0.1:{port}/v1/models "
        f"-H {quote(f'Authorization: Bearer {api_key}')}"
    )
