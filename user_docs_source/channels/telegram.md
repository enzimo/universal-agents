# Telegram

Telegram uses the `python-telegram-bot` adapter and communicates through the
shared gateway bridge.

## Create the Bot

1. Message BotFather in Telegram.
2. Run `/newbot`.
3. Choose a bot name and username.
4. Copy the bot token.
5. Put the token in `team.env`:

```dotenv
TELEGRAM_BOT_TOKEN=123456:token
```

## Onboarding Flags

```bash
.venv/bin/python assistant_cli.py onboard \
  --enable-telegram \
  --telegram-allow-user-id 123456789 \
  --telegram-allow-chat-id 123456789 \
  --default-delivery-channel telegram \
  --default-delivery-chat-id 123456789
```

For groups and supergroups, add allowed group chat ids and optional topic
scoping:

```bash
--telegram-group-policy mention_only
--telegram-topic-scoped-sessions
```

## Group Privacy

For groups and supergroups, BotFather Group Privacy can prevent ordinary group
messages from reaching the bot. Disable Group Privacy if you want ordinary
`@bot ...` mentions and replies to reach the gateway. Explicit slash commands
may still work while ordinary messages are hidden.

## Answering task questions

When a task pauses and offers numbered choices, reply in the same chat and
topic. Your answer continues the task that asked the question. This also works
when the prompt arrives after a response timeout or the gateway restarts, and
when the task asks another question after your answer.

## Replying to a message

Use Telegram's **Reply** on a message to tell the assistant what you are
answering. This matters most for scheduled reports, background checks and
notifications: they come from their own work, not from your current
conversation, so a short answer such as "expected" means nothing without the
message it answers.

When you reply, the assistant receives the text of the message you replied to
along with your own message, labeled by who wrote it: the assistant, you, or
another member of the chat (shown with the name Telegram displays). If you
highlight part of the message with Telegram's quote feature, the assistant
sees that part too. It reads the replied message as background for your
message. It does not follow instructions written inside the assistant's own
message or another member's message, since a report can quote an email and
another member's words are not yours. Your conversation does not change: the
reply and the answer stay in the conversation your chat already continues.

This also works for the assistant's rich-text reports and when your answer
continues a pending question. Rich-text reports keep their visible paragraphs,
lists, tables and captions; formatting and media identifiers are omitted.

Limits:

- A long report Telegram split into several messages: the assistant sees only
  the part you replied to.
- Text over 16,000 characters is cut, and the assistant is told it was
  shortened.
- A message without text, such as a sticker or a photo without a caption,
  gives the assistant nothing to read.
- In a group with the default `group_policy: mention`, a message reaches the
  assistant only when it mentions the bot or replies to one of the bot's
  messages. To ask about another member's message, reply to it and mention
  the bot. Group Privacy (above) must also let the message through.

## Offline Policy

`telegram.manager_offline_policy` controls behavior when the gateway is up but
the manager route is unavailable.

The default `pause_telegram_polling` policy:

- sends an outage notice,
- pauses Telegram polling,
- resumes when the manager recovers,
- sends a recovery notice,
- processes any Telegram updates still retained by Telegram.

Telegram keeps queued bot updates for no longer than 24 hours.

## Rich Replies

Telegram replies use Bot API rich messages by default when the connected bot
library supports `sendRichMessage`. This preserves richer Markdown structures
such as tables and task lists. If Telegram rejects a rich message or the Bot API
endpoint is unavailable, the adapter falls back to the legacy HTML
`sendMessage` path. Replies above Telegram's rich-message text limit also use
legacy chunking. Set `interactive_apps.plugins.builtin.telegram.settings.rich_messages` to `false` in
`instance_config.yaml` to force legacy replies.

## Proactive Delivery

Replies to Telegram-originated conversations return to the originating chat.
Proactive traffic such as heartbeat needs:

```yaml
delivery:
  default_target:
    channel_name: telegram
    chat_id: "123456789"
```

Allowlists are inbound-only. If a chat should receive proactive messages and
send messages to the bot, configure both the default delivery target and the
Telegram allowlist.

## Verification

1. Start the gateway:

   ```bash
   .venv/bin/python assistant_cli.py run-gateway \
     --config <instance_root>/instance_config.yaml \
     --env <instance_root>/team.env
   ```

2. Send `/help` to the bot.
3. Send a simple request from an allowed user/chat.
4. Confirm the bot shows typing while work is in progress.
5. Confirm the reply returns to the same chat/topic.
6. Run `/docs` and open the returned URL.
7. If configured as proactive default, trigger or wait for heartbeat and confirm delivery.

## Troubleshooting

- No updates in groups: check BotFather Group Privacy.
- Private chat works but proactive messages do not: configure `delivery.default_target.chat_id`.
- Bot responds to someone unexpected: tighten `allowed_user_ids` and `allowed_chat_ids`.
- Startup fails: verify `TELEGRAM_BOT_TOKEN` is loaded into the gateway env.
