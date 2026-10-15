"""Data folders registered in the access store (decision D16).

An administrator registers a host folder under a name; agents name it
``data/<name>`` and reach it only with folder grants. Registration refuses a
folder that holds or sits inside the instance, its configuration or the
broker's credentials, one that looks like a credential store, and one that
overlaps another data folder. Every process reads the list from the store on
use, so a new folder needs no restart, and a stored folder that breaks the
rules is left out and logged.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from access_control.data_folders import DataFolderRefused, DataFolderRegistry, protected_locations, registration_problem
from access_control.entities import PersonPrincipal
from assistant_gateway.access_commands import GatewayAccessCommands
from assistant_gateway.application.commands.access import DataFolderInput, parse_folders
from custom_tools.workspace_scope import configured_data_folders
from runtime_settings.instance_layout import InstanceLayout
from storage.access_repository import AccessRepository, AccessStoreConflict
from tests.data_folder_support import DATA_FOLDER_TEAM, agent_with_data_folders, sqlite_storage, store_data_folder

OWNER = PersonPrincipal("owner-1", DATA_FOLDER_TEAM, ("owner",))
MEMBER = PersonPrincipal("member-1", DATA_FOLDER_TEAM, ("member",))
NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


@pytest.fixture
def setup(tmp_path: Path):
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    storage = sqlite_storage(tmp_path / "manager.db")
    registry = DataFolderRegistry(AccessRepository(storage), DATA_FOLDER_TEAM)
    protected = protected_locations(
        layout.root, instance_config=tmp_path / "instance_config.yaml", broker_credentials=tmp_path / "secrets" / "broker"
    )
    return SimpleNamespace(tmp=tmp_path, layout=layout, storage=storage, registry=registry, protected=protected)


def register(setup, name: str, path: Path, *, person=OWNER, writable: bool = True):
    return setup.registry.register(
        person, name=name, path=str(path), writable=writable, reason="Reports", protected=setup.protected, now=NOW
    )


@pytest.mark.parametrize("name", ["a", "reports", "ref-lib", "reports_2026", "0data", "a" * 64])
def test_valid_names_are_accepted(setup, name) -> None:
    folder = setup.tmp / "outside"
    folder.mkdir()
    assert registration_problem(name, str(folder), protected=setup.protected) is None


@pytest.mark.parametrize("name", ["", "Reports", "-lead", "has space", "a/b", "../x", "a" * 65])
def test_malformed_names_are_refused(setup, name) -> None:
    assert "name" in registration_problem(name, str(setup.tmp), protected=setup.protected)


@pytest.mark.parametrize("path", ["", "reports", "./reports", "../reports", "/"])
def test_relative_paths_and_the_filesystem_root_are_refused(setup, path) -> None:
    assert "absolute folder" in registration_problem("reports", path, protected=setup.protected)


def test_a_missing_folder_is_refused_with_the_docker_hint(setup) -> None:
    assert "mount it" in registration_problem("reports", str(setup.tmp / "nowhere"), protected=setup.protected)


@pytest.mark.parametrize("inside", ["", "workspaces/team", "control", "dbs"])
def test_the_instance_root_and_anything_inside_it_are_refused(setup, inside) -> None:
    path = setup.layout.root / inside if inside else setup.layout.root
    path.mkdir(parents=True, exist_ok=True)
    assert "is inside" in registration_problem("reports", str(path), protected=setup.protected)


def test_a_folder_holding_the_instance_or_the_credentials_is_refused(setup) -> None:
    (setup.tmp / "secrets" / "broker").mkdir(parents=True)
    assert "contains" in registration_problem("all", str(setup.tmp), protected=setup.protected)
    assert "contains" in registration_problem("secrets", str(setup.tmp / "secrets"), protected=setup.protected)


def test_a_link_into_the_instance_and_credential_stores_are_refused(setup) -> None:
    link = setup.tmp / "looks-outside"
    link.symlink_to(setup.layout.control_dir, target_is_directory=True)
    assert "is inside" in registration_problem("reports", str(link), protected=setup.protected)
    ssh = setup.tmp / "home" / ".ssh"
    ssh.mkdir(parents=True)
    assert "credential store" in registration_problem("keys", str(ssh), protected=setup.protected)


def test_only_administrators_register_and_remove_and_folders_never_overlap(setup) -> None:
    reports = setup.tmp / "reports"
    (reports / "2026").mkdir(parents=True)
    with pytest.raises(DataFolderRefused, match="owner or an administrator"):
        register(setup, "reports", reports, person=MEMBER)

    record = register(setup, "reports", reports, writable=False)

    assert record.folder_id == "data/reports" and record.writable is False
    with pytest.raises(DataFolderRefused, match="overlaps"):
        register(setup, "year", reports / "2026")
    with pytest.raises(AccessStoreConflict, match="already registered"):
        setup.registry.store.register_data_folder(record.model_copy(update={"record_id": "again"}))
    with pytest.raises(DataFolderRefused, match="owner or an administrator"):
        setup.registry.remove(MEMBER, "reports", now=NOW)
    removed = setup.registry.remove(OWNER, "reports", now=NOW)
    assert removed.status.value == "removed" and setup.registry.records() == []
    kinds = [event.kind for event in setup.registry.store.events(DATA_FOLDER_TEAM)]
    assert kinds == ["data_folder_registered", "data_folder_removed"]


def test_agents_see_a_new_folder_on_next_use_and_never_one_that_breaks_the_rules(setup, caplog) -> None:
    agent = agent_with_data_folders(setup.storage, setup.layout)
    assert configured_data_folders(agent) == ()
    reports = setup.tmp / "reports"
    reports.mkdir()
    register(setup, "reports", reports)
    assert [folder.name for folder in configured_data_folders(agent)] == ["reports"], "no restart needed"

    # A record stored before the rules changed, reaching the instance, is left out when read.
    store_data_folder(setup.storage, "everything", setup.tmp)
    with caplog.at_level(logging.ERROR):
        names = [folder.name for folder in configured_data_folders(agent)]
    assert names == ["reports"]
    assert "event=data_folder_refused" in caplog.text


def test_the_folders_command_lists_adds_and_removes_in_both_modes(setup) -> None:
    reports = setup.tmp / "reports"
    reports.mkdir()
    commands = GatewayAccessCommands(
        requests=lambda: pytest.fail("data folders need no access service"),
        person={"owner-1": OWNER, "member-1": MEMBER}.get,
        review_url=lambda _id=None: "/webchat?view=inbox",
        authorization_enabled=False,
        data_folders=lambda: setup.registry,
        protected_locations=setup.protected,
    )

    assert parse_folders([]) == ("list", None)
    assert parse_folders(["add", "reports", str(reports), "--read-only"]) == (
        "add", DataFolderInput("reports", str(reports), writable=False, reason="")
    )
    refused = asyncio.run(commands.add_data_folder("member-1", DataFolderInput("reports", str(reports))))
    added = asyncio.run(commands.add_data_folder("owner-1", DataFolderInput("reports", str(reports), writable=False)))
    listed = asyncio.run(commands.list_data_folders("member-1"))
    removed = asyncio.run(commands.remove_data_folder("owner-1", "reports"))

    assert refused.startswith("Not done: Only an owner or an administrator")
    assert "Registered data/reports" in added and "read only" in added
    assert f"data/reports: {reports.resolve()} (read only)" in listed
    assert removed.startswith("Removed data/reports")
    assert "No data folders are registered" in asyncio.run(commands.list_data_folders("owner-1"))
