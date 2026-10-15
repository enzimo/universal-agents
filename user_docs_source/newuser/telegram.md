# Add a Telegram User

Telegram onboarding uses the numeric Telegram user id. Do not use `@username`
as the durable identity because usernames can change.

## Find the User ID

Use one of these methods:

1. Ask the target user to message the bot directly.
2. Read the gateway log line for the rejected message.
3. Use the value shown as `newuser_target=...`.

For private chats, Telegram `chat_id` is usually the same numeric value as
`from_user.id`. In groups, use the sender's numeric user id, not the group id.

An admin can also run:

```text
/user status
```

from Telegram to confirm the admin's current channel identity and binding.

## Add the User

From the already bound admin's Telegram chat, run:

```text
/user 123456789 "Alice"
```

The new user can then message the bot directly without restarting the gateway.

## Verify

Run:

```text
/user list
```

Confirm the new record shows:

- the new `canonical_user_id`
- status `active`
- a Telegram channel binding for `123456789`

## Add Web Chat Access

Alice can create her own reusable browser token from Telegram:

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

New direct users are accepted from the live registry immediately. Telegram
group or supergroup access still requires the group chat to satisfy configured
`allowed_chat_ids` and group policy. Changing group allowlists is a static
adapter config change and may require restart or config reload.
