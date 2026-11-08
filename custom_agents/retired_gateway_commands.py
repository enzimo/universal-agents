"""Answer gateway commands queued before an in-place upgrade whose handler is gone.

Why this exists: phase 2 of the message contract moved people's memory commands
(``/coremem``, ``/usermem`` and the Settings memory page) from a durable
``ManagerMemoryCommand``, and the Web Chat Knowledge page's requests from a
``ManagerKnowledgeCommand``, in the team lead's control queue to protocol 1
request/reply at the team services (``team_services.memory``,
``team_services.knowledge``). A command a 0.9
gateway queued just before the instance was upgraded in place can still be
waiting in that queue when 0.10 starts. Plan 13B.10 requires that the person
gets an answer, not silence.

What the team lead does: the envelope decodes as a ``RetiredGatewayCommand``
(``custom_messages.contracts.control_v1``), so it is no longer set aside as a
dead letter. The lead never carries it out. It tells the person, in the chat the
command came from, that the command was not carried out and should be sent
again, and logs ``event=retired_gateway_command_answered``.

Why a chat message and not the old response: the gateway that sent the command
restarted with the upgrade, and the 0.10 gateway drops a manager control
response nobody waits for (``assistant_gateway.application.manager_client``,
``event=gateway_unmatched_manager_control_response_dropped``). A
``UserNotification`` to the gateway's response route is delivered as a
proactive message to the chat in the command's gateway context, the way
stuck-work notices reach people (``custom_agents.stuck_work.send_chat_notice``).

Who is told: only the person the gateway signed the command for
(``verified_command_person``), and only when the command names that same person.
An unverifiable command tells nobody and is logged with its ids. Only the team
lead answers; a response-typed copy is ignored.

Remove this module and the retired model when the upgrade steps from 0.9 are
removed, once 0.11 ships (README_ARCHITECTURE.md, "Upgrading an instance in place").
"""

from __future__ import annotations

from typing import Any

from custom_agents.manager_control_runtime import verified_command_person
from custom_agents.stuck_tasks import request_chat_route
from custom_agents.stuck_work import StuckMessage, send_chat_notice
from custom_messages.contracts.control_v1 import RETIRED_GATEWAY_COMMAND_NAMES, RetiredGatewayCommand
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import MessageMetaType

# The reason the gateway sees on the chat message.
RETIRED_COMMAND_NOTICE_REASON = "retired_gateway_command"
# Why a retired command told nobody (the ``reason`` of the log line).
UNANSWERED_NOT_TEAM_LEAD = "not_team_lead"
UNANSWERED_PERSON_MISMATCH = "person_mismatch"
UNANSWERED_NO_CHAT_ROUTE = "no_chat_route"


def retired_command_notice(command: RetiredGatewayCommand) -> StuckMessage:
    """The plain refusal the person reads."""

    name = RETIRED_GATEWAY_COMMAND_NAMES.get(command.message_class, "command")
    return StuckMessage(
        title=f"Your {name} was not carried out",
        text=(
            f"Your {name} was sent before the team was upgraded, so nothing was changed. "
            "Please send it again."
        ),
    )


def _notice_correlation_id(command: RetiredGatewayCommand) -> str:
    """Stable per command, so a redelivered command's notice has the same idempotency key."""

    return f"retired_gateway_command:{command.command_id}"


def _log_unanswered(
    agent: Any,
    command: RetiredGatewayCommand,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
    *,
    reason: str,
) -> None:
    agent._logger.warning(
        "[%s] A gateway command sent before the upgrade was not carried out and nobody was told. "
        "event=retired_gateway_command_unanswered reason=%s command_class=%s command_id=%s "
        "message_id=%s claimed_person_id=%s",
        getattr(agent, "id", None),
        reason,
        command.message_class,
        command.command_id,
        msg_header.message_id,
        getattr(msg_meta.gateway_context, "canonical_user_id", None),
    )


async def answer_retired_gateway_command(
    agent: Any,
    *,
    command: RetiredGatewayCommand,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> None:
    """Tell the signed person their pre-upgrade command was not carried out (module docstring).

    Returns normally in every case it settles, so the delivery is acknowledged
    and not redelivered. A failure to send the notice raises: the delivery is
    retried, and the notice's stable idempotency key keeps a retry from
    showing it twice.
    """

    if command.command_type == MessageMetaType.RESPONSE:
        agent._logger.info(
            "[%s] Ignored a response to a gateway command sent before the upgrade. "
            "event=retired_gateway_command_response_ignored command_class=%s command_id=%s message_id=%s",
            getattr(agent, "id", None),
            command.message_class,
            command.command_id,
            msg_header.message_id,
        )
        return
    if not getattr(agent, "is_team_lead", False):
        _log_unanswered(agent, command, msg_header, msg_meta, reason=UNANSWERED_NOT_TEAM_LEAD)
        return
    person, refusal = verified_command_person(agent, msg_meta)
    if person is None:
        _log_unanswered(agent, command, msg_header, msg_meta, reason=refusal or "unverified")
        return
    claimed = str(getattr(msg_meta.gateway_context, "canonical_user_id", "") or "").strip()
    if claimed and claimed != person.canonical_user_id:
        # A 0.9 gateway always named the person it signed for. The chat in the
        # context is not signed, so a command naming someone else could point
        # the notice at another person's chat.
        _log_unanswered(agent, command, msg_header, msg_meta, reason=UNANSWERED_PERSON_MISMATCH)
        return
    route = request_chat_route(agent, msg_header, msg_meta, person.canonical_user_id, {str(agent.id)})
    sent = await send_chat_notice(
        agent,
        route,
        retired_command_notice(command),
        reason=RETIRED_COMMAND_NOTICE_REASON,
        correlation_id=_notice_correlation_id(command),
        source_task_id=None,
    )
    if not sent:
        _log_unanswered(agent, command, msg_header, msg_meta, reason=UNANSWERED_NO_CHAT_ROUTE)
        return
    agent._logger.warning(
        "[%s] Told a person their gateway command, sent before the upgrade, was not carried out. "
        "event=retired_gateway_command_answered command_class=%s command_id=%s message_id=%s "
        "person_id=%s route=%s",
        getattr(agent, "id", None),
        command.message_class,
        command.command_id,
        msg_header.message_id,
        person.canonical_user_id,
        route.routing_key if route is not None else None,
    )
