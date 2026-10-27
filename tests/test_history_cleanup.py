"""TeamLead cleans up a person's own chat history (custom_agents.history_cleanup).

Real Cedar, the SQLite access store and the manager's real conversation and
task stores. Covers: choosing which turns and tasks a clean-up deletes; the
staged plan and its review card (conversation titles while waiting, only ids,
dates and counts once decided); without the person's standing permission
``apply_change`` waits for the person in the Inbox, with it the clean-up
applies at once and records the grant used; what the manager deletes for a
whole and a partly deleted conversation (turns, summaries, tags, saved tool
outputs and their files, memory-review jobs, finished tasks and the tasks
delegated from them) and what it keeps (the conversation asked in, newer
turns, other people's records); a plan whose history changed is refused;
running the manager's part again deletes nothing more; another person's
conversation, the conversation being answered in, workers and work for no
person are refused; with authorization off the person's agreement applies
it; and a follow-up to a deleted conversation starts it afresh.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Runtime composition installs the cycle-free catalog lookup the services use.
from custom_agents import agent_catalog as _agent_catalog  # noqa: F401
from access_control.entities import agent_type_id
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import access_runtime
from custom_agents.conversation_runtime import ensure_task_conversation_context
from custom_agents.history_cleanup import (
    HISTORY_CLEANUP_TOOL_ID,
    HistoryCleanupRefused,
    finish_unfinished_history_cleanups,
    followup_conversation_deleted,
    stage_history_cleanup,
)
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestStatus,
    ChangeApprovalMode,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from storage.access_repository import AccessRepository
from storage.conversation_repository import create_conversation_repository
from storage.history_cleanup_repository import (
    ConversationSelectionV1,
    HistoryCleanupRepository,
    OrderStatus,
    TaskRow,
    TurnRow,
    select_conversation_turns,
    task_ids_to_delete,
)
from storage.task_archive_repository import create_task_archive_repository
from tests.change_approval_support import (
    ToolCall,
    change_agent,
    decide,
    needs_cedar,
    owner,
    run_apply_change,
    stored_request,
    use_signed_contexts,
)

TEAM = "team_history_cleanup"
LEAD = "TeamArchitect"
OTHER = "person_other"
NOW = datetime.now(timezone.utc)
CURRENT = "conversation_change"  # the conversation the test task is answering in (SignedTaskManager)


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    access_runtime._runtime_access.cache_clear()


class _NoPause(ToolCall):
    def interrupt(self, name: str, reason: Any = None) -> Any:
        raise AssertionError(f"unexpected pause for approval: {reason}")


def _days(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def _agent(tmp_path: Path, *, authorization_enabled: bool = True, is_team_lead: bool = True) -> SimpleNamespace:
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="history-cleanup-test", instance_root=str(root), team_id=TEAM, timezone="UTC"
    )
    path = config.save(root / "instance_config.yaml")
    agent = change_agent(config, path, authorization_enabled=authorization_enabled, is_team_lead=is_team_lead)
    agent.task_manager.operations_runtime = SimpleNamespace(run_sync=lambda op, *args, **kwargs: op(*args, **kwargs))
    agent._conversation_store = create_conversation_repository(
        config.manager_storage_settings, artifacts_dir=config.layout.conversation_artifacts_dir
    )
    agent.storage = config.manager_storage_settings
    return agent


def _person(agent: SimpleNamespace) -> str:
    return agent.task_manager.person_id


def _sql(agent: SimpleNamespace, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with sqlite3.connect(agent.storage.sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        conn.commit()
    return rows


def _seed(agent: SimpleNamespace, person: str, conversation_id: str, title: str, ages: list[float]) -> list[str]:
    """One conversation of ``person`` with a turn per age (days ago), each with a saved output and a delegated task."""

    repo = agent._conversation_store
    archive = create_task_archive_repository(agent.storage)
    repo.ensure_thread(
        conversation_id=conversation_id,
        team_id=agent.team_id,
        owner_agent_id=agent.id,
        owner_agent_name=LEAD,
        requester_name=None,
        requester_id=None,
        root_task_id=f"{conversation_id}_t1",
        subject_preview=title,
        owner_canonical_user_id=person,
    )
    task_ids = []
    for index, age in enumerate(ages, start=1):
        task_id = f"{conversation_id}_t{index}"
        task_ids.append(task_id)
        repo.append_turn(
            conversation_id=conversation_id,
            task_id=task_id,
            followup_to_task_id=None,
            owner_agent_id=agent.id,
            requester_name=None,
            requester_id=None,
            correlation_id=None,
            problem_text=f"{title} question {index}",
            owner_canonical_user_id=person,
        )
        repo.update_turn_completion(
            task_id=task_id, task_status="completed", final_answer_text=f"answer {index}", turn_summary_text="summary"
        )
        artifact = repo.offload_large_text(
            conversation_id=conversation_id, task_id=task_id, artifact_type="tool_output", raw_text="output " * 40,
            summary_text="tool output",
        )
        for archived, parent in ((task_id, None), (f"{task_id}_sub", task_id)):
            archive.save_completed_task(
                archive_id=f"archive_{archived}",
                task_id=archived,
                owner_agent_id=agent.id,
                owner_agent_name=LEAD,
                team_id=agent.team_id,
                task_json=json.dumps({"task_id": archived, "parent_task_id": parent}),
                status="completed",
                owner_canonical_user_id=person,
            )
        when = _days(age)
        _sql(agent, "UPDATE conversation_task_links SET created_at=?, updated_at=? WHERE task_id=?", (when, when, task_id))
        _sql(agent, "UPDATE completed_tasks SET timestamp=? WHERE task_id IN (?, ?)", (when, task_id, f"{task_id}_sub"))
        _sql(agent, "UPDATE conversation_artifacts SET created_at=? WHERE artifact_id=?", (when, artifact["artifact_id"]))
    repo.create_context_epoch(
        conversation_id=conversation_id,
        trigger="test",
        through_turn_index=len(ages),
        preserved_suffix_start_turn=len(ages) + 1,
        summary_markdown=f"{title} summary",
    )
    repo.update_thread_compaction(conversation_id, synopsis_markdown=f"{title} synopsis", summarized_through_turn_index=len(ages))
    return task_ids


def _stage(agent: SimpleNamespace, **kwargs: Any) -> dict[str, Any]:
    from custom_tools.runtime_context import set_agent_ref

    set_agent_ref(agent)
    return stage_history_cleanup(agent, **kwargs)


def _give_standing_permission(agent: SimpleNamespace) -> GrantV1:
    service = access_runtime.runtime_requests(agent).service
    person = owner(agent)
    return service.grant_by_person(
        person,
        GrantV1(
            grant_id="new",
            team_id=agent.team_id,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(agent.team_id, LEAD)),
            target=TargetRefV1(kind=TargetKind.TOOL, id=HISTORY_CLEANUP_TOOL_ID),
            level=AccessLevel.FULL,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=person.person_id),
            created_at_utc=NOW,
            for_person_id=person.person_id,
        ),
    )


def _turn_ids(agent: SimpleNamespace, conversation_id: str) -> list[str]:
    return [row["task_id"] for row in _sql(agent, "SELECT task_id FROM conversation_task_links WHERE conversation_id=? ORDER BY turn_index", (conversation_id,))]


def _task_ids(agent: SimpleNamespace) -> set[str]:
    return {row["task_id"] for row in _sql(agent, "SELECT task_id FROM completed_tasks")}


# -- choosing what to delete ---------------------------------------------------------------


def _turn(conversation_id: str, index: int, age: float, status: str = "completed") -> TurnRow:
    return TurnRow(conversation_id, "title", f"{conversation_id}_{index}", index, status, _days(age), _days(age), 10)


def test_selection_deletes_the_oldest_turns_and_keeps_newer_ones_running_and_excluded() -> None:
    turns = [
        _turn("old", 1, 400), _turn("old", 2, 380),
        _turn("mixed", 1, 300), _turn("mixed", 2, 10),
        _turn("new", 1, 5),
        _turn("busy", 1, 400), _turn("busy", 2, 0.1, status="in_progress"),
        _turn("asked_in", 1, 400),
    ]
    selections, left_out = select_conversation_turns(
        turns, limit_at=_days(100), selected_at=NOW.isoformat(), excluded=["asked_in"], active_since=_days(1)
    )
    by_id = {item.conversation_id: item for item in selections}
    assert set(by_id) == {"old", "mixed"}
    assert by_id["old"].whole and by_id["old"].through_turn_index == 2 and by_id["old"].latest_task_id == "old_2"
    assert not by_id["mixed"].whole and by_id["mixed"].through_turn_index == 1
    assert by_id["mixed"].keep_from == _days(10)
    assert left_out == ["asked_in", "busy"]


def test_tasks_to_delete_follow_delegation_and_skip_left_out_work() -> None:
    rows = [
        TaskRow("root", None, _days(200), 5), TaskRow("child", "root", _days(200), 5),
        TaskRow("grandchild", "child", _days(200), 5), TaskRow("kept_root", None, _days(200), 5),
        TaskRow("kept_child", "kept_root", _days(200), 5), TaskRow("recent", None, _days(1), 5),
    ]
    by_age = task_ids_to_delete(rows, limit_at=_days(100), roots=None, excluded_roots=["kept_root"])
    assert {row.task_id for row in by_age} == {"root", "child", "grandchild"}
    named = task_ids_to_delete(rows, limit_at=NOW.isoformat(), roots=["root"])
    assert {row.task_id for row in named} == {"root", "child", "grandchild"}


# -- staging, review and applying -----------------------------------------------------------


@needs_cedar
def test_without_permission_the_person_reviews_titles_which_the_decided_request_drops(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    person = _person(agent)
    _seed(agent, person, "conv_trip", "Planning the spring trip to the coast", [200, 190])
    staged = _stage(agent, what="both", conversation_ids=["conv_trip"])
    assert staged["status"] == "staged"
    assert any("Planning the spring trip to the coast" in line for line in staged["effect"])

    call = ToolCall()
    with pytest.raises(Exception):
        run_apply_change(agent, staged["change_id"], call)
    waiting = stored_request(agent, call)
    assert waiting.status is AccessRequestStatus.WAITING
    assert waiting.change is not None and waiting.change.owner_person_id == person
    assert any("Planning the spring trip" in line for line in waiting.change.effect)
    assert "Planning the spring trip" not in waiting.change.summary
    assert _turn_ids(agent, "conv_trip"), "nothing is deleted before the person decides"

    decide(agent, call)
    decided = stored_request(agent, call)
    assert decided.change is not None
    assert not any("Planning the spring trip" in line for line in decided.change.effect)
    assert any("Conversation conv_trip" in line for line in decided.change.effect)

    applied = run_apply_change(agent, staged["change_id"], call)
    assert applied["status"] == "applied"
    assert applied["approval"]["mode"] == ChangeApprovalMode.PERSON_APPROVAL.value
    assert _turn_ids(agent, "conv_trip") == []
    assert not {"conv_trip_t1", "conv_trip_t1_sub", "conv_trip_t2", "conv_trip_t2_sub"} & _task_ids(agent)
    order = HistoryCleanupRepository(agent.storage).get(staged["change_id"])
    assert order is not None and order.status is OrderStatus.WAITING_FOR_GATEWAY
    assert order.approval["grant_id"] is None


@needs_cedar
def test_with_the_standing_permission_it_applies_at_once_and_logs_the_grant(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    person = _person(agent)
    _seed(agent, person, "conv_old", "Old budget questions", [400])
    grant = _give_standing_permission(agent)
    staged = _stage(agent, what="conversations", older_than=_days(30)[:10])
    applied = run_apply_change(agent, staged["change_id"], _NoPause())
    assert applied["approval"]["mode"] == ChangeApprovalMode.HELD_ACCESS.value
    order = HistoryCleanupRepository(agent.storage).get(staged["change_id"])
    assert order is not None and order.approval["grant_id"] == grant.grant_id
    events = [event for event in AccessRepository(agent.storage).events(agent.team_id) if event.kind == "history_cleanup"]
    assert len(events) == 1 and events[0].detail["side"] == "manager" and events[0].detail["person_id"] == person
    assert "Old budget questions" not in json.dumps(events[0].detail)


@needs_cedar
def test_an_age_cleanup_keeps_newer_turns_the_conversation_asked_in_and_other_people(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    person = _person(agent)
    mixed = _seed(agent, person, "conv_mixed", "Garden plans", [300, 200, 5])
    _seed(agent, person, CURRENT, "This conversation", [400])
    _seed(agent, OTHER, "conv_other", "Someone else's chat", [400])
    files_before = sorted(row["storage_path"] for row in _sql(agent, "SELECT storage_path FROM conversation_artifacts WHERE conversation_id='conv_mixed'"))
    _give_standing_permission(agent)
    staged = _stage(agent, what="both", older_than=_days(100)[:10])
    assert staged["left_out_conversations"] == [CURRENT]
    run_apply_change(agent, staged["change_id"], _NoPause())

    assert _turn_ids(agent, "conv_mixed") == [mixed[2]]
    thread = _sql(agent, "SELECT * FROM conversation_threads WHERE conversation_id='conv_mixed'")[0]
    assert thread["synopsis_markdown"] is None and thread["summarized_through_turn_index"] == 0
    assert thread["subject_preview"] == "Garden plans question 3"
    assert _sql(agent, "SELECT * FROM conversation_context_epochs WHERE conversation_id='conv_mixed'") == []
    kept_files = [row["storage_path"] for row in _sql(agent, "SELECT storage_path FROM conversation_artifacts WHERE conversation_id='conv_mixed'")]
    assert len(kept_files) == 1
    assert [Path(path).exists() for path in files_before] == [path in kept_files for path in files_before]
    tasks = _task_ids(agent)
    assert not {f"{mixed[0]}", f"{mixed[0]}_sub", mixed[1], f"{mixed[1]}_sub"} & tasks
    assert {mixed[2], f"{mixed[2]}_sub", f"{CURRENT}_t1", "conv_other_t1", "conv_other_t1_sub"} <= tasks
    assert _turn_ids(agent, CURRENT) == [f"{CURRENT}_t1"]
    assert _turn_ids(agent, "conv_other") == ["conv_other_t1"]


@needs_cedar
def test_a_plan_whose_history_changed_is_refused(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    _seed(agent, _person(agent), "conv_named", "Holiday", [50])
    staged = _stage(agent, what="conversations", conversation_ids=["conv_named"])
    agent._conversation_store.append_turn(
        conversation_id="conv_named", task_id="conv_named_new", followup_to_task_id=None, owner_agent_id=agent.id,
        requester_name=None, requester_id=None, correlation_id=None, problem_text="one more thing",
        task_status="completed", owner_canonical_user_id=_person(agent),
    )
    _sql(agent, "UPDATE conversation_task_links SET created_at=? WHERE task_id='conv_named_new'", (_days(40),))
    refused = run_apply_change(agent, staged["change_id"], _NoPause())
    assert refused["status"] == "refused" and "changed after it was staged" in refused["reason"]
    assert len(_turn_ids(agent, "conv_named")) == 2


@needs_cedar
def test_running_the_manager_part_again_deletes_nothing_more(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    _seed(agent, _person(agent), "conv_once", "Once", [400])
    _give_standing_permission(agent)
    staged = _stage(agent, what="both", older_than=_days(30)[:10])
    run_apply_change(agent, staged["change_id"], _NoPause())
    store = HistoryCleanupRepository(agent.storage)
    _sql(agent, f"UPDATE history_cleanup_orders SET status='{OrderStatus.DELETING.value}' WHERE order_id=?", (staged["change_id"],))
    _seed(agent, _person(agent), "conv_later", "Later", [1])
    assert finish_unfinished_history_cleanups(agent) == 1
    assert store.get(staged["change_id"]).status is OrderStatus.WAITING_FOR_GATEWAY
    assert _turn_ids(agent, "conv_later") == ["conv_later_t1"]
    assert {"conv_later_t1", "conv_later_t1_sub"} <= _task_ids(agent)


@needs_cedar
def test_someone_elses_or_the_current_conversation_or_nothing_named_is_refused(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    _seed(agent, OTHER, "conv_other", "Not yours", [100])
    _seed(agent, _person(agent), CURRENT, "Asked here", [1])
    with pytest.raises(HistoryCleanupRefused, match="no conversation conv_other"):
        _stage(agent, what="conversations", conversation_ids=["conv_other"])
    with pytest.raises(HistoryCleanupRefused, match="while TeamLead is answering"):
        _stage(agent, what="conversations", conversation_ids=[CURRENT])
    with pytest.raises(HistoryCleanupRefused, match="Say what to delete"):
        _stage(agent, what="both")
    with pytest.raises(HistoryCleanupRefused, match="cannot be in the future"):
        _stage(agent, what="both", older_than=(NOW + timedelta(days=2)).date().isoformat())
    assert _turn_ids(agent, "conv_other") == ["conv_other_t1"]


@needs_cedar
def test_workers_never_stage_a_cleanup(tmp_path: Path) -> None:
    agent = _agent(tmp_path, is_team_lead=False)
    with pytest.raises(HistoryCleanupRefused, match="workers never do"):
        _stage(agent, what="both", older_than=_days(30)[:10])


def test_with_authorization_off_the_persons_agreement_applies_it(tmp_path: Path) -> None:
    agent = _agent(tmp_path, authorization_enabled=False)
    _seed(agent, _person(agent), "conv_off", "Off", [400])
    staged = _stage(agent, what="conversations", older_than=_days(30)[:10])
    unconfirmed = run_apply_change(agent, staged["change_id"])
    assert unconfirmed["status"] == "agreement_required"
    applied = run_apply_change(agent, staged["change_id"], confirmed=True)
    assert applied["status"] == "applied"
    assert applied["approval"]["mode"] == ChangeApprovalMode.AGREEMENT.value
    assert _turn_ids(agent, "conv_off") == []


# -- after the deletion -------------------------------------------------------------------


@needs_cedar
def test_a_followup_to_a_deleted_conversation_starts_it_afresh(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    person = _person(agent)
    tasks = _seed(agent, person, "conv_gone", "Gone", [400])
    _give_standing_permission(agent)
    staged = _stage(agent, what="conversations", conversation_ids=["conv_gone"])
    run_apply_change(agent, staged["change_id"], _NoPause())
    assert followup_conversation_deleted(agent, person, "conv_gone")
    assert not followup_conversation_deleted(agent, OTHER, "conv_gone")

    def admit(deleted_by_cleanup: Any) -> MessageMeta:
        return ensure_task_conversation_context(
            conversation_store=agent._conversation_store,
            team_id=agent.team_id,
            owner_agent_id=agent.id,
            owner_agent_name=LEAD,
            task_request=Task(task_id="task_next", task_type=MessageMetaType.REQUEST, problem_statement="Next question"),
            msg_header=MessageHeader(from_agent_id="gateway"),
            msg_meta=MessageMeta(conversation_id="conv_gone", followup_to_task_id=tasks[-1]),
            owner_person_id=person,
            deleted_by_cleanup=deleted_by_cleanup,
        )

    with pytest.raises(ValueError, match="does not exist in the conversation store"):
        admit(None)
    meta = admit(lambda who, conversation: followup_conversation_deleted(agent, who, conversation))
    assert meta.conversation_id == "conv_gone" and meta.followup_to_task_id is None
    assert _turn_ids(agent, "conv_gone")[-1] == "task_next"


def test_conversation_selections_compare_by_value() -> None:
    """The stale check compares freshly computed selections with the stored ones."""

    one = ConversationSelectionV1(conversation_id="c", through_turn_index=1, whole=True, turns=1, first_at="a", last_at="a")
    assert one == ConversationSelectionV1.model_validate_json(one.model_dump_json())


@needs_cedar
@pytest.mark.parametrize(
    ("management", "schedule_owner", "allowed"),
    [("user", "self", True), ("system", "self", False), ("user", OTHER, False)],
)
def test_a_scheduled_run_cleans_up_only_from_a_schedule_the_person_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, management: str, schedule_owner: str, allowed: bool
) -> None:
    from custom_messages.enums import ScheduleManagementClass

    agent = _agent(tmp_path)
    person = _person(agent)
    _seed(agent, person, "conv_sched", "Scheduled", [400])
    signed = access_runtime.verified_task_context(agent)
    assert signed is not None
    monkeypatch.setattr(
        access_runtime, "verified_task_context", lambda _agent, _access=None: signed.model_copy(update={"schedule_id": "sched_1"})
    )
    schedule = SimpleNamespace(
        management_class=ScheduleManagementClass(management),
        payload_meta=SimpleNamespace(
            gateway_context=SimpleNamespace(canonical_user_id=person if schedule_owner == "self" else schedule_owner)
        ),
    )
    agent._ensure_schedule_store = lambda: SimpleNamespace(get_scheduled_message=lambda _schedule_id: schedule)
    if allowed:
        assert _stage(agent, what="both", older_than=_days(30)[:10])["status"] == "staged"
    else:
        with pytest.raises(HistoryCleanupRefused, match="only from a schedule the person made themselves"):
            _stage(agent, what="both", older_than=_days(30)[:10])
