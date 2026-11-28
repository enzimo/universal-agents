"""Runtime exact-spelling enforcement across file_access, code_access, attachments, and broker task files.

Covers S2 (case-insensitive and Unicode normalization block-bypass refusals),
S3 (new file creation and code_access misspelled-grant skipping), S4 (zero
directory listings across repeated file_access.locate calls on ext4), C01
(authority before disk checks in file_access.locate), C10 (unverifiable
spelling fails closed), and Feature 3 (gateway attachments and broker task
files).
"""

from __future__ import annotations

import errno
import logging
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_control import folders
from access_control.entities import PersonPrincipal
from access_control.folders import (
    PATH_SPELLING_MISMATCH,
    PATH_SPELLING_UNVERIFIABLE,
    clear_spelling_cache,
)
from assistant_gateway.attachments import (
    stage_user_notification_attachments,
    validate_user_notification_attachments,
)
from custom_agents import access_runtime, code_access, folder_access
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    GrantKind,
    GrantLifetime,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.delivery_v1 import UserNotificationAttachment
from custom_tools import file_access
from custom_tools.non_interactive_file_tools import file_read, file_write
from custom_tools.path_policy import PathPolicyError
from custom_tools.workspace_scope import configured_data_folders, current_workspace_scope
from id_generation import new_id
from runtime_settings.instance_layout import InstanceLayout
from secure_tool_broker.task_files import TaskFileRejected, TaskFileStage
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer
from tests.data_folder_support import store_data_folder
from tests.folder_spelling_support import CountingPosixFS, SimulatedFS, install_simulated_fs
from tests.task_work_support import set_agent_ref
from tests.test_workspace_scope_policy import TEAM
from tests.test_workspace_scope_policy import layout as _scope_layout

layout = _scope_layout
BINARY = Path(__file__).resolve().parents[1] / "target" / "debug" / "ua-cedar-authorizer"
pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

OWNER = PersonPrincipal("owner-1", TEAM, ("owner",))
_active_services: list[Any] = []


@pytest.fixture(autouse=True)
def _cleanup() -> Any:
    clear_spelling_cache()
    folder_access.forget_kept_answers()
    yield
    clear_spelling_cache()
    folder_access.forget_kept_answers()
    set_agent_ref(None)
    while _active_services:
        _active_services.pop().cedar.close()


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


def _make_agent(
    layout: InstanceLayout,
    person: str = "alice",
    *,
    roles: tuple[str, ...] = ("member",),
    agent_type: str = "WebResearchAgent",
    task_id: str = "task-1",
) -> SimpleNamespace:
    context = context_signer().issue(team_id=TEAM, person_id=person, person_roles=roles)
    meta = SimpleNamespace(gateway_context=SimpleNamespace(canonical_user_id=person, task_context=context))
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(layout.root / "manager.db")}})
    task_manager = SimpleNamespace(
        current_task_id=task_id,
        get_task_info=lambda _tid: SimpleNamespace(meta=meta),
        storage_settings=storage,
    )
    agent = SimpleNamespace(
        id=f"agent-{agent_type}-{task_id}",
        name=agent_type,
        named_agent_type=agent_type,
        team_id=TEAM,
        is_team_lead=False,
        access_role="worker",
        instance_root=str(layout.root),
        workspace_dir=str(layout.workspaces_dir),
        authorization_config={"enabled": True, "cedar_binary_path": str(BINARY)},
        task_manager=task_manager,
        test_task_id=task_id,
    )
    service = access_runtime.runtime_access(agent).service
    if service is not None and service not in _active_services:
        _active_services.append(service)
    return agent


def _store_grant(
    agent: SimpleNamespace,
    *,
    target_id: str,
    kind: GrantKind,
    level: AccessLevel = AccessLevel.READ,
    target_kind: TargetKind = TargetKind.FOLDER,
    subject: SubjectRefV1 | None = None,
) -> None:
    service = access_runtime.runtime_access(agent).service
    assert service is not None
    service.store.create(
        GrantV1(
            grant_id=new_id("grant"),
            team_id=TEAM,
            kind=kind,
            subject=subject or SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/{agent.named_agent_type}"),
            target=TargetRefV1(kind=target_kind, id=target_id),
            level=level,
            lifetime=GrantLifetime.UNTIL_REVOKED,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
            reason="Test rule.",
            created_at_utc=datetime.now(timezone.utc),
        )
    )


def test_case_insensitive_and_unicode_block_bypasses_are_refused(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2/C06: with a block on team/finance/payroll.csv, case and Unicode misspellings are refused."""

    finance_dir = layout.team_dir / "finance"
    finance_dir.mkdir(parents=True, exist_ok=True)
    (finance_dir / "payroll.csv").write_text("salary,100000\n", encoding="utf-8")
    (finance_dir / "public.csv").write_text("quarter,Q4\n", encoding="utf-8")
    nfc_name = unicodedata.normalize("NFC", "café.txt")
    nfd_name = unicodedata.normalize("NFD", "café.txt")
    assert nfc_name != nfd_name
    (finance_dir / nfc_name).write_text("accent-secret\n", encoding="utf-8")

    agent = _make_agent(layout, "alice")
    _store_grant(agent, target_id="team/finance/payroll.csv", kind=GrantKind.BLOCK, level=AccessLevel.FULL)
    _store_grant(agent, target_id=f"team/finance/{nfc_name}", kind=GrantKind.BLOCK, level=AccessLevel.FULL)
    set_agent_ref(agent)

    # Patch both the spelling seam AND safe_open so without locate()'s spelling check
    # these misspelled paths would actually open and leak the blocked files (C06).
    sim = SimulatedFS(case_insensitive=True, unicode_normalizing=True)
    install_simulated_fs(monkeypatch, sim, patch_safe_open=True)

    # Warm folder cache via correctly spelled sibling; sibling stays readable.
    sibling = file_read(path=str(finance_dir / "public.csv"))
    assert sibling["status"] == "success"
    assert "quarter,Q4" in _text(sibling)

    # Misspelled parent folder is refused with exact disk spelling.
    bad_dir = file_read(path=str(layout.team_dir / "Finance" / "payroll.csv"))
    assert (bad_dir["status"], bad_dir["reason_code"]) == ("error", PATH_SPELLING_MISMATCH)
    assert "team/finance/payroll.csv" in _text(bad_dir)
    assert "salary,100000" not in _text(bad_dir)
    assert "Finance" in sim.stat_calls

    # Misspelled file name inside warmed folder cache is still refused (kept answers never bypass).
    bad_file = file_read(path=str(finance_dir / "PAYROLL.csv"))
    assert (bad_file["status"], bad_file["reason_code"]) == ("error", PATH_SPELLING_MISMATCH)
    assert "team/finance/payroll.csv" in _text(bad_file)
    assert "salary,100000" not in _text(bad_file)
    assert "PAYROLL.csv" in sim.stat_calls

    # Misspelled folder listing is refused.
    with pytest.raises(PathPolicyError) as list_err:
        file_access.list_directory(layout.team_dir / "FINANCE", agent=agent)
    assert list_err.value.reason_code == PATH_SPELLING_MISMATCH
    assert "team/finance" in str(list_err.value)
    assert "FINANCE" in sim.stat_calls

    # NFD vs NFC Unicode misspelling is refused with exact disk spelling and Unicode note.
    bad_unicode = file_read(path=str(finance_dir / nfd_name))
    assert (bad_unicode["status"], bad_unicode["reason_code"]) == ("error", PATH_SPELLING_MISMATCH)
    assert f"team/finance/{nfc_name}" in _text(bad_unicode)
    assert "Unicode normalization" in _text(bad_unicode)
    assert "accent-secret" not in _text(bad_unicode)
    assert nfd_name in sim.stat_calls

    # Correctly spelled sibling remains readable after all refusals.
    sibling_again = file_read(path=str(finance_dir / "public.csv"))
    assert sibling_again["status"] == "success"


def test_new_file_write_in_exact_folder_succeeds_while_misspelled_folder_is_refused(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S3: creating a new file with any spelling inside a correctly spelled folder succeeds."""

    finance_dir = layout.team_dir / "finance"
    finance_dir.mkdir(parents=True, exist_ok=True)

    agent = _make_agent(layout, "alice")
    set_agent_ref(agent)
    sim = SimulatedFS(case_insensitive=True)
    install_simulated_fs(monkeypatch, sim, patch_safe_open=True)

    created = file_write(path=str(finance_dir / "NewReport.CSV"), content="ok\n")
    assert created["status"] == "success"
    assert (finance_dir / "NewReport.CSV").read_text(encoding="utf-8") == "ok\n"

    bad_parent = file_write(path=str(layout.team_dir / "Finance" / "Other.CSV"), content="no\n")
    assert (bad_parent["status"], bad_parent["reason_code"]) == ("error", PATH_SPELLING_MISMATCH)
    assert "team/finance/Other.CSV" in _text(bad_parent)
    assert not (finance_dir / "Other.CSV").exists()


def test_authority_runs_before_disk_spelling_check_no_existence_oracle(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C01: probing another person's folder or an unshared data folder never touches disk spelling."""

    bob_taxes = layout.person_dir("bob") / "taxes"
    bob_taxes.mkdir(parents=True, exist_ok=True)
    (bob_taxes / "2025.pdf").write_text("bob-private\n", encoding="utf-8")

    reports_dir = layout.root.parent / "host_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "q1.csv").write_text("q1\n", encoding="utf-8")

    agent = _make_agent(layout, "alice")
    store_data_folder(agent.task_manager.storage_settings, "reports", reports_dir, team_id=TEAM)
    set_agent_ref(agent)

    sim = SimulatedFS(case_insensitive=True)
    install_simulated_fs(monkeypatch, sim)

    # Another person's private folder: misspelled existing, exact existing, and missing all fail with ACCESS_DENIED.
    other_misspelled = file_read(path=str(layout.person_dir("bob") / "Taxes" / "2025.pdf"))
    other_exact = file_read(path=str(bob_taxes / "2025.pdf"))
    other_missing = file_read(path=str(layout.person_dir("bob") / "Taxes" / "missing.pdf"))
    for res in (other_misspelled, other_exact, other_missing):
        assert (res["status"], res["reason_code"]) == ("error", file_access.ACCESS_DENIED)
    assert "users/bob/taxes/2025.pdf" not in _text(other_misspelled)
    assert "users/bob/taxes/2025.pdf" not in _text(other_missing)

    # Unshared data folder: misspelled existing and missing both fail with ACCESS_DENIED.
    data_misspelled = file_read(path=str(reports_dir / "Q1.csv"))
    data_missing = file_read(path=str(reports_dir / "missing.csv"))
    for res in (data_misspelled, data_missing):
        assert (res["status"], res["reason_code"]) == ("error", file_access.ACCESS_DENIED)
        assert "data/reports/q1.csv" not in _text(res)

    # Spelling check never probed disk for any unauthorized request.
    assert sim.stat_calls == []


def test_unverifiable_spelling_in_file_access_fails_closed(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C10: PermissionError on list_entries fails closed in file_read with PATH_SPELLING_UNVERIFIABLE."""

    finance_dir = layout.team_dir / "finance"
    finance_dir.mkdir(parents=True, exist_ok=True)
    (finance_dir / "payroll.csv").write_text("secret\n", encoding="utf-8")
    finance_ino = os.stat(finance_dir).st_ino

    agent = _make_agent(layout, "alice")
    set_agent_ref(agent)

    def deny_list(dir_fd: int) -> None:
        if os.fstat(dir_fd).st_ino == finance_ino:
            raise PermissionError(errno.EACCES, "Permission denied")

    sim = SimulatedFS(case_insensitive=True, on_list=deny_list)
    install_simulated_fs(monkeypatch, sim)

    res = file_read(path=str(finance_dir / "payroll.csv"))
    assert (res["status"], res["reason_code"]) == ("error", PATH_SPELLING_UNVERIFIABLE)
    assert "secret" not in _text(res)


def test_locate_does_not_list_directories_on_case_sensitive_ext4(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S4: across many file_access.locate() calls on case-sensitive ext4, directory listings stay at zero."""

    finance_dir = layout.team_dir / "finance"
    finance_dir.mkdir(parents=True, exist_ok=True)
    payroll = finance_dir / "payroll.csv"
    payroll.write_text("ok\n", encoding="utf-8")
    accented = finance_dir / unicodedata.normalize("NFC", "café.txt")
    accented.write_text("ok\n", encoding="utf-8")

    reports_dir = layout.root.parent / "s4_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    q1 = reports_dir / "q1.csv"
    q1.write_text("1\n", encoding="utf-8")

    agent = _make_agent(layout, "alice")
    store_data_folder(agent.task_manager.storage_settings, "reports", reports_dir, team_id=TEAM)
    _store_grant(agent, target_id="data/reports", kind=GrantKind.ACCESS, level=AccessLevel.READ)
    _store_grant(
        agent,
        target_id="data/reports",
        kind=GrantKind.ACCESS,
        level=AccessLevel.READ,
        subject=SubjectRefV1(kind=SubjectKind.USER, id="alice"),
    )
    set_agent_ref(agent)

    counting = CountingPosixFS()
    clear_spelling_cache()
    monkeypatch.setattr(folders, "_ACTIVE_FS", counting.as_spelling_fs())

    for _ in range(50):
        file_access.locate(payroll, agent=agent)
        file_access.locate(accented, agent=agent)
        file_access.locate(q1, agent=agent)

    assert counting.list_calls == 0
    assert "payroll.csv" in counting.stat_calls
    assert "q1.csv" in counting.stat_calls


def test_code_access_skips_stored_misspelled_folder_grant_with_warning_and_note(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S3: stored misspelled grant team/Finance is not mounted for code and agent gets a note."""

    finance_dir = layout.team_dir / "finance"
    finance_dir.mkdir(parents=True, exist_ok=True)
    (finance_dir / "payroll.csv").write_text("salary\n", encoding="utf-8")
    reports_dir = layout.root.parent / "code_reports"
    (reports_dir / "sub").mkdir(parents=True, exist_ok=True)

    agent = _make_agent(layout, "alice")
    store_data_folder(agent.task_manager.storage_settings, "reports", reports_dir, team_id=TEAM)
    # Block payroll.csv under exact case, and store a legacy misspelled grant on team/Finance
    # plus a misspelled grant on data/reports/Sub (whose disk name is 'sub').
    _store_grant(agent, target_id="team/finance/payroll.csv", kind=GrantKind.BLOCK, level=AccessLevel.FULL)
    _store_grant(agent, target_id="team/Finance", kind=GrantKind.ACCESS, level=AccessLevel.READ)
    _store_grant(agent, target_id="data/reports/Sub", kind=GrantKind.ACCESS, level=AccessLevel.READ)
    _store_grant(
        agent,
        target_id="data/reports/Sub",
        kind=GrantKind.ACCESS,
        level=AccessLevel.READ,
        subject=SubjectRefV1(kind=SubjectKind.USER, id="alice"),
    )
    set_agent_ref(agent)

    sim = SimulatedFS(case_insensitive=True)
    install_simulated_fs(monkeypatch, sim)

    scope = current_workspace_scope(agent)
    data_folders = configured_data_folders(agent)
    with caplog.at_level(logging.WARNING, logger="custom_agents.code_access"):
        planned = code_access.code_access(
            agent,
            scope,
            tool_id=code_access.CODE_SHELL_TOOL_ID,
            data_folders=data_folders,
        )

    mounted = set(planned.read_write) | set(planned.read_only)
    assert (layout.team_dir / "Finance") not in mounted
    assert (layout.team_dir / "finance") not in mounted
    assert (reports_dir / "Sub") not in mounted
    assert (reports_dir / "sub") not in mounted
    assert any("team/Finance" in note and "team/finance" in note for note in planned.notes)
    assert any("data/reports/Sub" in note and "data/reports/sub" in note for note in planned.notes)
    assert any("event=code_access_folder_spelling_skipped" in rec.message for rec in caplog.records)
    assert "Finance" in sim.stat_calls
    assert "Sub" in sim.stat_calls


def test_gateway_attachments_and_broker_task_files_refuse_misspelled_paths(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feature 3: gateway attachments and broker TaskFileStage refuse misspelled existing paths."""

    finance_dir = layout.team_dir / "finance"
    finance_dir.mkdir(parents=True, exist_ok=True)
    (finance_dir / "team.pdf").write_bytes(b"%PDF")
    alice_dir = layout.person_dir("alice")
    alice_dir.mkdir(parents=True, exist_ok=True)
    (alice_dir / "report.pdf").write_bytes(b"pdf")

    sim = SimulatedFS(case_insensitive=True)
    install_simulated_fs(monkeypatch, sim, patch_safe_open=True)

    config = SimpleNamespace(workspace_path=layout.workspaces_dir, instance_root_path=layout.root)

    # Gateway attachment validation refuses misspelled existing path with exact disk spelling.
    bad_path = str(layout.team_dir / "Finance" / "team.pdf")
    accepted, errors = validate_user_notification_attachments([bad_path], config=config, person_id="alice")
    assert accepted == []
    assert len(errors) == 1
    assert "team/finance/team.pdf" in errors[0]

    # Gateway attachment staging also refuses misspelled paths if handed directly.
    staged, stage_errors = stage_user_notification_attachments(
        [
            UserNotificationAttachment(
                path=bad_path,
                filename="team.pdf",
                content_type="application/pdf",
                kind="document",
            )
        ],
        config=config,
        person_id="alice",
    )
    assert staged == []
    assert len(stage_errors) == 1
    assert "team/finance/team.pdf" in stage_errors[0]

    # Exact spelling succeeds for gateway attachments.
    ok_path = str(finance_dir / "team.pdf")
    ok_accepted, ok_errors = validate_user_notification_attachments([ok_path], config=config, person_id="alice")
    assert ok_errors == [] and len(ok_accepted) == 1

    # Broker TaskFileStage refuses misspelled input and output paths, accepts new file in exact folder.
    stage = TaskFileStage.create(instance_root=str(layout.root), person_id="alice")
    try:
        with pytest.raises(TaskFileRejected, match="users/alice/report.pdf"):
            stage.input_file("REPORT.PDF")
        with pytest.raises(TaskFileRejected, match="team/finance/out.txt"):
            stage.output_file(str(layout.team_dir / "Finance" / "out.txt"))
        with pytest.raises(TaskFileRejected, match="team/finance"):
            stage.output_path(str(layout.team_dir / "FINANCE"))
        staged_in = stage.input_file("report.pdf")
        assert Path(staged_in).read_bytes() == b"pdf"
        staged_out = stage.output_file(str(finance_dir / "NewExport.txt"))
        assert Path(staged_out).name == "NewExport.txt"
    finally:
        stage.cleanup()


def test_file_access_locate_checks_spelling_on_backslash_filename_without_folder_id_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """file_access.locate uses check_spelling_below so POSIX filenames containing '\\' do not raise FolderIdError."""

    layout = InstanceLayout.at(tmp_path / "inst")
    layout.person_dir("alice").mkdir(parents=True, exist_ok=True)
    weird = layout.person_dir("alice") / "bad\\name.txt"
    weird.write_text("hi", encoding="utf-8")

    agent = SimpleNamespace(
        id="agent-backslash",
        named_agent_type="WebResearchAgent",
        team_id=TEAM,
        is_team_lead=False,
        access_role="worker",
        instance_root=str(layout.root),
        workspace_dir=str(layout.workspaces_dir),
        authorization_config={"enabled": False},
        task_manager=SimpleNamespace(current_task_id="t-1", get_task_info=lambda _tid: None, storage_settings=None),
    )
    sim = SimulatedFS(case_insensitive=True, unicode_normalizing=True)
    install_simulated_fs(monkeypatch, sim, patch_safe_open=True)
    monkeypatch.setattr(
        "custom_agents.access_runtime.verified_person_id",
        lambda _agent: "alice",
    )

    loc = file_access.locate(weird, agent=agent)
    assert loc.parts == ("bad\\name.txt",)

    with pytest.raises(PathPolicyError) as exc_info:
        file_access.locate(layout.person_dir("alice") / "BAD\\name.txt", agent=agent)
    assert exc_info.value.reason_code == PATH_SPELLING_MISMATCH
    assert repr("users/alice/bad\\name.txt") in str(exc_info.value)
