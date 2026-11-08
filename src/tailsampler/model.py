from dataclasses import dataclass, field
from enum import IntEnum

type AttributeValue = str | int | float | bool


class StatusCode(IntEnum):
    UNSET = 0
    OK = 1
    ERROR = 2


@dataclass(frozen=True, slots=True)
class SpanRecord:
    trace_id: str
    span_id: str
    parent_span_id: str
    service: str
    name: str
    kind: int
    start_unix_nano: int
    end_unix_nano: int
    status_code: StatusCode = StatusCode.UNSET
    status_message: str = ""
    attributes: dict[str, AttributeValue] = field(default_factory=dict)

    @property
    def duration_nano(self) -> int:
        return self.end_unix_nano - self.start_unix_nano

    @property
    def is_root(self) -> bool:
        return not self.parent_span_id

    @property
    def is_error(self) -> bool:
        return self.status_code == StatusCode.ERROR


@dataclass(frozen=True, slots=True)
class KeptTrace:
    trace_id: str
    reason: str
    weight: float
    spans: tuple[SpanRecord, ...]
    late: bool = False


@dataclass(slots=True)
class SamplerCounters:
    spans_received: int = 0
    spans_kept: int = 0
    spans_dropped: int = 0
    traces_kept: int = 0
    traces_dropped: int = 0
    late_spans_kept: int = 0
    late_spans_dropped: int = 0
    forced_decisions: int = 0
    peak_buffered_traces: int = 0
    peak_buffered_spans: int = 0
    kept_by_reason: dict[str, int] = field(default_factory=dict)
