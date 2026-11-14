"""``/upgrade``: what the chat hears about an owner's team export.

An owner types ``/upgrade``; the gateway opens the secure broker's export page
(``secure_tool_broker.team_export_page``) for them. The passphrase goes from
that page to the broker only; the supervisor's export process writes the
archive (``team_transfer.export_requests``). The gateway watches the page with
the shared broker watcher and posts one message in the chat where
``/upgrade`` ran (``link_results.GatewayLinkResults.watch_team_export``): the
file's name and size, what it holds and the next steps, or why nothing was
exported.

Who may: owners only (``access_control.people.team_export_refusal``), checked
here with the roles the gateway knows, again by the broker when the page opens
and on submission, and once more by the export process. Only in a private
chat: the reply holds the page's link, and anyone who sees it can open the
page, so the channel must say the chat is private
(``GatewayActorContext.private_chat``); a chat it cannot vouch for counts as
not private. Only a team started by the launcher can run the export
(``supervisor_runs_exports``); a gateway started by hand points to the host
command instead.

The chat never claims a file exists unless an export reported success: a page
that expired, or a broker that restarted, may or may not have left one.
"""

from __future__ import annotations

from typing import Any, Final

from assistant_gateway.application.broker_session_watch import (
    WATCH_EXPIRED,
    WATCH_FINISHED,
    BrokerSessionOutcome,
)
from secure_tools.chat_commands import UPGRADE_COMMAND
from size_presentation import size_words
from team_transfer.export_requests import (
    EXPORT_PAGE_CLOSED,
    EXPORT_PAGE_FAILED,
    EXPORT_PAGE_FINAL_STATUSES,
    EXPORT_PAGE_RESTARTED,
    NEXT_STEPS,
    TeamExportResultV1,
    section_lines,
)

EXPORT_PAGE_DESCRIPTION: Final = (
    "Team export page opened.\n"
    "Open the secure page to choose a passphrase and export the team. The passphrase goes directly to the "
    "secure broker and is not sent to the agent."
)
EXPORT_PAGE_RECEIPT: Final = "Team export page opened."
NOT_LAUNCHED_TEXT: Final = (
    f"{UPGRADE_COMMAND} needs the launcher: this team was not started by it, so nothing can run the export. "
    "On the host, run assistant_cli team export --config <instance_config.yaml> --owner <your person id> "
    "--out <new file> instead."
)
UPGRADE_USAGE: Final = f"Usage: {UPGRADE_COMMAND}"
PRIVATE_CHAT_REQUIRED_TEXT: Final = (
    f"Send {UPGRADE_COMMAND} in a direct message with the assistant (or in Web Chat). Its reply holds a link to "
    "the export page, and anyone who sees the link in a group could open the page."
)
_EXPORTS_FOLDER = "the exports folder of this instance's data folder"


def team_export_status_is_final(payload: dict[str, Any]) -> bool:
    return str(payload.get("status") or "") in EXPORT_PAGE_FINAL_STATUSES


def team_export_result_text(outcome: BrokerSessionOutcome) -> str:
    """The chat message for one ``/upgrade`` page; never holds the passphrase or a record."""

    status = str(outcome.payload.get("status") or "")
    if outcome.ending == WATCH_EXPIRED:
        return (
            "The team export page expired before any export reported how it went, so no file is confirmed. "
            f"If you started an export, check {_EXPORTS_FOLDER}; otherwise run {UPGRADE_COMMAND} again."
        )
    if outcome.ending != WATCH_FINISHED or status not in EXPORT_PAGE_FINAL_STATUSES:
        return f"Couldn't confirm whether the team export finished. Check {_EXPORTS_FOLDER}."
    if status == EXPORT_PAGE_CLOSED:
        return "The team export page was closed without exporting."
    if status == EXPORT_PAGE_RESTARTED:
        return (
            "The secure broker restarted, so the team export page stopped and could not report an export. "
            f"If you had started one, it may still have finished: check {_EXPORTS_FOLDER}. Otherwise run "
            f"{UPGRADE_COMMAND} again."
        )
    raw = outcome.payload.get("result")
    raw = raw if isinstance(raw, dict) else {}
    if status == EXPORT_PAGE_FAILED or not raw.get("ok"):
        why = " ".join(str(raw.get("error") or "").split())
        return f"The team was not exported. {why + ' ' if why else ''}Run {UPGRADE_COMMAND} to try again."
    try:
        result = TeamExportResultV1.model_validate(raw)
    except ValueError:
        return f"The team export finished, but its report could not be read. Look in {_EXPORTS_FOLDER}."
    parts = "\n".join(f"- {line}" for line in section_lines(result))
    steps = "\n".join(f"{number}. {step}" for number, step in enumerate(NEXT_STEPS, start=1))
    note = f" {result.note}" if result.note else ""
    return (
        f"Exported the team to exports/{result.file_name} in this instance's data folder "
        f"({size_words(result.size_bytes)}).{note} It holds:\n{parts}\n\nNext steps:\n{steps}"
    )


__all__ = [
    "EXPORT_PAGE_DESCRIPTION",
    "EXPORT_PAGE_RECEIPT",
    "NOT_LAUNCHED_TEXT",
    "PRIVATE_CHAT_REQUIRED_TEXT",
    "UPGRADE_USAGE",
    "team_export_result_text",
    "team_export_status_is_final",
]
