"""Atomic replacement and crash-recovery tests for proposal-written web API tools."""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from access_catalog.catalog import CatalogError
from access_catalog.instance_tools import (
    proposed_tool_path,
    read_proposed_tool_file,
    replace_instance_tool,
    restore_instance_tool,
    write_instance_tool,
)
from access_control.decision_effects import EffectJournal, ReplacedFile
from custom_messages.contracts.access_v2 import (
    AccessRequestStatus,
    CredentialInjectionV1,
    InjectionPlacement,
    ProposedCliToolV1,
    ProposedHttpToolChangeV1,
    ProposedHttpToolV1,
)
from custom_messages.contracts.cli_tool_v1 import InstalledCliV1
from runtime_settings.instance_layout import InstanceLayout
from storage.access_repository import (
    AccessRepository,
    DecisionClaim,
    DecisionClaimLost,
    DecisionRecovery,
)
from storage.config import resolve_storage_settings
from tests.test_access_store_postgres import NOW, OWNER, TEAM, _request


def _store(tmp_path: Path) -> AccessRepository:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}}
    )
    return AccessRepository(settings)


def _recovery(store: AccessRepository, *, claim_id: str = "claim_1") -> tuple[DecisionClaim, DecisionRecovery]:
    request = store.create_request(_request().model_copy(update={"request_id": f"req_{claim_id}"}))
    claim = DecisionClaim(request.request_id, claim_id)
    store.claim_decision(request.request_id, claim_id=claim.claim_id, at=NOW, until=NOW + timedelta(minutes=5))
    finished = request.model_copy(
        update={"status": AccessRequestStatus.GRANTED, "decided_by": OWNER, "decided_at_utc": NOW}
    )
    recovery = DecisionRecovery(
        claim,
        finished,
        {"tool_files": [], "replaced_tool_files": [], "programs": [], "servers": []},
        NOW + timedelta(minutes=5),
    )
    store.save_decision_recovery(recovery)
    return claim, recovery


def _gitea_v1() -> ProposedHttpToolV1:
    return ProposedHttpToolV1(
        tool_id="http:gitea",
        display_name="Gitea",
        hosts=("gitea.example.com",),
        uses_credential=False,
    )


def _gitea_v2() -> ProposedHttpToolChangeV1:
    return ProposedHttpToolChangeV1(
        tool_id="http:gitea",
        display_name="Gitea behind Cloudflare",
        hosts=("gitea.example.com",),
        uses_credential=False,
        gate_injections=(
            CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate-Secret", field="secret"),
        ),
    )


# read_proposed_tool_file -----------------------------------------------------


def test_read_proposed_tool_file_validates_single_http_tool_and_refuses_unsafe_or_foreign_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "instance"
    # Missing directory raises FileNotFoundError.
    with pytest.raises(FileNotFoundError):
        read_proposed_tool_file(root, "http:gitea")

    written = write_instance_tool(root, _gitea_v1())
    loaded = read_proposed_tool_file(root, "http:gitea")
    assert loaded.path == written
    assert loaded.raw_bytes == written.read_bytes()
    assert loaded.sha256 == hashlib.sha256(loaded.raw_bytes).hexdigest()
    assert loaded.definition.tool_id == "http:gitea"
    assert loaded.definition.hosts == ("gitea.example.com",)

    # Operator hand-written file with another name does not satisfy proposed-*.yaml lookup.
    tools_dir = InstanceLayout.at(root).instance_tools_dir
    (tools_dir / "operator-custom.yaml").write_text(
        yaml.safe_dump({"tools": [{"tool_id": "http:custom", "display_name": "Custom", "hosts": ["c.example.com"]}]}),
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError):
        read_proposed_tool_file(root, "http:custom")

    # Multi-tool proposed-*.yaml is refused.
    multi = proposed_tool_path(root, "http:multi")
    multi.write_text(
        yaml.safe_dump(
            {
                "tools": [
                    {"tool_id": "http:multi", "display_name": "Multi", "hosts": ["m1.example.com"]},
                    {"tool_id": "http:other", "display_name": "Other", "hosts": ["m2.example.com"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="exactly one tool"):
        read_proposed_tool_file(root, "http:multi")

    # Wrong tool_id inside proposed-*.yaml is refused.
    wrong_id = proposed_tool_path(root, "http:wrong")
    wrong_id.write_text(
        yaml.safe_dump({"tools": [{"tool_id": "http:else", "display_name": "Else", "hosts": ["e.example.com"]}]}),
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="does not match tool"):
        read_proposed_tool_file(root, "http:wrong")

    # Installed CLI proposed file is refused as not a web API tool.
    write_instance_tool(
        root,
        ProposedCliToolV1(
            tool_id="cli:demo",
            display_name="Demo",
            installed_cli=InstalledCliV1(
                binary_path="team/tools/demo/demo",
                sha256="a" * 64,
                commands={"version": "read"},
            ),
        ),
    )
    with pytest.raises(CatalogError, match="installed CLI"):
        read_proposed_tool_file(root, "cli:demo")

    # Symlink at proposed-*.yaml is refused without following.
    outside = tmp_path / "outside.yaml"
    outside.write_bytes(loaded.raw_bytes)
    symlink_path = proposed_tool_path(root, "http:symlink")
    symlink_path.symlink_to(outside)
    with pytest.raises(CatalogError, match="Cannot safely read"):
        read_proposed_tool_file(root, "http:symlink")

    # Multi-hardlink file is refused.
    hardlinked = proposed_tool_path(root, "http:hardlinked")
    hardlinked.write_bytes(loaded.raw_bytes)
    os.link(hardlinked, tmp_path / "second-link.yaml")
    with pytest.raises(CatalogError, match="Cannot safely read"):
        read_proposed_tool_file(root, "http:hardlinked")


# replace_instance_tool + ReplacedFile crash & recovery matrix ----------------


def test_replace_instance_tool_happy_path_and_precondition_conflicts(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    write_instance_tool(root, _gitea_v1())
    initial = read_proposed_tool_file(root, "http:gitea")

    # Mismatched expected digest is refused before any write.
    with pytest.raises(CatalogError, match="changed since it was reviewed"):
        replace_instance_tool(root, _gitea_v2(), expected_previous_sha256="0" * 64)
    assert initial.path.read_bytes() == initial.raw_bytes

    # File edited between initial read and publish is refused without overwriting the newer edit.
    concurrent_edit = b"tools:\n- tool_id: http:gitea\n  display_name: Edited\n  hosts:\n  - edited.example.com\n"

    def edit_before_publish(commit):
        initial.path.write_bytes(concurrent_edit)
        commit()

    with pytest.raises(CatalogError, match="changed since it was reviewed"):
        replace_instance_tool(
            root,
            _gitea_v2(),
            expected_previous_sha256=initial.sha256,
            publish=edit_before_publish,
        )
    assert initial.path.read_bytes() == concurrent_edit

    # Happy path with matching digest replaces atomically.
    current = read_proposed_tool_file(root, "http:gitea")
    replaced_path = replace_instance_tool(root, _gitea_v2(), expected_previous_sha256=current.sha256)
    assert replaced_path == initial.path
    updated = read_proposed_tool_file(root, "http:gitea")
    assert updated.definition.display_name == "Gitea behind Cloudflare"
    assert len(updated.definition.gate_injections) == 1


def test_replaced_file_restore_after_crash_before_os_replace_cleans_staging_quietly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "instance"
    write_instance_tool(root, _gitea_v1())
    initial = read_proposed_tool_file(root, "http:gitea")
    directory = initial.path.parent
    store = _store(tmp_path)
    _claim, recovery = _recovery(store)
    journal = EffectJournal(store, recovery)

    # Simulate a crash after journaling and writing the staging file, before os.replace:
    # the staging file stays on disk and the target still holds the previous bytes.
    staging = directory / ".proposed-crashed.tmp"
    new_bytes = b"tools:\n- tool_id: http:gitea\n  display_name: New\n  hosts:\n  - gitea.example.com\n"
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as handle:
        replaced = journal.prepare_replacement(
            "replaced_tool_files",
            directory,
            (initial.path.name,),
            handle.fileno(),
            new_bytes,
            initial.raw_bytes,
            staging_parts=(staging.name,),
        )
        handle.write(new_bytes)

    assert staging.exists()
    assert initial.path.read_bytes() == initial.raw_bytes

    caplog.set_level(logging.WARNING)
    reloaded = ReplacedFile.model_validate(store.decision_recoveries(TEAM)[0].effects["replaced_tool_files"][0])
    assert reloaded == replaced
    reloaded.restore(directory)

    assert not staging.exists()
    assert initial.path.read_bytes() == initial.raw_bytes
    assert "access_decision_artifact_changed" not in caplog.text


def test_replaced_file_restore_after_os_replace_restores_previous_bytes_and_is_idempotent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "instance"
    write_instance_tool(root, _gitea_v1())
    initial = read_proposed_tool_file(root, "http:gitea")
    directory = initial.path.parent
    store = _store(tmp_path)
    claim, recovery = _recovery(store)
    journal = EffectJournal(store, recovery)

    def before_replace(path: Path, descriptor: int, body: bytes, temporary: Path, previous_bytes: bytes) -> None:
        journal.prepare_replacement(
            "replaced_tool_files",
            path.parent,
            (path.name,),
            descriptor,
            body,
            previous_bytes,
            staging_parts=(temporary.name,),
        )

    def publish(commit) -> None:
        with store.decision_effect_transaction(claim):
            commit()

    replace_instance_tool(
        root,
        _gitea_v2(),
        expected_previous_sha256=initial.sha256,
        before_replace=before_replace,
        publish=publish,
    )
    assert initial.path.read_bytes() != initial.raw_bytes

    caplog.set_level(logging.WARNING)
    entry = ReplacedFile.model_validate(store.decision_recoveries(TEAM)[0].effects["replaced_tool_files"][0])
    entry.restore(directory, lambda data: restore_instance_tool(initial.path, data))
    assert initial.path.read_bytes() == initial.raw_bytes
    assert "instance_tool_restored" in caplog.text
    assert "access_decision_artifact_changed" not in caplog.text

    # A second recovery pass is a quiet no-op.
    caplog.clear()
    entry.restore(directory, lambda data: restore_instance_tool(initial.path, data))
    assert initial.path.read_bytes() == initial.raw_bytes
    assert caplog.text == ""


def test_replaced_file_restore_keeps_external_edit_or_deletion_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "instance"
    write_instance_tool(root, _gitea_v1())
    initial = read_proposed_tool_file(root, "http:gitea")
    directory = initial.path.parent
    store = _store(tmp_path)
    _claim, recovery = _recovery(store)
    journal = EffectJournal(store, recovery)

    def before_replace(path: Path, descriptor: int, body: bytes, temporary: Path, previous_bytes: bytes) -> None:
        journal.prepare_replacement(
            "replaced_tool_files",
            path.parent,
            (path.name,),
            descriptor,
            body,
            previous_bytes,
            staging_parts=(temporary.name,),
        )

    replace_instance_tool(
        root,
        _gitea_v2(),
        expected_previous_sha256=initial.sha256,
        before_replace=before_replace,
    )
    entry = ReplacedFile.model_validate(store.decision_recoveries(TEAM)[0].effects["replaced_tool_files"][0])

    # External edit after os.replace is preserved and logged.
    external = b"tools:\n- tool_id: http:gitea\n  display_name: Operator Edit\n  hosts:\n  - gitea.example.com\n"
    initial.path.write_bytes(external)
    caplog.set_level(logging.WARNING)
    entry.restore(directory)
    assert initial.path.read_bytes() == external
    assert "access_decision_artifact_changed" in caplog.text

    # External deletion after os.replace is not undone and is logged.
    caplog.clear()
    initial.path.unlink()
    entry.restore(directory)
    assert not initial.path.exists()
    assert "access_decision_artifact_changed" in caplog.text


def test_prepare_replacement_cleans_staging_when_journal_save_fails_with_or_without_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "instance"
    write_instance_tool(root, _gitea_v1())
    initial = read_proposed_tool_file(root, "http:gitea")
    directory = initial.path.parent
    store = _store(tmp_path)
    _claim, recovery = _recovery(store)
    journal = EffectJournal(store, recovery)

    real_save = store.save_decision_recovery

    def uncertain_save(rec: DecisionRecovery) -> None:
        real_save(rec)
        raise RuntimeError("lost database connection after commit")

    monkeypatch.setattr(store, "save_decision_recovery", uncertain_save)

    def before_replace(path: Path, descriptor: int, body: bytes, temporary: Path, previous_bytes: bytes) -> None:
        journal.prepare_replacement(
            "replaced_tool_files",
            path.parent,
            (path.name,),
            descriptor,
            body,
            previous_bytes,
            staging_parts=(temporary.name,),
        )

    with pytest.raises(RuntimeError, match="lost database connection"):
        replace_instance_tool(
            root,
            _gitea_v2(),
            expected_previous_sha256=initial.sha256,
            before_replace=before_replace,
        )

    # Original file is untouched and no staging temporary file was left behind.
    assert initial.path.read_bytes() == initial.raw_bytes
    assert [p.name for p in directory.iterdir()] == [initial.path.name]

    # Because the journal commit actually succeeded, later reconciliation runs restore() and sees no drift.
    caplog.set_level(logging.WARNING)
    saved_entry = ReplacedFile.model_validate(store.decision_recoveries(TEAM)[0].effects["replaced_tool_files"][0])
    saved_entry.restore(directory)
    assert initial.path.read_bytes() == initial.raw_bytes
    assert "access_decision_artifact_changed" not in caplog.text


def test_replace_instance_tool_refuses_publish_after_claim_is_lost(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    write_instance_tool(root, _gitea_v1())
    initial = read_proposed_tool_file(root, "http:gitea")
    store = _store(tmp_path)
    claim, _ = _recovery(store)
    store.release_decision(claim.request_id, claim.claim_id)

    def publish(commit) -> None:
        with store.decision_effect_transaction(claim):
            commit()

    with pytest.raises(DecisionClaimLost):
        replace_instance_tool(
            root,
            _gitea_v2(),
            expected_previous_sha256=initial.sha256,
            publish=publish,
        )
    assert initial.path.read_bytes() == initial.raw_bytes
