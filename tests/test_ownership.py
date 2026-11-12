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
    conversation_continuation,
    conversation_files_access,
    named_task_access,
    objective_access,
    private_access,
    sent_files_access,
)
from custom_messages.contracts.access_v2 import TaskContextV1


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


def _sent(**changes) -> TaskContextV1:
    """A context the gateway signed for a guidance to task_1 in c1, for usr_a."""

    now = datetime.now(timezone.utc)
    fields = {
        "context_id": "task_context_guidance",
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
        (_sent(root_task_id=None), "another_task"),
        (_sent(root_task_id="task_2"), "another_task"),
        (_sent(conversation_id=None), "another_conversation"),
        (_sent(conversation_id="c2"), "another_conversation"),
        # The task's own context: every worker of the task holds a copy.
        (_sent(context_id="task_context_admitted"), "copied_task_context"),
    ],
)
def test_a_messages_files_are_kept_only_when_the_gateway_sent_them_for_the_task(sender, reason) -> None:
    decision = sent_files_access(
        sender=sender,
        task_id="task_1",
        conversation_id="c1",
        person_id="usr_a",
        task_context_id="task_context_admitted",
    )
    assert (decision.allowed, decision.reason) == (reason == "sent_by_gateway_for_task", reason)


def test_a_requests_own_context_vouches_for_its_files_and_work_for_no_person_matches_no_person() -> None:
    # At admission the sender is the task's own context, so no context is excluded.
    own = sent_files_access(sender=_sent(), task_id="task_1", conversation_id="c1", person_id="usr_a")
    nobody = sent_files_access(sender=_sent(person_id=None), task_id="task_1", conversation_id="c1", person_id=" ")
    assert own.allowed and nobody.allowed
