import csv
import importlib.metadata
import json
import os
import platform
import sqlite3
import statistics
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import astuple, dataclass, fields
from itertools import chain
from pathlib import Path
from typing import Any

from tailsampler.model import KeptTrace, SpanRecord
from tailsampler.otlp import decode_export_request
from tailsampler.policies import quantile_of_sorted
from tailsampler.sampler import Sampler, TimedRequest
from tailsampler.store import SpanStore

NANOS_PER_SECOND = 1_000_000_000
NANOS_PER_MICROSECOND = 1_000
MEDIAN_QUANTILE = 0.5
TAIL_QUANTILE = 0.99
REPORTED_PACKAGES = ("starlette", "uvicorn")
CPU_INFO_PATH = Path("/proc/cpuinfo")
MEMORY_INFO_PATH = Path("/proc/meminfo")
BYTES_PER_KIBIBYTE = 1024
BYTES_PER_GIBIBYTE = 1024**3


@dataclass(frozen=True, slots=True)
class StageTiming:
    stage: str
    calls: int
    spans: int
    seconds: float
    spans_per_second: int
    p50_microseconds: float
    p99_microseconds: float


def summarize_timings(
    stage: str, spans: int, repeat_durations: Sequence[Sequence[int]]
) -> StageTiming:
    if not repeat_durations or not repeat_durations[0]:
        raise ValueError("at least one timed call is required")
    seconds = statistics.median(sum(durations) for durations in repeat_durations) / NANOS_PER_SECOND
    pooled = sorted(chain.from_iterable(repeat_durations))
    return StageTiming(
        stage=stage,
        calls=len(repeat_durations[0]),
        spans=spans,
        seconds=round(seconds, 4),
        spans_per_second=round(spans / seconds),
        p50_microseconds=round(
            quantile_of_sorted(pooled, MEDIAN_QUANTILE) / NANOS_PER_MICROSECOND, 2
        ),
        p99_microseconds=round(
            quantile_of_sorted(pooled, TAIL_QUANTILE) / NANOS_PER_MICROSECOND, 2
        ),
    )


def decode_and_time(
    bodies: Sequence[bytes], repeats: int
) -> tuple[list[list[SpanRecord]], StageTiming]:
    repeat_durations: list[list[int]] = []
    decoded: list[list[SpanRecord]] = []
    for _ in range(repeats):
        decoded = []
        durations: list[int] = []
        for body in bodies:
            started = time.perf_counter_ns()
            decoded.append(decode_export_request(body))
            durations.append(time.perf_counter_ns() - started)
        repeat_durations.append(durations)
    span_total = sum(len(spans) for spans in decoded)
    return decoded, summarize_timings("decode", span_total, repeat_durations)


def time_ingest(
    stage: str, make_sampler: Callable[[], Sampler], requests: Sequence[TimedRequest], repeats: int
) -> StageTiming:
    repeat_durations: list[list[int]] = []
    for _ in range(repeats):
        sampler = make_sampler()
        durations: list[int] = []
        for sent_unix_nano, spans in requests:
            started = time.perf_counter_ns()
            sampler.ingest(spans, sent_unix_nano)
            durations.append(time.perf_counter_ns() - started)
        repeat_durations.append(durations)
    span_total = sum(len(spans) for _, spans in requests)
    return summarize_timings(stage, span_total, repeat_durations)


def collect_kept_batches(
    make_sampler: Callable[[], Sampler], requests: Sequence[TimedRequest]
) -> list[list[KeptTrace]]:
    sampler = make_sampler()
    batches: list[list[KeptTrace]] = []
    for sent_unix_nano, spans in requests:
        kept = sampler.ingest(spans, sent_unix_nano)
        if kept:
            batches.append(kept)
    final = sampler.flush()
    if final:
        batches.append(final)
    return batches


def time_store_insert(
    stage: str, batches: Sequence[Sequence[KeptTrace]], repeats: int
) -> StageTiming:
    repeat_durations: list[list[int]] = []
    for _ in range(repeats):
        durations: list[int] = []
        with tempfile.TemporaryDirectory() as directory:
            store = SpanStore(Path(directory) / "spans.sqlite3")
            for batch in batches:
                started = time.perf_counter_ns()
                store.insert(batch)
                durations.append(time.perf_counter_ns() - started)
            store.close()
        repeat_durations.append(durations)
    span_total = sum(len(item.spans) for batch in batches for item in batch)
    return summarize_timings(stage, span_total, repeat_durations)


def write_timings(path: Path, timings: Sequence[StageTiming]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([item.name for item in fields(StageTiming)])
        for timing in timings:
            writer.writerow(astuple(timing))


def read_timings(path: Path) -> list[StageTiming]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            StageTiming(
                stage=row["stage"],
                calls=int(row["calls"]),
                spans=int(row["spans"]),
                seconds=float(row["seconds"]),
                spans_per_second=int(row["spans_per_second"]),
                p50_microseconds=float(row["p50_microseconds"]),
                p99_microseconds=float(row["p99_microseconds"]),
            )
            for row in csv.DictReader(handle)
        ]


def capture_environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "memory_gib": _memory_gibibytes(),
        "sqlite": sqlite3.sqlite_version,
        "packages": {name: importlib.metadata.version(name) for name in REPORTED_PACKAGES},
    }


def write_environment(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(capture_environment(), indent=2, sort_keys=True) + "\n", "utf-8")


def _cpu_model() -> str | None:
    if CPU_INFO_PATH.exists():
        for line in CPU_INFO_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                return line.partition(":")[2].strip()
    return platform.processor() or None


def _memory_gibibytes() -> float | None:
    if MEMORY_INFO_PATH.exists():
        for line in MEMORY_INFO_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                kibibytes = int(line.split()[1])
                return round(kibibytes * BYTES_PER_KIBIBYTE / BYTES_PER_GIBIBYTE, 1)
    return None
