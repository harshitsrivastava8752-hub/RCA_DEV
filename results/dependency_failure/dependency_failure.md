# Root Cause Analysis

## Root Cause
PostgreSQL deployment was scaled to zero replicas, causing checkout‑api to be unable to connect to the database and return 500 errors, which the frontend forwarded as 502 responses.

## Evidence
- checkout‑api pod logs show "OperationalError: connection to server at \"postgres\" failed: Connection refused"
- postgres deployment has 0 replicas and no running pods
- frontend logs report checkout‑api returned status 500 and POST /checkout -> 502
- jaeger traces show frontend POST /checkout returning 502
- prometheus query shows low CPU usage of checkout‑api, indicating it was not resource‑starved

## Timeline
- `2026-09-29T16:08:14Z` — Observed running checkout‑api and frontend pods; checkout‑api restart_count 0, frontend restart_count 3.
- `2026-09-29T16:08:16Z` — Checked checkout‑api deployment config; no CPU limits set.
- `2026-09-29T16:08:54Z` — Confirmed single checkout‑api pod running.
- `2026-09-29T16:09:27Z` — Jaeger trace shows frontend POST /checkout returning 502.
- `2026-09-29T16:10:27Z` — Checkout‑api pod logs show database connection refused.
- `2026-09-29T16:11:30Z` — No postgres pods found in namespace.
- `2026-09-29T16:13:04Z` — Frontend pod logs report checkout‑api returned status 500 and 502.
- `2026-09-29T16:14:13Z` — Postgres deployment retrieved: replicas 0, no ready replicas.
- `2026-09-29T16:16:21Z` — Postgres deployment resource config confirms 0 replicas.
- `2026-09-29T16:17:21Z` — Prometheus query shows low CPU usage of checkout‑api pod.

## Confidence
low

## Alternative Explanations
- Resource starvation of checkout‑api (refuted by low CPU usage and restart_count 0).
- Frontend pod high restart count causing 5xx (refuted by logs showing checkout‑api errors).
- Misconfigured ingress/service routing (refuted by traces showing requests reaching checkout‑api).

## Uncertainty
None; all evidence consistently points to PostgreSQL unavailability as the root cause.


---

## Ground truth (not shown to the agent)

The postgres Deployment was scaled to 0 replicas, so checkout-api cannot connect to its database: /checkout returns 500 and frontend returns 502, while checkout-api pods remain Ready. Evidence: no postgres pod and a scale-down event in demo-app, checkout-api logs with database connection errors, 5xx counters rising on both frontend and checkout-api.
