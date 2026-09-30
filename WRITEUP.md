# K8s RCA Agent — Write-up

## 1. Architecture
A kind-based Kubernetes cluster runs a demo-app (frontend, checkout-api, postgres) alongside an observability stack (Prometheus, Loki, Jaeger). The RCA agent runs outside the cluster, on the operator's laptop, authenticating with a restricted `rca-agent` ServiceAccount token (read-only across k8s, Prometheus, Loki, and Jaeger). Given a scenario name, it queries these sources, reasons over the evidence with an LLM, and produces a markdown RCA report plus a structured JSON evidence log under `results/<scenario>/`.

## 2. Agent design
The investigation runs as a LangGraph `StateGraph` (`agent/graph/graph.py`) over `GraphState` (`agent/graph/state.py`), which extends a fixed `InvestigationState` contract (incident_description, observed_facts, hypotheses, evidence_log, iteration, max_iterations, conclusion) plus two internal fields (`next_action`, `last_observation`) used only for plumbing between nodes.

Nodes execute in this order:
1. **initial_observation** — one fixed, non-LLM baseline gather: `k8s_get_pods`, `k8s_get_events` (last 30m), and a `prometheus_query` for a fixed 5xx-rate PromQL against frontend/checkout-api. Produces `observed_facts` and the first `evidence_log` entries.
2. **generate_hypotheses** — one LLM call proposes 2–4 distinct root-cause hypotheses (id, description, status="active", empty supporting/contradicting).
3. **select_investigation** — one LLM call picks exactly one next tool call from the catalog, explicitly instructed to prefer evidence that could *refute* the leading hypothesis over evidence that only confirms it. If the model names a tool outside the catalog, it gets one retry with an explicit correction notice; if that also fails, a hardcoded fallback (`k8s_get_events`) is used instead of aborting.
4. **execute_tools** — dispatches the chosen tool call, appends a new `EvidenceEntry`, and stores the full result as `last_observation` (only the most recent call is shown in full; older calls are represented by their truncated `result_summary`, to keep prompt size bounded).
5. **update_hypotheses** — one LLM call revises each hypothesis's status/supporting/contradicting given the new evidence, explicitly weighing refuting evidence for the leading hypothesis, not just confirming evidence. Returns the full hypothesis list every time (same ids).
6. **conclude** — one LLM call produces the final `RCAOutput` (root_cause, confidence, evidence, timeline, alternative_explanations, uncertainty).

Routing (`_route_after_update` in `graph.py`) sends control back to `select_investigation` unless: the leading hypothesis is `confirmed` with no contradicting evidence, or `iteration >= max_iterations` (default 12) — either case routes to `conclude`.

The LLM is called through `agent/llm.py`'s `complete_json`, against an OpenAI-compatible endpoint (`LLM_BASE_URL`/`LLM_MODEL`), rate-limited via `LLM_MAX_RPM`. Any node's LLM call can fail (`LLMError`, e.g. HTTP 429 or a timeout); every node catches this and short-circuits straight to `conclude` by setting `state["conclusion"]` to a deterministic `llm_unavailable_rca(...)` result — root_cause explicitly states the LLM became unavailable, confidence is forced to "low", and evidence/timeline are built directly from whatever was in `evidence_log`/`hypotheses` at the point of failure, with no further LLM calls made. This is what produced the `bad_deployment` and `config_problem` results in §7/§9.

## 3. Tools
| Tool | What it does | Read-only? | Input validation |
|---|---|---|---|
| k8s_tool | Wraps k8s_get_pods, k8s_get_events, k8s_get_pod_logs, k8s_get_deployment, k8s_get_resource_config | Yes | Pydantic schemas restrict namespace to {agent-system, demo-app, observability}, kind to {deployment, configmap}; rejects extra/missing/mistyped fields |
| prometheus_tool | Runs a PromQL range query (`/api/v1/query_range`) over a lookback window, compacts to the top 15 series by peak value, downsampled to ≤6 representative points per series | Yes | Non-finite values (NaN/inf) are dropped; malformed Prometheus error responses raise a clear RuntimeError instead of propagating raw JSON |
| loki_tool | Runs a LogQL range query (`/loki/api/v1/query_range`), returns up to 100 lines, newest first, with only `app`/`pod` labels kept | Yes | Rejects non-log-stream (e.g. metric-shaped) LogQL results, redirecting the caller to `prometheus_query` instead |
| jaeger_tool | Fetches traces for a service via Jaeger's `/api/v3/*` query API (OTLP-style response), compacts each trace to its spans (service, operation, duration, error flag, status code, exception detail if present), capped at 10 traces / 15 spans each | Yes | Raises on non-200 responses and on any `errors` field in the response body rather than silently returning partial/empty data |

All tool dispatch is funneled through a single allow-listed executor (`agent/tools/executor.py`); unknown tool names, disallowed write-style tools, missing/extra/mistyped arguments, and out-of-schema namespace/kind values are all rejected before any real API call is made — verified in `results/security_demo.txt` §(b).

**Note on jaeger_tool**: initially written against the legacy `/api/traces` endpoint; the deployed Jaeger (v2.21) only serves `/api/v3/*` with an OTLP-style response, so this was caught and fixed before any scenario runs — the version in this submission calls `/api/v3/traces` and `/api/v3/services`.

## 4. Sources used
- **oom**: k8s pod state (restart_count, OOMKilled reason), Prometheus (container memory, 5xx rate)
- **dependency_failure**: k8s pod/deployment state (postgres replica count), checkout-api logs (connection refused), Jaeger traces (502 at frontend), Prometheus (CPU, to rule out resource starvation)
- **bad_deployment**, **config_problem**: partial — k8s pod/event state, Prometheus, and (for bad_deployment) pod logs were gathered before each run was cut short by an LLM-side failure (see §9)

## 5. Hypothesis handling
Hypotheses are `{id, description, status: active|confirmed|refuted, supporting: [...], contradicting: [...]}`. Generation, selection, and updating are each a single structured-JSON LLM call with a dedicated system prompt (`agent/graph/prompts.py`):

- **Generation** produces 2–4 hypotheses, deliberately distinct, all starting `status="active"` with empty evidence lists. If the model returns zero usable hypotheses, a single fallback ("Unknown cause — no hypotheses could be generated") is substituted so the graph never runs with an empty hypothesis set. IDs are also sanitized independently of the model's output (`_sanitize_hypotheses` in `nodes.py`) to guarantee uniqueness even if the model repeats or omits an id.
- **Selection** is told which hypothesis is currently "leading" (see below) and is explicitly instructed to favor a tool call that could *refute* it over one that only adds more confirming evidence — this is meant to counteract confirmation bias in a small free-tier model.
- **Update** is given the newest tool result and must return the *full* hypothesis list every time (not a diff), with every status change required to cite a concrete piece of supporting/contradicting evidence.
- **Conclusion** receives the final hypothesis list and full evidence log, and produces the structured `RCAOutput`. The prompt explicitly instructs: if a hypothesis is confirmed with no unresolved contradictions, state it plainly with confidence reflecting evidence strength; otherwise confidence must be "low" and uncertainty must say what's unknown.

"Leading hypothesis" (`leading_hypothesis()` in `nodes.py`) is computed in code, not by the LLM: any `confirmed` hypothesis wins outright; otherwise, whichever `active` hypothesis has the most supporting-evidence entries (falling back to all hypotheses if none are active). Conflicting evidence is handled entirely inside the `update_hypotheses` LLM call — the graph itself has no separate conflict-resolution step, so a hypothesis with both supporting and contradicting entries simply remains `active` until the model resolves it.

`conclude` also has one code-level enforcement independent of what the model outputs: if the leading hypothesis was not confirmed-without-contradiction, confidence is forcibly overwritten to "low" regardless of what the model set it to. **This only covers the not-confirmed case** — it does not currently guard the opposite direction seen in `dependency_failure` (§9): a hypothesis effectively confirmed by the model's own reasoning, with an "Uncertainty" field stating no doubt remains, can still surface as "low" if the model's `RCAOutput.confidence` field itself said "low" and the hypothesis object's `status`/`contradicting` state didn't strictly satisfy `confirmed and not contradicting` at that point. This is the likely mechanism behind the confidence-miscalibration finding in §9.

## 6. Sandbox / security model
- **RBAC**: `rca-agent` ServiceAccount in `agent-system`, scoped to read verbs (get/list) on pods, deployments, events, configmaps in `{agent-system, demo-app, observability}`. Verified directly: `list pods: yes`, `delete pods: no`, `get secrets: no` (`kubectl auth can-i --as=system:serviceaccount:agent-system:rca-agent`).
- **NetworkPolicy** (`infra/manifests/networkpolicy.yaml`): applies to pods labeled `app=rca-agent` in `agent-system`. Ingress is denied entirely. Egress is default-deny except: DNS (53/UDP+TCP) to kube-dns, the kind control-plane API server (6443, via a /12 CIDR covering Docker's default bridge range), the `observability` namespace on 9090/3100/16686 (Prometheus/Loki/Jaeger), and outbound 443 (for the hosted LLM API). Two caveats acknowledged directly in the manifest: rule 4 (port 443) cannot be scoped to a specific destination with vanilla NetworkPolicy, so it permits 443 to any destination, not just the LLM provider; and kindnet only enforces NetworkPolicy on kind v0.30.0+, so enforcement should be verified rather than assumed. **Note**: per the assembly guide, the agent actually runs on the operator's laptop, outside the cluster, so in this project's actual deployment mode this policy constrains a future in-cluster agent pod, not the process that produced the results in §7.
- **Tool-input validation**: Pydantic schemas per tool, enforced in the executor allow-list (§3); rejects unknown tools, extra fields, wrong types, and out-of-scope namespaces/kinds.
- **Prompt injection defense**: every prompt embeds a fixed `SAFETY_LINE` instructing the model to treat anything inside `<observation source="...">...</observation>` tags as untrusted data, never as an instruction — applied identically across all four LLM call sites (generate/select/update/conclude).
- **Evidence** (full output in `results/security_demo.txt`):
  - (a) RBAC: as `rca-agent`, deleting a pod and reading a Secret both return `403 Forbidden` with the correct verb named (`cannot delete resource "pods"`, `cannot get resource "secrets"`). PASS.
  - (b) Executor: seven adversarial dispatch calls (unknown tool, disallowed write-tool, missing/mistyped/extra arguments, out-of-schema namespace/kind, null input) all rejected with specific validation errors. PASS.
  - (c) Prompt injection: a log line containing a fake "SYSTEM OVERRIDE" instruction was wrapped intact in one `<observation>` block; even simulating a fully-fooled model attempting the injected command, the executor rejected it as an unknown tool. PASS. Note: used a simulated fixture, since the live Loki query returned no matching line at test time.

## 7. Results
## 7. Results
| Scenario | Ground truth (manifest.yaml) | Agent root cause | Correct? | Confidence |
|---|---|---|---|---|
| oom | checkout-api leaks memory, gets OOMKilled, restarts, 5xx spike | OOMKilled from memory pressure; cited restart_count 4, peak memory ~112MB, 5xx rate rising 0.08→1.97/s | Yes | High |
| dependency_failure | postgres scaled to 0; checkout-api can't reach DB | Postgres unavailable — connection-refused in checkout-api logs, 0 replicas confirmed, 502s traced end-to-end from frontend | Yes | Low (label inconsistent with the model's own stated certainty — see §9) |
| bad_deployment | New rollout overrode DATABASE_URL with a wrong password; auth failures, 500→502 | Password authentication failure against postgres, directly quoted from checkout-api logs (`FATAL: password authentication failed for user "app"`); correctly refuted three alternative hypotheses (resource limits, routing, image/crashloop) with specific evidence | Yes | High |
| config_problem | checkout-api-config's DATABASE_URL points at a nonexistent host | Not completed — four attempts across two sessions, each halted by a Groq free-tier 429 (8,000 tokens/min limit) before reaching a conclusion; wait times grew across attempts (238s, 593s, 958s) | N/A | N/A |

Three of four scenarios were correctly diagnosed with well-supported evidence chains, one at high confidence with a directly-quoted log line as root evidence. The fourth could not be completed within the constraints of the free-tier LLM key used for testing (Groq, `openai/gpt-oss-20b`, 8,000 tokens/minute) — every attempt failed at the same point (hypothesis generation, immediately after baseline evidence gathering), suggesting this scenario's baseline evidence payload is large enough to consistently exhaust the per-minute token budget on the first LLM call. See Limitations and Interesting Failures.

## 8. Limitations
- - Free-tier LLM (Groq, `openai/gpt-oss-20b`, 8,000 tokens/min) — one of four scenarios (`config_problem`) could not complete an investigation within this budget across four attempts.
- Single kind cluster, single scenario run at a time — no concurrent-incident handling.
- Restricted kubeconfig token expires after 8 hours and must be manually recreated.
- Agent's own 90-second per-call retry deadline is shorter than some Groq-reported retry-after windows (observed 230s–1191s), so a rate-limited run fails rather than waiting it out.
- Read-only by design: the agent can identify a misconfiguration (e.g. 0 replicas, wrong env var) but not who or what caused the change, and cannot remediate.
- Fixed, pre-scripted scenarios (`scenarios/manifest.yaml`) rather than organically occurring incidents.
- Jaeger tool assumes the deployed backend serves the `/api/v3/*` shape (v2.21+); would need adjustment for older Jaeger versions.

## 9. Interesting failures

**1. Confidence miscalibration on strong evidence (`dependency_failure`).**
The agent correctly identified postgres unavailability, with a clean, consistent evidence chain (connection-refused logs → 0 replicas confirmed → 502s traced end-to-end). Its own "Uncertainty" field even stated: *"None; all evidence consistently points to PostgreSQL unavailability as the root cause."* Despite this, "Confidence" was labeled `low`. The confidence field and the model's own stated certainty aren't reliably linked — see §5 for the likely code-level cause. Separately, the agent correctly found postgres at 0 replicas but, being read-only, could not identify who or what scaled it down — a reasonable and expected limit for this design.

**2. Rate-limit fragility on the free tier (`config_problem`).**
This scenario failed to reach a conclusion across four separate attempts (in two testing sessions), always at the same point: immediately after baseline evidence gathering, on the first `generate_hypotheses` LLM call. Reported retry-after windows grew across attempts (238s → 593s → 958s), suggesting this scenario's baseline evidence (pod state, events, and — once Prometheus connectivity was fixed — real metric series) produces a prompt large enough to consistently exceed the free tier's 8,000 tokens/minute budget on the very first LLM call, rather than a one-off transient spike. By contrast, `bad_deployment` — which failed identically on its first attempt — succeeded cleanly on retry, suggesting its evidence payload sits closer to the budget boundary and is more timing-dependent. In every failed `config_problem` attempt, the agent degraded safely: it reported "Root Cause: Unable to determine," `low` confidence, and named the exact failure in its "Uncertainty" field, rather than crashing or hallucinating a cause. A paid tier, a model with a higher per-minute ceiling, or trimming the baseline evidence payload for token-heavy scenarios would likely resolve this.

**3. Jaeger API version mismatch (caught before it affected results).**
`jaeger_tool.py` was initially written against the legacy Jaeger REST shape (`/api/services`, `/api/traces`), but the deployed Jaeger (v2.21) only serves `/api/v3/*` with an OTLP-style response — confirmed by `curl "$JAEGER_URL/api/v3/services"` returning a valid service list where the legacy path 404'd. The tool was corrected to use `/api/v3/*` before any scenario was run, so this had no effect on the results in §7. Flagged here as a fragile integration point if Jaeger is upgraded or downgraded again.

## 10. Next steps
- Raise or make configurable the per-call retry deadline (or add exponential backoff with jitter) so a transient free-tier 429 doesn't abort an otherwise-successful investigation.
- Fix the confidence-assignment logic so it can't contradict the model's own stated certainty in the same response.
- Add a lightweight "who changed this" signal (e.g. correlate a Deployment/ReplicaSet's `lastTransitionTime` or annotations with recent k8s events) so the agent can attribute a config change or scale-down, not just detect its symptom.
- Test against a paid or higher-limit LLM tier to get a complete four-scenario baseline for comparison against this free-tier run.
- Re-run the injection test (§6c) against a live Loki-captured injected line rather than a simulated fixture, if scenario time allows.