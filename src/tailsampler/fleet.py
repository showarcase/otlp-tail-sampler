import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NANOS_PER_MILLI = 1_000_000
INCIDENT_KINDS = ("latency", "errors")


@dataclass(frozen=True, slots=True)
class Call:
    operation: str
    probability: float = 1.0
    background: bool = False


@dataclass(frozen=True, slots=True)
class Operation:
    name: str
    service: str
    span_name: str
    self_median_ms: float
    self_sigma: float
    error_rate: float = 0.0
    entrypoint_weight: float = 0.0
    rare: bool = False
    calls: tuple[Call, ...] = ()


@dataclass(frozen=True, slots=True)
class Incident:
    name: str
    kind: str
    operation: str
    start_second: float
    end_second: float
    probability: float
    added_median_ms: float = 0.0
    added_sigma: float = 0.0


@dataclass(frozen=True, slots=True)
class Fleet:
    operations: dict[str, Operation]
    incidents: tuple[Incident, ...]
    export_intervals_ms: dict[str, int]
    default_export_interval_ms: int
    network_delay_ms: tuple[int, int]
    propagate_error_probability: float
    start_unix_nano: int

    @property
    def entrypoints(self) -> list[Operation]:
        return [item for item in self.operations.values() if item.entrypoint_weight > 0]

    @property
    def services(self) -> list[str]:
        return sorted({item.service for item in self.operations.values()})

    def export_interval_nano(self, service: str) -> int:
        interval_ms = self.export_intervals_ms.get(service, self.default_export_interval_ms)
        return interval_ms * NANOS_PER_MILLI


@dataclass(frozen=True, slots=True)
class TrafficPlan:
    seed: int
    duration_seconds: float
    requests_per_second: float
    incidents: bool


@dataclass(frozen=True, slots=True)
class Simulation:
    fleet: Fleet
    calibration: TrafficPlan
    evaluation: TrafficPlan


def load_simulation(path: str | Path) -> Simulation:
    with Path(path).open("rb") as handle:
        document = tomllib.load(handle)
    traffic = document.get("traffic", {})
    operations: dict[str, Operation] = {}
    for raw in document.get("operation", []):
        operation = _operation(raw)
        if operation.name in operations:
            raise ValueError(f"duplicate operation {operation.name}")
        operations[operation.name] = operation
    fleet = Fleet(
        operations=operations,
        incidents=tuple(_build(Incident, raw, "incident") for raw in document.get("incident", [])),
        export_intervals_ms={
            str(entry["service"]): int(entry["interval_ms"])
            for entry in document.get("exporter", [])
        },
        default_export_interval_ms=int(traffic["default_export_interval_ms"]),
        network_delay_ms=_delay_range(traffic["network_delay_ms"]),
        propagate_error_probability=float(traffic["propagate_error_probability"]),
        start_unix_nano=int(traffic["start_unix_nano"]),
    )
    _validate_fleet(fleet)
    return Simulation(
        fleet=fleet,
        calibration=_plan(document, "calibration_traffic"),
        evaluation=_plan(document, "evaluation_traffic"),
    )


def _build[Item](kind: type[Item], raw: dict[str, Any], context: str) -> Item:
    try:
        return kind(**raw)
    except TypeError as error:
        raise ValueError(f"{context}: {error}") from error


def _operation(raw: dict[str, Any]) -> Operation:
    name = raw.get("name", "?")
    calls = tuple(_build(Call, call, f"operation {name} call") for call in raw.get("calls", []))
    return _build(Operation, {**raw, "calls": calls}, f"operation {name}")


def _delay_range(raw: list[int]) -> tuple[int, int]:
    if len(raw) != 2 or not 0 <= raw[0] <= raw[1]:
        raise ValueError("network_delay_ms must be two integers, low then high")
    return raw[0], raw[1]


def _plan(document: dict[str, Any], table: str) -> TrafficPlan:
    plan = _build(TrafficPlan, dict(document.get(table, {})), table)
    if plan.duration_seconds <= 0 or plan.requests_per_second <= 0:
        raise ValueError(f"{table}: duration_seconds and requests_per_second must be positive")
    return plan


def _validate_fleet(fleet: Fleet) -> None:
    if not fleet.entrypoints:
        raise ValueError("no entrypoint operation has a positive entrypoint_weight")
    if not 0 <= fleet.propagate_error_probability <= 1:
        raise ValueError("propagate_error_probability must be between 0 and 1")
    for operation in fleet.operations.values():
        if operation.self_median_ms <= 0 or operation.self_sigma < 0:
            raise ValueError(f"{operation.name}: latency model needs a positive median")
        if not 0 <= operation.error_rate <= 1 or operation.entrypoint_weight < 0:
            raise ValueError(f"{operation.name}: error_rate or entrypoint_weight out of range")
        for call in operation.calls:
            if call.operation not in fleet.operations:
                raise ValueError(f"{operation.name} calls unknown operation {call.operation}")
            if not 0 <= call.probability <= 1:
                raise ValueError(f"{operation.name}: call probability must be between 0 and 1")
    for incident in fleet.incidents:
        _validate_incident(fleet, incident)
    for service, interval_ms in fleet.export_intervals_ms.items():
        if service not in fleet.services or interval_ms <= 0:
            raise ValueError(f"exporter for unknown service or bad interval: {service}")
    _reject_cycles(fleet)


def _validate_incident(fleet: Fleet, incident: Incident) -> None:
    if incident.kind not in INCIDENT_KINDS:
        raise ValueError(f"incident {incident.name}: kind must be one of {INCIDENT_KINDS}")
    if incident.operation not in fleet.operations:
        raise ValueError(f"incident {incident.name} targets unknown operation")
    if not 0 <= incident.probability <= 1:
        raise ValueError(f"incident {incident.name}: probability must be between 0 and 1")
    if not 0 <= incident.start_second < incident.end_second:
        raise ValueError(f"incident {incident.name}: window must have start before end")
    if incident.kind == "latency" and incident.added_median_ms <= 0:
        raise ValueError(f"incident {incident.name}: latency incident needs added_median_ms")


def _reject_cycles(fleet: Fleet) -> None:
    finished: set[str] = set()

    def visit(name: str, path: tuple[str, ...]) -> None:
        if name in path:
            raise ValueError(f"call graph has a cycle: {' -> '.join((*path, name))}")
        if name in finished:
            return
        for call in fleet.operations[name].calls:
            visit(call.operation, (*path, name))
        finished.add(name)

    for name in fleet.operations:
        visit(name, ())
