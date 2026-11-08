import logging
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = "configs/gateway.toml"
CONFIG_PATH_VARIABLE = "TAILSAMPLER_CONFIG"
ENVIRONMENT_PREFIX = "TAILSAMPLER_"


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    host: str = "127.0.0.1"
    port: int = 4318
    database_path: str = "data/generated/spans.sqlite3"
    decision_wait_seconds: float = 10.0
    max_buffered_traces: int = 50000
    decision_cache_size: int = 200000
    baseline_rate: float = 0.05
    latency_thresholds_path: str = "results/thresholds.json"
    tick_seconds: float = 0.25
    log_level: str = "INFO"
    latency_quantile: float = 0.99
    min_calibration_samples: int = 200

    def __post_init__(self) -> None:
        if not 0 < self.latency_quantile < 1:
            raise ValueError(f"latency_quantile must be in (0, 1), got {self.latency_quantile}")
        if self.min_calibration_samples < 1:
            raise ValueError("min_calibration_samples must be at least 1")
        if not 1 <= self.port <= 65535:
            raise ValueError(f"port must be between 1 and 65535, got {self.port}")
        if not 0 < self.baseline_rate <= 1:
            raise ValueError(f"baseline_rate must be in (0, 1], got {self.baseline_rate}")
        if self.decision_wait_seconds <= 0:
            raise ValueError("decision_wait_seconds must be positive")
        if self.tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive")
        if self.max_buffered_traces < 1 or self.decision_cache_size < 1:
            raise ValueError("max_buffered_traces and decision_cache_size must be at least 1")
        if self.log_level.upper() not in logging.getLevelNamesMapping():
            raise ValueError(f"log_level {self.log_level!r} is not a logging level")


def load_gateway_settings(environ: Mapping[str, str] | None = None) -> GatewaySettings:
    environment = os.environ if environ is None else environ
    values = _file_values(environment)
    known = {item.name: item.type for item in fields(GatewaySettings)}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise ValueError(f"unknown settings in config file: {', '.join(unknown)}")
    for name in known:
        override = environment.get(ENVIRONMENT_PREFIX + name.upper())
        if override is not None:
            values[name] = override
    coerced = {name: _coerce(name, known[name], raw) for name, raw in values.items()}
    return GatewaySettings(**coerced)


def _file_values(environment: Mapping[str, str]) -> dict[str, Any]:
    explicit = environment.get(CONFIG_PATH_VARIABLE)
    path = Path(explicit if explicit is not None else DEFAULT_CONFIG_PATH)
    if explicit is None and not path.exists():
        return {}
    with path.open("rb") as handle:
        return dict(tomllib.load(handle).get("gateway", {}))


def _coerce(name: str, kind: type, raw: object) -> object:
    if isinstance(raw, str) and kind is not str:
        try:
            return kind(raw)
        except ValueError as error:
            raise ValueError(f"{name}: cannot read {raw!r} as {kind.__name__}") from error
    if kind is float and isinstance(raw, int) and not isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, bool) or not isinstance(raw, kind):
        raise ValueError(f"{name}: expected {kind.__name__}, got {type(raw).__name__}")
    return raw
