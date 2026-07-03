from __future__ import annotations

from pathlib import Path

from .schemas import ReviewResult


class PatchReviewerAgent:
    def review(self, patch_path: Path, test_log_path: Path) -> ReviewResult:
        issues: list[str] = []
        recommendations: list[str] = []
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace") if patch_path.exists() else ""
        test_text = test_log_path.read_text(encoding="utf-8", errors="replace") if test_log_path.exists() else ""
        test_passed = (
            "1 passed" in test_text
            or "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in test_text
            or "verify command passed" in test_text
        )
        if not patch_path.exists():
            issues.append("patch file is missing")
        elif not patch_text.strip():
            if test_passed:
                return ReviewResult(
                    "warning",
                    0.5,
                    ["patch file is empty for a verifier-passed synthetic task"],
                    ["use a real repository task to exercise non-empty patch review"],
                )
            issues.append("patch file is empty")
        if "Traceback" in test_text or "SyntaxError" in test_text or "ImportError" in test_text:
            issues.append("test log contains Python error")
        if any(path in patch_text for path in ["/etc/", "/root/", ".ssh/", "authorized_keys"]):
            issues.append("patch touches a dangerous path")
        if issues:
            recommendations.append("repair patch or inspect mini-swe-agent trajectory")
            return ReviewResult("rejected", 0.0, issues, recommendations)
        return ReviewResult("approved", 1.0, [], [])
