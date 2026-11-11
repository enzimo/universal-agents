from __future__ import annotations
from unittest.mock import AsyncMock

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands.sandbox.not_a_sandbox_local_environment import (
    NotASandboxLocalEnvironment,
)

from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import agent_requests
from custom_agents.live_controls import skills_changed
from custom_agents.skill_catalog_runtime import (
    apply_managed_skill_catalog_change,
    publish_managed_skill_catalog_notification,
    refresh_agent_skill_catalog,
    repair_agent_skill_catalog,
)
from custom_agents.skill_installation import resolve_skill_installation_paths
from custom_agents.skill_repository import build_skill_tree_manifest
from custom_agents.sdk.strands.skills import (
    build_skills_plugin,
    list_agent_skill_names,
    refresh_skills_plugin,
)
from custom_messages.skill_catalog import ManagedSkillCatalogChangedV1
from custom_messages.wire.frame import body_model


TEAM_ID = "team_skill_catalog"
INSTALL_CHANGE_ID = "skill_install_0123456789ab"
TREE_DIGEST = "a" * 64


class _LocalSkillAgent:
    def __init__(self) -> None:
        self.sandbox = NotASandboxLocalEnvironment()
        values: dict[str, object] = {}
        self.state = SimpleNamespace(
            get=values.get,
            set=lambda key, value: values.__setitem__(key, value),
        )


def _instance(tmp_path: Path) -> tuple[GatewayInstanceConfig, Path]:
    instance_root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="skill-catalog-test",
        instance_root=str(instance_root),
        team_id=TEAM_ID,
        timezone="UTC",
    )
    config_path = instance_root / "instance_config.yaml"
    config.save(config_path)
    return config, config_path


def _write_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} workflow.\n---\n\n# {name}\n",
        encoding="utf-8",
    )


async def _specialist_runtime(
    config: GatewayInstanceConfig,
    config_path: Path,
) -> tuple[SimpleNamespace, object]:
    # Operator-added skills live in control/skills; reviewed installs in
    # runtime/skill-installation/installed.
    team_skills = config.layout.skills_dir
    team_skills.mkdir(parents=True, exist_ok=True)
    _write_skill(team_skills, "base")
    managed_root = config.layout.installed_skills_dir
    managed_root.mkdir(parents=True, exist_ok=True)
    plugin = build_skills_plugin(
        skills_dir=str(team_skills),
        additional_skill_directories=[str(managed_root)],
        strict=True,
    )
    sdk_agent = _LocalSkillAgent()
    await plugin.init_agent(sdk_agent)  # type: ignore[arg-type]
    refreshed_state: list[bool] = []

    runtime = SimpleNamespace(
        id="agent_web_research",
        name="WebResearchAgent-0001",
        named_agent_type="WebResearchAgent",
        team_id=TEAM_ID,
        is_team_lead=False,
        is_executing=False,
        bootstrap_config={},
        instance_config_path=str(config_path),
        instance_root=str(config.layout.root),
        workspace_dir=str(config.workspace_path),
        skills_dir=str(team_skills),
        additional_skill_directories=[str(managed_root)],
        plugins=[plugin],
        _strands_agent=sdk_agent,
        _skills_fingerprint=None,
        _runtime_loop=object(),
        _logger=logging.getLogger("test_managed_skill_catalog_runtime"),
        _refresh_self_agent_state=AsyncMock(
            side_effect=lambda: refreshed_state.append(True)
        ),
        _refreshed_state=refreshed_state,
    )

    def refresh(force: bool = False) -> None:
        runtime._skills_fingerprint = refresh_skills_plugin(
            plugin,
            skills_dir=runtime.skills_dir,
            additional_skill_directories=runtime.additional_skill_directories,
            force=force,
            previous_fingerprint=runtime._skills_fingerprint,
        )

    runtime._refresh_skills_plugins = refresh
    refresh(force=False)
    await plugin.init_agent(sdk_agent)  # type: ignore[arg-type]
    return runtime, plugin


def _manager_runtime(monkeypatch: pytest.MonkeyPatch) -> tuple[SimpleNamespace, list[tuple[str, dict]]]:
    """The team lead, with its team events captured (custom_agents.agent_requests.announce)."""

    published: list[tuple[str, dict]] = []

    async def announce(agent, kind, body):
        body_model(kind)[0].validate_python(body)
        published.append((kind, dict(body)))

    monkeypatch.setattr(agent_requests, "announce", announce)
    return (
        SimpleNamespace(
            id="agent_team_architect",
            name="TeamArchitect-0001",
            named_agent_type="TeamArchitect",
            team_id=TEAM_ID,
            is_team_lead=True,
        ),
        published,
    )


def _event(body: dict, *, role: str = "lead", agent: str = "agent_team_architect") -> SimpleNamespace:
    """What the ``skills.changed`` handler reads from a verified event: its body and the sender's session."""

    return SimpleNamespace(raw={"body": body}, session=SimpleNamespace(agent_role=role, team=TEAM_ID, agent=agent))


def _write_receipt(
    runtime: SimpleNamespace,
    *,
    operation: str,
    change_id: str,
    skill_name: str,
    installed_tree_sha256: str,
) -> None:
    paths = resolve_skill_installation_paths(runtime)
    receipt_root = (
        paths.receipts if operation == "install" else paths.uninstallation_receipts
    )
    receipt_root.mkdir(parents=True, exist_ok=True)
    (receipt_root / f"{change_id}.json").write_text(
        json.dumps(
            {
                "change_id": change_id,
                "team_id": TEAM_ID,
                "skill_name": skill_name,
                "installed_tree_sha256": installed_tree_sha256,
            }
        ),
        encoding="utf-8",
    )


def _write_attestation(runtime: SimpleNamespace, skill_name: str) -> str:
    paths = resolve_skill_installation_paths(runtime)
    skill_path = paths.installed / skill_name
    tree_digest = build_skill_tree_manifest(skill_path).tree_sha256
    paths.attestations.mkdir(parents=True, exist_ok=True)
    (paths.attestations / f"{skill_name}.json").write_text(
        json.dumps(
            {
                "skill_name": skill_name,
                "installed_path": str(skill_path.resolve()),
                "tree_sha256": tree_digest,
            }
        ),
        encoding="utf-8",
    )
    return tree_digest


@pytest.mark.asyncio
async def test_install_notification_refreshes_an_idle_specialist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, config_path = _instance(tmp_path)
    specialist, plugin = await _specialist_runtime(config, config_path)
    managed_root = Path(specialist.additional_skill_directories[0])
    assert list_agent_skill_names(plugin, specialist._strands_agent) == ["base"]

    _write_skill(managed_root, "grill-me")
    tree_digest = _write_attestation(specialist, "grill-me")
    _write_receipt(
        specialist,
        operation="install",
        change_id=INSTALL_CHANGE_ID,
        skill_name="grill-me",
        installed_tree_sha256=tree_digest,
    )
    manager, published = _manager_runtime(monkeypatch)
    await publish_managed_skill_catalog_notification(
        manager,
        {
            "change_id": INSTALL_CHANGE_ID,
            "skill_name": "grill-me",
            "installed_tree_sha256": tree_digest,
        },
        operation="install",
    )

    # One team event, in the contract's form, that every agent process hands to its agents.
    [(kind, body)] = published
    assert kind == "skills.changed"
    assert body == {"operation": "install", "change_id": INSTALL_CHANGE_ID, "skill_name": "grill-me",
                    "installed_tree_sha256": tree_digest}
    await skills_changed(specialist, _event(body))
    assert list_agent_skill_names(plugin, specialist._strands_agent) == [
        "base",
        "grill-me",
    ]

    # The same change again changes nothing.
    await skills_changed(specialist, _event(body))


@pytest.mark.asyncio
async def test_notification_rejects_a_sender_that_is_not_the_lead_and_a_missing_receipt(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config, config_path = _instance(tmp_path)
    specialist, plugin = await _specialist_runtime(config, config_path)
    body = {"operation": "install", "change_id": INSTALL_CHANGE_ID, "skill_name": "grill-me",
            "installed_tree_sha256": TREE_DIGEST}
    notification = ManagedSkillCatalogChangedV1(
        team_id=TEAM_ID,
        source_agent_id="agent_team_architect",
        operation="install",
        change_id=INSTALL_CHANGE_ID,
        skill_name="grill-me",
        installed_tree_sha256=TREE_DIGEST,
    )
    with caplog.at_level(logging.WARNING):
        # Another agent of the lead's process may not send it.
        await skills_changed(specialist, _event(body, role="worker", agent="worker_1"))
        assert not await apply_managed_skill_catalog_change(specialist, notification)
    assert "event=skills_changed_sender_rejected" in caplog.text
    assert "event=managed_skill_catalog_notification_failed" in caplog.text
    assert list_agent_skill_names(plugin, specialist._strands_agent) == ["base"]


@pytest.mark.asyncio
async def test_install_notification_rejects_tampered_managed_tree(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config, config_path = _instance(tmp_path)
    specialist, plugin = await _specialist_runtime(config, config_path)
    managed_root = Path(specialist.additional_skill_directories[0])
    _write_skill(managed_root, "grill-me")
    tree_digest = _write_attestation(specialist, "grill-me")
    _write_receipt(
        specialist,
        operation="install",
        change_id=INSTALL_CHANGE_ID,
        skill_name="grill-me",
        installed_tree_sha256=tree_digest,
    )
    (managed_root / "grill-me" / "SKILL.md").write_text(
        "---\nname: grill-me\ndescription: Tampered.\n---\n\n# Changed\n",
        encoding="utf-8",
    )
    notification = ManagedSkillCatalogChangedV1(
        team_id=TEAM_ID,
        source_agent_id="agent_team_architect",
        operation="install",
        change_id=INSTALL_CHANGE_ID,
        skill_name="grill-me",
        installed_tree_sha256=tree_digest,
    )

    with caplog.at_level(logging.WARNING):
        assert not await apply_managed_skill_catalog_change(specialist, notification)
        assert not await repair_agent_skill_catalog(
            specialist,
            reason="presence_heartbeat",
        )
    assert list_agent_skill_names(plugin, specialist._strands_agent) == ["base"]
    assert "Managed skill contents changed" in caplog.text


@pytest.mark.asyncio
async def test_skill_reload_does_not_yield_to_event_loop(tmp_path: Path, monkeypatch) -> None:
    """A skill reload must finish before any other work, such as a turn, can start.

    The idle check before a reload does not hold the execution slot. That is
    safe only because reloading with the runtime's file access (the Strands
    agent's default sandbox; UniversalAgent sets none) never yields to the
    event loop, as checked on 2026-10-17. If a Strands upgrade makes skill file
    reads yield, this test fails: then hold the execution slot for the reload.
    """
    import custom_agents.skill_catalog_runtime as catalog_runtime
    from strands import Agent

    from tests.test_agent_execution_ownership import HeldModel

    config, config_path = _instance(tmp_path)
    specialist, _plugin = await _specialist_runtime(config, config_path)
    specialist._strands_agent.sandbox = Agent(model=HeldModel(), callback_handler=None).sandbox
    events: list[str] = []
    original = catalog_runtime._hydrate_skill_plugins

    async def traced(agent, *, changed):
        events.append("reload_started")
        try:
            return await original(agent, changed=changed)
        finally:
            events.append("reload_finished")

    monkeypatch.setattr(catalog_runtime, "_hydrate_skill_plugins", traced)
    stop = asyncio.Event()

    async def other_work() -> None:
        while not stop.is_set():
            events.append("other_work")
            await asyncio.sleep(0)

    other = asyncio.create_task(other_work())
    await asyncio.sleep(0)
    managed_root = Path(specialist.additional_skill_directories[0])
    _write_skill(managed_root, "grill-me")
    digest = _write_attestation(specialist, "grill-me")
    _write_receipt(
        specialist,
        operation="install",
        change_id=INSTALL_CHANGE_ID,
        skill_name="grill-me",
        installed_tree_sha256=digest,
    )
    manager, published = _manager_runtime(monkeypatch)
    await publish_managed_skill_catalog_notification(
        manager,
        {"change_id": INSTALL_CHANGE_ID, "skill_name": "grill-me", "installed_tree_sha256": digest},
        operation="install",
    )
    [(_kind, body)] = published
    try:
        await skills_changed(specialist, _event(body))
        _write_skill(managed_root, "second-skill")
        _write_attestation(specialist, "second-skill")
        assert await repair_agent_skill_catalog(specialist, reason="presence_heartbeat")
    finally:
        stop.set()
        await other

    assert events.count("reload_started") == 2
    window: list[str] = []
    inside = False
    for event in events:
        if event == "reload_started":
            inside = True
        elif event == "reload_finished":
            inside = False
        elif inside:
            window.append(event)
    assert window == [], "a skill reload let other work run halfway through"


@pytest.mark.asyncio
async def test_a_new_lane_agent_leaves_running_turns_their_skills_and_changes_reach_every_agent(
    tmp_path: Path,
) -> None:
    """Regression (PR #57 review): building a parallel lane's Strands agent re-read the
    skill folders, which cleared the skills of turns running in other lanes until their
    next model call; Objective work then found its required skills missing."""

    config, config_path = _instance(tmp_path)
    runtime, plugin = await _specialist_runtime(config, config_path)
    lane_agent = _LocalSkillAgent()
    await plugin.init_agent(lane_agent)  # type: ignore[arg-type]
    runtime._parallel_lanes = SimpleNamespace(parts={"parallel:request": SimpleNamespace(agent=lane_agent)})
    runtime._running_turns = {}

    # What building another lane's Strands agent does (strands_agent_builder).
    runtime._refresh_skills_plugins()
    assert list_agent_skill_names(plugin, lane_agent) == ["base"]
    assert list_agent_skill_names(plugin, runtime._strands_agent) == ["base"]

    # A skill added meanwhile reaches the lane's running agent, not only the main one.
    _write_skill(config.layout.skills_dir, "added")
    await refresh_agent_skill_catalog(runtime, force=False, reason="test")
    assert list_agent_skill_names(plugin, lane_agent) == ["added", "base"]
    assert list_agent_skill_names(plugin, runtime._strands_agent) == ["added", "base"]
