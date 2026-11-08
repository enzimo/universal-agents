"""Build with a bounded local cache while keeping the Buildx builder disposable.

The installed hook supplies a repository/ref/runner-specific cache directory.
Export to a new directory each time so old OCI blobs cannot accumulate forever.
Only replace the previous snapshot after a successful build and cache export.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import platform
import subprocess
from pathlib import Path

import build_cache
from ci_hook import remove

LOG = logging.getLogger("ua.ci.image-cache")


def build(dockerfile: str, target: str, tag: str, *, context: str = ".") -> None:
    if os.environ.get("UA_CI_IMAGE_CACHE_VERSION") != "1":
        raise RuntimeError("Install the image-cache hooks before using cached builds")
    root = Path(os.environ["UA_CI_IMAGE_CACHE"])
    build_cache.reject_link(root)
    if not root.is_dir():
        raise ValueError("The installed hook must create the image-cache directory")
    key = hashlib.sha256(f"{dockerfile}\n{target}\n{platform.machine()}".encode()).hexdigest()
    current, pending, previous = (root / (key + suffix) for suffix in ("", ".next", ".previous"))
    for path in (current, pending, previous):
        build_cache.reject_link(path)
    # Recover a promotion interrupted between the two renames. A partial export
    # is never imported, and a failed build leaves the last good snapshot intact.
    if previous.exists() and not current.exists():
        previous.rename(current)
    remove(previous)
    remove(pending)
    command = ["rtk", "proxy", "docker", "buildx", "build",
               "--builder", os.environ["UA_CI_BUILDER"], "--load", "--progress", "plain",
               "--label", "com.universalagents.ci.job=" + os.environ["UA_CI_ID"],
               "--file", dockerfile, "--target", target, "--tag", tag,
               "--cache-to", f"type=local,dest={pending},mode=max"]
    if (current / "index.json").is_file():
        command.extend(("--cache-from", f"type=local,src={current}"))
        LOG.info("Importing image-stage cache %s", key)
    else:
        LOG.info("Cold image-stage cache %s", key)
    try:
        subprocess.run([*command, context], check=True)
        if not (pending / "index.json").is_file():
            raise RuntimeError("BuildKit did not export a complete cache snapshot")
        if current.exists():
            current.rename(previous)
        pending.rename(current)
        remove(previous)
    finally:
        remove(pending)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dockerfile", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--context", default=".")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    build(args.dockerfile, args.target, args.tag, context=args.context)


if __name__ == "__main__":
    main()
