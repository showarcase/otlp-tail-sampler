import json
import random

import pytest

from tailsampler.cli import main
from tailsampler.model import StatusCode
from tailsampler.policies import (
    REASON_BASELINE,
    REASON_ERROR,
    REASON_LATENCY,
    REASON_NOT_SAMPLED,
    SamplingPolicy,
    baseline_selects,
    decide,
    fit_latency_thresholds,
    load_thresholds,
    quantile_of_sorted,
    route_key,
    write_thresholds,
)

MILLISECOND = 1_000_000


def random_trace_ids(count: int, seed: int = 1) -> list[str]:
    generator = random.Random(seed)
    return [f"{generator.getrandbits(128):032x}" for _ in range(count)]


def test_error_trace_is_kept_with_weight_one(make_span):
    policy = SamplingPolicy(baseline_rate=0.01, thresholds_nano={})
    spans = [make_span(status_code=StatusCode.ERROR), make_span(span_id="2" * 16)]
    decision = decide(policy, spans[0].trace_id, spans)
    assert (decision.keep, decision.reason, decision.weight) == (True, REASON_ERROR, 1.0)


def test_slow_root_is_kept_and_a_fast_one_is_not(make_span):
    start = 2_000_000_000_000_000_000
    slow = make_span(start_unix_nano=start, end_unix_nano=start + 80 * MILLISECOND)
    fast = make_span(start_unix_nano=start, end_unix_nano=start + 79 * MILLISECOND)
    policy = SamplingPolicy(1e-12, {route_key(slow): 80 * MILLISECOND})
    assert decide(policy, "0" * 32, [slow]).reason == REASON_LATENCY
    assert decide(policy, "f" * 32, [fast]).reason == REASON_NOT_SAMPLED


def test_latency_rule_needs_a_root_span_and_a_threshold(make_span):
    child = make_span(parent_span_id="9" * 16, end_unix_nano=10**19)
    unknown_route = make_span(service="other", end_unix_nano=10**19)
    policy = SamplingPolicy(1e-12, {"checkout POST /checkout": 1})
    assert decide(policy, "f" * 32, [child]).keep is False
    assert decide(policy, "f" * 32, [unknown_route]).keep is False


def test_error_rule_wins_over_latency_rule(make_span):
    span = make_span(status_code=StatusCode.ERROR, end_unix_nano=10**19)
    policy = SamplingPolicy(0.5, {route_key(span): 1})
    assert decide(policy, span.trace_id, [span]).reason == REASON_ERROR


def test_baseline_decision_depends_only_on_the_trace_id():
    ids = random_trace_ids(50)
    assert [baseline_selects(item, 0.3) for item in ids] == [baseline_selects(i, 0.3) for i in ids]
    assert baseline_selects("0" * 32, 1e-9)
    assert not baseline_selects("f" * 32, 0.999)
    assert all(baseline_selects(item, 1.0) for item in ids)


def test_baseline_keeps_about_the_configured_fraction():
    ids = random_trace_ids(20_000)
    kept = sum(baseline_selects(item, 0.1) for item in ids)
    assert abs(kept - 2000) < 5 * (20_000 * 0.1 * 0.9) ** 0.5


def test_baseline_weight_is_the_inverse_of_the_rate(make_span):
    policy = SamplingPolicy(baseline_rate=0.25, thresholds_nano={})
    decision = decide(policy, "0" * 32, [make_span(trace_id="0" * 32)])
    assert (decision.reason, decision.weight) == (REASON_BASELINE, 4.0)


def test_weights_estimate_the_number_of_unremarkable_traces(make_span):
    policy = SamplingPolicy(baseline_rate=0.1, thresholds_nano={})
    ids = random_trace_ids(20_000, seed=7)
    estimated = 0.0
    for trace_id in ids:
        decision = decide(policy, trace_id, [make_span(trace_id=trace_id)])
        if decision.keep:
            estimated += decision.weight
    assert abs(estimated - 20_000) < 5 * (20_000 * 0.9 / 0.1) ** 0.5


@pytest.mark.parametrize(
    ("values", "quantile", "expected"),
    [([5], 0.99, 5.0), ([1, 2, 3, 4], 0.0, 1.0), ([1, 2, 3, 4], 1.0, 4.0), ([10, 20], 0.5, 15.0)],
)
def test_quantile_interpolates_between_neighbours(values, quantile, expected):
    assert quantile_of_sorted(values, quantile) == pytest.approx(expected)


def test_quantile_of_nothing_is_rejected():
    with pytest.raises(ValueError):
        quantile_of_sorted([], 0.5)


def test_fitting_uses_the_requested_quantile_and_minimum_sample_count(make_span):
    steady = [make_span(end_unix_nano=1_700_000_000_000_000_000 + n) for n in range(1, 1001)]
    rare = [
        make_span(service="rare", end_unix_nano=1_700_000_000_000_000_000 + n) for n in range(5)
    ]
    fitted = fit_latency_thresholds(steady + rare, quantile=0.99, min_samples=100)
    assert fitted.thresholds_nano == {"checkout POST /checkout": 991}
    assert fitted.samples == {"checkout POST /checkout": 1000, "rare POST /checkout": 5}
    assert fitted.quantile == 0.99


def test_thresholds_round_trip_through_a_file(make_span, tmp_path):
    fitted = fit_latency_thresholds([make_span()], quantile=0.9, min_samples=1)
    path = tmp_path / "nested" / "thresholds.json"
    write_thresholds(fitted, path)
    assert load_thresholds(path) == fitted


def test_threshold_file_with_the_wrong_shape_is_rejected(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps({"quantile": 0.9}))
    with pytest.raises(ValueError, match="threshold"):
        load_thresholds(path)


def test_train_command_fits_thresholds_from_generated_traffic(
    tiny_simulation_path, tmp_path, monkeypatch
):
    output = tmp_path / "generated"
    destination = tmp_path / "fitted" / "thresholds.json"
    monkeypatch.setenv("TAILSAMPLER_LATENCY_THRESHOLDS_PATH", str(destination))
    monkeypatch.setenv("TAILSAMPLER_MIN_CALIBRATION_SAMPLES", "5")
    assert main(["generate", "--config", str(tiny_simulation_path), "--output", str(output)]) == 0
    assert main(["train", "--requests", str(output / "calibration.requests")]) == 0
    fitted = load_thresholds(destination)
    assert list(fitted.thresholds_nano) == ["web GET /"]
    assert fitted.samples["web GET /"] > 50
    assert fitted.thresholds_nano["web GET /"] > 0
