import json
import logging
import os
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Annotated

import psycopg2
import uvicorn
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE = "checkout-api"
DATABASE_URL = os.environ["DATABASE_URL"]
UNMEASURED_PATHS = {"/healthz", "/metrics"}
leaked: list[bytearray] = []


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
log = logging.getLogger(SERVICE)

provider = TracerProvider(resource=Resource.create({"service.name": SERVICE}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
# psycopg2-binary is not recognised by the instrumentor's dependency check
Psycopg2Instrumentor().instrument(skip_dep_check=True)

REQUESTS = Counter("http_requests_total", "Total HTTP requests", ["service", "status_code"])
DURATION = Histogram("http_request_duration_seconds", "HTTP request duration in seconds", ["service"])

app = FastAPI()


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
def checkout():
    try:
        with closing(psycopg2.connect(DATABASE_URL, connect_timeout=3)) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM orders")
            order_count = cur.fetchone()[0]
    except Exception as exc:
        log.error("database query failed: %s: %s", type(exc).__name__, str(exc).strip())
        return JSONResponse(status_code=500, content={"status": "error", "detail": "database error"})
    return {"status": "ok", "order_count": order_count}


@app.get("/debug/leak", include_in_schema=False)
def leak(mb: Annotated[int, Query(ge=1)]):
    # filled, not bytearray(n): untouched zero pages are never resident and would not grow RSS
    leaked.append(bytearray(b"\x01") * (mb * 1024 * 1024))
    return {"leaked_mb": sum(len(chunk) for chunk in leaked) // (1024 * 1024)}


FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,metrics")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_config=None, access_log=False)
