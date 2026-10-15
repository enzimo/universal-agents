#!/usr/bin/env python3
"""Copy the dark background renderer into the offline setup guide.

The setup guide (docker-compose-setup.html) must work as a single downloaded
file, so it embeds the renderer inline instead of loading it. The renderer's
only source is assistant_gateway/onboarding_web/assets/metallic_mesh.js; run
this script after changing it. ``--check`` reports a stale copy without
writing, which the test suite uses.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
RENDERER_SOURCE = REPO_ROOT / "assistant_gateway" / "onboarding_web" / "assets" / "metallic_mesh.js"
SETUP_GUIDE = REPO_ROOT / "docker-compose-setup.html"
BLOCK_START = '<script id="ua-dark-renderer">\n'
BLOCK_END = "</script>"


def embedded_renderer(guide_text: str) -> str:
    """The renderer text currently embedded in the guide."""

    start = guide_text.index(BLOCK_START) + len(BLOCK_START)
    end = guide_text.index(BLOCK_END, start)
    return guide_text[start:end]


def synced_guide(guide_text: str, renderer_text: str) -> str:
    """The guide with its embedded renderer replaced by ``renderer_text``."""

    if BLOCK_END in renderer_text:
        raise ValueError("The renderer must not contain a closing script tag.")
    start = guide_text.index(BLOCK_START) + len(BLOCK_START)
    end = guide_text.index(BLOCK_END, start)
    return guide_text[:start] + renderer_text + guide_text[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Fail when the embedded copy is stale.")
    args = parser.parse_args()
    guide_text = SETUP_GUIDE.read_text(encoding="utf-8")
    renderer_text = RENDERER_SOURCE.read_text(encoding="utf-8")
    if embedded_renderer(guide_text) == renderer_text:
        return 0
    if args.check:
        print(
            f"{SETUP_GUIDE.name} embeds an outdated copy of {RENDERER_SOURCE.name}. "
            f"Run: python scripts/sync_setup_guide_renderer.py",
            file=sys.stderr,
        )
        return 1
    SETUP_GUIDE.write_text(synced_guide(guide_text, renderer_text), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
