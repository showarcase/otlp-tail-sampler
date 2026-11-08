import argparse
import logging
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PROJECT_NAME = "otlp-tail-sampler"
LOCK_EXTRAS = ("dev",)
DEFAULT_OUTPUT = "requirements.lock"

logger = logging.getLogger("freeze_lock")


def pinned_versions(project: str, extras: Sequence[str]) -> dict[str, str]:
    project_key = canonicalize_name(project)
    pending = [(project_key, tuple(extras))]
    visited: set[tuple[str, tuple[str, ...]]] = set()
    versions: dict[str, str] = {}
    while pending:
        name, requested_extras = pending.pop()
        if (name, requested_extras) in visited:
            continue
        visited.add((name, requested_extras))
        try:
            installed = distribution(name)
        except PackageNotFoundError as error:
            raise RuntimeError(f"{name} is not installed, run make setup first") from error
        if name != project_key:
            versions[name] = installed.version
        for line in installed.requires or []:
            requirement = Requirement(line)
            if requirement_applies(requirement, requested_extras):
                pending.append(
                    (canonicalize_name(requirement.name), tuple(sorted(requirement.extras)))
                )
    return versions


def requirement_applies(requirement: Requirement, requested_extras: Sequence[str]) -> bool:
    if requirement.marker is None:
        return True
    return any(requirement.marker.evaluate({"extra": extra}) for extra in ("", *requested_extras))


def lock_text(project: str = PROJECT_NAME, extras: Sequence[str] = LOCK_EXTRAS) -> str:
    versions = pinned_versions(project, extras)
    return "".join(f"{name}=={versions[name]}\n" for name in sorted(versions))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="write the pinned versions of the dev install")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="fail if the file is out of date")
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    text = lock_text()
    output = Path(arguments.output)
    if arguments.check:
        if not output.exists() or output.read_text(encoding="utf-8") != text:
            logger.error("%s does not match the installed versions, run make lock", output)
            return 1
        return 0
    output.write_text(text, encoding="utf-8")
    logger.info("wrote %s with %d pinned packages", output, text.count("\n"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
