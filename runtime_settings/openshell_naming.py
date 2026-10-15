"""Names of the per-person OpenShell sandboxes (access plan, Phase 5; decision D6).

Each person's code runs in their own OpenShell sandbox, and work for no person
in a shared one (``custom_tools.openshell_sandboxes`` creates them). The
broker names the same sandbox when it gives code a linked account's
environment (``credential.bind_for_openshell``), so the name is computed here,
from the verified person, by both: never taken from a request.
"""

from __future__ import annotations

import hashlib
import re

from runtime_settings.instance_layout import person_folder_name

ENV_SANDBOX_NAME = "OPENSHELL_SANDBOX_NAME"  # the name prefix, set by the launcher
DEFAULT_SANDBOX_PREFIX = "ua"
SHARED_SANDBOX_SUFFIX = "shared"
_MAX_NAME_LENGTH = 63  # sandbox names follow DNS label rules


def _label(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")


def sandbox_prefix(value: str | None) -> str:
    """The name prefix: lowercase letters, digits and '-', at most 24 characters."""

    return (_label(str(value or "")) or DEFAULT_SANDBOX_PREFIX)[:24].rstrip("-") or DEFAULT_SANDBOX_PREFIX


def sandbox_name(prefix: str | None, person_id: str | None) -> str:
    """The sandbox for one person's code, or the shared one for work with no person.

    ``<prefix>-p-<readable part>-<hash of the exact person folder name>``: the
    hash keeps two people apart even when their folder names differ only in
    characters a sandbox name cannot hold, and the ``p`` marker keeps every
    person apart from ``<prefix>-shared``.
    """

    if not person_id:
        return f"{sandbox_prefix(prefix)}-{SHARED_SANDBOX_SUFFIX}"
    folder = person_folder_name(person_id)
    digest = hashlib.sha256(folder.encode("utf-8")).hexdigest()[:10]
    readable = _label(folder)[:20].strip("-") or "person"
    return f"{sandbox_prefix(prefix)}-p-{readable}-{digest}"[:_MAX_NAME_LENGTH]


def is_instance_sandbox(prefix: str | None, name: str) -> bool:
    """Whether ``name`` is one of this instance's sandboxes.

    The per-person and shared names, plus a sandbox named exactly the prefix,
    which versions before per-person sandboxes created for every person.
    """

    base = re.escape(sandbox_prefix(prefix))
    return re.fullmatch(rf"{base}(-{SHARED_SANDBOX_SUFFIX}|-p-[a-z0-9-]+-[0-9a-f]{{10}})?", name) is not None


__all__ = [
    "DEFAULT_SANDBOX_PREFIX",
    "ENV_SANDBOX_NAME",
    "SHARED_SANDBOX_SUFFIX",
    "is_instance_sandbox",
    "sandbox_name",
    "sandbox_prefix",
]
