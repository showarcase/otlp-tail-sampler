import pytest

from tailsampler.buffer import TraceBuffer

SECOND = 1_000_000_000
TRACE_A = "a" * 32
TRACE_B = "b" * 32
TRACE_C = "c" * 32


def test_spans_are_grouped_by_trace(make_span):
    buffer = TraceBuffer(decision_wait_nano=5 * SECOND, max_traces=10)
    buffer.add(make_span(trace_id=TRACE_A, span_id="1" * 16), 0)
    buffer.add(make_span(trace_id=TRACE_B, span_id="2" * 16), 0)
    buffer.add(make_span(trace_id=TRACE_A, span_id="3" * 16), SECOND)
    assert len(buffer) == 2
    assert buffer.span_count == 3
    (first, second) = buffer.pop_all()
    assert [span.span_id for span in first.spans] == ["1" * 16, "3" * 16]
    assert [span.span_id for span in second.spans] == ["2" * 16]


def test_trace_is_due_exactly_at_its_deadline(make_span):
    buffer = TraceBuffer(decision_wait_nano=5 * SECOND, max_traces=10)
    buffer.add(make_span(trace_id=TRACE_A), 2 * SECOND)
    assert buffer.pop_due(7 * SECOND - 1) == []
    (due,) = buffer.pop_due(7 * SECOND)
    assert due.trace_id == TRACE_A
    assert due.deadline_nano == 7 * SECOND
    assert len(buffer) == 0
    assert buffer.span_count == 0


def test_later_spans_do_not_extend_the_deadline(make_span):
    buffer = TraceBuffer(decision_wait_nano=5 * SECOND, max_traces=10)
    buffer.add(make_span(trace_id=TRACE_A, span_id="1" * 16), 0)
    buffer.add(make_span(trace_id=TRACE_A, span_id="2" * 16), 4 * SECOND)
    (due,) = buffer.pop_due(5 * SECOND)
    assert len(due.spans) == 2


def test_due_traces_come_out_in_first_seen_order(make_span):
    buffer = TraceBuffer(decision_wait_nano=SECOND, max_traces=10)
    for offset, trace_id in enumerate((TRACE_B, TRACE_A, TRACE_C)):
        buffer.add(make_span(trace_id=trace_id), offset * SECOND // 2)
    due = buffer.pop_due(10 * SECOND)
    assert [trace.trace_id for trace in due] == [TRACE_B, TRACE_A, TRACE_C]


def test_only_traces_past_their_deadline_are_returned(make_span):
    buffer = TraceBuffer(decision_wait_nano=SECOND, max_traces=10)
    buffer.add(make_span(trace_id=TRACE_A), 0)
    buffer.add(make_span(trace_id=TRACE_B), 3 * SECOND)
    assert [trace.trace_id for trace in buffer.pop_due(2 * SECOND)] == [TRACE_A]
    assert len(buffer) == 1


def test_overflow_gives_up_the_oldest_trace_first(make_span):
    buffer = TraceBuffer(decision_wait_nano=10 * SECOND, max_traces=2)
    for trace_id in (TRACE_A, TRACE_B):
        buffer.add(make_span(trace_id=trace_id), 0)
    assert not buffer.over_capacity
    buffer.add(make_span(trace_id=TRACE_C), SECOND)
    assert buffer.over_capacity
    assert buffer.pop_oldest().trace_id == TRACE_A
    assert not buffer.over_capacity
    assert buffer.span_count == 2


def test_clock_never_runs_backwards(make_span):
    buffer = TraceBuffer(decision_wait_nano=5 * SECOND, max_traces=10)
    buffer.add(make_span(trace_id=TRACE_A), 10 * SECOND)
    buffer.add(make_span(trace_id=TRACE_B), 4 * SECOND)
    first, second = buffer.pop_all()
    assert second.first_seen_nano == 10 * SECOND
    assert second.deadline_nano >= first.deadline_nano


def test_pop_all_empties_the_buffer(make_span):
    buffer = TraceBuffer(decision_wait_nano=SECOND, max_traces=10)
    buffer.add(make_span(trace_id=TRACE_A), 0)
    assert len(buffer.pop_all()) == 1
    assert len(buffer) == 0
    assert buffer.span_count == 0


@pytest.mark.parametrize(("wait", "capacity"), [(0, 5), (SECOND, 0)])
def test_invalid_limits_are_rejected(wait, capacity):
    with pytest.raises(ValueError):
        TraceBuffer(decision_wait_nano=wait, max_traces=capacity)
