import argparse
import http.client
import logging
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn

from tailsampler.api import create_app
from tailsampler.config import load_gateway_settings
from tailsampler.evaluation import run_evaluation
from tailsampler.fleet import load_simulation
from tailsampler.model import SpanRecord
from tailsampler.otlp import decode_export_request
from tailsampler.policies import fit_latency_thresholds, load_thresholds, write_thresholds
from tailsampler.traffic import read_requests, write_datasets

DEFAULT_SIMULATION_CONFIG = "configs/simulation.toml"
DEFAULT_DATA_DIRECTORY = "data/generated"
DEFAULT_CALIBRATION_REQUESTS = f"{DEFAULT_DATA_DIRECTORY}/calibration.requests"
DEFAULT_EVALUATION_REQUESTS = f"{DEFAULT_DATA_DIRECTORY}/evaluation.requests"
DEFAULT_RESULTS_DIRECTORY = "results"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:4318/v1/traces"
REPLAY_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tailsampler")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser(
        "generate", help="write the calibration and evaluation traffic files"
    )
    generate.add_argument("--config", default=DEFAULT_SIMULATION_CONFIG)
    generate.add_argument("--output", default=DEFAULT_DATA_DIRECTORY)
    generate.set_defaults(run=run_generate)
    train = commands.add_parser("train", help="fit per-route latency thresholds")
    train.add_argument("--requests", default=DEFAULT_CALIBRATION_REQUESTS)
    train.set_defaults(run=run_train)
    serve = commands.add_parser("serve", help="run the gateway")
    serve.set_defaults(run=run_serve)
    replay = commands.add_parser("replay", help="post generated requests to a running gateway")
    replay.add_argument("--requests", default=DEFAULT_EVALUATION_REQUESTS)
    replay.add_argument("--url", default=DEFAULT_GATEWAY_URL)
    replay.add_argument("--limit", type=int, default=0, help="stop after this many, 0 means all")
    replay.set_defaults(run=run_replay)
    evaluate = commands.add_parser(
        "evaluate", help="score the samplers on the generated evaluation traffic"
    )
    evaluate.add_argument("--simulation", default=DEFAULT_SIMULATION_CONFIG)
    evaluate.add_argument("--data", default=DEFAULT_DATA_DIRECTORY)
    evaluate.add_argument("--output", default=DEFAULT_RESULTS_DIRECTORY)
    evaluate.set_defaults(run=run_evaluate)
    return parser


def run_generate(arguments: argparse.Namespace) -> int:
    simulation = load_simulation(arguments.config)
    write_datasets(simulation, Path(arguments.output))
    return 0


def run_train(arguments: argparse.Namespace) -> int:
    settings = load_gateway_settings()
    thresholds = fit_latency_thresholds(
        _root_spans(Path(arguments.requests)),
        settings.latency_quantile,
        settings.min_calibration_samples,
    )
    destination = Path(settings.latency_thresholds_path)
    write_thresholds(thresholds, destination)
    logger.info(
        "fitted %d route thresholds at quantile %s from %d root spans, wrote %s",
        len(thresholds.thresholds_nano),
        thresholds.quantile,
        sum(thresholds.samples.values()),
        destination,
    )
    return 0


def run_serve(arguments: argparse.Namespace) -> int:
    settings = load_gateway_settings()
    thresholds_path = Path(settings.latency_thresholds_path)
    if not thresholds_path.exists():
        logger.error("%s not found, run make train first", thresholds_path)
        return 1
    logging.getLogger().setLevel(settings.log_level.upper())
    app = create_app(settings, load_thresholds(thresholds_path))
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    return 0


def run_replay(arguments: argparse.Namespace) -> int:
    target = urlsplit(arguments.url)
    connection = http.client.HTTPConnection(
        target.hostname or "127.0.0.1", target.port or 80, timeout=REPLAY_TIMEOUT_SECONDS
    )
    sent = rejected = 0
    started = time.perf_counter()
    try:
        for index, (_, body) in enumerate(read_requests(Path(arguments.requests))):
            if arguments.limit and index >= arguments.limit:
                break
            connection.request(
                "POST", target.path or "/", body=body, headers={"Content-Type": "application/json"}
            )
            response = connection.getresponse()
            response.read()
            sent += 1
            rejected += response.status != 200
    except (OSError, http.client.HTTPException) as error:
        logger.error("replay stopped after %d requests: %s", sent, error)
        return 1
    finally:
        connection.close()
    logger.info(
        "replayed %d requests, %d rejected, %.1f seconds",
        sent,
        rejected,
        time.perf_counter() - started,
    )
    return 1 if rejected else 0


def run_evaluate(arguments: argparse.Namespace) -> int:
    run_evaluation(
        Path(arguments.simulation),
        Path(arguments.data),
        Path(arguments.output),
    )
    return 0


def _root_spans(requests_path: Path) -> Iterator[SpanRecord]:
    for _, body in read_requests(requests_path):
        for span in decode_export_request(body):
            if span.is_root:
                yield span


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return arguments.run(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
