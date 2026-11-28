# Signal

Signal uses an authenticated `signal-cli-rest-api` server and the shared gateway
bridge.

## Server Setup

Run and authenticate signal-cli-rest-api separately, then record:

- REST API base URL, for example `http://127.0.0.1:8080`
- Signal account or number registered in that server

The gateway stores the REST URL and account in `instance_config.yaml`. Keep any
server-side credentials outside the agent process.

## Onboarding Flags

```bash
.venv/bin/python assistant_cli.py onboard \
  --enable-signal \
  --signal-base-url http://127.0.0.1:8080 \
  --signal-account +15550001111 \
  --signal-allow-sender-id +15551234567
```

For groups:

```bash
--signal-allow-group-id group.signal \
--signal-group-policy mention \
--signal-mention-marker @Lisa
```

## Inbound Identity

Direct Signal sessions bind to `sourceUuid` when available, otherwise the E.164
source phone number. Rejected sender logs include `newuser_target=...` for admin
onboarding.

## Attachments

Inbound Signal attachments are fetched only by attachment id from
`signal-cli-rest-api` (without following HTTP redirects) or decoded from inline
base64. Host-path keys (`path`, `localPath`, `storedFilename`,
`stored_filename`) are ignored and never read from disk. Entries without an
attachment id or inline base64 data (including bare strings or path-only
objects), invalid attachment ids, and filenames containing path separators or
absolute paths are refused.

Attachment sizes are checked against the configured upload limit before reading
or decoding bytes. Attachments are downloaded and saved one at a time, and an
attachment id repeated within one message is downloaded only once. If any
attachment in a message is refused or fails to download, the files already
saved for that message are deleted, so none of its attachments are kept. A
slash command sent with attachments is refused before anything is downloaded.
Unexpected `signal-cli-rest-api` download errors reply with a fixed
`Attachment download failed.` message; the gateway warning log records the
attachment id, its position in the message and the error type.

## Proactive Delivery

If Signal should receive proactive agent messages:

```bash
--default-delivery-channel signal \
--default-delivery-chat-id +15551234567
```

For groups, use the configured Signal group id as the chat id.

## Verification

1. Start signal-cli-rest-api and confirm the account is registered.
2. Start the gateway with Signal enabled.
3. Send a direct Signal message from an allowed sender.
4. Confirm the gateway logs the inbound sender id.
5. Confirm the agent replies through Signal.
6. Test a group only after `allowed_group_ids` and mention policy are configured.

## Troubleshooting

- Startup fails: verify `interactive_apps.plugins.builtin.signal.settings.base_url` and `account`.
- Direct messages are ignored: check `allowed_sender_ids` or onboard the user with `/user`.
- Groups are ignored: configure `allowed_group_ids` and mention markers.
- Attachments are missing: check the signal-cli-rest-api attachment settings.
