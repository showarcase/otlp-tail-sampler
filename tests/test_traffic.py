import json
import statistics
from dataclasses import replace

import pytest

from conftest import TINY_SIMULATION
from tailsampler.cli import main
from tailsampler.fleet import Incident, TrafficPlan, load_simulation
from tailsampler.otlp import parse_export_request
from tailsampler.traffic import (
    NANOS_PER_SECOND,
    generate_traffic,
    read_requests,
    read_truths,
    write_traffic,
)

SHORT_PLAN = TrafficPlan(seed=3, duration_seconds=20, requests_per_second=40.0, incidents=False)


@pytest.fixture(scope="module")
def short_traffic(simulation):
    return generate_traffic(simulation.fleet, SHORT_PLAN)


def test_same_seed_gives_identical_traffic(simulation, short_traffic):
    again = generate_traffic(simulation.fleet, SHORT_PLAN)
    assert again.truths == short_traffic.truths
    assert again.batches == short_traffic.batches


def test_different_seed_gives_different_traffic(simulation, short_traffic):
    other = generate_traffic(simulation.fleet, replace(SHORT_PLAN, seed=4))
    assert other.truths != short_traffic.truths


def test_every_span_is_exported_exactly_once(short_traffic):
    exported = [span for batch in short_traffic.batches for span in batch.spans]
    assert len(exported) == sum(truth.span_count for truth in short_traffic.truths)
    assert len({(span.trace_id, span.span_id) for span in exported}) == len(exported)


def test_each_trace_has_one_root_and_consistent_parents(short_traffic):
    spans_by_trace: dict[str, list] = {}
    for batch in short_traffic.batches:
        for span in batch.spans:
            spans_by_trace.setdefault(span.trace_id, []).append(span)
    assert len(spans_by_trace) == len(short_traffic.truths)
    for trace_spans in spans_by_trace.values():
        by_id = {span.span_id: span for span in trace_spans}
        assert sum(span.is_root for span in trace_spans) == 1
        for span in trace_spans:
            if span.is_root:
                continue
            parent = by_id[span.parent_span_id]
            assert span.start_unix_nano >= parent.start_unix_nano
            runs_in_background = span.service == "notifications" and parent.service == "frontend"
            if not runs_in_background:
                assert span.end_unix_nano <= parent.end_unix_nano


def test_truth_matches_the_spans(short_traffic):
    spans_by_trace: dict[str, list] = {}
    for batch in short_traffic.batches:
        for span in batch.spans:
            spans_by_trace.setdefault(span.trace_id, []).append(span)
    for truth in short_traffic.truths:
        trace_spans = spans_by_trace[truth.trace_id]
        root = next(span for span in trace_spans if span.is_root)
        assert truth.span_count == len(trace_spans)
        assert truth.duration_nano == root.duration_nano
        assert truth.error == any(span.is_error for span in trace_spans)
        assert truth.rare_path == any(span.service == "fraud" for span in trace_spans)
        assert truth.incident == ""


def test_traffic_arrives_at_about_the_planned_rate(short_traffic):
    expected = SHORT_PLAN.requests_per_second * SHORT_PLAN.duration_seconds
    assert abs(len(short_traffic.truths) - expected) < 5 * expected**0.5


def test_batches_hold_one_service_and_follow_the_exporter_interval(simulation, short_traffic):
    sent_times = [batch.sent_unix_nano for batch in short_traffic.batches]
    assert sent_times == sorted(sent_times)
    fleet = simulation.fleet
    delay_high = fleet.network_delay_ms[1] * 1_000_000
    for batch in short_traffic.batches:
        interval = fleet.export_interval_nano(batch.service)
        for span in batch.spans:
            assert span.service == batch.service
            lag = batch.sent_unix_nano - span.end_unix_nano
            assert 0 < lag <= interval + delay_high


def test_slow_exporter_spreads_a_trace_over_several_seconds(short_traffic):
    lags = [
        batch.sent_unix_nano - span.end_unix_nano
        for batch in short_traffic.batches
        if batch.service == "notifications"
        for span in batch.spans
    ]
    assert max(lags) > 2 * NANOS_PER_SECOND


def test_latency_incident_slows_only_affected_traces(simulation):
    slow_payments = Incident(
        name="slow-payments",
        kind="latency",
        operation="payments.charge",
        start_second=0,
        end_second=60,
        probability=1.0,
        added_median_ms=300.0,
        added_sigma=0.2,
    )
    fleet = replace(simulation.fleet, incidents=(slow_payments,))
    plan = TrafficPlan(seed=5, duration_seconds=60, requests_per_second=40.0, incidents=True)
    baseline = generate_traffic(fleet, replace(plan, incidents=False))
    affected = generate_traffic(fleet, plan)
    checkout = "frontend.checkout"
    normal_ms = statistics.median(
        t.duration_nano for t in baseline.truths if t.root_operation == checkout
    )
    slowed = [t for t in affected.truths if t.incident == "slow-payments"]
    assert slowed
    assert all(t.root_operation == checkout for t in slowed)
    assert statistics.median(t.duration_nano for t in slowed) > normal_ms + 200_000_000
    assert not any(t.incident for t in baseline.truths)


def test_error_incident_marks_every_checkout_in_its_window(simulation):
    burst = Incident(
        name="reserve-failures",
        kind="errors",
        operation="inventory.reserve",
        start_second=10,
        end_second=20,
        probability=1.0,
    )
    fleet = replace(simulation.fleet, incidents=(burst,))
    plan = TrafficPlan(seed=6, duration_seconds=30, requests_per_second=40.0, incidents=True)
    traffic = generate_traffic(fleet, plan)
    affected = [t for t in traffic.truths if t.incident]
    assert affected
    for truth in affected:
        offset = (truth.start_unix_nano - fleet.start_unix_nano) / NANOS_PER_SECOND
        assert 10 <= offset < 20
        assert truth.error
        assert truth.root_operation == "frontend.checkout"
    in_window_checkouts = [
        t
        for t in traffic.truths
        if t.root_operation == "frontend.checkout"
        and 10 <= (t.start_unix_nano - fleet.start_unix_nano) / NANOS_PER_SECOND < 20
    ]
    assert len(affected) == len(in_window_checkouts)


def test_written_files_round_trip(simulation, short_traffic, tmp_path):
    requests_path = tmp_path / "short.requests"
    truth_path = tmp_path / "short.truth.jsonl"
    summary = write_traffic(short_traffic, SHORT_PLAN, requests_path, truth_path)
    lines = list(read_requests(requests_path))
    assert [sent for sent, _ in lines] == [b.sent_unix_nano for b in short_traffic.batches]
    for (_, body), batch in zip(lines, short_traffic.batches, strict=True):
        assert parse_export_request(json.loads(body)) == list(batch.spans)
    assert read_truths(truth_path) == {t.trace_id: t for t in short_traffic.truths}
    assert summary.traces == len(short_traffic.truths)
    assert summary.spans == sum(t.span_count for t in short_traffic.truths)
    assert summary.requests == len(short_traffic.batches)
    assert summary.request_bytes == requests_path.stat().st_size


def test_generate_command_writes_both_datasets_and_a_manifest(tiny_simulation_path, tmp_path):
    output = tmp_path / "generated"
    assert main(["generate", "--config", str(tiny_simulation_path), "--output", str(output)]) == 0
    manifest = json.loads((output / "manifest.json").read_text())
    assert set(manifest) == {"calibration", "evaluation"}
    assert manifest["calibration"]["incident_traces"] == {}
    assert manifest["evaluation"]["incident_traces"]["slow-store"] > 0
    for name in ("calibration", "evaluation"):
        assert (output / f"{name}.requests").stat().st_size == manifest[name]["request_bytes"]
        assert len(read_truths(output / f"{name}.truth.jsonl")) == manifest[name]["traces"]


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('{ operation = "store.read" }', '{ operation = "missing.op" }', "unknown operation"),
        (
            "error_rate = 0.05",
            'error_rate = 0.05\ncalls = [{ operation = "web.index" }]',
            "cycle",
        ),
        (
            '{ operation = "store.read" }',
            '{ operation = "store.read", probability = 1.5 }',
            "probability",
        ),
        ("entrypoint_weight = 1.0", "entrypoint_weight = 0.0", "entrypoint"),
        ('operation = "store.read"\nstart_second', 'operation = "nope"\nstart_second', "unknown"),
        ("start_second = 1\nend_second = 4", "start_second = 4\nend_second = 1", "window"),
        ('span_name = "read"', 'span_name = "read"\ncolour = "x"', "colour"),
        ('name = "store.read"\nservice', 'name = "web.index"\nservice', "duplicate"),
        ("duration_seconds = 5\nrequests_per_second = 20.0\nincidents = false", "", "calibration"),
    ],
)
def test_invalid_simulation_files_are_rejected(old, new, message, tmp_path):
    assert old in TINY_SIMULATION
    path = tmp_path / "broken.toml"
    path.write_text(TINY_SIMULATION.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_simulation(path)


def test_shipped_simulation_has_incidents_only_in_the_evaluation_plan(simulation):
    assert simulation.evaluation.incidents
    assert not simulation.calibration.incidents
    assert len(simulation.fleet.incidents) == 3
