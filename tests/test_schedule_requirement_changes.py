"""Changing a scheduled job's requirements with configure_scheduled_worker (live report).

TeamLead created a job whose Google requirement named the function
(``cli:google`` / ``secure_cli``), which is not one of the tool's operations.
It then called configure_scheduled_worker three times with ``gmail_search`` and
``gmail_read``: each call said configured=true and advanced the revision, but
inspect_scheduled_worker and prepare_scheduled_job_access kept showing
``secure_cli``. The saved policy had automatic startup off, and that save
path copied the old record without the new requirements. These tests call
the real tools in that order against real stores.
"""

import json
from datetime import timedelta

import pytest

from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.schedule_worker_v1 import utc_now
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass
from custom_tools.providers.schedule_workers import (
    REQUIREMENTS_SHAPE,
    configure_scheduled_worker,
    inspect_scheduled_worker,
    install_scheduled_worker_draft,
    prepare_scheduled_job_access,
)
from custom_tools.runtime_context import set_agent_ref
from custom_tools.secure_cli_tool import secure_cli
from tests.test_schedule_worker_service import lead  # noqa: F401  (fixture)

WORKER = "Worker"
BY_FUNCTION_NAME = {"requirements": [{"capability_id": "cli:google", "operation": "secure_cli"}]}
GMAIL = {
    "requirements": [
        {"capability_id": "cli:google", "operation": "gmail_search", "necessity": "required"},
        {"capability_id": "cli:google", "operation": "gmail_read", "necessity": "required"},
    ]
}
GMAIL_PAIRS = [("cli:google", "gmail_search"), ("cli:google", "gmail_read")]
GMAIL_SAVED = "Saved requirements: cli:google gmail_search, cli:google gmail_read."
STARTUP = {"startup_off": json.dumps({"enabled": False}), "startup_on": ""}


def _call(tool, **kwargs) -> dict:
    return json.loads(tool._tool_func(**kwargs))


def _saved_pairs(agent, schedule_id: str) -> list[tuple[str, str]]:
    binding = agent._schedule_store.worker_records.get_binding(schedule_id, team_id="team")
    return [(item.capability_id, item.operation) for item in binding.capabilities.requirements]


def _create_job(agent, *, worker_policy_json: str) -> dict:
    """What create_scheduled_job does with target_agent_type and requirements_json."""

    schedule = ScheduledMessage(
        schedule_id="gmail",
        schedule_name="Gmail check",
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Check email."),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="owner")),
        claim_team_id="team",
        interval_ms=3 * 3600 * 1000,
        next_fire_at=(utc_now() + timedelta(hours=3)).isoformat(),
    )
    return install_scheduled_worker_draft(
        agent,
        agent._schedule_store,
        schedule,
        target_agent_type=WORKER,
        worker_policy_json=worker_policy_json,
        requirements_json=json.dumps(BY_FUNCTION_NAME),
    )


@pytest.fixture
def worker_with_broker(lead):  # noqa: F811
    lead.definitions[WORKER]["tools"] = [secure_cli]
    set_agent_ref(lead)
    return lead


@pytest.mark.parametrize("startup", sorted(STARTUP))
def test_configure_replaces_a_wrong_requirement_and_activates(worker_with_broker, startup):
    agent = worker_with_broker
    created = _create_job(agent, worker_policy_json=STARTUP[startup])
    assert created["enabled"] is False
    assert created["readiness"]["checks"][0]["reason"] == "not_in_tool_catalog"
    assert "Ready: no, blocked by cli:google secure_cli (not_in_tool_catalog)" in created["summary"]

    result = _call(
        configure_scheduled_worker,
        schedule_id="gmail",
        target_agent_type=WORKER,
        expected_revision=created["policy_revision"],
        requirements_json=json.dumps(GMAIL),
        activate_schedule=True,
    )

    assert result["configured"] is True and result["enabled"] is True, result
    assert _saved_pairs(agent, "gmail") == GMAIL_PAIRS
    assert [
        (item["capability_id"], item["operation"]) for item in result["saved_requirements"]["requirements"]
    ] == GMAIL_PAIRS
    assert result["summary"].startswith(GMAIL_SAVED) and "Ready: yes" in result["summary"], result["summary"]
    # Every later reader sees the saved requirements, not the ones from creation.
    inspection = _call(inspect_scheduled_worker, schedule_id="gmail")
    prepared = _call(prepare_scheduled_job_access, schedule_id="gmail")
    for readiness in (inspection["readiness"], prepared):
        assert [(item["capability_id"], item["operation"]) for item in readiness["checks"]] == GMAIL_PAIRS
        assert readiness["status"] != "blocked"
    assert inspection["schedule_enabled"] is True
    assert inspection["automation_enabled"] is (startup == "startup_on")


def test_configure_without_a_saved_policy_saves_the_requirements(worker_with_broker):
    """The fixture's job has no worker policy yet: the first save uses revision 0."""

    result = _call(
        configure_scheduled_worker,
        schedule_id="job",
        target_agent_type=WORKER,
        expected_revision=0,
        requirements_json=json.dumps(GMAIL),
    )

    assert result["policy_revision"] == 1 and result["enabled"] is False
    assert _saved_pairs(worker_with_broker, "job") == GMAIL_PAIRS
    assert result["summary"] == (
        f"{GMAIL_SAVED} Ready: yes, provisionally (some needs are checked when the tool runs). "
        "Schedule on: no. Automatic worker startup: on."
    )


def test_turning_startup_off_without_requirements_keeps_the_saved_ones(worker_with_broker):
    agent = worker_with_broker
    created = _create_job(agent, worker_policy_json="")
    fixed = _call(
        configure_scheduled_worker, schedule_id="gmail", target_agent_type=WORKER,
        expected_revision=created["policy_revision"], requirements_json=json.dumps(GMAIL),
    )

    off = _call(
        configure_scheduled_worker, schedule_id="gmail", target_agent_type=WORKER,
        expected_revision=fixed["policy_revision"], worker_policy_json=json.dumps({"enabled": False}),
    )

    assert off["automation_enabled"] is False
    assert _saved_pairs(agent, "gmail") == GMAIL_PAIRS
    assert off["summary"].startswith(GMAIL_SAVED) and off["summary"].endswith("Automatic worker startup: off.")


def test_a_still_wrong_requirement_is_saved_but_does_not_activate(worker_with_broker):
    """Activation is decided on exactly the requirements that are saved."""

    agent = worker_with_broker
    created = _create_job(agent, worker_policy_json=STARTUP["startup_off"])

    result = _call(
        configure_scheduled_worker, schedule_id="gmail", target_agent_type=WORKER,
        expected_revision=created["policy_revision"], requirements_json=json.dumps(BY_FUNCTION_NAME),
        activate_schedule=True,
    )

    assert result["enabled"] is False
    assert "Ready: no, blocked by cli:google secure_cli (not_in_tool_catalog)" in result["summary"]
    assert agent._schedule_store.get_scheduled_message("gmail").enabled is False


@pytest.mark.parametrize(
    "wrong_shape",
    [
        json.dumps(GMAIL["requirements"]),  # the list without its object
        json.dumps({"capabilities": GMAIL["requirements"]}),
        json.dumps({"requirements": [{"tool_id": "cli:google", "operation": "gmail_read"}]}),
    ],
)
def test_a_wrong_requirements_shape_is_an_error_and_saves_nothing(worker_with_broker, wrong_shape):
    agent = worker_with_broker
    created = _create_job(agent, worker_policy_json="")

    result = _call(
        configure_scheduled_worker, schedule_id="gmail", target_agent_type=WORKER,
        expected_revision=created["policy_revision"], requirements_json=wrong_shape,
    )

    assert "requirements_json is not valid" in result["error"] and REQUIREMENTS_SHAPE in result["error"]
    binding = agent._schedule_store.worker_records.get_binding("gmail", team_id="team")
    assert binding.revision == created["policy_revision"]
    assert _saved_pairs(agent, "gmail") == [("cli:google", "secure_cli")]
