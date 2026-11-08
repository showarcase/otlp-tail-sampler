import statistics
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from tailsampler.model import KeptTrace
from tailsampler.policies import quantile_of_sorted
from tailsampler.store import RouteEstimate
from tailsampler.traffic import TraceTruth

CATEGORY_ERROR = "error"
CATEGORY_SLOW = "slow"
CATEGORY_RARE_PATH = "rare_path"
CATEGORY_TYPICAL = "typical"


@dataclass(frozen=True, slots=True)
class RouteAccuracy:
    service: str
    name: str
    true_requests: int
    estimated_requests: float
    request_error_percent: float
    true_failed_requests: int
    estimated_failed_requests: float
    failed_error_percent: float | None


def categorize(truths: Mapping[str, TraceTruth], slow_quantile: float) -> dict[str, frozenset[str]]:
    durations: dict[str, list[int]] = {}
    for truth in truths.values():
        durations.setdefault(truth.root_operation, []).append(truth.duration_nano)
    cutoffs = {
        operation: quantile_of_sorted(sorted(values), slow_quantile)
        for operation, values in durations.items()
    }
    error: set[str] = set()
    slow: set[str] = set()
    rare_path: set[str] = set()
    typical: set[str] = set()
    incidents: dict[str, set[str]] = {}
    for truth in truths.values():
        is_slow = truth.duration_nano >= cutoffs[truth.root_operation]
        if truth.error:
            error.add(truth.trace_id)
        if is_slow:
            slow.add(truth.trace_id)
        if truth.rare_path:
            rare_path.add(truth.trace_id)
        if truth.incident:
            incidents.setdefault(truth.incident, set()).add(truth.trace_id)
        if not (truth.error or is_slow or truth.rare_path or truth.incident):
            typical.add(truth.trace_id)
    categories = {CATEGORY_ERROR: error, CATEGORY_SLOW: slow, CATEGORY_RARE_PATH: rare_path}
    categories.update(sorted(incidents.items()))
    categories[CATEGORY_TYPICAL] = typical
    return {name: frozenset(members) for name, members in categories.items() if members}


def recall_by_category(
    categories: Mapping[str, frozenset[str]], kept_trace_ids: Collection[str]
) -> dict[str, float]:
    kept = frozenset(kept_trace_ids)
    return {name: len(members & kept) / len(members) for name, members in categories.items()}


def span_counts_by_trace(kept: Iterable[KeptTrace]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in kept:
        counts[item.trace_id] = counts.get(item.trace_id, 0) + len(item.spans)
    return counts


def complete_trace_fraction(
    span_counts: Mapping[str, int], truths: Mapping[str, TraceTruth]
) -> float:
    if not span_counts:
        return 0.0
    complete = sum(
        1 for trace_id, count in span_counts.items() if count == truths[trace_id].span_count
    )
    return complete / len(span_counts)


def route_accuracy(
    estimates: Sequence[RouteEstimate],
    truths: Mapping[str, TraceTruth],
    routes: Mapping[str, tuple[str, str]],
) -> list[RouteAccuracy]:
    true_requests: dict[tuple[str, str], int] = {}
    true_failed: dict[tuple[str, str], int] = {}
    for truth in truths.values():
        route = routes[truth.root_operation]
        true_requests[route] = true_requests.get(route, 0) + 1
        true_failed[route] = true_failed.get(route, 0) + int(truth.error)
    estimated = {(item.service, item.name): item for item in estimates}
    accuracies: list[RouteAccuracy] = []
    for route in sorted(true_requests):
        found = estimated.get(route)
        estimated_requests = found.estimated_requests if found else 0.0
        estimated_failed = found.estimated_failed_requests if found else 0.0
        failed = true_failed[route]
        accuracies.append(
            RouteAccuracy(
                service=route[0],
                name=route[1],
                true_requests=true_requests[route],
                estimated_requests=estimated_requests,
                request_error_percent=_percent_error(estimated_requests, true_requests[route]),
                true_failed_requests=failed,
                estimated_failed_requests=estimated_failed,
                failed_error_percent=_percent_error(estimated_failed, failed) if failed else None,
            )
        )
    return accuracies


def summarize_accuracy(accuracies: Sequence[RouteAccuracy]) -> dict[str, Any]:
    request_errors = [abs(item.request_error_percent) for item in accuracies]
    failed_errors = [
        abs(item.failed_error_percent)
        for item in accuracies
        if item.failed_error_percent is not None
    ]
    return {
        "median_abs_request_error_percent": statistics.median(request_errors),
        "max_abs_request_error_percent": max(request_errors),
        "max_abs_failed_error_percent": max(failed_errors) if failed_errors else None,
        "true_failed_requests": sum(item.true_failed_requests for item in accuracies),
        "estimated_failed_requests": sum(item.estimated_failed_requests for item in accuracies),
        "routes": [asdict(item) for item in accuracies],
    }


def _percent_error(estimated: float, true: int) -> float:
    return (estimated - true) / true * 100.0
