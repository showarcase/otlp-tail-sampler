import argparse
import json
import logging
import math
import re
import shutil
import string
import struct
import subprocess
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import matplotlib
from matplotlib.figure import Figure
from matplotlib.patches import FancyBboxPatch
from matplotlib.ticker import FuncFormatter, NullFormatter, NullLocator
from PIL import Image

from render_results import ResultFiles, load_result_files

THEME_PATH = Path("docs/diagrams/theme.json")
DIAGRAM_DIRECTORY = Path("docs/diagrams")
IMAGE_DIRECTORY = Path("docs/images")
RESULT_DIRECTORY = Path("results")
MAXIMUM_IMAGE_BYTES = 1_500_000
BASE_DOT_DPI = 150
MAXIMUM_DOT_ATTEMPTS = 3
PNG_HEADER_LENGTH = 24
SPLINES = {"polyline": "polyline", "orthogonal": "ortho", "curved": "curved", "straight": "line"}
CATEGORY_LABEL_WIDTH = 14
NANOS_PER_MICROSECOND = 1_000

logger = logging.getLogger("render_figures")


@dataclass(frozen=True, slots=True)
class Theme:
    colors: dict[str, str]
    fonts: dict[str, str]
    shapes: dict[str, Any]
    charts: dict[str, Any]
    minimum_long_side: int


@dataclass(frozen=True, slots=True)
class SequenceParticipant:
    key: str
    label: str


@dataclass(frozen=True, slots=True)
class SequenceMessage:
    sender: str
    receiver: str
    label: str
    symbol: str
    is_return: bool = False
    phase: str = ""


SEQUENCE_PARTICIPANTS = (
    SequenceParticipant("exporter", "OTLP exporter"),
    SequenceParticipant("api", "api.py"),
    SequenceParticipant("otlp", "otlp.py"),
    SequenceParticipant("sampler", "sampler.py\nTailSampler"),
    SequenceParticipant("buffer", "buffer.py\nTraceBuffer"),
    SequenceParticipant("policies", "policies.py"),
    SequenceParticipant("store", "store.py\nSpanStore"),
    SequenceParticipant("client", "query client"),
)

SEQUENCE_MESSAGES = (
    SequenceMessage("exporter", "api", "POST /v1/traces, OTLP JSON", "", phase="ingest"),
    SequenceMessage("api", "otlp", "decode_export_request(body)", "decode_export_request"),
    SequenceMessage("otlp", "api", "SpanRecord list", "", is_return=True),
    SequenceMessage("api", "sampler", "ingest(spans, now)", "ingest"),
    SequenceMessage("sampler", "buffer", "add(span), once per span", "add"),
    SequenceMessage("api", "exporter", "200 with an empty body", "", is_return=True),
    SequenceMessage("api", "sampler", "tick: advance(now)", "advance", phase="decision"),
    SequenceMessage("sampler", "buffer", "pop_due(now)", "pop_due"),
    SequenceMessage("buffer", "sampler", "traces past their deadline", "", is_return=True),
    SequenceMessage("sampler", "policies", "decide(policy, trace_id, spans)", "decide"),
    SequenceMessage("policies", "sampler", "Decision(keep, reason, weight)", "", is_return=True),
    SequenceMessage("sampler", "api", "KeptTrace list", "", is_return=True),
    SequenceMessage("api", "store", "insert(kept)", "insert"),
    SequenceMessage("client", "api", "GET /v1/traces?errors_only=true", "", phase="query"),
    SequenceMessage("api", "store", "search(errors_only=True)", "search"),
    SequenceMessage("store", "api", "TraceSummary list", "", is_return=True),
    SequenceMessage("api", "client", "JSON traces with weight and reason", "", is_return=True),
)


def load_theme(path: Path) -> Theme:
    document = json.loads(path.read_text(encoding="utf-8"))
    return Theme(
        colors=document["colors"],
        fonts=document["fonts"],
        shapes=document["shapes"],
        charts=document["charts"],
        minimum_long_side=document["output"]["min_long_side_px"],
    )


def format_attributes(attributes: Mapping[str, str]) -> str:
    return ", ".join(f'{name}="{value}"' for name, value in attributes.items())


def dot_variables(theme: Theme) -> dict[str, str]:
    colors, fonts, shapes = theme.colors, theme.fonts, theme.shapes
    width = shapes["border_width"]
    failure = shapes["failure_path_style"]
    graph_defaults = {
        "bgcolor": "transparent",
        "fontname": fonts["body"],
        "fontcolor": colors["muted"],
        "fontsize": "22",
        "pad": "0.5",
        "nodesep": "0.5",
        "ranksep": "0.9",
        "splines": SPLINES[shapes["edge_style"]],
        "rankdir": shapes["flow_direction"],
        "labeljust": "l",
    }
    node_defaults = {
        "shape": "box",
        "style": "rounded",
        "color": colors["border"],
        "fontname": fonts["mono"],
        "fontcolor": colors["text"],
        "fontsize": "16",
        "penwidth": str(width),
        "margin": "0.24,0.14",
    }
    edge_defaults = {
        "color": colors["muted"],
        "fontname": fonts["body"],
        "fontcolor": colors["muted"],
        "fontsize": "15",
        "penwidth": str(width),
        "arrowhead": shapes["arrowhead"],
        "arrowtail": shapes["arrowhead"],
        "arrowsize": "1.0",
    }
    roles = {
        "component": {"color": colors["primary"]},
        "support": {"color": colors["secondary"]},
        "artifact": {"shape": "note", "color": colors["muted"]},
        "datastore": {"shape": "cylinder", "color": colors["accent"]},
        "external": {"color": colors["muted"], "fontcolor": colors["muted"]},
        "step": {"color": colors["primary"], "fontname": fonts["body"]},
        "decision": {"shape": "diamond", "color": colors["secondary"], "fontname": fonts["body"]},
        "keep": {"color": colors["accent"], "fontname": fonts["body"]},
        "drop": {
            "style": f"rounded,{failure}",
            "color": colors["error"],
            "fontname": fonts["body"],
        },
        "cluster": {
            "style": "rounded",
            "color": colors["border"],
            "fontcolor": colors["muted"],
            "labelloc": "b",
        },
        "cluster_top": {
            "style": "rounded",
            "color": colors["border"],
            "fontcolor": colors["muted"],
            "labelloc": "t",
        },
        "failure_edge": {
            "style": failure,
            "color": colors["error"],
            "fontcolor": colors["error"],
        },
    }
    defaults = "\n".join(
        [
            f"graph [{format_attributes(graph_defaults)}];",
            f"node [{format_attributes(node_defaults)}];",
            f"edge [{format_attributes(edge_defaults)}];",
        ]
    )
    return {
        **colors,
        "font_body": fonts["body"],
        "font_mono": fonts["mono"],
        "defaults": defaults,
        **{name: format_attributes(attributes) for name, attributes in roles.items()},
    }


def png_dimensions(content: bytes) -> tuple[int, int]:
    if content[:8] != b"\x89PNG\r\n\x1a\n" or len(content) < PNG_HEADER_LENGTH:
        raise ValueError("not a PNG image")
    width, height = struct.unpack(">II", content[16:PNG_HEADER_LENGTH])
    return width, height


def run_dot(source: str, dpi: int) -> bytes:
    try:
        completed = subprocess.run(
            ["dot", "-Tpng", f"-Gdpi={dpi}"],
            input=source.encode("utf-8"),
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(error.stderr.decode("utf-8", errors="replace")) from error
    return completed.stdout


EDGE_LABEL_PATTERN = re.compile(
    r'^(?P<head>[^\n]*->[^\n]*\[label=")(?P<text>[^"]*)(?P<tail>")', re.MULTILINE
)


def pad_edge_labels(source: str) -> str:
    def pad(match: re.Match[str]) -> str:
        lines = match.group("text").split("\\n")
        padded = "\\n".join(f"  {line}  " for line in lines)
        return f"{match.group('head')}{padded}{match.group('tail')}"

    return EDGE_LABEL_PATTERN.sub(pad, source)


def flatten_on_background(content: bytes, background: str) -> bytes:
    image = Image.open(BytesIO(content)).convert("RGBA")
    canvas = Image.new("RGBA", image.size, background)
    canvas.alpha_composite(image)
    buffer = BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_dot(source: Path, theme: Theme) -> bytes:
    template = string.Template(source.read_text(encoding="utf-8"))
    text = pad_edge_labels(template.substitute(dot_variables(theme)))
    dpi = BASE_DOT_DPI
    image = run_dot(text, dpi)
    for _ in range(MAXIMUM_DOT_ATTEMPTS):
        long_side = max(png_dimensions(image))
        if long_side >= theme.minimum_long_side:
            break
        dpi = math.ceil(dpi * theme.minimum_long_side / long_side * 1.02)
        image = run_dot(text, dpi)
    return flatten_on_background(image, theme.colors["background"])


def save_figure(figure: Figure, theme: Theme) -> bytes:
    long_inches = max(figure.get_size_inches())
    dpi = math.ceil(theme.minimum_long_side / long_inches)
    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=dpi, facecolor=theme.colors["background"])
    return buffer.getvalue()


def chart_style(theme: Theme) -> dict[str, Any]:
    return {
        "font.family": theme.fonts["body"],
        "text.color": theme.colors["text"],
        "axes.labelcolor": theme.colors["muted"],
        "xtick.color": theme.colors["muted"],
        "ytick.color": theme.colors["muted"],
    }


def style_axes(axes: Any, theme: Theme) -> None:
    colors = theme.colors
    axes.set_facecolor(colors["background"])
    for side in ("top", "left", "right"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(colors["border"])
    axes.spines["bottom"].set_linewidth(theme.shapes["border_width"])
    axes.tick_params(length=0, labelsize=14)
    axes.yaxis.grid(True, color=colors["border"], linewidth=1.0)
    axes.set_axisbelow(True)


def add_headline(figure: Figure, theme: Theme, title: str, subtitle: str) -> None:
    figure.text(0.04, 0.96, title, ha="left", va="top", fontsize=24, color=theme.colors["text"])
    figure.text(
        0.04, 0.9, subtitle, ha="left", va="top", fontsize=14.5, color=theme.colors["muted"]
    )


def percent_text(value: float) -> str:
    return f"{value:.0f}%" if value >= 99.95 else f"{value:.1f}%"


def thousands(value: float, _position: int) -> str:
    return f"{value:,.0f}"


def category_label(name: str, traces: int) -> str:
    wrapped = textwrap.fill(name.replace("_", " ").replace("-", " "), CATEGORY_LABEL_WIDTH)
    return f"{wrapped}\n{traces:,} traces"


def draw_recall_chart(files: ResultFiles, theme: Theme) -> Figure:
    metrics = files.metrics
    sizes = metrics["categories"]
    names = list(sizes)
    tail = [metrics["samplers"]["tail"]["recall"][name] * 100 for name in names]
    head = [metrics["samplers"]["head"]["recall"][name] * 100 for name in names]
    colors = theme.colors
    figure = Figure(figsize=(12.5, 7.2), facecolor=colors["background"])
    add_headline(
        figure,
        theme,
        "Share of each trace category kept, tail versus head sampling",
        f"Evaluation traffic of {metrics['dataset']['evaluation']['traces']:,} traces. "
        f"Both samplers keep {metrics['samplers']['tail']['span_fraction'] * 100:.1f}% of spans.\n"
        "Keep-all keeps every category in full.",
    )
    axes = figure.add_axes((0.09, 0.19, 0.89, 0.58))
    style_axes(axes, theme)
    width = 0.36
    positions = list(range(len(names)))
    for offset, values, color in (
        (-width / 2, tail, colors["primary"]),
        (width / 2, head, colors["accent"]),
    ):
        axes.bar([p + offset for p in positions], values, width, color=color)
        for position, value in zip(positions, values, strict=True):
            axes.text(
                position + offset,
                value + 1.5,
                percent_text(value),
                ha="center",
                va="bottom",
                fontsize=14,
                color=color,
            )
    axes.text(-width / 2, 110, "tail", ha="center", fontsize=17, color=colors["primary"])
    axes.text(width / 2, 110, "head", ha="center", fontsize=17, color=colors["accent"])
    axes.set_xticks(positions, [category_label(name, sizes[name]) for name in names])
    axes.set_ylim(0, 118)
    axes.set_yticks(range(0, 101, 20), [f"{tick}%" for tick in range(0, 101, 20)])
    axes.set_ylabel("Traces kept, share of the category", fontsize=14.5)
    return figure


def draw_decision_wait_chart(files: ResultFiles, theme: Theme) -> Figure:
    metrics = files.metrics
    sweep = metrics["sweeps"]["decision_wait"]
    waits = [point["decision_wait_seconds"] for point in sweep]
    error = [point["tail"]["recall"]["error"] * 100 for point in sweep]
    slow = [point["tail"]["recall"]["slow"] * 100 for point in sweep]
    peak = [point["tail"]["peak_buffered_spans"] for point in sweep]
    dropped = [point["tail"]["late_spans_dropped"] for point in sweep]
    colors = theme.colors
    marker, line_width = theme.charts["marker"], theme.charts["line_width"]
    figure = Figure(figsize=(12.5, 8.6), facecolor=colors["background"])
    add_headline(
        figure,
        theme,
        "Longer decision waits recover late spans and cost buffer memory",
        f"Tail sampler on {metrics['dataset']['evaluation']['spans']:,} spans, "
        f"baseline rate {metrics['policy']['baseline_rate'] * 100:.0f}%.\n"
        "Each point is one full replay in event time.",
    )
    top = figure.add_axes((0.10, 0.52, 0.77, 0.28))
    bottom = figure.add_axes((0.10, 0.16, 0.77, 0.25), sharex=top)
    for axes in (top, bottom):
        style_axes(axes, theme)
        axes.set_xscale("log")
        axes.xaxis.set_minor_locator(NullLocator())
        axes.xaxis.set_minor_formatter(NullFormatter())
    floor = math.floor(min(error + slow) - 1)
    top.set_ylim(floor, 100.7)
    top.set_yticks(range(floor, 101), [f"{tick}%" for tick in range(floor, 101)])
    for values, color in ((error, colors["primary"]), (slow, colors["accent"])):
        top.plot(waits, values, color=color, marker=marker, markersize=12, linewidth=line_width)
    top.set_ylabel(f"Recall, axis starts at {floor}%", fontsize=14.5)
    top.tick_params(labelbottom=False)
    label_x = waits[-1] * 1.15
    top.text(label_x, error[-1], "error traces", color=colors["primary"], fontsize=16, va="bottom")
    top.text(
        label_x, slow[-1], "slowest 1% per route", color=colors["accent"], fontsize=16, va="top"
    )
    for index in (0, len(waits) - 1):
        below = error[index] < slow[index]
        top.text(
            waits[index],
            error[index] + (-0.3 if below else 0.3),
            percent_text(error[index]),
            ha="center",
            va="top" if below else "bottom",
            color=colors["primary"],
            fontsize=14,
        )
    bottom.plot(
        waits, peak, color=colors["secondary"], marker=marker, markersize=12, linewidth=line_width
    )
    for wait, value in zip(waits, peak, strict=True):
        bottom.text(
            wait,
            value + max(peak) * 0.08,
            f"{value:,}",
            ha="center",
            color=colors["secondary"],
            fontsize=14,
        )
    bottom.set_ylim(0, max(peak) * 1.3)
    bottom.yaxis.set_major_formatter(FuncFormatter(thousands))
    bottom.set_ylabel("Peak buffered spans", fontsize=14.5)
    bottom.text(
        label_x, peak[-1], "buffer size", color=colors["secondary"], fontsize=16, va="center"
    )
    bottom.set_xticks(
        waits,
        [
            f"{wait:g} s\n{count:,} late spans\ndropped"
            for wait, count in zip(waits, dropped, strict=True)
        ],
    )
    bottom.set_xlim(waits[0] / 1.3, waits[-1] * 2.4)
    return figure


def draw_stage_latency_chart(files: ResultFiles, theme: Theme) -> Figure:
    timings = files.timings
    environment = files.environment
    colors = theme.colors
    figure = Figure(figsize=(12.5, 7.4), facecolor=colors["background"])
    add_headline(
        figure,
        theme,
        "Per-call latency by pipeline stage, median and 99th percentile",
        f"In-process timings on {environment['cpu_model']}, "
        f"{environment['cpu_count']} logical cores, "
        f"{environment['python_implementation']} {environment['python']}.\n"
        "Store inserts are timed once per non-empty batch of kept traces.",
    )
    axes = figure.add_axes((0.09, 0.2, 0.89, 0.56))
    style_axes(axes, theme)
    positions = list(range(len(timings)))
    width = 0.36
    series = (
        (-width / 2, [item.p50_microseconds for item in timings], colors["accent"], "median"),
        (
            width / 2,
            [item.p99_microseconds for item in timings],
            colors["primary"],
            "99th percentile",
        ),
    )
    ceiling = max(item.p99_microseconds for item in timings)
    for offset, values, color, name in series:
        axes.bar([p + offset for p in positions], values, width, color=color, log=True)
        for position, value in zip(positions, values, strict=True):
            axes.text(
                position + offset,
                value * 1.15,
                f"{value:,.0f}" if value >= 100 else f"{value:.1f}",
                ha="center",
                va="bottom",
                fontsize=14,
                color=color,
            )
        axes.text(
            positions[0] + (-0.06 if offset < 0 else 0.06),
            ceiling * 2.4,
            name,
            ha="right" if offset < 0 else "left",
            fontsize=16,
            color=color,
        )
    axes.set_ylim(1, ceiling * 5)
    axes.yaxis.set_minor_locator(NullLocator())
    axes.set_xticks(
        positions,
        [
            f"{item.stage}\n{item.spans_per_second:,} spans/s\n{item.calls:,} calls"
            for item in timings
        ],
    )
    axes.set_yticks([1, 10, 100, 1000, 10000], ["1", "10", "100", "1,000", "10,000"])
    axes.set_ylabel("Microseconds per call, log scale", fontsize=14.5)
    return figure


def draw_sequence(theme: Theme) -> Figure:
    colors = theme.colors
    participants = SEQUENCE_PARTICIPANTS
    messages = SEQUENCE_MESSAGES
    spacing = 2.0
    row_height = 0.6
    left = 1.7
    width = left + spacing * (len(participants) - 1) + 1.3
    height = 2.3 + row_height * len(messages)
    figure = Figure(figsize=(width, height), facecolor=colors["background"])
    axes = figure.add_axes((0, 0, 1, 1))
    axes.set_xlim(0, width)
    axes.set_ylim(0, height)
    axes.axis("off")
    x_of = {item.key: left + index * spacing for index, item in enumerate(participants)}
    box_width, box_height = 1.75, 0.8
    box_y = height - 1.45
    corner = theme.shapes["corner_radius"] / 25
    for item in participants:
        x = x_of[item.key]
        axes.add_patch(
            FancyBboxPatch(
                (x - box_width / 2, box_y - box_height / 2),
                box_width,
                box_height,
                boxstyle=f"round,pad=0,rounding_size={corner}",
                facecolor=colors["background"],
                edgecolor=colors["primary"],
                linewidth=theme.shapes["border_width"],
            )
        )
        axes.text(
            x,
            box_y,
            item.label,
            ha="center",
            va="center",
            fontsize=14,
            fontfamily=theme.fonts["mono"],
            color=colors["text"],
        )
        axes.plot(
            [x, x], [box_y - box_height / 2, 0.3], color=colors["border"], linewidth=1.6, zorder=0
        )
    axes.text(
        0.3,
        height - 0.35,
        "Core request path of the gateway",
        fontsize=24,
        color=colors["text"],
        va="top",
    )
    label_box = {"facecolor": colors["background"], "edgecolor": "none", "pad": 2.5}
    for index, message in enumerate(messages):
        y = box_y - box_height / 2 - 0.7 - index * row_height
        start, end = x_of[message.sender], x_of[message.receiver]
        color = colors["muted"] if message.is_return else colors["primary"]
        if message.phase:
            axes.plot(
                [0.3, width - 0.3], [y + row_height * 0.55] * 2, color=colors["border"], linewidth=1
            )
            axes.text(0.3, y + row_height * 0.6, message.phase, fontsize=15, color=colors["accent"])
        axes.annotate(
            "",
            xy=(end, y),
            xytext=(start, y),
            arrowprops={
                "arrowstyle": "->",
                "color": color,
                "linewidth": theme.shapes["border_width"],
                "shrinkA": 0,
                "shrinkB": 0,
            },
        )
        axes.text(
            (start + end) / 2,
            y + 0.08,
            message.label,
            ha="center",
            va="bottom",
            fontsize=14,
            color=color,
            bbox=label_box,
            zorder=3,
        )
    return figure


CHART_RENDERERS: dict[str, Callable[[ResultFiles, Theme], Figure]] = {
    "recall-by-category": draw_recall_chart,
    "decision-wait-tradeoff": draw_decision_wait_chart,
    "stage-latency": draw_stage_latency_chart,
}


def write_image(path: Path, content: bytes, theme: Theme) -> None:
    width, height = png_dimensions(content)
    if max(width, height) < theme.minimum_long_side:
        raise ValueError(f"{path.name} is {width}x{height}, below {theme.minimum_long_side} px")
    if len(content) > MAXIMUM_IMAGE_BYTES:
        raise ValueError(f"{path.name} is {len(content)} bytes, above {MAXIMUM_IMAGE_BYTES}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    logger.info("wrote %s, %dx%d, %d bytes", path, width, height, len(content))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="render diagrams and charts into docs/images")
    parser.add_argument("--root", default=".", help="repository root")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    root = Path(build_parser().parse_args(argv).root)
    if shutil.which("dot") is None:
        logger.error("Graphviz dot is not on the PATH, install graphviz first")
        return 1
    if not (root / RESULT_DIRECTORY / "metrics.json").exists():
        logger.error("results are missing, run make evaluate first")
        return 1
    theme = load_theme(root / THEME_PATH)
    files = load_result_files(root / RESULT_DIRECTORY)
    images = root / IMAGE_DIRECTORY
    for source in sorted((root / DIAGRAM_DIRECTORY).glob("*.dot")):
        name = source.stem.replace("_", "-")
        write_image(images / f"{name}.png", render_dot(source, theme), theme)
    with matplotlib.rc_context(chart_style(theme)):
        write_image(images / "sequence.png", save_figure(draw_sequence(theme), theme), theme)
        for name, draw in CHART_RENDERERS.items():
            write_image(images / f"{name}.png", save_figure(draw(files, theme), theme), theme)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
