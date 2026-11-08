from collections import OrderedDict
from collections.abc import Iterable, Sequence
from typing import Self

from tailsampler.buffer import BufferedTrace, TraceBuffer
from tailsampler.config import GatewaySettings
from tailsampler.model import KeptTrace, SamplerCounters, SpanRecord
from tailsampler.policies import (
    REASON_HEAD,
    REASON_KEEP_ALL,
    Decision,
    LatencyThresholds,
    SamplingPolicy,
    baseline_selects,
    decide,
)

NANOS_PER_SECOND = 1_000_000_000


class DecisionCache:
    def __init__(self, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._decisions: OrderedDict[str, Decision] = OrderedDict()

    def __len__(self) -> int:
        return len(self._decisions)

    def get(self, trace_id: str) -> Decision | None:
        return self._decisions.get(trace_id)

    def remember(self, trace_id: str, decision: Decision) -> None:
        self._decisions[trace_id] = decision
        if len(self._decisions) > self._max_entries:
            self._decisions.popitem(last=False)


class TailSampler:
    def __init__(
        self,
        policy: SamplingPolicy,
        decision_wait_nano: int,
        max_buffered_traces: int,
        decision_cache_size: int,
    ) -> None:
        self.policy = policy
        self.counters = SamplerCounters()
        self._max_buffered_traces = max_buffered_traces
        self._buffer = TraceBuffer(decision_wait_nano, max_buffered_traces)
        self._decisions = DecisionCache(decision_cache_size)

    @classmethod
    def from_settings(cls, settings: GatewaySettings, thresholds: LatencyThresholds) -> Self:
        return cls(
            policy=SamplingPolicy(settings.baseline_rate, thresholds.thresholds_nano),
            decision_wait_nano=round(settings.decision_wait_seconds * NANOS_PER_SECOND),
            max_buffered_traces=settings.max_buffered_traces,
            decision_cache_size=settings.decision_cache_size,
        )

    @property
    def buffered_traces(self) -> int:
        return len(self._buffer)

    @property
    def buffered_spans(self) -> int:
        return self._buffer.span_count

    def ingest(self, spans: Sequence[SpanRecord], now_nano: int) -> list[KeptTrace]:
        kept = self.advance(now_nano)
        late: dict[str, tuple[Decision, list[SpanRecord]]] = {}
        counters = self.counters
        for span in spans:
            counters.spans_received += 1
            decision = self._decisions.get(span.trace_id)
            if decision is None:
                self._buffer.add(span, now_nano)
                counters.peak_buffered_spans = max(
                    counters.peak_buffered_spans, self._buffer.span_count
                )
                while self._buffer.over_capacity:
                    counters.forced_decisions += 1
                    kept.extend(self._decide(self._buffer.pop_oldest()))
            elif decision.keep:
                late.setdefault(span.trace_id, (decision, []))[1].append(span)
                counters.late_spans_kept += 1
                counters.spans_kept += 1
            else:
                counters.late_spans_dropped += 1
                counters.spans_dropped += 1
        counters.peak_buffered_traces = max(counters.peak_buffered_traces, len(self._buffer))
        for trace_id, (decision, late_spans) in late.items():
            kept.append(
                KeptTrace(trace_id, decision.reason, decision.weight, tuple(late_spans), True)
            )
        return kept

    def advance(self, now_nano: int) -> list[KeptTrace]:
        kept: list[KeptTrace] = []
        for trace in self._buffer.pop_due(now_nano):
            kept.extend(self._decide(trace))
        return kept

    def flush(self) -> list[KeptTrace]:
        kept: list[KeptTrace] = []
        for trace in self._buffer.pop_all():
            kept.extend(self._decide(trace))
        return kept

    def _decide(self, trace: BufferedTrace) -> list[KeptTrace]:
        decision = decide(self.policy, trace.trace_id, trace.spans)
        self._decisions.remember(trace.trace_id, decision)
        span_total = len(trace.spans)
        counters = self.counters
        if not decision.keep:
            counters.traces_dropped += 1
            counters.spans_dropped += span_total
            return []
        counters.traces_kept += 1
        counters.spans_kept += span_total
        counters.kept_by_reason[decision.reason] = (
            counters.kept_by_reason.get(decision.reason, 0) + 1
        )
        return [KeptTrace(trace.trace_id, decision.reason, decision.weight, tuple(trace.spans))]


class HeadSampler:
    def __init__(self, rate: float) -> None:
        if not 0 < rate <= 1:
            raise ValueError("rate must be in (0, 1]")
        self.rate = rate
        self.counters = SamplerCounters()

    def ingest(self, spans: Sequence[SpanRecord], now_nano: int) -> list[KeptTrace]:
        selected: dict[str, list[SpanRecord]] = {}
        for span in spans:
            self.counters.spans_received += 1
            if baseline_selects(span.trace_id, self.rate):
                selected.setdefault(span.trace_id, []).append(span)
                self.counters.spans_kept += 1
            else:
                self.counters.spans_dropped += 1
        weight = 1.0 / self.rate
        return [
            KeptTrace(trace_id, REASON_HEAD, weight, tuple(members))
            for trace_id, members in selected.items()
        ]

    def advance(self, now_nano: int) -> list[KeptTrace]:
        return []

    def flush(self) -> list[KeptTrace]:
        return []


class KeepAllSampler:
    def __init__(self) -> None:
        self.counters = SamplerCounters()

    def ingest(self, spans: Sequence[SpanRecord], now_nano: int) -> list[KeptTrace]:
        grouped: dict[str, list[SpanRecord]] = {}
        for span in spans:
            self.counters.spans_received += 1
            self.counters.spans_kept += 1
            grouped.setdefault(span.trace_id, []).append(span)
        return [
            KeptTrace(trace_id, REASON_KEEP_ALL, 1.0, tuple(members))
            for trace_id, members in grouped.items()
        ]

    def advance(self, now_nano: int) -> list[KeptTrace]:
        return []

    def flush(self) -> list[KeptTrace]:
        return []


type Sampler = TailSampler | HeadSampler | KeepAllSampler
type TimedRequest = tuple[int, Sequence[SpanRecord]]


def replay_in_event_time(sampler: Sampler, requests: Iterable[TimedRequest]) -> list[KeptTrace]:
    kept: list[KeptTrace] = []
    for sent_unix_nano, spans in requests:
        kept.extend(sampler.ingest(spans, sent_unix_nano))
    kept.extend(sampler.flush())
    return kept
