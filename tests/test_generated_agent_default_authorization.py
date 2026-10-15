"""A new specialist with the shipped authorization settings, then a restart.

With the default settings authorization is on, so a person approves the exact
specialist, which is usable at once and needs no restart. A restart staged
afterwards is its own change with its own approval (real Cedar and SQLite): an
approval for one change never applies another.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from strands.interrupt import InterruptException

from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import access_runtime
from custom_agents.generated_agent_provisioning import GeneratedAgentProvisioningService
from custom_agents.runtime_restart_service import RuntimeRestartService
from docker.launch_headless import pop_runtime_restart_request
from tests.change_approval_support import (
    CEDAR_BINARY,
    ToolCall,
    change_agent,
    decide,
    needs_cedar,
    run_apply_change,
    stored_request,
    use_signed_contexts,
)
from tests.test_generated_agent_provisioning import stage_communications

pytestmark = needs_cedar


@pytest.fixture
def lead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="default-authorization-test", instance_root=str(root), team_id="team_default", timezone="UTC"
    )
    config_path = config.save(root / "instance_config.yaml")
    agent = change_agent(config, config_path, authorization_enabled=True)
    # The shipped settings, with this checkout's policy engine.
    agent.authorization_config = {
        **config.authorization.model_dump(mode="json"),
        "cedar_binary_path": str(CEDAR_BINARY),
    }
    yield config, agent
    access_runtime._runtime_access.cache_clear()


def _restart_requests(agent) -> Path:
    return Path(agent.instance_root) / "runtime" / "restart_requests"


def test_default_settings_need_a_person_for_the_specialist_and_again_for_its_restart(lead) -> None:
    config, agent = lead
    plan = stage_communications(GeneratedAgentProvisioningService(agent))
    call = ToolCall()

    with pytest.raises(InterruptException):
        run_apply_change(agent, plan["change_id"], call)
    specialist_request = stored_request(agent, call)
    decide(agent, call)
    applied = run_apply_change(agent, plan["change_id"], call)

    assert config.authorization.enabled is True
    assert plan["authority_mode"] == "person_approval"
    assert specialist_request.change.kind == "agent_definition"
    assert applied["status"] == "applied", applied
    result = applied["result"]
    assert result["status"] == "ready" and "requires_manager_restart" not in result
    assert "no restart" in result["next_step"]
    manifest = json.loads((Path(result["bundle_path"]) / "provisioning.json").read_text(encoding="utf-8"))
    assert manifest["agent_type"] == "AcmeCommunicationsAgent"
    assert manifest["default_tools"] == ["code:python", "fn:schedules"]
    assert manifest["approval"]["access_request_id"] == specialist_request.request_id
    assert Path(result["receipt_path"]).is_file()
    assert pop_runtime_restart_request(_restart_requests(agent)) is None, "creating a specialist restarts nothing"

    restart_plan = RuntimeRestartService(agent).stage(scope="manager", reason="Restart the manager after the specialist change")
    assert restart_plan["status"] == "staged" and restart_plan["authority_mode"] == "person_approval"

    # The specialist's approval does not apply the restart.
    reused = run_apply_change(agent, restart_plan["change_id"], call)
    assert reused["status"] == "refused" and "changed after it was approved" in reused["reason"]
    assert not list(_restart_requests(agent).glob("*.json"))

    restart_call = ToolCall("tooluse_restart")
    with pytest.raises(InterruptException):
        run_apply_change(agent, restart_plan["change_id"], restart_call)
    restart_request = stored_request(agent, restart_call)
    decide(agent, restart_call)
    restarted = run_apply_change(agent, restart_plan["change_id"], restart_call)

    assert restart_request.change.kind == "runtime_restart"
    assert restart_request.request_id != specialist_request.request_id
    assert restarted["status"] == "applied", restarted
    supervisor_request = pop_runtime_restart_request(_restart_requests(agent))
    assert supervisor_request is not None and supervisor_request["scope"] == "manager"
    assert supervisor_request["authority"]["approval"]["access_request_id"] == restart_request.request_id
