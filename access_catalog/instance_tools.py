"""Web API tools and installed CLIs added to an instance's catalog (``control/access/tools``).

The shipped catalog lives in ``ua_definitions/access/tools``. An instance adds
web API tools (``http:*``) in its own control folder, which agents cannot
reach; every process's ``CatalogSource`` reloads when a file there changes. An
approved access proposal (``access_control.proposals``) writes one file per
new tool, built here from the typed proposal item, so nothing an agent wrote
is copied into the catalog as text: only the fields of ``ProposedHttpToolV1``
become YAML, and the loader refuses anything but ``http:*`` tools in these
files. An approved CLI (``ProposedCliToolV1``) is written the same way, as a
``cli:*`` entry that carries its ``installed_cli`` data (program path, pinned
SHA-256, command table); the loader accepts ``cli:*`` entries only with it.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import logging
import os
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from access_catalog.catalog import CatalogError, ToolCatalog, _http_operations, _tool_fields
from access_control import safe_open
from custom_messages.contracts.access_v2 import (
    ProposedCliToolV1,
    ProposedHttpToolChangeV1,
    ProposedHttpToolV1,
    ToolDefinitionV1,
    ToolKind,
)
from runtime_settings.instance_layout import InstanceLayout

logger = logging.getLogger(__name__)

PROPOSED_FILE_PREFIX = "proposed-"


@dataclass(frozen=True)
class ProposedToolFile:
    """One proposal-written ``http:*`` tool file read safely from ``control/access/tools``."""

    path: Path
    raw_bytes: bytes
    sha256: str
    definition: ToolDefinitionV1


def proposed_tool_filename(tool_id: str) -> str:
    """Canonical filename of a proposal-written tool under ``control/access/tools``."""

    return f"{PROPOSED_FILE_PREFIX}{tool_id.replace(':', '-')}.yaml"


def proposed_tool_path(instance_root: str | Path, tool_id: str) -> Path:
    """Canonical path of a proposal-written tool file under ``control/access/tools``."""

    directory = InstanceLayout.at(instance_root).instance_tools_dir
    return directory / proposed_tool_filename(tool_id)


def read_proposed_tool_file(instance_root: str | Path, tool_id: str) -> ProposedToolFile:
    """Read and validate one single-tool proposal-written ``http:*`` file without following links.

    Raises ``FileNotFoundError`` when the tool has no ``proposed-*.yaml`` file
    (such as an operator-managed catalog file), and ``CatalogError`` if the path
    is unsafe, unreadable, malformed, or does not define exactly ``tool_id``.
    """

    directory = InstanceLayout.at(instance_root).instance_tools_dir
    filename = proposed_tool_filename(tool_id)
    path = directory / filename
    try:
        raw_bytes = safe_open.read_bytes(directory, (filename,))
    except safe_open.UnsafePath as exc:
        if exc.reason == safe_open.REASON_NOT_FOUND or (
            exc.reason == safe_open.REASON_ROOT_UNAVAILABLE and isinstance(exc.__cause__, FileNotFoundError)
        ):
            raise FileNotFoundError(str(path)) from exc
        logger.warning(
            "Refused to read an unsafe proposed tool file. event=instance_tool_unsafe_read "
            "tool_id=%s path=%s reason=%s",
            tool_id,
            path,
            exc.reason,
        )
        raise CatalogError(f"Cannot safely read the tool file for {tool_id}: {exc}") from exc
    try:
        parsed = yaml.safe_load(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CatalogError(f"The catalog file {filename} for {tool_id} is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("tools"), list):
        raise CatalogError(f"The catalog file {filename} for {tool_id} must contain a 'tools' list.")
    entries = parsed["tools"]
    if len(entries) != 1 or not isinstance(entries[0], dict):
        raise CatalogError(f"The catalog file {filename} for {tool_id} must define exactly one tool.")
    entry = entries[0]
    if str(entry.get("tool_id") or "").strip() != tool_id:
        raise CatalogError(f"The catalog file {filename} does not match tool {tool_id}.")
    if "installed_cli" in entry:
        raise CatalogError(f"Tool {tool_id} is an installed CLI, not a web API tool.")
    try:
        definition = ToolDefinitionV1(**_tool_fields(entry, _http_operations(entry)))
    except Exception as exc:
        raise CatalogError(f"The catalog file {filename} for {tool_id} is invalid: {exc}") from exc
    if definition.kind is not ToolKind.HTTP:
        raise CatalogError(f"Tool {tool_id} is not a web API tool.")
    return ProposedToolFile(
        path=path,
        raw_bytes=raw_bytes,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        definition=definition,
    )


def tool_document(tool: ProposedHttpToolV1) -> dict[str, Any]:
    """The catalog entry for a proposed web API tool (team-owned, in no tool set)."""

    entry: dict[str, Any] = {
        "tool_id": tool.tool_id,
        "display_name": tool.display_name,
        "description": tool.description,
        "hosts": [host.strip().lower() for host in tool.hosts],
        "account": "linked_credential" if tool.uses_credential else "none",
    }
    if tool.routes:
        entry["routes"] = [route.model_dump(mode="json") for route in tool.routes]
    if tool.credential_injections:
        entry["credential_injections"] = [item.model_dump(mode="json") for item in tool.credential_injections]
    if tool.gate_injections:
        entry["gate_injections"] = [item.model_dump(mode="json") for item in tool.gate_injections]
    if tool.setup is not None:
        # Typed and checked (custom_messages.contracts.tool_setup_spec_v1); set_up_tools reads it.
        entry["setup"] = tool.setup.model_dump(mode="json", exclude_defaults=True)
    return entry


def tool_definition(tool: ProposedHttpToolV1) -> ToolDefinitionV1:
    document = tool_document(tool)
    return ToolDefinitionV1(**_tool_fields(document, _http_operations(document)))


def cli_document(tool: ProposedCliToolV1) -> dict[str, Any]:
    """The catalog entry for a proposed CLI (team-owned, in no tool set)."""

    entry: dict[str, Any] = {
        "tool_id": tool.tool_id,
        "display_name": tool.display_name,
        "description": tool.description,
        "account": "linked_credential" if tool.credential_injections else "none",
        "link_owners": list(tool.link_owners),
        "installed_cli": tool.installed_cli.model_dump(mode="json", exclude_defaults=True),
    }
    if tool.credential_injections:
        entry["credential_injections"] = [item.model_dump(mode="json") for item in tool.credential_injections]
    if tool.setup is not None:
        entry["setup"] = tool.setup.model_dump(mode="json", exclude_defaults=True)
    return entry


def with_proposed_tools(
    catalog: ToolCatalog,
    tools: tuple[ProposedHttpToolV1, ...],
    clis: tuple[ProposedCliToolV1, ...] = (),
    tool_changes: tuple[ProposedHttpToolChangeV1, ...] = (),
) -> ToolCatalog:
    """A copy of ``catalog`` with proposed tools added and changed web API tools replaced; raises ``CatalogError`` on a clash."""

    candidate = copy.deepcopy(catalog)
    seen_changes: set[str] = set()
    for changed in tool_changes:
        if changed.tool_id in seen_changes:
            raise CatalogError(f"Tool {changed.tool_id!r} is listed more than once in tool_changes.")
        seen_changes.add(changed.tool_id)
        existing = candidate.tools.get(changed.tool_id)
        if existing is None or existing.kind is not ToolKind.HTTP:
            raise CatalogError(f"Tool {changed.tool_id!r} is not an existing web API tool.")
        if changed.tool_id in candidate.reached_through or any(
            owner == changed.tool_id for owner in candidate.http_origins.values()
        ):
            raise CatalogError(f"Tool {changed.tool_id!r} cannot be replaced through a web API tool change.")
        for host in existing.hosts:
            if candidate.http_hosts.get(host) == changed.tool_id:
                del candidate.http_hosts[host]
        del candidate.tools[changed.tool_id]
    for changed in tool_changes:
        candidate.add_tool(tool_definition(changed))
    for tool in tools:
        candidate.add_tool(tool_definition(tool))
    for cli in clis:
        candidate.add_installed_cli(cli_document(cli))
    candidate.validate()
    return candidate


def private_hosts(tool: ProposedHttpToolV1) -> list[str]:
    """Hosts that are literal private, loopback or link-local addresses, or local names."""

    return private_host_names(tool.hosts)


def private_host_names(hosts: Iterable[str]) -> list[str]:
    """The names that are literal private, loopback or link-local addresses, or local names.

    Shared by web API tools and MCP servers in access proposals, so a review
    flags the same hosts for both.
    """

    found: list[str] = []
    for host in hosts:
        name = host.strip().lower()
        if name == "localhost" or name.endswith((".local", ".localhost", ".internal")):
            found.append(name)
            continue
        try:
            address = ipaddress.ip_address(name)
        except ValueError:
            continue
        if address.is_private or address.is_loopback or address.is_link_local:
            found.append(name)
    return found


def write_instance_tool(
    instance_root: str | Path, tool: ProposedHttpToolV1 | ProposedCliToolV1,
    *, before_write: Callable[[Path, int, bytes, Path], None] | None = None,
) -> Path:
    """Write the tool's catalog file; refuses to replace an existing one."""

    directory = InstanceLayout.at(instance_root).instance_tools_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = proposed_tool_path(instance_root, tool.tool_id)
    document = cli_document(tool) if isinstance(tool, ProposedCliToolV1) else tool_document(tool)
    text = yaml.safe_dump({"tools": [document]}, sort_keys=False)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".proposed-", suffix=".tmp", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if before_write is not None:
                before_write(path, handle.fileno(), text.encode("utf-8"), temporary)
            handle.write(text)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
        # Publish complete YAML without replacing a different proposal's file.
        # The journal names both links, so recovery also handles a process
        # stopping between this link and removing the temporary name.
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise CatalogError(f"A catalog file for {tool.tool_id} already exists: {path.name}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    logger.warning(
        "A tool was added to the catalog. event=instance_tool_written tool_id=%s hosts=%s path=%s",
        tool.tool_id,
        ",".join(getattr(tool, "hosts", ()) or ()),
        path,
    )
    return path


def replace_instance_tool(
    instance_root: str | Path,
    tool: ProposedHttpToolChangeV1 | ProposedHttpToolV1,
    *,
    expected_previous_sha256: str,
    before_replace: Callable[[Path, int, bytes, Path, bytes], None] | None = None,
    publish: Callable[[Callable[[], None]], None] | None = None,
) -> Path:
    """Replace an existing proposal-written web API tool file atomically if its digest still matches.

    Reads the existing file without following links, verifies its SHA-256
    equals ``expected_previous_sha256``, journals the replacement before writing
    any bytes to the staging file, re-verifies the digest immediately before
    ``os.replace`` (optionally inside ``publish`` such as
    ``decision_effect_transaction``), and replaces the file in one rename.
    """

    directory = InstanceLayout.at(instance_root).instance_tools_dir
    filename = proposed_tool_filename(tool.tool_id)
    try:
        existing = read_proposed_tool_file(instance_root, tool.tool_id)
    except FileNotFoundError as exc:
        logger.warning(
            "Refused to replace a missing proposed tool file. event=instance_tool_replace_missing "
            "tool_id=%s path=%s",
            tool.tool_id,
            directory / filename,
        )
        raise CatalogError(f"The catalog file for {tool.tool_id} no longer exists.") from exc
    if existing.sha256 != expected_previous_sha256:
        logger.warning(
            "Refused to replace a tool file that changed since review. event=instance_tool_replace_conflict "
            "tool_id=%s path=%s",
            tool.tool_id,
            existing.path,
        )
        raise CatalogError(f"The catalog file for {tool.tool_id} changed since it was reviewed.")
    text = yaml.safe_dump({"tools": [tool_document(tool)]}, sort_keys=False)
    new_bytes = text.encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".proposed-", suffix=".tmp", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if before_replace is not None:
                before_replace(existing.path, handle.fileno(), new_bytes, temporary, existing.raw_bytes)
            handle.write(new_bytes)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)

        def _commit_replace() -> None:
            try:
                current_bytes = safe_open.read_bytes(directory, (filename,))
            except safe_open.UnsafePath as exc:
                logger.warning(
                    "Refused to publish replacement because the tool file changed or became unsafe. "
                    "event=instance_tool_replace_conflict tool_id=%s path=%s reason=%s",
                    tool.tool_id,
                    existing.path,
                    exc.reason,
                )
                raise CatalogError(f"The catalog file for {tool.tool_id} changed since it was reviewed.") from exc
            if hashlib.sha256(current_bytes).hexdigest() != expected_previous_sha256:
                logger.warning(
                    "Refused to publish replacement because the tool file changed after journaling. "
                    "event=instance_tool_replace_conflict tool_id=%s path=%s",
                    tool.tool_id,
                    existing.path,
                )
                raise CatalogError(f"The catalog file for {tool.tool_id} changed since it was reviewed.")
            dir_fd = safe_open.open_directory(directory, ())
            try:
                os.replace(temporary.name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            finally:
                os.close(dir_fd)

        if publish is not None:
            publish(_commit_replace)
        else:
            _commit_replace()
    finally:
        temporary.unlink(missing_ok=True)
    logger.warning(
        "A tool definition in the catalog was replaced. event=instance_tool_replaced tool_id=%s hosts=%s path=%s",
        tool.tool_id,
        ",".join(tool.hosts),
        existing.path,
    )
    return existing.path


def restore_instance_tool(path: Path, previous_bytes: bytes) -> None:
    """Restore a replaced proposed tool file to its previous bytes atomically."""

    safe_open.write_bytes(path.parent, (path.name,), previous_bytes)
    logger.warning(
        "A proposed tool's catalog file was restored to its previous definition. event=instance_tool_restored path=%s",
        path,
    )


def remove_instance_tool(path: Path) -> None:
    """Take back a file written for a proposal that could not be applied."""

    try:
        path.unlink()
    except FileNotFoundError:
        return
    logger.warning("A proposed tool's catalog file was removed again. event=instance_tool_removed path=%s", path)


__all__ = [
    "PROPOSED_FILE_PREFIX",
    "ProposedToolFile",
    "cli_document",
    "private_host_names",
    "private_hosts",
    "proposed_tool_filename",
    "proposed_tool_path",
    "read_proposed_tool_file",
    "remove_instance_tool",
    "replace_instance_tool",
    "restore_instance_tool",
    "tool_definition",
    "tool_document",
    "with_proposed_tools",
    "write_instance_tool",
]
