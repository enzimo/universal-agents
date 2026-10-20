"""Administrators' Status page: what is found, from where, and who may see it.

Covered: the launcher lists operator variables that override saved settings
and model keys (names only); the gateway turns them, the broker's model key
status, agents' heartbeat findings and its own configuration into findings,
problems first; a broker that does not answer is a problem, not silence; a
configured endpoint with no key from anywhere is a problem, one with an
environment key is not; a fixed rejection disappears; administrators get one
notice per set of problems; agents report MCP servers that do not load and
degraded memory search; only administrators read the page.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.status_page import StatusPage
from custom_agents.mcp_server_tools import LoadFailure, ServerFailure
from custom_agents.status_findings import agent_status_findings
from custom_messages.contracts.access_v2 import AccessNoticeKind
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.status_v1 import EnvironmentOverridesV1, StatusFindingV1, StatusSeverity
from custom_messages.enums import MessageMetaType
from docker.launch_headless import build_instance_env_defaults, build_process_env
from docker.settings_environment import SettingsEnvironment, environment_overrides, write_environment_overrides

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
BIFROST = "https://bifrost.example.com/v1"


def _config(tmp_path: Path, *, authorization: bool = True) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="status", instance_root=str(tmp_path / "instance"), team_id="team_status", timezone="UTC"
    )
    config.llm.provider, config.llm.model_id, config.llm.litellm_api_base = "litellm", "openai/gpt-6-luna", BIFROST
    config.authorization.enabled = authorization
    return config


def _lead(findings: list[StatusFindingV1] | None = None) -> AgentStatus:
    return AgentStatus(
        self_id="lead-1", team_id="team_status", named_agent_type="TeamArchitect", name="Nova", is_paused=False, agent_role="lead",
        is_team_lead=True, status_type=MessageMetaType.RESPONSE, status_findings=findings or [],
    )


class _Notices:
    def __init__(self) -> None:
        self.stored: dict[str, object] = {}

    def __call__(self, notice) -> bool:
        if notice.notice_id in self.stored:
            return False
        self.stored[notice.notice_id] = notice
        return True


def _page(config, *, keys: dict | Exception | None = None, agents=None, notices=None) -> StatusPage:
    async def model_key_status() -> dict:
        if isinstance(keys, Exception):
            raise keys
        return keys if keys is not None else {"endpoints": []}

    return StatusPage(
        config=config,
        model_key_status=model_key_status,
        agent_statuses=lambda: [_lead()] if agents is None else agents,
        send_notice_once=notices or _Notices(),
        now=lambda: NOW,
    )


def _ids(report) -> list[str]:
    return [finding.finding_id for finding in report.findings]


@pytest.mark.asyncio
async def test_a_configured_endpoint_without_any_key_is_a_problem(tmp_path) -> None:
    report = await _page(_config(tmp_path)).report()

    assert _ids(report) == ["model_key_missing:https://bifrost.example.com"]
    assert report.findings[0].severity is StatusSeverity.PROBLEM
    assert "/auth model" in report.findings[0].action


@pytest.mark.asyncio
async def test_an_environment_key_is_a_warning_and_the_endpoint_is_not_missing(tmp_path) -> None:
    config = _config(tmp_path)
    write_environment_overrides(
        config.layout.environment_overrides_path,
        EnvironmentOverridesV1(written_at=NOW, setting_variables=("LITELLM_API_BASE",), model_key_variables=("LITELLM_API_KEY",)),
    )

    report = await _page(config).report()

    assert _ids(report) == [
        "environment_overrides_setting:LITELLM_API_BASE",
        "environment_overrides_model_key:LITELLM_API_KEY",
    ]
    assert {finding.severity for finding in report.findings} == {StatusSeverity.WARNING}
    assert "secret" not in config.layout.environment_overrides_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_rejected_key_is_a_problem_until_a_new_key_is_saved(tmp_path) -> None:
    rejected_at = NOW - timedelta(minutes=5)
    endpoint = {
        "address": "https://bifrost.example.com", "kind": "openai_compatible", "state": "stored",
        "saved_at": (rejected_at - timedelta(days=1)).isoformat(),
        "last_rejection": {"caller": "manager", "source": "stored", "status_code": 401, "rejected_at": rejected_at.isoformat()},
    }

    rejected = await _page(_config(tmp_path), keys={"endpoints": [endpoint]}).report()
    endpoint["saved_at"] = NOW.isoformat()
    replaced = await _page(_config(tmp_path), keys={"endpoints": [endpoint]}).report()

    assert _ids(rejected) == ["model_key_rejected:https://bifrost.example.com"]
    assert "no restart is needed" in rejected.findings[0].action
    assert _ids(replaced) == []


@pytest.mark.asyncio
async def test_a_broker_that_does_not_answer_is_a_problem_and_listed_as_unavailable(tmp_path) -> None:
    report = await _page(_config(tmp_path), keys=RuntimeError("connection refused")).report()

    assert report.unavailable_sources == ("secure tool broker",)
    assert _ids(report) == ["broker_unreachable"]


@pytest.mark.asyncio
async def test_agent_findings_are_shown_once_and_a_silent_team_lead_is_a_problem(tmp_path) -> None:
    finding = StatusFindingV1(
        finding_id="mcp_server_not_loaded:mcp:weather", severity=StatusSeverity.PROBLEM, title="weather down", source="TeamArchitect"
    )
    worker = _lead([finding]).model_copy(update={"self_id": "worker-1", "is_team_lead": False})
    keys = {"endpoints": [{"address": "https://bifrost.example.com", "state": "stored"}]}

    reported = await _page(_config(tmp_path), keys=keys, agents=[_lead([finding]), worker]).report()
    silent = await _page(_config(tmp_path), keys=keys, agents=[]).report()

    assert _ids(reported) == ["mcp_server_not_loaded:mcp:weather"]
    assert _ids(silent) == ["team_lead_not_reporting"]


@pytest.mark.asyncio
async def test_authorization_off_is_a_note_after_problems_and_warnings(tmp_path) -> None:
    report = await _page(_config(tmp_path, authorization=False)).report()

    assert [finding.severity for finding in report.findings] == [StatusSeverity.PROBLEM, StatusSeverity.NOTE]


@pytest.mark.asyncio
async def test_checks_between_a_persons_requests_turned_off_are_a_note(tmp_path) -> None:
    config = _config(tmp_path)
    config.agent.request_conflict_checks = False

    report = await _page(config).report()

    off = [finding for finding in report.findings if finding.finding_id == "request_conflict_checks_off"]
    assert [finding.severity for finding in off] == [StatusSeverity.NOTE]
    assert "agent.request_conflict_checks: true" in off[0].action
    assert "request_conflict_checks_off" not in _ids(await _page(_config(tmp_path)).report())


@pytest.mark.asyncio
async def test_administrators_get_one_notice_per_set_of_problems(tmp_path, monkeypatch) -> None:
    from tests.loop_storage_support import record_loop_opens

    notices = _Notices()
    page = _page(_config(tmp_path), notices=notices)

    # The check runs every five minutes on the gateway's loop; its reads run off it.
    with record_loop_opens(monkeypatch) as loop_opens:
        await page.notify_new_problems()
        await page.notify_new_problems()
    assert loop_opens.where == [], loop_opens.report()

    [notice] = notices.stored.values()
    assert notice.kind is AccessNoticeKind.STATUS_PROBLEMS and notice.recipient_roles == ("admin",)
    assert "/webchat?view=status" in notice.summary


def test_the_launcher_lists_overriding_variables_by_name_only(tmp_path) -> None:
    config = _config(tmp_path)
    saved = config.save()
    ambient = {"PATH": os.environ.get("PATH", "/usr/bin"), "LITELLM_API_BASE": "https://other.example.com/v1", "LITELLM_API_KEY": "sk-secret-value-0001"}
    with mock.patch.dict(os.environ, ambient, clear=True):
        loaded = GatewayInstanceConfig.load_for_process(saved)
        runtime_env = build_process_env(loaded, env_path=None)
        settings = SettingsEnvironment.at_launch(
            loaded, defaults=build_instance_env_defaults, launch_env=runtime_env, operator_keys=frozenset(os.environ)
        )
        overrides = environment_overrides(settings, config=GatewayInstanceConfig.load_saved(saved), launch_env=runtime_env)

    assert "LITELLM_API_BASE" in overrides.setting_variables
    assert overrides.model_key_variables == ("LITELLM_API_KEY",)
    assert "sk-secret" not in overrides.model_dump_json()


def test_agents_report_mcp_servers_that_do_not_load_and_degraded_memory_search() -> None:
    failure = ServerFailure(
        kind=LoadFailure.GAVE_UP, connection_id="c1", record_digest="d", first_failed_at=NOW, last_failed_at=NOW,
        error="connection refused", tries=3, next_try_at=None,
    )
    agent = SimpleNamespace(
        named_agent_type="TeamArchitect", team_id="team_status", is_team_lead=True,
        _memory_graph_degraded_reason="embedding backend is unavailable",
        _skill_catalog_runtime_state=SimpleNamespace(last_error=""),
        _mcp_server_tools=SimpleNamespace(failures=lambda: {"mcp:weather": failure}),
    )

    findings = {finding.finding_id: finding for finding in agent_status_findings(agent)}

    assert findings["mcp_server_not_loaded:mcp:weather"].severity is StatusSeverity.PROBLEM
    assert findings["memory_graph_degraded:TeamArchitect"].severity is StatusSeverity.WARNING
    assert json.loads(AgentStatus.model_validate(_lead(list(findings.values())).model_dump()).model_dump_json())["status_findings"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("roles", "allowed"), [(("member",), False), (("admin",), True), (("owner",), True)])
async def test_only_administrators_read_the_status_route(tmp_path, roles, allowed) -> None:
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from assistant_gateway.interactive_apps.website_handlers.status import WebsiteStatusHandlers

    page = _page(_config(tmp_path))
    application = SimpleNamespace(
        access_inbox=SimpleNamespace(person=lambda person_id: SimpleNamespace(roles=roles)), status=page
    )
    operations = SimpleNamespace(
        _require_permission_browser_session=lambda request: SimpleNamespace(canonical_user_id="usr_alice"),
        _permission_json_response=lambda body, status=200: web.json_response(body, status=status),
    )
    handlers = WebsiteStatusHandlers(application=application, operations=operations)  # type: ignore[arg-type]

    access = json.loads((await handlers.access(make_mocked_request("GET", "/api/web-chat/status/access"))).text)
    response = await handlers.report(make_mocked_request("GET", "/api/web-chat/status?offset=0"))
    body = json.loads(response.text)

    assert access["allowed"] is allowed
    if not allowed:
        assert response.status == 403 and "administrators" in body["error"]
        return
    assert response.status == 200
    assert body["total"] == 1 and body["counts"] == {"problem": 1, "warning": 0, "note": 0}
    assert body["findings"][0]["finding_id"] == "model_key_missing:https://bifrost.example.com"


@pytest.mark.asyncio
async def test_well_known_providers_in_use_need_their_own_key(tmp_path) -> None:
    config = _config(tmp_path)
    config.llm.provider, config.llm.model_id = "anthropic", "claude-sonnet-4-5"
    config.save()

    missing = await _page(config).report()
    write_environment_overrides(
        config.layout.environment_overrides_path,
        EnvironmentOverridesV1(written_at=NOW, model_key_variables=("ANTHROPIC_API_KEY",)),
    )
    from_environment = await _page(config).report()

    assert "model_key_missing:https://api.anthropic.com" in _ids(missing)
    assert "model_key_missing:https://api.anthropic.com" not in _ids(from_environment)


def test_people_without_their_own_model_are_a_warning_and_own_models_a_note() -> None:
    from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
    from custom_messages.contracts.person_settings_v1 import PersonModelProfileV1, PersonSettingsV1
    from assistant_gateway.status_page import person_settings_findings
    from model_providers.endpoints import EndpointKind

    at = datetime(2026, 10, 2, tzinfo=timezone.utc)
    people = [
        TeamPersonV1(team_id="t", person_id=person, standing=standing, changed_at_utc=at)
        for person, standing in (
            ("usr_alice", PersonStanding.ACTIVE),
            ("usr_bob", PersonStanding.ACTIVE),
            ("usr_gone", PersonStanding.REMOVED),
        )
    ]
    own = PersonModelProfileV1(kind=EndpointKind.OPENAI_COMPATIBLE, base_url="https://bifrost.example.com/v1", model_id="openai/gpt-6-luna", tested_at_utc=at)
    settings = [
        PersonSettingsV1(team_id="t", person_id="usr_alice", uses_team_keys=False, changed_at_utc=at),
        PersonSettingsV1(team_id="t", person_id="usr_bob", uses_team_keys=False, own_model=own, changed_at_utc=at),
        PersonSettingsV1(team_id="t", person_id="usr_gone", uses_team_keys=False, changed_at_utc=at),
    ]

    warning, note = person_settings_findings(people, settings)

    assert warning.severity is StatusSeverity.WARNING and "usr_alice" in warning.summary and "usr_gone" not in warning.summary
    assert note.severity is StatusSeverity.NOTE and "usr_bob: openai/gpt-6-luna at bifrost.example.com" in note.summary


def test_a_key_only_in_team_env_is_reported_with_that_file_and_how_to_remove_it(tmp_path) -> None:
    """The case an operator reported: docker exec shows no LITELLM_API_KEY, but team.env has it.

    The launcher reads team.env and passes its values only to the processes it
    starts, so the warning must name that file, not "the environment".
    """

    from docker.launch_headless import load_env_file_values

    config = _config(tmp_path)
    saved = config.save()
    team_env = tmp_path / "team.env"
    secret = tmp_path / "embedding_key"
    secret.write_text("sk-embedding-secret-0002")
    team_env.write_text(f"LITELLM_API_KEY=sk-from-team-env-0001\nEMBEDDING_API_KEY_FILE={secret}\n")
    container = {"PATH": os.environ.get("PATH", "/usr/bin")}
    with mock.patch.dict(os.environ, container, clear=True):
        loaded = GatewayInstanceConfig.load_for_process(saved)
        file_values = load_env_file_values(team_env)
        runtime_env = {**build_process_env(loaded, env_path=None), **file_values}
        settings = SettingsEnvironment.at_launch(
            loaded, defaults=build_instance_env_defaults, launch_env=runtime_env,
            operator_keys=frozenset(file_values) | frozenset(os.environ),
        )
        overrides = environment_overrides(
            settings, config=GatewayInstanceConfig.load_saved(saved), launch_env=runtime_env,
            env_file=team_env, env_file_values=file_values, container_env=os.environ,
        )
    write_environment_overrides(config.layout.environment_overrides_path, overrides)

    assert "LITELLM_API_KEY" not in container
    findings = {item.finding_id: item for item in _override_findings_for(overrides)}
    key = findings["environment_overrides_model_key:LITELLM_API_KEY"]
    assert f"the instance's env file {team_env}" in key.summary and "docker exec does not show" in key.summary
    assert key.action == f"To use saved keys: Delete the LITELLM_API_KEY line from {team_env}, then restart the container."
    embedding = findings["environment_overrides_model_key:EMBEDDING_API_KEY"]
    assert f"the secret file {secret}, named by EMBEDDING_API_KEY_FILE" in embedding.summary
    assert "sk-from-team-env" not in overrides.model_dump_json() and "sk-embedding" not in overrides.model_dump_json()


def _override_findings_for(overrides):
    from assistant_gateway.status_page import _override_findings

    return _override_findings(overrides)
