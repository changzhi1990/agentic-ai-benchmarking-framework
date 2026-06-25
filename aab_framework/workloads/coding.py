from __future__ import annotations

from dataclasses import dataclass

from aab_framework.agent_team import AgentRoleSpec, AgentTeamSpec, WorkloadSpec


@dataclass(frozen=True)
class WorkloadSchema:
    name: str
    required_fields: tuple[str, ...]


def build_coding_workload_spec() -> WorkloadSpec:
    return WorkloadSpec(
        name="coding",
        description="Synthetic coding bugfix workload that models context building, LLM diagnosis, and verification planning.",
        default_team=AgentTeamSpec(
            name="coding-bugfix-team",
            roles=(
                AgentRoleSpec(
                    name="planner",
                    responsibility="Plan the coding investigation and identify needed context.",
                    consumes=("task",),
                    produces=("investigation_plan",),
                ),
                AgentRoleSpec(
                    name="context_builder",
                    responsibility="Collect code, logs, and failure context for the prompt.",
                    consumes=("investigation_plan",),
                    produces=("context_bundle",),
                ),
                AgentRoleSpec(
                    name="solver",
                    responsibility="Generate diagnosis and patch plan using the shared LLM service.",
                    consumes=("context_bundle",),
                    produces=("diagnosis", "patch_plan"),
                ),
                AgentRoleSpec(
                    name="verifier",
                    responsibility="Describe verification steps for the proposed patch.",
                    consumes=("patch_plan",),
                    produces=("verification_plan",),
                ),
                AgentRoleSpec(
                    name="challenge",
                    responsibility="Challenge weak assumptions, missing evidence, and insufficient verification.",
                    consumes=("diagnosis", "patch_plan", "verification_plan"),
                    produces=("challenge_review",),
                ),
            ),
            challenge_role="challenge",
        ),
        base_metrics=(
            "completed_tasks",
            "failed_tasks",
            "success_rate_pct",
            "lat_ok_p95_ms",
            "throughput_task_per_min_workload",
        ),
        business_metrics=(
            "diagnosis",
            "patch_plan",
            "verification",
            "response_chars",
        ),
        executor_specific_fields=(),
    )


def build_coding_prompt_template() -> str:
    return (
        "You are a coding bugfix agent. Diagnose this synthetic bug and propose a minimal patch plan. "
        "Task ${task_id}: retry_state is not persisted after timeout. "
        "Return concise JSON fields diagnosis, patch_plan, verification."
    )


def build_coding_chat_payload_template() -> str:
    return (
        '{"model":"/workspace/models/Qwen2.5-Coder-32B-Instruct/",'
        '"messages":[{"role":"system","content":"You are a coding bugfix agent. Return JSON fields diagnosis, patch_plan, verification."},'
        '{"role":"user","content":"${prompt}"}],'
        '"temperature":0.1,"max_tokens":${llm_max_tokens}}'
    )


def build_coding_trace_schema() -> WorkloadSchema:
    return WorkloadSchema(
        name="coding-trace",
        required_fields=(
            "task_id",
            "event_type",
            "stage",
            "role",
            "workload",
            "status",
            "latency_ms",
            "response_chars",
            "memory_rounds",
            "memory_mb",
            "memory_mode",
            "memory_touched_bytes",
        ),
    )


def build_coding_result_schema() -> WorkloadSchema:
    return WorkloadSchema(
        name="coding-result",
        required_fields=(
            "agent_logic",
            "completed_tasks",
            "failed_tasks",
            "host_vllm_url",
            "vllm_health",
            "trace_path",
            "vm_id",
        ),
    )


def build_coding_guest_contract_script() -> str:
    prompt_template = build_coding_prompt_template().replace('"', '\\"')
    return f"""# BEGIN coding workload contract
AAB_WORKLOAD_NAME="coding"

build_synthetic_prefill_context() {{
  context_kb="$1"
  context_index=0
  while [ "${{context_index}}" -lt "${{context_kb}}" ]; do
    printf ' synthetic_file_%04d.py retry_state timeout persistence branch analysis candidate_patch verifier_notes context_padding_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ' "${{context_index}}"
    context_index=$((context_index + 1))
  done
}}

build_coding_prompt() {{
  task_id="$1"
  base_prompt="{prompt_template}"
  prompt=""
  prompt_repeat_index=0
  while [ "${{prompt_repeat_index}}" -lt "${{llm_prompt_repeat}}" ]; do
    prompt="${{prompt}} ${{base_prompt}}"
    prompt_repeat_index=$((prompt_repeat_index + 1))
  done
  if [ "${{llm_context_kb}}" -gt 0 ]; then
    prompt="${{prompt}} Synthetic repository prefill context: $(build_synthetic_prefill_context "${{llm_context_kb}}")"
  fi
  printf '%s' "${{prompt}}"
}}

build_coding_payload() {{
  prompt="$1"
cat <<EOF_PAYLOAD
{{"model":"/workspace/models/Qwen2.5-Coder-32B-Instruct/","messages":[{{"role":"system","content":"You are a coding bugfix agent. Return JSON fields diagnosis, patch_plan, verification."}},{{"role":"user","content":"${{prompt}}"}}],"temperature":0.1,"max_tokens":${{llm_max_tokens}}}}
EOF_PAYLOAD
}}

write_stage_event() {{
  task_id="$1"
  stage="$2"
  role="$3"
  status="$4"
  latency_ms="$5"
  printf '{{"task_id":"%s","workload":"coding_bugfix","event_type":"stage","stage":"%s","role":"%s","status":"%s","latency_ms":%s}}\\n' \
    "${{task_id}}" "${{stage}}" "${{role}}" "${{status}}" "${{latency_ms}}" >> "${{trace_path}}"
}}
# END coding workload contract
"""
