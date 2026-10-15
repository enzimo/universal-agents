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
import ipaddress
import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from access_catalog.catalog import CatalogError, ToolCatalog, _http_operations, _tool_fields
from custom_messages.contracts.access_v2 import ProposedCliToolV1, ProposedHttpToolV1, ToolDefinitionV1
from runtime_settings.instance_layout import InstanceLayout

logger = logging.getLogger(__name__)

PROPOSED_FILE_PREFIX = "proposed-"


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
    catalog: ToolCatalog, tools: tuple[ProposedHttpToolV1, ...], clis: tuple[ProposedCliToolV1, ...] = ()
) -> ToolCatalog:
    """A copy of ``catalog`` with the proposed tools added; raises ``CatalogError`` on a clash."""

    candidate = copy.deepcopy(catalog)
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


def write_instance_tool(instance_root: str | Path, tool: ProposedHttpToolV1 | ProposedCliToolV1) -> Path:
    """Write the tool's catalog file; refuses to replace an existing one."""

    directory = InstanceLayout.at(instance_root).instance_tools_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{PROPOSED_FILE_PREFIX}{tool.tool_id.replace(':', '-')}.yaml"
    document = cli_document(tool) if isinstance(tool, ProposedCliToolV1) else tool_document(tool)
    text = yaml.safe_dump({"tools": [document]}, sort_keys=False)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    except FileExistsError as exc:
        raise CatalogError(f"A catalog file for {tool.tool_id} already exists: {path.name}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    logger.warning(
        "A tool was added to the catalog. event=instance_tool_written tool_id=%s hosts=%s path=%s",
        tool.tool_id,
        ",".join(getattr(tool, "hosts", ()) or ()),
        path,
    )
    return path


def remove_instance_tool(path: Path) -> None:
    """Take back a file written for a proposal that could not be applied."""

    try:
        path.unlink()
    except FileNotFoundError:
        return
    logger.warning("A proposed tool's catalog file was removed again. event=instance_tool_removed path=%s", path)


__all__ = [
    "PROPOSED_FILE_PREFIX",
    "cli_document",
    "private_host_names",
    "private_hosts",
    "remove_instance_tool",
    "tool_definition",
    "tool_document",
    "with_proposed_tools",
    "write_instance_tool",
]
