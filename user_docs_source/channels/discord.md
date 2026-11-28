# Discord

Discord uses a bot application and the shared gateway bridge.

## Create the App and Bot

1. Open the Discord Developer Portal.
2. Create an application.
3. Add a bot user.
4. Copy the bot token into `team.env`:

```dotenv
DISCORD_BOT_TOKEN=...
```

5. Invite the bot with the `bot` scope. The `applications.commands` scope is
included for slash commands.
6. Enable `MESSAGE CONTENT INTENT` if ordinary non-command channel messages
should be bridged.

## Onboarding Flags

```bash
.venv/bin/python assistant_cli.py onboard \
  --enable-discord \
  --discord-allow-user-id 123456789012345678 \
  --discord-allow-channel-id 123456789012345678 \
  --discord-command-guild-id 123456789012345678
```

If Discord should receive proactive messages:

```bash
--default-delivery-channel discord \
--default-delivery-chat-id 123456789012345678 \
--default-delivery-thread-id 123456789012345678
```

## Sessions and Threads

Discord sessions bind to parent channel plus optional thread id. Messages inside
a Discord thread stay on the same conversation lane.

Enable thread-scoped sessions when you want independent conversations by thread:

```bash
--discord-thread-scoped-sessions
```

## Allowlists and Slash Commands

`allowed_user_ids` and `allowed_channel_ids` apply to Discord slash commands
with the same rule used for ordinary text and voice messages:

- In guild channels and threads, both `allowed_channel_ids` (matching the
  channel or a thread's parent channel) and `allowed_user_ids` (unless the
  sender is a registered user) must allow the request.
- In a one-to-one DM with the bot, a registered user (when
  `gateway.users.enabled` is true) is allowed even when `allowed_channel_ids`
  is set, whether the Discord app was installed for a server or for the user.
- DMs and group DMs between other people through a user-installed app are not
  treated as a DM with the bot and obey `allowed_channel_ids`.
- When `allowed_channel_ids` is non-empty, anyone who is not a registered user
  (including an owner listed in `allowed_user_ids` when `gateway.users.enabled`
  is false) is refused in a bot DM for both messages and slash commands.
- A refused slash command receives an ephemeral reply without running the
  command, and the gateway logs `newuser_target=<discord_user_id>` for
  onboarding.

## Group Policy

`discord.group_policy` controls how guild channels accept ordinary text:

- open channel traffic,
- mention-only traffic,
- or stricter allowlist behavior.

Use allowlists for production deployments.

## Verification

1. Start the gateway.
2. Confirm slash command sync logs appear.
3. Run `/help` in an allowed guild/channel.
4. Send a simple request from an allowed user.
5. Confirm Discord shows activity while the agent works.
6. Confirm replies stay in the originating channel/thread.
7. Test proactive delivery if Discord is the default target.

## Troubleshooting

- Slash commands missing: configure `--discord-command-guild-id` for immediate guild sync, then restart.
- Ordinary messages ignored: enable Message Content Intent and check group policy.
- Replies appear in the wrong place: verify default delivery channel/thread id and thread-scoped session setting.
- Startup fails: verify `DISCORD_BOT_TOKEN` is present in the gateway env.
