#!/usr/bin/env python3
"""Copy reviewed canonical CLI skills, or verify bundled resource parity."""

import argparse
import logging
import shutil
from pathlib import Path

SKILLS = ("xurl", "hf-cli")
ROOT = Path(__file__).resolve().parents[1]


def files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--canonical-root", type=Path, default=ROOT.parent / "ua-skills" / "skills"
    )
    args = parser.parse_args()
    for name in SKILLS:
        source = args.canonical_root / name
        destination = ROOT / "custom_agents" / "skill_templates" / name
        if not (source / "SKILL.md").is_file():
            logging.error("Canonical skill is missing: %s", source)
            return 1
        if not args.check:
            # Remove only obsolete resources from this exact managed skill pair.
            for stale in set(files(destination)) - set(files(source)):
                (destination / stale).unlink()
            shutil.copytree(source, destination, dirs_exist_ok=True)
        if files(source) != files(destination):
            logging.error("Canonical and bundled skill differ: %s", name)
            return 1
    logging.info("Canonical and bundled CLI skills match.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
