#!/usr/bin/env python3
"""Validate that a release tag is canonical and matches VERSION.

A stable tag must be exactly ``v{VERSION}``. A pre-release tag (beta, rc or
nightly) must lead up to the next patch, minor or major release after
``VERSION``; see ``scripts/release_versions.py`` for why.

Prints the full version (``0.9.1`` or ``0.9.2-beta.1``). With ``--kind`` it
prints ``stable``, ``beta``, ``rc`` or ``nightly`` instead; anything other than
``stable`` is a GitHub pre-release. ``--version-file`` reads ``VERSION`` from
another checkout, so the workflow's own copy of this script can check an older
release tag's source.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.release_versions import (  # noqa: E402
    SEMVER_PATTERN,
    ReleaseKind,
    ReleaseTag,
    next_versions,
    parse_release_tag,
)


def classify_release_tag(
    tag: str, *, version_path: Path = PROJECT_ROOT / "VERSION"
) -> ReleaseTag:
    version = version_path.read_text(encoding="utf-8").strip()
    if not SEMVER_PATTERN.fullmatch(version):
        raise ValueError(f"VERSION must contain canonical MAJOR.MINOR.PATCH, found {version!r}.")
    release = parse_release_tag(tag)
    if release.kind is ReleaseKind.STABLE:
        if release.base != version:
            raise ValueError(
                f"Release tag {tag!r} does not match VERSION; expected 'v{version}'."
            )
        return release
    upcoming = next_versions(version)
    if release.base not in upcoming:
        choices = ", ".join(upcoming)
        raise ValueError(
            f"Pre-release tag {tag!r} must lead up to the release after VERSION "
            f"{version} ({choices}), not {release.base}."
        )
    return release


def validate_release_tag(tag: str, *, version_path: Path = PROJECT_ROOT / "VERSION") -> str:
    return classify_release_tag(tag, version_path=version_path).version


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=Path(argv[0]).name,
        description="Check a release tag against VERSION and print its version.",
    )
    parser.add_argument("tag", help="vX.Y.Z, vX.Y.Z-beta.N, vX.Y.Z-rc.N or vX.Y.Z-nightly.YYYYMMDDHHMM")
    parser.add_argument(
        "--kind",
        action="store_true",
        help="print stable, beta, rc or nightly instead of the version",
    )
    parser.add_argument(
        "--version-file",
        type=Path,
        default=PROJECT_ROOT / "VERSION",
        help="VERSION file to check against (default: this checkout's)",
    )
    args = parser.parse_args(argv[1:])
    try:
        release = classify_release_tag(args.tag, version_path=args.version_file)
    except (OSError, ValueError) as exc:
        print(f"Release validation failed: {exc}", file=sys.stderr)
        return 1
    print(release.kind.value if args.kind else release.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
