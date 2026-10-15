"""MCP operations: annotations are hints; an administrator may confirm each level.

Until an operation's level is confirmed, and whenever the operation changes,
it needs full access (mcp_control.operation_levels, access_catalog.catalog).
Confirming is optional (access plan 7d): a full grant on the server's tool
covers every operation. The command-line confirmation records an active
administrator, never a free-text name.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from typing import Any

import pytest

from access_catalog.catalog import load_catalog
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents.providers.mcp_connections import MCPConnectionAdministration
from custom_messages.contracts.access_v2 import AccessLevel, ClassificationSource
from mcp_control.operation_levels import (
    confirm_operation_levels,
    load_operation_levels,
    normalize_hints,
    record_discovered_hints,
    suggest_level,
)
from mcp_control.registry import (
    MCPConnectionLifecycle,
    MCPOperationDefinitionV1,
    reconcile_instance_mcp_connections,
)

READ, WRITE, FULL = AccessLevel.READ, AccessLevel.WRITE, AccessLevel.FULL


@pytest.mark.parametrize(
    ("hints", "level"),
    [
        ({"readOnlyHint": True}, READ),
        ({"readOnlyHint": True, "destructiveHint": True}, READ),
        ({"destructiveHint": False}, WRITE),
        ({"idempotentHint": True}, FULL),
        ({}, FULL),
    ],
)
def test_suggested_level_follows_mcp_defaults(hints, level):
    assert suggest_level(hints) is level


def test_only_known_hint_keys_and_types_are_kept():
    assert normalize_hints(
        {"readOnlyHint": "yes", "destructiveHint": False, "title": "Ask", "trustMe": True}
    ) == {"destructiveHint": False, "title": "Ask"}


def _operation(name: str, description: str = "Answer a question.") -> MCPOperationDefinitionV1:
    return MCPOperationDefinitionV1.build(operation_name=name, description=description, input_schema={"type": "object"})


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="mcp-levels", instance_root=str(root), team_id="team_mcp", timezone="UTC"
    )
    config.agent.external_mcp_servers = [
        {
            "name": "crawl4ai",
            "transport": "streamable_http",
            "server_url": "https://crawl4ai.example.test/mcp",
            "tool_name_prefix": "crawl4ai",
        }
    ]
    path = config.save(root / "instance_config.yaml")
    pending = reconcile_instance_mcp_connections(config)[0]
    store = MCPConnectionAdministration.for_instance_config(config).store
    active = store.update_lifecycle(
        pending.connection_id,
        lifecycle=MCPConnectionLifecycle.ACTIVE,
        approval=MCPConnectionAdministration.person_approval(config.user.canonical_user_id),
    )
    operations = (_operation("ask"), _operation("crawl"), _operation("delete_index"))
    active = store.set_operations(active.connection_id, operations)
    record_discovered_hints(
        store.paths,
        active.connection_id,
        active.operations,
        {"ask": {"readOnlyHint": True}, "crawl": {"destructiveHint": False}},
    )
    return root, path, store, active


def test_unconfirmed_operations_need_full_and_show_the_suggestion(installed):
    root, _path, _store, _active = installed

    tool = load_catalog(root).tools["mcp:crawl4ai"]

    ask = tool.operation("ask")
    assert (ask.level, ask.source, ask.suggested_level) == (FULL, ClassificationSource.UNREVIEWED, READ)
    assert tool.operation("delete_index").suggested_level is FULL


def test_confirmed_levels_apply_to_the_agents_prefixed_tool_name(installed):
    root, _path, store, active = installed
    confirm_operation_levels(store.paths, active.connection_id, {"ask": READ, "crawl": WRITE}, confirmed_by="admin")

    catalog = load_catalog(root)
    ask = catalog.resolve_function("crawl4ai_ask")
    crawl = catalog.resolve_function("crawl4ai_crawl")
    delete = catalog.resolve_function("crawl4ai_delete_index")
    unknown = catalog.resolve_function("crawl4ai_new_thing")

    assert (ask.tool_id, ask.level, ask.source) == ("mcp:crawl4ai", READ, ClassificationSource.ADMIN_CONFIRMED)
    assert crawl.level is WRITE
    assert (delete.level, delete.source) == (FULL, ClassificationSource.UNREVIEWED)
    assert unknown.level is FULL


def test_a_changed_operation_loses_its_confirmation(installed):
    _root, _path, store, active = installed
    confirm_operation_levels(store.paths, active.connection_id, {"ask": READ}, confirmed_by="admin")

    changed = (_operation("ask", description="Now also deletes things."), *active.operations[1:])
    levels = record_discovered_hints(store.paths, active.connection_id, changed, {"ask": {"readOnlyHint": True}})

    assert levels.operations["ask"].confirmed_level is None
    assert levels.operations["ask"].suggested_level is READ


def test_only_discovered_operations_can_be_confirmed(installed):
    _root, _path, store, active = installed

    with pytest.raises(KeyError, match="not discovered"):
        confirm_operation_levels(store.paths, active.connection_id, {"invented": READ}, confirmed_by="admin")


class _Identity:
    """Active people and their team roles, as the gateway identity store keeps them."""

    def __init__(self, roles: dict[str, str]) -> None:
        self.roles = roles

    def get_gateway_user(self, person_id: str) -> Any:
        return SimpleNamespace(status="active") if person_id in self.roles else None

    def list_team_memberships(self, person_id: str, status: str = "active") -> list[Any]:
        role = self.roles.get(person_id)
        return [SimpleNamespace(team_id="team_mcp", role=role)] if role else []


@pytest.fixture
def people(monkeypatch):
    from assistant_gateway.access_setup import MembershipRoles

    identity = _Identity({"alice": "admin", "bob": "member"})
    monkeypatch.setattr(MembershipRoles, "for_config", classmethod(lambda cls, _config: cls(identity)))
    return identity


def test_operator_command_accepts_suggestions_and_sets_levels(installed, people, capsys):
    from assistant_cli import _mcp_operation_levels

    _root, path, store, active = installed
    arguments = argparse.Namespace(
        config=str(path), connection="crawl4ai", set=["delete_index=full"], accept_suggested=True, by="alice"
    )

    _mcp_operation_levels(arguments)

    levels = load_operation_levels(store.paths, active.connection_id).operations
    assert {name: entry.confirmed_level for name, entry in levels.items()} == {
        "ask": READ,
        "crawl": WRITE,
        "delete_index": FULL,
    }
    assert levels["ask"].confirmed_by == "alice"
    assert "ask: read; suggested read" in capsys.readouterr().out


@pytest.mark.parametrize(("by", "refusal"), [("bob", "Only a team administrator"), ("mallory", "not an active member")])
def test_only_an_active_administrator_confirms_levels(installed, people, by, refusal):
    from assistant_cli import _mcp_operation_levels

    _root, path, store, active = installed
    arguments = argparse.Namespace(config=str(path), connection="crawl4ai", set=["ask=read"], accept_suggested=False, by=by)

    with pytest.raises(SystemExit, match=refusal):
        _mcp_operation_levels(arguments)
    assert load_operation_levels(store.paths, active.connection_id).operations["ask"].confirmed_level is None
