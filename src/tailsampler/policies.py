import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from tailsampler.model import SpanRecord

REASON_ERROR = "error"
REASON_LATENCY = "latency"
REASON_BASELINE = "baseline"
REASON_NOT_SAMPLED = "not_sampled"
REASON_HEAD = "head"
REASON_KEEP_ALL = "keep_all"

TRACE_ID_HASH_DIGITS = 16
TRACE_ID_HASH_RANGE = 2**64


@dataclass(frozen=True, slots=True)
class Decision:
    keep: bool
    reason: str
    weight: float


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    baseline_rate: float
    thresholds_nano: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class LatencyThresholds:
    quantile: float
    min_samples: int
    thresholds_nano: dict[str, int]
    samples: dict[str, int]


def route_key(span: SpanRecord) -> str:
    return f"{span.service} {span.name}"


def baseline_selects(trace_id: str, rate: float) -> bool:
    return int(trace_id[-TRACE_ID_HASH_DIGITS:], 16) < rate * TRACE_ID_HASH_RANGE


def decide(policy: SamplingPolicy, trace_id: str, spans: Sequence[SpanRecord]) -> Decision:
    if any(span.is_error for span in spans):
        return Decision(True, REASON_ERROR, 1.0)
    root = next((span for span in spans if span.is_root), None)
    if root is not None:
        threshold = policy.thresholds_nano.get(route_key(root))
        if threshold is not None and root.duration_nano >= threshold:
            return Decision(True, REASON_LATENCY, 1.0)
    if baseline_selects(trace_id, policy.baseline_rate):
        return Decision(True, REASON_BASELINE, 1.0 / policy.baseline_rate)
    return Decision(False, REASON_NOT_SAMPLED, 0.0)


def quantile_of_sorted(values: Sequence[int], quantile: float) -> float:
    if not values:
        raise ValueError("cannot take a quantile of no values")
    position = quantile * (len(values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def fit_latency_thresholds(
    root_spans: Iterable[SpanRecord], quantile: float, min_samples: int
) -> LatencyThresholds:
    durations: dict[str, list[int]] = {}
    for span in root_spans:
        durations.setdefault(route_key(span), []).append(span.duration_nano)
    thresholds: dict[str, int] = {}
    samples: dict[str, int] = {}
    for key in sorted(durations):
        ordered = sorted(durations[key])
        samples[key] = len(ordered)
        if len(ordered) >= min_samples:
            thresholds[key] = math.ceil(quantile_of_sorted(ordered, quantile))
    return LatencyThresholds(quantile, min_samples, thresholds, samples)


def write_thresholds(thresholds: LatencyThresholds, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(thresholds), indent=2, sort_keys=True) + "\n", "utf-8")


def load_thresholds(path: Path) -> LatencyThresholds:
    try:
        return LatencyThresholds(**json.loads(path.read_text(encoding="utf-8")))
    except TypeError as error:
        raise ValueError(f"{path} is not a latency threshold file: {error}") from error
