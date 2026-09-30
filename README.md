# k8s-rca-agent

Prototype read-only AI agent that investigates a live Kubernetes cluster and its observability stack (Prometheus, Loki, Jaeger) from a natural-language incident description and produces a structured RCA.

## Prerequisites

- Docker
- kind
- Helm
- kubectl and make
- Python 3.11
- A free API key for any OpenAI-compatible LLM provider

Install Python dependencies from the repo root:

```
pip install -r requirements.txt
```

## LLM configuration

| Variable | Meaning |
|---|---|
| `LLM_BASE_URL` | OpenAI-compatible base URL |
| `LLM_API_KEY` | Provider API key |
| `LLM_MODEL` | Model name as the provider spells it |
| `LLM_MAX_RPM` | Client-side requests-per-minute cap (default 20) |

Groq-style:

```
export LLM_BASE_URL="https://api.groq.com/openai/v1"
export LLM_API_KEY="<your-groq-key>"
export LLM_MODEL="llama-3.3-70b-versatile"
export LLM_MAX_RPM=20
```

Gemini OpenAI-compat-style:

```
export LLM_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/"
export LLM_API_KEY="<your-gemini-key>"
export LLM_MODEL="gemini-2.5-flash"
export LLM_MAX_RPM=10
```

Free-tier limits and model names change often. Verify the current limits and model names with your provider yourself, and set `LLM_MAX_RPM` at or below the limit you find.

## Run an investigation

1. Create the cluster and observability stack (blocks until the Helm releases are ready):

   ```
   make -C infra up
   ```

2. Build, load and deploy the demo app:

   ```
   kubectl apply -f infra/manifests/namespaces.yaml
   for svc in frontend checkout-api; do
     docker build -t rca-demo/$svc:v1 demo-app/$svc
     kind load docker-image rca-demo/$svc:v1 --name rca-agent
   done
   kubectl apply -f demo-app/k8s/
   kubectl -n demo-app wait --for=condition=Available deployment --all --timeout=180s
   ```

3. Make the observability services reachable from the host (the default URLs are in-cluster `.svc` addresses):

   ```
   kubectl -n observability port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 &
   kubectl -n observability port-forward svc/loki 3100:3100 &
   kubectl -n observability port-forward svc/jaeger-query 16686:16686 &
   export PROMETHEUS_URL=http://localhost:9090
   export LOKI_URL=http://localhost:3100
   export JAEGER_URL=http://localhost:16686
   ```

4. Apply a scenario (`oom`, `bad_deployment`, `dependency_failure` or `config_problem`):

   ```
   python scenarios/oom.py apply
   ```

5. Wait about 60 seconds for metrics, logs and traces to be ingested.

6. Investigate:

   ```
   python runner/investigate.py --scenario oom
   ```

7. Revert:

   ```
   python scenarios/oom.py revert
   ```

The RCA is printed and written to `results/<name>/<name>.md` and `results/<name>/<name>.json`. The JSON holds the scenario name, incident prompt, the manifest's `ground_truth` and the RCA; the markdown file ends with the ground truth for comparison.

### All scenarios

```
python runner/investigate.py --all
```

For each scenario except `injection_fixture`, in order, this applies it, waits 60 seconds, investigates, and reverts. It sleeps 60 seconds between scenarios to stay within `LLM_MAX_RPM`. Do not apply scenarios manually when using `--all`.

## Notes

- Prometheus, Loki and Jaeger keep data in memory only. Data is lost on pod restart, so run a scenario and its investigation close together.
- Do not run scenarios concurrently; their symptoms overlap and `bad_deployment` masks `config_problem`.
- Scenario scripts require an admin kubeconfig (default context `kind-rca-agent`, override with `KUBE_CONTEXT`).
- Run from the host, the agent's Kubernetes tools use your local kubeconfig, not the `rca-agent` ServiceAccount. The read-only RBAC and NetworkPolicy boundary applies only to the agent running in-cluster as that ServiceAccount, not to this host-side run.
- This command sequence has not been run end-to-end on a live cluster. Service names and ports, notably `jaeger-query`, are unverified.
