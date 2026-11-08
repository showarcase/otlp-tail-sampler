from collections import OrderedDict
from dataclasses import dataclass

from tailsampler.model import SpanRecord


@dataclass(slots=True)
class BufferedTrace:
    trace_id: str
    first_seen_nano: int
    deadline_nano: int
    spans: list[SpanRecord]


class TraceBuffer:
    def __init__(self, decision_wait_nano: int, max_traces: int) -> None:
        if decision_wait_nano <= 0 or max_traces < 1:
            raise ValueError("decision_wait_nano and max_traces must be positive")
        self._wait_nano = decision_wait_nano
        self._max_traces = max_traces
        self._traces: OrderedDict[str, BufferedTrace] = OrderedDict()
        self._latest_nano = 0
        self.span_count = 0

    def __len__(self) -> int:
        return len(self._traces)

    @property
    def over_capacity(self) -> bool:
        return len(self._traces) > self._max_traces

    def add(self, span: SpanRecord, now_nano: int) -> None:
        self._latest_nano = max(self._latest_nano, now_nano)
        trace = self._traces.get(span.trace_id)
        if trace is None:
            trace = BufferedTrace(
                trace_id=span.trace_id,
                first_seen_nano=self._latest_nano,
                deadline_nano=self._latest_nano + self._wait_nano,
                spans=[],
            )
            self._traces[span.trace_id] = trace
        trace.spans.append(span)
        self.span_count += 1

    def pop_due(self, now_nano: int) -> list[BufferedTrace]:
        self._latest_nano = max(self._latest_nano, now_nano)
        due: list[BufferedTrace] = []
        while self._traces:
            oldest = next(iter(self._traces.values()))
            if oldest.deadline_nano > self._latest_nano:
                break
            due.append(self.pop_oldest())
        return due

    def pop_oldest(self) -> BufferedTrace:
        _, trace = self._traces.popitem(last=False)
        self.span_count -= len(trace.spans)
        return trace

    def pop_all(self) -> list[BufferedTrace]:
        remaining = list(self._traces.values())
        self._traces.clear()
        self.span_count = 0
        return remaining
