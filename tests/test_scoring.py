import pytest

from tailsampler.model import KeptTrace
from tailsampler.scoring import (
    categorize,
    complete_trace_fraction,
    recall_by_category,
    route_accuracy,
    span_counts_by_trace,
    summarize_accuracy,
)
from tailsampler.store import RouteEstimate
from tailsampler.traffic import TraceTruth

MILLISECOND = 1_000_000


def truth(trace_id, duration_ms=10, span_count=3, error=False, incident="", rare_path=False):
    return TraceTruth(
        trace_id=trace_id,
        root_operation="web.index",
        start_unix_nano=0,
        duration_nano=duration_ms * MILLISECOND,
        span_count=span_count,
        error=error,
        incident=incident,
        rare_path=rare_path,
    )


@pytest.fixture
def truths():
    items = {f"t{index}": truth(f"t{index}", duration_ms=index) for index in range(1, 101)}
    items["t1"] = truth("t1", duration_ms=1, error=True)
    items["t2"] = truth("t2", duration_ms=2, incident="slow-store")
    items["t3"] = truth("t3", duration_ms=3, rare_path=True)
    return items


def test_categories_follow_the_truth_labels(truths):
    categories = categorize(truths, slow_quantile=0.99)
    assert categories["error"] == {"t1"}
    assert categories["slow"] == {"t100"}
    assert categories["rare_path"] == {"t3"}
    assert categories["slow-store"] == {"t2"}
    assert len(categories["typical"]) == 96
    assert not categories["typical"] & {"t1", "t2", "t3", "t100"}
    assert list(categories) == ["error", "slow", "rare_path", "slow-store", "typical"]


def test_categories_without_members_are_left_out():
    categories = categorize({"a": truth("a"), "b": truth("b", duration_ms=20)}, slow_quantile=0.5)
    assert list(categories) == ["slow", "typical"]


def test_recall_is_the_share_of_each_category_that_was_kept(truths):
    categories = categorize(truths, slow_quantile=0.99)
    recall = recall_by_category(categories, {"t1", "t100", "t50", "t51", "unrelated"})
    assert recall["error"] == 1.0
    assert recall["slow"] == 1.0
    assert recall["rare_path"] == 0.0
    assert recall["typical"] == pytest.approx(2 / 96)


def test_span_counts_add_late_spans_to_the_trace(make_span):
    first = KeptTrace("a", "error", 1.0, (make_span(), make_span()))
    late = KeptTrace("a", "error", 1.0, (make_span(),), late=True)
    other = KeptTrace("b", "baseline", 20.0, (make_span(),))
    assert span_counts_by_trace([first, late, other]) == {"a": 3, "b": 1}


def test_completeness_compares_span_counts_with_the_truth(truths):
    assert complete_trace_fraction({"t10": 3, "t11": 2, "t12": 3, "t13": 1}, truths) == 0.5
    assert complete_trace_fraction({}, truths) == 0.0


def test_route_accuracy_compares_estimates_with_true_counts():
    truths = {
        "a": truth("a", error=True),
        "b": truth("b"),
        "c": truth("c"),
        "d": truth("d"),
    }
    routes = {"web.index": ("web", "GET /")}
    estimates = [RouteEstimate("web", "GET /", 5.0, 2.0, 2)]
    (accuracy,) = route_accuracy(estimates, truths, routes)
    assert (accuracy.true_requests, accuracy.estimated_requests) == (4, 5.0)
    assert accuracy.request_error_percent == pytest.approx(25.0)
    assert (accuracy.true_failed_requests, accuracy.estimated_failed_requests) == (1, 2.0)
    assert accuracy.failed_error_percent == pytest.approx(100.0)


def test_route_without_estimate_counts_as_fully_missed():
    truths = {"a": truth("a"), "b": truth("b")}
    (accuracy,) = route_accuracy([], truths, {"web.index": ("web", "GET /")})
    assert accuracy.estimated_requests == 0.0
    assert accuracy.request_error_percent == pytest.approx(-100.0)
    assert accuracy.failed_error_percent is None


def test_accuracy_summary_uses_absolute_errors():
    truths = {f"a{index}": truth(f"a{index}") for index in range(20)}
    truths["a0"] = truth("a0", error=True)
    routes = {"web.index": ("web", "GET /")}
    estimates = [RouteEstimate("web", "GET /", 25.0, 3.0, 4)]
    summary = summarize_accuracy(route_accuracy(estimates, truths, routes))
    assert summary["median_abs_request_error_percent"] == pytest.approx(25.0)
    assert summary["max_abs_request_error_percent"] == pytest.approx(25.0)
    assert summary["max_abs_failed_error_percent"] == pytest.approx(200.0)
    assert (summary["true_failed_requests"], summary["estimated_failed_requests"]) == (1, 3.0)
    assert len(summary["routes"]) == 1
