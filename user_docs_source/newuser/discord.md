# Add a Discord User

Discord onboarding uses the numeric Discord user id. Do not use a display name
or handle because those are not durable identities.

## Find the User ID

Use one of these methods:

1. Enable Discord Developer Mode and copy the target user's ID.
2. Ask the target user to DM or mention the bot.
3. Read the gateway log line for the rejected message.
4. Use the value shown as `newuser_target=...`.

An admin can also run:

```text
/user status
```

from Discord to confirm the admin's current channel identity and binding.

## Add the User

From the already bound admin's Discord DM or allowed channel, run:

```text
/user 987654321012345678 "Alice"
```

The new user can then DM the bot without restarting the gateway.

## Verify

Run:

```text
/user list
```

Confirm the new record shows:

- the new `canonical_user_id`
- status `active`
- a Discord channel binding for `987654321012345678`

## Add Web Chat Access

Alice can create her own reusable browser token from Discord:

```text
/user token "Alice desktop"
```

Here, `"Alice desktop"` is only a device label. Alternatively, an admin copies
Alice's `usr_...` value from `/user list` and creates a one-time invite:

```text
/user invite usr_a1b2c3d4e5f6 --label "Alice desktop"
```

Alice opens the returned `/webchat` address and pastes the invitation code into
the form. The required setup popup then generates Alice's reusable
`uauser_...` token. She reveals or copies it, saves it securely, and confirms
before Chat unlocks.

## Guild Channels

New direct-message users are accepted from the live registry immediately.
Guild channels and threads still need to satisfy configured channel allowlists
and group policy. Changing those channel allowlists is a static adapter config
change and may require restart or config reload.
