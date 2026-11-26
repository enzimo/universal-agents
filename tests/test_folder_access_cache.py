"""Folder decision cache isolation and reuse tests (custom_agents.folder_access, AccessService.reusable_folder_allowance).

Verifies against the real Cedar evaluator and SQLite store that:
- single-file read and write shares never open sibling files or parent folder listings (B1, B2);
- folder crawls (direct, worker hand-on, and alongside a blocked sibling) reuse the kept folder answer (B3);
- cached folder/file answers match a fresh AccessService.decide_folder across grant and block setups;
- blocks added or removed while the cache is warm take effect immediately via the store version.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_control.data_folders import DataFolderRegistry, protected_locations
from access_control.entities import PersonPrincipal
from access_control.folders import person_folder_id
from custom_agents import folder_access
from custom_agents.access_runtime import runtime_access, task_access
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    DelegationMode,
    GrantKind,
    GrantLifetime,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from custom_tools import file_access
from custom_tools.non_interactive_file_tools import file_read, file_write
from custom_tools.path_policy import PathPolicyError
from runtime_settings.instance_layout import InstanceLayout
from tests.task_work_support import set_agent_ref
from tests.test_workspace_scope_policy import TEAM, task_agent
from tests.test_workspace_scope_policy import layout as _scope_layout

layout = _scope_layout
BINARY = Path(__file__).resolve().parents[1] / "target" / "debug" / "ua-cedar-authorizer"
pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

OWNER = PersonPrincipal("owner-1", TEAM, ("owner",))
ALICE = PersonPrincipal("alice", TEAM, ("member",))
BOB = PersonPrincipal("bob", TEAM, ("member",))


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


_active_services: list[Any] = []


@pytest.fixture(autouse=True)
def _close_cedar_clients() -> Any:
    yield
    while _active_services:
        _active_services.pop().cedar.close()


def _authorized_agent(
    layout: InstanceLayout,
    person: str = "alice",
    *,
    agent_type: str = "WebResearchAgent",
) -> SimpleNamespace:
    agent = task_agent(layout, person)
    agent.authorization_config = {"enabled": True, "cedar_binary_path": str(BINARY)}
    agent.named_agent_type = agent_type
    set_agent_ref(agent)
    folder_access.forget_kept_answers()
    service = runtime_access(agent).service
    if service is not None and service not in _active_services:
        _active_services.append(service)
    return agent


def _grant(
    *,
    subject: SubjectRefV1,
    target: TargetRefV1,
    level: AccessLevel,
    granted_by: str = "owner-1",
    kind: GrantKind = GrantKind.ACCESS,
    lifetime: GrantLifetime = GrantLifetime.UNTIL_REVOKED,
    uses_remaining: int | None = None,
    task_id: str | None = None,
    for_person_id: str | None = None,
    delegation_depth: int = 0,
    delegation_mode: DelegationMode = DelegationMode.ASK_OWNER,
) -> GrantV1:
    return GrantV1(
        grant_id="new",
        team_id=TEAM,
        kind=kind,
        subject=subject,
        target=target,
        level=level,
        lifetime=lifetime,
        uses_remaining=uses_remaining,
        task_id=task_id,
        for_person_id=for_person_id,
        delegation_depth=delegation_depth,
        delegation_mode=delegation_mode,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=granted_by),
        reason="Test access setup.",
        created_at_utc=datetime.now(timezone.utc),
    )


def _count_cedar_calls(service: Any, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Wrap ``service.cedar.decide`` and return a mutable 1-element call counter."""

    calls = [0]
    original = service.cedar.decide

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(service.cedar, "decide", counting)
    return calls


def _register_data_folder(layout: InstanceLayout, tmp_path: Path, service: Any, name: str = "reports") -> Path:
    folder = tmp_path / f"data_{name}"
    folder.mkdir(parents=True, exist_ok=True)
    registry = DataFolderRegistry(service.store, TEAM)
    protected = protected_locations(layout.root)
    registry.register(
        OWNER,
        name=name,
        path=str(folder),
        writable=True,
        reason="Test data folder.",
        protected=protected,
        now=datetime.now(timezone.utc),
    )
    return folder


# -- B1: Single-file read share isolation ------------------------------------


def test_single_file_read_share_does_not_open_sibling_file_or_folder_listing(layout: InstanceLayout) -> None:
    agent = _authorized_agent(layout, "alice")
    bob_dir = layout.person_dir("bob")
    (bob_dir / "shared.txt").write_text("shared-content", encoding="utf-8")
    (bob_dir / "secret.txt").write_text("top-secret", encoding="utf-8")

    bob_folder = person_folder_id("bob")
    service = runtime_access(agent).service
    assert service is not None
    service.grant_by_person(
        BOB,
        _grant(
            subject=SubjectRefV1(kind=SubjectKind.USER, id="alice"),
            target=TargetRefV1(kind=TargetKind.FILE, id=f"{bob_folder}/shared.txt"),
            level=AccessLevel.READ,
            granted_by="bob",
        ),
    )

    # Reading the shared file succeeds.
    first = file_read(path=str(bob_dir / "shared.txt"))
    assert first["status"] == "success"
    assert "shared-content" in _text(first)

    # Within the 10-second cache window, a sibling file and the parent folder listing are both refused.
    sibling = file_read(path=str(bob_dir / "secret.txt"))
    assert (sibling["status"], sibling["reason_code"]) == ("error", file_access.ACCESS_DENIED)
    assert "top-secret" not in _text(sibling)

    with pytest.raises(PathPolicyError) as listed:
        file_access.list_directory(bob_dir)
    assert listed.value.reason_code == file_access.ACCESS_DENIED

    # Re-reading the shared file itself still succeeds.
    again = file_read(path=str(bob_dir / "shared.txt"))
    assert again["status"] == "success"
    assert "shared-content" in _text(again)


# -- B2: Single-file write share isolation -----------------------------------


def test_single_file_write_share_does_not_allow_writing_sibling(layout: InstanceLayout) -> None:
    agent = _authorized_agent(layout, "alice")
    bob_dir = layout.person_dir("bob")
    (bob_dir / "shared.txt").write_text("before", encoding="utf-8")
    (bob_dir / "sibling.txt").write_text("untouched", encoding="utf-8")

    bob_folder = person_folder_id("bob")
    service = runtime_access(agent).service
    assert service is not None
    service.grant_by_person(
        BOB,
        _grant(
            subject=SubjectRefV1(kind=SubjectKind.USER, id="alice"),
            target=TargetRefV1(kind=TargetKind.FILE, id=f"{bob_folder}/shared.txt"),
            level=AccessLevel.WRITE,
            granted_by="bob",
        ),
    )

    wrote_shared = file_write(path=str(bob_dir / "shared.txt"), content="updated")
    assert wrote_shared["status"] == "success"
    assert (bob_dir / "shared.txt").read_text(encoding="utf-8") == "updated"

    wrote_sibling = file_write(path=str(bob_dir / "sibling.txt"), content="overwritten")
    assert (wrote_sibling["status"], wrote_sibling["reason_code"]) == ("error", file_access.ACCESS_DENIED)
    assert (bob_dir / "sibling.txt").read_text(encoding="utf-8") == "untouched"


# -- B3: Folder crawls reuse the kept folder answer --------------------------


def test_crawl_writing_many_files_into_allowed_folder_reuses_kept_folder_answer(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _authorized_agent(layout, "alice")
    crawl_dir = layout.team_dir / "crawl"
    crawl_dir.mkdir(parents=True, exist_ok=True)
    service = runtime_access(agent).service
    assert service is not None
    calls = _count_cedar_calls(service, monkeypatch)

    per_file: list[int] = []
    for index in range(20):
        before = calls[0]
        res = file_write(path=str(crawl_dir / f"item_{index}.txt"), content=f"row {index}")
        assert res["status"] == "success"
        per_file.append(calls[0] - before)

    # File 0 pays decide_folder (agent + person) plus reusable_folder_allowance (agent + person).
    assert per_file[0] == 4
    assert all(count == 0 for count in per_file[1:])
    assert calls[0] == 4


def test_worker_crawl_with_lead_hand_on_reuses_kept_folder_answer(
    layout: InstanceLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _authorized_agent(layout, "alice", agent_type="WebResearchAgent")
    service = runtime_access(agent).service
    assert service is not None
    reports_dir = _register_data_folder(layout, tmp_path, service, "reports")

    # Alice holds write on data/reports; TeamArchitect holds automatic delegable write for Alice's work.
    service.grant_by_person(
        OWNER,
        _grant(
            subject=SubjectRefV1(kind=SubjectKind.USER, id="alice"),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports"),
            level=AccessLevel.WRITE,
        ),
    )
    service.grant_by_person(
        OWNER,
        _grant(
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports"),
            level=AccessLevel.WRITE,
            for_person_id="alice",
            delegation_depth=1,
            delegation_mode=DelegationMode.AUTOMATIC,
        ),
    )

    calls = _count_cedar_calls(service, monkeypatch)
    per_file: list[int] = []
    for index in range(10):
        before = calls[0]
        res = file_write(path=str(reports_dir / f"report_{index}.txt"), content=f"data {index}")
        assert res["status"] == "success"
        per_file.append(calls[0] - before)

    # File 0 triggers hand_on_for_task (agent deny, lead delegate check, agent allow, person allow,
    # then reusable_folder_allowance agent + person) and bumps the store version.
    # File 1 misses the old version, pays decide_folder + reusable_folder_allowance, and caches at the new version.
    # Files 2..N hit the kept folder answer with zero Cedar calls.
    assert per_file[0] == 6
    assert per_file[1] == 4
    assert all(count == 0 for count in per_file[2:])


def test_crawl_beside_blocked_sibling_reuses_folder_answer_and_refuses_blocked_file(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _authorized_agent(layout, "alice")
    crawl_dir = layout.team_dir / "crawl"
    crawl_dir.mkdir(parents=True, exist_ok=True)
    blocked_path = crawl_dir / "blocked.txt"
    blocked_path.write_text("keep-out", encoding="utf-8")

    service = runtime_access(agent).service
    assert service is not None
    service.grant_by_person(
        OWNER,
        _grant(
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
            target=TargetRefV1(kind=TargetKind.FILE, id="team/crawl/blocked.txt"),
            level=AccessLevel.FULL,
            kind=GrantKind.BLOCK,
        ),
    )

    calls = _count_cedar_calls(service, monkeypatch)
    per_file: list[int] = []
    for index in range(12):
        before = calls[0]
        res = file_write(path=str(crawl_dir / f"item_{index}.txt"), content=f"row {index}")
        assert res["status"] == "success"
        per_file.append(calls[0] - before)
        if index == 5:
            # Mid-crawl attempt on the blocked sibling is refused and leaves the folder entry intact.
            refused = file_write(path=str(blocked_path), content="clobber")
            assert (refused["status"], refused["reason_code"]) == ("error", file_access.ACCESS_DENIED)
            assert blocked_path.read_text(encoding="utf-8") == "keep-out"

    assert per_file[0] == 4
    assert all(count == 0 for count in per_file[1:])

    # Listing the folder itself (READ level) stays allowed, warms the READ folder entry with the
    # same blocked-child exclusion, and subsequent listings / sibling reads hit it with zero Cedar calls.
    before_list = calls[0]
    _loc, entries = file_access.list_directory(crawl_dir)
    assert calls[0] - before_list == 4
    assert any(name == "item_0.txt" for name, _stat in entries)

    before_second = calls[0]
    file_access.list_directory(crawl_dir)
    sibling_read = file_read(path=str(crawl_dir / "item_0.txt"))
    assert sibling_read["status"] == "success"
    assert calls[0] - before_second == 0

    blocked_read = file_read(path=str(blocked_path))
    assert (blocked_read["status"], blocked_read["reason_code"]) == ("error", file_access.ACCESS_DENIED)
    assert "keep-out" not in _text(blocked_read)


# -- C02: Equivalence matrix against fresh AccessService.decide_folder -------


@pytest.mark.parametrize(
    "scenario",
    [
        "folder_grant",
        "single_file_read_share",
        "single_file_write_share",
        "folder_block_on_child_file",
        "file_block_on_child",
        "block_on_child_subfolder",
        "block_on_folder",
        "one_time_grant",
        "person_side_block",
    ],
)
def test_folder_access_cache_matches_fresh_decide_folder(
    layout: InstanceLayout, tmp_path: Path, scenario: str
) -> None:
    agent = _authorized_agent(layout, "alice")
    access = runtime_access(agent)
    service = access.service
    assert service is not None
    principal, person, _task_id = task_access(agent, access)
    bob_folder = person_folder_id("bob")
    worker_subject = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent")
    alice_subject = SubjectRefV1(kind=SubjectKind.USER, id="alice")

    level = AccessLevel.READ
    if scenario == "folder_grant":
        _register_data_folder(layout, tmp_path, service, "reports")
        service.grant_by_person(
            OWNER, _grant(subject=alice_subject, target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports"), level=AccessLevel.READ)
        )
        service.grant_by_person(
            OWNER,
            _grant(
                subject=worker_subject,
                target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports"),
                level=AccessLevel.READ,
                lifetime=GrantLifetime.TASK,
                task_id=principal.work_id,
                for_person_id="alice",
            ),
        )
        warm = ("data/reports/a.txt", True)
        probes = [
            ("data/reports/a.txt", True),
            ("data/reports/b.txt", True),
            ("data/reports", False),
            ("data/reports/sub/c.txt", True),
        ]
    elif scenario == "single_file_read_share":
        service.grant_by_person(
            BOB,
            _grant(
                subject=alice_subject,
                target=TargetRefV1(kind=TargetKind.FILE, id=f"{bob_folder}/shared.txt"),
                level=AccessLevel.READ,
                granted_by="bob",
            ),
        )
        warm = (f"{bob_folder}/shared.txt", True)
        probes = [
            (f"{bob_folder}/shared.txt", True),
            (f"{bob_folder}/secret.txt", True),
            (bob_folder, False),
        ]
    elif scenario == "single_file_write_share":
        level = AccessLevel.WRITE
        service.grant_by_person(
            BOB,
            _grant(
                subject=alice_subject,
                target=TargetRefV1(kind=TargetKind.FILE, id=f"{bob_folder}/shared.txt"),
                level=AccessLevel.WRITE,
                granted_by="bob",
            ),
        )
        warm = (f"{bob_folder}/shared.txt", True)
        probes = [
            (f"{bob_folder}/shared.txt", True),
            (f"{bob_folder}/sibling.txt", True),
            (bob_folder, False),
        ]
    elif scenario == "folder_block_on_child_file":
        service.grant_by_person(
            OWNER,
            _grant(
                subject=worker_subject,
                target=TargetRefV1(kind=TargetKind.FOLDER, id="team/work/blocked.txt"),
                level=AccessLevel.FULL,
                kind=GrantKind.BLOCK,
            ),
        )
        warm = ("team/work/allowed.txt", True)
        probes = [
            ("team/work/allowed.txt", True),
            ("team/work/other.txt", True),
            ("team/work/blocked.txt", True),
            ("team/work", False),
        ]
    elif scenario == "file_block_on_child":
        service.grant_by_person(
            OWNER,
            _grant(
                subject=worker_subject,
                target=TargetRefV1(kind=TargetKind.FILE, id="team/work/blocked.txt"),
                level=AccessLevel.FULL,
                kind=GrantKind.BLOCK,
            ),
        )
        warm = ("team/work/allowed.txt", True)
        probes = [
            ("team/work/allowed.txt", True),
            ("team/work/other.txt", True),
            ("team/work/blocked.txt", True),
            ("team/work", False),
        ]
    elif scenario == "block_on_child_subfolder":
        service.grant_by_person(
            OWNER,
            _grant(
                subject=worker_subject,
                target=TargetRefV1(kind=TargetKind.FOLDER, id="team/work/sub"),
                level=AccessLevel.FULL,
                kind=GrantKind.BLOCK,
            ),
        )
        warm = ("team/work/allowed.txt", True)
        probes = [
            ("team/work/allowed.txt", True),
            ("team/work/other.txt", True),
            ("team/work", False),
            ("team/work/sub", False),
            ("team/work/sub/secret.txt", True),
        ]
    elif scenario == "block_on_folder":
        service.grant_by_person(
            OWNER,
            _grant(
                subject=worker_subject,
                target=TargetRefV1(kind=TargetKind.FOLDER, id="team/work"),
                level=AccessLevel.FULL,
                kind=GrantKind.BLOCK,
            ),
        )
        warm = ("team/work/a.txt", True)
        probes = [
            ("team/work/a.txt", True),
            ("team/work/b.txt", True),
            ("team/work", False),
        ]
    elif scenario == "one_time_grant":
        _register_data_folder(layout, tmp_path, service, "once")
        service.grant_by_person(
            OWNER, _grant(subject=alice_subject, target=TargetRefV1(kind=TargetKind.FOLDER, id="data/once"), level=AccessLevel.READ)
        )
        service.grant_by_person(
            OWNER,
            _grant(
                subject=worker_subject,
                target=TargetRefV1(kind=TargetKind.FOLDER, id="data/once"),
                level=AccessLevel.READ,
                lifetime=GrantLifetime.ONE_TIME,
                uses_remaining=1,
                task_id=principal.work_id,
                for_person_id="alice",
            ),
        )
        warm = ("data/once/a.txt", True)
        probes = [
            ("data/once/a.txt", True),
            ("data/once/b.txt", True),
            ("data/once", False),
        ]
    elif scenario == "person_side_block":
        service.grant_by_person(
            OWNER,
            _grant(
                subject=alice_subject,
                target=TargetRefV1(kind=TargetKind.FILE, id="team/work/blocked.txt"),
                level=AccessLevel.FULL,
                kind=GrantKind.BLOCK,
            ),
        )
        warm = ("team/work/allowed.txt", True)
        probes = [
            ("team/work/allowed.txt", True),
            ("team/work/other.txt", True),
            ("team/work/blocked.txt", True),
            ("team/work", False),
        ]
    else:
        raise AssertionError(f"Unknown scenario: {scenario}")

    # Warm the cache with one call.
    warm_decision = folder_access.decide_folder_use(agent, warm[0], is_file=warm[1], level=level)
    assert warm_decision is not None
    if scenario == "one_time_grant":
        assert warm_decision.allowed is True

    # Every probe through the warm cache must match a fresh AccessService.decide_folder verdict.
    for target_id, is_file in probes:
        cached = folder_access.decide_folder_use(agent, target_id, is_file=is_file, level=level)
        fresh = service.decide_folder(
            agent=principal,
            person=person,
            target_id=target_id,
            is_file=is_file,
            level=level,
            task_id=principal.work_id,
            consume=False,
            hand_on_for_task=False,
        )
        assert cached is not None
        assert cached.allowed == fresh.allowed, (
            f"scenario={scenario} target={target_id} is_file={is_file}: "
            f"cached.allowed={cached.allowed} != fresh.allowed={fresh.allowed}"
        )


# -- C05b: Block added and removed while cache is warm -----------------------


def test_block_added_and_removed_while_cache_is_warm(layout: InstanceLayout) -> None:
    agent = _authorized_agent(layout, "alice")
    service = runtime_access(agent).service
    assert service is not None

    # Warm the cache on team/work/a.txt -> caches folder team/work.
    first = folder_access.decide_folder_use(agent, "team/work/a.txt", is_file=True, level=AccessLevel.READ)
    assert first is not None and first.allowed

    # Add a block on team/work/b.txt within the 10-second window.
    block = service.grant_by_person(
        OWNER,
        _grant(
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
            target=TargetRefV1(kind=TargetKind.FILE, id="team/work/b.txt"),
            level=AccessLevel.FULL,
            kind=GrantKind.BLOCK,
        ),
    )

    refused = folder_access.decide_folder_use(agent, "team/work/b.txt", is_file=True, level=AccessLevel.READ)
    assert refused is not None and not refused.allowed

    # Sibling still succeeds and warms the folder entry with b.txt excluded.
    sibling = folder_access.decide_folder_use(agent, "team/work/a.txt", is_file=True, level=AccessLevel.READ)
    assert sibling is not None and sibling.allowed
    still_refused = folder_access.decide_folder_use(agent, "team/work/b.txt", is_file=True, level=AccessLevel.READ)
    assert still_refused is not None and not still_refused.allowed

    # Remove the block within the 10-second window -> b.txt is immediately allowed again.
    service.revoke(block.grant_id, actor=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id))
    allowed_again = folder_access.decide_folder_use(agent, "team/work/b.txt", is_file=True, level=AccessLevel.READ)
    assert allowed_again is not None and allowed_again.allowed


# -- Unit check for reusable_folder_allowance fail-closed edge cases ---------


def test_reusable_folder_allowance_fails_closed_on_invalid_ids(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    agent = _authorized_agent(layout, "alice")
    access = runtime_access(agent)
    service = access.service
    assert service is not None
    principal, person, _task_id = task_access(agent, access)

    assert (
        service.reusable_folder_allowance(
            agent=principal,
            person=person,
            folder_id="../outside",
            level=AccessLevel.READ,
            task_id=principal.work_id,
        )
        is None
    )

    bad_block = _grant(
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
        target=TargetRefV1(kind=TargetKind.FOLDER, id="../escape"),
        level=AccessLevel.FULL,
        kind=GrantKind.BLOCK,
    )
    original_principal_grants = service._principal_grants
    monkeypatch.setattr(
        service,
        "_principal_grants",
        lambda *args, **kwargs: [*original_principal_grants(*args, **kwargs), bad_block],
    )
    with caplog.at_level(logging.WARNING, logger="access_control.decisions"):
        assert (
            service.reusable_folder_allowance(
                agent=principal,
                person=person,
                folder_id="team/work",
                level=AccessLevel.READ,
                task_id=principal.work_id,
            )
            is None
        )
    assert "event=folder_allowance_invalid_block_target" in caplog.text
