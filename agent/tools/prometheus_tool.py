import math
import os
import time

import requests

from agent.tools.schemas import PromResult, PromSeries, iso_utc

_DEFAULT_URL = "http://prometheus-kube-prometheus-prometheus.observability.svc:9090"
_TIMEOUT = 20
_MAX_SERIES = 15
_MAX_SAMPLES = 6


def _sig(x: float) -> float:
    return float(f"{x:.6g}")


def _sample_indices(n: int) -> list[int]:
    if n <= _MAX_SAMPLES:
        return list(range(n))
    return [round(i * (n - 1) / (_MAX_SAMPLES - 1)) for i in range(_MAX_SAMPLES)]


def prometheus_query(promql: str, lookback_minutes: int = 30) -> PromResult:
    end = time.time()
    start = end - lookback_minutes * 60
    step = max(15, lookback_minutes * 60 // 120)
    base = os.environ.get("PROMETHEUS_URL", _DEFAULT_URL).rstrip("/")
    resp = requests.get(
        f"{base}/api/v1/query_range",
        params={"query": promql, "start": start, "end": end, "step": step},
        timeout=_TIMEOUT,
    )
    try:
        body = resp.json()
    except ValueError:
        raise RuntimeError(f"prometheus HTTP {resp.status_code}: {resp.text[:200]}") from None
    if body.get("status") != "success":
        raise RuntimeError(f"prometheus {body.get('errorType', 'error')}: {body.get('error', resp.status_code)}")

    series = []
    for r in body["data"]["result"]:
        pts = []
        for t, v in r["values"]:
            f = float(v)
            if math.isfinite(f):
                pts.append((t, f))
        if not pts:
            continue
        vals = [f for _, f in pts]
        series.append(PromSeries(
            metric=r["metric"],
            first=_sig(vals[0]),
            last=_sig(vals[-1]),
            min=_sig(min(vals)),
            max=_sig(max(vals)),
            samples=[(iso_utc(pts[i][0]), _sig(pts[i][1])) for i in _sample_indices(len(pts))],
        ))
    # largest first so the wrapper's 3000-char truncation keeps the highest-signal series
    series.sort(key=lambda s: s.max, reverse=True)
    return PromResult(series_total=len(series), series=series[:_MAX_SERIES])
