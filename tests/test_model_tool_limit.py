"""Raising a model's tool cap when an agent asks and an administrator allows it (custom_agents.model_tool_limit).

The real runtime settings service and apply_change. A stand-in model refuses
more tools than it takes. Staging checks the raise and sends one test request
(a refusal stages nothing), applying sends it again before anything is
written, the cap lands in LLM_OVERRIDES.yaml for that exact model and the
running agent uses it at once. The cap belongs to the model, so with
authorization on only an administrator approves it, never the person who
asked; it only raises, never for a person's own model, and TeamLead can ask
for an agent type's model on a worker's behalf.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.tool_result_support import tool_json
import yaml
from strands.models import Model
from strands.types.exceptions import ContextWindowOverflowException, ModelThrottledException

from custom_agents.model_choice import is_model_choice
from custom_agents.person_model_runtime import _state as person_model_state
from custom_agents.runtime_model_switch import RuntimeModelSwitchProbeError, probe_tool_capacity
from custom_agents.runtime_settings_service import RuntimeSettingsService
from custom_agents.tool_loading import tool_limit
from custom_tools.providers.runtime_settings import plan_runtime_setting_change, plan_runtime_settings_change
from custom_tools.providers.tool_loading import _higher_cap_hint
from custom_tools.runtime_context import set_agent_ref
from tests.test_runtime_settings_tools import _apply_settings_change, _fake_agent, _overrides_path, _write_config
from tests.test_runtime_settings_tools import _signed_task_contexts  # noqa: F401 - the gateway's signing keys

PATH = "model.max_tools_per_request"


class _Model(Model):
    """A provider that takes at most ``accepts`` tools in one request, or fails with ``error``."""

    config: dict = {}

    def __init__(self, accepts: int, error: Exception | None = None) -> None:
        self.accepts = accepts
        self.error = error
        self.sent: list[int] = []

    def update_config(self, **_kw) -> None: ...

    def get_config(self) -> dict:
        return {}

    async def structured_output(self, *_a, **_k):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **_kw):
        self.sent.append(len(tool_specs or []))
        if self.error is not None:
            raise self.error
        if len(tool_specs or []) > self.accepts:
            raise RuntimeError(f"Invalid 'tools': array too long. Expected at most {self.accepts}.")
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": "OK"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


@pytest.fixture
def lead(tmp_path: Path):
    config_path = _write_config(tmp_path)
    agent = _fake_agent(config_path)
    agent.model_capabilities = {}
    agent.request_timeout_seconds = 30
    agent.provider_model = _Model(accepts=200)
    agent.tested_models: list[str] = []

    def configure(profile):
        agent.tested_models.append(f"{profile.provider}/{profile.model_id}")
        return agent.provider_model

    agent._configure_model_for_profile = configure
    set_agent_ref(agent)
    yield agent, config_path
    set_agent_ref(None)


def stage(value, **extra) -> dict:
    return json.loads(plan_runtime_setting_change._tool_func(path=PATH, value=value, reason="The plan needs more tools at once.", **extra))


def saved_caps(config_path: Path) -> dict:
    path = _overrides_path(config_path)
    return (yaml.safe_load(path.read_text()) or {}).get("model_capabilities", {}) if path.exists() else {}


def test_the_plan_names_the_model_is_tested_first_and_needs_an_administrator(lead) -> None:
    agent, _config_path = lead

    plan = stage(160)

    [change] = plan["changes"]
    assert change["actual_path"] == "model_capabilities.litellm.old/model.max_tools_per_request"
    assert change["previous_value"] == 128 and change["value"] == 160
    assert plan["decision_class"] == "lead_managed", "not user_authorized: the asking person cannot approve it"
    assert agent.provider_model.sent == [160], "one test request before anyone is asked"
    assert plan["model_test"]["tools_sent"] == 160
    assert "accepted a test request with 160 tools" in plan["instructions"]
    proposal = RuntimeSettingsService(agent).describe(plan["change_id"])
    assert proposal.owner_person_id is None
    assert not is_model_choice(RuntimeSettingsService(agent).load_plan(plan["change_id"]).changes)
    assert proposal.summary.startswith("Raise the tool cap of old/model via litellm from 128 to 160")
    assert any("Every agent that uses old/model via litellm" in line for line in proposal.effect)


def test_an_agreed_raise_is_tested_again_then_saved_for_that_model(lead) -> None:
    agent, config_path = lead
    plan = stage(160)

    result = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))

    assert not result.get("error"), result
    assert agent.provider_model.sent == [160, 160], "tested at staging and again right before writing"
    assert saved_caps(config_path)["litellm"]["old/model"]["max_tools_per_request"] == 160
    assert tool_limit(agent) == (160, "litellm", "old/model"), "the running agent uses it at once"
    assert result["model_tool_limit"] == {"provider": "litellm", "model_id": "old/model", "max_tools_per_request": 160}
    assert result["live_applied"] == ["model_capabilities.litellm.old/model.max_tools_per_request"]


def test_a_model_that_refuses_that_many_tools_stages_nothing(lead) -> None:
    agent, config_path = lead
    agent.provider_model = _Model(accepts=128)

    plan = stage(160)

    assert "change_id" not in plan
    assert "Nothing was staged" in plan["error"] and "refused a request with 160 tools (RuntimeError)" in plan["error"]
    assert saved_caps(config_path) == {} and tool_limit(agent)[0] == 128


def test_a_model_that_refuses_at_apply_time_keeps_its_cap(lead) -> None:
    agent, config_path = lead
    plan = stage(160)
    agent.provider_model = _Model(accepts=128)

    result = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))

    assert result["phase"] == "preflight" and "refused a request with 160 tools" in result["error"]
    assert saved_caps(config_path) == {} and tool_limit(agent)[0] == 128


def test_a_plan_for_a_model_the_agent_no_longer_uses_writes_nothing(lead) -> None:
    agent, config_path = lead
    plan = stage(160)
    agent.model_id = "new/model"

    result = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))

    assert "another model" in result["error"] and agent.provider_model.sent == [160]
    assert saved_caps(config_path) == {}


def test_it_only_raises_the_cap(lead) -> None:
    agent, _config_path = lead

    same = stage(128)
    lower = stage(5)

    assert "only raises" in same["error"] and "only raises" in lower["error"]
    assert agent.provider_model.sent == [], "nothing is tested for a refused value"


def test_a_persons_own_model_cannot_be_raised_this_way(lead) -> None:
    agent, config_path = lead
    person_model_state(agent).active = SimpleNamespace(provider="openai", profile=SimpleNamespace(model_id="own/model"))

    plan = stage(160)

    assert "change_id" not in plan
    assert "a person's own model (own/model via openai)" in plan["error"]
    assert agent.provider_model.sent == [] and saved_caps(config_path) == {}


def test_team_lead_raises_the_cap_of_a_workers_model(lead) -> None:
    agent, config_path = lead
    _overrides_path(config_path).parent.mkdir(parents=True, exist_ok=True)
    _overrides_path(config_path).write_text(
        "agent_types:\n  Researcher:\n    provider: litellm\n    model_id: worker/model\n", encoding="utf-8"
    )

    plan = stage(160, target_agent_type="Researcher")
    result = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))

    assert plan["changes"][0]["actual_path"] == "model_capabilities.litellm.worker/model.max_tools_per_request"
    assert agent.tested_models == ["litellm/worker/model", "litellm/worker/model"], "the worker's model is tested"
    assert not result.get("error"), result
    assert saved_caps(config_path) == {"litellm": {"worker/model": {"max_tools_per_request": 160}}}
    assert tool_limit(agent)[0] == 128, "TeamLead's own model keeps its cap"


def test_the_cap_is_staged_on_its_own_for_one_model_and_within_range(lead) -> None:
    mixed = json.loads(
        plan_runtime_settings_change._tool_func(
            changes=[{"path": PATH, "value": 160}, {"path": "current_agent.llm.supports_image_input", "value": True}],
            reason="Both at once",
        )
    )
    two_models = json.loads(
        plan_runtime_settings_change._tool_func(
            changes=[{"path": PATH, "value": 160}, {"path": PATH, "value": 160, "target_agent_type": "Researcher"}],
            reason="Two at once",
        )
    )
    zero = stage(0)

    assert "in its own plan, for one model" in mixed["error"]
    assert "in its own plan, for one model" in two_models["error"]
    assert "change_id" not in zero


def test_a_failed_receipt_puts_back_the_file_and_the_running_cap(lead, monkeypatch) -> None:
    import custom_agents.runtime_settings_service as service_module

    agent, config_path = lead
    plan = stage(160)
    real_write = service_module.atomic_write_bytes

    def no_receipts(path, content, **kwargs):
        if kwargs.get("immutable"):
            raise OSError("disk full")
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(service_module, "atomic_write_bytes", no_receipts)

    with pytest.raises(OSError, match="disk full"):
        _apply_settings_change(plan["change_id"], confirmed=True)

    assert saved_caps(config_path) == {}
    assert tool_limit(agent)[0] == 128


def test_with_authorization_on_only_an_administrator_approves_the_raise(lead) -> None:
    from strands.interrupt import InterruptException

    from access_control.entities import PersonPrincipal
    from custom_agents import access_runtime
    from custom_messages.contracts.access_v2 import AccessReviewChoice
    from custom_tools.providers.changes import apply_change
    from custom_tools.runtime_context import _agent_ctx
    from tests.test_runtime_settings_tools import CEDAR_BINARY, _authorization_on, _PausingToolCall

    if not CEDAR_BINARY.is_file():
        pytest.skip("build ua-cedar-authorizer first (cargo build)")
    agent, _config_path = lead
    _authorization_on(agent)
    call = _PausingToolCall()
    try:
        plan = stage(160)
        with pytest.raises(InterruptException):
            asyncio.run(apply_change._tool_func(plan["change_id"], "More tools at once", call))
        assert agent.provider_model.sent == [160], "only the staging test before a person decides"
        requests = access_runtime.runtime_requests(agent)
        request = requests.service.store.get_request(call.reasons[0]["access_request_id"])
        asker_as_member = PersonPrincipal("user_1", "test_team", ("member",))
        assert not requests.may_decide(asker_as_member, request), "the person who asked cannot approve it"
        requests.decide(request.request_id, PersonPrincipal("admin_1", "test_team", ("owner",)), AccessReviewChoice.APPROVE)
        call.response = {"kind": "access_request_decision", "status": "granted"}
        applied = tool_json(asyncio.run(apply_change._tool_func(plan["change_id"], "More tools at once", call)))
    finally:
        _agent_ctx.set(None)

    assert request.change.owner_person_id is None
    assert applied["status"] == "applied", applied
    assert agent.provider_model.sent == [160, 160]
    assert tool_limit(agent)[0] == 160


@pytest.mark.parametrize(
    ("error", "words"),
    [
        (ModelThrottledException("slow down"), "limiting requests right now"),
        (ContextWindowOverflowException("too long"), "do not fit in the model's context window"),
        (ConnectionError("reset"), "refused a request with 3 tools (ConnectionError)"),
    ],
)
def test_the_test_request_says_why_it_failed(error, words) -> None:
    with pytest.raises(RuntimeModelSwitchProbeError, match=words.replace("(", r"\(").replace(")", r"\)")):
        probe_tool_capacity(_Model(accepts=10, error=error), tool_count=3, timeout_seconds=5)


def test_a_worker_at_its_cap_is_told_to_ask_team_lead() -> None:
    worker = _higher_cap_hint(SimpleNamespace(is_team_lead=False, named_agent_type="Researcher"), "worker/model")
    team_lead = _higher_cap_hint(SimpleNamespace(is_team_lead=True), "old/model")

    assert "say so in your result" in worker and "worker/model for Researcher" in worker
    assert "plan_runtime_setting_change" not in worker
    assert "plan_runtime_setting_change(path='model.max_tools_per_request'" in team_lead
