# Add a Signal User

Signal onboarding uses `sourceUuid` when signal-cli-rest-api provides it.
Otherwise use the E.164 source phone number.

## Find the Sender ID

Use one of these methods:

1. Ask the target user to message the Signal account used by the adapter.
2. Read the gateway log line for the rejected message.
3. Use the value shown as `newuser_target=...`.

Rejected Signal logs include candidate sender identifiers so you can see both
the UUID and phone-number forms when available.

An admin can run:

```text
/user status
```

from Signal to confirm the admin's current channel identity and binding.

## Add the User

From the already bound admin's Signal chat, run one of:

```text
/user 550e8400-e29b-41d4-a716-446655440000 "Alice"
/user +15551234567 "Alice"
```

Prefer the UUID form when available. The new user can then message the Signal
account directly without restarting the gateway.

## Verify

Run:

```text
/user list
```

Confirm the new record shows:

- the new `canonical_user_id`
- status `active`
- a Signal channel binding for the chosen sender id

## Add Web Chat Access

Alice can create her own reusable browser token from Signal:

```text
/user token "Alice laptop"
```

Here, `"Alice laptop"` is only a device label. Alternatively, an admin copies
Alice's `usr_...` value from `/user list` and creates a one-time invite:

```text
/user invite usr_a1b2c3d4e5f6 --label "Alice laptop"
```

Alice opens the returned `/webchat` address and pastes the invitation code into
the form. The required setup popup then generates Alice's reusable
`uauser_...` token. She reveals or copies it, saves it securely, and confirms
before Chat unlocks.

## Groups

New direct users are accepted from the live registry immediately. Signal groups
still need to satisfy configured `allowed_group_ids` and group policy. Changing
group allowlists is a static adapter config change and may require restart or
config reload.
