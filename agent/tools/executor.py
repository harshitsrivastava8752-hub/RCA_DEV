import json
import re
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from agent.tools import jaeger_tool, k8s_tool, loki_tool, prometheus_tool
from agent.tools.schemas import (
    JaegerGetTracesInput,
    K8sGetDeploymentInput,
    K8sGetEventsInput,
    K8sGetPodLogsInput,
    K8sGetPodsInput,
    K8sGetResourceConfigInput,
    LokiQueryInput,
    PrometheusQueryInput,
    ToolResult,
)

__all__ = ["dispatch", "ToolResult", "TOOL_CATALOG"]

_MAX_WRAPPED = 3000
_MAX_ERROR = 500
_OBSERVATION_TAG_RE = re.compile(r"<\s*(/?)\s*observation", re.IGNORECASE)

_TOOLS: dict[str, tuple[type[BaseModel], Callable[..., Any]]] = {
    "k8s_get_events": (K8sGetEventsInput, k8s_tool.k8s_get_events),
    "k8s_get_pods": (K8sGetPodsInput, k8s_tool.k8s_get_pods),
    "k8s_get_pod_logs": (K8sGetPodLogsInput, k8s_tool.k8s_get_pod_logs),
    "k8s_get_deployment": (K8sGetDeploymentInput, k8s_tool.k8s_get_deployment),
    "k8s_get_resource_config": (K8sGetResourceConfigInput, k8s_tool.k8s_get_resource_config),
    "prometheus_query": (PrometheusQueryInput, prometheus_tool.prometheus_query),
    "loki_query": (LokiQueryInput, loki_tool.loki_query),
    "jaeger_get_traces": (JaegerGetTracesInput, jaeger_tool.jaeger_get_traces),
}

TOOL_CATALOG: dict[str, str] = {
    "k8s_get_events": (
        "List recent Kubernetes events (newest first). "
        "Args: namespace (demo-app|observability|agent-system), since_minutes (int, default 30)."
    ),
    "k8s_get_pods": (
        "List pods with phase, restart counts, container states and last termination reason (e.g. OOMKilled). "
        "Args: namespace, label_selector (optional, e.g. app=checkout-api)."
    ),
    "k8s_get_pod_logs": (
        "Fetch raw container logs from one pod. "
        "Args: namespace, pod (exact pod name), container (optional), tail_lines (int, default 200), previous (bool, logs of the prior crashed container)."
    ),
    "k8s_get_deployment": (
        "Get a Deployment's rollout status: replica counts, revision, images, conditions. "
        "Args: namespace, name."
    ),
    "k8s_get_resource_config": (
        "Get configuration of a Deployment (env vars, envFrom, resource requests/limits, args) or a ConfigMap (data). "
        "Args: namespace, kind (deployment|configmap), name."
    ),
    "prometheus_query": (
        "Run a PromQL range query over the lookback window; returns per-series first/last/min/max and a few samples. "
        "Args: promql (e.g. sum by (service,status_code)(rate(http_requests_total[1m])); "
        "kube_pod_container_status_restarts_total), lookback_minutes (int, default 30)."
    ),
    "loki_query": (
        "Run a LogQL log-stream query; returns log lines newest first. "
        "Args: logql (e.g. {namespace=\"demo-app\",app=\"checkout-api\"} |= \"error\"), lookback_minutes (int, default 30)."
    ),
    "jaeger_get_traces": (
        "Search Jaeger traces for a service; returns compact spans with errors, status codes and durations. "
        "Args: service (frontend|checkout-api), operation (optional), lookback_minutes (int, default 30), tags (optional dict, e.g. {\"error\": \"true\"})."
    ),
}


def _plain(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json", exclude_none=True)
    if isinstance(obj, list):
        return [_plain(o) for o in obj]
    return obj


def _render(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data or "(no output)"
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def _clip(text: str, limit: int, keep_tail: bool) -> str:
    if len(text) <= limit:
        return text
    if keep_tail:
        marker = "[truncated]...\n"
        return marker + text[-(limit - len(marker)):]
    marker = "...[truncated]"
    return text[: limit - len(marker)] + marker


def _wrap(tool_name: str, body: str, keep_tail: bool) -> str:
    source = re.sub(r"[^A-Za-z0-9_.-]", "_", tool_name)[:64]
    head = f'<observation source="{source}">\n'
    foot = "\n</observation>"
    # neutralise any observation tag inside the payload so untrusted text cannot close the wrapper early
    body = _OBSERVATION_TAG_RE.sub(r"&lt;\1observation", body)
    return head + _clip(body, _MAX_WRAPPED - len(head) - len(foot), keep_tail) + foot


def _failure(tool_name: str, error: str) -> ToolResult:
    tool_name = tool_name[:64]
    error = error[:_MAX_ERROR]
    return ToolResult(
        tool_name=tool_name,
        ok=False,
        data=None,
        error=error,
        wrapped_for_model=_wrap(tool_name, f"ERROR: {error}", keep_tail=False),
    )


def _summarize_validation(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
    )


def dispatch(tool_name: str, tool_input: dict) -> ToolResult:
    try:
        spec = _TOOLS.get(tool_name) if isinstance(tool_name, str) else None
        if spec is None:
            return _failure(str(tool_name), f"unknown tool {str(tool_name)[:64]!r}; allowed tools: {', '.join(_TOOLS)}")
        input_cls, fn = spec
        args = input_cls.model_validate(tool_input)
    except ValidationError as exc:
        return _failure(tool_name, f"invalid input: {_summarize_validation(exc)}")
    except Exception as exc:
        return _failure(str(tool_name), f"{type(exc).__name__}: {exc}")

    try:
        data = _plain(fn(**args.model_dump()))
    except Exception as exc:
        return _failure(tool_name, f"{type(exc).__name__}: {exc}")

    return ToolResult(
        tool_name=tool_name,
        ok=True,
        data=data,
        error=None,
        wrapped_for_model=_wrap(tool_name, _render(data), keep_tail=isinstance(data, str)),
    )
