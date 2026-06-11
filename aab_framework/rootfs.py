from __future__ import annotations


def build_guest_agent_script() -> str:
    return """#!/bin/sh
set -eu

# Expected kernel arguments include agent.vm_id and agent.host_vllm_url.
# The readiness result is written to /var/lib/aab/result.json.

value_from_cmdline() {
  key="$1"
  for item in $(cat /proc/cmdline); do
    case "$item" in
      "$key="*) printf '%s' "${item#*=}"; return 0 ;;
    esac
  done
  return 0
}

vm_id="$(value_from_cmdline agent.vm_id)"
guest_ip="$(value_from_cmdline agent.guest_ip)"
host_ip="$(value_from_cmdline agent.host_ip)"
host_vllm_url="$(value_from_cmdline agent.host_vllm_url)"
timestamp_unix="$(date +%s)"
models_url="${host_vllm_url%/}/models"
vllm_health="unavailable"
vllm_models_payload=""
if command -v curl >/dev/null 2>&1; then
  if vllm_models_payload="$(curl -fsS --max-time 5 "${models_url}" 2>/tmp/aab-vllm-probe.err)"; then
    vllm_health="ok"
  else
    vllm_health="error"
    vllm_models_payload="$(cat /tmp/aab-vllm-probe.err 2>/dev/null || true)"
  fi
elif command -v wget >/dev/null 2>&1; then
  if vllm_models_payload="$(wget -q -T 5 -O - "${models_url}" 2>/tmp/aab-vllm-probe.err)"; then
    vllm_health="ok"
  else
    vllm_health="error"
    vllm_models_payload="$(cat /tmp/aab-vllm-probe.err 2>/dev/null || true)"
  fi
else
  vllm_models_payload="curl_or_wget_not_found"
fi
vllm_models_payload_escaped="$(printf '%s' "${vllm_models_payload}" | tr '\\n' ' ' | sed 's/"/\\\\"/g' | cut -c 1-2048)"

mkdir -p /var/lib/aab
cat > /var/lib/aab/result.json <<EOF
{
  "agent_logic": "noop",
  "guest_ip": "${guest_ip}",
  "host_ip": "${host_ip}",
  "host_vllm_url": "${host_vllm_url}",
  "status": "ready",
  "timestamp_unix": ${timestamp_unix},
  "vllm_health": "${vllm_health}",
  "vllm_models_url": "${models_url}",
  "vllm_models_payload": "${vllm_models_payload_escaped}",
  "vm_id": "${vm_id}"
}
EOF

cat /var/lib/aab/result.json
"""


def build_guest_systemd_unit() -> str:
    return """[Unit]
Description=Agentic AI Benchmark Guest Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/aab-guest-agent
StandardOutput=journal+console
StandardError=journal+console
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
