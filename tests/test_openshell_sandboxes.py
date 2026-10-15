"""One OpenShell sandbox per person (custom_tools.openshell_sandboxes).

A stand-in ``openshell`` program records every call and keeps the names of
the sandboxes it "created", so these tests check the exact command lines: a
person's sandbox gets copies of only their folder and the shared folders, it
is created once, and another person gets their own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_tools import openshell_sandboxes
from custom_tools.openshell_sandboxes import (
    create_arguments,
    ensure_sandbox,
    instance_sandboxes,
    sandbox_name,
)
from runtime_settings.instance_layout import InstanceLayout, person_folder_name, workspace_scope

FAKE_OPENSHELL = """#!/bin/sh
# Records each call; "sandbox list --names" prints the created names.
printf '%s\\n' "$*" >> "{log}"
if [ "$1 $2" = "sandbox list" ]; then cat "{names}" 2>/dev/null; exit 0; fi
if [ "$1 $2" = "sandbox create" ]; then printf '%s\\n' "$4" >> "{names}"; exit 0; fi
exit 0
"""


@pytest.fixture
def openshell(tmp_path: Path):
    log, names = tmp_path / "calls.log", tmp_path / "names.txt"
    program = tmp_path / "openshell"
    program.write_text(FAKE_OPENSHELL.format(log=log, names=names), encoding="utf-8")
    program.chmod(0o755)
    openshell_sandboxes.forget_ready_sandboxes()
    yield SimpleCli(str(program), log)
    openshell_sandboxes.forget_ready_sandboxes()


class SimpleCli:
    def __init__(self, path: str, log: Path) -> None:
        self.path = path
        self.log = log

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


@pytest.fixture
def layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    return layout


ENV = {"OPENSHELL_SANDBOX_NAME": "team", "OPENSHELL_WORKSPACE_MOUNT": "/workspace"}


def test_names_keep_people_apart_and_fit_a_sandbox_name() -> None:
    names = {sandbox_name("team", person) for person in ("usr_a", "usr-a", "shared", "Alice Smith", None)}

    assert len(names) == 5
    assert sandbox_name("team", None) == "team-shared"
    assert all(len(name) <= 63 and name == name.lower() and "_" not in name for name in names)
    assert sandbox_name("A very long instance prefix " * 4, "someone@example.com").startswith("a-very-long")


def test_a_persons_sandbox_holds_copies_of_only_their_folders(layout: InstanceLayout) -> None:
    scope = workspace_scope(layout.workspaces_dir, "alice")
    arguments = create_arguments("team-p-alice", scope, {**ENV, "OPENSHELL_SANDBOX_POLICY_FILE": "/policies/closed.yaml"})

    uploads = [arguments[index + 1] for index, value in enumerate(arguments) if value == "--upload"]
    alice = layout.person_dir("alice")
    assert uploads == [
        f"{alice}:/workspace/users/{person_folder_name('alice')}",
        f"{layout.team_dir}:/workspace/team",
        f"{layout.agents_dir}:/workspace/agents",
    ]
    assert arguments[arguments.index("--policy") + 1] == "/policies/closed.yaml"
    assert not any("bob" in value for value in arguments)


def test_a_sandbox_is_created_once_per_person(layout: InstanceLayout, openshell) -> None:
    alice = workspace_scope(layout.workspaces_dir, "alice")
    bob = workspace_scope(layout.workspaces_dir, "bob")

    first = ensure_sandbox(openshell.path, alice, ENV)
    again = ensure_sandbox(openshell.path, alice, ENV)
    other = ensure_sandbox(openshell.path, bob, ENV)

    assert first == again == sandbox_name("team", "alice")
    assert other == sandbox_name("team", "bob") != first
    creates = [call for call in openshell.calls() if call.startswith("sandbox create")]
    assert len(creates) == 2
    assert f"users/{person_folder_name('bob')}" not in creates[0]
    assert instance_sandboxes(openshell.path, ENV) == sorted([first, other])
    # A later process finds the existing sandbox instead of creating it again.
    openshell_sandboxes.forget_ready_sandboxes()
    ensure_sandbox(openshell.path, alice, ENV)
    assert len([call for call in openshell.calls() if call.startswith("sandbox create")]) == 2
