#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

AGENT_COUNTS="${AAB_AGENT_COUNTS:-1,2,4}"
CONTEXT_LENGTHS="${AAB_CONTEXT_LENGTHS:-1024}"
EXPERIMENT_MODE="${AAB_EXPERIMENT_MODE:-fixed_llm}"
REPEATS="${AAB_REPEATS:-1}"
MAX_ACTIVE_LLM_REQUESTS="${AAB_MAX_ACTIVE_LLM_REQUESTS:-}"
MAX_ACTIVE_PREFILL_TOKENS="${AAB_MAX_ACTIVE_PREFILL_TOKENS:-}"

usage() {
  cat <<'EOF'
Usage: scripts/test_mini_swe_agent_team_v2.sh [options]

Options:
  --agent-counts N[,N...]              Default: AAB_AGENT_COUNTS or 1,2,4
  --context-lengths N[,N...]           Default: AAB_CONTEXT_LENGTHS or 1024
  --experiment-mode fixed_llm|unlimited_llm
  --repeats N
  --max-active-llm-requests N
  --max-active-prefill-tokens N
EOF
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --agent-counts)
      AGENT_COUNTS="$2"
      shift 2
      ;;
    --context-lengths)
      CONTEXT_LENGTHS="$2"
      shift 2
      ;;
    --experiment-mode)
      EXPERIMENT_MODE="$2"
      shift 2
      ;;
    --repeats)
      REPEATS="$2"
      shift 2
      ;;
    --max-active-llm-requests)
      MAX_ACTIVE_LLM_REQUESTS="$2"
      shift 2
      ;;
    --max-active-prefill-tokens)
      MAX_ACTIVE_PREFILL_TOKENS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

extra_sweep_args=()
if [[ -n "${MAX_ACTIVE_LLM_REQUESTS}" ]]; then
  extra_sweep_args+=(--max-active-llm-requests "${MAX_ACTIVE_LLM_REQUESTS}")
fi
if [[ -n "${MAX_ACTIVE_PREFILL_TOKENS}" ]]; then
  extra_sweep_args+=(--max-active-prefill-tokens "${MAX_ACTIVE_PREFILL_TOKENS}")
fi

python3 -m unittest tests.test_mini_swe_agent_team_v2
python3 -m aab_framework.cli --help >/dev/null

SMOKE_ROOT="/tmp/aab-mini-swe-agent-team-v2-smoke"
rm -rf "${SMOKE_ROOT}"
python3 -m aab_framework.cli run \
  --workload mini_swe_agent_team_v2 \
  --num-agents 1 \
  --parallelism 1 \
  --max-rounds-per-issue 2 \
  --context-length 1024 \
  --adapter-mode mock \
  --out-dir "${SMOKE_ROOT}" >/tmp/aab-mini-swe-agent-team-v2-smoke.log

python3 - "${SMOKE_ROOT}" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
latest = max(root.iterdir(), key=lambda item: item.stat().st_mtime)
data = json.loads((latest / "result.json").read_text())
assert data["workload_type"] == "mini_swe_agent_team_v2"
assert data["config"]["context_length"] == 1024
assert data["config"]["runtime"]["type"] == "docker"
assert data["team"]["runtime"] == "DockerRuntime"
assert data["issues"] and data["agents"]
assert all(agent["effective_context_length"] == 1024 for agent in data["agents"])
assert data["issues"][0]["rounds"][0]["requested_context_length"] == 1024
assert data["issues"][0]["rounds"][0]["verifier_result"]
assert data["issues"][0]["rounds"][0]["review_result"]
assert data["issues"][0]["rounds"][0]["stage_timings"]
assert data["overall_metrics_summary"]
assert data["metrics_summary"]["cpu"]["unit"] == "percent"
assert data["metrics_timeline"]["system"] == "metrics/system_metrics.jsonl"
print(latest / "result.json")
PY

SWEEP_ROOT="/tmp/aab-mini-swe-agent-team-v2-sweep"
rm -rf "${SWEEP_ROOT}"
python3 -m aab_framework.cli sweep \
  --workload mini_swe_agent_team_v2 \
  --agent-counts "${AGENT_COUNTS}" \
  --context-lengths "${CONTEXT_LENGTHS}" \
  --experiment-mode "${EXPERIMENT_MODE}" \
  --repeats "${REPEATS}" \
  --adapter-mode mock \
  --out-dir "${SWEEP_ROOT}" \
  "${extra_sweep_args[@]}" >/tmp/aab-mini-swe-agent-team-v2-sweep.log

python3 - "${SWEEP_ROOT}" "${AGENT_COUNTS}" "${CONTEXT_LENGTHS}" "${EXPERIMENT_MODE}" "${REPEATS}" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
agent_counts = [int(item) for item in sys.argv[2].replace(",", " ").split()]
context_lengths = [int(item) for item in sys.argv[3].replace(",", " ").split()]
experiment_mode = sys.argv[4]
repeats = int(sys.argv[5])
latest = max(root.iterdir(), key=lambda item: item.stat().st_mtime)
result = json.loads((latest / "result.json").read_text())
sweep = json.loads((latest / "sweep.json").read_text())
expected_cases = len(agent_counts) * len(context_lengths) * repeats
assert result["workload_type"] == "mini_swe_agent_team_v2_sweep"
assert len(result["runs"]) == expected_cases
assert len(result["points"]) == expected_cases
assert sweep["parameters"]["agent_counts"] == agent_counts
assert sweep["parameters"]["context_lengths"] == context_lengths
assert sweep["parameters"]["repeats"] == repeats
assert {point["experiment_mode"] for point in result["points"]} == {experiment_mode}
for run in result["runs"]:
    child = json.loads(Path(run["result_path"]).read_text())
    assert child["config"]["sweep"]["agent_count"] == run["num_agents"]
    assert child["config"]["sweep"]["context_length"] == run["context_length"]
    assert child["config"]["sweep"]["repeat"] == run["repeat"]
print(latest / "sweep.json")
PY

FIXED_ROOT="/tmp/aab-mini-swe-agent-team-v2-fixed-llm-smoke"
rm -rf "${FIXED_ROOT}"
python3 -m aab_framework.cli sweep \
  --workload mini_swe_agent_team_v2 \
  --agent-counts 1,2,4 \
  --context-lengths 2048 \
  --experiment-mode fixed_llm \
  --repeats 1 \
  --max-active-llm-requests 2 \
  --adapter-mode mock \
  --out-dir "${FIXED_ROOT}" >/tmp/aab-mini-swe-agent-team-v2-fixed.log

UNLIMITED_ROOT="/tmp/aab-mini-swe-agent-team-v2-unlimited-llm-smoke"
rm -rf "${UNLIMITED_ROOT}"
python3 -m aab_framework.cli sweep \
  --workload mini_swe_agent_team_v2 \
  --agent-counts 1,2,4 \
  --context-lengths 2048 \
  --experiment-mode unlimited_llm \
  --repeats 1 \
  --adapter-mode mock \
  --out-dir "${UNLIMITED_ROOT}" >/tmp/aab-mini-swe-agent-team-v2-unlimited.log

echo "mini_swe_agent_team_v2 smoke tests passed"
