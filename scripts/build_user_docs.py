#!/usr/bin/env python3
"""Build the static browser documentation site from MkDocs sources."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "mkdocs.yml"
DEFAULT_SITE_DIR = REPO_ROOT / "user_docs" / "site"
DEFAULT_COMPOSE_SETUP = REPO_ROOT / "docker-compose-setup.html"
# The dark background renderer has one source file, served by the gateway.
# The built site needs it at assets/metallic_mesh.js (mkdocs.yml lists it).
DARK_RENDERER_SOURCE = REPO_ROOT / "assistant_gateway" / "onboarding_web" / "assets" / "metallic_mesh.js"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build static user docs with MkDocs.")
    parser.add_argument("--config-file", default=str(DEFAULT_CONFIG))
    parser.add_argument("--site-dir", default=str(DEFAULT_SITE_DIR))
    parser.add_argument(
        "--no-strict", action="store_true", help="Disable MkDocs strict mode."
    )
    parser.add_argument(
        "--github-pages",
        action="store_true",
        help="Prepare the built site for deployment as a GitHub Pages artifact.",
    )
    return parser


def _replace_required(content: str, old: str, new: str, *, source: Path) -> str:
    if old not in content:
        raise RuntimeError(
            f"Expected GitHub Pages rewrite target is missing from {source}: {old!r}"
        )
    return content.replace(old, new)


def copy_dark_renderer(site_dir: Path, *, source: Path = DARK_RENDERER_SOURCE) -> None:
    """Place the single dark background renderer where the built pages load it."""

    target = site_dir / "assets" / "metallic_mesh.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())


def prepare_github_pages_artifact(
    site_dir: Path,
    *,
    compose_setup_source: Path = DEFAULT_COMPOSE_SETUP,
) -> None:
    """Add repository-level docs and make their links safe for a Pages subpath."""

    compose_output = site_dir / "docker-compose-setup.html"
    compose_content = compose_setup_source.read_text(encoding="utf-8")
    compose_content = _replace_required(
        compose_content,
        'href="/" aria-label="Universal Agents home"',
        'href="./" aria-label="Universal Agents home"',
        source=compose_setup_source,
    )
    compose_output.write_text(compose_content, encoding="utf-8")
    for page in site_dir.rglob("*.html"):
        content = page.read_text(encoding="utf-8")
        relative = Path(os.path.relpath(compose_output, page.parent)).as_posix()
        updated = content.replace('href="/docker-compose-setup.html"', f'href="{relative}"')
        if updated != content:
            page.write_text(updated, encoding="utf-8")
    (site_dir / ".nojekyll").write_text("", encoding="utf-8")


def main() -> int:
    args = build_arg_parser().parse_args()
    site_dir = Path(args.site_dir).expanduser()
    command = [
        sys.executable,
        "-m",
        "mkdocs",
        "build",
        "--config-file",
        str(Path(args.config_file).expanduser()),
        "--site-dir",
        str(site_dir),
    ]
    if not args.no_strict:
        command.append("--strict")
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
    if completed.returncode != 0:
        return int(completed.returncode)
    copy_dark_renderer(site_dir)
    if args.github_pages:
        prepare_github_pages_artifact(site_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
