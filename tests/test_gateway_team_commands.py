"""How the gateway sends a person's command to the team services and explains what went wrong.

assistant_gateway.application.team_commands.GatewayTeamCommandService: each command has a
fresh request id from the injected generator; when no answer comes in time, the same command
is sent once more under the same request id (the team services answer a copy already carried
out from its stored reply), and after that the person is told whether a change may still be
made. assistant_gateway.team_services_client.MessageHostTeamServices turns the message host's
errors into the application's, with messages written for people. The signed path over NATS is
in tests/test_live_team_memory.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from assistant_gateway.application.team_commands import (
    GatewayTeamCommandService,
    TeamCommandRefused,
    TeamServicesNoAnswer,
    TeamServicesUnavailable,
)
from assistant_gateway.message_host import GatewayHostUnavailable
from assistant_gateway.team_services_client import MessageHostTeamServices
from custom_transport.protocol1.live import NO_ANSWER_IN_TIME, NO_RESPONDERS, RequestRefused, ServiceUnavailable

PERSON, REFERENCE = "Ada Admin", "p-AdaAdaAdaAdaAdaAdaAdaA"
REQUEST_ID = "019a5c3e-8f00-7000-8000-000000000001"
CREATE = {"bank": "usermem", "fact_text": {"type": "inline", "text": "Likes tea."}}
OK = {"outcome": "ok", "facts": [], "total": 0}


class TeamServices:
    """The team services port: answers each send with the next of ``answers`` (an exception is raised)."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.sent: list[tuple[str, dict[str, Any], str, str]] = []

    async def command(self, kind: str, body: dict[str, Any], *, person_ref: str, request_id: str) -> dict[str, Any]:
        self.sent.append((kind, body, person_ref, request_id))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class References:
    def person_ref(self, person_id: str) -> str | None:
        return REFERENCE if person_id == PERSON else None


def service(team_services: TeamServices, ids: list[str]) -> GatewayTeamCommandService:
    return GatewayTeamCommandService(team_services=team_services, references=References(),
                                     request_ids=lambda: ids.pop(0))


@pytest.mark.asyncio
async def test_each_command_gets_its_request_id_from_the_injected_generator() -> None:
    team_services = TeamServices(OK, OK)
    commands = service(team_services, [REQUEST_ID, "019a5c3e-8f00-7000-8000-000000000002"])
    await commands.request("memory.create", CREATE, person_id=PERSON)
    await commands.request("memory.create", CREATE, person_id=PERSON)
    assert [(body["request_id"], request_id) for _, body, _, request_id in team_services.sent] == [
        (REQUEST_ID, REQUEST_ID), ("019a5c3e-8f00-7000-8000-000000000002", "019a5c3e-8f00-7000-8000-000000000002")]
    assert team_services.sent[0][2] == REFERENCE


@pytest.mark.asyncio
async def test_a_command_with_no_answer_in_time_is_sent_once_more_under_the_same_request_id(caplog) -> None:
    team_services = TeamServices(TeamServicesNoAnswer("no answer in time"), OK)
    ids = [REQUEST_ID, "unused"]
    assert await service(team_services, ids).request("memory.create", CREATE, person_id=PERSON) == OK
    first, second = team_services.sent
    assert first == second == ("memory.create", {"request_id": REQUEST_ID, **CREATE}, REFERENCE, REQUEST_ID)
    assert ids == ["unused"]  # one id for both copies
    assert "event=gateway_team_command_unanswered" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, body, says, never_says", [
    ("memory.create", CREATE, "the change may still be made", None),
    ("memory.list", {"bank": "usermem", "sort": "memory_id", "offset": 0, "limit": 10}, "Try again",
     "may still be made"),
])
async def test_after_two_sends_without_an_answer_the_person_is_told_what_may_have_happened(
        kind: str, body: dict, says: str, never_says: str | None) -> None:
    team_services = TeamServices(TeamServicesNoAnswer("no answer"), TeamServicesNoAnswer("no answer"), OK)
    with pytest.raises(TeamServicesNoAnswer) as unanswered:
        await service(team_services, [REQUEST_ID]).request(kind, body, person_id=PERSON)
    assert says in str(unanswered.value)
    assert never_says is None or never_says not in str(unanswered.value)
    assert len(team_services.sent) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    TeamServicesUnavailable("The team services are not running right now."),
    TeamCommandRefused("memory.create", "bad_authority"),
])
async def test_other_errors_are_not_sent_again(error: Exception) -> None:
    team_services = TeamServices(error, OK)
    with pytest.raises(type(error)):
        await service(team_services, [REQUEST_ID]).request("memory.create", CREATE, person_id=PERSON)
    assert len(team_services.sent) == 1


# -- the message host's errors, in words for people ---------------------------------------------------------

class Host:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def command(self, kind: str, body: dict[str, Any], **_options: Any) -> Any:
        raise self.error


async def send(error: Exception) -> None:
    client = MessageHostTeamServices(Host(error), timeout_seconds=30)  # type: ignore[arg-type]
    await client.command("memory.create", {"request_id": REQUEST_ID, **CREATE}, person_ref=REFERENCE,
                         request_id=REQUEST_ID)


@pytest.mark.asyncio
async def test_no_answer_in_time_may_still_be_carried_out_and_no_responders_is_not() -> None:
    with pytest.raises(TeamServicesNoAnswer):
        await send(ServiceUnavailable("memory.create", "ua.v1.team_one.svc.team", NO_ANSWER_IN_TIME))
    with pytest.raises(TeamServicesUnavailable) as not_running:
        await send(ServiceUnavailable("memory.create", "ua.v1.team_one.svc.team", NO_RESPONDERS))
    assert not isinstance(not_running.value, TeamServicesNoAnswer)
    assert "not running" in str(not_running.value) and NO_RESPONDERS not in str(not_running.value)
    with pytest.raises(TeamServicesUnavailable) as connecting:
        await send(GatewayHostUnavailable("the gateway is not a protocol 1 host yet"))
    assert "still connecting" in str(connecting.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["unsupported", "handler_failed", "bad_authority", "not_yet_valid"])
async def test_a_refused_request_is_explained_in_plain_words(code: str) -> None:
    with pytest.raises(TeamCommandRefused) as refused:
        await send(RequestRefused("memory.create", code, "/authority"))
    message = str(refused.value)
    assert refused.value.code == code and refused.value.kind == "memory.create"
    assert message.startswith("The team services ") and message.endswith(".")
    assert message != code and "refused memory.create" not in message and "/authority" not in message
    assert len(message.replace(code, "").split()) >= 8  # a sentence, not the code alone


@pytest.mark.asyncio
async def test_a_gateway_without_a_message_host_says_how_to_start_one() -> None:
    client = MessageHostTeamServices(None, timeout_seconds=30)
    with pytest.raises(TeamServicesUnavailable, match="launcher"):
        await client.command("memory.create", {}, person_ref=REFERENCE, request_id=REQUEST_ID)
