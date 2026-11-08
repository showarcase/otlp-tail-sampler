import argparse
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tailsampler.benchmark import StageTiming, read_timings

NANOS_PER_MILLISECOND = 1_000_000
BYTES_PER_MEBIBYTE = 1024**2
SAMPLER_ORDER = ("tail", "head", "keep_all")
BEGIN_LINE = re.compile(r"^<!-- results:begin (?P<name>[a-z_]+) -->$", re.MULTILINE)
BLOCK = re.compile(
    r"(?P<begin><!-- results:begin (?P<name>[a-z_]+) -->\n)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- results:end (?P=name) -->)",
    re.DOTALL,
)

logger = logging.getLogger("render_results")


@dataclass(frozen=True, slots=True)
class ResultFiles:
    metrics: dict[str, Any]
    timings: list[StageTiming]
    environment: dict[str, Any]
    thresholds: dict[str, Any]


def load_result_files(directory: Path) -> ResultFiles:
    return ResultFiles(
        metrics=json.loads((directory / "metrics.json").read_text(encoding="utf-8")),
        timings=read_timings(directory / "benchmarks.csv"),
        environment=json.loads((directory / "environment.json").read_text(encoding="utf-8")),
        thresholds=json.loads((directory / "thresholds.json").read_text(encoding="utf-8")),
    )


def markdown_table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], text_columns: int = 1
) -> str:
    divider = ["---"] * text_columns + ["---:"] * (len(headers) - text_columns)
    lines = [headers, divider, *rows]
    return "\n".join("| " + " | ".join(cells) + " |" for cells in lines)


def percent(fraction: float, digits: int = 1) -> str:
    return f"{fraction * 100:.{digits}f}%"


def count(value: float) -> str:
    return f"{round(value):,}"


def optional_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def signed_percent(value: float) -> str:
    rounded = round(value, 1)
    return "0.0%" if rounded == 0 else f"{rounded:+.1f}%"


def render_policy(files: ResultFiles) -> str:
    policy = files.metrics["policy"]
    rows = [
        ["baseline rate", percent(policy["baseline_rate"], 0)],
        ["decision wait (s)", f"{policy['decision_wait_seconds']:g}"],
        ["latency threshold quantile", f"{policy['latency_quantile']:g}"],
        ["routes with a fitted threshold", str(policy["routes_with_threshold"])],
        ["head sampling rate (matched to tail span share)", percent(policy["head_rate"], 2)],
        ["slow trace definition, per route quantile", f"{policy['slow_quantile']:g}"],
    ]
    return markdown_table(["Setting", "Value"], rows, text_columns=2)


def render_kept_reasons(files: ResultFiles) -> str:
    tail = files.metrics["samplers"]["tail"]
    total = tail["traces_kept"]
    rows = [
        [f"`{reason}`", count(traces), percent(traces / total)]
        for reason, traces in tail["kept_by_reason"].items()
    ]
    return markdown_table(["Tail keep reason", "Traces kept", "Share of kept traces"], rows)


def render_dataset(files: ResultFiles) -> str:
    rows = []
    for name in ("calibration", "evaluation"):
        summary = files.metrics["dataset"][name]
        rows.append(
            [
                name,
                str(summary["seed"]),
                f"{summary['duration_seconds']:g}",
                count(summary["traces"]),
                count(summary["spans"]),
                count(summary["requests"]),
                f"{summary['request_bytes'] / BYTES_PER_MEBIBYTE:.1f}",
                count(summary["error_traces"]),
                count(summary["rare_path_traces"]),
            ]
        )
    headers = [
        "Dataset",
        "Seed",
        "Seconds",
        "Traces",
        "Spans",
        "Export requests",
        "Request file (MiB)",
        "Error traces",
        "Rare path traces",
    ]
    return markdown_table(headers, rows)


def render_samplers(files: ResultFiles) -> str:
    rows = []
    for name in SAMPLER_ORDER:
        outcome = files.metrics["samplers"][name]
        rows.append(
            [
                name,
                count(outcome["spans_kept"]),
                percent(outcome["span_fraction"]),
                count(outcome["traces_kept"]),
                percent(outcome["trace_fraction"]),
                count(outcome["peak_buffered_spans"]),
            ]
        )
    headers = [
        "Sampler",
        "Spans kept",
        "Share of spans",
        "Traces kept",
        "Share of traces",
        "Peak buffered spans",
    ]
    return markdown_table(headers, rows)


def render_recall(files: ResultFiles) -> str:
    sizes = files.metrics["categories"]
    rows = []
    for category, traces in sizes.items():
        rows.append(
            [
                f"`{category}`",
                count(traces),
                *(
                    percent(files.metrics["samplers"][name]["recall"][category])
                    for name in SAMPLER_ORDER
                ),
            ]
        )
    return markdown_table(["Trace category", "Traces", *SAMPLER_ORDER], rows)


def render_estimate_summary(files: ResultFiles) -> str:
    rows = []
    for name in SAMPLER_ORDER:
        summary = files.metrics["estimates"][name]
        rows.append(
            [
                name,
                f"{summary['median_abs_request_error_percent']:.1f}%",
                f"{summary['max_abs_request_error_percent']:.1f}%",
                count(summary["true_failed_requests"]),
                count(summary["estimated_failed_requests"]),
                optional_percent(summary["max_abs_failed_error_percent"]),
            ]
        )
    headers = [
        "Sampler",
        "Median route error, requests",
        "Max route error, requests",
        "Failed requests, true",
        "Failed requests, estimated",
        "Max route error, failed requests",
    ]
    return markdown_table(headers, rows)


def render_estimate_routes(files: ResultFiles) -> str:
    tail_routes = files.metrics["estimates"]["tail"]["routes"]
    head_routes = files.metrics["estimates"]["head"]["routes"]
    rows = []
    for tail, head in zip(tail_routes, head_routes, strict=True):
        rows.append(
            [
                f"{tail['service']} {tail['name']}",
                count(tail["true_requests"]),
                count(tail["estimated_requests"]),
                signed_percent(tail["request_error_percent"]),
                count(head["estimated_requests"]),
                signed_percent(head["request_error_percent"]),
                count(tail["true_failed_requests"]),
                count(tail["estimated_failed_requests"]),
                count(head["estimated_failed_requests"]),
            ]
        )
    headers = [
        "Route",
        "True requests",
        "Tail estimate",
        "Tail error",
        "Head estimate",
        "Head error",
        "True failed",
        "Tail failed",
        "Head failed",
    ]
    return markdown_table(headers, rows)


def render_decision_wait_sweep(files: ResultFiles) -> str:
    rows = []
    for point in files.metrics["sweeps"]["decision_wait"]:
        tail = point["tail"]
        rows.append(
            [
                f"{point['decision_wait_seconds']:g}",
                percent(tail["recall"]["error"]),
                percent(tail["recall"]["slow"]),
                count(tail["late_spans_dropped"]),
                count(tail["late_spans_kept"]),
                count(tail["peak_buffered_spans"]),
                count(tail["peak_buffered_traces"]),
            ]
        )
    headers = [
        "Decision wait (s)",
        "Error recall",
        "Slow recall",
        "Late spans dropped",
        "Late spans kept",
        "Peak buffered spans",
        "Peak buffered traces",
    ]
    return markdown_table(headers, rows)


def render_baseline_rate_sweep(files: ResultFiles) -> str:
    rows = []
    for point in files.metrics["sweeps"]["baseline_rate"]:
        tail, head = point["tail"], point["head"]
        rows.append(
            [
                percent(point["baseline_rate"], 0),
                percent(tail["span_fraction"]),
                percent(head["span_fraction"]),
                percent(tail["recall"]["error"]),
                percent(head["recall"]["error"]),
                percent(tail["recall"]["slow"]),
                percent(head["recall"]["slow"]),
            ]
        )
    headers = [
        "Tail baseline rate",
        "Tail spans kept",
        "Head spans kept",
        "Tail error recall",
        "Head error recall",
        "Tail slow recall",
        "Head slow recall",
    ]
    return markdown_table(headers, rows)


def render_benchmarks(files: ResultFiles) -> str:
    rows = [
        [
            f"`{timing.stage}`",
            count(timing.calls),
            count(timing.spans),
            f"{timing.seconds:.3f}",
            count(timing.spans_per_second),
            f"{timing.p50_microseconds:.1f}",
            f"{timing.p99_microseconds:.1f}",
        ]
        for timing in files.timings
    ]
    headers = [
        "Stage",
        "Calls",
        "Spans",
        "Seconds",
        "Spans per second",
        "p50 per call (µs)",
        "p99 per call (µs)",
    ]
    return markdown_table(headers, rows)


def render_environment(files: ResultFiles) -> str:
    environment = files.environment
    packages = environment["packages"]
    rows = [
        ["Python", f"{environment['python_implementation']} {environment['python']}"],
        ["Platform", environment["platform"]],
        ["CPU", f"{environment['cpu_model']}, {environment['cpu_count']} logical cores"],
        ["Memory (GiB)", f"{environment['memory_gib']:g}"],
        ["SQLite", environment["sqlite"]],
        *[[name, version] for name, version in sorted(packages.items())],
    ]
    return markdown_table(["Item", "Value"], rows, text_columns=2)


def render_thresholds(files: ResultFiles) -> str:
    thresholds = files.thresholds["thresholds_nano"]
    samples = files.thresholds["samples"]
    rows = [
        [route, f"{nanos / NANOS_PER_MILLISECOND:.2f}", count(samples[route])]
        for route, nanos in sorted(thresholds.items())
    ]
    return markdown_table(["Route", "Latency threshold (ms)", "Calibration traces"], rows)


BLOCK_RENDERERS: dict[str, Callable[[ResultFiles], str]] = {
    "policy": render_policy,
    "dataset": render_dataset,
    "samplers": render_samplers,
    "kept_reasons": render_kept_reasons,
    "recall": render_recall,
    "estimate_summary": render_estimate_summary,
    "estimate_routes": render_estimate_routes,
    "decision_wait_sweep": render_decision_wait_sweep,
    "baseline_rate_sweep": render_baseline_rate_sweep,
    "benchmarks": render_benchmarks,
    "environment": render_environment,
    "thresholds": render_thresholds,
}


def fill_blocks(text: str, blocks: Mapping[str, str]) -> str:
    def replacement(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in blocks:
            raise ValueError(f"unknown results block {name!r}")
        return f"{match.group('begin')}\n{blocks[name]}\n\n{match.group('end')}"

    filled, replaced = BLOCK.subn(replacement, text)
    if replaced != len(BEGIN_LINE.findall(text)):
        raise ValueError("a results begin marker has no matching end marker")
    return filled


def markdown_files(root: Path) -> list[Path]:
    candidates = [root / "README.md", *sorted((root / "docs").rglob("*.md"))]
    return [path for path in candidates if path.is_file()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="fill results blocks in the markdown files")
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--check", action="store_true", help="fail if a file is out of date")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    arguments = build_parser().parse_args(argv)
    root = Path(arguments.root)
    files = load_result_files(root / "results")
    blocks = {name: render(files) for name, render in BLOCK_RENDERERS.items()}
    outdated: list[Path] = []
    for path in markdown_files(root):
        text = path.read_text(encoding="utf-8")
        filled = fill_blocks(text, blocks)
        if filled == text:
            continue
        outdated.append(path)
        if not arguments.check:
            path.write_text(filled, encoding="utf-8")
            logger.info("updated %s", path)
    if arguments.check and outdated:
        logger.error("out of date, run make evaluate: %s", ", ".join(map(str, outdated)))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
