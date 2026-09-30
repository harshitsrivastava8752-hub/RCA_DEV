# Root Cause Analysis

## Root Cause
Memory pressure caused the checkout-api pods to be OOMKilled, leading to restarts and a spike in 5xx errors.

## Evidence
- k8s_get_pods shows restart_count 4 and last_termination_reason OOMKilled for checkout-api-6d6fd68c5d-6bgz7
- prometheus_query for container_memory_working_set_bytes shows peak memory usage of 112,116,000 bytes (~112MB) within 30m
- prometheus_query for 5xx error rate shows a sharp increase from 0.084 to ~1.96 per second during the same period

## Timeline
- `2026-09-29T16:40:38Z` — checkout-api pod created
- `2026-09-30T04:06:43Z` — Container image pulled (Normal event)
- `2026-09-30T04:07:12Z` — 5xx error rate starts at 0.084 per second
- `2026-09-30T04:10:27Z` — 5xx error rate rises to 1.384 per second
- `2026-09-30T04:13:27Z` — 5xx error rate rises to 1.958 per second
- `2026-09-30T04:16:42Z` — 5xx error rate remains high at 1.968 per second
- `2026-09-30T04:22:57Z` — k8s_get_pods reports restart_count 4 and OOMKilled termination
- `2026-09-30T04:22:59Z` — Prometheus shows memory usage peaking at 112,116,000 bytes

## Confidence
high

## Alternative Explanations
- Traffic surge overwhelming the service (no supporting evidence)
- Downstream dependency failure (no supporting evidence)
- Bug in newly deployed image (no supporting evidence)

## Uncertainty
None; all evidence consistently points to OOMKilled events as the root cause.


---

## Ground truth (not shown to the agent)

checkout-api containers were repeatedly OOMKilled: memory grew past the 256Mi limit (leak driven through /debug/leak), the container restarted each time, and requests through frontend failed with 5xx/502 while it was down. Evidence: pod lastState OOMKilled and rising restartCount, checkout-api memory climbing to the limit before each drop, 5xx bursts aligned with the restarts.
