import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tailsampler.cli import main
from tailsampler.fleet import Simulation, load_simulation
from tailsampler.model import SpanRecord

BASE_START_NANO = 1_700_000_000_000_000_000
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
POLL_SECONDS = 0.01


def wait_until(check: Callable[[], bool], timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met before the timeout")
        time.sleep(POLL_SECONDS)


TINY_SIMULATION = """\
[traffic]
start_unix_nano = 1_700_000_000_000_000_000
propagate_error_probability = 0.5
network_delay_ms = [1, 5]
default_export_interval_ms = 200

[calibration_traffic]
seed = 1
duration_seconds = 5
requests_per_second = 20.0
incidents = false

[evaluation_traffic]
seed = 2
duration_seconds = 5
requests_per_second = 20.0
incidents = true

[evaluation]
slow_quantile = 0.9
decision_wait_seconds = [1.0, 2.0]
baseline_rates = [0.1, 0.5]
benchmark_repeats = 1

[[operation]]
name = "web.index"
service = "web"
span_name = "GET /"
self_median_ms = 2.0
self_sigma = 0.3
entrypoint_weight = 1.0
calls = [{ operation = "store.read" }]

[[operation]]
name = "store.read"
service = "store"
span_name = "read"
self_median_ms = 1.0
self_sigma = 0.3
error_rate = 0.05

[[incident]]
name = "slow-store"
kind = "latency"
operation = "store.read"
start_second = 1
end_second = 4
probability = 1.0
added_median_ms = 20.0
added_sigma = 0.2
"""


@pytest.fixture(scope="session")
def tiny_results(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("tiny-pipeline")
    simulation_path = root / "simulation.toml"
    simulation_path.write_text(TINY_SIMULATION, encoding="utf-8")
    results = root / "results"
    traffic_directory = root / "generated"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TAILSAMPLER_LATENCY_THRESHOLDS_PATH", str(results / "thresholds.json"))
        patch.setenv("TAILSAMPLER_MIN_CALIBRATION_SAMPLES", "5")
        patch.setenv("TAILSAMPLER_DECISION_WAIT_SECONDS", "1.0")
        assert (
            main(["generate", "--config", str(simulation_path), "--output", str(traffic_directory)])
            == 0
        )
        assert main(["train", "--requests", str(traffic_directory / "calibration.requests")]) == 0
        arguments = ["--simulation", str(simulation_path)]
        arguments += ["--data", str(traffic_directory), "--output", str(results)]
        assert main(["evaluate", *arguments]) == 0
    return results


@pytest.fixture
def make_span() -> Callable[..., SpanRecord]:
    def build(**overrides: Any) -> SpanRecord:
        fields: dict[str, Any] = {
            "trace_id": "0123456789abcdef0123456789abcdef",
            "span_id": "0123456789abcdef",
            "parent_span_id": "",
            "service": "checkout",
            "name": "POST /checkout",
            "kind": 2,
            "start_unix_nano": BASE_START_NANO,
            "end_unix_nano": BASE_START_NANO + 50_000_000,
        }
        fields.update(overrides)
        return SpanRecord(**fields)

    return build


@pytest.fixture(scope="session")
def simulation() -> Simulation:
    return load_simulation(REPOSITORY_ROOT / "configs" / "simulation.toml")


@pytest.fixture
def tiny_simulation_path(tmp_path: Path) -> Path:
    path = tmp_path / "tiny.toml"
    path.write_text(TINY_SIMULATION, encoding="utf-8")
    return path
