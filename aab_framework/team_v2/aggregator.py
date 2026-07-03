from __future__ import annotations

import statistics
from typing import Any


class ResultAggregator:
    def aggregate(self, issues: list[dict[str, Any]], agents: list[dict[str, Any]], metrics: dict[str, Any]) -> dict[str, Any]:
        total = len(issues)
        verified = sum(1 for item in issues if item.get("verified"))
        failed = sum(1 for item in issues if item.get("status") not in {"verified_success"})
        latencies = [float(item.get("latency_sec", 0) or 0) for item in issues]
        total_rounds = sum(len(item.get("rounds", [])) for item in issues)
        review_rounds = [round_item for item in issues for round_item in item.get("rounds", [])]
        review_pass = sum(1 for item in review_rounds if item.get("review_status") in {"approved", "warning"})
        duration = sum(latencies)
        return {
            "total_issues": total,
            "completed_issues": total,
            "verified_success_issues": verified,
            "failed_issues": failed,
            "timeout_issues": sum(1 for item in issues if item.get("status") == "timeout"),
            "total_candidates": total_rounds,
            "total_rounds": total_rounds,
            "avg_rounds_per_issue": round(total_rounds / total, 3) if total else 0,
            "issue_per_hour": round(total / (duration / 3600), 3) if duration else 0,
            "candidate_per_min": round(total_rounds / (duration / 60), 3) if duration else 0,
            "success_rate": round(100 * (total - failed) / total, 3) if total else 0,
            "verified_success_rate": round(100 * verified / total, 3) if total else 0,
            "review_pass_rate": round(100 * review_pass / len(review_rounds), 3) if review_rounds else 0,
            "issue_latency_p50_sec": _percentile(latencies, 50),
            "issue_latency_p95_sec": _percentile(latencies, 95),
            "generation_latency_p95_sec": _percentile(
                [float(r.get("generation_latency_sec", 0) or 0) for item in issues for r in item.get("rounds", [])],
                95,
            ),
            "verification_latency_p95_sec": 0,
            "metrics_attribution_method": metrics.get("attribution_method", "time_window"),
        }


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0
    if len(values) == 1:
        return round(values[0], 3)
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((percentile / 100) * (len(ordered) - 1)))
    return round(ordered[index], 3)
