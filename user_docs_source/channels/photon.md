# Photon/iMessage

Photon/iMessage uses Photon Spectrum through the bundled Node.js sidecar and
the shared gateway bridge.

## Photon Setup

Create or select a Photon Spectrum project, then put the project credentials in
`team.env`:

```dotenv
PHOTON_PROJECT_ID=...
PHOTON_PROJECT_SECRET=...
```

The Python adapter launches the bundled sidecar by default. The runtime image
must include Node.js and the sidecar npm dependencies.

Minimal `instance_config.yaml`:

```yaml
interactive_apps:
  plugins:
    builtin.photon:
      settings:
        enabled: true
```

When `project_id` is empty, the adapter reads `PHOTON_PROJECT_ID` from the
environment. The project secret is always read through
`project_secret_env_var`, which defaults to `PHOTON_PROJECT_SECRET`.

Common explicit form:

```yaml
interactive_apps:
  plugins:
    builtin.photon:
      settings:
        enabled: true
        project_id_env_var: "PHOTON_PROJECT_ID"
        project_secret_env_var: "PHOTON_PROJECT_SECRET"
        sidecar_autostart: true
        node_bin: "node"
        allowed_sender_ids: []
        allowed_group_ids: []
```

`PHOTON_SIDECAR_TOKEN` is optional. It is not a Photon/Spectrum credential; it
is only the shared loopback token used between the Python adapter and the local
Node sidecar. If it is unset, the adapter generates a fresh token for the
sidecar process it starts.

## Onboarding Flags

```bash
.venv/bin/python assistant_cli.py onboard \
  --enable-photon \
  --photon-project-id "$PHOTON_PROJECT_ID" \
  --photon-allow-sender-id imessage:+15551234567
```

For groups:

```bash
--photon-allow-group-id imessage:group-space-id \
--photon-group-policy mention \
--photon-mention-marker @Lisa
```

## Inbound Identity

Direct Photon sessions bind to Photon `sender.id` when available. If Photon
does not emit a sender id, the adapter uses the emitted iMessage phone or space
id, such as `imessage:+15551234567`. Rejected sender logs include
`newuser_target=...` for admin onboarding.

To find the direct sender id, temporarily leave direct allowlisting open:

```yaml
interactive_apps:
  plugins:
    builtin.photon:
      settings:
        enabled: true
        allowed_sender_ids: []
        allowed_group_ids: []
```

Start the adapter and send an iMessage from the account you want to allow. Look
for a gateway log line like:

```text
Photon inbound to manager: user_id=... chat_id=... group=False message_id=... session=...
```

Use the exact logged `user_id` or `chat_id` in `allowed_sender_ids`. For
proactive delivery, prefer the exact logged `chat_id`.

## Group Chats

Photon group chats are selected by the group/space id and a text marker, not by
a native iMessage mention. First allow the group id:

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

Then address the assistant in the group with one of the configured markers:

```text
Nova what is on my schedule today?
@Nova summarize this thread
```

When `group_policy` is `mention`, the adapter ignores group messages that do
not contain one of the configured `mention_markers`. When a marker is present,
the marker is stripped and the rest of the message is forwarded to the manager.

To find the group id, temporarily enable the group or inspect logs from a group
message. Use the logged `chat_id` from:

```text
Photon inbound to manager: user_id=... chat_id=... group=True message_id=... session=...
```

Photon must deliver the group event before Universal Agents can discover or
allowlist it. On Photon shared-pool plans, the group must include the exact
Photon line assigned to the user; shared mode cannot create a new group chat
for you. Existing iMessage groups appear as chat ids such as
`any;+;<group-guid>` only after Spectrum sees traffic for that group. If direct
messages work but no sidecar metadata log appears for the group, changing
`allowed_group_ids` or mention markers will not help because the message has
not reached the adapter yet. Confirm the group includes the assigned Photon
line shown in the Photon dashboard or move to a dedicated line if you need
reliable group routing and group creation.

## Proactive Delivery

If Photon/iMessage should receive proactive agent messages:

```bash
--default-delivery-channel photon \
--default-delivery-chat-id imessage:+15551234567
```

For group spaces, use the configured Photon group/space id as the chat id.

Equivalent `instance_config.yaml`:

```yaml
delivery:
  default_target:
    channel_name: "photon"
    chat_id: "THE_LOGGED_CHAT_ID"
    thread_id: ""
```

## Verification

1. Confirm Node.js 24 or newer is available in the runtime.
2. Confirm `PHOTON_PROJECT_ID` and `PHOTON_PROJECT_SECRET` are loaded.
3. Start the gateway with Photon enabled.
4. Confirm logs show the Photon sidecar is healthy.
5. Send an iMessage through the Photon-backed channel.
6. Confirm the gateway logs the inbound sender id.
7. Confirm the agent replies through Photon/iMessage.
8. If using a group, send a message with a configured marker such as `Nova`.

## Interrupting Active Work

Photon continues reading new iMessages while an earlier request is waiting for
the manager. Send `/stop` in the same conversation to cancel the active task;
the stop message is handled through the gateway control path rather than queued
behind the reply. Ordinary follow-up text can likewise reach the manager's
mid-turn steering path. This does not run multiple model turns concurrently.

Each inbound message is tracked independently. A processing or reply-delivery
error is logged for that message and does not disconnect the inbound sidecar
stream.

Spectrum read receipts and reactions are status events. Photon logs their
metadata but does not create manager tasks from them.

## Troubleshooting

- Startup fails: verify Photon credentials and Node.js 24-or-newer availability.
- Sidecar health fails: run `npm ci` in the sidecar directory or rebuild the Docker image with Node.js support.
- Direct messages are ignored: check `allowed_sender_ids` or onboard the user with `/user`.
- A Markdown reply with a link fails: confirm the sidecar lockfile contains the
  scoped Spectrum v12 packages. Spectrum v8+ leaves Apple data detection and
  link previews to iMessage instead of sending IMAgentKit's unsupported option;
  send errors are not broadly retried.
- Groups are ignored: configure `allowed_group_ids` and mention markers.
- The assistant replies in direct messages but not a group: if a sidecar
  metadata log appears for the group, use the exact logged group `chat_id`, set
  `group_policy: "mention"`, and include a configured marker such as `Nova`
  or `@Nova` in the group message. If no sidecar metadata log appears, the
  Photon/Spectrum stream has not delivered that group event; confirm the group
  includes the assigned Photon line, and remember that shared-pool mode cannot
  create group chats.
- To distinguish adapter filtering from Photon/Spectrum delivery, look for
  `Photon sidecar: photon-sidecar: inbound metadata ...`. This sidecar log is
  emitted before the Python adapter applies allowlists and contains only
  metadata such as message id, direction, space id, space type, sender id, and
  content type. If direct messages produce this log but group messages do not,
  Spectrum is not delivering that group event to the sidecar.
