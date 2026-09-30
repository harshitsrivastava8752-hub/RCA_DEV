import datetime as dt
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ALLOWED_NAMESPACES = frozenset({"demo-app", "observability", "agent-system"})
_NAME_PATTERN = r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"
_SELECTOR_RE = re.compile(r"^[\w.\-/=!, ()]+$")


def iso_utc(epoch_seconds: float) -> str:
    return dt.datetime.fromtimestamp(epoch_seconds, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ToolResult(BaseModel):
    tool_name: str
    ok: bool
    data: dict | list | str | None
    error: str | None
    wrapped_for_model: str


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _NamespacedInput(_Input):
    namespace: str

    @field_validator("namespace")
    @classmethod
    def _namespace_allowed(cls, v: str) -> str:
        if v not in ALLOWED_NAMESPACES:
            raise ValueError(f"namespace must be one of {sorted(ALLOWED_NAMESPACES)}")
        return v


class K8sGetEventsInput(_NamespacedInput):
    since_minutes: int = Field(30, ge=1, le=1440)


class K8sGetPodsInput(_NamespacedInput):
    label_selector: str | None = Field(None, min_length=1, max_length=256)

    @field_validator("label_selector")
    @classmethod
    def _selector_charset(cls, v: str | None) -> str | None:
        if v is not None and not _SELECTOR_RE.match(v):
            raise ValueError("label_selector contains characters outside [A-Za-z0-9_.-/=!, ()]")
        return v


class K8sGetPodLogsInput(_NamespacedInput):
    pod: str = Field(min_length=1, max_length=253, pattern=_NAME_PATTERN)
    container: str | None = Field(None, min_length=1, max_length=253, pattern=_NAME_PATTERN)
    tail_lines: int = Field(200, ge=1, le=1000)
    previous: bool = False


class K8sGetDeploymentInput(_NamespacedInput):
    name: str = Field(min_length=1, max_length=253, pattern=_NAME_PATTERN)


class K8sGetResourceConfigInput(_NamespacedInput):
    kind: Literal["deployment", "configmap"]
    name: str = Field(min_length=1, max_length=253, pattern=_NAME_PATTERN)


class PrometheusQueryInput(_Input):
    promql: str = Field(min_length=1, max_length=2000)
    lookback_minutes: int = Field(30, ge=1, le=1440)


class LokiQueryInput(_Input):
    logql: str = Field(min_length=1, max_length=2000)
    lookback_minutes: int = Field(30, ge=1, le=1440)


class JaegerGetTracesInput(_Input):
    service: str = Field(min_length=1, max_length=128)
    operation: str | None = Field(None, min_length=1, max_length=256)
    lookback_minutes: int = Field(30, ge=1, le=1440)
    tags: dict | None = None

    @field_validator("tags")
    @classmethod
    def _tags_scalar(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        if len(v) > 10:
            raise ValueError("tags may contain at most 10 entries")
        if not all(isinstance(k, str) and isinstance(x, (str, int, float, bool)) for k, x in v.items()):
            raise ValueError("tags must map string keys to string/number/bool values")
        return v


class K8sEvent(BaseModel):
    type: str | None = None
    reason: str | None = None
    message: str | None = None
    involved_object: str
    count: int | None = None
    last_timestamp: str


class K8sContainerStatus(BaseModel):
    name: str
    ready: bool
    restart_count: int
    state: str
    last_termination_reason: str | None = None
    last_exit_code: int | None = None
    last_terminated_at: str | None = None


class K8sPod(BaseModel):
    name: str
    phase: str | None = None
    reason: str | None = None
    restart_count: int
    last_termination_reason: str | None = None
    created: str | None = None
    containers: list[K8sContainerStatus]


class K8sCondition(BaseModel):
    type: str
    status: str
    reason: str | None = None
    message: str | None = None
    last_update: str | None = None


class K8sDeployment(BaseModel):
    name: str
    replicas: int
    ready_replicas: int
    updated_replicas: int
    available_replicas: int
    generation: int | None = None
    observed_generation: int | None = None
    revision: str | None = None
    images: list[str]
    conditions: list[K8sCondition]
    created: str | None = None


class PromSeries(BaseModel):
    metric: dict[str, str]
    first: float
    last: float
    min: float
    max: float
    samples: list[tuple[str, float]]


class PromResult(BaseModel):
    series_total: int
    series: list[PromSeries]


class LokiLine(BaseModel):
    timestamp: str
    stream: dict[str, str]
    line: str


class JaegerSpan(BaseModel):
    service: str
    operation: str
    duration_ms: float
    error: bool
    status_code: str | None = None
    detail: str | None = None


class JaegerTrace(BaseModel):
    trace_id: str
    start: str
    duration_ms: float
    span_count: int
    error_span_count: int
    spans: list[JaegerSpan]
