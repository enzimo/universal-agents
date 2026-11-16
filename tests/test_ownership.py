"""Who may see or change a person's private records (access_control.ownership)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from access_control.ownership import (
    PRIVATE_CREATE,
    PRIVATE_DELETE,
    PRIVATE_READ,
    PRIVATE_UPDATE,
    RESOURCE_CONVERSATION,
    RESOURCE_FILE_PATH,
    RESOURCE_SCHEDULE,
    RESOURCE_TEAM_MEMORY,
    RESOURCE_USER_MEMORY,
    WORKSPACE_FILE_READ,
    answer_context_id,
    conversation_continuation,
    conversation_files_access,
    escalation_result_files_access,
    named_task_access,
    objective_access,
    private_access,
    request_context_id,
    request_files_access,
    sent_files_access,
    steering_context_id,
)
from custom_messages.contracts.access_v2 import MESSAGE_CONTEXT_ID_PREFIX, TaskContextV1
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.task_v1 import Task, TaskQuery, TaskSteering
from custom_messages.enums import MessageMetaType, TaskSteeringType


@pytest.mark.parametrize("action", [PRIVATE_READ, PRIVATE_UPDATE, PRIVATE_DELETE])
def test_only_the_owner_may_act_on_a_private_record(action: str) -> None:
    mine = private_access(person_id="usr_a", owner_id="usr_a", resource_type=RESOURCE_SCHEDULE, action=action)
    theirs = private_access(person_id="usr_b", owner_id="usr_a", resource_type=RESOURCE_SCHEDULE, action=action)
    nobody = private_access(person_id=None, owner_id="usr_a", resource_type=RESOURCE_SCHEDULE, action=action)
    unowned = private_access(person_id="usr_a", owner_id=None, resource_type=RESOURCE_SCHEDULE, action=action)

    assert (mine.allowed, mine.reason) == (True, "resource_owner")
    assert [d.allowed for d in (theirs, nobody, unowned)] == [False, False, False]
    assert theirs.reason == "resource_owner_required"


def test_administrators_have_no_special_access_to_someone_elses_records() -> None:
    decision = private_access(
        person_id="usr_admin",
        owner_id="usr_a",
        resource_type=RESOURCE_CONVERSATION,
        team_id="team_1",
        roles=("admin",),
    )

    assert decision.allowed is False


def test_only_memory_records_are_created_through_the_check() -> None:
    conversation = private_access(
        person_id="usr_a", owner_id="usr_a", resource_type=RESOURCE_CONVERSATION, action=PRIVATE_CREATE
    )
    memory = private_access(person_id="usr_a", owner_id="usr_a", resource_type=RESOURCE_USER_MEMORY, action=PRIVATE_CREATE)

    assert (conversation.allowed, conversation.reason) == (False, "private_create_resource_unsupported")
    assert memory.allowed is True


def test_team_memory_is_read_by_members_and_managed_by_administrators() -> None:
    def team_memory(person: str, roles: tuple[str, ...], action: str):
        return private_access(
            person_id=person,
            owner_id=None,
            resource_type=RESOURCE_TEAM_MEMORY,
            resource_id="coremem:team_1",
            action=action,
            team_id="team_1",
            roles=roles,
        )

    assert team_memory("usr_member", ("member",), PRIVATE_READ).reason == "same_team_member"
    member_update = team_memory("usr_member", ("member",), PRIVATE_UPDATE)
    assert (member_update.allowed, member_update.reason) == (False, "team_admin_required")
    assert all(
        team_memory("usr_admin", ("admin",), action).allowed
        for action in (PRIVATE_CREATE, PRIVATE_UPDATE, PRIVATE_DELETE)
    )
    outsider = team_memory("usr_unknown", (), PRIVATE_READ)
    assert (outsider.allowed, outsider.reason) == (False, "same_team_member_required")


def test_members_browse_their_own_folder_and_administrators_also_the_shared_ones() -> None:
    def browse(path: str, roles: tuple[str, ...], person: str = "usr_x"):
        return private_access(
            person_id=person,
            owner_id=None,
            resource_type=RESOURCE_FILE_PATH,
            resource_id=f"workspace/{path}",
            action=WORKSPACE_FILE_READ,
            team_id="team_1",
            roles=roles,
        )

    assert browse("users/usr_x/notes.md", ("member",)).reason == "own_workspace_folder"
    assert browse("users", ("member",)).allowed and browse(".", ("member",)).allowed
    assert (browse("team/plan.md", ("member",)).allowed, browse("team/plan.md", ("member",)).reason) == (
        False,
        "own_folder_only",
    )
    assert not browse("users/usr_y/diary.md", ("member",)).allowed
    assert browse("team/plan.md", ("admin",)).reason == "team_admin_workspace_read"
    assert not browse("users/usr_y/diary.md", ("admin",)).allowed, "administrators cannot open other people's folders"
    assert browse("users/usr_x/notes.md", ()).reason == "team_member_required"


def test_unknown_actions_are_refused() -> None:
    decision = private_access(person_id="usr_a", owner_id="usr_a", resource_type=RESOURCE_SCHEDULE, action="schedule.update")

    assert (decision.allowed, decision.reason) == (False, "unknown_private_action")


@pytest.mark.parametrize("action", [PRIVATE_READ, PRIVATE_CREATE, PRIVATE_UPDATE])
def test_every_member_reads_and_edits_team_knowledge(action: str) -> None:
    from access_control.ownership import RESOURCE_KNOWLEDGE_TEAM_PAGE

    member = private_access(
        person_id="usr_m", owner_id=None, resource_type=RESOURCE_KNOWLEDGE_TEAM_PAGE, team_id="team_1",
        roles=("member",), action=action,
    )
    outsider = private_access(
        person_id="usr_x", owner_id=None, resource_type=RESOURCE_KNOWLEDGE_TEAM_PAGE, action=action,
    )
    assert member.allowed and not outsider.allowed


def test_nobody_deletes_team_knowledge() -> None:
    from access_control.ownership import RESOURCE_KNOWLEDGE_TEAM_PAGE

    decision = private_access(
        person_id="usr_o", owner_id=None, resource_type=RESOURCE_KNOWLEDGE_TEAM_PAGE, team_id="team_1",
        roles=("owner",), action=PRIVATE_DELETE,
    )
    assert (decision.allowed, decision.reason) == (False, "team_knowledge_never_deleted")


@pytest.mark.parametrize("action", [PRIVATE_READ, PRIVATE_UPDATE])
def test_personal_knowledge_and_contributions_are_their_owners_alone(action: str) -> None:
    from access_control.ownership import RESOURCE_KNOWLEDGE_CONTRIBUTION, RESOURCE_KNOWLEDGE_PERSONAL_PAGE

    for resource in (RESOURCE_KNOWLEDGE_PERSONAL_PAGE, RESOURCE_KNOWLEDGE_CONTRIBUTION):
        mine = private_access(person_id="usr_a", owner_id="usr_a", resource_type=resource, action=action)
        admin = private_access(
            person_id="usr_admin", owner_id="usr_a", resource_type=resource, team_id="team_1", roles=("admin",),
            action=action,
        )
        assert mine.allowed and not admin.allowed


def test_a_task_sees_its_persons_objectives_and_the_teams_never_anothers() -> None:
    def objective(person: str | None, owner: str | None, action: str, roles: tuple[str, ...] = ("member",)):
        return objective_access(person_id=person, owner_id=owner, objective_id="objective_1", action=action, roles=roles)

    for action in (PRIVATE_READ, PRIVATE_UPDATE, PRIVATE_DELETE):
        assert objective("usr_a", "usr_a", action).reason == "resource_owner"
        theirs = objective("usr_a", "usr_b", action)
        assert (theirs.allowed, theirs.reason) == (False, "resource_owner_required")
        # Administrators and work for no person get no view of a person's Objectives.
        assert objective("usr_admin", "usr_b", action, roles=("admin",)).allowed is False
        assert objective(None, "usr_b", action, roles=()).allowed is False

    # Every task reads the team's Objectives (no owner, also an empty owner).
    assert objective("usr_a", None, PRIVATE_READ).reason == "team_objective"
    assert objective("usr_a", "  ", PRIVATE_READ).reason == "team_objective"
    assert objective(None, None, PRIVATE_READ, roles=()).reason == "team_objective"


def test_team_objectives_change_only_in_work_for_no_person_or_an_administrator() -> None:
    def change(person: str | None, roles: tuple[str, ...]):
        return objective_access(
            person_id=person, owner_id=None, objective_id="objective_team", action=PRIVATE_UPDATE, roles=roles
        )

    member = change("usr_member", ("member",))
    assert (member.allowed, member.reason) == (False, "team_objective_admin_required")
    assert change("usr_admin", ("admin",)).reason == "team_objective_admin"
    assert change("usr_owner", ("owner",)).reason == "team_objective_admin"  # an owner is an administrator
    assert change(None, ()).reason == "team_objective_system_work"
    unknown = objective_access(person_id="usr_a", owner_id="usr_a", objective_id="objective_1", action="private.share")
    assert (unknown.allowed, unknown.reason) == (False, "unknown_private_action")


def test_a_task_names_only_tasks_that_work_for_the_same_person() -> None:
    assert named_task_access(person_id="usr_a", owner_id="usr_a", task_id="task_1").reason == "same_person_task"
    assert named_task_access(person_id=None, owner_id=None, task_id="task_1").reason == "no_person_task"
    for person, owner in (("usr_a", "usr_b"), ("usr_a", None), (None, "usr_a")):
        refused = named_task_access(person_id=person, owner_id=owner, task_id="task_1")
        assert (refused.allowed, refused.reason) == (False, "same_person_task_required")


@pytest.mark.parametrize(
    ("person", "owner", "allowed"),
    [
        ("usr_a", "usr_a", True),
        (None, None, True),
        (" ", None, True),  # blank is no person
        ("usr_b", "usr_a", False),
        (None, "usr_a", False),
        ("usr_a", None, False),
    ],
)
def test_a_conversations_files_follow_the_same_rule_as_continuing_it(person, owner, allowed) -> None:
    files = conversation_files_access(person_id=person, owner_id=owner, conversation_id="c1")
    continuation = conversation_continuation(person_id=person, owner_id=owner, conversation_id="c1")
    assert (files.allowed, files.reason) == (continuation.allowed, continuation.reason)
    assert files.allowed is allowed


@pytest.mark.parametrize(
    ("person", "request_person", "reason"),
    [
        ("usr_a", "usr_a", "same_person_request"),
        (None, None, "no_person_request"),
        (" ", None, "no_person_request"),  # blank is no person
        ("usr_b", "usr_a", "same_person_request_required"),
        (None, "usr_a", "same_person_request_required"),
        ("usr_a", None, "same_person_request_required"),
    ],
)
def test_work_for_a_request_shares_its_files_only_when_it_works_for_the_same_person(
    person, request_person, reason, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO", logger="access_control.ownership"):
        decision = request_files_access(person_id=person, request_person_id=request_person, request_task_id="task_req")

    assert (decision.allowed, decision.reason) == (reason != "same_person_request_required", reason)
    refusals = [record.getMessage() for record in caplog.records if "event=private_access_refused" in record.getMessage()]
    if decision.allowed:
        assert refusals == []
    else:
        [refused] = refusals
        assert "reason=same_person_request_required" in refused and "resource_id=task_req" in refused


@pytest.mark.parametrize(("person", "allowed"), [("usr_a", True), (None, False), ("  ", False)])
def test_only_work_for_a_person_keeps_the_files_its_escalations_answers_brought(
    person, allowed, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO", logger="access_control.ownership"):
        decision = escalation_result_files_access(person_id=person, task_id="task_run")

    assert decision.allowed is allowed
    assert decision.reason == ("work_for_a_person" if allowed else "work_for_a_person_required")
    refused = [record.getMessage() for record in caplog.records if "event=private_access_refused" in record.getMessage()]
    assert bool(refused) is not allowed
    assert all("resource_id=task_run" in message for message in refused)


def _file(attachment_id: str = "att_1", path: str = "/uploads/web/att_1.png") -> UserTaskAttachment:
    return UserTaskAttachment(
        attachment_id=attachment_id,
        filename="page.png",
        content_type="image/png",
        kind="image",
        size_bytes=3,
        sha256="a" * 64,
        created_at="2026-11-12T00:00:00+00:00",
        storage_refs=[AttachmentStorageRef(storage_type="local", path=path)],
    )


def _guidance(key: str = "corr_guidance", files: list[UserTaskAttachment] | None = None) -> TaskSteering:
    return TaskSteering(
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        steering_id="task_steer_id_1",
        idempotency_key=key,
        attachments=[_file()] if files is None else files,
    )


def _request(files: list[UserTaskAttachment] | None = None, task_id: str = "task_1") -> Task:
    return Task(
        task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="Read it.", attachments=files or []
    )


GUIDANCE_CONTEXT_ID = steering_context_id(_guidance())


def _sent(**changes) -> TaskContextV1:
    """The context the gateway signed for ``_guidance()`` to task_1 in c1, for usr_a."""

    now = datetime.now(timezone.utc)
    fields = {
        "context_id": GUIDANCE_CONTEXT_ID,
        "issuer": "gateway",
        "team_id": "team",
        "person_id": "usr_a",
        "root_task_id": "task_1",
        "conversation_id": "c1",
        "issued_at_utc": now,
        "expires_at_utc": now + timedelta(days=1),
    }
    return TaskContextV1(**{**fields, **changes})


@pytest.mark.parametrize(
    ("sender", "reason"),
    [
        (_sent(), "sent_by_gateway_for_task"),
        (None, "gateway_context_required"),
        (_sent(issuer="scheduler"), "sender_not_gateway"),
        (_sent(person_id="usr_b"), "another_person"),
        # As the gateway signed a request before the update: no task named.
        (_sent(root_task_id=None), "context_names_no_task"),
        (_sent(root_task_id="task_2"), "another_task"),
        (_sent(conversation_id=None), "another_conversation"),
        (_sent(conversation_id="c2"), "another_conversation"),
        # The request's own context: every worker of the task holds a copy.
        (_sent(context_id=request_context_id(_request())), "context_not_for_message"),
        # A random id, as for contexts not bound to a message.
        (_sent(context_id="task_context_other"), "context_not_for_message"),
    ],
)
def test_a_messages_files_are_kept_only_when_the_gateway_sent_them_for_the_task(sender, reason) -> None:
    decision = sent_files_access(
        sender=sender,
        task_id="task_1",
        conversation_id="c1",
        person_id="usr_a",
        message_context_id=GUIDANCE_CONTEXT_ID,
    )
    assert (decision.allowed, decision.reason) == (reason == "sent_by_gateway_for_task", reason)


def test_work_for_no_person_matches_only_a_context_for_no_person() -> None:
    nobody = sent_files_access(
        sender=_sent(person_id=None), task_id="task_1", conversation_id="c1", person_id=" ",
        message_context_id=GUIDANCE_CONTEXT_ID,
    )
    assert nobody.allowed


def test_a_message_context_id_names_one_message_and_its_files_exactly() -> None:
    """A context overheard on one message vouches for no other message and no other file."""

    request = _request([_file()])
    ids = {
        GUIDANCE_CONTEXT_ID,
        steering_context_id(_guidance(key="corr_other")),  # another message
        steering_context_id(_guidance(files=[])),  # its files left out
        steering_context_id(_guidance(files=[_file(), _file("att_2")])),  # a file added
        steering_context_id(_guidance(files=[_file("att_bob")])),  # another file id
        steering_context_id(_guidance(files=[_file(path="/uploads/web/bobs.png")])),  # same id, other bytes
        steering_context_id(_guidance(files=[_file().model_copy(update={"filename": "other.png"})])),
        request_context_id(request),
        # A request whose task id is the guidance's key: the kinds still differ.
        request_context_id(request.model_copy(update={"task_id": "corr_guidance"})),
    }
    assert len(ids) == 9
    assert all(value.startswith(f"{MESSAGE_CONTEXT_ID_PREFIX}_") and len(value) <= 120 for value in ids)
    # Every resend of the same held message, and the message as the lead parses it, gets the same id.
    resent = TaskSteering.model_validate_json(_guidance().model_dump_json())
    assert steering_context_id(resent) == GUIDANCE_CONTEXT_ID
    # Without an idempotency key the lead deduplicates on the steering id, and so does the id.
    unkeyed = _guidance().model_copy(update={"idempotency_key": None})
    assert steering_context_id(unkeyed) == steering_context_id(_guidance(key="task_steer_id_1"))


def _answer(task_query_id: str = "task_query_1", files: list[UserTaskAttachment] | None = None) -> TaskQuery:
    return TaskQuery(
        task_query_id=task_query_id,
        query_type=MessageMetaType.RESPONSE,
        parent_task_id="task_1",
        message="This one.",
        attachments=[_file()] if files is None else files,
    )


def test_an_answer_context_id_names_the_question_it_answers_and_its_files_exactly() -> None:
    """An answer's id differs from any guidance's or request's, and from another answer's or files'."""

    answer_id = answer_context_id(_answer())
    ids = {
        answer_id,
        answer_context_id(_answer("task_query_2")),  # an answer to another question
        answer_context_id(_answer(files=[])),  # its files left out
        answer_context_id(_answer(files=[_file("att_bob")])),  # another file
        # A guidance and a request keyed on the question's id: the kinds still differ.
        steering_context_id(_guidance(key="task_query_1")),
        request_context_id(_request([_file()], task_id="task_query_1")),
    }
    assert len(ids) == 6
    assert answer_id.startswith(f"{MESSAGE_CONTEXT_ID_PREFIX}_") and len(answer_id) <= 120
    # The answer's text is not part of it; the answer as the lead parses it gets the same id.
    assert answer_context_id(_answer().model_copy(update={"message": "Another text."})) == answer_id
    assert answer_context_id(TaskQuery.model_validate_json(_answer().model_dump_json())) == answer_id
