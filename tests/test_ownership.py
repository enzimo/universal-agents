"""Who may see or change a person's private records (access_control.ownership)."""

from __future__ import annotations

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
    private_access,
)


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
