# k8s-rca-agent

Prototype read-only AI agent that investigates a live Kubernetes cluster and
its observability stack (Prometheus, Loki, Jaeger) from a natural-language
incident description and produces a structured RCA.

Verified end-to-end on a kind cluster, including all four scenarios and the
security demo. `jaeger-query` on this deployment serves Jaeger v2's
`/api/v3/*` API, not the legacy `/api/services` shape — see
`agent/tools/jaeger_tool.py`.

## Prerequisites

- Docker
- kind
- Helm
- kubectl and make
- Python 3.11
- A free API key for any OpenAI-compatible LLM provider

Install Python dependencies from the repo root:

```bash
pip install -r requirements.txt
```

## LLM configuration

| Variable | Meaning |
|---|---|
| `LLM_BASE_URL` | OpenAI-compatible base URL |
| `LLM_API_KEY` | Provider API key |
| `LLM_MODEL` | Model name as the provider spells it |
| `LLM_MAX_RPM` | Client-side requests-per-minute cap (default 20) |

Groq-style (used for this submission's runs):

```bash
export LLM_BASE_URL="https://api.groq.com/openai/v1"
export LLM_API_KEY="<your-groq-key>"
export LLM_MODEL="openai/gpt-oss-20b"
export LLM_MAX_RPM=20
```

Gemini OpenAI-compat-style:

```bash
export LLM_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/"
export LLM_API_KEY="<your-gemini-key>"
export LLM_MODEL="gemini-2.5-flash"
export LLM_MAX_RPM=10
```

Free-tier limits and model names change often. Verify the current limits and
model names with your provider yourself, and set `LLM_MAX_RPM` at or below
the limit you find. On Groq's free tier, `openai/gpt-oss-20b` is capped at
8,000 tokens/minute — some scenarios' baseline evidence payload can exceed
this on the very first LLM call (see `WRITEUP.md` §9 for a documented case).

A copy of all required variable names (with no values) is in `.env.example`.

## Cluster setup

Create the cluster and observability stack (blocks until the Helm releases
are ready):

```bash
make -C infra up
```

Build, load and deploy the demo app:

```bash
kubectl apply -f infra/manifests/namespaces.yaml
for svc in frontend checkout-api; do
  docker build -t rca-demo/$svc:v1 demo-app/$svc
  kind load docker-image rca-demo/$svc:v1 --name rca-agent
done
kubectl apply -f demo-app/k8s/
kubectl -n demo-app wait --for=condition=Available deployment --all --timeout=180s
```

Make the demo app and observability services reachable from the host (the
default tool URLs are in-cluster `.svc` addresses):

```bash
kubectl -n demo-app port-forward svc/frontend 8080:8080 &
kubectl -n observability port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 &
kubectl -n observability port-forward svc/loki 3100:3100 &
kubectl -n observability port-forward svc/jaeger-query 16686:16686 &
export PROMETHEUS_URL=http://localhost:9090
export LOKI_URL=http://localhost:3100
export JAEGER_URL=http://localhost:16686
```

Generate a little traffic and confirm all three observability sources have
data before running a scenario:

```bash
for i in $(seq 1 20); do curl -s -o /dev/null localhost:8080/; done
curl -s "$PROMETHEUS_URL/api/v1/query?query=up" | head -c 150; echo
curl -s "$JAEGER_URL/api/v3/services"; echo
curl -s "$LOKI_URL/ready"; echo
```

## Restricted agent identity (RBAC)

The agent is meant to run as a read-only `rca-agent` ServiceAccount, not as
your admin kubeconfig. Apply the RBAC objects and mint a token:

```bash
kubectl apply -f infra/manifests/rbac.yaml

TOKEN=$(kubectl create token rca-agent -n agent-system --duration=8h)
KUBECONFIG=~/.kube/rca-agent-config kubectl config set-credentials rca-agent --token="$TOKEN"
KUBECONFIG=~/.kube/rca-agent-config kubectl config set-context --current --user=rca-agent
```

Verify the boundary:

```bash
export KUBECONFIG=~/.kube/rca-agent-config
kubectl auth can-i list pods -n demo-app        # expect: yes
kubectl auth can-i delete pods -n demo-app      # expect: no
kubectl auth can-i get secrets -n demo-app      # expect: no
unset KUBECONFIG
```

The token expires after 8 hours; recreate it with the same two `kubectl
create token` / `config set-credentials` commands above when it does.

From here on, use **two separate shells/roles**:

- **Admin** (no `KUBECONFIG` set): applies and reverts scenarios, runs the
  security demo's RBAC probe, manages the cluster.
- **Agent** (`export KUBECONFIG=~/.kube/rca-agent-config`): runs
  `runner/investigate.py`. Its Kubernetes tool calls go through this
  restricted identity, so the RBAC and NetworkPolicy boundary is actually
  exercised during an investigation, not bypassed.

Applying or reverting a scenario as the restricted identity will fail with a
`403 Forbidden` (by design) — if that happens, `unset KUBECONFIG` first.

## Run an investigation

Apply a scenario (`oom`, `bad_deployment`, `dependency_failure` or
`config_problem`) as **admin**:

```bash
unset KUBECONFIG
python scenarios/oom.py apply
```

Wait 2–3 minutes for the failure to show up in metrics, logs and traces.

Investigate as the **agent**:

```bash
export KUBECONFIG=~/.kube/rca-agent-config
python runner/investigate.py --scenario oom
```

Revert as **admin**:

```bash
unset KUBECONFIG
python scenarios/oom.py revert
```

The RCA is printed and written to `results/<name>/<name>.md` and
`results/<name>/<name>.json`. The JSON holds the scenario name, incident
prompt, the manifest's `ground_truth` and the RCA; the markdown file ends
with the ground truth for comparison. A scenario's results folder may
contain more than one run if an earlier attempt failed and was archived for
reference (e.g. `results/config_problem/run1_429_attempt/`).

### All scenarios

```bash
export KUBECONFIG=~/.kube/rca-agent-config
python runner/investigate.py --all
```

For each scenario except `injection_fixture`, in order, this applies it,
waits, investigates, and reverts, sleeping between scenarios to stay within
`LLM_MAX_RPM`. Do not apply scenarios manually when using `--all`.

## Security demo

```bash
export KUBECONFIG=~/.kube/rca-agent-config
python runner/security_demo.py | tee results/security_demo.txt
```

This checks three things: the restricted identity gets `403 Forbidden` on
disallowed actions (delete pods, read Secrets), the tool executor rejects
disallowed or malformed dispatch inputs, and a prompt-injection attempt
embedded in a log line is treated as inert data rather than an instruction.

## Notes

- Prometheus, Loki and Jaeger keep data in memory only. Data is lost on pod
  restart, so run a scenario and its investigation close together.
- Do not run scenarios concurrently; their symptoms overlap and
  `bad_deployment` masks `config_problem`.
- Port-forwards and exported `*_URL`/`KUBECONFIG` variables do not survive a
  closed terminal; re-run the relevant commands above in a new shell.
- Confirm your admin kubeconfig's context name with
  `kubectl config current-context` before running scenario scripts if you
  see unexpected-cluster errors; override with `KUBE_CONTEXT` if your
  scripts support it.
