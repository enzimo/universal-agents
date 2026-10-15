# Channels

The gateway treats CLI, Telegram, WhatsApp, Discord, Signal, and
Photon/iMessage as conversation sources using a shared bridge and session
model.

## Shared Behavior

- Local CLI sessions bind to a stable CLI channel identity.
- Telegram sessions bind to chat plus optional topic/thread id.
- WhatsApp sessions bind to the sender address from Twilio.
- Discord sessions bind to parent channel plus optional thread id.
- Signal sessions bind to `sourceUuid` when available, otherwise source phone.
- Photon/iMessage sessions bind to Photon `sender.id` when available, otherwise
  the emitted iMessage phone or space id.
- Replies return to the originating session.
- Proactive delivery uses the configured default target, not the most recent chat, except for documented outage/recovery fallbacks.

For the practical differences between direct allowlists, group allowlists,
mention markers, `/user`, and proactive delivery, see
[Chat Interface Configuration Nuances](configuration-nuances.md).

## Default Delivery

Configure proactive delivery with:

```yaml
delivery:
  default_target:
    channel_name: telegram
    chat_id: "123456789"
    thread_id: ""
```

Supported channels include `cli`, `telegram`, `whatsapp`, `discord`, `signal`,
and `photon`.

## Shared Slash Commands

Interactive channels expose session-management commands such as:

- `/help`
- `/docs`
- `/tour`
- `/auth` to check your sign-ins, set up password managers, and (for
  administrators) store the team's sign-in apps
- `/access` to see, give, remove and block access, and to list requests
  waiting for you
- `/link` to sign in to a tool or link its account
- `/user` for admin-only multi-user onboarding and user management
- `/new`
- `/stop`
- `/list_sessions`
- `/list_conversations`
- `/switch_session`
- `/switch_conversation`

`/reply`, `/exit`, and `/quit` remain local-CLI-only.

Telegram and Discord list `/link` in their command menus. Discord's `/link`
slash command takes a `tool` (for example `cli:github` or `http:portainer`) and
an optional `option`: `me` (the default), `team`, or a way to sign in such as
`web`.

Manager-bound commands such as `/tour`, `/new`, `/stop`, and `/auth` use the
same authenticated canonical user identity as an ordinary message. Web Chat
derives that identity from its signed session. Other app channels use an
active channel binding; an unregistered sender is asked to use an
`/user` link code generated from another channel already linked to the same
person's canonical user.

## Deciding Agents' Requests

When an agent's request waits for a person, the gateway announces it once in a
chat where it can be decided. Telegram and Discord show decision buttons
(**Allow for this task**, **Allow and keep**, **Allow reading only** and
**Deny** for access, or **Approve all as proposed** and **Deny** for a
proposal, a file delivery or a change) and an **Open Inbox** link when Web
Chat has an HTTPS address. A press decides as the person linked to that chat
account. WhatsApp, Signal, and Photon/iMessage show the Inbox link instead. See
[Decide requests in chat](../permissions.md#decide-requests-in-chat).

## Verification Pattern

For each enabled channel:

1. Start the gateway or adapter.
2. Confirm startup logs show the adapter enabled.
3. Send a message from an allowed user/chat/channel.
4. Confirm the gateway logs the inbound session identity.
5. Confirm the agent replies in the same channel.
6. Confirm `/docs` and `/auth status` work where supported.
7. Test proactive delivery if the channel is configured as the default target.
