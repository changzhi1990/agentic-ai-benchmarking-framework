#!/usr/bin/env bash
set -euo pipefail

MODEL="${MODEL:-/home/user/models/Qwen2.5-Coder-32B-Instruct}"
IMAGE="${IMAGE:-vllm/vllm-openai:latest}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-agentic-model}"
API_KEY="${API_KEY:-token-abc123}"
PORT="${PORT:-8000}"
TP="${TP:-8}"
CONTAINER_NAME="${CONTAINER_NAME:-aab-vllm}"

docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true

COMMAND=$(
  python3 -m aab_framework.cli vllm-docker-command \
    --model "${MODEL}" \
    --image "${IMAGE}" \
    --served-model-name "${SERVED_MODEL_NAME}" \
    --api-key "${API_KEY}" \
    --tp "${TP}" \
    --port "${PORT}" \
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
