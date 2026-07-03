from __future__ import annotations

import subprocess
from pathlib import Path

from .schemas import VerifierResult


class TestVerifierAgent:
    def verify_synthetic(self, test_log_path: Path) -> VerifierResult:
        text = test_log_path.read_text(encoding="utf-8", errors="replace") if test_log_path.exists() else ""
        ok = "passed" in text or "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in text
        return VerifierResult(
            verified=ok,
            test_status="passed" if ok else "failed",
            passed_tests=1 if ok else 0,
            failed_tests=0 if ok else 1,
            verifier_score=1.0 if ok else 0.0,
            test_log_path=str(test_log_path),
        )

    def verify_command(self, command: list[str], *, cwd: Path, test_log_path: Path, timeout: int = 120) -> VerifierResult:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
            output = result.stdout
            if result.returncode == 0 and not output.strip():
                output = "verify command passed\n"
            test_log_path.write_text(output, encoding="utf-8")
        except subprocess.TimeoutExpired as exc:
            test_log_path.write_text((exc.stdout or "") if isinstance(exc.stdout, str) else "", encoding="utf-8")
            return VerifierResult(False, "timeout", test_log_path=str(test_log_path))
        ok = result.returncode == 0
        return VerifierResult(
            verified=ok,
            test_status="passed" if ok else "failed",
            passed_tests=1 if ok else 0,
            failed_tests=0 if ok else 1,
            verifier_score=1.0 if ok else 0.0,
            test_log_path=str(test_log_path),
        )
