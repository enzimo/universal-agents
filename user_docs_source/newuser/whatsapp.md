# Add a WhatsApp User

WhatsApp onboarding uses the Twilio sender identity. In most deployments this
is an E.164 phone number normalized from Twilio's `From` value.

## Find the Sender ID

Use one of these methods:

1. Ask the target user to message the Twilio WhatsApp sender.
2. Read the gateway log line for the rejected message.
3. Use the value shown as `newuser_target=...`.

The adapter also logs `chat_id`, `user_id`, `external_user_id`, and `wa_id` for
rejected senders. Prefer `newuser_target` when it is present.

An admin can run:

```text
/user status
```

from WhatsApp to confirm the admin's current channel identity and binding.

## Add the User

From the already bound admin's WhatsApp chat, run:

```text
/user +15551234567 "Alice"
```

The new user can then message the Twilio WhatsApp sender without restarting the
gateway.

## Verify

Run:

```text
/user list
```

Confirm the new record shows:

- the new `canonical_user_id`
- status `active`
- a WhatsApp channel binding for the sender id

## Add Web Chat Access

Alice can create her own reusable browser token from WhatsApp:

```text
/user token "Alice phone"
```

Here, `"Alice phone"` is only a device label. Alternatively, an admin copies
Alice's `usr_...` value from `/user list` and creates a one-time invite:

```text
/user invite usr_a1b2c3d4e5f6 --label "Alice phone"
```

Alice opens the returned `/webchat` address and pastes the invitation code into
the form. The required setup popup then generates Alice's reusable
`uauser_...` token. She reveals or copies it, saves it securely, and confirms
before Chat unlocks.
