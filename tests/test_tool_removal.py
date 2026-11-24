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
from access_control.decision_effects import EffectJournal, RemovedFile
from access_control.proposals import proposal_digest
from custom_messages.contracts.access_v2 import (
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
    InjectionPlacement,
    LinkOwnerKind,
    LinkPurpose,
    ProposedCliToolV1,
    ProposedHttpToolChangeV1,
    ProposedHttpToolV1,
    ProposedToolRemovalV1,
    ReviewItemV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
    ToolKind,
    ToolSetupState,
    ToolSetupStateV1,
)
from custom_messages.contracts.cli_tool_v1 import InstalledCliV1
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

    # Restore real file and simulate a concurrent swap between opened_file and safe_open.remove_file.
    loaded.path.unlink()
    loaded.path.write_bytes(loaded.raw_bytes)
    real_remove = safe_open.remove_file
    replacement_bytes = b"tools:\n- tool_id: http:gitea\n  display_name: Swapped\n  hosts:\n  - gitea.example.com\n"

    def _swap_before_unlink(directory: Path, parts: tuple[str, ...], *, expected_identity=None) -> bool:
        temp = directory / ".swap.tmp"
        temp.write_bytes(replacement_bytes)
        os.replace(temp, directory / parts[0])
        return real_remove(directory, parts, expected_identity=expected_identity)

    monkeypatch.setattr(safe_open, "remove_file", _swap_before_unlink)
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
    store = _access_store(tmp_path)
    _claim, _recovery, journal = _journal(store)

    entry = journal.prepare_removal("removed_tool_files", (initial.path.name,), initial.raw_bytes)
    reloaded = RemovedFile.model_validate(store.decision_recoveries(TEAM)[0].effects["removed_tool_files"][0])
    assert reloaded == entry

    # Crash before unlink: original bytes still on disk -> quiet no-op.
    caplog.set_level(logging.WARNING)
    caplog.clear()
    reloaded.restore(directory)
    assert initial.path.read_bytes() == initial.raw_bytes
    assert caplog.text == ""

    # Crash after unlink: missing file -> restores exact previous bytes; second pass is quiet no-op.
    initial.path.unlink()
    reloaded.restore(directory, lambda data: restore_removed_instance_tool(initial.path, data))
    assert initial.path.read_bytes() == initial.raw_bytes
    assert "instance_tool_restored" in caplog.text
    assert "access_decision_artifact_changed" not in caplog.text

    caplog.clear()
    reloaded.restore(directory, lambda data: restore_removed_instance_tool(initial.path, data))
    assert caplog.text == ""

    # External different file at path -> kept untouched and logged.
    external = b"tools:\n- tool_id: http:gitea\n  display_name: External\n  hosts:\n  - gitea.example.com\n"
    initial.path.write_bytes(external)
    caplog.clear()
    reloaded.restore(directory)
    assert initial.path.read_bytes() == external
    assert "access_decision_artifact_changed" in caplog.text

    # External symlink at leaf -> kept untouched (target untouched) and logged.
    outside = tmp_path / "outside.yaml"
    outside.write_bytes(b"keep-me")
    initial.path.unlink()
    initial.path.symlink_to(outside)
    caplog.clear()
    reloaded.restore(directory)
    assert initial.path.is_symlink()
    assert outside.read_bytes() == b"keep-me"
    assert "access_decision_artifact_changed" in caplog.text

    # External directory at leaf -> kept untouched and logged.
    initial.path.unlink()
    initial.path.mkdir()
    caplog.clear()
    reloaded.restore(directory)
    assert initial.path.is_dir()
    assert "access_decision_artifact_changed" in caplog.text
    initial.path.rmdir()

    # External multi-hardlink at leaf -> kept untouched and logged.
    initial.path.write_bytes(initial.raw_bytes)
    second_link = tmp_path / "hardlink.yaml"
    os.link(initial.path, second_link)
    caplog.clear()
    reloaded.restore(directory)
    assert "access_decision_artifact_changed" in caplog.text
    second_link.unlink()

    # Unsafe parent directory (symlink) re-raises so recovery retries.
    bad_root = tmp_path / "bad_root"
    bad_root.mkdir()
    real_sub = tmp_path / "real_sub"
    real_sub.mkdir()
    (bad_root / "sub").symlink_to(real_sub)
    nested = RemovedFile.prepared(("sub", "proposed-http-gitea.yaml"), initial.raw_bytes)
    with pytest.raises(safe_open.UnsafePath) as exc_info:
        nested.restore(bad_root)
    assert exc_info.value.reason == safe_open.REASON_LINK

    # Concurrent creation race (REASON_DESTINATION_EXISTS): differing bytes logs; matching bytes is quiet.
    initial.path.unlink()

    def _race_different(_data: bytes) -> None:
        initial.path.write_bytes(external)
        raise safe_open.UnsafePath("exists", reason=safe_open.REASON_DESTINATION_EXISTS)

    caplog.clear()
    reloaded.restore(directory, _race_different)
    assert initial.path.read_bytes() == external
    assert "access_decision_artifact_changed" in caplog.text

    initial.path.unlink()

    def _race_same(_data: bytes) -> None:
        initial.path.write_bytes(initial.raw_bytes)
        raise safe_open.UnsafePath("exists", reason=safe_open.REASON_DESTINATION_EXISTS)

    caplog.clear()
    reloaded.restore(directory, _race_same)
    assert initial.path.read_bytes() == initial.raw_bytes
    assert caplog.text == ""

    # prepare_removal drops unsaved in-memory entry when save_decision_recovery fails.
    real_save = store.save_decision_recovery

    def _failing_save(rec: DecisionRecovery) -> None:
        real_save(rec)
        raise RuntimeError("db dropped after commit")

    monkeypatch.setattr(store, "save_decision_recovery", _failing_save)
    before_len = len(_recovery.effects["removed_tool_files"])
    with pytest.raises(RuntimeError, match="db dropped"):
        journal.prepare_removal("removed_tool_files", (initial.path.name,), initial.raw_bytes)
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

    # Setup states for http:api and sibling http:api.v2.
    store.put_tool_setup_state(_setup_state("alice", "http:api"))
    store.put_tool_setup_state(_setup_state("bob", "http:api"))
    store.put_tool_setup_state(_setup_state("alice", "http:api.v2"))

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


def test_revoke_links_for_tools_revokes_links_clears_setup_states_and_bumps_version(tmp_path: Path) -> None:
    store = _access_store(tmp_path)
    later = datetime(2026, 11, 24, 16, 5, tzinfo=timezone.utc)

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

    version_before = store.version(TEAM)
    revoked = store.revoke_links_for_tools(TEAM, ("http:gitea",), actor=OWNER, at=later)
    assert set(revoked) == {"l_acct", "l_gate"}
    assert store.version(TEAM) == version_before + 1
    assert store.get_link("l_acct").status is AccountLinkStatus.REVOKED  # type: ignore[union-attr]
    assert store.get_link("l_gate").status is AccountLinkStatus.REVOKED  # type: ignore[union-attr]
    assert store.get_link("l_other").status is AccountLinkStatus.ACTIVE  # type: ignore[union-attr]
    assert [(s.person_id, s.tool_id) for s in store.tool_setup_states(TEAM)] == [("alice", "http:other")]

    # Second call with no active links left does not bump the version.
    assert store.revoke_links_for_tools(TEAM, ("http:gitea",), actor=OWNER, at=later) == []
    assert store.version(TEAM) == version_before + 1
