import datetime as dt
import functools
import json
import os
from typing import Literal

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from agent.tools.schemas import (
    K8sCondition,
    K8sContainerStatus,
    K8sDeployment,
    K8sEvent,
    K8sPod,
    iso_utc,
)

_TIMEOUT = 15
_MAX_EVENTS = 50
_MAX_PODS = 30
_IN_CLUSTER: bool | None = None


def load_config() -> bool:
    """Load in-cluster config (rca-agent ServiceAccount); fall back to local kubeconfig for dev. Returns True when in-cluster."""
    global _IN_CLUSTER
    if _IN_CLUSTER is None:
        try:
            config.load_incluster_config()
            _IN_CLUSTER = True
        except config.ConfigException:
            config.load_kube_config(context=os.environ.get("KUBE_CONTEXT", "kind-rca-agent"))
            _IN_CLUSTER = False
    return _IN_CLUSTER


@functools.lru_cache(maxsize=1)
def _apis() -> tuple[client.CoreV1Api, client.AppsV1Api]:
    load_config()
    return client.CoreV1Api(), client.AppsV1Api()


def describe_api_error(e: ApiException) -> str:
    try:
        detail = str(json.loads(e.body)["message"])[:300]
    except (TypeError, ValueError, KeyError):
        detail = ""
    return f"Kubernetes API {e.status} {e.reason}" + (f": {detail}" if detail else "")


def _translate_api_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ApiException as e:
            raise RuntimeError(describe_api_error(e)) from None

    return wrapper


def _iso(ts: dt.datetime | None) -> str | None:
    return iso_utc(ts.timestamp()) if ts else None


def _state(st) -> str:
    if st is None:
        return "unknown"
    for name in ("running", "waiting", "terminated"):
        s = getattr(st, name)
        if s is not None:
            reason = getattr(s, "reason", None)
            return f"{name}:{reason}" if reason else name
    return "unknown"


def _container_status(cs) -> K8sContainerStatus:
    last = cs.last_state.terminated if cs.last_state else None
    return K8sContainerStatus(
        name=cs.name,
        ready=bool(cs.ready),
        restart_count=cs.restart_count or 0,
        state=_state(cs.state),
        last_termination_reason=last.reason if last else None,
        last_exit_code=last.exit_code if last else None,
        last_terminated_at=_iso(last.finished_at) if last else None,
    )


def _value_source(vf) -> str:
    if vf.config_map_key_ref:
        return f"configMap:{vf.config_map_key_ref.name}/{vf.config_map_key_ref.key}"
    if vf.secret_key_ref:
        return f"secretRef:{vf.secret_key_ref.name}/{vf.secret_key_ref.key}"
    if vf.field_ref:
        return f"field:{vf.field_ref.field_path}"
    return "other"


def _env_from_source(ef) -> str:
    if ef.config_map_ref:
        return f"configMap:{ef.config_map_ref.name}"
    if ef.secret_ref:
        return f"secretRef:{ef.secret_ref.name}"
    return "other"


def _container_config(c) -> dict:
    res = c.resources
    out = {
        "name": c.name,
        "image": c.image,
        "env": [
            {"name": e.name, "value": e.value} if e.value_from is None else {"name": e.name, "from": _value_source(e.value_from)}
            for e in (c.env or [])
        ],
        "env_from": [_env_from_source(ef) for ef in (c.env_from or [])],
        "resources": {
            "requests": (res.requests if res else None) or {},
            "limits": (res.limits if res else None) or {},
        },
    }
    if c.command:
        out["command"] = c.command
    if c.args:
        out["args"] = c.args
    return out


@_translate_api_errors
def k8s_get_events(namespace: str, since_minutes: int = 30) -> list[K8sEvent]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=since_minutes)
    items = _apis()[0].list_namespaced_event(namespace, _request_timeout=_TIMEOUT).items
    rows = []
    for e in items:
        ts = e.last_timestamp or e.event_time or e.first_timestamp or e.metadata.creation_timestamp
        if ts < cutoff:
            continue
        obj = e.involved_object
        rows.append((
            ts,
            K8sEvent(
                type=e.type,
                reason=e.reason,
                message=e.message[:300] if e.message else None,
                involved_object=f"{obj.kind}/{obj.name}" if obj else "unknown",
                count=e.count,
                last_timestamp=iso_utc(ts.timestamp()),
            ),
        ))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [ev for _, ev in rows[:_MAX_EVENTS]]


@_translate_api_errors
def k8s_get_pods(namespace: str, label_selector: str | None = None) -> list[K8sPod]:
    items = _apis()[0].list_namespaced_pod(
        namespace, label_selector=label_selector, _request_timeout=_TIMEOUT
    ).items
    pods = []
    for p in items[:_MAX_PODS]:
        containers = [_container_status(cs) for cs in (p.status.container_statuses or [])]
        with_last = [c for c in containers if c.last_termination_reason]
        latest = max(with_last, key=lambda c: c.last_terminated_at or "") if with_last else None
        pods.append(K8sPod(
            name=p.metadata.name,
            phase=p.status.phase,
            reason=p.status.reason,
            restart_count=sum(c.restart_count for c in containers),
            last_termination_reason=latest.last_termination_reason if latest else None,
            created=_iso(p.metadata.creation_timestamp),
            containers=containers,
        ))
    return pods


@_translate_api_errors
def k8s_get_pod_logs(
    namespace: str,
    pod: str,
    container: str | None = None,
    tail_lines: int = 200,
    previous: bool = False,
) -> str:
    # _preload_content=False: the client's default str deserialization mangles logs that are a single JSON line
    resp = _apis()[0].read_namespaced_pod_log(
        name=pod,
        namespace=namespace,
        container=container,
        tail_lines=tail_lines,
        previous=previous,
        _preload_content=False,
        _request_timeout=_TIMEOUT,
    )
    return resp.data.decode("utf-8", errors="replace")


@_translate_api_errors
def k8s_get_deployment(namespace: str, name: str) -> K8sDeployment:
    d = _apis()[1].read_namespaced_deployment(name, namespace, _request_timeout=_TIMEOUT)
    s = d.status
    return K8sDeployment(
        name=d.metadata.name,
        replicas=d.spec.replicas or 0,
        ready_replicas=s.ready_replicas or 0,
        updated_replicas=s.updated_replicas or 0,
        available_replicas=s.available_replicas or 0,
        generation=d.metadata.generation,
        observed_generation=s.observed_generation,
        revision=(d.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
        images=[c.image for c in d.spec.template.spec.containers],
        conditions=[
            K8sCondition(
                type=c.type,
                status=c.status,
                reason=c.reason,
                message=c.message[:300] if c.message else None,
                last_update=_iso(c.last_update_time),
            )
            for c in (s.conditions or [])
        ],
        created=_iso(d.metadata.creation_timestamp),
    )


@_translate_api_errors
def k8s_get_resource_config(
    namespace: str, kind: Literal["deployment", "configmap"], name: str
) -> dict:
    if kind == "deployment":
        d = _apis()[1].read_namespaced_deployment(name, namespace, _request_timeout=_TIMEOUT)
        tpl = d.spec.template
        return {
            "kind": "deployment",
            "name": name,
            "replicas": d.spec.replicas,
            "pod_annotations": (tpl.metadata.annotations if tpl.metadata else None) or {},
            "containers": [_container_config(c) for c in tpl.spec.containers],
        }
    if kind == "configmap":
        cm = _apis()[0].read_namespaced_config_map(name, namespace, _request_timeout=_TIMEOUT)
        return {"kind": "configmap", "name": name, "data": cm.data or {}}
    raise ValueError(f"unsupported kind {kind!r}")
