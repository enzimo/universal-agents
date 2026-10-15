from __future__ import annotations
from unittest.mock import AsyncMock

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands.sandbox.not_a_sandbox_local_environment import (
    NotASandboxLocalEnvironment,
)

from assistant_gateway.config import GatewayInstanceConfig
from custom_agents.deferred_message_runtime import requires_execution_slot
from custom_agents.skill_catalog_runtime import (
    handle_managed_skill_catalog_notification,
    publish_managed_skill_catalog_notification,
    repair_agent_skill_catalog,
)
from custom_agents.skill_installation import resolve_skill_installation_paths
from custom_agents.skill_repository import build_skill_tree_manifest
from custom_agents.strands_agent import UniversalAgent
from custom_agents.sdk.strands.skills import (
    build_skills_plugin,
    list_agent_skill_names,
    refresh_skills_plugin,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.skill_catalog import ManagedSkillCatalogChangedV1


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
    published_presence: list[tuple[str, ...]] = []
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
        _published_presence=published_presence,
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

    async def publish_presence(*, is_exiting: bool) -> None:
        assert is_exiting is False
        published_presence.append(tuple(list_agent_skill_names(plugin, sdk_agent)))

    runtime._refresh_skills_plugins = refresh
    runtime._publish_presence_status = publish_presence
    refresh(force=False)
    await plugin.init_agent(sdk_agent)  # type: ignore[arg-type]
    return runtime, plugin


def _manager_runtime() -> tuple[SimpleNamespace, list[MessageEnvelope]]:
    published: list[MessageEnvelope] = []

    async def send_operational(message: MessageEnvelope) -> None:
        published.append(message)

    return (
        SimpleNamespace(
            id="agent_team_architect",
            name="TeamArchitect-0001",
            named_agent_type="TeamArchitect",
            team_id=TEAM_ID,
            is_team_lead=True,
            _send_operational_message=send_operational,
        ),
        published,
    )


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
async def test_install_notification_refreshes_idle_specialist_and_presence(
    tmp_path: Path,
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
    manager, published = _manager_runtime()
    await publish_managed_skill_catalog_notification(
        manager,
        {
            "change_id": INSTALL_CHANGE_ID,
            "skill_name": "grill-me",
            "installed_tree_sha256": tree_digest,
        },
        operation="install",
    )
    envelope = MessageEnvelope.model_validate_json(published[0].model_dump_json())

    assert UniversalAgent._is_operational_content(envelope.msg_content)
    assert not requires_execution_slot(envelope)
    assert UniversalAgent._operational_target_matches(
        specialist,
        msg_content=envelope.msg_content,
        msg_header=envelope.msg_header,
        msg_meta=envelope.msg_meta,
    )
    assert await handle_managed_skill_catalog_notification(
        specialist,
        notification=envelope.msg_content,
        msg_header=envelope.msg_header,
        msg_meta=envelope.msg_meta,
    )
    assert list_agent_skill_names(plugin, specialist._strands_agent) == [
        "base",
        "grill-me",
    ]
    assert specialist._published_presence == [("base", "grill-me")]

    assert await handle_managed_skill_catalog_notification(
        specialist,
        notification=envelope.msg_content,
        msg_header=envelope.msg_header,
        msg_meta=envelope.msg_meta,
    )
    assert specialist._published_presence == [("base", "grill-me")]


@pytest.mark.asyncio
async def test_notification_rejects_sender_mismatch_and_missing_receipt(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config, config_path = _instance(tmp_path)
    specialist, _plugin = await _specialist_runtime(config, config_path)
    notification = ManagedSkillCatalogChangedV1(
        team_id=TEAM_ID,
        source_agent_id="agent_team_architect",
        operation="install",
        change_id=INSTALL_CHANGE_ID,
        skill_name="grill-me",
        installed_tree_sha256=TREE_DIGEST,
    )
    with caplog.at_level(logging.WARNING):
        assert not await handle_managed_skill_catalog_notification(
            specialist,
            notification=notification,
            msg_header=MessageHeader(from_agent_id="forged_agent"),
            msg_meta=MessageMeta(from_agent_name="forged"),
        )
        assert not await handle_managed_skill_catalog_notification(
            specialist,
            notification=notification,
            msg_header=MessageHeader(from_agent_id="agent_team_architect"),
            msg_meta=MessageMeta(from_agent_name="TeamArchitect-0001"),
        )
    assert "event=managed_skill_catalog_sender_rejected" in caplog.text
    assert "event=managed_skill_catalog_notification_failed" in caplog.text


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
        assert not await handle_managed_skill_catalog_notification(
            specialist,
            notification=notification,
            msg_header=MessageHeader(from_agent_id="agent_team_architect"),
            msg_meta=MessageMeta(from_agent_name="TeamArchitect-0001"),
        )
        assert not await repair_agent_skill_catalog(
            specialist,
            reason="presence_heartbeat",
        )
    assert list_agent_skill_names(plugin, specialist._strands_agent) == ["base"]
    assert "Managed skill contents changed" in caplog.text
