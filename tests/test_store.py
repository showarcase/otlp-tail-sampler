import pytest

from conftest import BASE_START_NANO
from tailsampler.model import KeptTrace, StatusCode
from tailsampler.store import SpanStore

MILLISECOND = 1_000_000


@pytest.fixture
def store():
    opened = SpanStore(":memory:")
    yield opened
    opened.close()


def trace_of(make_span, trace_id, duration_ms, service="checkout", failed=False, weight=1.0, at=0):
    start = BASE_START_NANO + at
    root = make_span(
        trace_id=trace_id,
        span_id="1" * 16,
        service=service,
        start_unix_nano=start,
        end_unix_nano=start + duration_ms * MILLISECOND,
    )
    child = make_span(
        trace_id=trace_id,
        span_id="2" * 16,
        parent_span_id="1" * 16,
        service="database",
        start_unix_nano=start + MILLISECOND,
        end_unix_nano=start + 2 * MILLISECOND,
        status_code=StatusCode.ERROR if failed else StatusCode.UNSET,
    )
    reason = "error" if failed else "baseline"
    return KeptTrace(trace_id, reason, weight, (root, child))


def test_spans_round_trip_with_weight_and_reason(store, make_span):
    span = make_span(
        attributes={"http.status_code": 500, "ratio": 0.5, "cached": True, "route": "/x"},
        status_code=StatusCode.ERROR,
        status_message="boom",
    )
    store.insert([KeptTrace(span.trace_id, "error", 1.0, (span,))])
    (stored,) = store.trace_spans(span.trace_id)
    assert stored.span == span
    assert (stored.weight, stored.reason) == (1.0, "error")


def test_inserting_the_same_spans_twice_changes_nothing(store, make_span):
    kept = trace_of(make_span, "a" * 32, 10)
    assert store.insert([kept]) == 2
    assert store.insert([kept]) == 0
    assert store.span_count() == 2


def test_late_spans_join_the_stored_trace(store, make_span):
    store.insert([trace_of(make_span, "a" * 32, 10)])
    straggler = make_span(
        trace_id="a" * 32,
        span_id="3" * 16,
        parent_span_id="1" * 16,
        start_unix_nano=BASE_START_NANO + 5,
    )
    store.insert([KeptTrace("a" * 32, "baseline", 1.0, (straggler,), late=True)])
    spans = store.trace_spans("a" * 32)
    assert [item.span.span_id for item in spans] == ["1" * 16, "3" * 16, "2" * 16]


def test_unknown_trace_has_no_spans(store):
    assert store.trace_spans("0" * 32) == []
    assert store.insert([]) == 0


def test_estimates_add_up_root_weights_per_route(store, make_span):
    store.insert(
        [
            trace_of(make_span, "a" * 32, 10, failed=True, weight=1.0),
            trace_of(make_span, "b" * 32, 10, weight=20.0),
            trace_of(make_span, "c" * 32, 10, service="cart", weight=20.0),
        ]
    )
    by_route = {item.service: item for item in store.estimates()}
    assert by_route["checkout"].estimated_requests == 21.0
    assert by_route["checkout"].estimated_failed_requests == 1.0
    assert by_route["checkout"].stored_traces == 2
    assert by_route["cart"].estimated_requests == 20.0
    assert by_route["cart"].estimated_failed_requests == 0.0


def test_estimates_can_be_limited_to_a_window(store, make_span):
    store.insert(
        [
            trace_of(make_span, "a" * 32, 10, at=0),
            trace_of(make_span, "b" * 32, 10, at=100),
            trace_of(make_span, "c" * 32, 10, at=200),
        ]
    )
    window = store.estimates(BASE_START_NANO + 100, BASE_START_NANO + 200)
    assert [item.estimated_requests for item in window] == [1.0]
    assert store.estimates(BASE_START_NANO + 1000) == []


def test_search_orders_by_duration_and_applies_filters(store, make_span):
    store.insert(
        [
            trace_of(make_span, "a" * 32, 10),
            trace_of(make_span, "b" * 32, 30, failed=True),
            trace_of(make_span, "c" * 32, 20, service="cart"),
        ]
    )
    assert [item.trace_id[0] for item in store.search()] == ["b", "c", "a"]
    assert [item.trace_id[0] for item in store.search(limit=2)] == ["b", "c"]
    assert [item.trace_id[0] for item in store.search(service="cart")] == ["c"]
    assert [item.trace_id[0] for item in store.search(errors_only=True)] == ["b"]
    assert [item.trace_id[0] for item in store.search(min_duration_nano=15 * MILLISECOND)] == [
        "b",
        "c",
    ]
    top = store.search(limit=1)[0]
    assert (top.service, top.duration_nano, top.failed) == ("checkout", 30 * MILLISECOND, True)


def test_file_store_creates_its_folder_and_keeps_rows_after_reopening(tmp_path, make_span):
    path = tmp_path / "nested" / "spans.sqlite3"
    first = SpanStore(path)
    first.insert([trace_of(make_span, "a" * 32, 10)])
    assert first.size_bytes() > 0
    first.close()
    second = SpanStore(path)
    assert second.span_count() == 2
    second.close()
