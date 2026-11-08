import json

import pytest

from conftest import REPOSITORY_ROOT
from tailsampler.benchmark import read_timings
from tailsampler.config import GatewaySettings
from tailsampler.evaluation import (
    EvaluationSettings,
    evaluate_samplers,
    load_evaluation_settings,
    measure_sampler,
    route_names,
)
from tailsampler.fleet import load_simulation
from tailsampler.otlp import decode_export_request
from tailsampler.policies import fit_latency_thresholds, load_thresholds
from tailsampler.sampler import HeadSampler, KeepAllSampler, replay_in_event_time
from tailsampler.scoring import categorize
from tailsampler.traffic import generate_traffic, read_requests, read_truths, write_traffic


@pytest.fixture(scope="module")
def tiny_scores(tiny_results):
    return json.loads((tiny_results / "metrics.json").read_text(encoding="utf-8"))


def test_shipped_evaluation_settings_load():
    settings = load_evaluation_settings(REPOSITORY_ROOT / "configs" / "simulation.toml")
    assert settings.decision_wait_seconds == tuple(sorted(settings.decision_wait_seconds))
    assert all(0 < rate < 1 for rate in settings.baseline_rates)


def test_settings_file_overrides_defaults(tmp_path):
    path = tmp_path / "evaluation.toml"
    path.write_text("[evaluation]\ndecision_wait_seconds = [1, 3]\nbenchmark_repeats = 2\n")
    settings = load_evaluation_settings(path)
    assert settings.decision_wait_seconds == (1.0, 3.0)
    assert settings.benchmark_repeats == 2
    assert settings.slow_quantile == EvaluationSettings().slow_quantile


def test_unknown_setting_is_rejected(tmp_path):
    path = tmp_path / "evaluation.toml"
    path.write_text("[evaluation]\nslow_percentile = 0.99\n")
    with pytest.raises(ValueError, match="slow_percentile"):
        load_evaluation_settings(path)


@pytest.mark.parametrize(
    "overrides",
    [
        {"slow_quantile": 1.0},
        {"decision_wait_seconds": ()},
        {"decision_wait_seconds": (0.0,)},
        {"baseline_rates": ()},
        {"baseline_rates": (1.0,)},
        {"benchmark_repeats": 0},
    ],
)
def test_invalid_settings_are_rejected(overrides):
    with pytest.raises(ValueError):
        EvaluationSettings(**overrides)


def test_pipeline_writes_the_three_result_files(tiny_results):
    assert sorted(path.name for path in tiny_results.iterdir()) == [
        "benchmarks.csv",
        "environment.json",
        "metrics.json",
        "thresholds.json",
    ]


def test_keep_all_is_the_reference(tiny_scores):
    keep_all = tiny_scores["samplers"]["keep_all"]
    assert keep_all["span_fraction"] == 1.0
    assert set(keep_all["recall"].values()) == {1.0}
    assert tiny_scores["estimates"]["keep_all"]["max_abs_request_error_percent"] == 0.0


def test_tail_keeps_every_error_and_the_incident_traces(tiny_scores):
    tail = tiny_scores["samplers"]["tail"]
    assert tail["recall"]["error"] == 1.0
    assert tail["recall"]["slow-store"] >= tail["recall"]["typical"]
    assert tail["spans_kept"] < tail["spans_received"]
    assert sum(tail["kept_by_reason"].values()) == tail["traces_kept"]
    assert (
        tiny_scores["estimates"]["tail"]["estimated_failed_requests"]
        == (tiny_scores["estimates"]["tail"]["true_failed_requests"])
    )


def test_head_sampler_is_matched_to_the_tail_budget(tiny_scores):
    policy = tiny_scores["policy"]
    tail = tiny_scores["samplers"]["tail"]
    head = tiny_scores["samplers"]["head"]
    assert policy["head_rate"] == tail["span_fraction"]
    assert abs(head["span_fraction"] - policy["head_rate"]) < 0.15
    assert set(head["kept_by_reason"]) == {"head"}


def test_sweeps_follow_the_settings_file(tiny_scores):
    waits = [point["decision_wait_seconds"] for point in tiny_scores["sweeps"]["decision_wait"]]
    rates = [point["baseline_rate"] for point in tiny_scores["sweeps"]["baseline_rate"]]
    assert waits == [1.0, 2.0]
    assert rates == [0.1, 0.5]
    spans_kept = [
        point["tail"]["span_fraction"] for point in tiny_scores["sweeps"]["baseline_rate"]
    ]
    assert spans_kept == sorted(spans_kept)


def test_metrics_describe_the_generated_dataset(tiny_scores):
    evaluation = tiny_scores["dataset"]["evaluation"]
    assert 0 < tiny_scores["categories"]["slow-store"] <= evaluation["traces"]
    assert tiny_scores["samplers"]["keep_all"]["traces_kept"] == evaluation["traces"]
    assert tiny_scores["samplers"]["keep_all"]["spans_kept"] == evaluation["spans"]


def test_benchmark_file_lists_every_stage(tiny_results):
    lines = (tiny_results / "benchmarks.csv").read_text(encoding="utf-8").splitlines()
    stages = [line.split(",")[0] for line in lines[1:]]
    assert stages == [
        "decode",
        "ingest_tail",
        "ingest_head",
        "ingest_keep_all",
        "store_insert_tail",
    ]


@pytest.fixture
def tiny_inputs(tiny_simulation_path, tmp_path):
    simulation = load_simulation(tiny_simulation_path)
    traffic = generate_traffic(simulation.fleet, simulation.evaluation)
    write_traffic(
        traffic, simulation.evaluation, tmp_path / "e.requests", tmp_path / "e.truth.jsonl"
    )
    requests = [
        (sent, decode_export_request(body)) for sent, body in read_requests(tmp_path / "e.requests")
    ]
    return simulation, requests, read_truths(tmp_path / "e.truth.jsonl")


def test_event_time_replay_delivers_every_span(tiny_inputs):
    _, requests, truths = tiny_inputs
    kept = replay_in_event_time(KeepAllSampler(), requests)
    assert sum(len(item.spans) for item in kept) == sum(
        truth.span_count for truth in truths.values()
    )


def test_evaluation_is_deterministic(tiny_inputs):
    simulation, requests, truths = tiny_inputs
    thresholds = fit_latency_thresholds(
        (span for _, spans in requests for span in spans if span.is_root), 0.9, 5
    )
    settings = EvaluationSettings(
        decision_wait_seconds=(1.0,), baseline_rates=(0.2,), benchmark_repeats=1
    )
    gateway = GatewaySettings(decision_wait_seconds=1.0, baseline_rate=0.1)
    first = evaluate_samplers(simulation, gateway, thresholds, settings, requests, truths)
    second = evaluate_samplers(simulation, gateway, thresholds, settings, requests, truths)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_measured_outcome_counts_spans_from_the_kept_traces(tiny_inputs):
    simulation, requests, truths = tiny_inputs
    categories = categorize(truths, 0.9)
    sampler = HeadSampler(0.5)
    kept = replay_in_event_time(sampler, requests)
    outcome = measure_sampler(sampler, kept, truths, categories)
    assert outcome.spans_kept == sum(len(item.spans) for item in kept)
    assert outcome.spans_received == sum(truth.span_count for truth in truths.values())
    assert outcome.traces_kept == len({item.trace_id for item in kept})
    assert outcome.complete_trace_fraction == 1.0
    assert route_names(simulation)["web.index"] == ("web", "GET /")


def test_thresholds_from_the_pipeline_cover_the_tiny_route(tiny_results):
    thresholds = load_thresholds(tiny_results / "thresholds.json")
    assert list(thresholds.thresholds_nano) == ["web GET /"]


@pytest.fixture(scope="module")
def shipped_scores():
    path = REPOSITORY_ROOT / "results" / "metrics.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def shipped_timings():
    path = REPOSITORY_ROOT / "results" / "benchmarks.csv"
    return {item.stage: item for item in read_timings(path)}


def test_documented_recall_findings_hold_in_the_shipped_results(shipped_scores):
    tail = shipped_scores["samplers"]["tail"]
    head = shipped_scores["samplers"]["head"]
    assert tail["recall"]["error"] == 1.0
    assert tail["recall"]["slow"] >= 0.99
    for category in ("error", "slow", "typical"):
        assert abs(head["recall"][category] - head["span_fraction"]) < 0.05
    assert tail["recall"]["rare_path"] < 0.5
    assert tail["recall"]["search-subtle-shift"] < 0.5
    assert tail["recall"]["search-subtle-shift"] > head["recall"]["search-subtle-shift"]
    assert set(tail["kept_by_reason"]) == {"baseline", "error", "latency"}


def test_documented_estimate_findings_hold_in_the_shipped_results(shipped_scores):
    tail = shipped_scores["estimates"]["tail"]
    head = shipped_scores["estimates"]["head"]
    assert tail["estimated_failed_requests"] == tail["true_failed_requests"]
    assert head["estimated_failed_requests"] < head["true_failed_requests"]
    assert tail["median_abs_request_error_percent"] < head["median_abs_request_error_percent"]
    assert tail["max_abs_request_error_percent"] > head["max_abs_request_error_percent"]
    worst = max(tail["routes"], key=lambda route: abs(route["request_error_percent"]))
    assert (worst["service"], worst["name"]) == ("frontend", "POST /checkout")


def test_documented_sweep_findings_hold_in_the_shipped_results(shipped_scores):
    waits = shipped_scores["sweeps"]["decision_wait"]
    assert waits[0]["tail"]["late_spans_dropped"] > 0
    lossless = [item for item in waits if item["tail"]["late_spans_dropped"] == 0]
    assert lossless[0]["decision_wait_seconds"] == shipped_scores["policy"]["decision_wait_seconds"]
    peaks = [item["tail"]["peak_buffered_spans"] for item in waits]
    assert peaks == sorted(peaks)
    rates = shipped_scores["sweeps"]["baseline_rate"]
    assert all(item["tail"]["recall"]["error"] == 1.0 for item in rates)
    assert rates[-1]["head"]["recall"]["error"] < 0.5


def test_documented_benchmark_findings_hold_in_the_shipped_results(shipped_timings):
    throughput = {stage: timing.spans_per_second for stage, timing in shipped_timings.items()}
    assert throughput["decode"] < throughput["ingest_tail"] < throughput["ingest_head"]
    slowest = min(shipped_timings.values(), key=lambda timing: timing.spans_per_second)
    longest_tail = max(shipped_timings.values(), key=lambda timing: timing.p99_microseconds)
    assert slowest.stage == longest_tail.stage == "store_insert_tail"
