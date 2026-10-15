# Chat Interface Configuration Nuances

All chat adapters use the same gateway bridge and session model, but each
provider exposes different identifiers, group behavior, and delivery affordances.
Use this page when deciding what to put in `instance_config.yaml` or when asking
the assistant how to configure a channel.

## Three Separate Concepts

Do not collapse these fields into one setting:

- Inbound allowlists decide who may talk to the assistant.
- `delivery.default_target` decides where proactive messages, reminders, and
  autonomous notices go.
- `/user` binds a real external channel identity to a UA user record.

For app channels, a chat can be allowed for inbound messages but not configured
for proactive delivery, or configured for proactive delivery but not allowed to
start inbound conversations.

## Finding IDs

Start the adapter, send a message from the target user or group, and inspect the
gateway log line for `user_id`, `chat_id`, and optional `thread_id`.

Use the exact logged values in configuration:

- `user_id` or sender identity for direct allowlists.
- `chat_id` for chat/channel/group allowlists and proactive delivery.
- `thread_id` only for adapters that support thread/topic scoping.

If the message is rejected, many adapters log an onboarding hint such as
`newuser_target=...`; prefer that value for `/user`.

## Group Policies

Group channels usually need both an allowlist and a policy:

```yaml
interactive_apps:
  plugins:
    example.some_channel:
      distribution: "example-some-channel"
      settings:
        enabled: true
        allowed_group_ids:
          - "GROUP_OR_CHANNEL_ID"
        group_policy: "mention"
        mention_markers:
          - "@assistant"
```

With `group_policy: "mention"`, unrelated group chatter is ignored unless the
message contains a configured marker or provider-native mention understood by
that adapter. With `group_policy: "open"`, the adapter accepts ordinary text
from allowed groups. Use `open` only for groups where most messages should go to
the assistant.

## Channel Differences

| Channel | Direct identity | Group identity | Mention behavior | Proactive target |
| --- | --- | --- | --- | --- |
| Telegram | Numeric `user_id`; private `chat_id` is often the same number | Group/supergroup `chat_id`; forum topics may have `thread_id` | BotFather group privacy affects whether ordinary mentions reach the bot; configured mention mode filters group text | `delivery.default_target.chat_id`; optional topic `thread_id` |
| Discord | Numeric Discord user id | Channel id plus optional thread id | Guild channels use configured channel allowlists and mention/open policy; slash commands are synced separately | Channel id; optional thread id |
| WhatsApp | Twilio sender identity, usually `whatsapp:+15551234567` | Not a native group adapter path here | No native mention handling in this adapter | Twilio recipient address |
| Signal | `sourceUuid` when available, otherwise E.164 phone number | Signal group id | Configured `mention_markers` are required in mention mode | Direct sender id or group id |
| Photon/iMessage | Photon `sender.id` when available, otherwise emitted iMessage phone/space id | Photon group/space `chat_id` | Text markers such as `Nova`, `@Nova`, or `nova:`; iMessage native mentions are not used by the adapter | Exact logged Photon `chat_id` |

## Photon/iMessage Group Addressing

Photon group chats require `allowed_group_ids` and, by default,
`group_policy: "mention"`. Configure one or more text markers:

```yaml
interactive_apps:
  plugins:
    builtin.photon:
      settings:
        enabled: true
        allowed_group_ids:
          - "THE_LOGGED_GROUP_CHAT_ID"
        group_policy: "mention"
        mention_markers:
          - "Nova"
          - "@Nova"
          - "nova:"
```

Then address the assistant in the iMessage group with one of those markers:

```text
Nova what is next on my calendar?
@Nova summarize the plan so far.
```

The marker is stripped before the message is forwarded to the manager.

## Proactive Delivery

Configure proactive delivery separately from inbound allowlists:

```yaml
delivery:
  default_target:
    channel_name: "photon"
    chat_id: "THE_LOGGED_CHAT_ID"
    thread_id: ""
```

If `chat_id` is empty for an app adapter, the gateway does not infer a default
DM target from recent activity.

## Verification Checklist

1. Load the relevant secrets from `team.env`, Docker secrets, or `--env`.
2. Start `run-app --app <channel>` for strict single-adapter troubleshooting, or
   `run-gateway` for normal operation.
3. Send a direct message from an allowed sender.
4. Confirm the gateway logs the expected `user_id`, `chat_id`, and session.
5. If using groups, send a message that satisfies the group policy.
6. Confirm the assistant replies in the originating chat.
7. Trigger a proactive message only after `delivery.default_target` is set.
