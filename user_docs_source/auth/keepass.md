# KeePassXC CLI

KeePassXC uses broker-side `keepassxc-cli` plus a configured database and
broker-only unlock material.

## Broker Setup

Install `keepassxc-cli` in the broker/container image:

```bash
./scripts/build_docker_image.sh --credential-clis keepassxc team-assistant:keepass
```

Broker-only env:

```dotenv
KEEPASSXC_DATABASE=/run/secrets/operator.kdbx
KEEPASSXC_PASSWORD_FILE=/run/secrets/keepass_password
KEEPASSXC_KEY_FILE=/run/secrets/operator.key
```

For no-password databases, configure:

```dotenv
KEEPASSXC_NO_PASSWORD=true
```

## Chat Command

```text
/auth keepass
```

This reports broker-side status and setup guidance. It does not open a web form.

## Supported Broker Actions

- `credential.search`
- `credential.get_metadata`
- `credential.use_for_action`

## Verification

1. Confirm `keepassxc-cli` is installed in the broker container.
2. Confirm database and password/key-file paths exist inside the broker container.
3. Run `/auth keepass`.
4. Search metadata for a known item.
5. Confirm passwords and custom secret values are not returned.
