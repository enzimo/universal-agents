# WhatsApp

WhatsApp uses Twilio Messaging API and an in-process `aiohttp` webhook listener.

## Twilio Setup

1. Create or select a Twilio account.
2. Enable WhatsApp messaging for a sender.
3. Record:
   - Account SID
   - Auth token
   - WhatsApp sender, for example `whatsapp:+15550001111`
4. Put them in `team.env`:

```dotenv
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+15550001111
```

## Webhook URL

The adapter validates Twilio signatures using the public URL. Configure:

```bash
--enable-whatsapp \
--whatsapp-webhook-host 0.0.0.0 \
--whatsapp-webhook-port 8081 \
--whatsapp-webhook-path /webhooks/whatsapp \
--whatsapp-public-base-url https://example.ngrok.app
```

Set the Twilio webhook to:

```text
https://example.ngrok.app/webhooks/whatsapp
```

Use HTTPS for public Twilio webhooks. Local development commonly uses a tunnel
such as ngrok or a reverse proxy.

## Inbound Allowlists

Restrict inbound senders:

```bash
--whatsapp-allow-sender-id whatsapp:+15551234567
```

The sender id should match Twilio's `From` value.

## Proactive Delivery

If WhatsApp should receive proactive agent messages:

```bash
--default-delivery-channel whatsapp \
--default-delivery-chat-id whatsapp:+15551234567
```

If no default target is set, ordinary proactive traffic does not infer a
WhatsApp recipient. Manager available/unavailable notices may fall back to the
most recently active WhatsApp session.

## Verification

1. Start the gateway with WhatsApp enabled.
2. Confirm logs show the webhook listener path and port.
3. Send a WhatsApp message from an allowed sender.
4. Confirm the webhook returns quickly.
5. Confirm the agent replies through Twilio.
6. Confirm invalid Twilio signatures are rejected unless signature validation is intentionally disabled.
7. Confirm the public base URL and webhook path exactly match the Twilio console.

## Troubleshooting

- Signature validation fails: verify `whatsapp.public_base_url`, path, scheme, and Twilio auth token.
- No inbound messages: verify the Twilio webhook URL and tunnel.
- Replies fail: verify `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_WHATSAPP_FROM`.
- Long tasks appear idle: WhatsApp has no native typing indicator in this adapter path; it sends periodic textual progress updates.
