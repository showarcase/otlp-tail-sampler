import json

import pytest

from tailsampler.benchmark import (
    StageTiming,
    capture_environment,
    collect_kept_batches,
    decode_and_time,
    read_timings,
    summarize_timings,
    time_ingest,
    time_store_insert,
    write_environment,
    write_timings,
)
from tailsampler.otlp import build_export_request
from tailsampler.sampler import HeadSampler, KeepAllSampler

SECOND = 1_000_000_000
MICROSECOND = 1_000


@pytest.fixture
def requests(make_span):
    spans = [
        [make_span(trace_id=f"{index:016x}{2**64 - index:016x}", span_id=f"{index:016x}")]
        for index in range(1, 51)
    ]
    return [(index * SECOND, group) for index, group in enumerate(spans)]


def test_timing_summary_uses_median_total_and_pooled_percentiles():
    repeats = [[100 * MICROSECOND] * 100, [300 * MICROSECOND] * 100, [200 * MICROSECOND] * 100]
    timing = summarize_timings("stage", 1000, repeats)
    assert timing.stage == "stage"
    assert timing.calls == 100
    assert timing.seconds == pytest.approx(0.02)
    assert timing.spans_per_second == 50_000
    assert timing.p50_microseconds == 200.0
    assert timing.p99_microseconds == 300.0


def test_timing_summary_needs_timed_calls():
    with pytest.raises(ValueError, match="at least one timed call"):
        summarize_timings("stage", 0, [])
    with pytest.raises(ValueError, match="at least one timed call"):
        summarize_timings("stage", 0, [[]])


def test_decoding_returns_the_spans_and_a_timing(make_span):
    spans = [make_span(span_id=f"{index:016x}") for index in range(1, 4)]
    body = json.dumps(build_export_request(spans)).encode()
    decoded, timing = decode_and_time([body, body], repeats=2)
    assert decoded == [spans, spans]
    assert (timing.stage, timing.calls, timing.spans) == ("decode", 2, 6)
    assert timing.seconds > 0


def test_ingest_timing_counts_every_request_and_span(requests):
    timing = time_ingest("ingest_keep_all", KeepAllSampler, requests, repeats=2)
    assert (timing.calls, timing.spans) == (50, 50)
    assert timing.spans_per_second > 0
    assert timing.p99_microseconds >= timing.p50_microseconds


def test_kept_batches_leave_out_calls_that_kept_nothing(requests):
    everything = collect_kept_batches(KeepAllSampler, requests)
    assert len(everything) == 50
    nothing = collect_kept_batches(lambda: HeadSampler(1e-12), requests)
    assert nothing == []


def test_store_timing_covers_the_kept_spans(requests):
    batches = collect_kept_batches(KeepAllSampler, requests)
    timing = time_store_insert("store_insert", batches, repeats=1)
    assert (timing.calls, timing.spans) == (50, 50)
    assert timing.seconds > 0


def test_timings_survive_a_csv_round_trip(tmp_path):
    timings = [
        StageTiming("decode", 10, 100, 1.5, 66, 12.25, 99.5),
        StageTiming("ingest_tail", 10, 100, 0.25, 400, 1.5, 2.75),
    ]
    path = tmp_path / "nested" / "benchmarks.csv"
    write_timings(path, timings)
    assert path.read_text(encoding="utf-8").splitlines()[0] == (
        "stage,calls,spans,seconds,spans_per_second,p50_microseconds,p99_microseconds"
    )
    assert read_timings(path) == timings


def test_environment_reports_runtime_and_package_versions():
    environment = capture_environment()
    assert environment["python"].count(".") == 2
    assert environment["cpu_count"] >= 1
    assert set(environment["packages"]) == {"starlette", "uvicorn"}
    assert all(environment["packages"].values())
    assert environment["sqlite"]
    assert environment["memory_gib"] is None or environment["memory_gib"] > 0


def test_environment_file_is_written_as_sorted_json(tmp_path):
    path = tmp_path / "results" / "environment.json"
    write_environment(path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == capture_environment()
    assert list(loaded) == sorted(loaded)
