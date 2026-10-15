#!/usr/bin/env python3
"""Release tag rules shared by the release workflows and release tools.

Universal Agents publishes three kinds of release tags:

- ``vX.Y.Z``: a stable release. ``X.Y.Z`` is the ``VERSION`` file of the
  tagged commit.
- ``vX.Y.Z-beta.N`` and ``vX.Y.Z-rc.N``: a person tags a test build of an
  upcoming release (``N`` is 1, 2, ... without leading zeros).
- ``vX.Y.Z-nightly.YYYYMMDDHHMM``: the nightly workflow tags ``main`` (UTC
  minute of the build).

``VERSION`` on ``main`` names the newest *published* release; it changes only
in the ``Release vX.Y.Z`` commit. A pre-release therefore names the release it
leads up to, which must be the next patch, minor or major version after
``VERSION``. Semantic versioning sorts ``0.9.2-nightly.N`` after ``0.9.1`` and
before ``0.9.2``, so the desktop launcher (which compares versions this way)
sees a nightly built after 0.9.1 as newer than 0.9.1. A ``0.9.1-nightly.N``
tag would sort before the already published 0.9.1 and never be offered.

Run as a script, ``previous-stable VERSION`` reads tag names (one per line)
from standard input and prints the newest stable tag older than ``VERSION``,
or nothing. The release workflow uses it as the start of generated notes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import re
import sys
from typing import Any, Iterable


SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_BASE = r"(?P<base>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"
RELEASE_TAG_PATTERN = re.compile(
    rf"^v{_BASE}"
    r"(?:-(?:(?P<test_kind>beta|rc)\.(?P<number>[1-9]\d*)|nightly\.(?P<stamp>\d{12})))?$"
)
NIGHTLY_TAG_PATTERN = re.compile(rf"^v{_BASE}-nightly\.(?P<stamp>\d{{12}})$")
NIGHTLY_STAMP_FORMAT = "%Y%m%d%H%M"
TAG_FORMS = "vX.Y.Z, vX.Y.Z-beta.N, vX.Y.Z-rc.N or vX.Y.Z-nightly.YYYYMMDDHHMM"


class ReleaseKind(StrEnum):
    STABLE = "stable"
    BETA = "beta"
    RC = "rc"
    NIGHTLY = "nightly"


@dataclass(frozen=True)
class ReleaseTag:
    tag: str
    version: str
    """The full version without ``v``, for example ``0.9.2-beta.1``."""
    base: str
    """``MAJOR.MINOR.PATCH`` without the pre-release suffix."""
    kind: ReleaseKind

    @property
    def prerelease(self) -> bool:
        return self.kind is not ReleaseKind.STABLE


def parse_release_tag(tag: str) -> ReleaseTag:
    """The parts of a release tag; ``ValueError`` when it has none of the allowed forms."""

    match = RELEASE_TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise ValueError(f"Release tag {tag!r} is not one of {TAG_FORMS}.")
    if match["stamp"] is not None:
        try:
            datetime.strptime(match["stamp"], NIGHTLY_STAMP_FORMAT)
        except ValueError:
            raise ValueError(
                f"Nightly tag {tag!r} does not end in a real UTC minute (YYYYMMDDHHMM)."
            ) from None
        kind = ReleaseKind.NIGHTLY
    elif match["test_kind"] is not None:
        kind = ReleaseKind(match["test_kind"])
    else:
        kind = ReleaseKind.STABLE
    return ReleaseTag(tag=tag, version=tag[1:], base=match["base"], kind=kind)


def semver_key(version: str) -> tuple[Any, ...]:
    """Sort key following semantic-version precedence (``0.9.2-rc.1 < 0.9.2``)."""

    core, _, prerelease = version.removeprefix("v").partition("-")
    if not SEMVER_PATTERN.fullmatch(core):
        raise ValueError(f"{version!r} is not a semantic version.")
    major, minor, patch = (int(part) for part in core.split("."))
    if not prerelease:
        return (major, minor, patch, 1, ())
    identifiers = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in prerelease.split(".")
    )
    return (major, minor, patch, 0, identifiers)


def next_versions(version: str) -> tuple[str, str, str]:
    """The next patch, minor and major release after ``version``."""

    if not SEMVER_PATTERN.fullmatch(version):
        raise ValueError(f"{version!r} is not canonical MAJOR.MINOR.PATCH.")
    major, minor, patch = (int(part) for part in version.split("."))
    return (
        f"{major}.{minor}.{patch + 1}",
        f"{major}.{minor + 1}.0",
        f"{major + 1}.0.0",
    )


def nightly_tag(version: str, now: datetime) -> str:
    """The nightly tag for a build at ``now`` when ``VERSION`` is ``version``."""

    if now.tzinfo is None:
        raise ValueError("A nightly tag needs a timezone-aware time.")
    stamp = now.astimezone(timezone.utc).strftime(NIGHTLY_STAMP_FORMAT)
    return f"v{next_versions(version)[0]}-nightly.{stamp}"


def previous_stable_tag(version: str, tags: Iterable[str]) -> str | None:
    """The newest stable ``vX.Y.Z`` tag older than ``version``, if any."""

    current = semver_key(version)
    older = []
    for tag in tags:
        tag = tag.strip()
        try:
            parsed = parse_release_tag(tag)
        except ValueError:
            continue
        if parsed.kind is ReleaseKind.STABLE and semver_key(parsed.version) < current:
            older.append(parsed)
    if not older:
        return None
    return max(older, key=lambda parsed: semver_key(parsed.version)).tag


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "previous-stable":
        print(f"Usage: {argv[0]} previous-stable VERSION < tags", file=sys.stderr)
        return 2
    try:
        previous = previous_stable_tag(argv[2], sys.stdin.read().splitlines())
    except ValueError as exc:
        print(f"Release version lookup failed: {exc}", file=sys.stderr)
        return 1
    if previous:
        print(previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
