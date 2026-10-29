"""Agent file tools open only checked files and never follow links (custom_tools.file_access).

Sandboxed code may create links in the folders it can write, and it runs at
the same time as trusted code. These tests plant links (and swap one in
between the check and the open) and prove that every tool family refuses to
reach control files or another person's folder through them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from custom_tools import file_access
from custom_tools.path_policy import (
    PERSON_WORKSPACE_DENIED,
    PROTECTED_RUNTIME_CONTROL_DENIED,
    PathPolicyError,
)
from runtime_settings.instance_layout import InstanceLayout
from tests.test_workspace_scope_policy import as_person
from tests.test_workspace_scope_policy import layout as _scope_layout

layout = _scope_layout  # the same instance layout fixture, with alice and bob


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def test_a_link_to_the_persons_own_file_is_read_where_it_points(layout: InstanceLayout) -> None:
    from custom_tools.non_interactive_file_tools import file_read

    as_person(layout, "alice")
    alice = layout.person_dir("alice")
    (alice / "shortcut.txt").symlink_to(alice / "notes.txt")

    result = file_read(path="shortcut.txt")

    assert result["status"] == "success"
    assert _text(result).endswith("alice")
    assert f"Path: {alice / 'notes.txt'}" in _text(result)


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        (lambda layout: layout.soul_path, PROTECTED_RUNTIME_CONTROL_DENIED),
        (lambda layout: layout.person_dir("bob") / "notes.txt", PERSON_WORKSPACE_DENIED),
    ],
)
def test_a_link_out_of_the_persons_folders_is_judged_where_it_points(layout: InstanceLayout, target, reason) -> None:
    from custom_tools.non_interactive_file_tools import file_read, file_write

    as_person(layout, "alice")
    link = layout.person_dir("alice") / "innocent.txt"
    link.symlink_to(target(layout))
    before = target(layout).read_text(encoding="utf-8")

    read = file_read(path=str(link))
    write = file_write(path=str(link), content="overwritten")

    assert (read["status"], read["reason_code"]) == ("error", reason)
    assert (write["status"], write["reason_code"]) == ("error", reason)
    assert target(layout).read_text(encoding="utf-8") == before


def test_a_planted_folder_link_does_not_let_writes_reach_control(layout: InstanceLayout) -> None:
    from custom_tools.non_interactive_file_tools import file_write

    as_person(layout, "alice")
    (layout.person_dir("alice") / "reports").symlink_to(layout.control_dir, target_is_directory=True)

    result = file_write(path="reports/SOUL.md", content="replaced")

    assert (result["status"], result["reason_code"]) == ("error", PROTECTED_RUNTIME_CONTROL_DENIED)
    assert layout.soul_path.read_text(encoding="utf-8") == "soul"


def test_a_hard_linked_file_is_refused(layout: InstanceLayout) -> None:
    from custom_tools.non_interactive_file_tools import file_read, file_write

    as_person(layout, "alice")
    os.link(layout.person_dir("bob") / "notes.txt", layout.person_dir("alice") / "copy.txt")

    read = file_read(path="copy.txt")
    write = file_write(path="copy.txt", content="changed")

    assert read["reason_code"] == file_access.SEVERAL_HARD_LINKS
    assert write["reason_code"] == file_access.SEVERAL_HARD_LINKS
    assert (layout.person_dir("bob") / "notes.txt").read_text(encoding="utf-8") == "bob"


def _swap_after_check(monkeypatch, folder: Path, target: Path) -> None:
    """Replace ``folder`` with a link to ``target`` right after the path check passes."""

    checked = file_access.ensure_allowed_path

    def check_then_swap(*args, **kwargs):
        result = checked(*args, **kwargs)
        folder.rename(folder.with_name(folder.name + ".old"))
        folder.symlink_to(target, target_is_directory=True)
        return result

    monkeypatch.setattr(file_access, "ensure_allowed_path", check_then_swap)


@pytest.mark.parametrize("operation", ["read", "write"])
def test_a_link_swapped_in_between_the_check_and_the_open_is_not_followed(
    layout: InstanceLayout, monkeypatch, operation: str
) -> None:
    from custom_tools.non_interactive_file_tools import file_read, file_write

    as_person(layout, "alice")
    docs = layout.person_dir("alice") / "docs"
    docs.mkdir()
    (docs / "SOUL.md").write_text("mine", encoding="utf-8")
    _swap_after_check(monkeypatch, docs, layout.control_dir)

    if operation == "read":
        result = file_read(path="docs/SOUL.md")
    else:
        result = file_write(path="docs/SOUL.md", content="replaced")

    assert (result["status"], result["reason_code"]) == ("error", file_access.LINK_NOT_FOLLOWED)
    assert "soul" not in _text(result)
    assert layout.soul_path.read_text(encoding="utf-8") == "soul"


def test_removing_a_folder_never_follows_a_link_swapped_into_its_path(layout: InstanceLayout, monkeypatch) -> None:
    as_person(layout, "alice")
    envs = layout.person_dir("alice") / "python_envs"
    (envs / "default").mkdir(parents=True)
    (layout.control_dir / "default").mkdir()
    (layout.control_dir / "default" / "keep.txt").write_text("keep", encoding="utf-8")
    _swap_after_check(monkeypatch, envs, layout.control_dir)

    with pytest.raises(PathPolicyError) as refused:
        file_access.remove_tree(envs / "default")

    assert refused.value.reason_code == file_access.LINK_NOT_FOLLOWED
    assert (layout.control_dir / "default" / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_local_media_and_attachments_refuse_a_link_to_control(layout: InstanceLayout) -> None:
    from custom_tools.local_media_context_tool import load_local_media_for_model_context
    from custom_tools.user_attachment_tools import inspect_user_attachment

    as_person(layout, "alice")
    link = layout.person_dir("alice") / "picture.md"
    link.symlink_to(layout.soul_path)

    media = load_local_media_for_model_context(path=str(link))
    attachment = inspect_user_attachment(path=str(link))

    assert media["status"] == "error"
    assert "soul" not in json.dumps(media)
    assert attachment["status"] == "error"
    assert "soul" not in json.dumps(attachment)


BINARY = Path(__file__).resolve().parents[1] / "target" / "debug" / "ua-cedar-authorizer"


@pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_with_authorization_on_file_tools_ask_for_each_folder(layout: InstanceLayout) -> None:
    from datetime import datetime, timezone

    from access_control.entities import PersonPrincipal
    from custom_agents import folder_access
    from custom_agents.access_runtime import runtime_access
    from custom_messages.contracts.access_v2 import (
        AccessLevel,
        GrantKind,
        GrantV1,
        SubjectKind,
        SubjectRefV1,
        TargetKind,
        TargetRefV1,
    )
    from custom_tools.non_interactive_file_tools import file_read, file_write
    from tests.task_work_support import set_agent_ref
    from tests.test_workspace_scope_policy import TEAM, task_agent

    agent = task_agent(layout, "alice")
    agent.authorization_config = {"enabled": True, "cedar_binary_path": str(BINARY)}
    agent.named_agent_type = "WebResearchAgent"
    set_agent_ref(agent)
    folder_access.forget_kept_answers()
    (layout.team_dir / "finance").mkdir()
    (layout.team_dir / "finance" / "budget.md").write_text("numbers", encoding="utf-8")

    assert file_read(path="notes.txt")["status"] == "success"
    assert file_write(path=str(layout.team_dir / "finance" / "plan.md"), content="draft")["status"] == "success"

    service = runtime_access(agent).service
    service.grant_by_person(
        PersonPrincipal("owner-1", TEAM, ("owner",)),
        GrantV1(
            grant_id="new",
            team_id=TEAM,
            kind=GrantKind.BLOCK,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance"),
            level=AccessLevel.FULL,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id="owner-1"),
            reason="Keep finance away from research.",
            created_at_utc=datetime.now(timezone.utc),
        ),
    )

    refused = file_read(path=str(layout.team_dir / "finance" / "budget.md"))

    assert (refused["status"], refused["reason_code"]) == ("error", file_access.ACCESS_DENIED)
    assert "only a person can remove the block" in _text(refused)
    assert "numbers" not in _text(refused)
    assert file_read(path="notes.txt")["status"] == "success"


@pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_a_blocked_folder_cannot_be_sent_as_an_attachment(layout: InstanceLayout) -> None:
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from access_control.entities import PersonPrincipal
    from custom_agents import folder_access
    from custom_agents.access_runtime import runtime_access
    from custom_messages.contracts.access_v2 import (
        AccessLevel,
        GrantKind,
        GrantV1,
        SubjectKind,
        SubjectRefV1,
        TargetKind,
        TargetRefV1,
    )
    from custom_tools.providers.communication import _attachment_access_errors
    from tests.task_work_support import set_agent_ref
    from tests.test_workspace_scope_policy import TEAM, task_agent

    agent = task_agent(layout, "alice")
    agent.authorization_config = {"enabled": True, "cedar_binary_path": str(BINARY)}
    agent.named_agent_type = "WebResearchAgent"
    set_agent_ref(agent)
    folder_access.forget_kept_answers()
    (layout.team_dir / "finance").mkdir()
    budget = layout.team_dir / "finance" / "budget.md"
    budget.write_text("numbers", encoding="utf-8")
    notes = layout.person_dir("alice") / "notes.txt"
    runtime_access(agent).service.grant_by_person(
        PersonPrincipal("owner-1", TEAM, ("owner",)),
        GrantV1(
            grant_id="new",
            team_id=TEAM,
            kind=GrantKind.BLOCK,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance"),
            level=AccessLevel.FULL,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id="owner-1"),
            created_at_utc=datetime.now(timezone.utc),
        ),
    )

    errors = _attachment_access_errors(agent, [SimpleNamespace(path=str(notes)), SimpleNamespace(path=str(budget))])

    assert len(errors) == 1
    assert errors[0].startswith("attachments[1].path cannot be sent")
