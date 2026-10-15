#!/usr/bin/env python3
"""Tag a nightly pre-release of ``main``, start its image build, and prune old nightlies.

Run by ``.github/workflows/nightly.yml``:

1. If the newest nightly pre-release already points at the target commit,
   ``main`` has not changed: print a notice and create nothing.
2. Otherwise create a GitHub pre-release (never marked latest) named
   ``v{next patch after VERSION}-nightly.{UTC YYYYMMDDHHMM}`` on the target
   commit; GitHub creates the tag with it.
3. Dispatch ``publish-container.yml`` with ``release_tag`` set to the new tag.
   Tags and releases created with the workflow's ``GITHUB_TOKEN`` do not start
   other workflows, but a ``workflow_dispatch`` request does, so the image
   build and its ``container-image.txt`` must be started explicitly.
4. Keep the newest nightly pre-releases (seven by default) and delete older
   nightly releases and their tags. Stable releases, betas and release
   candidates are never touched: only pre-releases whose tag has the nightly
   form are considered.

All GitHub calls go through ``gh api`` (token from ``GH_TOKEN``).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.github_api import GhCliApi, GitHubApi, GitHubError, get_optional  # noqa: E402
from scripts.release_versions import NIGHTLY_TAG_PATTERN, nightly_tag, parse_release_tag  # noqa: E402
from scripts.validate_release_tag import classify_release_tag  # noqa: E402


CONTAINER_WORKFLOW = "publish-container.yml"
DEFAULT_KEEP = 7
PAGE_SIZE = 100


@dataclass(frozen=True)
class Nightly:
    release_id: int
    tag: str
    stamp: str


def nightly_releases(api: GitHubApi, repository: str) -> list[Nightly]:
    """Published nightly pre-releases, newest first."""

    found: list[Nightly] = []
    page = 1
    while True:
        releases = api.request(
            "GET", f"repos/{repository}/releases?per_page={PAGE_SIZE}&page={page}"
        )
        for release in releases:
            match = NIGHTLY_TAG_PATTERN.fullmatch(release.get("tag_name", ""))
            if match and release.get("prerelease") and not release.get("draft"):
                found.append(Nightly(release["id"], release["tag_name"], match["stamp"]))
        if len(releases) < PAGE_SIZE:
            break
        page += 1
    return sorted(found, key=lambda nightly: (nightly.stamp, nightly.tag), reverse=True)


def release_title(tag: str) -> str:
    release = parse_release_tag(tag)
    stamp = datetime.strptime(tag.rsplit(".", 1)[1], "%Y%m%d%H%M")
    return f"Universal Agents {release.base} nightly ({stamp:%Y-%m-%d %H:%M} UTC)"


def release_notes(target_sha: str) -> str:
    return (
        f"Nightly build of main at {target_sha}. Not checked for desktop launcher "
        "compatibility; for testing only."
    )


def prune(
    api: GitHubApi,
    repository: str,
    nightlies: list[Nightly],
    keep: int,
    out: Callable[[str], None],
) -> list[str]:
    deleted = []
    for nightly in nightlies[keep:]:
        # Checked again here: nothing but a nightly tag may ever be deleted.
        if not NIGHTLY_TAG_PATTERN.fullmatch(nightly.tag):
            raise ValueError(f"Refusing to delete non-nightly tag {nightly.tag!r}.")
        # The release is the durable record for retries. Delete it last so an
        # interrupted tag deletion cannot leave an undiscoverable orphan tag.
        try:
            api.request("DELETE", f"repos/{repository}/git/refs/tags/{nightly.tag}")
        except GitHubError as exc:
            if exc.status not in (404, 422):
                raise
            # A 422 can also refuse deletion of an existing tag. Confirm absence
            # before removing its release, rather than silently losing the retry.
            if exc.status == 422 and get_optional(
                api, f"repos/{repository}/git/ref/tags/{nightly.tag}"
            ) is not None:
                raise
            out(f"Tag {nightly.tag} was already gone while pruning its release.")
        api.request("DELETE", f"repos/{repository}/releases/{nightly.release_id}")
        out(f"Deleted old nightly {nightly.tag} and its tag.")
        deleted.append(nightly.tag)
    return deleted


def run(
    api: GitHubApi,
    *,
    repository: str,
    target_sha: str,
    version_path: Path,
    now: datetime,
    keep: int = DEFAULT_KEEP,
    dispatch_ref: str = "main",
    out: Callable[[str], None] = print,
    notice: Callable[[str], None] = print,
) -> str | None:
    """Publish a nightly for ``target_sha``; return its tag, or ``None`` when skipped."""

    if keep < 1:
        raise ValueError("At least one nightly must be kept.")
    nightlies = nightly_releases(api, repository)
    if nightlies:
        newest = nightlies[0]
        newest_sha = api.request("GET", f"repos/{repository}/commits/{newest.tag}")["sha"]
        if newest_sha == target_sha:
            notice(
                f"main is still at {target_sha}, already built as nightly {newest.tag}; "
                "no new nightly was created."
            )
            prune(api, repository, nightlies, keep, out)
            return None

    version = version_path.read_text(encoding="utf-8").strip()
    tag = nightly_tag(version, now)
    # The container workflow checks the tag against the same VERSION file.
    classify_release_tag(tag, version_path=version_path)
    api.request(
        "POST",
        f"repos/{repository}/releases",
        {
            "tag_name": tag,
            "target_commitish": target_sha,
            "name": release_title(tag),
            "body": release_notes(target_sha),
            "prerelease": True,
            "make_latest": "false",
        },
    )
    out(f"Created nightly pre-release {tag} at {target_sha}.")
    api.request(
        "POST",
        f"repos/{repository}/actions/workflows/{CONTAINER_WORKFLOW}/dispatches",
        {"ref": dispatch_ref, "inputs": {"release_tag": tag}},
    )
    out(f"Started {CONTAINER_WORKFLOW} for {tag} from {dispatch_ref}.")
    # The new nightly counts toward ``keep``; it is never among those deleted.
    prune(api, repository, nightlies, keep - 1, out)
    return tag


def _notice(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title=Nightly skipped::{message}")
    else:
        print(message)


def main(argv: list[str], api: GitHubApi | None = None) -> int:
    parser = argparse.ArgumentParser(prog=Path(argv[0]).name, description=__doc__.splitlines()[0])
    parser.add_argument("--repository", required=True, help="owner/name of the UA repository")
    parser.add_argument("--target-sha", required=True, help="main commit to build")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="nightlies to keep (default 7)")
    parser.add_argument("--dispatch-ref", default="main", help="branch whose publish-container.yml runs")
    parser.add_argument(
        "--version-file", type=Path, default=PROJECT_ROOT / "VERSION", help="VERSION of the target commit"
    )
    args = parser.parse_args(argv[1:])
    try:
        run(
            api or GhCliApi(),
            repository=args.repository,
            target_sha=args.target_sha,
            version_path=args.version_file,
            now=datetime.now(timezone.utc),
            keep=args.keep,
            dispatch_ref=args.dispatch_ref,
            notice=_notice,
        )
    except (OSError, ValueError, GitHubError) as exc:
        print(f"Nightly release failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
