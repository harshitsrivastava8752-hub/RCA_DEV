import base64
import json
import os
from datetime import datetime, timedelta, timezone

import requests

from agent.tools.schemas import JaegerSpan, JaegerTrace, iso_utc

_DEFAULT_URL = "http://jaeger-query.observability.svc:16686"
_TIMEOUT = 20
_MAX_TRACES = 10
_MAX_SPANS = 15
_STATUS_ATTR_KEYS = ("http.status_code", "http.response.status_code")

# Jaeger v2 (this deployment) serves its query API at /api/v3/*, returning
# streamed OTLP-JSON (opentelemetry.proto.trace.v1.TracesData), NOT the old
# v1 REST shape (/api/traces, /api/services) that earlier Jaeger versions used.
# See: https://github.com/jaegertracing/jaeger-idl/blob/main/proto/api_v3/query_service.proto


def _base_url() -> str:
    return os.environ.get("JAEGER_URL", _DEFAULT_URL).rstrip("/")


def _rfc3339_ns(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}000Z"


def _decode_id(b64_id: str) -> str:
    try:
        return base64.b64decode(b64_id).hex()
    except Exception:
        return b64_id


def _any_value(v):
    if not isinstance(v, dict):
        return v
    for k in ("stringValue", "boolValue", "doubleValue"):
        if k in v:
            return v[k]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "arrayValue" in v:
        return [_any_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return {kv["key"]: _any_value(kv.get("value")) for kv in v["kvlistValue"].get("values", [])}
    return None


def _attrs_to_dict(attrs: list | None) -> dict:
    return {a["key"]: _any_value(a.get("value")) for a in (attrs or [])}


def _iter_json_objects(text: str):
    """Jaeger v3 streams multiple JSON objects (one per gRPC-gateway chunk),
    not a JSON array and not always newline-delimited. Parse them by
    repeatedly decoding the next top-level JSON value."""
    decoder = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        obj, end = decoder.raw_decode(text, i)
        yield obj
        i = end


def _exception_detail(span: dict) -> str | None:
    for ev in span.get("events", []):
        if ev.get("name") != "exception":
            continue
        attrs = _attrs_to_dict(ev.get("attributes"))
        etype = attrs.get("exception.type", "")
        emsg = attrs.get("exception.message", "")
        if etype or emsg:
            return f"{etype}: {emsg}"[:300]
    return None


def _collect_traces(body_text: str) -> dict[str, list[dict]]:
    """Returns {trace_id_hex: [span_dict, ...]}, span_dict has our own
    flattened fields (service, operation, start_ns, duration_ns, error,
    status_code, detail)."""
    traces: dict[str, list[dict]] = {}
    for msg in _iter_json_objects(body_text):
        trace_data = msg.get("result", msg)
        for rs in trace_data.get("resourceSpans", []):
            resource_attrs = _attrs_to_dict(rs.get("resource", {}).get("attributes"))
            service_name = resource_attrs.get("service.name", "unknown")
            for ss in rs.get("scopeSpans", []):
                for s in ss.get("spans", []):
                    trace_id = _decode_id(s["traceId"])
                    attrs = _attrs_to_dict(s.get("attributes"))
                    status = s.get("status", {})
                    error = status.get("code") == "STATUS_CODE_ERROR"
                    status_code = None
                    for k in _STATUS_ATTR_KEYS:
                        if k in attrs:
                            status_code = str(attrs[k])
                            break
                    detail = _exception_detail(s) or status.get("message")
                    start_ns = int(s["startTimeUnixNano"])
                    end_ns = int(s.get("endTimeUnixNano", start_ns))
                    traces.setdefault(trace_id, []).append({
                        "service": service_name,
                        "operation": s.get("name", "unknown"),
                        "start_ns": start_ns,
                        "duration_ns": max(end_ns - start_ns, 0),
                        "error": error,
                        "status_code": status_code,
                        "detail": str(detail)[:300] if detail else None,
                    })
    return traces


def _compact_trace(trace_id: str, spans: list[dict]) -> JaegerTrace:
    spans = sorted(spans, key=lambda s: s["start_ns"])
    compact = [
        JaegerSpan(
            service=s["service"],
            operation=s["operation"],
            duration_ms=round(s["duration_ns"] / 1e6, 2),
            error=s["error"],
            status_code=s["status_code"],
            detail=s["detail"],
        )
        for s in spans[:_MAX_SPANS]
    ]
    start_ns = spans[0]["start_ns"]
    end_ns = max(s["start_ns"] + s["duration_ns"] for s in spans)
    return JaegerTrace(
        trace_id=trace_id,
        start=iso_utc(start_ns / 1e9),
        duration_ms=round((end_ns - start_ns) / 1e6, 2),
        span_count=len(spans),
        error_span_count=sum(1 for s in spans if s["error"]),
        spans=compact,
    )


def jaeger_get_traces(
    service: str,
    operation: str | None = None,
    lookback_minutes: int = 30,
    tags: dict | None = None,
) -> list[JaegerTrace]:
    now = datetime.now(timezone.utc)
    params = {
        "query.service_name": service,
        "query.start_time_min": _rfc3339_ns(now - timedelta(minutes=lookback_minutes)),
        "query.start_time_max": _rfc3339_ns(now),
        "query.num_traces": _MAX_TRACES,
    }
    if operation:
        params["query.operation_name"] = operation

    resp = requests.get(f"{_base_url()}/api/v3/traces", params=params, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"jaeger HTTP {resp.status_code}: {resp.text[:300]}")

    traces = _collect_traces(resp.text)
    # NOTE: the v3 API has no server-side arbitrary-tag filter (unlike v1's
    # ?tags=...). `tags` is accepted per the §1 signature but not applied
    # here yet; callers filtering on tags should inspect span.detail /
    # status_code on the returned traces instead.
    del tags

    result = [_compact_trace(tid, spans) for tid, spans in traces.items()]
    result.sort(key=lambda t: t.start, reverse=True)
    return result[:_MAX_TRACES]


def jaeger_get_services() -> list[str]:
    """Not part of the SHARED_CONTEXT §1 tool catalog, but useful standalone
    for verifying the pipeline (see runner/security_demo.py or ad-hoc checks)."""
    resp = requests.get(f"{_base_url()}/api/v3/services", timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"jaeger HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    return body.get("result", body).get("services", [])
