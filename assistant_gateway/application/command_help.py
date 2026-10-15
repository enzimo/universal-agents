"""Application-owned presentation text for interactive gateway commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


_BASE_SESSION_COMMAND_LINES = (
    "/help - show this help",
    "/docs - show the documentation home page",
    "/tour - start the onboarding tour",
    "/link <tool_id> [me|team] - sign in to a tool (GitHub, Google, X) or choose which stored credential it uses, for you (default) or the team (administrators)",
    "/accounts [share|private <tool_id|model>] - see whose account agents use for each service; administrators share a service's team account with everyone or keep it private, and decide whether people they add may use the team's model keys",
    "/setup - see what you still set up for your own work: your own model and your own accounts for services the team keeps private",
    "/auth [status|model [me]|google team_client|x team_client|bitwarden [server <url>]|onepassword|keepass|vault] - check your sign-ins, test and save the team's model provider keys or set its sign-in apps (administrators), or sign in to a password manager",
    "/access [list|grant|revoke|block|unblock|requests] - see, give, remove and block agents' and people's access to tools, and see requests waiting for you",
    "/share <file or folder> with <person> [read|write] [--for 7d] - let another person use one of your files or folders",
    "/shares - list what you share; /unshare <share id> - stop sharing it",
    "/send <file> to <person|team/folder> [--move] - deliver a copy (or move it) to a person, who approves it, or to a team folder",
    "/user help|status|list [active|disabled|removed]|<external_user_id> [display_name]|enable <user>|disable <user>|remove <user>|restore <user>|role <user> <owner|admin|member>|token [label]|invite <user>|invite --new <name>|code|link <code> - add, remove and manage people, their roles and identities",
    "/coremem [list|add|replace|delete] - manage team-scoped pinned memory",
    "/usermem [list|add|replace|delete] - manage your registered user memory",
    "/topics [tag] - list topic tags or tagged threads",
    "/tag_thread <tag[,tag]> [--private] - tag the active conversation",
    "/new - start a new session",
    "/stop - list active work and exact cancellation commands",
    "/objectives - list your durable Objectives",
    "/stop objective <objective_id> - checkpoint/cancel active work and pause future runs",
    "/pause objective <objective_id> - pause future managed Objective runs",
    "/resume objective <objective_id> - resume future managed Objective runs",
    "/abandon objective <objective_id> - stop work and retain a terminal record",
    "/delete objective <objective_id> - stop work and create a recoverable tombstone",
    "/schedules - list all schedules owned by you",
    "/pause schedule <schedule_id> <until_iso> - pause a schedule until an exact time",
    "/resume schedule <schedule_id> - resume a paused or disabled schedule",
    "/disable schedule <schedule_id> - disable a schedule indefinitely",
    "/delete schedule <schedule_id> - remove a schedule",
    "/mcp - list MCP connections and hosted servers with available actions",
    "/mcp connection disable|remove <connection_id> - control a connection",
    "/mcp hosted install|start|stop|remove <hosted_server_id> - control a hosted server",
)
_DEBUG_SESSION_COMMAND_LINES = (
    "/edit config - open the instance_config.yaml editor",
    "/switch_topic <tag> - switch the active session to the latest tagged thread",
    "/list_sessions [start-end] - list recent sessions",
    "/list_conversations - list conversations in the active session",
    "/preview_session <session|alias> - show session details",
    "/search_session <text> - search sessions",
    "/switch_session <session|alias> - switch the active session",
    "/switch_conversation <conversation_id|index> - switch active conversation",
    "/rename_session <alias> - alias the active session",
    "/rename_session <session|alias> <alias> - alias another session",
)
_REPLY_COMMAND_LINE = "/reply <msg_ref> <text> - continue the referenced conversation"
USER_ROLE_USAGE = (
    "Usage: /user role <canonical_user_id|current_channel_external_user_id> "
    "<owner|admin|member>"
)
USER_LIST_USAGE = "Usage: /user list [active|disabled|removed]"
USER_REMOVE_USAGE = (
    "Usage: /user remove <canonical_user_id|current_channel_external_user_id>"
)
USER_RESTORE_USAGE = (
    "Usage: /user restore <canonical_user_id|current_channel_external_user_id>"
)
USER_TEAM_KEYS_USAGE = "Usage: /user team-keys <canonical_user_id|current_channel_external_user_id> on|off"
USER_HELP_TEXT = """User onboarding and administration:
- Run /user status in a chat to show that chat system's current external user id and canonical user status.
- Admins add users from the same chat system as the target identity: /user <external_user_id> "Display Name".
- Telegram uses numeric from_user.id, not @username: /user 123456789 "Alice".
- Discord uses numeric user id, not @name: /user 987654321012345678 "Alice".
- WhatsApp uses the normalized E.164 sender id: /user +15551234567 "Alice".
- Signal uses sourceUuid when available, otherwise E.164 phone number: /user 550e8400-e29b-41d4-a716-446655440000 "Alice".
- Photon/iMessage uses sender.id when available, otherwise the emitted iMessage phone/space id: /user imessage:+15551234567 "Alice".
- Existing users create their own reusable Web Chat token: /user token "Alice laptop". The quoted value is only a label; the token always belongs to the identity running the command.
- Admins create a one-time Web Chat invite for an existing user: /user invite <usr_...> --label "Alice laptop".
- Admins create a web-only user and one-time invite together: /user invite --new "Alice" --label "Initial browser".
- When adding someone (either way), admins choose whether their work may use the team's model keys with --team-keys or --own-keys (without either, the team's model-key sharing decides: shared unless an administrator ran /accounts private model), and may name a shared working folder with --shared <name> (team/<name>). Everyone also has team/team-shared. The reply ends with a report of how the person is set up.
- A new person is shown their first-time setup on their first message (their own model when they may not use the team's keys, and their own account for each service the team keeps private); /setup shows what is still left. The add reply lists it.
- Admins change whether a person's work may use the team's model keys: /user team-keys <usr_...> on|off. Without them, the person's agents use only their own model (/auth model me), and their requests do not run until they set one up. /user status shows a person's model settings and shared folders.
- Redeeming an invite opens a required popup where the user generates, copies, and confirms a reusable uauser_... token before Chat unlocks.
- Admins can list canonical ids, where each person stands, and bound channel identities: /user list, /user list active, /user list disabled, or /user list removed.
- Admins can disable or re-enable a person: /user disable <usr_...> and /user enable <usr_...>. Disabling is reversible: the person stays on the team and cannot use it, their running and queued work is cancelled, their objectives stop and their schedules are deleted; their folder, memory, conversations, access and linked accounts are kept. Enabling them again reinstalls their memory-review schedules; deleted schedules do not come back. Only an owner can disable or enable an owner.
- Admins take a person off the team with /user remove <usr_...>: everything disabling does, and their task records, objectives, chat sessions, sign-ins, access grants for their work and grants they gave, personal account links and stored credentials, their GitHub, Google and X sign-ins, shares, requests and notices are deleted at once. Their folder, personal memory, own knowledge pages and conversations are held for gateway.users.removed_data_retention_days (7 by default) and then deleted. Audit records, what they put in the shared team/ and agents/ folders, and files they sent to other people are kept. Only an owner can remove an owner, nobody can remove themselves, and the team always keeps an owner and an administrator.
- Admins bring a removed person back with their held data using /user restore <usr_...> while it is held. Adding them back the usual way (/user <external_user_id> "Name" from their chat app, or /user invite <usr_...> for Web Chat) also gives them their held data back while it is held, and starts fresh after that. Tasks, schedules and objectives never come back.
- Owners and admins change a person's role: /user role <usr_...> owner|admin|member. Only an owner makes someone an owner or changes an owner's role, and the team always keeps at least one owner. The new role applies from the person's next action.
- /user code creates a one-time, short-lived code for linking another channel identity to the current canonical user; it is not a Web Chat credential. Do not use an admin's link code to onboard a new person."""


@dataclass(frozen=True)
class WebChatCommandGuideItem:
    """One compact browser guide row with trusted documentation metadata."""

    syntax: str
    description: str
    documentation_url: str
    documentation_label: str = ""

    def as_payload(self) -> dict[str, str]:
        """Return the JSON-safe shape consumed by the Web Chat renderer."""

        return {
            "syntax": self.syntax,
            "description": self.description,
            "documentation_url": self.documentation_url,
            "documentation_label": self.documentation_label,
        }

    def as_help_line(self) -> str:
        """Render the compact text form used by the `/help` response."""

        return f"{self.syntax} - {self.description}"


WEB_CHAT_COMMAND_GUIDE = (
    WebChatCommandGuideItem(
        syntax="/help",
        description="show this guide",
        documentation_url="/docs/features/#web-chat-commands",
    ),
    WebChatCommandGuideItem(
        syntax="/docs",
        description="open the documentation",
        documentation_url="/docs/",
    ),
    WebChatCommandGuideItem(
        syntax="/tour",
        description="start the onboarding tour",
        documentation_url="/docs/features/#chat-and-sessions",
    ),
    WebChatCommandGuideItem(
        syntax="/link <tool_id> [ me | team ]",
        description="sign in to a tool or choose the stored credential it uses",
        documentation_url="/docs/features/#secure-tool-authentication",
        documentation_label="see options",
    ),
    WebChatCommandGuideItem(
        syntax="/accounts [ share | private <tool_id | model> ]",
        description="see whose account agents use; share a team account or keep it private",
        documentation_url="/docs/permissions/#shared-or-private",
        documentation_label="see options",
    ),
    WebChatCommandGuideItem(
        syntax="/setup",
        description="see what you still set up for your own work",
        documentation_url="/docs/newuser/#first-time-setup",
    ),
    WebChatCommandGuideItem(
        syntax="/auth [ status | model | google team_client | x team_client ... ]",
        description="check sign-ins, save the team's model keys and set up team sign-in apps",
        documentation_url="/docs/features/#secure-tool-authentication",
        documentation_label="see options",
    ),
    WebChatCommandGuideItem(
        syntax="/access [ list | grant | block | requests ... ]",
        description="see, give, remove and block access to tools",
        documentation_url="/docs/permissions/#access-commands-in-chat",
        documentation_label="see options",
    ),
    WebChatCommandGuideItem(
        syntax="/user [ help | status | list | remove | restore ... ]",
        description="add, remove and manage people and their Web Chat access",
        documentation_url="/docs/newuser/",
        documentation_label="see options",
    ),
    WebChatCommandGuideItem(
        syntax="/new",
        description="start a new chat thread",
        documentation_url="/docs/features/#web-chat-commands",
    ),
    WebChatCommandGuideItem(
        syntax="/objectives",
        description="list your Objectives",
        documentation_url="/docs/features/#runtime-controls",
        documentation_label="see controls",
    ),
    WebChatCommandGuideItem(
        syntax="/schedules",
        description="list your scheduled work",
        documentation_url="/docs/features/#runtime-controls",
        documentation_label="see controls",
    ),
    WebChatCommandGuideItem(
        syntax="/mcp [ list | connection | hosted ... ]",
        description="review and control MCP resources",
        documentation_url="/docs/features/#mcp-connections-and-hosted-tools",
        documentation_label="see options",
    ),
    WebChatCommandGuideItem(
        syntax="/stop [ task | conversation | session ... ]",
        description="list active work and choose what to cancel",
        documentation_url="/docs/features/#stopping-active-work",
        documentation_label="see options",
    ),
)


def build_web_chat_command_guide_payload() -> list[dict[str, str]]:
    """Return structured browser guide entries from the shared command catalog."""

    return [item.as_payload() for item in WEB_CHAT_COMMAND_GUIDE]


def build_web_chat_help() -> str:
    """Render the compact browser-safe command list for `/help`."""

    return "\n".join(
        [
            "Interactive commands:",
            *[f"- {item.as_help_line()}" for item in WEB_CHAT_COMMAND_GUIDE],
        ]
    )


def build_interactive_session_help(
    *,
    include_exit: bool = False,
    include_debug_commands: bool = True,
    include_reply: bool = True,
    trailer: Optional[str] = None,
) -> str:
    """Render shared command help with adapter-specific capability choices."""

    lines = ["Interactive commands:"]
    command_lines = list(_BASE_SESSION_COMMAND_LINES)
    if include_debug_commands:
        command_lines.extend(_DEBUG_SESSION_COMMAND_LINES)
    if include_reply:
        command_lines.append(_REPLY_COMMAND_LINE)
    lines.extend(f"- {line}" for line in command_lines)
    if include_exit:
        lines.append("- /exit or /quit")
    lines.extend(["", USER_HELP_TEXT])
    if trailer:
        lines.extend(["", trailer])
    return "\n".join(lines)
