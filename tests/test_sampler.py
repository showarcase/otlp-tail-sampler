import pytest

from tailsampler.fleet import TrafficPlan
from tailsampler.model import StatusCode
from tailsampler.policies import (
    REASON_BASELINE,
    REASON_ERROR,
    REASON_HEAD,
    REASON_KEEP_ALL,
    SamplingPolicy,
    baseline_selects,
    fit_latency_thresholds,
)
from tailsampler.sampler import HeadSampler, KeepAllSampler, TailSampler
from tailsampler.traffic import generate_traffic

SECOND = 1_000_000_000
ERROR_TRACE = "e" * 32
QUIET_TRACE = "f" * 32
SELECTED_TRACE = "0" * 31 + "1"


def make_sampler(rate=1e-12, wait_seconds=5, capacity=1000, cache_size=1000, thresholds=None):
    policy = SamplingPolicy(rate, thresholds or {})
    return TailSampler(policy, wait_seconds * SECOND, capacity, cache_size)


def replay(sampler, traffic):
    kept = []
    for batch in traffic.batches:
        kept.extend(sampler.ingest(batch.spans, batch.sent_unix_nano))
    kept.extend(sampler.flush())
    return kept


def test_spans_are_held_until_the_decision_wait_has_passed(make_span):
    sampler = make_sampler(wait_seconds=5)
    failing = make_span(trace_id=ERROR_TRACE, status_code=StatusCode.ERROR)
    assert sampler.ingest([failing], 0) == []
    assert sampler.buffered_traces == 1
    assert sampler.advance(5 * SECOND - 1) == []
    (kept,) = sampler.advance(5 * SECOND)
    assert (kept.trace_id, kept.reason, kept.weight, kept.late) == (
        ERROR_TRACE,
        REASON_ERROR,
        1.0,
        False,
    )
    assert kept.spans == (failing,)
    assert sampler.buffered_traces == 0


def test_spans_from_separate_requests_are_decided_together(make_span):
    sampler = make_sampler(wait_seconds=5)
    sampler.ingest([make_span(trace_id=ERROR_TRACE, span_id="1" * 16)], 0)
    sampler.ingest(
        [make_span(trace_id=ERROR_TRACE, span_id="2" * 16, status_code=StatusCode.ERROR)],
        2 * SECOND,
    )
    (kept,) = sampler.advance(5 * SECOND)
    assert [span.span_id for span in kept.spans] == ["1" * 16, "2" * 16]
    assert kept.reason == REASON_ERROR


def test_unremarkable_trace_is_dropped_and_counted(make_span):
    sampler = make_sampler()
    sampler.ingest([make_span(trace_id=QUIET_TRACE), make_span(trace_id=QUIET_TRACE)], 0)
    assert sampler.advance(10 * SECOND) == []
    assert sampler.counters.traces_dropped == 1
    assert sampler.counters.spans_dropped == 2
    assert sampler.counters.traces_kept == 0


def test_baseline_trace_keeps_the_inverse_rate_weight(make_span):
    sampler = make_sampler(rate=0.5)
    sampler.ingest([make_span(trace_id=SELECTED_TRACE)], 0)
    (kept,) = sampler.advance(5 * SECOND)
    assert (kept.reason, kept.weight) == (REASON_BASELINE, 2.0)
    assert sampler.counters.kept_by_reason == {REASON_BASELINE: 1}


def test_span_arriving_at_the_deadline_is_late(make_span):
    sampler = make_sampler(wait_seconds=5)
    sampler.ingest([make_span(trace_id=ERROR_TRACE, status_code=StatusCode.ERROR)], 0)
    straggler = make_span(trace_id=ERROR_TRACE, span_id="2" * 16)
    (kept_main, kept_late) = sampler.ingest([straggler], 5 * SECOND)
    assert not kept_main.late
    assert kept_late.late
    assert kept_late.spans == (straggler,)
    assert (kept_late.reason, kept_late.weight) == (REASON_ERROR, 1.0)
    assert sampler.counters.late_spans_kept == 1


def test_late_span_of_a_dropped_trace_is_dropped(make_span):
    sampler = make_sampler(wait_seconds=1)
    sampler.ingest([make_span(trace_id=QUIET_TRACE)], 0)
    kept = sampler.ingest([make_span(trace_id=QUIET_TRACE, span_id="2" * 16)], 2 * SECOND)
    assert kept == []
    assert sampler.counters.late_spans_dropped == 1
    assert sampler.counters.spans_dropped == 2
    assert sampler.buffered_traces == 0


def test_late_error_span_cannot_rescue_a_trace_dropped_earlier(make_span):
    sampler = make_sampler(wait_seconds=1)
    sampler.ingest([make_span(trace_id=QUIET_TRACE)], 0)
    failing = make_span(trace_id=QUIET_TRACE, span_id="2" * 16, status_code=StatusCode.ERROR)
    assert sampler.ingest([failing], 3 * SECOND) == []


def test_full_buffer_forces_a_decision_on_the_oldest_trace(make_span):
    sampler = make_sampler(wait_seconds=100, capacity=2)
    failing = make_span(trace_id=ERROR_TRACE, status_code=StatusCode.ERROR)
    assert sampler.ingest([failing], 0) == []
    assert sampler.ingest([make_span(trace_id="1" * 32)], SECOND) == []
    (kept,) = sampler.ingest([make_span(trace_id="2" * 32)], 2 * SECOND)
    assert kept.trace_id == ERROR_TRACE
    assert sampler.counters.forced_decisions == 1
    assert sampler.counters.peak_buffered_traces == 2
    assert sampler.buffered_traces == 2


def test_flush_decides_everything_still_buffered(make_span):
    sampler = make_sampler(wait_seconds=100)
    sampler.ingest([make_span(trace_id=ERROR_TRACE, status_code=StatusCode.ERROR)], 0)
    sampler.ingest([make_span(trace_id=QUIET_TRACE)], 0)
    (kept,) = sampler.flush()
    assert kept.trace_id == ERROR_TRACE
    assert sampler.buffered_traces == 0
    assert sampler.counters.traces_dropped == 1


def test_forgotten_decision_makes_a_very_late_span_start_a_new_trace(make_span):
    sampler = make_sampler(wait_seconds=1, cache_size=1)
    sampler.ingest([make_span(trace_id=QUIET_TRACE)], 0)
    sampler.ingest([make_span(trace_id="1" * 32)], 2 * SECOND)
    sampler.advance(4 * SECOND)
    sampler.ingest([make_span(trace_id=QUIET_TRACE, span_id="2" * 16)], 5 * SECOND)
    assert sampler.buffered_traces == 1
    assert sampler.counters.late_spans_dropped == 0


def test_head_sampler_decides_per_trace_id_and_weights_by_rate(make_span):
    sampler = HeadSampler(rate=0.5)
    spans = [
        make_span(trace_id=SELECTED_TRACE, span_id="1" * 16),
        make_span(trace_id=SELECTED_TRACE, span_id="2" * 16),
        make_span(trace_id=QUIET_TRACE),
    ]
    assert not baseline_selects(QUIET_TRACE, 0.5)
    (kept,) = sampler.ingest(spans, 0)
    assert (kept.trace_id, kept.reason, kept.weight) == (SELECTED_TRACE, REASON_HEAD, 2.0)
    assert len(kept.spans) == 2
    assert sampler.counters.spans_received == 3
    assert sampler.counters.spans_kept == 2
    assert sampler.counters.spans_dropped == 1
    assert sampler.advance(SECOND) == []
    assert sampler.flush() == []


def test_head_sampler_rejects_a_rate_outside_zero_to_one():
    with pytest.raises(ValueError):
        HeadSampler(rate=0.0)


def test_keep_all_sampler_passes_every_span_through(make_span):
    sampler = KeepAllSampler()
    spans = [make_span(trace_id=QUIET_TRACE), make_span(trace_id=ERROR_TRACE)]
    kept = sampler.ingest(spans, 0)
    assert {item.trace_id for item in kept} == {QUIET_TRACE, ERROR_TRACE}
    assert all(item.reason == REASON_KEEP_ALL and item.weight == 1.0 for item in kept)
    assert sampler.counters.spans_kept == 2


@pytest.fixture(scope="module")
def thresholds(simulation):
    plan = TrafficPlan(seed=21, duration_seconds=60, requests_per_second=60.0, incidents=False)
    calibration = generate_traffic(simulation.fleet, plan)
    roots = [span for batch in calibration.batches for span in batch.spans if span.is_root]
    return fit_latency_thresholds(roots, quantile=0.99, min_samples=100)


@pytest.fixture(scope="module")
def evaluation_traffic(simulation):
    plan = TrafficPlan(seed=22, duration_seconds=60, requests_per_second=60.0, incidents=False)
    return generate_traffic(simulation.fleet, plan)


def tail_sampler_for(thresholds, wait_seconds=10, capacity=100_000):
    policy = SamplingPolicy(0.1, thresholds.thresholds_nano)
    return TailSampler(policy, wait_seconds * SECOND, capacity, 1_000_000)


def test_replay_keeps_every_error_trace_and_every_slow_root(thresholds, evaluation_traffic):
    sampler = tail_sampler_for(thresholds)
    kept = replay(sampler, evaluation_traffic)
    kept_ids = {item.trace_id for item in kept}
    truths = {truth.trace_id: truth for truth in evaluation_traffic.truths}
    error_ids = {trace_id for trace_id, truth in truths.items() if truth.error}
    assert error_ids
    assert error_ids <= kept_ids
    roots = {
        span.trace_id: span
        for batch in evaluation_traffic.batches
        for span in batch.spans
        if span.is_root
    }
    slow_ids = {
        trace_id
        for trace_id, root in roots.items()
        if root.duration_nano
        >= thresholds.thresholds_nano.get(f"{root.service} {root.name}", 1 << 62)
    }
    assert slow_ids
    assert slow_ids <= kept_ids


def test_replay_stores_every_span_of_each_kept_trace(thresholds, evaluation_traffic):
    kept = replay(tail_sampler_for(thresholds), evaluation_traffic)
    spans_by_trace: dict[str, int] = {}
    for item in kept:
        spans_by_trace[item.trace_id] = spans_by_trace.get(item.trace_id, 0) + len(item.spans)
    expected = {truth.trace_id: truth.span_count for truth in evaluation_traffic.truths}
    assert all(count == expected[trace_id] for trace_id, count in spans_by_trace.items())


def test_replay_counters_add_up(thresholds, evaluation_traffic):
    sampler = tail_sampler_for(thresholds)
    replay(sampler, evaluation_traffic)
    counters = sampler.counters
    total_spans = sum(truth.span_count for truth in evaluation_traffic.truths)
    assert counters.spans_received == total_spans
    assert counters.spans_kept + counters.spans_dropped == total_spans
    assert counters.traces_kept + counters.traces_dropped == len(evaluation_traffic.truths)
    assert sum(counters.kept_by_reason.values()) == counters.traces_kept
    assert counters.forced_decisions == 0


def test_weighted_root_count_estimates_the_number_of_traces(thresholds, evaluation_traffic):
    kept = replay(tail_sampler_for(thresholds), evaluation_traffic)
    estimated = sum(item.weight for item in kept for span in item.spans if span.is_root)
    actual = len(evaluation_traffic.truths)
    assert abs(estimated - actual) < 0.2 * actual


def test_a_short_wait_splits_traces_and_misses_late_exporters(thresholds, evaluation_traffic):
    patient = tail_sampler_for(thresholds, wait_seconds=10)
    hasty = tail_sampler_for(thresholds, wait_seconds=1)
    replay(patient, evaluation_traffic)
    replay(hasty, evaluation_traffic)
    assert patient.counters.late_spans_kept + patient.counters.late_spans_dropped == 0
    assert hasty.counters.late_spans_kept + hasty.counters.late_spans_dropped > 0


def test_a_small_buffer_forces_early_decisions_but_stays_within_its_cap(
    thresholds, evaluation_traffic
):
    sampler = tail_sampler_for(thresholds, capacity=50)
    replay(sampler, evaluation_traffic)
    assert sampler.counters.forced_decisions > 0
    assert sampler.counters.peak_buffered_traces == 50
    assert sampler.counters.spans_kept + sampler.counters.spans_dropped == (
        sampler.counters.spans_received
    )
