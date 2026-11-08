import json
import logging
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from tailsampler import benchmark
from tailsampler.config import GatewaySettings, load_gateway_settings
from tailsampler.fleet import Simulation, load_simulation
from tailsampler.model import KeptTrace
from tailsampler.policies import LatencyThresholds, load_thresholds
from tailsampler.sampler import (
    HeadSampler,
    KeepAllSampler,
    Sampler,
    TailSampler,
    TimedRequest,
    replay_in_event_time,
)
from tailsampler.scoring import (
    categorize,
    complete_trace_fraction,
    recall_by_category,
    route_accuracy,
    span_counts_by_trace,
    summarize_accuracy,
)
from tailsampler.store import SpanStore
from tailsampler.traffic import TraceTruth, read_requests, read_truths

STORE_CHUNK_TRACES = 5000

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EvaluationSettings:
    slow_quantile: float = 0.99
    decision_wait_seconds: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 20.0)
    baseline_rates: tuple[float, ...] = (0.01, 0.02, 0.05, 0.1)
    benchmark_repeats: int = 3

    def __post_init__(self) -> None:
        if not 0 < self.slow_quantile < 1:
            raise ValueError(f"slow_quantile must be in (0, 1), got {self.slow_quantile}")
        if not self.decision_wait_seconds or min(self.decision_wait_seconds) <= 0:
            raise ValueError("decision_wait_seconds needs at least one positive value")
        if not self.baseline_rates or not all(0 < rate < 1 for rate in self.baseline_rates):
            raise ValueError("baseline_rates needs at least one value in (0, 1)")
        if self.benchmark_repeats < 1:
            raise ValueError("benchmark_repeats must be at least 1")


@dataclass(frozen=True, slots=True)
class SamplerOutcome:
    spans_received: int
    spans_kept: int
    span_fraction: float
    traces_kept: int
    trace_fraction: float
    complete_trace_fraction: float
    peak_buffered_traces: int
    peak_buffered_spans: int
    late_spans_kept: int
    late_spans_dropped: int
    forced_decisions: int
    kept_by_reason: dict[str, int]
    recall: dict[str, float]


def load_evaluation_settings(path: str | Path) -> EvaluationSettings:
    with Path(path).open("rb") as handle:
        values = dict(tomllib.load(handle).get("evaluation", {}))
    known = {item.name for item in fields(EvaluationSettings)}
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError(f"unknown evaluation settings: {', '.join(unknown)}")
    for name in ("decision_wait_seconds", "baseline_rates"):
        if name in values:
            values[name] = tuple(float(item) for item in values[name])
    return EvaluationSettings(**values)


def route_names(simulation: Simulation) -> dict[str, tuple[str, str]]:
    return {
        name: (operation.service, operation.span_name)
        for name, operation in simulation.fleet.operations.items()
    }


def measure_sampler(
    sampler: Sampler,
    kept: Sequence[KeptTrace],
    truths: Mapping[str, TraceTruth],
    categories: Mapping[str, frozenset[str]],
) -> SamplerOutcome:
    span_counts = span_counts_by_trace(kept)
    spans_received = sampler.counters.spans_received
    spans_kept = sum(span_counts.values())
    reasons: dict[str, int] = {}
    counted: set[str] = set()
    for item in kept:
        if item.trace_id not in counted:
            counted.add(item.trace_id)
            reasons[item.reason] = reasons.get(item.reason, 0) + 1
    return SamplerOutcome(
        spans_received=spans_received,
        spans_kept=spans_kept,
        span_fraction=spans_kept / spans_received,
        traces_kept=len(span_counts),
        trace_fraction=len(span_counts) / len(truths),
        complete_trace_fraction=complete_trace_fraction(span_counts, truths),
        peak_buffered_traces=sampler.counters.peak_buffered_traces,
        peak_buffered_spans=sampler.counters.peak_buffered_spans,
        late_spans_kept=sampler.counters.late_spans_kept,
        late_spans_dropped=sampler.counters.late_spans_dropped,
        forced_decisions=sampler.counters.forced_decisions,
        kept_by_reason=dict(sorted(reasons.items())),
        recall=recall_by_category(categories, span_counts),
    )


def estimate_accuracy(
    kept: Sequence[KeptTrace],
    truths: Mapping[str, TraceTruth],
    routes: Mapping[str, tuple[str, str]],
) -> dict[str, Any]:
    store = SpanStore(":memory:")
    for start in range(0, len(kept), STORE_CHUNK_TRACES):
        store.insert(kept[start : start + STORE_CHUNK_TRACES])
    estimates = store.estimates()
    store.close()
    return summarize_accuracy(route_accuracy(estimates, truths, routes))


def make_tail_sampler(
    settings: GatewaySettings,
    thresholds: LatencyThresholds,
    decision_wait_seconds: float,
    baseline_rate: float,
) -> TailSampler:
    adjusted = replace(
        settings, decision_wait_seconds=decision_wait_seconds, baseline_rate=baseline_rate
    )
    return TailSampler.from_settings(adjusted, thresholds)


def evaluate_samplers(
    simulation: Simulation,
    gateway_settings: GatewaySettings,
    thresholds: LatencyThresholds,
    settings: EvaluationSettings,
    requests: Sequence[TimedRequest],
    truths: Mapping[str, TraceTruth],
) -> dict[str, Any]:
    categories = categorize(truths, settings.slow_quantile)
    routes = route_names(simulation)

    def run_tail(wait: float, rate: float) -> tuple[SamplerOutcome, list[KeptTrace]]:
        sampler = make_tail_sampler(gateway_settings, thresholds, wait, rate)
        kept = replay_in_event_time(sampler, requests)
        return measure_sampler(sampler, kept, truths, categories), kept

    def run_head(rate: float) -> tuple[SamplerOutcome, list[KeptTrace]]:
        sampler = HeadSampler(rate)
        kept = replay_in_event_time(sampler, requests)
        return measure_sampler(sampler, kept, truths, categories), kept

    main_wait = gateway_settings.decision_wait_seconds
    main_rate = gateway_settings.baseline_rate
    tail, tail_kept = run_tail(main_wait, main_rate)
    head, head_kept = run_head(tail.span_fraction)
    keep_all_sampler = KeepAllSampler()
    keep_all_kept = replay_in_event_time(keep_all_sampler, requests)
    keep_all = measure_sampler(keep_all_sampler, keep_all_kept, truths, categories)
    logger.info(
        "tail kept %.1f%% of spans, head matched at %.1f%%",
        tail.span_fraction * 100,
        head.span_fraction * 100,
    )
    estimates = {
        "tail": estimate_accuracy(tail_kept, truths, routes),
        "head": estimate_accuracy(head_kept, truths, routes),
        "keep_all": estimate_accuracy(keep_all_kept, truths, routes),
    }
    wait_sweep = [
        {"decision_wait_seconds": wait, "tail": asdict(run_tail(wait, main_rate)[0])}
        for wait in settings.decision_wait_seconds
    ]
    rate_sweep = []
    for rate in settings.baseline_rates:
        swept_tail = run_tail(main_wait, rate)[0]
        rate_sweep.append(
            {
                "baseline_rate": rate,
                "tail": asdict(swept_tail),
                "head": asdict(run_head(swept_tail.span_fraction)[0]),
            }
        )
    return {
        "categories": {name: len(members) for name, members in categories.items()},
        "policy": {
            "baseline_rate": main_rate,
            "decision_wait_seconds": main_wait,
            "latency_quantile": thresholds.quantile,
            "slow_quantile": settings.slow_quantile,
            "routes_with_threshold": len(thresholds.thresholds_nano),
            "head_rate": tail.span_fraction,
        },
        "samplers": {
            "tail": asdict(tail),
            "head": asdict(head),
            "keep_all": asdict(keep_all),
        },
        "estimates": estimates,
        "sweeps": {"decision_wait": wait_sweep, "baseline_rate": rate_sweep},
    }


def run_timings(
    gateway_settings: GatewaySettings,
    thresholds: LatencyThresholds,
    requests: Sequence[TimedRequest],
    repeats: int,
) -> list[benchmark.StageTiming]:
    def make_tail() -> Sampler:
        return make_tail_sampler(
            gateway_settings,
            thresholds,
            gateway_settings.decision_wait_seconds,
            gateway_settings.baseline_rate,
        )

    def make_head() -> Sampler:
        return HeadSampler(gateway_settings.baseline_rate)

    batches = benchmark.collect_kept_batches(make_tail, requests)
    return [
        benchmark.time_ingest("ingest_tail", make_tail, requests, repeats),
        benchmark.time_ingest("ingest_head", make_head, requests, repeats),
        benchmark.time_ingest("ingest_keep_all", KeepAllSampler, requests, repeats),
        benchmark.time_store_insert("store_insert_tail", batches, repeats),
    ]


def run_evaluation(simulation_path: Path, data_directory: Path, output_directory: Path) -> None:
    gateway_settings = load_gateway_settings()
    simulation = load_simulation(simulation_path)
    settings = load_evaluation_settings(simulation_path)
    thresholds = load_thresholds(Path(gateway_settings.latency_thresholds_path))
    truths = read_truths(data_directory / "evaluation.truth.jsonl")
    sent_times: list[int] = []
    bodies: list[bytes] = []
    for sent_unix_nano, body in read_requests(data_directory / "evaluation.requests"):
        sent_times.append(sent_unix_nano)
        bodies.append(body)
    decoded, decode_timing = benchmark.decode_and_time(bodies, settings.benchmark_repeats)
    del bodies
    requests = list(zip(sent_times, decoded, strict=True))
    logger.info("decoded %d requests, scoring samplers", len(requests))
    scores = evaluate_samplers(simulation, gateway_settings, thresholds, settings, requests, truths)
    manifest = json.loads((data_directory / "manifest.json").read_text(encoding="utf-8"))
    metrics = {"dataset": manifest, **scores}
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "metrics.json").write_text(
        json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
    )
    logger.info("timing the pipeline stages")
    timings = [
        decode_timing,
        *run_timings(gateway_settings, thresholds, requests, settings.benchmark_repeats),
    ]
    benchmark.write_timings(output_directory / "benchmarks.csv", timings)
    benchmark.write_environment(output_directory / "environment.json")
    logger.info("wrote metrics.json, benchmarks.csv and environment.json to %s", output_directory)
