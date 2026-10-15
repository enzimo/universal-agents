# 1Password CLI

1Password uses broker-side `op` CLI authentication with non-interactive
broker-only credentials.

## Broker Setup

Install `op` in the broker/container image or include it with:

```bash
./scripts/build_docker_image.sh --credential-clis op team-assistant:op
```

For service-account auth, put this in the broker-only env:

```dotenv
OP_SERVICE_ACCOUNT_TOKEN_FILE=/run/secrets/op_service_account_token
```

Depending on your 1Password setup, broker-side `OP_CONNECT_HOST` plus
`OP_CONNECT_TOKEN`, or a valid `OP_SESSION_*` value, may also be used.
`OP_ACCOUNT` alone is only an account selector; it is not enough for a
non-interactive broker status check.

The broker sets `OP_CONFIG_DIR` under
`broker_credentials/onepassword/config`, so any broker-side `op` state is
durable when the instance directory is mounted.

## Chat Command

```text
/auth onepassword
```

This reports broker-side status and setup guidance. It does not open a web form,
and it will not run `op` unless non-interactive broker auth material is already
configured. That prevents the CLI from asking to add an account manually inside
the container.

## Supported Broker Actions

- `credential.search`
- `credential.get_metadata`
- `credential.use_for_action`

## Verification

1. Confirm `op` is installed in the broker container.
2. Confirm the broker env contains `OP_SERVICE_ACCOUNT_TOKEN` or a valid `OP_SERVICE_ACCOUNT_TOKEN_FILE`.
3. Run `/auth onepassword`.
4. Search metadata for a known item.
5. Confirm no secret values appear in tool output.
