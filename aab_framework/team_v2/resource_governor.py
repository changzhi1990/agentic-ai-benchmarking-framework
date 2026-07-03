from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class ResourceGovernor:
    max_active_agents: int
    max_active_llm_requests: int | None = None
    failure_threshold: int = 3
    abort_on_vllm_unhealthy: bool = False
    failed_checks: int = 0

    def vllm_healthy(self, base_url: str) -> bool:
        url = base_url.rstrip("/") + "/models"
        result = subprocess.run(
            ["curl", "-fsS", "--max-time", "2", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        ok = result.returncode == 0
        self.failed_checks = 0 if ok else self.failed_checks + 1
        return ok

    @property
    def degraded(self) -> bool:
        return self.failed_checks >= self.failure_threshold
