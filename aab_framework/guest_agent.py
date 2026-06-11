from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def run_noop_agent(*, vm_id: str, host_vllm_url: str, output_path: str | Path) -> dict[str, Any]:
    result = {
        "vm_id": vm_id,
        "host_vllm_url": host_vllm_url,
        "status": "ready",
        "agent_logic": "noop",
        "timestamp_unix": time.time(),
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
