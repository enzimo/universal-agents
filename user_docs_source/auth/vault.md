# HashiCorp Vault

Vault uses broker-owned `vault` CLI authentication and environment tokens.

## Broker Setup

Install the Vault CLI in the broker/container image:

```bash
./scripts/build_docker_image.sh --credential-clis vault team-assistant:vault
```

Broker-only env:

```dotenv
VAULT_ADDR=https://vault.example.com
VAULT_TOKEN_FILE=/run/secrets/vault_token
VAULT_KV_MOUNT=secret
```

`VAULT_KV_MOUNT` selects the default KV mount and defaults to `secret`.
Vault can also back broker credential-state storage for GitHub token auth and
Google `gog` token/client JSON imports:

```yaml
security_layer:
  credential_state_backend:
    mode: credential_source
    source: vault
    namespace: agent-credential-store
    fallback_to_files: false
```

Stored broker payloads are written under that namespace.

## Chat Command

```text
/auth vault
```

This reports broker-side status and setup guidance. It does not open a web form.

## Supported Broker Actions

- `credential.search`
- `credential.get_metadata`
- `credential.use_for_action`

## Verification

1. Confirm `vault` is installed in the broker container.
2. Confirm `VAULT_ADDR` points to the correct Vault.
3. Confirm `VAULT_TOKEN` or `VAULT_TOKEN_FILE` is broker-only.
4. Run `/auth vault`.
5. Search metadata under the intended KV mount/folder.
6. Confirm secret values are not printed.
