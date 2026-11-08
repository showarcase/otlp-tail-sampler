import json
import socket
import threading
import urllib.request
from urllib.parse import urlunsplit

import pytest
import uvicorn

from conftest import wait_until
from tailsampler.api import create_app
from tailsampler.cli import main
from tailsampler.config import GatewaySettings
from tailsampler.policies import load_thresholds
from tailsampler.traffic import read_truths


def loopback_url(port: int, path: str = "") -> str:
    return urlunsplit(("http", f"127.0.0.1:{port}", path, "", ""))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode()


@pytest.fixture
def running_gateway(tiny_simulation_path, tmp_path, monkeypatch):
    output = tmp_path / "generated"
    thresholds_path = tmp_path / "thresholds.json"
    monkeypatch.setenv("TAILSAMPLER_LATENCY_THRESHOLDS_PATH", str(thresholds_path))
    monkeypatch.setenv("TAILSAMPLER_MIN_CALIBRATION_SAMPLES", "5")
    assert main(["generate", "--config", str(tiny_simulation_path), "--output", str(output)]) == 0
    assert main(["train", "--requests", str(output / "calibration.requests")]) == 0
    settings = GatewaySettings(
        decision_wait_seconds=0.5,
        tick_seconds=0.05,
        baseline_rate=0.5,
        database_path=str(tmp_path / "spans.sqlite3"),
        latency_thresholds_path=str(thresholds_path),
    )
    app = create_app(settings, load_thresholds(thresholds_path))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until(lambda: server.started)
    yield loopback_url(port), output
    server.should_exit = True
    thread.join(timeout=10)


def test_generate_train_serve_replay_and_query(running_gateway):
    base_url, output = running_gateway
    exit_code = main(
        [
            "replay",
            "--requests",
            str(output / "evaluation.requests"),
            "--url",
            f"{base_url}/v1/traces",
        ]
    )
    assert exit_code == 0
    wait_until(lambda: "tailsampler_buffered_traces 0\n" in fetch(f"{base_url}/metrics"))
    truths = read_truths(output / "evaluation.truth.jsonl")
    error_ids = {trace_id for trace_id, truth in truths.items() if truth.error}
    assert error_ids
    found = json.loads(fetch(f"{base_url}/v1/traces?errors_only=true&limit=200"))["traces"]
    assert {item["trace_id"] for item in found} == error_ids
    total_spans = sum(truth.span_count for truth in truths.values())
    assert f"tailsampler_spans_received_total {total_spans}\n" in fetch(f"{base_url}/metrics")
    estimates = json.loads(fetch(f"{base_url}/v1/estimates"))
    assert estimates["total_estimated_requests"] > 0


def test_replay_reports_an_unreachable_gateway(tiny_simulation_path, tmp_path):
    output = tmp_path / "generated"
    main(["generate", "--config", str(tiny_simulation_path), "--output", str(output)])
    url = loopback_url(free_port(), "/v1/traces")
    assert main(["replay", "--requests", str(output / "evaluation.requests"), "--url", url]) == 1


def test_serve_refuses_to_start_without_fitted_thresholds(tmp_path, monkeypatch):
    monkeypatch.setenv("TAILSAMPLER_LATENCY_THRESHOLDS_PATH", str(tmp_path / "missing.json"))
    assert main(["serve"]) == 1
