"""Validate two native image indexes before publishing their combined tags."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def docker(*args: str) -> str:
    return subprocess.run(
        ["rtk", "proxy", "docker", "buildx", "imagetools", *args],
        check=True, capture_output=True, text=True, timeout=300,
    ).stdout


def merge(root: Path, image: str, tags: list[str], *, dry_run: bool = False) -> str | None:
    if {p.name for p in root.iterdir()} != {"amd64.digest", "arm64.digest"}:
        raise ValueError("Expected exactly the two native platform digests")
    if not tags or any(not re.fullmatch(re.escape(image) + r":[A-Za-z0-9_][A-Za-z0-9_.-]*", tag) for tag in tags):
        raise ValueError("Every output tag must belong to the intended image")
    sources = []
    for arch in ("amd64", "arm64"):
        digest = (root / f"{arch}.digest").read_text().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"Invalid {arch} digest")
        source = f"{image}@{digest}"
        index = json.loads(docker("inspect", "--raw", source))
        platforms = []
        for manifest in index["manifests"]:
            if manifest.get("annotations", {}).get("vnd.docker.reference.type") == "attestation-manifest":
                continue
            platform = dict(manifest.get("platform", {}))
            variant = platform.pop("variant", "")
            if variant and not (arch == "arm64" and variant == "v8"):
                raise ValueError(f"Unexpected CPU variant: {variant}")
            platforms.append(platform)
        if platforms != [{"architecture": arch, "os": "linux"}]:
            raise ValueError(f"Unexpected platform in {arch} build: {platforms}")
        sources.append(source)
    arguments = ["create"]
    for tag in dict.fromkeys(tags):
        arguments.extend(["--tag", tag])
    if dry_run:
        arguments.append("--dry-run")
    output = docker(*arguments, *sources)
    if dry_run:
        print(output)
        return None
    descriptor = json.loads(docker("inspect", "--format", "{{json .Manifest}}", tags[0]))
    digest = descriptor["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Docker returned an invalid combined digest")
    return digest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("digests", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = merge(args.digests, os.environ["IMAGE"], os.environ["IMAGE_TAGS"].splitlines(), dry_run=args.dry_run)
    if result:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"digest={result}\n")
        print(f"Published {os.environ['IMAGE']}@{result}")
