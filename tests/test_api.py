import gzip
import json
import re

import pytest
from starlette.testclient import TestClient

from conftest import BASE_START_NANO, REPOSITORY_ROOT, wait_until
from tailsampler import api
from tailsampler.api import create_app
from tailsampler.config import GatewaySettings
from tailsampler.model import StatusCode
from tailsampler.otlp import build_export_request
from tailsampler.policies import LatencyThresholds
from tailsampler.store import SpanStore

SECOND = 1_000_000_000
MILLISECOND = 1_000_000
NO_THRESHOLDS = LatencyThresholds(0.99, 1, {}, {})
JSON_HEADERS = {"content-type": "application/json"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def settings():
    return GatewaySettings(
        decision_wait_seconds=5.0,
        tick_seconds=0.01,
        baseline_rate=1e-9,
        database_path=":memory:",
    )


@pytest.fixture
def client(settings, clock):
    with TestClient(create_app(settings, NO_THRESHOLDS, clock)) as running:
        yield running


def failing_trace(make_span, trace_id, duration_ms=10):
    start = BASE_START_NANO
    return [
        make_span(
            trace_id=trace_id,
            span_id="1" * 16,
            start_unix_nano=start,
            end_unix_nano=start + duration_ms * MILLISECOND,
        ),
        make_span(
            trace_id=trace_id,
            span_id="2" * 16,
            parent_span_id="1" * 16,
            service="database",
            start_unix_nano=start + MILLISECOND,
            end_unix_nano=start + 2 * MILLISECOND,
            status_code=StatusCode.ERROR,
        ),
    ]


def post_spans(client, spans):
    return client.post(
        "/v1/traces", content=json.dumps(build_export_request(spans)), headers=JSON_HEADERS
    )


def metric(client, name):
    for line in client.get("/metrics").text.splitlines():
        if line.startswith(name + " "):
            return float(line.split()[1])
    raise AssertionError(f"metric {name} not found")


def test_health_endpoint_answers(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_otlp_request_is_accepted_and_buffered(client, make_span):
    response = post_spans(client, failing_trace(make_span, "e" * 32))
    assert response.status_code == 200
    assert response.json() == {}
    assert metric(client, "tailsampler_spans_received_total") == 2
    assert metric(client, "tailsampler_buffered_traces") == 1
    assert metric(client, "tailsampler_stored_spans") == 0


def test_kept_trace_is_readable_after_the_decision_wait(client, clock, make_span):
    post_spans(client, failing_trace(make_span, "e" * 32))
    clock.now = 6 * SECOND
    wait_until(lambda: client.get("/v1/traces/" + "e" * 32).status_code == 200)
    body = client.get("/v1/traces/" + "e" * 32).json()
    assert (body["reason"], body["weight"], len(body["spans"])) == ("error", 1.0, 2)
    assert metric(client, "tailsampler_traces_kept_total") == 1
    assert metric(client, "tailsampler_stored_spans") == 2
    assert (
        'tailsampler_traces_kept_by_reason_total{reason="error"} 1' in client.get("/metrics").text
    )


def test_dropped_trace_is_not_stored(client, clock, make_span):
    post_spans(client, [make_span(trace_id="f" * 32)])
    clock.now = 6 * SECOND
    wait_until(lambda: metric(client, "tailsampler_traces_dropped_total") == 1)
    assert client.get("/v1/traces/" + "f" * 32).status_code == 404


def test_metrics_follow_the_text_exposition_format(client):
    text = client.get("/metrics").text
    assert "# TYPE tailsampler_spans_received_total counter" in text
    assert "# TYPE tailsampler_buffered_traces gauge" in text
    assert client.get("/metrics").headers["content-type"].startswith("text/plain")


def test_other_content_types_are_refused(client, make_span):
    response = client.post(
        "/v1/traces", content=b"\x00", headers={"content-type": "application/x-protobuf"}
    )
    assert response.status_code == 415
    assert "application/json" in response.json()["error"]


def test_malformed_request_names_the_bad_field(client):
    body = {"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "abc"}]}]}]}
    response = client.post("/v1/traces", content=json.dumps(body), headers=JSON_HEADERS)
    assert response.status_code == 400
    assert response.json()["field"] == "resourceSpans[0].scopeSpans[0].spans[0].traceId"


def test_gzip_bodies_are_accepted(client, make_span):
    raw = json.dumps(build_export_request([make_span()])).encode()
    response = client.post(
        "/v1/traces",
        content=gzip.compress(raw),
        headers={**JSON_HEADERS, "content-encoding": "gzip"},
    )
    assert response.status_code == 200
    assert metric(client, "tailsampler_spans_received_total") == 1


@pytest.mark.parametrize(
    ("encoding", "status"), [("gzip", 400), ("br", 415)], ids=["corrupt-gzip", "unsupported"]
)
def test_bad_content_encoding_is_rejected(client, encoding, status):
    response = client.post(
        "/v1/traces",
        content=b"not compressed",
        headers={**JSON_HEADERS, "content-encoding": encoding},
    )
    assert response.status_code == status


def test_oversized_bodies_are_rejected(client, make_span, monkeypatch):
    monkeypatch.setattr(api, "MAX_BODY_BYTES", 100)
    assert post_spans(client, [make_span()]).status_code == 413
    inflated = gzip.compress(b" " * 1000)
    response = client.post(
        "/v1/traces", content=inflated, headers={**JSON_HEADERS, "content-encoding": "gzip"}
    )
    assert response.status_code == 413


def test_trace_lookup_checks_the_identifier(client):
    assert client.get("/v1/traces/xyz").status_code == 400
    assert client.get("/v1/traces/" + "0" * 32).status_code == 404


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=abc", "limit=500", "min_duration_ms=-1", "min_duration_ms=x"]
)
def test_search_parameters_are_validated(client, query):
    response = client.get(f"/v1/traces?{query}")
    assert response.status_code == 400
    assert "error" in response.json()


def test_search_and_estimates_cover_the_kept_traces(client, clock, make_span):
    for index, duration_ms in enumerate((10, 30, 20)):
        post_spans(client, failing_trace(make_span, f"{index}" * 32, duration_ms))
    clock.now = 6 * SECOND
    wait_until(lambda: metric(client, "tailsampler_traces_kept_total") == 3)
    found = client.get("/v1/traces?errors_only=true").json()["traces"]
    assert [item["duration_ms"] for item in found] == [30, 20, 10]
    assert all(item["failed"] and item["reason"] == "error" for item in found)
    assert client.get("/v1/traces?service=nothing").json() == {"traces": []}
    estimates = client.get("/v1/estimates").json()
    assert estimates["total_estimated_requests"] == 3
    (route,) = estimates["routes"]
    assert (route["service"], route["estimated_failed_requests"], route["stored_traces"]) == (
        "checkout",
        3,
        3,
    )
    assert client.get("/v1/estimates?since=-1").status_code == 400
    far_future = BASE_START_NANO + 10**12
    assert client.get(f"/v1/estimates?since={far_future}").json()["routes"] == []


def test_shutdown_flushes_traces_that_are_still_buffered(tmp_path, clock, make_span):
    path = tmp_path / "spans.sqlite3"
    settings = GatewaySettings(
        decision_wait_seconds=100.0, tick_seconds=0.01, baseline_rate=1e-9, database_path=str(path)
    )
    with TestClient(create_app(settings, NO_THRESHOLDS, clock)) as running:
        post_spans(running, failing_trace(make_span, "e" * 32))
    reopened = SpanStore(path)
    assert len(reopened.trace_spans("e" * 32)) == 2
    reopened.close()


def test_api_reference_lists_every_route_and_metric(settings):
    reference = (REPOSITORY_ROOT / "docs" / "api.md").read_text(encoding="utf-8")
    app = create_app(settings, NO_THRESHOLDS)
    for route in app.routes:
        for method in route.methods - {"HEAD"}:
            assert f"## {method} {route.path}" in reference, (method, route.path)
    exposed = api.render_metrics(app.state.gateway)
    documented = set(re.findall(r"`(tailsampler_[a-z_]+)`", reference))
    assert documented
    for name in documented:
        assert f"# TYPE {name} " in exposed, name
    assert "`MAX_BODY_BYTES`" in reference
    assert f"from 1 to {api.MAX_SEARCH_LIMIT}" in reference
