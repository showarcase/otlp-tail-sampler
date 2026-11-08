import importlib.metadata
import re
import shutil
import string
import tomllib
from dataclasses import fields
from io import BytesIO
from pathlib import Path

import matplotlib
import matplotlib.image
import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

import freeze_lock
from freeze_lock import lock_text, pinned_versions, requirement_applies
from render_figures import (
    CHART_RENDERERS,
    SEQUENCE_MESSAGES,
    Theme,
    chart_style,
    dot_variables,
    draw_sequence,
    load_theme,
    pad_edge_labels,
    png_dimensions,
    render_dot,
    save_figure,
    write_image,
)
from render_figures import main as render_figures_main
from render_results import (
    BLOCK_RENDERERS,
    fill_blocks,
    load_result_files,
    main,
    markdown_files,
    markdown_table,
    signed_percent,
)
from tailsampler.config import GatewaySettings
from tailsampler.fleet import load_simulation

BLOCK_TEXT = "intro\n\n<!-- results:begin policy -->\nstale\n<!-- results:end policy -->\n\noutro\n"


@pytest.fixture(scope="module")
def files(tiny_results):
    return load_result_files(tiny_results)


@pytest.fixture
def repository(tmp_path, tiny_results):
    shutil.copytree(tiny_results, tmp_path / "results")
    (tmp_path / "docs").mkdir()
    names = sorted(BLOCK_RENDERERS)
    markers = "\n".join(
        f"<!-- results:begin {name} -->\n<!-- results:end {name} -->\n" for name in names
    )
    (tmp_path / "docs" / "evaluation.md").write_text(markers, encoding="utf-8")
    (tmp_path / "README.md").write_text(BLOCK_TEXT, encoding="utf-8")
    return tmp_path


def test_every_block_renders_a_table(files):
    for name, render in BLOCK_RENDERERS.items():
        lines = render(files).splitlines()
        assert len(lines) >= 3, name
        assert lines[0].startswith("| ") and lines[1].startswith("| ---"), name
        assert len({line.count("|") for line in lines}) == 1, name


def test_table_aligns_numbers_to_the_right():
    table = markdown_table(["Name", "Count", "Share"], [["a", "1", "2%"]])
    assert table.splitlines()[1] == "| --- | ---: | ---: |"
    wide = markdown_table(["Item", "Value"], [["a", "b"]], text_columns=2)
    assert wide.splitlines()[1] == "| --- | --- |"


@pytest.mark.parametrize(
    ("value", "text"), [(-0.04, "0.0%"), (0.0, "0.0%"), (2.46, "+2.5%"), (-15.0, "-15.0%")]
)
def test_signed_percent_has_no_negative_zero(value, text):
    assert signed_percent(value) == text


def test_filling_replaces_only_the_marked_body():
    filled = fill_blocks(BLOCK_TEXT, {"policy": "| table |"})
    begin, end = "<!-- results:begin policy -->", "<!-- results:end policy -->"
    assert filled == f"intro\n\n{begin}\n\n| table |\n\n{end}\n\noutro\n"
    assert fill_blocks(filled, {"policy": "| table |"}) == filled


def test_unknown_block_names_are_rejected():
    with pytest.raises(ValueError, match="unknown results block 'policy'"):
        fill_blocks(BLOCK_TEXT, {})


def test_marker_without_an_end_is_rejected():
    with pytest.raises(ValueError, match="no matching end marker"):
        fill_blocks("<!-- results:begin policy -->\ntext\n", {"policy": "x"})


def test_text_without_markers_is_unchanged():
    assert fill_blocks("no markers\n", {"policy": "x"}) == "no markers\n"


def test_only_readme_and_docs_are_searched(repository):
    (repository / "docs" / "decisions").mkdir()
    (repository / "docs" / "decisions" / "0001.md").write_text("x", encoding="utf-8")
    (repository / "CONTRIBUTING.md").write_text("x", encoding="utf-8")
    found = [path.relative_to(repository).as_posix() for path in markdown_files(repository)]
    assert found == ["README.md", "docs/decisions/0001.md", "docs/evaluation.md"]


def test_render_fills_every_file_and_check_then_passes(repository):
    assert main(["--root", str(repository), "--check"]) == 1
    assert (repository / "README.md").read_text(encoding="utf-8") == BLOCK_TEXT
    assert main(["--root", str(repository)]) == 0
    evaluation = (repository / "docs" / "evaluation.md").read_text(encoding="utf-8")
    assert "stale" not in (repository / "README.md").read_text(encoding="utf-8")
    assert "| Setting | Value |" in evaluation
    assert "| Sampler | Spans kept |" in evaluation
    assert main(["--root", str(repository), "--check"]) == 0


def test_render_reports_missing_result_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        main(["--root", str(tmp_path)])


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOT_SOURCES = sorted((REPOSITORY_ROOT / "docs" / "diagrams").glob("*.dot"))
MAKE_TARGET_PATTERN = re.compile(r"\bmake ([a-z]+)\b")
MODULE_PATTERN = re.compile(r"\b([a-z_]+)\.py\b")
HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-f]{6}$")
dot_available = pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz is not installed")


def relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.fixture(scope="module")
def theme() -> Theme:
    return load_theme(REPOSITORY_ROOT / "docs" / "diagrams" / "theme.json")


def test_theme_defines_nine_hex_colors(theme):
    assert len(theme.colors) == 9
    assert all(HEX_COLOR_PATTERN.match(value) for value in theme.colors.values())


@pytest.mark.parametrize("name", ["text", "muted", "primary", "secondary", "accent", "error"])
def test_theme_text_colors_meet_the_contrast_floor(theme, name):
    assert contrast_ratio(theme.colors[name], theme.colors["background"]) >= 4.5


def test_every_dot_source_substitutes_without_missing_placeholders(theme):
    assert DOT_SOURCES
    for source in DOT_SOURCES:
        template = string.Template(source.read_text(encoding="utf-8"))
        text = template.substitute(dot_variables(theme))
        assert "$" not in text, source.name
        assert text.count("{") == text.count("}"), source.name


def test_dot_sources_name_real_modules_and_make_targets():
    makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-z]+):", makefile, re.MULTILINE))
    modules = {path.name for path in (REPOSITORY_ROOT / "src" / "tailsampler").glob("*.py")}
    modules |= {path.name for path in (REPOSITORY_ROOT / "scripts").glob("*.py")}
    for source in DOT_SOURCES:
        text = source.read_text(encoding="utf-8").replace("\\n", " ")
        for target in MAKE_TARGET_PATTERN.findall(text):
            assert target in targets, f"{source.name}: make {target}"
        for module in MODULE_PATTERN.findall(text):
            assert f"{module}.py" in modules, f"{source.name}: {module}.py"


def test_sequence_messages_call_functions_that_exist():
    defined = set()
    for path in (REPOSITORY_ROOT / "src" / "tailsampler").glob("*.py"):
        defined.update(re.findall(r"^\s*def (\w+)\(", path.read_text(encoding="utf-8"), re.M))
    symbols = {message.symbol for message in SEQUENCE_MESSAGES if message.symbol}
    assert symbols
    assert symbols <= defined


def test_edge_labels_are_padded_but_node_labels_are_not():
    source = 'a [label="node\\ntext"];\na -> b [label="one\\ntwo"];\n'
    padded = pad_edge_labels(source)
    assert 'a [label="node\\ntext"];' in padded
    assert 'a -> b [label="  one  \\n  two  "];' in padded


def test_png_dimensions_reads_the_header_and_rejects_other_bytes():
    header = b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
    header += (640).to_bytes(4, "big") + (480).to_bytes(4, "big")
    assert png_dimensions(header) == (640, 480)
    with pytest.raises(ValueError, match="not a PNG"):
        png_dimensions(b"GIF89a" + bytes(40))


def corner_pixel(content: bytes) -> tuple[int, int, int]:
    pixels = matplotlib.image.imread(BytesIO(content), format="png")
    return tuple(round(float(value) * 255) for value in pixels[0, 0, :3])


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))


def test_sequence_diagram_meets_the_image_rules(theme):
    content = save_figure(draw_sequence(theme), theme)
    assert max(png_dimensions(content)) >= theme.minimum_long_side
    assert len(content) < 1_500_000
    assert corner_pixel(content) == hex_to_rgb(theme.colors["background"])


@pytest.mark.parametrize("name", sorted(CHART_RENDERERS))
def test_charts_render_from_results_with_the_theme_background(name, theme, files):
    with matplotlib.rc_context(chart_style(theme)):
        content = save_figure(CHART_RENDERERS[name](files, theme), theme)
    assert max(png_dimensions(content)) >= theme.minimum_long_side
    assert len(content) < 1_500_000
    assert corner_pixel(content) == hex_to_rgb(theme.colors["background"])


def test_write_image_rejects_undersized_pictures(tmp_path, theme):
    small = b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
    small += (100).to_bytes(4, "big") + (100).to_bytes(4, "big")
    with pytest.raises(ValueError, match="below"):
        write_image(tmp_path / "small.png", small, theme)
    assert not (tmp_path / "small.png").exists()


@dot_available
@pytest.mark.parametrize("source", DOT_SOURCES, ids=lambda path: path.stem)
def test_dot_sources_render_at_the_minimum_size(source, theme):
    content = render_dot(source, theme)
    pixels = matplotlib.image.imread(BytesIO(content), format="png")
    assert max(png_dimensions(content)) >= theme.minimum_long_side
    assert pixels.shape[2] == 3
    expected = hex_to_rgb(theme.colors["background"])
    for corner in (pixels[0, 0], pixels[0, -1], pixels[-1, 0], pixels[-1, -1]):
        assert tuple(round(float(value) * 255) for value in corner) == expected


@dot_available
def test_render_figures_writes_every_image(tmp_path, tiny_results):
    shutil.copytree(REPOSITORY_ROOT / "docs" / "diagrams", tmp_path / "docs" / "diagrams")
    shutil.copytree(tiny_results, tmp_path / "results")
    assert render_figures_main(["--root", str(tmp_path)]) == 0
    written = {path.stem for path in (tmp_path / "docs" / "images").glob("*.png")}
    expected = {source.stem.replace("_", "-") for source in DOT_SOURCES}
    expected |= set(CHART_RENDERERS) | {"sequence"}
    assert written == expected


def test_render_figures_requires_results(tmp_path):
    shutil.copytree(REPOSITORY_ROOT / "docs" / "diagrams", tmp_path / "docs" / "diagrams")
    assert render_figures_main(["--root", str(tmp_path)]) == 1


def project_requirements() -> list[Requirement]:
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = project["project"]["dependencies"]
    declared += project["project"]["optional-dependencies"]["dev"]
    return [Requirement(line) for line in declared]


def lock_entries() -> dict[str, str]:
    lines = (REPOSITORY_ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
    return dict(line.split("==") for line in lines)


def test_requirement_markers_depend_on_the_requested_extras():
    development_only = Requirement('pytest>=9; extra == "dev"')
    assert requirement_applies(development_only, ("dev",))
    assert not requirement_applies(development_only, ())
    assert requirement_applies(Requirement("starlette>=1"), ())
    assert not requirement_applies(Requirement('colorama; python_version < "3.0"'), ("dev",))


def test_pinned_versions_follow_the_installed_runtime_dependencies():
    versions = pinned_versions("otlp-tail-sampler", ())
    assert {"starlette", "uvicorn", "anyio", "h11"} <= set(versions)
    assert "otlp-tail-sampler" not in versions
    assert versions["starlette"] == importlib.metadata.version("starlette")


def test_lock_text_is_sorted_name_equals_version_lines():
    lines = lock_text("otlp-tail-sampler", ()).splitlines()
    assert lines == sorted(lines)
    assert all(re.fullmatch(r"[a-z0-9-]+==[0-9][^\s=]*", line) for line in lines)


def test_missing_distribution_is_reported_with_its_name():
    with pytest.raises(RuntimeError, match="no-such-distribution is not installed"):
        pinned_versions("no-such-distribution", ())


def test_freeze_lock_check_detects_a_stale_file(tmp_path, monkeypatch):
    monkeypatch.setattr(freeze_lock, "lock_text", lambda: "alpha==1.0\nbeta==2.0\n")
    target = tmp_path / "requirements.lock"
    assert freeze_lock.main(["--output", str(target), "--check"]) == 1
    assert freeze_lock.main(["--output", str(target)]) == 0
    assert target.read_text(encoding="utf-8") == "alpha==1.0\nbeta==2.0\n"
    assert freeze_lock.main(["--output", str(target), "--check"]) == 0
    target.write_text("alpha==0.9\n", encoding="utf-8")
    assert freeze_lock.main(["--output", str(target), "--check"]) == 1


def test_lock_file_pins_every_declared_dependency_within_its_range():
    entries = lock_entries()
    for requirement in project_requirements():
        name = canonicalize_name(requirement.name)
        assert name in entries, name
        assert requirement.specifier.contains(entries[name]), requirement


def test_lock_file_is_sorted():
    names = list(lock_entries())
    assert names == sorted(names)


def dockerfile_lines() -> list[str]:
    text = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
    return [line.strip() for line in text.replace("\\\n", " ").splitlines() if line.strip()]


def test_dockerfile_copies_only_files_that_exist_and_are_not_ignored():
    ignored = {
        line.strip()
        for line in (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    }
    sources = []
    for line in dockerfile_lines():
        if line.startswith("COPY "):
            sources += line.split()[1:-1]
    assert sources
    for source in sources:
        assert (REPOSITORY_ROOT / source).exists(), source
        assert Path(source).parts[0] not in ignored, source


def test_python_version_agrees_across_image_ci_and_project():
    image = next(line for line in dockerfile_lines() if line.startswith("FROM "))
    image_version = re.search(r"python:(\d+\.\d+)", image).group(1)
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_version = re.search(r'python-version: "(\d+\.\d+)"', workflow).group(1)
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert image_version == ci_version
    assert project["project"]["requires-python"] == f">={ci_version}"


def test_workflow_lints_tests_and_builds_the_image_from_the_lock_file():
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for command in (
        "make lint",
        "make test",
        "render_results.py --check",
        "docker build -t otlp-tail-sampler .",
        "-c requirements.lock",
    ):
        assert command in workflow, command


def test_image_environment_names_real_settings_and_the_exposed_port_is_the_default():
    names = {f"TAILSAMPLER_{item.name.upper()}" for item in fields(GatewaySettings)}
    text = "\n".join(dockerfile_lines())
    assert set(re.findall(r"(TAILSAMPLER_[A-Z_]+)=", text)) <= names
    assert f"EXPOSE {GatewaySettings().port}" in text


def test_every_setting_is_documented_in_the_env_example_and_the_deployment_guide():
    names = {f"TAILSAMPLER_{item.name.upper()}" for item in fields(GatewaySettings)}
    example = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
    guide = (REPOSITORY_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    for name in names:
        assert f"{name}=" in example, name
        assert f"| {name} |" in guide, name


MARKDOWN_FILES = [REPOSITORY_ROOT / "README.md", *sorted((REPOSITORY_ROOT / "docs").rglob("*.md"))]
PYTHON_FILE_PATTERN = re.compile(r"`([a-z_]+\.py)`")
IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
DECISION_PATTERN = re.compile(r"docs/decisions/[0-9a-z-]+\.md")


def existing_markdown() -> list[Path]:
    return [path for path in MARKDOWN_FILES if path.exists()]


@pytest.mark.parametrize("path", existing_markdown(), ids=lambda path: path.name)
def test_markdown_images_exist(path):
    for reference in IMAGE_PATTERN.findall(path.read_text(encoding="utf-8")):
        target = reference.split(" ")[0]
        if not target.startswith("https://"):
            assert (path.parent / target).exists(), target


@pytest.mark.parametrize("path", existing_markdown(), ids=lambda path: path.name)
def test_markdown_names_real_python_files_and_decision_records(path):
    text = path.read_text(encoding="utf-8")
    folders = (REPOSITORY_ROOT / "src" / "tailsampler", REPOSITORY_ROOT / "scripts")
    known = {item.name for folder in folders for item in folder.glob("*.py")}
    for name in PYTHON_FILE_PATTERN.findall(text):
        assert name in known, name
    for target in DECISION_PATTERN.findall(text):
        assert (REPOSITORY_ROOT / target).exists(), target


def test_every_rendered_block_in_the_markdown_files_is_current():
    assert main(["--root", str(REPOSITORY_ROOT), "--check"]) == 0


def test_data_card_lists_the_incidents_of_the_simulation():
    simulation = load_simulation(REPOSITORY_ROOT / "configs" / "simulation.toml")
    card = (REPOSITORY_ROOT / "docs" / "data.md").read_text(encoding="utf-8")
    assert simulation.fleet.incidents
    for incident in simulation.fleet.incidents:
        assert f"| {incident.name} | {incident.operation} | {incident.kind} |" in card
