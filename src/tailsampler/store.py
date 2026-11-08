import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from tailsampler.model import KeptTrace, SpanRecord, StatusCode

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT NOT NULL,
    service TEXT NOT NULL,
    name TEXT NOT NULL,
    kind INTEGER NOT NULL,
    start_unix_nano INTEGER NOT NULL,
    end_unix_nano INTEGER NOT NULL,
    status_code INTEGER NOT NULL,
    status_message TEXT NOT NULL,
    attributes TEXT NOT NULL,
    weight REAL NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (trace_id, span_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS spans_by_service_start ON spans (service, start_unix_nano);
"""

INSERT_SPAN = "INSERT OR IGNORE INTO spans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"

TRACE_FAILED = (
    "EXISTS (SELECT 1 FROM spans AS failing "
    "WHERE failing.trace_id = root.trace_id AND failing.status_code = 2)"
)


@dataclass(frozen=True, slots=True)
class StoredSpan:
    span: SpanRecord
    weight: float
    reason: str


@dataclass(frozen=True, slots=True)
class TraceSummary:
    trace_id: str
    service: str
    name: str
    start_unix_nano: int
    duration_nano: int
    weight: float
    reason: str
    failed: bool


@dataclass(frozen=True, slots=True)
class RouteEstimate:
    service: str
    name: str
    estimated_requests: float
    estimated_failed_requests: float
    stored_traces: int


class SpanStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def insert(self, kept: Iterable[KeptTrace]) -> int:
        rows = [
            (
                span.trace_id,
                span.span_id,
                span.parent_span_id,
                span.service,
                span.name,
                span.kind,
                span.start_unix_nano,
                span.end_unix_nano,
                int(span.status_code),
                span.status_message,
                json.dumps(span.attributes, separators=(",", ":"), sort_keys=True),
                item.weight,
                item.reason,
            )
            for item in kept
            for span in item.spans
        ]
        if not rows:
            return 0
        with self._lock:
            before = self._connection.total_changes
            self._connection.executemany(INSERT_SPAN, rows)
            self._connection.commit()
            return self._connection.total_changes - before

    def trace_spans(self, trace_id: str) -> list[StoredSpan]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT trace_id, span_id, parent_span_id, service, name, kind, start_unix_nano, "
                "end_unix_nano, status_code, status_message, attributes, weight, reason "
                "FROM spans WHERE trace_id = ? ORDER BY start_unix_nano, span_id",
                (trace_id,),
            ).fetchall()
        return [
            StoredSpan(
                span=SpanRecord(
                    trace_id=row[0],
                    span_id=row[1],
                    parent_span_id=row[2],
                    service=row[3],
                    name=row[4],
                    kind=row[5],
                    start_unix_nano=row[6],
                    end_unix_nano=row[7],
                    status_code=StatusCode(row[8]),
                    status_message=row[9],
                    attributes=json.loads(row[10]),
                ),
                weight=row[11],
                reason=row[12],
            )
            for row in rows
        ]

    def search(
        self,
        service: str | None = None,
        errors_only: bool = False,
        min_duration_nano: int = 0,
        limit: int = 20,
    ) -> list[TraceSummary]:
        conditions = ["root.parent_span_id = ''", "root.end_unix_nano - root.start_unix_nano >= ?"]
        parameters: list[object] = [min_duration_nano]
        if service is not None:
            conditions.append("root.service = ?")
            parameters.append(service)
        if errors_only:
            conditions.append(TRACE_FAILED)
        query = (
            "SELECT root.trace_id, root.service, root.name, root.start_unix_nano, "
            "root.end_unix_nano - root.start_unix_nano, root.weight, root.reason, "
            f"{TRACE_FAILED} FROM spans AS root WHERE {' AND '.join(conditions)} "
            "ORDER BY root.end_unix_nano - root.start_unix_nano DESC, root.trace_id LIMIT ?"
        )
        with self._lock:
            rows = self._connection.execute(query, [*parameters, limit]).fetchall()
        return [TraceSummary(*row[:7], failed=bool(row[7])) for row in rows]

    def estimates(
        self, since_unix_nano: int | None = None, until_unix_nano: int | None = None
    ) -> list[RouteEstimate]:
        conditions = ["root.parent_span_id = ''"]
        parameters: list[object] = []
        if since_unix_nano is not None:
            conditions.append("root.start_unix_nano >= ?")
            parameters.append(since_unix_nano)
        if until_unix_nano is not None:
            conditions.append("root.start_unix_nano < ?")
            parameters.append(until_unix_nano)
        query = (
            "SELECT root.service, root.name, SUM(root.weight), "
            f"SUM(CASE WHEN {TRACE_FAILED} THEN root.weight ELSE 0 END), COUNT(*) "
            f"FROM spans AS root WHERE {' AND '.join(conditions)} "
            "GROUP BY root.service, root.name ORDER BY root.service, root.name"
        )
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [RouteEstimate(*row) for row in rows]

    def span_count(self) -> int:
        with self._lock:
            return self._connection.execute("SELECT COUNT(*) FROM spans").fetchone()[0]

    def size_bytes(self) -> int:
        with self._lock:
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            pages = self._connection.execute("PRAGMA page_count").fetchone()[0]
            page_size = self._connection.execute("PRAGMA page_size").fetchone()[0]
        return pages * page_size
