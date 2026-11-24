"""Unit and integration tests for reviewed removal of proposal-written tools (tool_removals)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from access_catalog.catalog import INSTALLED_CLI_ACTION, CatalogError, load_catalog
from access_catalog.instance_tools import (
    proposed_tool_path,
    read_proposed_tool_file,
    remove_proposed_tool_file,
    restore_removed_instance_tool,
    with_proposed_tools,
    write_instance_tool,
)
from access_control import safe_open
from custom_messages.contracts.access_v2 import (
    REVIEW_SECTIONS,
    AccessProposalV1,
    ProposedCliToolV1,
    ProposedHttpToolV1,
    ProposedToolRemovalV1,
    ReviewItemV1,
    ToolKind,
)
from custom_messages.contracts.cli_tool_v1 import InstalledCliV1
from custom_messages.contracts.tool_setup_spec_v1 import ToolSetupSpecV1


def _http_tool(tool_id: str = "http:gitea", host: str = "gitea.example.com", *, setup: ToolSetupSpecV1 | None = None) -> ProposedHttpToolV1:
    return ProposedHttpToolV1(
        tool_id=tool_id,
        display_name="Gitea",
        hosts=(host,),
        uses_credential=False,
        setup=setup,
    )


def _cli_tool(name: str = "demo") -> ProposedCliToolV1:
    return ProposedCliToolV1(
        tool_id=f"cli:{name}",
        display_name=f"{name.title()} CLI",
        installed_cli=InstalledCliV1(
            binary_path=f"team/tools/{name}/{name}",
            sha256="a" * 64,
            commands={"version": "read", "deploy": "full"},
        ),
    )


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def test_proposed_tool_removal_contract_and_review_section() -> None:
    assert "tool_removal" in REVIEW_SECTIONS
    row = ReviewItemV1(item_id="remove-0", section="tool_removal", text="Remove Gitea (http:gitea)")
    assert row.section == "tool_removal"

    removal = ProposedToolRemovalV1(tool_id="http:gitea")
    assert removal.expected_sha256 is None
    pinned = ProposedToolRemovalV1(tool_id="cli:demo", expected_sha256="f" * 64)
    assert pinned.expected_sha256 == "f" * 64

    proposal = AccessProposalV1(
        title="Remove Gitea",
        reason="Service retired",
        tool_removals=(removal,),
    )
    assert proposal.installs is True

    with pytest.raises(ValidationError):
        ProposedToolRemovalV1(tool_id="not a tool id")
    with pytest.raises(ValidationError):
        ProposedToolRemovalV1(tool_id="http:gitea", expected_sha256="not-hex")


# ---------------------------------------------------------------------------
# safe_open primitives
# ---------------------------------------------------------------------------


def test_safe_open_remove_file_returns_bool_and_translates_concurrent_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "proposed-http-gitea.yaml"
    target.write_bytes(b"v1")
    info = target.stat()
    wrong_identity = (info.st_dev, info.st_ino, info.st_mtime_ns + 1, info.st_ctime_ns)

    assert safe_open.remove_file(tmp_path, (target.name,), expected_identity=wrong_identity) is False
    assert target.exists()

    exact_identity = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
    assert safe_open.remove_file(tmp_path, (target.name,), expected_identity=exact_identity) is True
    assert not target.exists()

    # Concurrent deletion between os.stat and os.unlink surfaces as UnsafePath(REASON_NOT_FOUND).
    raced = tmp_path / "raced.yaml"
    raced.write_bytes(b"v2")

    def _vanished_unlink(*_args, **_kwargs) -> None:
        raise FileNotFoundError("deleted concurrently")

    monkeypatch.setattr(safe_open.os, "unlink", _vanished_unlink)
    with pytest.raises(safe_open.UnsafePath) as exc_info:
        safe_open.remove_file(tmp_path, (raced.name,))
    assert exc_info.value.reason == safe_open.REASON_NOT_FOUND


def test_safe_open_write_bytes_exclusive_creates_atomically_and_never_overwrites(tmp_path: Path) -> None:
    payload = b"tools:\n- tool_id: http:gitea\n"
    written = safe_open.write_bytes_exclusive(tmp_path, ("proposed-http-gitea.yaml",), payload)
    assert written == len(payload)
    assert (tmp_path / "proposed-http-gitea.yaml").read_bytes() == payload
    assert [p.name for p in tmp_path.iterdir()] == ["proposed-http-gitea.yaml"]

    # Existing regular file is never overwritten.
    with pytest.raises(safe_open.UnsafePath) as exc_info:
        safe_open.write_bytes_exclusive(tmp_path, ("proposed-http-gitea.yaml",), b"other")
    assert exc_info.value.reason == safe_open.REASON_DESTINATION_EXISTS
    assert (tmp_path / "proposed-http-gitea.yaml").read_bytes() == payload
    assert [p.name for p in tmp_path.iterdir()] == ["proposed-http-gitea.yaml"]

    # Existing dangling symlink is also refused without following.
    link = tmp_path / "proposed-http-link.yaml"
    outside = tmp_path / "outside.yaml"
    link.symlink_to(outside)
    with pytest.raises(safe_open.UnsafePath) as exc_info:
        safe_open.write_bytes_exclusive(tmp_path, (link.name,), payload)
    assert exc_info.value.reason == safe_open.REASON_DESTINATION_EXISTS
    assert not outside.exists()


# ---------------------------------------------------------------------------
# instance_tools: read_proposed_tool_file, with_proposed_tools, remove/restore
# ---------------------------------------------------------------------------


def test_read_proposed_tool_file_supports_cli_when_allowed_and_rejects_malformed_entries(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    write_instance_tool(root, _cli_tool("demo"))

    # Default remains HTTP-only for tool_changes.
    with pytest.raises(CatalogError, match="installed CLI"):
        read_proposed_tool_file(root, "cli:demo")

    loaded = read_proposed_tool_file(root, "cli:demo", allowed_kinds=frozenset({ToolKind.HTTP, ToolKind.CLI}))
    assert loaded.definition.tool_id == "cli:demo"
    assert loaded.definition.kind is ToolKind.CLI
    assert loaded.definition.installed_cli is not None
    assert loaded.definition.installed_cli.name == "demo"
    assert {op.name for op in loaded.definition.operations} == {"command:version", "command:deploy"}

    # Unexpected top-level keys are refused.
    extra_top = proposed_tool_path(root, "http:extra")
    extra_top.write_text(
        yaml.safe_dump({"tools": [{"tool_id": "http:extra", "display_name": "Extra", "hosts": ["e.example.com"]}], "extra": 1}),
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="unexpected top-level keys"):
        read_proposed_tool_file(root, "http:extra", allowed_kinds=frozenset({ToolKind.HTTP, ToolKind.CLI}))

    # Non-dict installed_cli is translated to CatalogError (not TypeError).
    bad_cli = proposed_tool_path(root, "cli:bad")
    bad_cli.write_text(
        yaml.safe_dump({"tools": [{"tool_id": "cli:bad", "display_name": "Bad", "installed_cli": None}]}),
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="invalid installed_cli"):
        read_proposed_tool_file(root, "cli:bad", allowed_kinds=frozenset({ToolKind.HTTP, ToolKind.CLI}))

    # CLI file missing installed_cli is refused.
    missing_cli = proposed_tool_path(root, "cli:missing")
    missing_cli.write_text(
        yaml.safe_dump({"tools": [{"tool_id": "cli:missing", "display_name": "Missing"}]}),
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="missing its installed_cli"):
        read_proposed_tool_file(root, "cli:missing", allowed_kinds=frozenset({ToolKind.HTTP, ToolKind.CLI}))


def test_with_proposed_tools_applies_removals_before_additions_and_validates_prerequisites(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    write_instance_tool(root, _http_tool("http:old", "api.example.com"))
    write_instance_tool(root, _cli_tool("demo"))
    catalog = load_catalog(root)

    # Removing http:old frees api.example.com for http:new in the same proposal, and removing cli:demo clears policies/bindings.
    updated = with_proposed_tools(
        catalog,
        tools=(_http_tool("http:new", "api.example.com"),),
        tool_removals=(
            ProposedToolRemovalV1(tool_id="http:old"),
            ProposedToolRemovalV1(tool_id="cli:demo"),
        ),
    )
    assert "http:old" not in updated.tools
    assert "cli:demo" not in updated.tools
    assert "demo" not in updated.cli_policies
    assert ("demo", INSTALLED_CLI_ACTION) not in updated.broker_actions
    assert updated.http_hosts["api.example.com"] == "http:new"

    # Removing a tool that another remaining tool lists as a setup prerequisite fails validation.
    write_instance_tool(
        root,
        _http_tool(
            "http:dependent",
            "dep.example.com",
            setup=ToolSetupSpecV1(prerequisites=("http:old",)),
        ),
    )
    catalog_with_dep = load_catalog(root)
    with pytest.raises(CatalogError, match="http:old"):
        with_proposed_tools(
            catalog_with_dep,
            tools=(),
            tool_removals=(ProposedToolRemovalV1(tool_id="http:old"),),
        )

    # Duplicate or built-in/unknown removals are refused.
    with pytest.raises(CatalogError, match="more than once"):
        with_proposed_tools(
            catalog,
            tools=(),
            tool_removals=(ProposedToolRemovalV1(tool_id="http:old"), ProposedToolRemovalV1(tool_id="http:old")),
        )
    with pytest.raises(CatalogError, match="removable"):
        with_proposed_tools(
            catalog,
            tools=(),
            tool_removals=(ProposedToolRemovalV1(tool_id="http:unknown"),),
        )


def test_remove_proposed_tool_file_verifies_digest_and_restores_exclusively(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "instance"
    write_instance_tool(root, _http_tool("http:gitea", "gitea.example.com"))
    write_instance_tool(root, _cli_tool("demo"))
    loaded_http = read_proposed_tool_file(root, "http:gitea")
    loaded_cli = read_proposed_tool_file(root, "cli:demo", allowed_kinds=frozenset({ToolKind.HTTP, ToolKind.CLI}))

    # Digest mismatch is refused without unlinking.
    caplog.set_level(logging.WARNING)
    with pytest.raises(CatalogError, match="changed since it was reviewed"):
        remove_proposed_tool_file(root, "http:gitea", expected_sha256="0" * 64)
    assert loaded_http.path.exists()
    assert "instance_tool_remove_conflict" in caplog.text

    # Concurrent modification before publish is refused without unlinking.
    concurrent_bytes = b"tools:\n- tool_id: http:gitea\n  display_name: Edited\n  hosts:\n  - gitea.example.com\n"

    def _edit_before_publish(commit) -> None:
        loaded_http.path.write_bytes(concurrent_bytes)
        commit()

    with pytest.raises(CatalogError, match="changed since it was reviewed"):
        remove_proposed_tool_file(
            root, "http:gitea", expected_sha256=loaded_http.sha256, publish=_edit_before_publish
        )
    assert loaded_http.path.read_bytes() == concurrent_bytes

    # Happy path removes both http:* and cli:* proposed files and records before_remove.
    seen: list[tuple[Path, str]] = []
    current_http_sha = hashlib.sha256(concurrent_bytes).hexdigest()
    removed_http = remove_proposed_tool_file(
        root,
        "http:gitea",
        expected_sha256=current_http_sha,
        before_remove=lambda path, raw: seen.append((path, hashlib.sha256(raw).hexdigest())),
    )
    assert removed_http == loaded_http.path
    assert not loaded_http.path.exists()
    assert seen == [(loaded_http.path, current_http_sha)]

    removed_cli = remove_proposed_tool_file(root, "cli:demo", expected_sha256=loaded_cli.sha256)
    assert removed_cli == loaded_cli.path
    assert not loaded_cli.path.exists()

    # Missing file is refused with instance_tool_remove_missing.
    caplog.clear()
    with pytest.raises(CatalogError, match="no longer exists"):
        remove_proposed_tool_file(root, "cli:demo", expected_sha256=loaded_cli.sha256)
    assert "instance_tool_remove_missing" in caplog.text

    # restore_removed_instance_tool restores exact bytes and refuses to overwrite an existing file.
    restore_removed_instance_tool(loaded_cli.path, loaded_cli.raw_bytes)
    assert loaded_cli.path.read_bytes() == loaded_cli.raw_bytes
    with pytest.raises(safe_open.UnsafePath) as exc_info:
        restore_removed_instance_tool(loaded_cli.path, b"different")
    assert exc_info.value.reason == safe_open.REASON_DESTINATION_EXISTS
    assert loaded_cli.path.read_bytes() == loaded_cli.raw_bytes
