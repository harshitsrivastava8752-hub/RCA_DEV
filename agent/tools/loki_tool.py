import os
import time

import requests

from agent.tools.schemas import LokiLine, iso_utc

_DEFAULT_URL = "http://loki.observability.svc:3100"
_TIMEOUT = 20
_MAX_LINES = 100
_KEEP_LABELS = ("app", "pod")


def loki_query(logql: str, lookback_minutes: int = 30) -> list[LokiLine]:
    end_ns = time.time_ns()
    start_ns = end_ns - lookback_minutes * 60 * 1_000_000_000
    base = os.environ.get("LOKI_URL", _DEFAULT_URL).rstrip("/")
    resp = requests.get(
        f"{base}/loki/api/v1/query_range",
        params={
            "query": logql,
            "start": start_ns,
            "end": end_ns,
            "limit": _MAX_LINES,
            "direction": "backward",
        },
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"loki HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()["data"]
    if data.get("resultType") != "streams":
        raise RuntimeError("loki_query supports log stream queries only; use prometheus_query for metrics")

    rows = []
    for stream in data["result"]:
        labels = {k: v for k, v in stream["stream"].items() if k in _KEEP_LABELS}
        for ts, line in stream["values"]:
            rows.append((int(ts), LokiLine(timestamp=iso_utc(int(ts) / 1e9), stream=labels, line=line)))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [line for _, line in rows[:_MAX_LINES]]
