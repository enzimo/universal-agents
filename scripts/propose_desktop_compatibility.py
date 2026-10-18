#!/usr/bin/env python3
"""Open a pull request adding a stable UA release to the desktop launcher's list.

The desktop launcher repository keeps ``compatible-runtimes.json`` on its
``main`` branch: every UA version known to work with each launcher "host
contract", with its digest-pinned image. Entries are in ascending version
order and are only ever appended. This script, run by
``publish-container.yml`` after a stable release image is published:

1. reads the list from the launcher's ``main`` branch;
2. stops if the version is already listed with the same image (a rerun), and
   fails if it is listed with a different image;
3. appends the release with the host contract of the newest listed entry;
4. commits the updated file to branch ``ua-release/vX.Y.Z`` and opens a pull
   request, reusing that branch and an open pull request when they exist.

The launcher repository's own check verifies the digest against the release's
``container-image.txt`` and exercises the published image against the desktop
contract on native AMD64 and ARM64, then merges the pull request. Only that check decides compatibility;
this script just proposes the entry.

All GitHub calls go through ``gh api``, which reads its token from
``GH_TOKEN``. The script never prints the token. ``--dry-run`` only reads and
prints the new file, branch and pull request text.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.github_api import GhCliApi, GitHubApi, GitHubError, get_optional  # noqa: E402
from scripts.release_versions import ReleaseKind, parse_release_tag, semver_key  # noqa: E402


LAUNCHER_REPOSITORY = "enzimo/universal-agents-desktop"
LAUNCHER_BRANCH = "main"
LIST_PATH = "compatible-runtimes.json"
LIST_SCHEMA_VERSION = 1
UA_REPOSITORY = "enzimo/universal-agents"
DEFAULT_IMAGE_REPOSITORY = "ghcr.io/enzimo/universal-agents"
BRANCH_PREFIX = "ua-release/"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
IMMUTABLE_IMAGE_LINE = re.compile(r"^Immutable image: (?P<image>\S+)$", re.MULTILINE)


class ProposalError(Exception):
    """The entry cannot be proposed; the message tells a person what to do."""


@dataclass(frozen=True)
class ListUpdate:
    text: str
    """The new file, or the unchanged file when ``already_listed``."""
    already_listed: bool
    host_contract_version: int
    newest_listed_version: str


class OlderThanListed(ProposalError):
    """The release is older than the newest listed version; the list is append-only."""


def render_list(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2) + "\n"


def read_list(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The parsed list and its entries; ``ProposalError`` when it breaks the list's rules."""

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProposalError(f"{LIST_PATH} on the launcher's main branch is not valid JSON: {exc}") from None
    if document.get("schema_version") != LIST_SCHEMA_VERSION:
        raise ProposalError(
            f"{LIST_PATH} uses schema {document.get('schema_version')!r}; "
            f"this script writes schema {LIST_SCHEMA_VERSION}. Update the script first."
        )
    runtimes = document.get("runtimes")
    if not isinstance(runtimes, list) or not runtimes:
        raise ProposalError(
            f"{LIST_PATH} lists no runtimes, so there is no host contract to copy. "
            "Add the first entry by hand."
        )
    required = {"ua_version", "host_contract_version", "image"}
    if any(not isinstance(entry, dict) or set(entry) != required for entry in runtimes):
        raise ProposalError(
            f"{LIST_PATH} has an entry without exactly {sorted(required)}; fix the list first."
        )
    keys = [semver_key(entry["ua_version"]) for entry in runtimes]
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        raise ProposalError(f"{LIST_PATH} is not in ascending version order; fix the list first.")
    return document, runtimes


def add_runtime(text: str, ua_version: str, image: str) -> ListUpdate:
    """The list with ``ua_version`` appended, following the list's rules."""

    document, runtimes = read_list(text)
    newest = runtimes[-1]
    for entry in runtimes:
        if entry["ua_version"] == ua_version:
            if entry["image"] == image:
                return ListUpdate(text, True, entry["host_contract_version"], newest["ua_version"])
            raise ProposalError(
                f"UA {ua_version} is already listed with image {entry['image']}, "
                f"not the published {image}. The list is append-only; "
                "a person must decide which image is right."
            )
        if entry["image"] == image:
            raise ProposalError(
                f"Image {image} is already listed for UA {entry['ua_version']}; "
                f"it cannot also be UA {ua_version}."
            )
    if semver_key(ua_version) < semver_key(newest["ua_version"]):
        raise OlderThanListed(
            f"UA {ua_version} is older than the newest listed version "
            f"{newest['ua_version']}. The list is append-only, so it was not proposed; "
            "add it by hand if the launcher should offer it."
        )
    contract = newest["host_contract_version"]
    runtimes.append(
        {"ua_version": ua_version, "host_contract_version": contract, "image": image}
    )
    return ListUpdate(render_list(document), False, contract, newest["ua_version"])


def pull_request_body(
    *, ua_version: str, image: str, contract: int, newest: str, container_image_text: str | None
) -> str:
    release_url = f"https://github.com/{UA_REPOSITORY}/releases/tag/v{ua_version}"
    reference = (
        "`container-image.txt` from the release:\n\n```text\n"
        f"{container_image_text.rstrip()}\n```"
        if container_image_text
        else "The release's `container-image.txt` names the same image."
    )
    return (
        f"Universal Agents {ua_version} was released. This pull request adds it to the "
        f"launcher's compatibility list with host contract {contract}, the contract of "
        f"the newest listed version (UA {newest}).\n\n"
        "This repository's compatibility check verifies two things:\n\n"
        "- the image digest below matches the `container-image.txt` attached to the UA release;\n"
        f"- the published image passes host contract {contract} runtime tests on native AMD64 and ARM64.\n\n"
        "If both pass, the check merges this pull request automatically. If either fails, "
        "the pull request stays open for a person to review.\n\n"
        f"UA release: {release_url}\n\n"
        f"Image: `{image}`\n\n"
        f"{reference}\n"
    )


@dataclass(frozen=True)
class ProposalRequest:
    ua_version: str
    image: str
    container_image_text: str | None = None

    @property
    def branch(self) -> str:
        return f"{BRANCH_PREFIX}v{self.ua_version}"

    @property
    def title(self) -> str:
        return f"Add UA {self.ua_version} to the compatibility list"


def _decode_contents(response: dict[str, Any]) -> str:
    return base64.b64decode(response["content"]).decode("utf-8")


def _contents_path(ref: str) -> str:
    return f"repos/{LAUNCHER_REPOSITORY}/contents/{LIST_PATH}?ref={quote(ref, safe='')}"


def propose(
    api: GitHubApi,
    request: ProposalRequest,
    *,
    dry_run: bool = False,
    out: Callable[[str], None] = print,
) -> None:
    repo = LAUNCHER_REPOSITORY
    main_ref = api.request("GET", f"repos/{repo}/git/ref/heads/{LAUNCHER_BRANCH}")
    main_sha = main_ref["object"]["sha"]
    listed = get_optional(api, _contents_path(main_sha))
    if listed is None:
        raise ProposalError(
            f"The launcher repository's {LAUNCHER_BRANCH} branch has no {LIST_PATH} yet, "
            "so there is no list to add UA "
            f"{request.ua_version} to. Merge the list into {repo} first, then rerun."
        )
    update = add_runtime(_decode_contents(listed), request.ua_version, request.image)
    if update.already_listed:
        out(
            f"UA {request.ua_version} is already on the launcher's compatibility list "
            f"with image {request.image}; nothing to do."
        )
        return

    body = pull_request_body(
        ua_version=request.ua_version,
        image=request.image,
        contract=update.host_contract_version,
        newest=update.newest_listed_version,
        container_image_text=request.container_image_text,
    )
    branch_ref = get_optional(api, f"repos/{repo}/git/ref/heads/{request.branch}")
    owner = repo.split("/")[0]
    open_pulls = api.request(
        "GET",
        f"repos/{repo}/pulls?state=open&base={LAUNCHER_BRANCH}"
        f"&head={quote(f'{owner}:{request.branch}', safe='')}",
    )

    if dry_run:
        out(f"Dry run: nothing was written to {repo}.")
        out(f"Branch: {request.branch} ({'exists, would be reused' if branch_ref else 'would be created from main'})")
        out(f"Commit message and pull request title: {request.title}")
        if open_pulls:
            out(f"Open pull request that would be reused: {open_pulls[0]['html_url']}")
        out("Pull request body:")
        out(body)
        out(f"New {LIST_PATH}:")
        out(update.text)
        return

    if branch_ref is None:
        api.request(
            "POST",
            f"repos/{repo}/git/refs",
            {"ref": f"refs/heads/{request.branch}", "sha": main_sha},
        )
        out(f"Created branch {request.branch} from {LAUNCHER_BRANCH} at {main_sha}.")
        _commit_list(api, request, update.text, listed["sha"])
    else:
        on_branch = get_optional(api, _contents_path(request.branch))
        if on_branch is not None and _decode_contents(on_branch) == update.text:
            out(f"Branch {request.branch} already has this change; reusing it.")
        else:
            # Start the branch again from main so the pull request changes only
            # this file relative to the current list and cannot conflict.
            api.request(
                "PATCH",
                f"repos/{repo}/git/refs/heads/{request.branch}",
                {"sha": main_sha, "force": True},
            )
            out(f"Reset existing branch {request.branch} to {LAUNCHER_BRANCH} at {main_sha}.")
            _commit_list(api, request, update.text, listed["sha"])

    if open_pulls:
        out(f"Reusing open pull request {open_pulls[0]['html_url']}")
        return
    created = api.request(
        "POST",
        f"repos/{repo}/pulls",
        {"title": request.title, "head": request.branch, "base": LAUNCHER_BRANCH, "body": body},
    )
    out(f"Opened pull request {created['html_url']}")


def _commit_list(api: GitHubApi, request: ProposalRequest, text: str, file_sha: str) -> None:
    api.request(
        "PUT",
        f"repos/{LAUNCHER_REPOSITORY}/contents/{LIST_PATH}",
        {
            "message": request.title,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "sha": file_sha,
            "branch": request.branch,
        },
    )


def build_request(
    tag: str,
    *,
    digest: str | None,
    image_repository: str,
    container_image_text: str | None,
) -> ProposalRequest:
    release = parse_release_tag(tag)
    if release.kind is not ReleaseKind.STABLE:
        raise ProposalError(
            f"{tag} is a {release.kind.value} pre-release; only stable releases join the list."
        )
    published = None
    if container_image_text is not None:
        match = IMMUTABLE_IMAGE_LINE.search(container_image_text)
        if match is None:
            raise ProposalError("The container-image.txt file has no 'Immutable image:' line.")
        published = match["image"]
    if digest is None:
        if published is None:
            raise ProposalError("Give --digest or --container-image-file.")
        image = published
    else:
        if not DIGEST_PATTERN.fullmatch(digest):
            raise ProposalError(f"Digest {digest!r} is not sha256:<64 lowercase hex characters>.")
        image = f"{image_repository}@{digest}"
        if published is not None and published != image:
            raise ProposalError(
                f"container-image.txt names {published}, not {image}; check which image was published."
            )
    return ProposalRequest(release.version, image, container_image_text)


def _annotate(level: str, title: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level} title={title}::{message}")
    else:
        print(f"{level.capitalize()}: {message}", file=sys.stderr)


def main(argv: list[str], api: GitHubApi | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=Path(argv[0]).name,
        description="Open a pull request adding a stable UA release to the desktop launcher's compatibility list.",
    )
    parser.add_argument("tag", help="stable release tag, for example v0.9.2")
    parser.add_argument("--digest", help="multi-platform image digest, sha256:...")
    parser.add_argument(
        "--image-repository",
        default=DEFAULT_IMAGE_REPOSITORY,
        help=f"image repository (default: {DEFAULT_IMAGE_REPOSITORY})",
    )
    parser.add_argument(
        "--container-image-file",
        type=Path,
        help="the release's container-image.txt; quoted in the pull request and checked against --digest",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the change without writing anything")
    args = parser.parse_args(argv[1:])
    try:
        text = (
            args.container_image_file.read_text(encoding="utf-8")
            if args.container_image_file
            else None
        )
        request = build_request(
            args.tag,
            digest=args.digest,
            image_repository=args.image_repository,
            container_image_text=text,
        )
        propose(api or GhCliApi(), request, dry_run=args.dry_run)
    except OlderThanListed as exc:
        _annotate("warning", "Launcher compatibility entry not proposed", str(exc))
        return 0
    except (OSError, ValueError, ProposalError, GitHubError) as exc:
        print(f"Launcher compatibility proposal failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
