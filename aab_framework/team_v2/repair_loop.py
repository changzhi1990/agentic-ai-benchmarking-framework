from __future__ import annotations

from .schemas import ReviewResult, VerifierResult, TeamRunConfig


class RepairLoopController:
    def should_retry(self, *, round_id: int, review: ReviewResult, verifier: VerifierResult, config: TeamRunConfig) -> tuple[bool, str | None]:
        if round_id + 1 >= config.max_rounds_per_issue:
            return False, None
        if config.retry_on_review_reject and review.review_status == "rejected":
            return True, "review_rejected"
        if config.retry_on_test_failure and not verifier.verified:
            return True, "test_failed"
        return False, None
