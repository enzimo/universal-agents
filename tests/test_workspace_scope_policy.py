"""Per-person folders and read-only skill folders for agent file tools.

Each tool call resolves its folders from the person its task works for
(custom_tools.workspace_scope), taken from the task's signed context. These
tests drive the real path policy with a minimal agent whose active task
carries a gateway-signed context for that person.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents import access_runtime
from custom_tools.path_policy import (
    PERSON_WORKSPACE_DENIED,
    PROTECTED_RUNTIME_CONTROL_DENIED,
    TEAM_SKILL_READ_ONLY,
    WORKSPACE_PATH_DENIED,
    PathPolicyError,
    contains_protected_runtime_control_reference,
    ensure_allowed_path,
)
from tests.task_work_support import set_agent_ref
from custom_tools.workspace_scope import current_workspace_scope
from runtime_settings.instance_layout import InstanceLayout, person_folder_name
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer, context_verify_keys

TEAM = "team-files"


def task_agent(layout: InstanceLayout, person: str | None, *, signed: bool = True) -> SimpleNamespace:
    """An agent whose current task works for ``person`` (or for no person).

    ``signed=False`` names the person only in the unsigned message field, as a
    forged or damaged message would.
    """

    context = context_signer().issue(team_id=TEAM, person_id=person, person_roles=("member",)) if person else None
    meta = SimpleNamespace(
        gateway_context=(
            SimpleNamespace(canonical_user_id=person, task_context=context if signed else None) if person else None
        )
    )
    task_manager = SimpleNamespace(
        current_task_id="task-1",
        get_task_info=lambda _task_id: SimpleNamespace(meta=meta),
        storage_settings=resolve_storage_settings(
            {"backend": "sqlite", "sqlite": {"path": str(layout.root / "manager.db")}}
        ),
    )
    return SimpleNamespace(
        id="agent-files",
        team_id=TEAM,
        instance_root=str(layout.root),
        workspace_dir=str(layout.workspaces_dir),
        authorization_config={"enabled": False},
        task_manager=task_manager,
    )


@pytest.fixture
def layout(tmp_path: Path, monkeypatch) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    for person in ("alice", "bob"):
        layout.person_dir(person).mkdir(parents=True)
        (layout.person_dir(person) / "notes.txt").write_text(person, encoding="utf-8")
    layout.soul_path.write_text("soul", encoding="utf-8")
    (layout.skills_dir / "markitdown" / "scripts").mkdir(parents=True)
    (layout.skills_dir / "markitdown" / "SKILL.md").write_text("skill", encoding="utf-8")
    (layout.installed_skills_dir / "reviewed").mkdir(parents=True)
    (layout.skill_installation_dir / "plans").mkdir(parents=True)
    for name in ("TEAM_INSTANCE_ROOT", "TEAM_INSTANCE_CONFIG", "TOOL_DENY_PATHS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    yield layout
    set_agent_ref(None)
    access_runtime._runtime_access.cache_clear()


def as_person(layout: InstanceLayout, person: str | None) -> None:
    set_agent_ref(task_agent(layout, person))


def denied(path: str | Path, *, for_write: bool = False) -> str:
    with pytest.raises(PathPolicyError) as raised:
        ensure_allowed_path(str(path), for_write=for_write)
    return raised.value.reason_code


def test_relative_paths_resolve_in_the_persons_folder(layout):
    as_person(layout, "alice")

    assert ensure_allowed_path("notes.txt") == layout.person_dir("alice") / "notes.txt"
    assert current_workspace_scope().home == layout.person_dir("alice")


def test_a_person_uses_their_folder_and_the_shared_folders(layout):
    as_person(layout, "alice")

    for folder in (layout.person_dir("alice"), layout.team_dir, layout.agents_dir):
        assert ensure_allowed_path(str(folder / "x.txt"), for_write=True) == folder / "x.txt"


def test_another_persons_folder_is_private(layout):
    as_person(layout, "alice")

    assert denied(layout.person_dir("bob") / "notes.txt") == PERSON_WORKSPACE_DENIED
    assert denied(layout.users_dir) == PERSON_WORKSPACE_DENIED
    assert denied("../bob/notes.txt") == PERSON_WORKSPACE_DENIED


def test_work_for_no_person_cannot_open_any_persons_folder(layout):
    as_person(layout, None)

    assert current_workspace_scope().home == layout.agents_dir
    assert denied(layout.person_dir("alice") / "notes.txt") == PERSON_WORKSPACE_DENIED
    assert ensure_allowed_path("x.txt") == layout.agents_dir / "x.txt"


@pytest.mark.parametrize(
    "relative",
    ["control/SOUL.md", "dbs/task_history.db", "state/uploads/a.png", "team.env", "runtime/skill-installation/plans"],
)
def test_instance_private_areas_are_closed(layout, relative):
    as_person(layout, "alice")

    assert denied(layout.root / relative) == PROTECTED_RUNTIME_CONTROL_DENIED


def test_paths_outside_the_instance_are_refused(layout, tmp_path):
    as_person(layout, "alice")

    assert denied(tmp_path / "elsewhere.txt") == WORKSPACE_PATH_DENIED


def test_skill_folders_are_readable_not_writable(layout):
    as_person(layout, "alice")
    skill = layout.skills_dir / "markitdown" / "SKILL.md"
    reviewed = layout.installed_skills_dir / "reviewed"

    assert ensure_allowed_path(str(skill)) == skill
    assert ensure_allowed_path(str(reviewed)) == reviewed
    assert denied(skill, for_write=True) == TEAM_SKILL_READ_ONLY
    assert denied(reviewed / "SKILL.md", for_write=True) == TEAM_SKILL_READ_ONLY


def test_traversal_out_of_a_skill_folder_is_refused(layout):
    as_person(layout, "alice")

    assert denied(layout.skills_dir / ".." / "SOUL.md") == PROTECTED_RUNTIME_CONTROL_DENIED


def test_command_text_may_name_skill_scripts_but_not_control_files(layout):
    as_person(layout, "alice")
    script = layout.skills_dir / "markitdown" / "scripts" / "convert.py"

    assert not contains_protected_runtime_control_reference(f"python {script} in.pdf")
    assert contains_protected_runtime_control_reference(f"cat {layout.soul_path}")
    assert contains_protected_runtime_control_reference(f"cat {layout.skills_dir}/../SOUL.md")


def test_concurrent_tasks_for_different_people_get_their_own_folders(layout):
    seen: dict[str, Path] = {}
    ready = threading.Barrier(2)

    def run(person: str) -> None:
        as_person(layout, person)
        ready.wait()
        seen[person] = ensure_allowed_path("notes.txt")

    threads = [threading.Thread(target=run, args=(person,)) for person in ("alice", "bob")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert seen == {
        "alice": layout.users_dir / person_folder_name("alice") / "notes.txt",
        "bob": layout.users_dir / person_folder_name("bob") / "notes.txt",
    }


def test_research_files_go_to_the_tasks_own_folder(layout):
    from custom_tools import web_research_artifact_tools, web_service_tools
    from custom_tools.research_identity import research_identity

    # Both research tool modules use the one shared helper.
    assert web_research_artifact_tools.research_identity is research_identity
    assert web_service_tools.research_identity is research_identity
    as_person(layout, "alice")
    alice_root = research_identity().research_root
    as_person(layout, None)
    shared_root = research_identity().research_root

    assert alice_root == layout.person_dir("alice") / "research"
    assert shared_root == layout.agents_dir / "research"


def test_a_person_named_without_a_signed_context_gets_only_the_shared_folders(layout: InstanceLayout) -> None:
    set_agent_ref(task_agent(layout, "alice", signed=False))

    scope = current_workspace_scope()

    assert scope.person_id is None
    assert denied(layout.person_dir("alice") / "notes.txt") == PERSON_WORKSPACE_DENIED
