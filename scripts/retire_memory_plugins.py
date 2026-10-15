#!/usr/bin/env python3
"""Remove four retired memory-plugin settings from an offline instance config.

Uses only PyYAML and the standard library so config validation cannot prevent
the repair. Stored memories and other plugin settings are never modified.
"""

from __future__ import annotations

import argparse
import logging
import os
import stat
import tempfile
from pathlib import Path
from uuid import uuid4

import yaml

LOGGER = logging.getLogger("retire_memory_plugins")
RETIRED_PLUGIN_IDS = frozenset(
    {
        "long_term_profile_full",
        "memory_graph_retrieval",
        "team_profile_summary",
        "user_profile_summary",
    }
)


def retire_memory_plugins(path: Path, *, apply: bool) -> tuple[list[str], Path | None]:
    """Back up original bytes before atomically replacing the edited config."""
    path = path.expanduser().resolve(strict=True)
    original = path.read_bytes()
    config = yaml.safe_load(original)
    try:
        plugins = config["agent"]["memory"]["plugins"]
        if not isinstance(plugins, dict):
            raise TypeError
        order = plugins.get("order", [])
        if not isinstance(order, list) or any(
            not isinstance(name, str) for name in order
        ):
            raise TypeError
    except (KeyError, TypeError):
        raise ValueError(
            "Expected agent.memory.plugins mapping with an optional list named order."
        ) from None
    removed = sorted((set(plugins) | set(order)) & RETIRED_PLUGIN_IDS)
    if not removed or not apply:
        return removed, None
    for name in removed:
        plugins.pop(name, None)
    if "order" in plugins:
        plugins["order"] = [name for name in order if name not in RETIRED_PLUGIN_IDS]
    replacement = yaml.safe_dump(config, sort_keys=False).encode("utf-8")
    backup = path.with_name(f"{path.name}.before-memory-retirement-{uuid4().hex}.bak")
    with os.fdopen(
        os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
    ) as handle:
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())
    LOGGER.info("Config backup: %s", backup)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(replacement)
            handle.flush()
            os.fchmod(handle.fileno(), stat.S_IMODE(path.stat().st_mode))
            os.fsync(handle.fileno())
        # Fail rather than overwrite a config that changed during preparation.
        if path.read_bytes() != original:
            raise RuntimeError(
                "Config changed during repair; original file was not replaced."
            )
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
            os.replace(temporary, path)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return removed, backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument(
        "--yes",
        action="store_true",
        help="Back up and edit the config with the instance stopped.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    removed, _ = retire_memory_plugins(args.config, apply=args.yes)
    LOGGER.info(
        "%s retired plugin references: %s",
        "Removed" if args.yes else "Would remove",
        ", ".join(removed) or "none",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
