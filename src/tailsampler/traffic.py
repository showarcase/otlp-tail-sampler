import json
import logging
import math
import random
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from tailsampler.fleet import NANOS_PER_MILLI, Fleet, Incident, Simulation, TrafficPlan
from tailsampler.model import SpanRecord, StatusCode
from tailsampler.otlp import build_export_request

NANOS_PER_SECOND = 1_000_000_000
CALL_GAP_NANO = 200_000
SERVER_SPAN_KIND = 2

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TraceTruth:
    trace_id: str
    root_operation: str
    start_unix_nano: int
    duration_nano: int
    span_count: int
    error: bool
    incident: str
    rare_path: bool


@dataclass(frozen=True, slots=True)
class ExportBatch:
    sent_unix_nano: int
    service: str
    spans: tuple[SpanRecord, ...]


@dataclass(frozen=True, slots=True)
class GeneratedTraffic:
    batches: list[ExportBatch]
    truths: list[TraceTruth]


@dataclass(frozen=True, slots=True)
class DatasetSummary:
    seed: int
    duration_seconds: float
    traces: int
    spans: int
    requests: int
    request_bytes: int
    error_traces: int
    rare_path_traces: int
    incident_traces: dict[str, int]


class _TraceBuilder:
    def __init__(
        self, fleet: Fleet, rng: random.Random, trace_id: str, active: list[Incident]
    ) -> None:
        self.fleet = fleet
        self.rng = rng
        self.trace_id = trace_id
        self.active = active
        self.spans: list[SpanRecord] = []
        self.applied: list[str] = []
        self.rare_path = False

    def run(self, operation_name: str, parent_span_id: str, start_nano: int) -> tuple[int, bool]:
        operation = self.fleet.operations[operation_name]
        span_id = f"{self.rng.getrandbits(64) or 1:016x}"
        self_ms = self.rng.lognormvariate(math.log(operation.self_median_ms), operation.self_sigma)
        failure = ""
        for incident in self.active:
            if incident.operation != operation_name or self.rng.random() >= incident.probability:
                continue
            if incident.kind == "latency":
                self_ms += self.rng.lognormvariate(
                    math.log(incident.added_median_ms), incident.added_sigma
                )
            else:
                failure = "internal error"
            if incident.name not in self.applied:
                self.applied.append(incident.name)
        half_nano = round(self_ms * NANOS_PER_MILLI / 2)
        cursor = start_nano + half_nano
        for call in operation.calls:
            if failure:
                break
            if self.rng.random() >= call.probability:
                continue
            if call.background:
                self.run(call.operation, span_id, cursor)
                continue
            child_end, child_failed = self.run(call.operation, span_id, cursor + CALL_GAP_NANO)
            cursor = child_end + CALL_GAP_NANO
            if child_failed and self.rng.random() < self.fleet.propagate_error_probability:
                failure = "downstream failure"
        if not failure and self.rng.random() < operation.error_rate:
            failure = "internal error"
        end_nano = cursor + half_nano
        if operation.rare:
            self.rare_path = True
        is_entrypoint = not parent_span_id
        self.spans.append(
            SpanRecord(
                trace_id=self.trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                service=operation.service,
                name=operation.span_name,
                kind=SERVER_SPAN_KIND,
                start_unix_nano=start_nano,
                end_unix_nano=end_nano,
                status_code=StatusCode.ERROR if failure else StatusCode.UNSET,
                status_message=failure,
                attributes={"http.status_code": 500 if failure else 200} if is_entrypoint else {},
            )
        )
        return end_nano, bool(failure)


def generate_traffic(fleet: Fleet, plan: TrafficPlan) -> GeneratedTraffic:
    trace_rng = random.Random(f"{plan.seed}:traces")
    export_rng = random.Random(f"{plan.seed}:export")
    entrypoints = fleet.entrypoints
    weights = [item.entrypoint_weight for item in entrypoints]
    spans: list[SpanRecord] = []
    truths: list[TraceTruth] = []
    offset_seconds = trace_rng.expovariate(plan.requests_per_second)
    while offset_seconds < plan.duration_seconds:
        start_nano = fleet.start_unix_nano + round(offset_seconds * NANOS_PER_SECOND)
        active = [
            incident
            for incident in fleet.incidents
            if plan.incidents and incident.start_second <= offset_seconds < incident.end_second
        ]
        builder = _TraceBuilder(fleet, trace_rng, f"{trace_rng.getrandbits(128):032x}", active)
        entry = trace_rng.choices(entrypoints, weights=weights)[0]
        end_nano, _ = builder.run(entry.name, "", start_nano)
        spans.extend(builder.spans)
        truths.append(
            TraceTruth(
                trace_id=builder.trace_id,
                root_operation=entry.name,
                start_unix_nano=start_nano,
                duration_nano=end_nano - start_nano,
                span_count=len(builder.spans),
                error=any(span.is_error for span in builder.spans),
                incident=builder.applied[0] if builder.applied else "",
                rare_path=builder.rare_path,
            )
        )
        offset_seconds += trace_rng.expovariate(plan.requests_per_second)
    return GeneratedTraffic(batches=_export_batches(fleet, spans, export_rng), truths=truths)


def _export_batches(fleet: Fleet, spans: list[SpanRecord], rng: random.Random) -> list[ExportBatch]:
    phases = {
        service: rng.randrange(fleet.export_interval_nano(service)) for service in fleet.services
    }
    grouped: dict[tuple[str, int], list[SpanRecord]] = {}
    for span in sorted(spans, key=lambda item: (item.end_unix_nano, item.span_id)):
        interval = fleet.export_interval_nano(span.service)
        index = (span.end_unix_nano - phases[span.service]) // interval
        grouped.setdefault((span.service, index), []).append(span)
    low_ms, high_ms = fleet.network_delay_ms
    batches: list[ExportBatch] = []
    for (service, index), members in sorted(grouped.items()):
        interval = fleet.export_interval_nano(service)
        flush_nano = phases[service] + (index + 1) * interval
        delay_nano = rng.randint(low_ms, high_ms) * NANOS_PER_MILLI
        batches.append(ExportBatch(flush_nano + delay_nano, service, tuple(members)))
    batches.sort(key=lambda batch: batch.sent_unix_nano)
    return batches


def write_traffic(
    traffic: GeneratedTraffic, plan: TrafficPlan, requests_path: Path, truth_path: Path
) -> DatasetSummary:
    requests_path.parent.mkdir(parents=True, exist_ok=True)
    with requests_path.open("wb") as handle:
        for batch in traffic.batches:
            body = json.dumps(build_export_request(batch.spans), separators=(",", ":"))
            handle.write(f"{batch.sent_unix_nano}\t{body}\n".encode())
    with truth_path.open("w", encoding="utf-8") as handle:
        for truth in traffic.truths:
            handle.write(json.dumps(asdict(truth), separators=(",", ":")) + "\n")
    incident_traces: dict[str, int] = {}
    for truth in traffic.truths:
        if truth.incident:
            incident_traces[truth.incident] = incident_traces.get(truth.incident, 0) + 1
    return DatasetSummary(
        seed=plan.seed,
        duration_seconds=plan.duration_seconds,
        traces=len(traffic.truths),
        spans=sum(truth.span_count for truth in traffic.truths),
        requests=len(traffic.batches),
        request_bytes=requests_path.stat().st_size,
        error_traces=sum(truth.error for truth in traffic.truths),
        rare_path_traces=sum(truth.rare_path for truth in traffic.truths),
        incident_traces=dict(sorted(incident_traces.items())),
    )


def write_datasets(simulation: Simulation, directory: Path) -> dict[str, DatasetSummary]:
    summaries: dict[str, DatasetSummary] = {}
    for name, plan in (
        ("calibration", simulation.calibration),
        ("evaluation", simulation.evaluation),
    ):
        traffic = generate_traffic(simulation.fleet, plan)
        summary = write_traffic(
            traffic, plan, directory / f"{name}.requests", directory / f"{name}.truth.jsonl"
        )
        logger.info(
            "wrote %s: %d traces, %d spans, %d requests, %d bytes",
            name,
            summary.traces,
            summary.spans,
            summary.requests,
            summary.request_bytes,
        )
        summaries[name] = summary
    manifest = {name: asdict(summary) for name, summary in summaries.items()}
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return summaries


def read_requests(path: Path) -> Iterator[tuple[int, bytes]]:
    with path.open("rb") as handle:
        for line in handle:
            sent, _, body = line.partition(b"\t")
            yield int(sent), body.rstrip()


def read_truths(path: Path) -> dict[str, TraceTruth]:
    with path.open(encoding="utf-8") as handle:
        truths = (TraceTruth(**json.loads(line)) for line in handle)
        return {truth.trace_id: truth for truth in truths}
