# Coding Workload Example

This document describes one workload that can run on top of the general framework.

## Task

Each task is a coding bug-fix episode.

Input:

```text
task.json
repo/
failing test
expected behavior
allowed files
```

Agent steps:

```text
1. Read the bug report.
2. Scan the repository.
3. Build a context window.
4. Call the host LLM service for diagnosis.
5. Generate a patch.
6. Apply the patch.
7. Run tests.
8. If tests fail, call the LLM again with failure logs.
9. Write final result and trace.
```

Output:

```text
result.json
patch.diff
test.log
trace.jsonl
metrics.json
```

## Verifier

The verifier should check:

- patch applies cleanly
- target regression test passes
- existing tests still pass
- patch only modifies allowed files
- no hard-coded test answer

## Business Metrics

Recommended coding metrics:

- verified repair success rate
- first patch pass rate
- retry repair success rate
- time to first patch
- time to verified success
- test runtime p95
- tokens per successful repair
- regression failure rate

## System Metrics

The workload should also report:

- LLM request latency
- prompt tokens per second
- completion tokens per second
- CPU utilization
- memory bandwidth
- GPU utilization
- GPU memory usage
- sandbox overhead

