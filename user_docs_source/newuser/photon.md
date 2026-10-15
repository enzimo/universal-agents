# Add a Photon/iMessage User

Photon/iMessage onboarding uses Photon `sender.id` when present. If Photon does
not emit a sender id, use the iMessage phone or space id emitted by the adapter,
such as `imessage:+15551234567`.

## Find the Sender ID

Use one of these methods:

1. Ask the target user to send an iMessage through the Photon-backed channel.
2. Read the gateway log line for the rejected message.
3. Use the value shown as `newuser_target=...`.

The adapter logs `chat_id` and `user_id` for rejected senders. Prefer
`newuser_target` when it is present.

For direct allowlist configuration, use the exact logged `user_id` or `chat_id`
in `interactive_apps.plugins.builtin.photon.settings.allowed_sender_ids`. For proactive delivery, prefer
the exact logged `chat_id`.

An admin can run:

```text
/user status
```

from Photon/iMessage to confirm the admin's current channel identity and
binding.

## Add the User

From the already bound admin's Photon/iMessage chat, run one of:

```text
/user sender-id "Alice"
/user imessage:+15551234567 "Alice"
```

Prefer `sender.id` when available. The new user can then message through the
Photon channel directly without restarting the gateway.

## Verify

Run:

```text
/user list
```

Confirm the new record shows:

- the new `canonical_user_id`
- status `active`
- a Photon channel binding for the chosen sender id

## Add Web Chat Access

Alice can create her own reusable browser token from Photon/iMessage:

```text
/user token "Alice Mac"
```

Here, `"Alice Mac"` is only a device label. Alternatively, an admin copies
Alice's `usr_...` value from `/user list` and creates a one-time invite:

```text
/user invite usr_a1b2c3d4e5f6 --label "Alice Mac"
```

Alice opens the returned `/webchat` address and pastes the invitation code into
the form. The required setup popup then generates Alice's reusable
`uauser_...` token. She reveals or copies it, saves it securely, and confirms
before Chat unlocks.

## Groups

New direct users are accepted from the live registry immediately. Photon group
spaces still need to satisfy configured `allowed_group_ids` and group policy.
Changing group allowlists is a static adapter config change and may require
restart or config reload.

Photon group chats use text markers rather than native iMessage mentions. To
address the assistant in a group, configure the logged group `chat_id` and one
or more `mention_markers`:

```yaml
interactive_apps:
  plugins:
    builtin.photon:
      settings:
        allowed_group_ids:
          - "THE_LOGGED_GROUP_CHAT_ID"
        group_policy: "mention"
        mention_markers:
          - "Nova"
          - "@Nova"
          - "nova:"
```

Then send a group message such as `Nova what should we do next?`. Messages
without a configured marker are ignored when `group_policy` is `mention`.

If direct messages work but group messages produce no
`Photon sidecar: photon-sidecar: inbound metadata ...` log, the group event has
not reached Universal Agents. On Photon shared-pool plans, the group must
include the exact Photon line assigned to the user, and shared mode cannot
create a new group chat. Existing iMessage groups only become allowlistable
after Spectrum emits their `any;+;<group-guid>` chat id.
