import asyncio
import contextlib
import logging
import time
import zlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from tailsampler.config import GatewaySettings
from tailsampler.model import KeptTrace
from tailsampler.otlp import OtlpFormatError, decode_export_request
from tailsampler.policies import LatencyThresholds
from tailsampler.sampler import TailSampler
from tailsampler.store import SpanStore, StoredSpan

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_SEARCH_LIMIT = 200
NANOS_PER_MILLI = 1_000_000
TRACE_ID_LENGTH = 32

COUNTER_FIELDS = (
    ("spans_received", "Spans received by the sampler."),
    ("spans_kept", "Spans kept after sampling."),
    ("spans_dropped", "Spans dropped by sampling."),
    ("traces_kept", "Traces kept after a decision."),
    ("traces_dropped", "Traces dropped after a decision."),
    ("late_spans_kept", "Spans that arrived after their trace was kept."),
    ("late_spans_dropped", "Spans that arrived after their trace was dropped."),
    ("forced_decisions", "Traces decided early because the buffer was full."),
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Gateway:
    settings: GatewaySettings
    sampler: TailSampler
    store: SpanStore
    clock: Callable[[], int]

    def publish(self, kept: list[KeptTrace]) -> None:
        self.store.insert(kept)


def create_app(
    settings: GatewaySettings,
    thresholds: LatencyThresholds,
    clock: Callable[[], int] = time.monotonic_ns,
) -> Starlette:
    gateway = Gateway(
        settings=settings,
        sampler=TailSampler.from_settings(settings, thresholds),
        store=SpanStore(settings.database_path),
        clock=clock,
    )
    app = Starlette(
        routes=[
            Route("/v1/traces", ingest_traces, methods=["POST"]),
            Route("/v1/traces", search_traces, methods=["GET"]),
            Route("/v1/traces/{trace_id}", read_trace, methods=["GET"]),
            Route("/v1/estimates", read_estimates, methods=["GET"]),
            Route("/metrics", read_metrics, methods=["GET"]),
            Route("/healthz", read_health, methods=["GET"]),
        ],
        exception_handlers={HTTPException: json_error},
        lifespan=lifespan,
    )
    app.state.gateway = gateway
    return app


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    gateway: Gateway = app.state.gateway
    ticker = asyncio.create_task(tick_forever(gateway))
    try:
        yield
    finally:
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker
        gateway.publish(gateway.sampler.flush())
        gateway.store.close()


async def tick_forever(gateway: Gateway) -> None:
    while True:
        await asyncio.sleep(gateway.settings.tick_seconds)
        gateway.publish(gateway.sampler.advance(gateway.clock()))


async def json_error(request: Request, error: Exception) -> JSONResponse:
    status = error.status_code if isinstance(error, HTTPException) else 500
    detail = error.detail if isinstance(error, HTTPException) else "internal error"
    return JSONResponse({"error": detail}, status_code=status)


async def ingest_traces(request: Request) -> Response:
    gateway: Gateway = request.app.state.gateway
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(415, "only application/json OTLP requests are supported")
    body = _inflate(await request.body(), request.headers.get("content-encoding", ""))
    try:
        spans = decode_export_request(body)
    except OtlpFormatError as error:
        return JSONResponse({"error": str(error), "field": error.field_path}, status_code=400)
    gateway.publish(gateway.sampler.ingest(spans, gateway.clock()))
    return JSONResponse({})


def _inflate(raw: bytes, content_encoding: str) -> bytes:
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(413, "request body is too large")
    encoding = content_encoding.strip().lower()
    if encoding in ("", "identity"):
        return raw
    if encoding != "gzip":
        raise HTTPException(415, f"unsupported content-encoding {encoding}")
    try:
        inflated = zlib.decompressobj(zlib.MAX_WBITS | 16).decompress(raw, MAX_BODY_BYTES + 1)
    except zlib.error as error:
        raise HTTPException(400, "body is not valid gzip") from error
    if len(inflated) > MAX_BODY_BYTES:
        raise HTTPException(413, "request body is too large after decompression")
    return inflated


async def read_trace(request: Request) -> Response:
    gateway: Gateway = request.app.state.gateway
    trace_id = request.path_params["trace_id"].lower()
    if len(trace_id) != TRACE_ID_LENGTH or any(c not in "0123456789abcdef" for c in trace_id):
        raise HTTPException(400, "trace id must be 32 hexadecimal characters")
    stored = gateway.store.trace_spans(trace_id)
    if not stored:
        raise HTTPException(404, "trace not found, it may have been dropped by sampling")
    return JSONResponse(
        {
            "trace_id": trace_id,
            "reason": stored[0].reason,
            "weight": stored[0].weight,
            "spans": [_span_json(item) for item in stored],
        }
    )


def _span_json(item: StoredSpan) -> dict[str, Any]:
    span = item.span
    return {
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "service": span.service,
        "name": span.name,
        "kind": span.kind,
        "start_unix_nano": span.start_unix_nano,
        "end_unix_nano": span.end_unix_nano,
        "status_code": int(span.status_code),
        "status_message": span.status_message,
        "attributes": span.attributes,
    }


async def search_traces(request: Request) -> Response:
    gateway: Gateway = request.app.state.gateway
    parameters = request.query_params
    limit = _query_integer(parameters, "limit", default=20, minimum=1, maximum=MAX_SEARCH_LIMIT)
    min_duration_ms = _query_integer(parameters, "min_duration_ms", default=0, minimum=0)
    errors_only = parameters.get("errors_only", "false").lower() in ("1", "true", "yes")
    summaries = gateway.store.search(
        service=parameters.get("service"),
        errors_only=errors_only,
        min_duration_nano=min_duration_ms * NANOS_PER_MILLI,
        limit=limit,
    )
    return JSONResponse(
        {
            "traces": [
                {
                    "trace_id": item.trace_id,
                    "service": item.service,
                    "operation": item.name,
                    "start_unix_nano": item.start_unix_nano,
                    "duration_ms": item.duration_nano / NANOS_PER_MILLI,
                    "weight": item.weight,
                    "reason": item.reason,
                    "failed": item.failed,
                }
                for item in summaries
            ]
        }
    )


async def read_estimates(request: Request) -> Response:
    gateway: Gateway = request.app.state.gateway
    since = _query_integer(request.query_params, "since", default=None, minimum=0)
    until = _query_integer(request.query_params, "until", default=None, minimum=0)
    routes = gateway.store.estimates(since, until)
    return JSONResponse(
        {
            "since_unix_nano": since,
            "until_unix_nano": until,
            "total_estimated_requests": sum(item.estimated_requests for item in routes),
            "routes": [
                {
                    "service": item.service,
                    "operation": item.name,
                    "estimated_requests": item.estimated_requests,
                    "estimated_failed_requests": item.estimated_failed_requests,
                    "stored_traces": item.stored_traces,
                }
                for item in routes
            ],
        }
    )


def _query_integer(
    parameters: Any, name: str, default: int | None, minimum: int, maximum: int | None = None
) -> int | None:
    raw = parameters.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise HTTPException(400, f"{name} must be an integer") from error
    if value < minimum or (maximum is not None and value > maximum):
        upper = "" if maximum is None else f" and at most {maximum}"
        raise HTTPException(400, f"{name} must be at least {minimum}{upper}")
    return value


async def read_metrics(request: Request) -> Response:
    gateway: Gateway = request.app.state.gateway
    return PlainTextResponse(render_metrics(gateway), media_type="text/plain; version=0.0.4")


def render_metrics(gateway: Gateway) -> str:
    counters = gateway.sampler.counters
    lines: list[str] = []
    for name, description in COUNTER_FIELDS:
        metric = f"tailsampler_{name}_total"
        lines += [
            f"# HELP {metric} {description}",
            f"# TYPE {metric} counter",
            f"{metric} {getattr(counters, name)}",
        ]
    reason_metric = "tailsampler_traces_kept_by_reason_total"
    lines += [
        f"# HELP {reason_metric} Traces kept, by the rule that kept them.",
        f"# TYPE {reason_metric} counter",
    ]
    lines += [
        f'{reason_metric}{{reason="{reason}"}} {count}'
        for reason, count in sorted(counters.kept_by_reason.items())
    ]
    gauges = (
        ("buffered_traces", "Traces waiting for a decision.", gateway.sampler.buffered_traces),
        ("buffered_spans", "Spans waiting for a decision.", gateway.sampler.buffered_spans),
        ("peak_buffered_traces", "Highest buffered trace count.", counters.peak_buffered_traces),
        ("peak_buffered_spans", "Highest buffered span count.", counters.peak_buffered_spans),
        ("stored_spans", "Spans in the store.", gateway.store.span_count()),
    )
    for name, description, value in gauges:
        metric = f"tailsampler_{name}"
        lines += [f"# HELP {metric} {description}", f"# TYPE {metric} gauge", f"{metric} {value}"]
    return "\n".join(lines) + "\n"


async def read_health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})
