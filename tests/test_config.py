from pathlib import Path

import pytest

from tailsampler.config import GatewaySettings, load_gateway_settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_CONFIG = str(REPOSITORY_ROOT / "configs" / "gateway.toml")


def test_shipped_config_matches_the_dataclass_defaults():
    settings = load_gateway_settings({"TAILSAMPLER_CONFIG": GATEWAY_CONFIG})
    assert settings == GatewaySettings()


def test_environment_overrides_the_config_file_and_is_converted():
    settings = load_gateway_settings(
        {
            "TAILSAMPLER_CONFIG": GATEWAY_CONFIG,
            "TAILSAMPLER_PORT": "4999",
            "TAILSAMPLER_BASELINE_RATE": "0.2",
            "TAILSAMPLER_HOST": "0.0.0.0",
        }
    )
    assert settings.port == 4999
    assert settings.baseline_rate == 0.2
    assert settings.host == "0.0.0.0"


def test_integer_in_a_float_field_is_accepted(tmp_path):
    config = tmp_path / "gateway.toml"
    config.write_text("[gateway]\ndecision_wait_seconds = 3\n")
    settings = load_gateway_settings({"TAILSAMPLER_CONFIG": str(config)})
    assert settings.decision_wait_seconds == 3.0
    assert isinstance(settings.decision_wait_seconds, float)


def test_unknown_key_in_config_file_is_rejected(tmp_path):
    config = tmp_path / "gateway.toml"
    config.write_text("[gateway]\nprot = 1\n")
    with pytest.raises(ValueError, match="prot"):
        load_gateway_settings({"TAILSAMPLER_CONFIG": str(config)})


def test_wrong_type_in_config_file_is_rejected(tmp_path):
    config = tmp_path / "gateway.toml"
    config.write_text('[gateway]\nport = "high"\n')
    with pytest.raises(ValueError, match="port"):
        load_gateway_settings({"TAILSAMPLER_CONFIG": str(config)})


def test_unreadable_environment_value_is_rejected():
    with pytest.raises(ValueError, match="TAILSAMPLER_PORT|port"):
        load_gateway_settings({"TAILSAMPLER_CONFIG": GATEWAY_CONFIG, "TAILSAMPLER_PORT": "x"})


@pytest.mark.parametrize(
    "overrides",
    [
        {"baseline_rate": 0.0},
        {"baseline_rate": 1.5},
        {"port": 0},
        {"decision_wait_seconds": -1.0},
        {"tick_seconds": 0.0},
        {"max_buffered_traces": 0},
        {"log_level": "LOUD"},
        {"latency_quantile": 1.0},
        {"min_calibration_samples": 0},
    ],
)
def test_out_of_range_settings_are_rejected(overrides):
    with pytest.raises(ValueError):
        GatewaySettings(**overrides)


def test_missing_explicit_config_path_raises():
    with pytest.raises(FileNotFoundError):
        load_gateway_settings({"TAILSAMPLER_CONFIG": "/nonexistent/gateway.toml"})


def test_missing_default_config_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_gateway_settings({}) == GatewaySettings()
