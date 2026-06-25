#!/usr/bin/env bash
set -euo pipefail

MODEL="${MODEL:-/home/user/models/Qwen2.5-Coder-32B-Instruct}"
IMAGE="${IMAGE:-vllm/vllm-openai:latest}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-agentic-model}"
API_KEY="${API_KEY:-token-abc123}"
PORT="${PORT:-8000}"
TP="${TP:-8}"
CONTAINER_NAME="${CONTAINER_NAME:-aab-vllm}"
NCCL_P2P_LEVEL="${NCCL_P2P_LEVEL:-SYS}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.9}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-128}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-}"

docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true

extra_vllm_args=()
if [[ -n "${MAX_MODEL_LEN}" ]]; then
  extra_vllm_args+=(--max-model-len "${MAX_MODEL_LEN}")
fi
if [[ -n "${MAX_NUM_BATCHED_TOKENS}" ]]; then
  extra_vllm_args+=(--max-num-batched-tokens "${MAX_NUM_BATCHED_TOKENS}")
fi

COMMAND=$(
  python3 -m aab_framework.cli vllm-docker-command \
    --model "${MODEL}" \
    --image "${IMAGE}" \
    --served-model-name "${SERVED_MODEL_NAME}" \
    --api-key "${API_KEY}" \
    --tp "${TP}" \
    --port "${PORT}" \
    --nccl-p2p-level "${NCCL_P2P_LEVEL}" \
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
    --max-num-seqs "${MAX_NUM_SEQS}" \
    "${extra_vllm_args[@]}" \
  | sed -n '1p'
)
SERVE_COMMAND=$(
  python3 -m aab_framework.cli vllm-docker-command \
    --model "${MODEL}" \
    --image "${IMAGE}" \
    --served-model-name "${SERVED_MODEL_NAME}" \
    --api-key "${API_KEY}" \
    --tp "${TP}" \
    --port "${PORT}" \
    --nccl-p2p-level "${NCCL_P2P_LEVEL}" \
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
    --max-num-seqs "${MAX_NUM_SEQS}" \
    "${extra_vllm_args[@]}" \
  | sed -n '2p'
)

COMMAND="${COMMAND/aab-vllm/${CONTAINER_NAME}}"
SERVE_COMMAND="${SERVE_COMMAND/aab-vllm/${CONTAINER_NAME}}"
echo "${COMMAND}"
eval "${COMMAND}"
echo "${SERVE_COMMAND}"
eval "${SERVE_COMMAND}"

for _ in $(seq 1 300); do
  if curl -fsS "http://127.0.0.1:${PORT}/v1/models" \
    -H "Authorization: Bearer ${API_KEY}" >/dev/null; then
    echo "vLLM is ready at http://127.0.0.1:${PORT}/v1"
    exit 0
  fi
  sleep 2
done

docker logs --tail 200 "${CONTAINER_NAME}" >&2 || true
exit 1
