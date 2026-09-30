import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE = "frontend"
CHECKOUT_API_URL = os.environ.get("CHECKOUT_API_URL", "http://checkout-api:8080")
UNMEASURED_PATHS = {"/healthz", "/metrics"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "service": SERVICE,
            "message": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(SERVICE)

provider = TracerProvider(resource=Resource.create({"service.name": SERVICE}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
HTTPXClientInstrumentor().instrument()

REQUESTS = Counter("http_requests_total", "Total HTTP requests", ["service", "status_code"])
DURATION = Histogram("http_request_duration_seconds", "HTTP request duration in seconds", ["service"])

app = FastAPI()
client = httpx.AsyncClient(timeout=5.0)


@app.middleware("http")
async def measure(request: Request, call_next):
    if request.url.path in UNMEASURED_PATHS:
        return await call_next(request)
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        elapsed = time.perf_counter() - started
        REQUESTS.labels(service=SERVICE, status_code=str(status)).inc()
        DURATION.labels(service=SERVICE).observe(elapsed)
        log.log(
            logging.ERROR if status >= 500 else logging.INFO,
            "%s %s -> %d",
            request.method,
            request.url.path,
            status,
            extra={"fields": {"status_code": status, "duration_ms": round(elapsed * 1000, 1)}},
        )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/checkout")
async def checkout():
    try:
        upstream = await client.post(f"{CHECKOUT_API_URL}/checkout")
    except httpx.HTTPError as exc:
        log.error("checkout-api request failed: %s: %s", type(exc).__name__, exc)
        return JSONResponse(status_code=502, content={"status": "error", "detail": "checkout-api request failed"})
    if upstream.status_code != 200:
        log.error("checkout-api returned status %d", upstream.status_code)
        return JSONResponse(status_code=502, content={"status": "error", "detail": "checkout-api error"})
    return upstream.json()


FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,metrics")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_config=None, access_log=False)
