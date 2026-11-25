"""Unit and integration tests for reviewed removal of proposal-written tools (tool_removals)."""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
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
from access_control.account_sharing import TeamAccountSharing
from access_control.decision_effects import EffectJournal, RemovedFile
from access_control.entities import PersonPrincipal
from access_control.proposals import (
    ProposalCompiler,
    ProposalRefused,
    proposal_digest,
    proposal_summary,
    with_tool_removal_baselines,
)
from access_control.tool_removals import active_grants_for_tool, tool_removal_rows
from custom_messages.contracts.access_v2 import (
    CHANGE_ITEMS_MAX_COUNT,
    REVIEW_SECTIONS,
    AccessLevel,
    AccessProposalV1,
    AccessRequestStatus,
    AccountLinkStatus,
    AccountLinkV1,
    CredentialInjectionV1,
    GrantKind,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    HttpRouteV1,
    InjectionPlacement,
    LinkOwnerKind,
    LinkPurpose,
    ProposedAccountLinkV1,
    ProposedBlockV1,
    ProposedCliToolV1,
    ProposedGrantV1,
    ProposedHttpToolChangeV1,
    ProposedHttpToolV1,
    ProposedToolRemovalV1,
    ReviewItemV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
    ToolDefinitionV1,
    ToolKind,
    ToolSetupState,
    ToolSetupStateV1,
)
from custom_messages.contracts.cli_tool_v1 import MAX_COMMANDS, InstalledCliV1
from custom_messages.contracts.tool_setup_spec_v1 import ToolSetupSpecV1
from storage.access_repository import AccessRepository, DecisionClaim, DecisionRecovery
from storage.config import resolve_storage_settings
from tests.test_access_store_postgres import NOW, OWNER, TEAM, _request


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

    # Happy path removes both http:* and cli:* proposed files and records before_remove with staging path.
    seen: list[tuple[Path, int, int, str, Path]] = []
    current_http_sha = hashlib.sha256(concurrent_bytes).hexdigest()
    http_stat = loaded_http.path.stat()
    removed_http = remove_proposed_tool_file(
        root,
        "http:gitea",
        expected_sha256=current_http_sha,
        before_remove=lambda path, dev, ino, raw, staging: seen.append(
            (path, dev, ino, hashlib.sha256(raw).hexdigest(), staging)
        ),
    )
    assert removed_http == loaded_http.path
    assert not loaded_http.path.exists()
    assert len(seen) == 1
    assert seen[0][:4] == (loaded_http.path, http_stat.st_dev, http_stat.st_ino, current_http_sha)
    assert seen[0][4].name.startswith(".removed-") and seen[0][4].name.endswith(".tmp")
    # Standalone call (keep_staging=False) unlinks staging file immediately.
    assert not seen[0][4].exists()

    # With keep_staging=True, the staging inode stays on disk and is ignored by *.yaml catalog glob.
    cli_staged: list[tuple[int, int, Path]] = []
    removed_cli = remove_proposed_tool_file(
        root,
        "cli:demo",
        expected_sha256=loaded_cli.sha256,
        before_remove=lambda _path, dev, ino, _raw, staging: cli_staged.append((dev, ino, staging)),
        keep_staging=True,
    )
    assert removed_cli == loaded_cli.path
    assert not loaded_cli.path.exists()
    cli_dev, cli_ino, cli_staging = cli_staged[0]
    assert cli_staging.exists()
    assert "cli:demo" not in load_catalog(root).tools

    # Missing file is refused with instance_tool_remove_missing.
    caplog.clear()
    with pytest.raises(CatalogError, match="no longer exists"):
        remove_proposed_tool_file(root, "cli:demo", expected_sha256=loaded_cli.sha256)
    assert "instance_tool_remove_missing" in caplog.text

    # restore_removed_instance_tool restores exact bytes/inode from staging and refuses to overwrite an existing file.
    restore_removed_instance_tool(loaded_cli.path, cli_staging, expected_device=cli_dev, expected_inode=cli_ino)
    assert loaded_cli.path.read_bytes() == loaded_cli.raw_bytes
    assert not cli_staging.exists()
    other_staging = loaded_cli.path.parent / ".removed-other.tmp"
    other_staging.write_bytes(b"different")
    other_stat = other_staging.stat()
    with pytest.raises(safe_open.UnsafePath) as exc_info:
        restore_removed_instance_tool(
            loaded_cli.path,
            other_staging,
            expected_device=other_stat.st_dev,
            expected_inode=other_stat.st_ino,
        )
    assert exc_info.value.reason == safe_open.REASON_DESTINATION_EXISTS
    assert loaded_cli.path.read_bytes() == loaded_cli.raw_bytes
    other_staging.unlink()


def test_proposal_digest_omits_empty_tool_removals_and_changes_when_non_empty() -> None:
    base = AccessProposalV1(
        title="Add Gitea",
        reason="Inspect repositories.",
        tools=(_http_tool("http:gitea", "gitea.example.com"),),
    )
    with_empty = AccessProposalV1(
        title="Add Gitea",
        reason="Inspect repositories.",
        tools=(_http_tool("http:gitea", "gitea.example.com"),),
        tool_removals=(),
    )
    assert proposal_digest(base) == proposal_digest(with_empty)

    with_removal = AccessProposalV1(
        title="Add Gitea",
        reason="Inspect repositories.",
        tools=(_http_tool("http:gitea", "gitea.example.com"),),
        tool_removals=(ProposedToolRemovalV1(tool_id="http:old"),),
    )
    with_pinned = AccessProposalV1(
        title="Add Gitea",
        reason="Inspect repositories.",
        tools=(_http_tool("http:gitea", "gitea.example.com"),),
        tool_removals=(ProposedToolRemovalV1(tool_id="http:old", expected_sha256="a" * 64),),
    )
    assert proposal_digest(with_removal) != proposal_digest(base)
    assert proposal_digest(with_pinned) != proposal_digest(with_removal)


def test_with_proposed_tools_refuses_builtins_reached_through_and_same_tool_conflicts(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    write_instance_tool(root, _http_tool("http:old", "old.example.com"))
    write_instance_tool(root, _cli_tool("demo"))
    catalog = load_catalog(root)

    # Built-in CLI (installed_cli is None) is refused.
    with pytest.raises(CatalogError, match="not an installed CLI"):
        with_proposed_tools(catalog, tools=(), tool_removals=(ProposedToolRemovalV1(tool_id="cli:github"),))

    # reached_through and http_origins HTTP tools are refused.
    with pytest.raises(CatalogError, match="cannot be removed"):
        with_proposed_tools(catalog, tools=(), tool_removals=(ProposedToolRemovalV1(tool_id="http:exa"),))
    with pytest.raises(CatalogError, match="cannot be removed"):
        with_proposed_tools(catalog, tools=(), tool_removals=(ProposedToolRemovalV1(tool_id="http:crawl4ai"),))

    # Same tool_id in tool_removals and tools / clis / tool_changes is refused with a clear error.
    with pytest.raises(CatalogError, match="both removed and added"):
        with_proposed_tools(
            catalog,
            tools=(_http_tool("http:old", "old.example.com"),),
            tool_removals=(ProposedToolRemovalV1(tool_id="http:old"),),
        )
    with pytest.raises(CatalogError, match="both removed and added"):
        with_proposed_tools(
            catalog,
            tools=(),
            clis=(_cli_tool("demo"),),
            tool_removals=(ProposedToolRemovalV1(tool_id="cli:demo"),),
        )
    with pytest.raises(CatalogError, match="both removed and changed"):
        with_proposed_tools(
            catalog,
            tools=(),
            tool_changes=(
                ProposedHttpToolChangeV1(tool_id="http:old", display_name="Changed", hosts=("old.example.com",)),
            ),
            tool_removals=(ProposedToolRemovalV1(tool_id="http:old"),),
        )


def test_remove_proposed_tool_file_refuses_symlinks_and_concurrent_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "instance"
    write_instance_tool(root, _http_tool("http:gitea", "gitea.example.com"))
    loaded = read_proposed_tool_file(root, "http:gitea")

    # Initial symlink at proposed-*.yaml is refused without touching the target.
    outside = tmp_path / "outside.yaml"
    outside.write_bytes(loaded.raw_bytes)
    sym_path = proposed_tool_path(root, "http:symlink")
    sym_path.symlink_to(outside)
    with pytest.raises(CatalogError, match="Cannot safely read"):
        remove_proposed_tool_file(root, "http:symlink", expected_sha256=loaded.sha256)
    assert outside.read_bytes() == loaded.raw_bytes
    assert sym_path.is_symlink()

    # Swapping to a symlink before publish is refused without touching the target.
    caplog.set_level(logging.WARNING)
    caplog.clear()

    def _swap_to_symlink(commit) -> None:
        loaded.path.unlink()
        loaded.path.symlink_to(outside)
        commit()

    with pytest.raises(CatalogError, match="changed since it was reviewed"):
        remove_proposed_tool_file(root, "http:gitea", expected_sha256=loaded.sha256, publish=_swap_to_symlink)
    assert outside.read_bytes() == loaded.raw_bytes
    assert loaded.path.is_symlink()
    assert "instance_tool_remove_conflict" in caplog.text

    # Restore real file and simulate a concurrent swap between opened_file and safe_open.move_file.
    loaded.path.unlink()
    loaded.path.write_bytes(loaded.raw_bytes)
    real_move = safe_open.move_file
    replacement_bytes = b"tools:\n- tool_id: http:gitea\n  display_name: Swapped\n  hosts:\n  - gitea.example.com\n"

    def _swap_before_move(
        directory: Path,
        source_parts: tuple[str, ...],
        destination_parts: tuple[str, ...],
        *,
        expected_identity=None,
    ) -> bool:
        temp = directory / ".swap.tmp"
        temp.write_bytes(replacement_bytes)
        os.replace(temp, directory / source_parts[0])
        return real_move(directory, source_parts, destination_parts, expected_identity=expected_identity)

    monkeypatch.setattr(safe_open, "move_file", _swap_before_move)
    caplog.clear()
    with pytest.raises(CatalogError, match="changed since it was reviewed"):
        remove_proposed_tool_file(root, "http:gitea", expected_sha256=loaded.sha256)
    assert loaded.path.read_bytes() == replacement_bytes
    assert "reason=identity_changed" in caplog.text


# ---------------------------------------------------------------------------
# RemovedFile & EffectJournal.prepare_removal
# ---------------------------------------------------------------------------


def _access_store(tmp_path: Path) -> AccessRepository:
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    return AccessRepository(settings)


def _journal(store: AccessRepository, claim_id: str = "claim_rm") -> tuple[DecisionClaim, DecisionRecovery, EffectJournal]:
    request = store.create_request(_request().model_copy(update={"request_id": f"req_{claim_id}"}))
    claim = DecisionClaim(request.request_id, claim_id)
    store.claim_decision(request.request_id, claim_id=claim.claim_id, at=NOW, until=NOW + timedelta(minutes=5))
    finished = request.model_copy(
        update={"status": AccessRequestStatus.GRANTED, "decided_by": OWNER, "decided_at_utc": NOW}
    )
    recovery = DecisionRecovery(
        claim,
        finished,
        {"tool_files": [], "replaced_tool_files": [], "removed_tool_files": [], "programs": [], "servers": []},
        NOW + timedelta(minutes=5),
    )
    store.save_decision_recovery(recovery)
    return claim, recovery, EffectJournal(store, recovery)


def test_removed_file_restore_handles_pre_unlink_post_unlink_external_files_and_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "instance"
    write_instance_tool(root, _http_tool("http:gitea", "gitea.example.com"))
    initial = read_proposed_tool_file(root, "http:gitea")
    directory = initial.path.parent
    info = initial.path.stat()
    staging = directory / ".removed-unit-test.tmp"
    store = _access_store(tmp_path)
    _claim, _recovery, journal = _journal(store)

    entry = journal.prepare_removal(
        "removed_tool_files",
        (initial.path.name,),
        info.st_dev,
        info.st_ino,
        initial.raw_bytes,
        staging_parts=(staging.name,),
    )
    reloaded = RemovedFile.model_validate(store.decision_recoveries(TEAM)[0].effects["removed_tool_files"][0])
    assert reloaded == entry

    # Crash before staging move: staging missing, original intact -> quiet no-op.
    caplog.set_level(logging.WARNING)
    caplog.clear()
    reloaded.restore(directory)
    assert initial.path.read_bytes() == initial.raw_bytes
    assert caplog.text == ""

    # F1: crash before staging move followed by another decision legitimately deleting target ->
    # staging is missing so restore MUST NOT resurrect the deleted file!
    initial.path.unlink()
    caplog.clear()
    reloaded.restore(directory)
    assert not initial.path.exists()
    assert caplog.text == ""

    # Recreate original file and move to this claim's staging inode.
    initial.path.write_bytes(initial.raw_bytes)
    info = initial.path.stat()
    entry = RemovedFile.prepared(
        (initial.path.name,), info.st_dev, info.st_ino, initial.raw_bytes, staging_parts=(staging.name,)
    )
    os.rename(initial.path, staging)
    assert not initial.path.exists() and staging.exists()

    # Crash after staging move: restores exact previous bytes/inode and unlinks staging; second pass is quiet no-op.
    entry.restore(
        directory,
        lambda: restore_removed_instance_tool(
            initial.path, staging, expected_device=entry.device, expected_inode=entry.inode
        ),
    )
    assert initial.path.read_bytes() == initial.raw_bytes
    assert not staging.exists()
    assert "instance_tool_restored" in caplog.text
    assert "access_decision_artifact_changed" not in caplog.text

    caplog.clear()
    entry.restore(directory)
    assert caplog.text == ""

    # Crash right between os.link(staging, target) and os.unlink(staging): target already holds our inode ->
    # unlinks staging quietly without logging artifact_changed.
    os.link(initial.path, staging)
    caplog.clear()
    entry.restore(directory)
    assert initial.path.read_bytes() == initial.raw_bytes
    assert not staging.exists()
    assert caplog.text == ""

    # External different file at target while our staging exists -> external kept, staging discarded, logged.
    external = b"tools:\n- tool_id: http:gitea\n  display_name: External\n  hosts:\n  - gitea.example.com\n"
    os.rename(initial.path, staging)
    initial.path.write_bytes(external)
    caplog.clear()
    entry.restore(directory)
    assert initial.path.read_bytes() == external
    assert not staging.exists()
    assert "access_decision_artifact_changed" in caplog.text

    # External symlink at target while our staging exists -> symlink & target untouched, staging discarded, logged.
    outside = tmp_path / "outside.yaml"
    outside.write_bytes(b"keep-me")
    initial.path.unlink()
    initial.path.write_bytes(initial.raw_bytes)
    info = initial.path.stat()
    entry = RemovedFile.prepared(
        (initial.path.name,), info.st_dev, info.st_ino, initial.raw_bytes, staging_parts=(staging.name,)
    )
    os.rename(initial.path, staging)
    initial.path.symlink_to(outside)
    caplog.clear()
    entry.restore(directory)
    assert initial.path.is_symlink()
    assert outside.read_bytes() == b"keep-me"
    assert not staging.exists()
    assert "access_decision_artifact_changed" in caplog.text

    # External directory at target while our staging exists -> directory untouched, staging discarded, logged.
    initial.path.unlink()
    initial.path.write_bytes(initial.raw_bytes)
    info = initial.path.stat()
    entry = RemovedFile.prepared(
        (initial.path.name,), info.st_dev, info.st_ino, initial.raw_bytes, staging_parts=(staging.name,)
    )
    os.rename(initial.path, staging)
    initial.path.mkdir()
    caplog.clear()
    entry.restore(directory)
    assert initial.path.is_dir()
    assert not staging.exists()
    assert "access_decision_artifact_changed" in caplog.text
    initial.path.rmdir()

    # Unsafe parent directory (symlink) re-raises so recovery retries.
    bad_root = tmp_path / "bad_root"
    bad_root.mkdir()
    real_sub = tmp_path / "real_sub"
    real_sub.mkdir()
    (bad_root / "sub").symlink_to(real_sub)
    nested = RemovedFile.prepared(
        ("sub", "proposed-http-gitea.yaml"),
        info.st_dev,
        info.st_ino,
        initial.raw_bytes,
        staging_parts=("sub", ".removed-nested.tmp"),
    )
    with pytest.raises(safe_open.UnsafePath) as exc_info:
        nested.restore(bad_root)
    assert exc_info.value.reason == safe_open.REASON_LINK

    # Concurrent creation race (REASON_DESTINATION_EXISTS): differing bytes logs & discards staging.
    initial.path.write_bytes(initial.raw_bytes)
    info = initial.path.stat()
    entry = RemovedFile.prepared(
        (initial.path.name,), info.st_dev, info.st_ino, initial.raw_bytes, staging_parts=(staging.name,)
    )
    os.rename(initial.path, staging)

    def _race_different() -> None:
        initial.path.write_bytes(external)
        raise safe_open.UnsafePath("exists", reason=safe_open.REASON_DESTINATION_EXISTS)

    caplog.clear()
    entry.restore(directory, _race_different)
    assert initial.path.read_bytes() == external
    assert not staging.exists()
    assert "access_decision_artifact_changed" in caplog.text

    # prepare_removal drops unsaved in-memory entry when save_decision_recovery fails.
    real_save = store.save_decision_recovery

    def _failing_save(rec: DecisionRecovery) -> None:
        real_save(rec)
        raise RuntimeError("db dropped after commit")

    monkeypatch.setattr(store, "save_decision_recovery", _failing_save)
    before_len = len(_recovery.effects["removed_tool_files"])
    with pytest.raises(RuntimeError, match="db dropped"):
        journal.prepare_removal(
            "removed_tool_files",
            (initial.path.name,),
            info.st_dev,
            info.st_ino,
            initial.raw_bytes,
            staging_parts=(".removed-fail.tmp",),
        )
    assert len(_recovery.effects["removed_tool_files"]) == before_len


# ---------------------------------------------------------------------------
# Atomic DB cleanup (apply_together + revoke_links_for_tools)
# ---------------------------------------------------------------------------


def _grant(
    grant_id: str,
    *,
    kind: GrantKind = GrantKind.ACCESS,
    target_kind: TargetKind = TargetKind.TOOL,
    target_id: str = "http:api",
    status: GrantStatus = GrantStatus.ACTIVE,
    parent_grant_id: str | None = None,
) -> GrantV1:
    return GrantV1(
        grant_id=grant_id,
        team_id=TEAM,
        kind=kind,
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id="TeamLead"),
        target=TargetRefV1(kind=target_kind, id=target_id),
        level=AccessLevel.READ,
        lifetime=GrantLifetime.UNTIL_REVOKED,
        status=status,
        granted_by=OWNER,
        created_at_utc=NOW,
        parent_grant_id=parent_grant_id,
    )


def _link(link_id: str, *, tool_id: str, owner_kind: LinkOwnerKind, owner_id: str, purpose: LinkPurpose) -> AccountLinkV1:
    injections = (
        (CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate-Secret", field="secret"),)
        if purpose is LinkPurpose.GATE
        else ()
    )
    return AccountLinkV1(
        link_id=link_id,
        team_id=TEAM,
        tool_id=tool_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        purpose=purpose,
        credential_key=f"cred_{link_id}",
        injections=injections,
        status=AccountLinkStatus.ACTIVE,
        created_by=OWNER,
        created_at_utc=NOW,
    )


def _setup_state(person_id: str, tool_id: str) -> ToolSetupStateV1:
    return ToolSetupStateV1(
        team_id=TEAM,
        person_id=person_id,
        tool_id=tool_id,
        state=ToolSetupState.READY,
        how="team_link",
        source="gateway",
        checked_at_utc=NOW,
        changed_at_utc=NOW,
    )


def test_apply_together_atomically_cleans_up_removed_tool_and_rolls_back_on_refusal(tmp_path: Path) -> None:
    store = _access_store(tmp_path)
    later = datetime(2026, 11, 24, 16, 0, tzinfo=timezone.utc)

    # Grants and blocks targeting http:api (tool + operation), plus a sibling dotted tool http:api.v2.
    g_parent = store.create(_grant("g_parent", target_id="http:api"))
    g_child = store.create(_grant("g_child", target_id="http:api", parent_grant_id=g_parent.grant_id))
    g_pending = store.create(_grant("g_pending", target_id="http:api", status=GrantStatus.PENDING))
    b_tool = store.create(_grant("b_tool", kind=GrantKind.BLOCK, target_id="http:api"))
    b_op = store.create(
        _grant("b_op", kind=GrantKind.BLOCK, target_kind=TargetKind.OPERATION, target_id="http:api#route:delete")
    )
    g_sibling = store.create(_grant("g_sibling", target_id="http:api.v2"))
    b_sibling_op = store.create(
        _grant(
            "b_sibling_op",
            kind=GrantKind.BLOCK,
            target_kind=TargetKind.OPERATION,
            target_id="http:api.v2#route:delete",
        )
    )

    # Active links across team/person owners and account/gate purposes, plus sibling link.
    l_team_acct = store.create_link(
        _link("l_team_acct", tool_id="http:api", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM, purpose=LinkPurpose.ACCOUNT)
    )
    l_person_acct = store.create_link(
        _link("l_person_acct", tool_id="http:api", owner_kind=LinkOwnerKind.PERSON, owner_id="alice", purpose=LinkPurpose.ACCOUNT)
    )
    l_team_gate = store.create_link(
        _link("l_team_gate", tool_id="http:api", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM, purpose=LinkPurpose.GATE)
    )
    l_sibling = store.create_link(
        _link("l_sibling", tool_id="http:api.v2", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM, purpose=LinkPurpose.ACCOUNT)
    )

    # Setup states and account-sharing rows for http:api and sibling http:api.v2 (F4).
    from custom_messages.contracts.access_v2 import AccountSharing, AccountSharingV1

    store.put_tool_setup_state(_setup_state("alice", "http:api"))
    store.put_tool_setup_state(_setup_state("bob", "http:api"))
    store.put_tool_setup_state(_setup_state("alice", "http:api.v2"))
    store.set_account_sharing(
        AccountSharingV1(
            team_id=TEAM,
            subject="http:api",
            sharing=AccountSharing.PRIVATE,
            changed_by_person_id="owner-1",
            changed_at_utc=NOW,
        )
    )
    store.set_account_sharing(
        AccountSharingV1(
            team_id=TEAM,
            subject="http:api.v2",
            sharing=AccountSharing.PRIVATE,
            changed_by_person_id="owner-1",
            changed_at_utc=NOW,
        )
    )

    # If check_tool_removal_grants refuses, the whole transaction rolls back cleanly.
    seen_checked: list[str] = []
    version_before = store.version(TEAM)

    def _refuse(grants: list[GrantV1]) -> None:
        seen_checked.extend(g.grant_id for g in grants)
        raise PermissionError("not allowed to revoke")

    with pytest.raises(PermissionError, match="not allowed to revoke"):
        store.apply_together(
            TEAM,
            revoke_tool_ids=("http:api",),
            check_tool_removal_grants=_refuse,
            actor=OWNER,
            at=later,
        )
    assert set(seen_checked) == {g_parent.grant_id, g_child.grant_id, g_pending.grant_id, b_tool.grant_id, b_op.grant_id}
    assert store.version(TEAM) == version_before
    assert store.get(g_parent.grant_id).status is GrantStatus.ACTIVE  # type: ignore[union-attr]
    assert store.get_link(l_team_acct.link_id).status is AccountLinkStatus.ACTIVE  # type: ignore[union-attr]
    assert len(store.tool_setup_states(TEAM)) == 3
    assert store.account_sharing(TEAM, "http:api") is not None

    # When check passes, everything for http:api is cleaned up atomically and http:api.v2 stays untouched.
    store.apply_together(
        TEAM,
        revoke_tool_ids=("http:api",),
        check_tool_removal_grants=lambda _grants: None,
        actor=OWNER,
        at=later,
    )
    assert store.version(TEAM) == version_before + 1
    for gid in (g_parent.grant_id, g_child.grant_id, g_pending.grant_id, b_tool.grant_id, b_op.grant_id):
        revoked = store.get(gid)
        assert revoked is not None and revoked.status is GrantStatus.REVOKED
    assert store.get(g_sibling.grant_id).status is GrantStatus.ACTIVE  # type: ignore[union-attr]
    assert store.get(b_sibling_op.grant_id).status is GrantStatus.ACTIVE  # type: ignore[union-attr]

    for lid in (l_team_acct.link_id, l_person_acct.link_id, l_team_gate.link_id):
        link = store.get_link(lid)
        assert link is not None and link.status is AccountLinkStatus.REVOKED
        assert link.revoked_by == OWNER
    assert store.get_link(l_sibling.link_id).status is AccountLinkStatus.ACTIVE  # type: ignore[union-attr]

    remaining_states = store.tool_setup_states(TEAM)
    assert [(s.person_id, s.tool_id) for s in remaining_states] == [("alice", "http:api.v2")]
    assert store.account_sharing(TEAM, "http:api") is None
    assert store.account_sharing(TEAM, "http:api.v2") is not None


def test_revoke_links_for_tools_revokes_links_grants_sharing_clears_setup_states_and_bumps_version(
    tmp_path: Path,
) -> None:
    from custom_messages.contracts.access_v2 import AccountSharing, AccountSharingV1

    store = _access_store(tmp_path)
    later = datetime(2026, 11, 24, 16, 5, tzinfo=timezone.utc)

    store.create(_grant("g_old_active", target_id="http:gitea"))
    store.create(_grant("g_old_pending", target_id="http:gitea", status=GrantStatus.PENDING))
    store.create(_grant("g_other_keep", target_id="http:other"))
    store.create_link(
        _link("l_acct", tool_id="http:gitea", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM, purpose=LinkPurpose.ACCOUNT)
    )
    store.create_link(
        _link("l_gate", tool_id="http:gitea", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM, purpose=LinkPurpose.GATE)
    )
    store.create_link(
        _link("l_other", tool_id="http:other", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM, purpose=LinkPurpose.ACCOUNT)
    )
    store.put_tool_setup_state(_setup_state("alice", "http:gitea"))
    store.put_tool_setup_state(_setup_state("alice", "http:other"))
    store.set_account_sharing(
        AccountSharingV1(
            team_id=TEAM,
            subject="http:gitea",
            sharing=AccountSharing.PRIVATE,
            changed_by_person_id="owner-1",
            changed_at_utc=NOW,
        )
    )
    store.set_account_sharing(
        AccountSharingV1(
            team_id=TEAM,
            subject="http:other",
            sharing=AccountSharing.PRIVATE,
            changed_by_person_id="owner-1",
            changed_at_utc=NOW,
        )
    )

    version_before = store.version(TEAM)
    revoked = store.revoke_links_for_tools(TEAM, ("http:gitea",), actor=OWNER, at=later)
    assert set(revoked) == {"l_acct", "l_gate"}
    assert store.version(TEAM) == version_before + 1
    assert store.get("g_old_active").status is GrantStatus.REVOKED  # type: ignore[union-attr]
    assert store.get("g_old_pending").status is GrantStatus.REVOKED  # type: ignore[union-attr]
    assert store.get("g_other_keep").status is GrantStatus.ACTIVE  # type: ignore[union-attr]
    assert store.get_link("l_acct").status is AccountLinkStatus.REVOKED  # type: ignore[union-attr]
    assert store.get_link("l_gate").status is AccountLinkStatus.REVOKED  # type: ignore[union-attr]
    assert store.get_link("l_other").status is AccountLinkStatus.ACTIVE  # type: ignore[union-attr]
    assert [(s.person_id, s.tool_id) for s in store.tool_setup_states(TEAM)] == [("alice", "http:other")]
    assert store.account_sharing(TEAM, "http:gitea") is None
    assert store.account_sharing(TEAM, "http:other") is not None

    # Second call with no active links, grants, or sharing rows left does not bump the version.
    assert store.revoke_links_for_tools(TEAM, ("http:gitea",), actor=OWNER, at=later) == []
    assert store.version(TEAM) == version_before + 1


def test_active_grants_for_tool_in_store_filters_expired_spent_revoked_and_siblings(tmp_path: Path) -> None:
    """F8: unit test for active_grants_for_tool_in_store querying real AccessRepository."""

    from access_control.tool_removals import active_grants_for_tool_in_store

    store = _access_store(tmp_path)
    at = datetime(2026, 11, 24, 16, 0, tzinfo=timezone.utc)
    for item in (
        _grant("g_live", target_id="http:api"),
        _grant("g_pending", target_id="http:api", status=GrantStatus.PENDING),
        _grant("b_tool", kind=GrantKind.BLOCK, target_id="http:api"),
        _grant("b_op", kind=GrantKind.BLOCK, target_kind=TargetKind.OPERATION, target_id="http:api#route:delete"),
        _grant("g_expired", target_id="http:api").model_copy(
            update={"lifetime": GrantLifetime.UNTIL, "expires_at_utc": at - timedelta(seconds=1)}
        ),
        _grant("g_spent", target_id="http:api").model_copy(
            update={"lifetime": GrantLifetime.ONE_TIME, "uses_remaining": 0, "task_id": "t-1"}
        ),
        _grant("g_revoked", target_id="http:api", status=GrantStatus.REVOKED),
        _grant("g_denied", target_id="http:api", status=GrantStatus.DENIED),
        _grant("g_sibling", target_id="http:api.v2"),
        _grant("b_sibling_op", kind=GrantKind.BLOCK, target_kind=TargetKind.OPERATION, target_id="http:api.v2#route:delete"),
    ):
        store.create(item)

    found = active_grants_for_tool_in_store(store, TEAM, "http:api", now=at)
    assert [g.grant_id for g in found] == ["b_tool", "g_live", "g_pending", "b_op"]


def test_apply_together_omits_expired_and_spent_active_grants_from_check_but_still_revokes_them(
    tmp_path: Path,
) -> None:
    store = _access_store(tmp_path)
    at = datetime(2026, 11, 24, 16, 0, tzinfo=timezone.utc)

    g_live = store.create(_grant("g_live", target_id="http:api"))
    g_expired = store.create(
        _grant("g_expired", target_id="http:api").model_copy(
            update={"lifetime": GrantLifetime.UNTIL, "expires_at_utc": at - timedelta(minutes=5)}
        )
    )
    g_exact_expiry = store.create(
        _grant("g_exact_expiry", target_id="http:api").model_copy(
            update={"lifetime": GrantLifetime.UNTIL, "expires_at_utc": at}
        )
    )
    g_spent = store.create(
        _grant("g_spent", target_id="http:api").model_copy(
            update={"lifetime": GrantLifetime.ONE_TIME, "uses_remaining": 0, "task_id": "task-1"}
        )
    )

    checked: list[str] = []
    store.apply_together(
        TEAM,
        revoke_tool_ids=("http:api",),
        check_tool_removal_grants=lambda grants: checked.extend(g.grant_id for g in grants),
        actor=OWNER,
        at=at,
    )
    assert checked == [g_live.grant_id]
    for gid in (g_live.grant_id, g_expired.grant_id, g_exact_expiry.grant_id, g_spent.grant_id):
        row = store.get(gid)
        assert row is not None and row.status is GrantStatus.REVOKED

    # When only expired/spent rows exist for a removed tool, check_tool_removal_grants is not called,
    # yet stale rows are still revoked cleanly.
    g_only_expired = store.create(
        _grant("g_only_expired", target_id="http:stale").model_copy(
            update={"lifetime": GrantLifetime.UNTIL, "expires_at_utc": at - timedelta(seconds=1)}
        )
    )
    g_only_spent = store.create(
        _grant("g_only_spent", target_id="http:stale").model_copy(
            update={"lifetime": GrantLifetime.ONE_TIME, "uses_remaining": 0, "task_id": "task-1"}
        )
    )
    invocations = 0

    def _fail_if_called(grants: list[GrantV1]) -> None:
        nonlocal invocations
        invocations += 1
        raise AssertionError(f"Unexpected check on dead grants: {[g.grant_id for g in grants]}")

    store.apply_together(
        TEAM,
        revoke_tool_ids=("http:stale",),
        check_tool_removal_grants=_fail_if_called,
        actor=OWNER,
        at=at,
    )
    assert invocations == 0
    for gid in (g_only_expired.grant_id, g_only_spent.grant_id):
        row = store.get(gid)
        assert row is not None and row.status is GrantStatus.REVOKED


# ---------------------------------------------------------------------------
# ProposalCompiler validation, conflicts, and review rows (A1 + A2)
# ---------------------------------------------------------------------------


def _compiler(
    root: Path,
    store: AccessRepository,
    *,
    grants: list[GrantV1] | None = None,
    links: list[AccountLinkV1] | None = None,
) -> ProposalCompiler:
    catalog = load_catalog(root)
    all_grants = list(grants or ())
    all_links = list(links or ())
    return ProposalCompiler(
        catalog=catalog,
        team_id=TEAM,
        holds=lambda _person, _target, _level: True,
        account_sharing=TeamAccountSharing(store, TEAM),
        instance_root=root,
        grants_for_tool=lambda tool_id: [g for g in all_grants if g.target.id.partition("#")[0] == tool_id],
        links_for_tool=lambda tool_id: [link for link in all_links if link.tool_id == tool_id],
    )


def test_proposal_compiler_refuses_ineligible_tool_removals_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "instance"
    store = _access_store(tmp_path)
    write_instance_tool(root, _http_tool("http:gitea", "gitea.example.com"))
    write_instance_tool(root, _cli_tool("demo"))

    # Operator hand-written catalog file (not proposed-*.yaml).
    operator_file = proposed_tool_path(root, "http:gitea").parent / "custom.yaml"
    operator_file.write_text(
        yaml.safe_dump({"tools": [{"tool_id": "http:custom", "display_name": "Custom", "hosts": ["c.example.com"]}]}),
        encoding="utf-8",
    )

    # Symlinked proposed-*.yaml (unsafe file).
    outside = tmp_path / "outside.yaml"
    outside.write_text(
        yaml.safe_dump({"tools": [{"tool_id": "http:sym", "display_name": "Sym", "hosts": ["sym.example.com"]}]}),
        encoding="utf-8",
    )
    sym_path = proposed_tool_path(root, "http:sym")
    sym_path.symlink_to(outside)

    compiler = _compiler(root, store)
    # Add an active MCP server to the catalog to prove mcp:* is refused even when present.
    compiler.catalog.tools["mcp:linear"] = ToolDefinitionV1(
        tool_id="mcp:linear",
        kind=ToolKind.MCP,
        display_name="Linear",
    )
    person = PersonPrincipal("owner-1", TEAM, ("owner",))

    cases = [
        ("http:exa", "builtin_tool"),
        ("cli:github", "builtin_tool"),
        ("fn:access", "builtin_tool"),
        ("mcp:linear", "mcp_tool"),
        ("mcp:other", "mcp_tool"),
        ("http:missing", "unknown_tool"),
        ("http:custom", "operator_managed_file"),
    ]
    caplog.set_level(logging.WARNING)
    for tool_id, reason in cases:
        caplog.clear()
        proposal = AccessProposalV1(
            title=f"Remove {tool_id}",
            reason="No longer needed.",
            tool_removals=(ProposedToolRemovalV1(tool_id=tool_id, expected_sha256="a" * 64),),
        )
        with pytest.raises(ProposalRefused):
            compiler.compile(proposal, person=person, task_id="task-1")
        assert "event=proposal_tool_removal_refused" in caplog.text
        assert f"reason={reason}" in caplog.text

    # Symlinked proposed file: inject tool into catalog so catalog lookup succeeds, then safe_open refuses the symlink.
    compiler.catalog.tools["http:sym"] = compiler.catalog.tools["http:gitea"].model_copy(update={"tool_id": "http:sym"})
    caplog.clear()
    with pytest.raises(ProposalRefused, match="cannot be removed"):
        compiler.compile(
            AccessProposalV1(
                title="Remove sym",
                reason="Unsafe.",
                tool_removals=(ProposedToolRemovalV1(tool_id="http:sym", expected_sha256="a" * 64),),
            ),
            person=person,
            task_id="task-1",
        )
    assert "event=proposal_tool_removal_refused" in caplog.text
    assert "reason=invalid_proposed_file" in caplog.text

    # Missing baseline digest and mismatched baseline digest are refused.
    caplog.clear()
    with pytest.raises(ProposalRefused, match="missing its baseline digest"):
        compiler.compile(
            AccessProposalV1(
                title="Remove Gitea",
                reason="Retired.",
                tool_removals=(ProposedToolRemovalV1(tool_id="http:gitea"),),
            ),
            person=person,
            task_id="task-1",
        )
    assert "reason=missing_baseline" in caplog.text

    caplog.clear()
    with pytest.raises(ProposalRefused, match="changed after the proposal was prepared"):
        compiler.compile(
            AccessProposalV1(
                title="Remove Gitea",
                reason="Retired.",
                tool_removals=(ProposedToolRemovalV1(tool_id="http:gitea", expected_sha256="0" * 64),),
            ),
            person=person,
            task_id="task-1",
        )
    assert "reason=baseline_changed" in caplog.text

    # Removing a tool that another tool lists as a setup prerequisite fails with an accurate removal message.
    write_instance_tool(
        root,
        _http_tool(
            "http:dependent",
            "dep.example.com",
            setup=ToolSetupSpecV1(prerequisites=("http:gitea",)),
        ),
    )
    dep_compiler = _compiler(root, store)
    pinned = with_tool_removal_baselines(
        AccessProposalV1(
            title="Remove Gitea",
            reason="Retired.",
            tool_removals=(ProposedToolRemovalV1(tool_id="http:gitea"),),
        ),
        instance_root=root,
        catalog=dep_compiler.catalog,
    )
    with pytest.raises(ProposalRefused, match=r"A proposed tool cannot be removed:.*http:gitea"):
        dep_compiler.compile(pinned, person=person, task_id="task-1")


def test_proposal_compiler_refuses_duplicate_and_same_proposal_conflicts_and_pins_baselines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "instance"
    store = _access_store(tmp_path)
    write_instance_tool(root, _http_tool("http:gitea", "gitea.example.com"))
    write_instance_tool(root, _cli_tool("demo"))
    loaded_http = read_proposed_tool_file(root, "http:gitea")
    loaded_cli = read_proposed_tool_file(root, "cli:demo", allowed_kinds=frozenset({ToolKind.HTTP, ToolKind.CLI}))
    compiler = _compiler(root, store)
    person = PersonPrincipal("owner-1", TEAM, ("owner",))

    # with_tool_removal_baselines overwrites any caller-supplied digest with the actual file SHA-256.
    pinned = with_tool_removal_baselines(
        AccessProposalV1(
            title="Remove tools",
            reason="Retired.",
            tool_removals=(
                ProposedToolRemovalV1(tool_id="http:gitea", expected_sha256="0" * 64),
                ProposedToolRemovalV1(tool_id="cli:demo"),
            ),
        ),
        instance_root=root,
        catalog=compiler.catalog,
    )
    assert pinned.tool_removals[0].expected_sha256 == loaded_http.sha256
    assert pinned.tool_removals[1].expected_sha256 == loaded_cli.sha256

    conflicts = [
        (
            "duplicate_removal",
            AccessProposalV1(
                title="Dup",
                reason="Retired.",
                tool_removals=(
                    ProposedToolRemovalV1(tool_id="http:gitea", expected_sha256=loaded_http.sha256),
                    ProposedToolRemovalV1(tool_id="http:gitea", expected_sha256=loaded_http.sha256),
                ),
            ),
        ),
        (
            "conflict_with_add",
            AccessProposalV1(
                title="Remove+add http",
                reason="Retired.",
                tools=(_http_tool("http:gitea", "gitea.example.com"),),
                tool_removals=(ProposedToolRemovalV1(tool_id="http:gitea", expected_sha256=loaded_http.sha256),),
            ),
        ),
        (
            "conflict_with_add",
            AccessProposalV1(
                title="Remove+add cli",
                reason="Retired.",
                clis=(_cli_tool("demo"),),
                tool_removals=(ProposedToolRemovalV1(tool_id="cli:demo", expected_sha256=loaded_cli.sha256),),
            ),
        ),
        (
            "conflict_with_change",
            AccessProposalV1(
                title="Remove+change",
                reason="Retired.",
                tool_changes=(
                    ProposedHttpToolChangeV1(
                        tool_id="http:gitea",
                        display_name="Gitea 2",
                        hosts=("gitea.example.com",),
                        previous_definition_digest=loaded_http.sha256,
                    ),
                ),
                tool_removals=(ProposedToolRemovalV1(tool_id="http:gitea", expected_sha256=loaded_http.sha256),),
            ),
        ),
        (
            "conflict_with_grant",
            AccessProposalV1(
                title="Remove+grant",
                reason="Retired.",
                grants=(
                    ProposedGrantV1(
                        agent_type="TeamArchitect",
                        target=TargetRefV1(kind=TargetKind.TOOL, id="http:gitea"),
                        level=AccessLevel.READ,
                    ),
                ),
                tool_removals=(ProposedToolRemovalV1(tool_id="http:gitea", expected_sha256=loaded_http.sha256),),
            ),
        ),
        (
            "conflict_with_block",
            AccessProposalV1(
                title="Remove+tool-block",
                reason="Retired.",
                blocks=(
                    ProposedBlockV1(
                        agent_type="Worker",
                        target=TargetRefV1(kind=TargetKind.TOOL, id="http:gitea"),
                        reason="Stop using it.",
                    ),
                ),
                tool_removals=(ProposedToolRemovalV1(tool_id="http:gitea", expected_sha256=loaded_http.sha256),),
            ),
        ),
        (
            "conflict_with_block",
            AccessProposalV1(
                title="Remove+op-block",
                reason="Retired.",
                blocks=(
                    ProposedBlockV1(
                        agent_type="Worker",
                        target=TargetRefV1(kind=TargetKind.OPERATION, id="http:gitea#route:delete"),
                        reason="Stop deleting.",
                    ),
                ),
                tool_removals=(ProposedToolRemovalV1(tool_id="http:gitea", expected_sha256=loaded_http.sha256),),
            ),
        ),
        (
            "conflict_with_link",
            AccessProposalV1(
                title="Remove+link",
                reason="Retired.",
                account_links=(
                    ProposedAccountLinkV1(tool_id="http:gitea", owner="team", purpose="Link Gitea"),
                ),
                tool_removals=(ProposedToolRemovalV1(tool_id="http:gitea", expected_sha256=loaded_http.sha256),),
            ),
        ),
    ]
    caplog.set_level(logging.WARNING)
    for reason, proposal in conflicts:
        caplog.clear()
        with pytest.raises(ProposalRefused):
            compiler.compile(proposal, person=person, task_id="task-1")
        assert "event=proposal_tool_removal_refused" in caplog.text
        assert f"reason={reason}" in caplog.text


def test_tool_removal_rows_compiler_summary_bounded_text_and_80_row_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "instance"
    store = _access_store(tmp_path)
    write_instance_tool(
        root,
        ProposedHttpToolV1(
            tool_id="http:gitea",
            display_name="Gitea",
            hosts=("gitea.example.com", "mirror.example.net"),
            routes=(HttpRouteV1(name="delete", method="DELETE", path="/repos/**", level=AccessLevel.FULL),),
            uses_credential=False,
        ),
    )
    # Large CLI with MAX_COMMANDS (300) commands to prove overview row stays compact and bounded.
    big_commands = {f"cmd{i}": ("read" if i < 200 else ("write" if i < 280 else "full")) for i in range(MAX_COMMANDS)}
    write_instance_tool(
        root,
        ProposedCliToolV1(
            tool_id="cli:bigcli",
            display_name="Big CLI",
            installed_cli=InstalledCliV1(
                binary_path="team/tools/bigcli/bigcli",
                sha256="b" * 64,
                commands=big_commands,
            ),
        ),
    )

    long_reason = "R" * 1900
    g_active = _grant("g_active", target_id="http:gitea").model_copy(
        update={"level": AccessLevel.WRITE, "for_person_id": "alice", "reason": long_reason}
    )
    g_spent = _grant("g_spent", target_id="http:gitea").model_copy(
        update={"lifetime": GrantLifetime.ONE_TIME, "uses_remaining": 0, "task_id": "task-1"}
    )
    g_expired = _grant("g_expired", target_id="http:gitea").model_copy(
        update={"lifetime": GrantLifetime.UNTIL, "expires_at_utc": NOW - timedelta(minutes=1)}
    )
    g_sibling = _grant("g_sibling", target_id="http:gitea.v2")
    b_tool = _grant("b_tool", kind=GrantKind.BLOCK, target_id="http:gitea").model_copy(update={"reason": long_reason})
    b_op = _grant(
        "b_op", kind=GrantKind.BLOCK, target_kind=TargetKind.OPERATION, target_id="http:gitea#route:delete"
    )
    l_team_acct = _link("l_team_acct", tool_id="http:gitea", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM, purpose=LinkPurpose.ACCOUNT)
    l_person_acct = _link(
        "l_person_acct", tool_id="http:gitea", owner_kind=LinkOwnerKind.PERSON, owner_id="alice", purpose=LinkPurpose.ACCOUNT
    )
    l_team_gate = _link("l_team_gate", tool_id="http:gitea", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM, purpose=LinkPurpose.GATE)
    l_revoked = l_team_acct.model_copy(update={"link_id": "l_revoked", "status": AccountLinkStatus.REVOKED})

    # Verify active_grants_for_tool excludes expired, spent, and sibling grants.
    live_grants, live_blocks = active_grants_for_tool(
        [g_active, g_spent, g_expired, g_sibling, b_tool, b_op], "http:gitea", now=NOW
    )
    assert [g.grant_id for g in live_grants] == ["g_active"]
    assert [b.grant_id for b in live_blocks] == ["b_tool", "b_op"]

    compiler = _compiler(
        root,
        store,
        grants=[g_active, g_spent, g_sibling, b_tool, b_op],
        links=[l_team_acct, l_person_acct, l_team_gate, l_revoked],
    )
    proposal = with_tool_removal_baselines(
        AccessProposalV1(
            title="Remove Gitea and Big CLI",
            reason="Retiring old tools.",
            tool_removals=(
                ProposedToolRemovalV1(tool_id="http:gitea"),
                ProposedToolRemovalV1(tool_id="cli:bigcli"),
            ),
        ),
        instance_root=root,
        catalog=compiler.catalog,
    )
    compiled = compiler.compile(proposal, person=PersonPrincipal("owner-1", TEAM, ("owner",)), task_id="task-1")
    assert all(row.section == "tool_removal" for row in compiled.rows)
    assert all(len(row.text) <= 1600 for row in compiled.rows)
    assert proposal_summary(compiled.rows) == "Access proposal: remove 2 tools. Takes effect when approved."

    by_id = {row.item_id: row.text for row in compiled.rows}
    # Overview row for HTTP tool names display name, tool_id, kind, hosts, and impact counts.
    assert "Remove web API tool Gitea (http:gitea)" in by_id["remove-0"]
    assert "gitea.example.com, mirror.example.net" in by_id["remove-0"]
    assert "Revokes 1 active grant, 2 active blocks, and 3 active credential links." in by_id["remove-0"]
    # Explicit rows for every active grant, tool/operation block, and account/gate link (without unbounded reason).
    assert "g_active" in by_id["remove-0-grant-0"] and "alice's work" in by_id["remove-0-grant-0"]
    assert long_reason[:50] not in by_id["remove-0-grant-0"]
    assert "g_spent" not in " ".join(by_id.values())
    assert "g_sibling" not in " ".join(by_id.values())
    assert "b_tool" in by_id["remove-0-block-0"] and "tool block" in by_id["remove-0-block-0"]
    assert "b_op" in by_id["remove-0-block-1"] and "operation route:delete" in by_id["remove-0-block-1"]
    link_texts = [by_id[f"remove-0-link-{i}"] for i in range(3)]
    assert any("team account link l_team_acct: on Gitea (http:gitea)." in text for text in link_texts)
    assert any("personal account link l_person_acct: for alice on Gitea (http:gitea)." in text for text in link_texts)
    assert any("team gate credential link l_team_gate: on Gitea (http:gitea)." in text for text in link_texts)
    assert "l_revoked" not in " ".join(link_texts)

    # CLI overview row is compact (even with 300 commands), notes workspace files stay untouched, and notes no impact.
    assert "Remove installed CLI tool Big CLI (cli:bigcli)" in by_id["remove-1"]
    assert "team/tools/bigcli/bigcli" in by_id["remove-1"]
    assert "300 classified commands (200 read, 80 write, 20 full)" in by_id["remove-1"]
    assert "Workspace files under team/tools/bigcli/ are not deleted." in by_id["remove-1"]
    assert "No active grants, blocks, or credential links are affected." in by_id["remove-1"]

    # 80-row review card cap is enforced without silent truncation.
    many_grants = [_grant(f"g_{i:03d}", target_id="http:gitea") for i in range(CHANGE_ITEMS_MAX_COUNT)]
    crowded_compiler = _compiler(root, store, grants=many_grants)
    one_removal = with_tool_removal_baselines(
        AccessProposalV1(
            title="Remove Gitea",
            reason="Retired.",
            tool_removals=(ProposedToolRemovalV1(tool_id="http:gitea"),),
        ),
        instance_root=root,
        catalog=crowded_compiler.catalog,
    )
    caplog.set_level(logging.WARNING)
    caplog.clear()
    with pytest.raises(ProposalRefused, match=f"exceeds the {CHANGE_ITEMS_MAX_COUNT}-row review card limit"):
        crowded_compiler.compile(one_removal, person=PersonPrincipal("owner-1", TEAM, ("owner",)), task_id="task-1")
    assert "event=proposal_review_rows_limit_exceeded" in caplog.text

    # ValueError from tool_removal_rows is logged and converted to ProposalRefused (never silently truncated).
    import access_control.proposals as proposals_mod

    def _too_long(*_args, **_kwargs):
        raise ValueError("Review row 'remove-0' is 1700 characters, exceeding the 1600-character limit.")

    monkeypatch.setattr(proposals_mod, "tool_removal_rows", _too_long)
    caplog.clear()
    with pytest.raises(ProposalRefused, match="without hiding part of its impact"):
        compiler.compile(one_removal, person=PersonPrincipal("owner-1", TEAM, ("owner",)), task_id="task-1")
    assert "event=proposal_tool_removal_refused" in caplog.text
    assert "reason=review_row_too_long" in caplog.text
    assert callable(tool_removal_rows)


def test_http_tool_removal_with_eight_long_hosts_packs_continuation_rows_without_overflow(tmp_path: Path) -> None:
    """C11: valid multi-host HTTP tools with 7-8 max-length hosts stay removable with packed continuation rows."""

    root = tmp_path / "instance"
    store = _access_store(tmp_path)
    long_hosts = tuple(
        f"h{i}." + ".".join([f"seg{j:02d}" + ("a" * 45) for j in range(4)]) + ".example.com"
        for i in range(8)
    )
    assert all(len(host) <= 253 for host in long_hosts)
    assert len(", ".join(long_hosts)) > 1600
    write_instance_tool(
        root,
        ProposedHttpToolV1(
            tool_id="http:manyhosts",
            display_name="Many Hosts API",
            hosts=long_hosts,
            uses_credential=False,
        ),
    )
    compiler = _compiler(root, store)
    proposal = with_tool_removal_baselines(
        AccessProposalV1(
            title="Remove Many Hosts API",
            reason="Retired.",
            tool_removals=(ProposedToolRemovalV1(tool_id="http:manyhosts"),),
        ),
        instance_root=root,
        catalog=compiler.catalog,
    )
    compiled = compiler.compile(proposal, person=PersonPrincipal("owner-1", TEAM, ("owner",)), task_id="task-1")
    assert all(row.section == "tool_removal" for row in compiled.rows)
    assert all(len(row.text) <= 1600 for row in compiled.rows)
    assert compiled.rows[0].item_id == "remove-0"
    assert "8 hosts (listed below)" in compiled.rows[0].text
    host_rows = [row for row in compiled.rows if row.item_id.startswith("remove-0-hosts-")]
    assert len(host_rows) >= 2
    combined_hosts = " ".join(row.text for row in host_rows)
    for host in long_hosts:
        assert host in combined_hosts
    # Continuation rows have item_id.count('-') > 1 so proposal_summary counts 1 tool.
    assert proposal_summary(compiled.rows) == "Access proposal: remove 1 tool. Takes effect when approved."


def test_compiler_keeps_all_new_tool_rows_contiguous_before_changes_and_removals(tmp_path: Path) -> None:
    """C12: combining tools, clis, tool_changes, and tool_removals preserves REVIEW_SECTIONS order."""

    root = tmp_path / "instance"
    store = _access_store(tmp_path)
    write_instance_tool(root, _http_tool("http:old", "old.example.com"))
    write_instance_tool(root, _http_tool("http:editable", "editable.example.com"))
    compiler = _compiler(root, store)

    from access_control.proposals import with_tool_change_baselines

    proposal = AccessProposalV1(
        title="Add, change, and remove tools together",
        reason="Consolidating catalog.",
        tools=(_http_tool("http:added", "added.example.com"),),
        clis=(_cli_tool("fresh"),),
        tool_changes=(
            ProposedHttpToolChangeV1(
                tool_id="http:editable",
                display_name="Editable Updated",
                hosts=("editable.example.com", "editable2.example.com"),
                uses_credential=False,
            ),
        ),
        tool_removals=(ProposedToolRemovalV1(tool_id="http:old"),),
    )
    proposal = with_tool_removal_baselines(proposal, instance_root=root, catalog=compiler.catalog)
    proposal = with_tool_change_baselines(proposal, instance_root=root, catalog=compiler.catalog)
    compiled = compiler.compile(proposal, person=PersonPrincipal("owner-1", TEAM, ("owner",)), task_id="task-1")
    sections = [row.section for row in compiled.rows]
    # Compress consecutive identical sections: must be strictly ["tool", "tool_change", "tool_removal"].
    deduped = [sec for i, sec in enumerate(sections) if i == 0 or sections[i - 1] != sec]
    assert deduped == ["tool", "tool_change", "tool_removal"]


# ---------------------------------------------------------------------------
# Checkpoint 3: ProposalApplier (auth-on), recovery (A4/C01/C02), auth-off (A5/C03)
# ---------------------------------------------------------------------------


def _build_real_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a real Cedar + SQLite + ProposalApplier + BrokerAccessGate world."""

    from access_catalog.catalog import CatalogSource
    from access_control.cedar import CedarAccessClient
    from access_control.data_folders import DataFolderRegistry
    from access_control.decisions import AccessService
    from access_control.entities import agent_role_resolver
    from access_control.identity import ContextVerifier
    from access_control.proposals import ACCESS_PROPOSAL_KIND, ProposalApplier
    from access_control.requests import AccessRequests
    from custom_agents import access_runtime
    from secure_tool_broker.access_gate import BrokerAccessGate
    from tests.broker_access_support import context_verify_keys
    from tests.task_work_support import set_agent_ref
    from tests.test_access_request_tools import BINARY, OWNER as REQ_OWNER, TEAM as REQ_TEAM, make_agent

    if not BINARY.is_file():
        pytest.skip("build ua-cedar-authorizer first (cargo build)")

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    instance = tmp_path / "instance"
    (instance / "control").mkdir(parents=True)
    lead = make_agent(None, tmp_path, agent_type="TeamArchitect", lead=True)
    lead.instance_root = str(instance)
    storage = lead.task_manager.storage_settings
    catalogs = CatalogSource(instance)
    service = AccessService(
        store=AccessRepository(storage),
        catalog=catalogs.current(),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=REQ_TEAM,
        agent_role_of=agent_role_resolver("TeamArchitect"),
    )
    second_admin = PersonPrincipal("admin-2", REQ_TEAM, ("admin",))
    member = PersonPrincipal("member-1", REQ_TEAM, ("member",))
    people = {REQ_OWNER.person_id: REQ_OWNER, second_admin.person_id: second_admin, member.person_id: member}
    holder: dict = {}

    def requests() -> AccessRequests:
        service.catalog = catalogs.current()
        return AccessRequests(
            service=service,
            inbox=access_runtime.StoredAccessInbox(service.store),
            lead_agent_type="TeamArchitect",
            appliers={ACCESS_PROPOSAL_KIND: holder["applier"]},
        )

    holder["applier"] = ProposalApplier(
        requests=requests,
        instance_root=instance,
        catalogs=catalogs,
        data_folders=lambda: DataFolderRegistry(service.store, REQ_TEAM),
        protected=(instance,),
        person=people.get,
    )
    gate = BrokerAccessGate(
        catalogs=catalogs,
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=REQ_TEAM,
        access=service,
        links=service.store,
    )
    set_agent_ref(lead)
    return {
        "lead": lead,
        "requests": requests,
        "applier": holder["applier"],
        "gate": gate,
        "instance": instance,
        "service": service,
        "owner": REQ_OWNER,
        "second_admin": second_admin,
        "member": member,
        "team": REQ_TEAM,
        "cleanup": lambda: (service.cedar.close(), access_runtime._runtime_access.cache_clear(), set_agent_ref(None)),
    }


def _seed_http_tool_with_impacts(world: dict, tool_id: str = "http:portainer", host: str = "portainer.example.test") -> Path:
    service = world["service"]
    team = world["team"]
    owner = world["owner"]
    now = service.clock()
    path = write_instance_tool(
        world["instance"],
        ProposedHttpToolV1(
            tool_id=tool_id,
            display_name="Portainer",
            hosts=(host,),
            routes=(
                HttpRouteV1(name="stacks.restart", method="POST", path="/api/stacks/{id}/restart", level=AccessLevel.WRITE),
                HttpRouteV1(name="stacks.delete", method="DELETE", path="/api/stacks/{id}", level=AccessLevel.FULL),
            ),
            credential_injections=(
                CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-API-Key", field="token"),
            ),
            gate_injections=(
                CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate", field="gate"),
            ),
        ),
    )
    # Active lead grant, handed-down worker grant, tool block, operation block, links, and setup state.
    lead_grant = GrantV1(
        grant_id="grant_lead_portainer",
        team_id=team,
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{team}/TeamArchitect"),
        target=TargetRefV1(kind=TargetKind.TOOL, id=tool_id),
        level=AccessLevel.WRITE,
        delegation_depth=1,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=owner.person_id),
        for_person_id="member-1",
        created_at_utc=now,
    )
    worker_grant = GrantV1(
        grant_id="grant_worker_portainer",
        team_id=team,
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{team}/BackgroundOperationsAgent"),
        target=TargetRefV1(kind=TargetKind.TOOL, id=tool_id),
        level=AccessLevel.WRITE,
        lifetime=GrantLifetime.TASK,
        task_id="task-live",
        parent_grant_id=lead_grant.grant_id,
        granted_by=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{team}/TeamArchitect"),
        for_person_id="member-1",
        created_at_utc=now,
    )
    tool_block = GrantV1(
        grant_id="blk_tool_portainer",
        team_id=team,
        kind=GrantKind.BLOCK,
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{team}/BackgroundOperationsAgent"),
        target=TargetRefV1(kind=TargetKind.TOOL, id=tool_id),
        level=AccessLevel.FULL,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=owner.person_id),
        created_at_utc=now,
    )
    op_block = GrantV1(
        grant_id="blk_op_portainer",
        team_id=team,
        kind=GrantKind.BLOCK,
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{team}/BackgroundOperationsAgent"),
        target=TargetRefV1(kind=TargetKind.OPERATION, id=f"{tool_id}#route:stacks.delete"),
        level=AccessLevel.FULL,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=owner.person_id),
        created_at_utc=now,
    )
    for g in (lead_grant, worker_grant, tool_block, op_block):
        service.store.create(g)
    service.store.create_link(
        AccountLinkV1(
            link_id="link_team_acct",
            team_id=team,
            tool_id=tool_id,
            purpose=LinkPurpose.ACCOUNT,
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=team,
            credential_key="cred-team",
            created_by=SubjectRefV1(kind=SubjectKind.USER, id=owner.person_id),
            created_at_utc=now,
        )
    )
    service.store.create_link(
        AccountLinkV1(
            link_id="link_person_acct",
            team_id=team,
            tool_id=tool_id,
            purpose=LinkPurpose.ACCOUNT,
            owner_kind=LinkOwnerKind.PERSON,
            owner_id="member-1",
            credential_key="cred-person",
            created_by=SubjectRefV1(kind=SubjectKind.USER, id="member-1"),
            created_at_utc=now,
        )
    )
    service.store.create_link(
        AccountLinkV1(
            link_id="link_team_gate",
            team_id=team,
            tool_id=tool_id,
            purpose=LinkPurpose.GATE,
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=team,
            credential_key="cred-gate",
            injections=(CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate", field="gate"),),
            created_by=SubjectRefV1(kind=SubjectKind.USER, id=owner.person_id),
            created_at_utc=now,
        )
    )
    service.store.put_tool_setup_state(
        ToolSetupStateV1(
            team_id=team,
            person_id="member-1",
            tool_id=tool_id,
            state=ToolSetupState.READY,
            how="team_link",
            source="gateway",
            checked_at_utc=now,
            changed_at_utc=now,
        )
    )
    return path


def test_end_to_end_http_and_cli_tool_removal_through_propose_access_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A3 + C02: second admin approves HTTP + CLI removal, revoking grants/blocks/links/setup while keeping CLI binary."""

    import asyncio
    import json

    from strands.interrupt import InterruptException

    from access_control.caller_tokens import CALLER_MANAGER, BrokerCaller
    from access_control.decisions import HandDownRefused
    from custom_messages.contracts.access_v2 import AccessReviewChoice
    from custom_tools.providers.access_proposals import propose_access_change
    from secure_tool_broker.access_gate import REASON_UNLISTED_HOST
    from tests.broker_access_support import agent_context
    from tests.test_access_request_tools import Context

    world = _build_real_world(tmp_path, monkeypatch)
    try:
        instance = world["instance"]
        service = world["service"]
        requests = world["requests"]
        gate = world["gate"]
        team = world["team"]
        owner = world["owner"]
        second_admin = world["second_admin"]
        member = world["member"]

        http_file = _seed_http_tool_with_impacts(world)

        # Also seed a proposal-written CLI tool and its workspace binary under workspaces/team/tools/demo/demo.
        cli_binary = instance / "workspaces" / "team" / "tools" / "demo" / "demo"
        cli_binary.parent.mkdir(parents=True, exist_ok=True)
        cli_binary.write_bytes(b"#!/bin/sh\necho demo\n")
        cli_file = write_instance_tool(instance, _cli_tool("demo"))
        cli_grant = GrantV1(
            grant_id="grant_cli_demo",
            team_id=team,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{team}/TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="cli:demo"),
            level=AccessLevel.READ,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=owner.person_id),
            for_person_id="member-1",
            created_at_utc=service.clock(),
        )
        service.store.create(cli_grant)

        # Refresh lead's runtime catalog cache so _submit sees the seeded tools.
        from custom_agents import access_runtime

        access_runtime._runtime_access.cache_clear()

        ctx = Context("tooluse-remove-http-cli")
        with pytest.raises(InterruptException):
            asyncio.run(
                propose_access_change(
                    title="Remove retired Portainer and Demo CLI",
                    reason="Both tools have been decommissioned.",
                    tool_context=ctx,
                    tool_removals=[{"tool_id": "http:portainer"}, {"tool_id": "cli:demo"}],
                    # Also test C02 overlap: revoke_grant_ids naming a grant on a removed tool.
                    revoke_grant_ids=["grant_lead_portainer"],
                )
            )
        request_id = ctx.raised[0]["access_request_id"]
        waiting = service.store.get_request(request_id)
        assert waiting is not None and waiting.change is not None
        assert any(item.section == "tool_removal" and "http:portainer" in item.text for item in waiting.change.items)
        assert any(item.section == "tool_removal" and "cli:demo" in item.text for item in waiting.change.items)

        # Non-administrator cannot approve a tool removal.
        with pytest.raises(HandDownRefused, match="administrator"):
            requests().decide(request_id, member, AccessReviewChoice.APPROVE)
        assert http_file.exists() and cli_file.exists()

        # Second administrator (neither granted_by nor for_person_id) approves cleanly (C02).
        decided = requests().decide(request_id, second_admin, AccessReviewChoice.APPROVE)
        assert decided.status is AccessRequestStatus.GRANTED
        assert not http_file.exists()
        assert not cli_file.exists()
        # CLI workspace binary must remain untouched!
        assert cli_binary.exists() and cli_binary.read_bytes() == b"#!/bin/sh\necho demo\n"

        # Every grant, tool block, operation block, account/gate link, and setup state is gone/revoked.
        for gid in ("grant_lead_portainer", "grant_worker_portainer", "blk_tool_portainer", "blk_op_portainer", "grant_cli_demo"):
            stored_grant = service.store.get(gid)
            assert stored_grant is not None and stored_grant.status is GrantStatus.REVOKED
        assert [link for link in service.store.list_links(team, purpose=None) if link.tool_id == "http:portainer"] == []
        assert service.store.tool_setup_state(team, "member-1", "http:portainer") is None

        # Broker access gate now refuses the removed host as unlisted_host AND refuses the removed cli:demo (F8).
        decision = gate.check_broker_action(
            BrokerCaller(CALLER_MANAGER),
            "secure_requests",
            "POST",
            {"url": "https://portainer.example.test/api/stacks/1/restart"},
            agent_context(
                owner.person_id,
                agent_id="agent-boa",
                named_agent_type="BackgroundOperationsAgent",
                roles=("owner",),
                task_id="task-after-remove",
            ),
        )
        assert not decision.allowed and decision.reason_code == REASON_UNLISTED_HOST

        cli_decision = gate.check_broker_action(
            BrokerCaller(CALLER_MANAGER),
            "demo",
            "run",
            {"argv": ["version"]},
            agent_context(
                owner.person_id,
                agent_id="agent-lead",
                named_agent_type="TeamArchitect",
                roles=("owner",),
                task_id="task-after-remove-cli",
            ),
        )
        assert not cli_decision.allowed
        assert list(http_file.parent.glob(".removed-*.tmp")) == []

        # Resuming the paused propose_access_change call returns applied.
        resumed = json.loads(
            asyncio.run(
                propose_access_change(
                    title="Remove retired Portainer and Demo CLI",
                    reason="Both tools have been decommissioned.",
                    tool_context=ctx,
                    tool_removals=[{"tool_id": "http:portainer"}, {"tool_id": "cli:demo"}],
                    revoke_grant_ids=["grant_lead_portainer"],
                )
            )
        )
        assert resumed["status"] == "applied"
    finally:
        world["cleanup"]()


def test_tool_removal_refuses_file_changed_or_symlinked_after_review_and_rolls_back_on_authority_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A3: file modified or symlinked after review is refused; authority failure inside apply_together restores the file."""

    import asyncio

    from strands.interrupt import InterruptException

    from access_control.decisions import AccessService, HandDownRefused
    from custom_agents import access_runtime
    from custom_messages.contracts.access_v2 import AccessReviewChoice
    from custom_tools.providers.access_proposals import propose_access_change
    from tests.test_access_request_tools import Context

    world = _build_real_world(tmp_path, monkeypatch)
    try:
        service = world["service"]
        requests = world["requests"]
        owner = world["owner"]
        second_admin = world["second_admin"]

        http_file = _seed_http_tool_with_impacts(world)
        original_bytes = http_file.read_bytes()
        access_runtime._runtime_access.cache_clear()

        # 1. Propose removal, then modify file on disk before approval -> refused, file untouched.
        c1 = Context("tooluse-remove-modified")
        with pytest.raises(InterruptException):
            asyncio.run(
                propose_access_change(
                    title="Remove Portainer",
                    reason="Retired.",
                    tool_context=c1,
                    tool_removals=[{"tool_id": "http:portainer"}],
                )
            )
        req1 = c1.raised[0]["access_request_id"]
        modified_bytes = original_bytes + b"\n# edited after review\n"
        http_file.write_bytes(modified_bytes)
        with pytest.raises(HandDownRefused, match="changed after the proposal was prepared"):
            requests().decide(req1, owner, AccessReviewChoice.APPROVE)
        assert http_file.read_bytes() == modified_bytes
        assert service.store.get("grant_lead_portainer").status is GrantStatus.ACTIVE

        # 2. Replace file with a symlink after review -> refused without following or unlinking symlink.
        target_outside = tmp_path / "outside.yaml"
        target_outside.write_bytes(original_bytes)
        http_file.unlink()
        http_file.symlink_to(target_outside)
        with pytest.raises(HandDownRefused, match="cannot be removed|no longer applies"):
            requests().decide(req1, owner, AccessReviewChoice.APPROVE)
        assert http_file.is_symlink()
        assert target_outside.read_bytes() == original_bytes

        # Restore original file and prepare a fresh proposal to test mid-apply authority failure rollback.
        http_file.unlink()
        http_file.write_bytes(original_bytes)
        access_runtime._runtime_access.cache_clear()

        c2 = Context("tooluse-remove-auth-fail")
        with pytest.raises(InterruptException):
            asyncio.run(
                propose_access_change(
                    title="Remove Portainer",
                    reason="Retired.",
                    tool_context=c2,
                    tool_removals=[{"tool_id": "http:portainer"}],
                )
            )
        req2 = c2.raised[0]["access_request_id"]

        # F6: fail may_revoke ONLY AFTER remove_proposed_tool_file has actually staged the file,
        # proving synchronous rollback in ProposalApplier._apply_items restores the staged file!
        import access_control.proposals as proposals_mod

        real_remove_proposed = proposals_mod.remove_proposed_tool_file
        staged_state = {"staged": False, "file_gone_when_failed": False}

        def spy_remove_proposed(*args, **kwargs):
            result = real_remove_proposed(*args, **kwargs)
            assert not http_file.exists()
            assert len(list(http_file.parent.glob(".removed-*.tmp"))) == 1
            staged_state["staged"] = True
            return result

        original_may_revoke = AccessService.may_revoke

        def fail_inside_tx(self_svc, person, grant, **kwargs):
            if staged_state["staged"] and grant.grant_id == "grant_lead_portainer":
                staged_state["file_gone_when_failed"] = not http_file.exists()
                return False
            return original_may_revoke(self_svc, person, grant, **kwargs)

        with monkeypatch.context() as tx_ctx:
            tx_ctx.setattr(proposals_mod, "remove_proposed_tool_file", spy_remove_proposed)
            tx_ctx.setattr(AccessService, "may_revoke", fail_inside_tx)
            with pytest.raises(
                HandDownRefused,
                match="The proposal could not be applied, so nothing changed: You cannot remove grant grant_lead_portainer",
            ):
                requests().decide(req2, second_admin, AccessReviewChoice.APPROVE)
        assert staged_state["staged"] is True
        assert staged_state["file_gone_when_failed"] is True
        # File was staged before apply_together and must now be restored with exact bytes, no .tmp left, and DB intact!
        assert http_file.exists() and http_file.read_bytes() == original_bytes
        assert list(http_file.parent.glob(".removed-*.tmp")) == []
        assert service.store.get("grant_lead_portainer").status is GrantStatus.ACTIVE
        assert service.store.get_request(req2).status is AccessRequestStatus.WAITING

        # F6: separate unmocked real-Cedar fast pre-check refusal test:
        # blocking second_admin on http:portainer makes real Cedar deny may_revoke(second_admin, grant_lead_portainer)
        # BEFORE remove_proposed_tool_file ever touches disk!
        admin_block = GrantV1(
            grant_id="blk_second_admin_portainer",
            team_id=world["team"],
            kind=GrantKind.BLOCK,
            subject=SubjectRefV1(kind=SubjectKind.USER, id=second_admin.person_id),
            target=TargetRefV1(kind=TargetKind.TOOL, id="http:portainer"),
            level=AccessLevel.FULL,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=owner.person_id),
            created_at_utc=service.clock(),
        )
        service.store.create(admin_block)
        c3 = Context("tooluse-remove-real-cedar-precheck")
        with pytest.raises(InterruptException):
            asyncio.run(
                propose_access_change(
                    title="Remove Portainer Real Cedar Precheck",
                    reason="Retired.",
                    tool_context=c3,
                    tool_removals=[{"tool_id": "http:portainer"}],
                )
            )
        req3 = c3.raised[0]["access_request_id"]
        remove_called = {"called": False}

        def _never_called(*args, **kwargs):
            remove_called["called"] = True
            return real_remove_proposed(*args, **kwargs)

        with monkeypatch.context() as pre_ctx:
            pre_ctx.setattr(proposals_mod, "remove_proposed_tool_file", _never_called)
            with pytest.raises(HandDownRefused, match=r"You cannot remove grant grant_(?:worker|lead)_portainer"):
                requests().decide(req3, second_admin, AccessReviewChoice.APPROVE)
        assert remove_called["called"] is False
        assert http_file.exists() and http_file.read_bytes() == original_bytes
        assert list(http_file.parent.glob(".removed-*.tmp")) == []
        assert service.store.get_request(req3).status is AccessRequestStatus.WAITING
    finally:
        world["cleanup"]()


@pytest.mark.parametrize("committed", [False, True])
def test_tool_removal_crash_recovery_after_reopening_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    """A4: unconfirmed tool removal is reconciled after store reopen (restored if uncommitted, kept gone if committed)."""

    import asyncio

    from strands.interrupt import InterruptException

    from access_control.requests import DecisionOutcomeUnknown
    from custom_agents import access_runtime
    from custom_messages.contracts.access_v2 import AccessReviewChoice
    from custom_tools.providers.access_proposals import propose_access_change
    from tests.access_decision_support import uncertain_store
    from tests.test_access_request_tools import Context

    world = _build_real_world(tmp_path, monkeypatch)
    try:
        service = world["service"]
        requests = world["requests"]
        owner = world["owner"]
        team = world["team"]

        http_file = _seed_http_tool_with_impacts(world)
        original_bytes = http_file.read_bytes()
        access_runtime._runtime_access.cache_clear()

        ctx = Context(f"tooluse-remove-crash-{committed}")
        with pytest.raises(InterruptException):
            asyncio.run(
                propose_access_change(
                    title="Remove Portainer",
                    reason="Retired.",
                    tool_context=ctx,
                    tool_removals=[{"tool_id": "http:portainer"}],
                )
            )
        req_id = ctx.raised[0]["access_request_id"]

        with uncertain_store(monkeypatch, service.store, "apply_together", committed=committed):
            with pytest.raises(DecisionOutcomeUnknown, match="could not yet confirm"):
                requests().decide(req_id, owner, AccessReviewChoice.APPROVE)

        # File was unlinked before apply_together raised uncertain error.
        assert not http_file.exists()
        service.store = AccessRepository(service.store.settings)
        later = service.clock() + timedelta(minutes=11)
        assert len(service.store.decision_recoveries(team, now=later)) == 1
        monkeypatch.setattr(service, "clock", lambda: later)
        requests().recover_decisions()
        assert service.store.decision_recoveries(team, now=later) == []

        if committed:
            assert not http_file.exists()
            assert list(http_file.parent.glob(".removed-*.tmp")) == []
            assert service.store.get_request(req_id).status is AccessRequestStatus.GRANTED
            assert service.store.get("grant_lead_portainer").status is GrantStatus.REVOKED
        else:
            assert http_file.exists() and http_file.read_bytes() == original_bytes
            assert list(http_file.parent.glob(".removed-*.tmp")) == []
            assert service.store.get_request(req_id).status is AccessRequestStatus.WAITING
            assert service.store.get("grant_lead_portainer").status is GrantStatus.ACTIVE
            # Re-approving after recovery succeeds cleanly.
            assert requests().decide(req_id, owner, AccessReviewChoice.APPROVE).status is AccessRequestStatus.GRANTED
            assert not http_file.exists()
            assert list(http_file.parent.glob(".removed-*.tmp")) == []
    finally:
        world["cleanup"]()


def test_tool_removal_recovery_refuses_to_overwrite_external_file_and_respects_granted_or_claimed_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A4 + C01: recover keeps externally recreated file, skips restore on non-matching GRANTED or WAITING+claimed_elsewhere, and retries PermissionError."""

    import asyncio

    from strands.interrupt import InterruptException

    from access_control.requests import DecisionOutcomeUnknown
    from custom_agents import access_runtime
    from custom_messages.contracts.access_v2 import AccessReviewChoice
    from custom_tools.providers.access_proposals import propose_access_change
    from tests.access_decision_support import uncertain_store
    from tests.test_access_request_tools import Context

    world = _build_real_world(tmp_path, monkeypatch)
    try:
        service = world["service"]
        requests = world["requests"]
        applier = world["applier"]
        owner = world["owner"]
        second_admin = world["second_admin"]
        team = world["team"]

        http_file = _seed_http_tool_with_impacts(world)
        original_bytes = http_file.read_bytes()
        access_runtime._runtime_access.cache_clear()

        # Part 1: externally recreated file after crash is NOT overwritten; logs access_decision_artifact_changed.
        c1 = Context("tooluse-remove-external-recreate")
        with pytest.raises(InterruptException):
            asyncio.run(
                propose_access_change(
                    title="Remove Portainer",
                    reason="Retired.",
                    tool_context=c1,
                    tool_removals=[{"tool_id": "http:portainer"}],
                )
            )
        req1 = c1.raised[0]["access_request_id"]
        with uncertain_store(monkeypatch, service.store, "apply_together", committed=False):
            with pytest.raises(DecisionOutcomeUnknown):
                requests().decide(req1, owner, AccessReviewChoice.APPROVE)
        assert not http_file.exists()

        external_bytes = b"tools: []\n# manually recreated\n"
        http_file.write_bytes(external_bytes)
        later = service.clock() + timedelta(minutes=11)
        monkeypatch.setattr(service, "clock", lambda: later)
        caplog.set_level(logging.WARNING)
        caplog.clear()
        requests().recover_decisions()
        assert http_file.read_bytes() == external_bytes
        assert "event=access_decision_artifact_changed" in caplog.text
        assert service.store.decision_recoveries(team, now=later) == []

        # Part 2 (C01 + F1): ProposalApplier.recover must NOT restore removed_tool_files when
        # (a) current is GRANTED by another decision (recovery.matches(current) is False) — and unlinks its staging file, or
        # (b) current is WAITING and claimed_elsewhere is True.
        http_file.unlink()
        stale_staging = http_file.parent / ".removed-stale-test.tmp"
        stale_staging.write_bytes(original_bytes)
        stale_stat = stale_staging.stat()
        removed_entry = RemovedFile.prepared(
            (http_file.name,),
            stale_stat.st_dev,
            stale_stat.st_ino,
            original_bytes,
            staging_parts=(stale_staging.name,),
        )
        base_req = service.store.get_request(req1)
        assert base_req is not None
        recovery = DecisionRecovery(
            DecisionClaim(req1, "claim-stale"),
            base_req.model_copy(
                update={
                    "status": AccessRequestStatus.GRANTED,
                    "decided_by": SubjectRefV1(kind=SubjectKind.USER, id=owner.person_id),
                    "decided_at_utc": later,
                }
            ),
            {
                "tool_files": [],
                "replaced_tool_files": [],
                "removed_tool_files": [removed_entry.model_dump(mode="json")],
                "programs": [],
                "servers": [],
            },
            later,
        )
        other_granted = base_req.model_copy(
            update={
                "status": AccessRequestStatus.GRANTED,
                "decided_by": SubjectRefV1(kind=SubjectKind.USER, id=second_admin.person_id),
                "decided_at_utc": later + timedelta(seconds=5),
            }
        )
        assert not recovery.matches(other_granted)
        caplog.clear()
        applier.recover(requests(), recovery, other_granted, False)
        assert not http_file.exists()
        assert not stale_staging.exists()
        assert "event=proposal_removals_kept" in caplog.text

        waiting_other_claim = base_req.model_copy(update={"status": AccessRequestStatus.WAITING})
        caplog.clear()
        applier.recover(requests(), recovery, waiting_other_claim, True)
        assert not http_file.exists()
        assert "event=proposal_removals_kept" in caplog.text

        # Part 3: failed restore (PermissionError) keeps recovery record and succeeds on retry.
        http_file.write_bytes(original_bytes)
        access_runtime._runtime_access.cache_clear()
        c2 = Context("tooluse-remove-retry-restore")
        with pytest.raises(InterruptException):
            asyncio.run(
                propose_access_change(
                    title="Remove Portainer Retry",
                    reason="Retired.",
                    tool_context=c2,
                    tool_removals=[{"tool_id": "http:portainer"}],
                )
            )
        req2 = c2.raised[0]["access_request_id"]
        with uncertain_store(monkeypatch, service.store, "apply_together", committed=False):
            with pytest.raises(DecisionOutcomeUnknown):
                requests().decide(req2, owner, AccessReviewChoice.APPROVE)
        assert not http_file.exists()

        retry_time = later + timedelta(minutes=11)
        monkeypatch.setattr(service, "clock", lambda: retry_time)
        with monkeypatch.context() as fail_ctx:
            fail_ctx.setattr(
                "access_control.proposals.restore_removed_instance_tool",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("read-only")),
            )
            requests().recover_decisions()
        assert not http_file.exists()
        assert len(service.store.decision_recoveries(team, now=retry_time)) == 1
        requests().recover_decisions()
        assert http_file.exists() and http_file.read_bytes() == original_bytes
        assert list(http_file.parent.glob(".removed-*.tmp")) == []
        assert service.store.decision_recoveries(team, now=retry_time) == []
    finally:
        world["cleanup"]()


def test_tool_removal_with_authorization_off_previews_applies_revokes_links_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A5 + C03: auth-off preview returns trusted impact rows, confirmed=true removes + revokes links, rollback restores file & links, and missing store works."""

    import asyncio
    import json

    from custom_agents import access_runtime
    from custom_tools.providers.access_proposals import propose_access_change
    from tests.broker_access_support import context_verify_keys
    from tests.task_work_support import set_agent_ref
    from tests.test_access_request_tools import TEAM as REQ_TEAM, Context, make_agent

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    instance = tmp_path / "instance"
    (instance / "control").mkdir(parents=True)
    lead = make_agent(None, tmp_path, agent_type="TeamArchitect", lead=True)
    lead.instance_root = str(instance)
    lead.authorization_config = {"enabled": False}
    set_agent_ref(lead)

    try:
        # 1. Seed HTTP tool + active team account link, gate link, and setup state.
        tool_file = write_instance_tool(instance, _http_tool("http:gitea", "gitea.example.com"))
        original_bytes = tool_file.read_bytes()
        store = AccessRepository(lead.task_manager.storage_settings)
        now = datetime.now(timezone.utc)
        store.create_link(
            AccountLinkV1(
                link_id="link_off_team",
                team_id=REQ_TEAM,
                tool_id="http:gitea",
                purpose=LinkPurpose.ACCOUNT,
                owner_kind=LinkOwnerKind.TEAM,
                owner_id=REQ_TEAM,
                credential_key="cred-team",
                created_by=SubjectRefV1(kind=SubjectKind.USER, id="owner-1"),
                created_at_utc=now,
            )
        )
        store.create_link(
            AccountLinkV1(
                link_id="link_off_gate",
                team_id=REQ_TEAM,
                tool_id="http:gitea",
                purpose=LinkPurpose.GATE,
                owner_kind=LinkOwnerKind.TEAM,
                owner_id=REQ_TEAM,
                credential_key="cred-gate",
                injections=(CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate", field="gate"),),
                created_by=SubjectRefV1(kind=SubjectKind.USER, id="owner-1"),
                created_at_utc=now,
            )
        )
        store.put_tool_setup_state(
            ToolSetupStateV1(
                team_id=REQ_TEAM,
                person_id="owner-1",
                tool_id="http:gitea",
                state=ToolSetupState.READY,
                how="team_link",
                source="gateway",
                checked_at_utc=now,
                changed_at_utc=now,
            )
        )

        # 2. confirmed=False returns trusted overview + active link impact rows without deleting or revoking.
        preview = json.loads(
            asyncio.run(
                propose_access_change(
                    title="Remove Gitea",
                    reason="Retired.",
                    tool_context=Context("tooluse-off-preview"),
                    tool_removals=[{"tool_id": "http:gitea"}],
                    confirmed=False,
                )
            )
        )
        assert preview["status"] == "agreement_needed"
        assert any("Remove web API tool Gitea (http:gitea)" in line for line in preview["changes"])
        assert any("link_off_team" in line for line in preview["changes"])
        assert any("link_off_gate" in line for line in preview["changes"])
        assert tool_file.exists() and tool_file.read_bytes() == original_bytes
        assert len(store.list_links(REQ_TEAM, purpose=None)) == 2

        # 3. Built-in tool is still refused with authorization off.
        builtin_refused = json.loads(
            asyncio.run(
                propose_access_change(
                    title="Remove GitHub",
                    reason="No.",
                    tool_context=Context("tooluse-off-builtin"),
                    tool_removals=[{"tool_id": "http:github"}],
                    confirmed=True,
                )
            )
        )
        assert builtin_refused["status"] == "refused" and "built-in tool" in builtin_refused["reason"]

        # 4. C03: combining tool_removals with a protected data_folders entry refuses cleanly:
        # BOTH the tool file AND its active links survive!
        bad_folder = json.loads(
            asyncio.run(
                propose_access_change(
                    title="Remove Gitea with bad folder",
                    reason="Should fail.",
                    tool_context=Context("tooluse-off-bad-folder"),
                    tool_removals=[{"tool_id": "http:gitea"}],
                    data_folders=[{"name": "bad", "path": str(instance), "writable": False}],
                    confirmed=True,
                )
            )
        )
        assert bad_folder["status"] == "refused"
        assert tool_file.exists() and tool_file.read_bytes() == original_bytes
        assert len(store.list_links(REQ_TEAM, purpose=None)) == 2

        # 5. Rollback after file removal: if DB apply_together raises, the removed tool file, data folder,
        # and activated MCP server are all rolled back cleanly (F5).
        external_dir = tmp_path / "shared_data"
        external_dir.mkdir()
        with monkeypatch.context() as fail_ctx:
            fail_ctx.setattr(
                AccessRepository,
                "apply_together",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db down")),
            )
            failed = json.loads(
                asyncio.run(
                    propose_access_change(
                        title="Remove Gitea DB fail",
                        reason="Retired.",
                        tool_context=Context("tooluse-off-db-fail"),
                        tool_removals=[{"tool_id": "http:gitea"}],
                        data_folders=[{"name": "shared_data", "path": str(external_dir), "writable": False}],
                        confirmed=True,
                    )
                )
            )
        assert failed["status"] == "refused" and "db down" in failed["reason"]
        assert tool_file.exists() and tool_file.read_bytes() == original_bytes
        assert list(tool_file.parent.glob(".removed-*.tmp")) == []
        assert store.data_folders(REQ_TEAM) == []
        assert len(store.list_links(REQ_TEAM, purpose=None)) == 2

        # 5b. F7 + F4: seed an old ACTIVE grant and an access_account_sharing row before auth-off apply.
        from custom_messages.contracts.access_v2 import AccountSharing, AccountSharingV1

        store.create(
            GrantV1(
                grant_id="g_old_auth_off",
                team_id=REQ_TEAM,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{REQ_TEAM}/TeamArchitect"),
                target=TargetRefV1(kind=TargetKind.TOOL, id="http:gitea"),
                level=AccessLevel.WRITE,
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id="owner-1"),
                created_at_utc=now,
            )
        )
        store.set_account_sharing(
            AccountSharingV1(
                team_id=REQ_TEAM,
                subject="http:gitea",
                sharing=AccountSharing.PRIVATE,
                changed_by_person_id="owner-1",
                changed_at_utc=now,
            )
        )

        # 6. confirmed=True removes the file, revokes active links & old grants, clears setup states & sharing, and bumps version.
        version_before = store.version(REQ_TEAM)
        applied = json.loads(
            asyncio.run(
                propose_access_change(
                    title="Remove Gitea",
                    reason="Retired.",
                    tool_context=Context("tooluse-off-apply"),
                    tool_removals=[{"tool_id": "http:gitea"}],
                    confirmed=True,
                )
            )
        )
        assert applied["status"] == "applied"
        assert not tool_file.exists()
        assert list(tool_file.parent.glob(".removed-*.tmp")) == []
        assert store.list_links(REQ_TEAM, purpose=None) == []
        assert store.get("g_old_auth_off").status is GrantStatus.REVOKED  # type: ignore[union-attr]
        assert store.tool_setup_state(REQ_TEAM, "owner-1", "http:gitea") is None
        assert store.account_sharing(REQ_TEAM, "http:gitea") is None
        assert store.version(REQ_TEAM) > version_before

        # 7. C03: when storage_settings is absent in auth-off mode, removal still previews and applies cleanly.
        from types import SimpleNamespace

        cli_file = write_instance_tool(instance, _cli_tool("nostore"))
        lead.task_manager = SimpleNamespace(storage_settings=None)
        no_store_preview = json.loads(
            asyncio.run(
                propose_access_change(
                    title="Remove CLI without store",
                    reason="Retired.",
                    tool_context=Context("tooluse-off-nostore-preview"),
                    tool_removals=[{"tool_id": "cli:nostore"}],
                    confirmed=False,
                )
            )
        )
        assert no_store_preview["status"] == "agreement_needed"
        assert cli_file.exists()
        no_store_applied = json.loads(
            asyncio.run(
                propose_access_change(
                    title="Remove CLI without store",
                    reason="Retired.",
                    tool_context=Context("tooluse-off-nostore-apply"),
                    tool_removals=[{"tool_id": "cli:nostore"}],
                    confirmed=True,
                )
            )
        )
        assert no_store_applied["status"] == "applied"
        assert not cli_file.exists()
    finally:
        access_runtime._runtime_access.cache_clear()
        set_agent_ref(None)


def test_f1_concurrent_removal_proposals_and_stalled_claim_never_resurrect_deleted_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: concurrent removal loser and stalled-claim wakeup after maintenance reconciliation never resurrect a deleted tool."""

    import asyncio

    from strands.interrupt import InterruptException

    import access_control.proposals as proposals_mod
    from access_control.decisions import HandDownRefused
    from custom_agents import access_runtime
    from custom_messages.contracts.access_v2 import AccessReviewChoice
    from custom_tools.providers.access_proposals import propose_access_change
    from tests.test_access_request_tools import Context

    world = _build_real_world(tmp_path, monkeypatch)
    try:
        instance = world["instance"]
        service = world["service"]
        requests = world["requests"]
        owner = world["owner"]
        second_admin = world["second_admin"]

        # Scenario 1: Two concurrent removal proposals for the same tool.
        # Proposal 2 prepares its removal journal while Proposal 1 wins and deletes the tool file;
        # Proposal 2 then fails in _commit_remove and rolls back — it must NOT resurrect the deleted tool!
        http_file = write_instance_tool(instance, _http_tool("http:race", "race.example.com"))
        access_runtime._runtime_access.cache_clear()

        c1 = Context("tooluse-f1-race-1")
        with pytest.raises(InterruptException):
            asyncio.run(
                propose_access_change(
                    title="Remove Race Tool 1",
                    reason="Decommissioned.",
                    tool_context=c1,
                    tool_removals=[{"tool_id": "http:race"}],
                )
            )
        req1 = c1.raised[0]["access_request_id"]

        c2 = Context("tooluse-f1-race-2")
        with pytest.raises(InterruptException):
            asyncio.run(
                propose_access_change(
                    title="Remove Race Tool 2",
                    reason="Decommissioned too.",
                    tool_context=c2,
                    tool_removals=[{"tool_id": "http:race"}],
                )
            )
        req2 = c2.raised[0]["access_request_id"]

        real_remove = proposals_mod.remove_proposed_tool_file
        interleaved = {"done": False}

        def interleave_winner(*args, **kwargs):
            original_before = kwargs["before_remove"]

            def wrapped_before(*b_args):
                original_before(*b_args)
                if not interleaved["done"]:
                    interleaved["done"] = True
                    # Winner runs end-to-end and legitimately deletes http:race!
                    assert requests().decide(req1, owner, AccessReviewChoice.APPROVE).status is AccessRequestStatus.GRANTED
                    assert not http_file.exists()

            kwargs["before_remove"] = wrapped_before
            return real_remove(*args, **kwargs)

        with monkeypatch.context() as race_ctx:
            race_ctx.setattr(proposals_mod, "remove_proposed_tool_file", interleave_winner)
            with pytest.raises(HandDownRefused, match="changed since it was reviewed"):
                requests().decide(req2, second_admin, AccessReviewChoice.APPROVE)

        assert interleaved["done"] is True
        assert not http_file.exists()
        assert list(http_file.parent.glob(".removed-*.tmp")) == []

        # Scenario 2: Stalled claim wakes up after maintenance reconciliation AND subsequent legitimate deletion.
        # Proposal 3 stages the file, then stalls before apply_together; maintenance restores & clears its recovery row;
        # then Proposal 3 is legitimately approved by a new claim (deleting the file);
        # finally the stalled first claim wakes up and enters _apply_items's except handler —
        # require_record=True, skip_live_claim=False skips undo and NEVER resurrects the deleted tool!
        stalled_file = write_instance_tool(instance, _http_tool("http:stalled", "stalled.example.com"))
        access_runtime._runtime_access.cache_clear()

        c3 = Context("tooluse-f1-stalled")
        with pytest.raises(InterruptException):
            asyncio.run(
                propose_access_change(
                    title="Remove Stalled Tool",
                    reason="Decommissioned.",
                    tool_context=c3,
                    tool_removals=[{"tool_id": "http:stalled"}],
                )
            )
        req3 = c3.raised[0]["access_request_id"]

        real_apply_together = service.store.apply_together
        stall_state = {"reconciled": False}
        undo_calls = {"count": 0}
        real_undo = proposals_mod.ProposalApplier._undo_items

        def count_undo(self_applier, *args, **kwargs):
            undo_calls["count"] += 1
            return real_undo(self_applier, *args, **kwargs)

        def stall_before_db(*args, **kwargs):
            if not stall_state["reconciled"]:
                stall_state["reconciled"] = True
                # Advance clock past claim timeout and run maintenance reconciliation.
                later = service.clock() + timedelta(minutes=11)
                monkeypatch.setattr(service, "clock", lambda: later)
                requests().recover_decisions()
                assert stalled_file.exists()
                # Now a fresh approval legitimately deletes the tool!
                assert requests().decide(req3, second_admin, AccessReviewChoice.APPROVE).status is AccessRequestStatus.GRANTED
                assert not stalled_file.exists()
                # Stalled first claim now hits DecisionClaimLost in apply_together.
                return real_apply_together(*args, **kwargs)
            return real_apply_together(*args, **kwargs)

        with monkeypatch.context() as stall_ctx:
            stall_ctx.setattr(service.store, "apply_together", stall_before_db)
            stall_ctx.setattr(proposals_mod.ProposalApplier, "_undo_items", count_undo)
            with pytest.raises(HandDownRefused):
                requests().decide(req3, owner, AccessReviewChoice.APPROVE)

        assert stall_state["reconciled"] is True
        # Maintenance ran _undo_items once to restore before the fresh approval;
        # the stalled wakeup must NOT run _undo_items a second time!
        assert undo_calls["count"] == 1
        assert not stalled_file.exists()
        assert list(stalled_file.parent.glob(".removed-*.tmp")) == []
    finally:
        world["cleanup"]()


def test_f2_refuses_new_unreviewed_grants_blocks_or_links_added_after_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """F2: newly added grant, block, pending grant, or credential link after submission is refused without revoking."""

    import asyncio

    from strands.interrupt import InterruptException

    import access_control.proposals as proposals_mod
    from access_control.decisions import HandDownRefused
    from custom_agents import access_runtime
    from custom_messages.contracts.access_v2 import AccessReviewChoice
    from custom_tools.providers.access_proposals import propose_access_change
    from tests.test_access_request_tools import Context

    world = _build_real_world(tmp_path, monkeypatch)
    try:
        instance = world["instance"]
        service = world["service"]
        requests = world["requests"]
        owner = world["owner"]
        team = world["team"]

        http_file = write_instance_tool(instance, _http_tool("http:clean", "clean.example.com"))
        original_bytes = http_file.read_bytes()
        access_runtime._runtime_access.cache_clear()

        # Submit removal when 0 grants, blocks, or links exist on http:clean.
        ctx = Context("tooluse-f2-clean")
        with pytest.raises(InterruptException):
            asyncio.run(
                propose_access_change(
                    title="Remove Clean Tool",
                    reason="No longer needed.",
                    tool_context=ctx,
                    tool_removals=[{"tool_id": "http:clean"}],
                )
            )
        req_id = ctx.raised[0]["access_request_id"]
        now = service.clock()
        owner_ref = SubjectRefV1(kind=SubjectKind.USER, id=owner.person_id)

        # 1. New active grant added before approval -> refused before touching disk.
        new_grant = GrantV1(
            grant_id="g_post_submit",
            team_id=team,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{team}/TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="http:clean"),
            level=AccessLevel.READ,
            granted_by=owner_ref,
            created_at_utc=now,
        )
        service.store.create(new_grant)
        caplog.set_level(logging.WARNING)
        caplog.clear()
        with pytest.raises(HandDownRefused, match="New access grants or restrictions were added"):
            requests().decide(req_id, owner, AccessReviewChoice.APPROVE)
        assert "event=proposal_tool_removal_impact_changed" in caplog.text
        assert http_file.exists() and http_file.read_bytes() == original_bytes
        assert service.store.get("g_post_submit").status is GrantStatus.ACTIVE  # type: ignore[union-attr]
        assert service.store.get_request(req_id).status is AccessRequestStatus.WAITING  # type: ignore[union-attr]
        service.store.revoke("g_post_submit", actor=owner_ref, at=now)

        # 2. New active block added before approval -> refused before touching disk.
        new_block = GrantV1(
            grant_id="b_post_submit",
            team_id=team,
            kind=GrantKind.BLOCK,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{team}/Worker"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="http:clean"),
            level=AccessLevel.FULL,
            granted_by=owner_ref,
            created_at_utc=now,
        )
        service.store.create(new_block)
        caplog.clear()
        with pytest.raises(HandDownRefused, match="New access grants or restrictions were added"):
            requests().decide(req_id, owner, AccessReviewChoice.APPROVE)
        assert "event=proposal_tool_removal_impact_changed" in caplog.text
        assert "b_post_submit" in caplog.text
        assert http_file.exists() and http_file.read_bytes() == original_bytes
        assert service.store.get("b_post_submit").status is GrantStatus.ACTIVE  # type: ignore[union-attr]
        assert service.store.get_request(req_id).status is AccessRequestStatus.WAITING  # type: ignore[union-attr]
        service.store.revoke("b_post_submit", actor=owner_ref, at=now)

        # 3. New pending grant added before approval -> refused before touching disk.
        new_pending = GrantV1(
            grant_id="g_pending_post_submit",
            team_id=team,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{team}/Worker"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="http:clean"),
            level=AccessLevel.READ,
            status=GrantStatus.PENDING,
            granted_by=owner_ref,
            created_at_utc=now,
        )
        service.store.create(new_pending)
        caplog.clear()
        with pytest.raises(HandDownRefused, match="New access grants or restrictions were added"):
            requests().decide(req_id, owner, AccessReviewChoice.APPROVE)
        assert "event=proposal_tool_removal_impact_changed" in caplog.text
        assert "g_pending_post_submit" in caplog.text
        assert http_file.exists() and http_file.read_bytes() == original_bytes
        assert service.store.get("g_pending_post_submit").status is GrantStatus.PENDING  # type: ignore[union-attr]
        assert service.store.get_request(req_id).status is AccessRequestStatus.WAITING  # type: ignore[union-attr]
        service.store.revoke("g_pending_post_submit", actor=owner_ref, at=now)

        # 4. New active grant racing in AFTER file staging (inside apply_together lock) ->
        # refused under DB write lock, DB state unchanged, and staged file restored cleanly.
        real_remove = proposals_mod.remove_proposed_tool_file
        raced_grant = GrantV1(
            grant_id="g_raced_in",
            team_id=team,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{team}/TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="http:clean"),
            level=AccessLevel.READ,
            granted_by=owner_ref,
            created_at_utc=now,
        )
        version_after_grant_race: dict[str, int] = {}

        def inject_grant_after_staging(*args, **kwargs):
            result = real_remove(*args, **kwargs)
            assert not http_file.exists()
            service.store.create(raced_grant)
            version_after_grant_race["v"] = service.store.version(team)
            return result

        with monkeypatch.context() as race_ctx:
            race_ctx.setattr(proposals_mod, "remove_proposed_tool_file", inject_grant_after_staging)
            with pytest.raises(HandDownRefused, match="New access grants or restrictions were added"):
                requests().decide(req_id, owner, AccessReviewChoice.APPROVE)

        assert http_file.exists() and http_file.read_bytes() == original_bytes
        assert list(http_file.parent.glob(".removed-*.tmp")) == []
        assert service.store.version(team) == version_after_grant_race["v"]
        assert service.store.get("g_raced_in").status is GrantStatus.ACTIVE  # type: ignore[union-attr]
        assert service.store.get_request(req_id).status is AccessRequestStatus.WAITING  # type: ignore[union-attr]
        service.store.revoke("g_raced_in", actor=owner_ref, at=now)

        # 5. New active block racing in AFTER file staging (inside apply_together lock) ->
        # refused under DB write lock, DB state unchanged, and staged file restored cleanly.
        raced_block = GrantV1(
            grant_id="b_raced_in",
            team_id=team,
            kind=GrantKind.BLOCK,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{team}/Worker"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="http:clean"),
            level=AccessLevel.FULL,
            granted_by=owner_ref,
            created_at_utc=now,
        )
        version_after_block_race: dict[str, int] = {}

        def inject_block_after_staging(*args, **kwargs):
            result = real_remove(*args, **kwargs)
            assert not http_file.exists()
            service.store.create(raced_block)
            version_after_block_race["v"] = service.store.version(team)
            return result

        with monkeypatch.context() as race_ctx:
            race_ctx.setattr(proposals_mod, "remove_proposed_tool_file", inject_block_after_staging)
            with pytest.raises(HandDownRefused, match="New access grants or restrictions were added"):
                requests().decide(req_id, owner, AccessReviewChoice.APPROVE)

        assert http_file.exists() and http_file.read_bytes() == original_bytes
        assert list(http_file.parent.glob(".removed-*.tmp")) == []
        assert service.store.version(team) == version_after_block_race["v"]
        assert service.store.get("b_raced_in").status is GrantStatus.ACTIVE  # type: ignore[union-attr]
        assert service.store.get_request(req_id).status is AccessRequestStatus.WAITING  # type: ignore[union-attr]
        service.store.revoke("b_raced_in", actor=owner_ref, at=now)

        # 6. New active credential link racing in AFTER file staging (inside apply_together lock) ->
        # refused under DB write lock, link stays ACTIVE, and staged file is restored cleanly!
        raced_link = AccountLinkV1(
            link_id="l_raced_in",
            team_id=team,
            tool_id="http:clean",
            purpose=LinkPurpose.ACCOUNT,
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=team,
            credential_key="cred-raced",
            created_by=owner_ref,
            created_at_utc=now,
        )
        version_after_link_race: dict[str, int] = {}

        def inject_link_after_staging(*args, **kwargs):
            result = real_remove(*args, **kwargs)
            assert not http_file.exists()
            service.store.create_link(raced_link)
            version_after_link_race["v"] = service.store.version(team)
            return result

        with monkeypatch.context() as race_ctx:
            race_ctx.setattr(proposals_mod, "remove_proposed_tool_file", inject_link_after_staging)
            with pytest.raises(HandDownRefused, match="New credential links were added"):
                requests().decide(req_id, owner, AccessReviewChoice.APPROVE)

        assert http_file.exists() and http_file.read_bytes() == original_bytes
        assert list(http_file.parent.glob(".removed-*.tmp")) == []
        assert service.store.version(team) == version_after_link_race["v"]
        assert service.store.get_link("l_raced_in").status is AccountLinkStatus.ACTIVE  # type: ignore[union-attr]
        assert service.store.get_request(req_id).status is AccessRequestStatus.WAITING  # type: ignore[union-attr]
    finally:
        world["cleanup"]()


def test_link_row_parsing_and_approval_succeed_when_owner_id_contains_on_or_colon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: link_id precedes free-form owner_id with ':' so ' on ' in owner_id never breaks impact locking."""

    import asyncio

    from strands.interrupt import InterruptException

    from access_control.tool_removals import (
        is_tool_removal_overview_row,
        parse_tool_removal_row_target,
        reviewed_removal_impact_ids,
    )
    from custom_agents import access_runtime
    from custom_messages.contracts.access_v2 import AccessReviewChoice
    from custom_tools.providers.access_proposals import propose_access_change
    from tests.test_access_request_tools import Context

    # Direct parser check with ' on ' and ':' inside owner_id, plus malformed detail rows.
    world = _build_real_world(tmp_path, monkeypatch)
    try:
        instance = world["instance"]
        service = world["service"]
        requests = world["requests"]
        owner = world["owner"]
        team = world["team"]

        http_file = write_instance_tool(instance, _http_tool("http:oncall", "oncall.example.com"))
        access_runtime._runtime_access.cache_clear()
        now = service.clock()
        tricky_owner = "Alice on Call: Primary"
        personal_link = service.store.create_link(
            AccountLinkV1(
                link_id="l_alice_on_call",
                team_id=team,
                tool_id="http:oncall",
                purpose=LinkPurpose.ACCOUNT,
                owner_kind=LinkOwnerKind.PERSON,
                owner_id=tricky_owner,
                credential_key="cred-alice-on-call",
                created_by=SubjectRefV1(kind=SubjectKind.USER, id=tricky_owner),
                created_at_utc=now,
            )
        )

        ctx = Context("tooluse-f2-owner-on")
        with pytest.raises(InterruptException):
            asyncio.run(
                propose_access_change(
                    title="Remove On-Call Tool",
                    reason="Decommissioned.",
                    tool_context=ctx,
                    tool_removals=[{"tool_id": "http:oncall"}],
                )
            )
        req_id = ctx.raised[0]["access_request_id"]
        waiting = service.store.get_request(req_id)
        assert waiting is not None and waiting.change is not None
        assert is_tool_removal_overview_row(waiting.change.items[0]) is True
        link_rows = [item for item in waiting.change.items if item.item_id == "remove-0-link-0"]
        assert len(link_rows) == 1
        assert is_tool_removal_overview_row(link_rows[0]) is False
        assert f"personal account link {personal_link.link_id}: for {tricky_owner} on " in link_rows[0].text
        assert parse_tool_removal_row_target(link_rows[0]) == ("link", personal_link.link_id)
        _, reviewed_links = reviewed_removal_impact_ids(waiting.change.items)
        assert reviewed_links == frozenset({personal_link.link_id})
        # Malformed detail row without ':' returns None rather than guessing.
        malformed = ReviewItemV1(
            item_id="remove-0-link-99",
            section="tool_removal",
            text="Revoke active personal account link l_bad on OnCall",
        )
        assert is_tool_removal_overview_row(malformed) is False
        assert parse_tool_removal_row_target(malformed) is None

        # Approval succeeds (not refused as unreviewed_links) and revokes the link.
        decided = requests().decide(req_id, owner, AccessReviewChoice.APPROVE)
        assert decided.status is AccessRequestStatus.GRANTED
        assert not http_file.exists()
        assert service.store.get_link(personal_link.link_id).status is AccountLinkStatus.REVOKED  # type: ignore[union-attr]
    finally:
        world["cleanup"]()


def test_f5_auth_off_rollback_isolates_step_errors_and_restores_mcp_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """F5: auth-off rollback isolates per-step failures and restores activated MCP records to pending."""

    import asyncio
    import json

    from custom_agents import access_runtime
    import custom_tools.providers.access_proposals as proposals_tool_mod
    from custom_tools.providers.access_proposals import propose_access_change
    from mcp_control.registry import (
        MCPConnectionLifecycle,
        MCPConnectionSource,
        MCPConnectionStore,
        _new_record,
        descriptor_from_server_config,
        resolve_mcp_connection_paths,
    )
    from tests.broker_access_support import context_verify_keys
    from tests.task_work_support import set_agent_ref
    from tests.test_access_request_tools import TEAM as REQ_TEAM, Context, make_agent

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    instance = tmp_path / "instance"
    (instance / "control").mkdir(parents=True)
    lead = make_agent(None, tmp_path, agent_type="TeamArchitect", lead=True)
    lead.instance_root = str(instance)
    lead.authorization_config = {"enabled": False}
    set_agent_ref(lead)

    try:
        file_a = write_instance_tool(instance, _http_tool("http:alpha", "alpha.example.com"))
        write_instance_tool(instance, _http_tool("http:beta", "beta.example.com"))
        bytes_a = file_a.read_bytes()

        # Register a pending MCP server to include in the same auth-off proposal.
        mcp_store = MCPConnectionStore(resolve_mcp_connection_paths(instance))
        descriptor = descriptor_from_server_config(
            {"name": "docs", "transport": "streamable_http", "server_url": "https://docs.example.com/mcp"},
            source=MCPConnectionSource.AGENT_PROPOSAL,
            source_locator="agent-proposal:test:https://docs.example.com/mcp",
            team_id=REQ_TEAM,
            owner_canonical_user_id="owner-1",
        )
        pending_record = mcp_store.save(
            _new_record(
                descriptor,
                lifecycle=MCPConnectionLifecycle.PENDING,
                target_agent_types=("TeamArchitect",),
            )
        )

        # Make apply_together fail AND make restoring the FIRST removed file (file_b, reversed order) raise OSError:
        # rollback must STILL restore file_a AND put the MCP server back to PENDING!
        real_restore = proposals_tool_mod.restore_removed_instance_tool
        first_failed = {"done": False}

        def fail_first_restore(path: Path, staging_path: Path, *, expected_device: int, expected_inode: int) -> None:
            if not first_failed["done"]:
                first_failed["done"] = True
                raise OSError("simulated disk error on first restore")
            real_restore(path, staging_path, expected_device=expected_device, expected_inode=expected_inode)

        caplog.set_level(logging.WARNING)
        caplog.clear()
        with monkeypatch.context() as ctx_m:
            ctx_m.setattr(proposals_tool_mod, "restore_removed_instance_tool", fail_first_restore)
            ctx_m.setattr(
                AccessRepository,
                "apply_together",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db transaction failed")),
            )
            result = json.loads(
                asyncio.run(
                    propose_access_change(
                        title="Combined removal and MCP install",
                        reason="Test rollback isolation.",
                        tool_context=Context("tooluse-f5-isolated-rollback"),
                        tool_removals=[{"tool_id": "http:alpha"}, {"tool_id": "http:beta"}],
                        mcp_servers=[
                            {
                                "connection_id": pending_record.connection_id,
                                "record_digest": pending_record.record_digest,
                            }
                        ],
                        confirmed=True,
                    )
                )
            )
        assert result["status"] == "refused" and "db transaction failed" in result["reason"]
        assert first_failed["done"] is True
        assert "event=proposal_agreement_rollback_step_failed" in caplog.text
        # Even though file_b's restore raised, file_a was still restored AND the MCP server was restored to PENDING!
        assert file_a.exists() and file_a.read_bytes() == bytes_a
        reloaded_mcp = mcp_store.require(pending_record.connection_id)
        assert reloaded_mcp.lifecycle is MCPConnectionLifecycle.PENDING
        assert reloaded_mcp.record_digest == pending_record.record_digest
    finally:
        access_runtime._runtime_access.cache_clear()
        set_agent_ref(None)


def test_c13_auth_off_multi_mcp_failure_and_mcp_store_rollback_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """C13: multi-MCP auth-off proposals validate upfront, roll back mid-loop activations, and isolate _mcp_store errors."""

    import asyncio
    import json

    from custom_agents import access_runtime
    import custom_tools.providers.access_proposals as proposals_tool_mod
    from custom_tools.providers.access_proposals import propose_access_change
    from mcp_control.registry import (
        MCPConnectionLifecycle,
        MCPConnectionSource,
        MCPConnectionStore,
        _new_record,
        descriptor_from_server_config,
        resolve_mcp_connection_paths,
    )
    from tests.broker_access_support import context_verify_keys
    from tests.task_work_support import set_agent_ref
    from tests.test_access_request_tools import TEAM as REQ_TEAM, Context, make_agent

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    instance = tmp_path / "instance"
    (instance / "control").mkdir(parents=True)
    lead = make_agent(None, tmp_path, agent_type="TeamArchitect", lead=True)
    lead.instance_root = str(instance)
    lead.authorization_config = {"enabled": False}
    set_agent_ref(lead)

    try:
        mcp_store = MCPConnectionStore(resolve_mcp_connection_paths(instance))
        pending_1 = mcp_store.save(
            _new_record(
                descriptor_from_server_config(
                    {"name": "docs-one", "transport": "streamable_http", "server_url": "https://one.example.com/mcp"},
                    source=MCPConnectionSource.AGENT_PROPOSAL,
                    source_locator="agent-proposal:test:https://one.example.com/mcp",
                    team_id=REQ_TEAM,
                    owner_canonical_user_id="owner-1",
                ),
                lifecycle=MCPConnectionLifecycle.PENDING,
                target_agent_types=("TeamArchitect",),
            )
        )
        pending_2 = mcp_store.save(
            _new_record(
                descriptor_from_server_config(
                    {"name": "docs-two", "transport": "streamable_http", "server_url": "https://two.example.com/mcp"},
                    source=MCPConnectionSource.AGENT_PROPOSAL,
                    source_locator="agent-proposal:test:https://two.example.com/mcp",
                    team_id=REQ_TEAM,
                    owner_canonical_user_id="owner-1",
                ),
                lifecycle=MCPConnectionLifecycle.PENDING,
                target_agent_types=("TeamArchitect",),
            )
        )

        # 1. Second MCP server fails upfront validation (stale digest): first MCP server is never activated.
        update_calls: list[str] = []
        real_update = MCPConnectionStore.update_lifecycle

        def spy_update(self: MCPConnectionStore, connection_id: str, *args: object, **kwargs: object) -> object:
            update_calls.append(connection_id)
            return real_update(self, connection_id, *args, **kwargs)

        with monkeypatch.context() as ctx_m:
            ctx_m.setattr(MCPConnectionStore, "update_lifecycle", spy_update)
            stale_result = json.loads(
                asyncio.run(
                    propose_access_change(
                        title="Install two MCP servers with stale second digest",
                        reason="Verify upfront validation.",
                        tool_context=Context("tooluse-c13-stale-second"),
                        mcp_servers=[
                            {
                                "connection_id": pending_1.connection_id,
                                "record_digest": pending_1.record_digest,
                            },
                            {
                                "connection_id": pending_2.connection_id,
                                "record_digest": "0" * 64,
                            },
                        ],
                        confirmed=True,
                    )
                )
            )
        assert stale_result["status"] == "refused"
        assert "not the pending server" in stale_result["reason"]
        assert update_calls == []
        assert mcp_store.require(pending_1.connection_id).lifecycle is MCPConnectionLifecycle.PENDING
        assert mcp_store.require(pending_2.connection_id).lifecycle is MCPConnectionLifecycle.PENDING

        # 2. Both pass validation, first activates, second raises inside update_lifecycle:
        # rollback restores the first MCP server back to PENDING with its original digest.
        def fail_second_update(self: MCPConnectionStore, connection_id: str, *args: object, **kwargs: object) -> object:
            if connection_id == pending_2.connection_id:
                raise RuntimeError("simulated MCP store failure on second server")
            return real_update(self, connection_id, *args, **kwargs)

        with monkeypatch.context() as ctx_m:
            ctx_m.setattr(MCPConnectionStore, "update_lifecycle", fail_second_update)
            mid_fail_result = json.loads(
                asyncio.run(
                    propose_access_change(
                        title="Install two MCP servers where second activation fails",
                        reason="Verify mid-loop MCP rollback.",
                        tool_context=Context("tooluse-c13-mid-loop-fail"),
                        mcp_servers=[
                            {
                                "connection_id": pending_1.connection_id,
                                "record_digest": pending_1.record_digest,
                            },
                            {
                                "connection_id": pending_2.connection_id,
                                "record_digest": pending_2.record_digest,
                            },
                        ],
                        confirmed=True,
                    )
                )
            )
        assert mid_fail_result["status"] == "refused"
        assert "simulated MCP store failure on second server" in mid_fail_result["reason"]
        reloaded_1 = mcp_store.require(pending_1.connection_id)
        reloaded_2 = mcp_store.require(pending_2.connection_id)
        assert reloaded_1.lifecycle is MCPConnectionLifecycle.PENDING
        assert reloaded_1.record_digest == pending_1.record_digest
        assert reloaded_2.lifecycle is MCPConnectionLifecycle.PENDING
        assert reloaded_2.record_digest == pending_2.record_digest

        # 3. _mcp_store failure during rollback must not prevent earlier file rollback steps from restoring files.
        file_gamma = write_instance_tool(instance, _http_tool("http:gamma", "gamma.example.com"))
        bytes_gamma = file_gamma.read_bytes()
        real_mcp_store = proposals_tool_mod._mcp_store
        store_calls = {"count": 0}

        def fail_mcp_store_during_rollback(agent_obj: object) -> object:
            store_calls["count"] += 1
            # Call 1: _agreement_rows; Call 2: _install_by_agreement; Call 3+: rollback.
            if store_calls["count"] >= 3:
                raise RuntimeError("simulated _mcp_store resolution error during rollback")
            return real_mcp_store(agent_obj)

        caplog.set_level(logging.WARNING)
        caplog.clear()
        with monkeypatch.context() as ctx_m:
            ctx_m.setattr(proposals_tool_mod, "_mcp_store", fail_mcp_store_during_rollback)
            ctx_m.setattr(
                AccessRepository,
                "apply_together",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db failed after staging")),
            )
            isolated_result = json.loads(
                asyncio.run(
                    propose_access_change(
                        title="Combined removal and MCP with broken rollback MCP store",
                        reason="Verify file restoration runs even if _mcp_store fails during rollback.",
                        tool_context=Context("tooluse-c13-rollback-store-error"),
                        tool_removals=[{"tool_id": "http:gamma"}],
                        mcp_servers=[
                            {
                                "connection_id": pending_1.connection_id,
                                "record_digest": pending_1.record_digest,
                            }
                        ],
                        confirmed=True,
                    )
                )
            )
        assert isolated_result["status"] == "refused"
        assert "db failed after staging" in isolated_result["reason"]
        assert file_gamma.exists() and file_gamma.read_bytes() == bytes_gamma
        assert list(file_gamma.parent.glob(".removed-*.tmp")) == []
        assert "event=proposal_agreement_rollback_step_failed" in caplog.text
        assert "simulated _mcp_store resolution error during rollback" in caplog.text

        # 4. Concurrent modification of an activated MCP record before rollback keeps the replacement,
        # logs event=proposal_agreement_mcp_changed, and still restores removed proposal files.
        pending_3 = mcp_store.save(
            _new_record(
                descriptor_from_server_config(
                    {"name": "docs-three", "transport": "streamable_http", "server_url": "https://three.example.com/mcp"},
                    source=MCPConnectionSource.AGENT_PROPOSAL,
                    source_locator="agent-proposal:test:https://three.example.com/mcp",
                    team_id=REQ_TEAM,
                    owner_canonical_user_id="owner-1",
                ),
                lifecycle=MCPConnectionLifecycle.PENDING,
                target_agent_types=("TeamArchitect",),
            )
        )
        file_delta = write_instance_tool(instance, _http_tool("http:delta", "delta.example.com"))
        bytes_delta = file_delta.read_bytes()
        concurrent_record = None

        def modify_mcp_then_fail_db(*_args: object, **_kwargs: object) -> object:
            nonlocal concurrent_record
            # Another writer updates the newly activated MCP record (changing its lifecycle to DISABLED).
            concurrent_record = mcp_store.update_lifecycle(
                pending_3.connection_id,
                lifecycle=MCPConnectionLifecycle.DISABLED,
            )
            raise RuntimeError("db failed after concurrent MCP update")

        caplog.set_level(logging.WARNING)
        caplog.clear()
        with monkeypatch.context() as ctx_m:
            ctx_m.setattr(AccessRepository, "apply_together", modify_mcp_then_fail_db)
            concurrent_result = json.loads(
                asyncio.run(
                    propose_access_change(
                        title="Combined removal and MCP with concurrent MCP modification",
                        reason="Verify _restore_agreement_server keeps concurrent MCP replacement.",
                        tool_context=Context("tooluse-c03-concurrent-mcp-guard"),
                        tool_removals=[{"tool_id": "http:delta"}],
                        mcp_servers=[
                            {
                                "connection_id": pending_3.connection_id,
                                "record_digest": pending_3.record_digest,
                            }
                        ],
                        confirmed=True,
                    )
                )
            )
        assert concurrent_result["status"] == "refused"
        assert "db failed after concurrent MCP update" in concurrent_result["reason"]
        assert concurrent_record is not None
        kept = mcp_store.require(pending_3.connection_id)
        assert kept.lifecycle is MCPConnectionLifecycle.DISABLED
        assert kept.record_digest == concurrent_record.record_digest
        assert file_delta.exists() and file_delta.read_bytes() == bytes_delta
        assert list(file_delta.parent.glob(".removed-*.tmp")) == []
        assert "event=proposal_agreement_mcp_changed" in caplog.text
        assert f"connection_id={pending_3.connection_id}" in caplog.text
        assert "event=proposal_agreement_apply_failed" in caplog.text
        assert f"mcp_servers={pending_3.connection_id}" in caplog.text
        assert "tool_removals=http:delta" in caplog.text
    finally:
        access_runtime._runtime_access.cache_clear()
        set_agent_ref(None)
