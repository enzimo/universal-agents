# Bitwarden CLI

Bitwarden authentication runs inside the secure broker. The `/auth bitwarden`
page accepts a Bitwarden personal API key client ID, client secret, optional
server URL, and the master password needed for one-time unlock. The agent never
receives those values or the resulting session.

## Flow

Run in chat:

```text
/auth bitwarden
```

Open the broker URL and complete the broker-side API key login and unlock form.
The API key client ID, client secret, and master password fields include an
eye button that shows or hides the value, so you can confirm pasted values
before submitting.
For self-hosted Bitwarden or Vaultwarden, configure the server first:

```text
/auth bitwarden server https://vault.example.com
```

The broker reads the existing `bw` server state first. It runs
`bw config server` only when a server URL is supplied and the persisted CLI
state does not already match, uses `bw login --apikey` if the broker-side CLI is
not already authenticated, unlocks with
`bw unlock --passwordenv BW_PASSWORD --raw`, verifies with `bw status`, and
stores only the resulting session under broker-private credential state. With
the default file backend, that is
`broker_credentials/bitwarden/session`, and the broker also sets
`BW_SESSION_FILE` to that path. If the broker-side CLI is already unlocked and
the broker process has the active `BW_SESSION`, the form reuses that session
rather than forcing a second unlock. If raw unlock output is empty, the broker
retries Bitwarden's JSON response and normal unlock output formats, then retries
raw/JSON/plain unlock under a pseudo-terminal for Bitwarden CLI builds that
suppress unlock output when stdout is piped, and extracts `BW_SESSION` from
those alternate responses. On failure, the broker warning and failure page
include redacted
`form_diagnostics`, `bw_auth_diagnostics`, `bw_status_before`, and
`bw_status_after` snapshots so operators can see form payload presence,
subprocess context, server URL, and login state around the auth attempt.

Use **Save for Restart Unlock** on the same page as the current recommended
form path. The immediate **Authenticate Bitwarden** button remains available as
a de-emphasized diagnostic fallback, but some Bitwarden CLI/runtime
combinations can still return success without producing captureable unlock
output from the web-request process. The broker writes a 15-minute pending
request to
`SECURE_TOOL_BROKER_CREDENTIALS_DIR/bitwarden/pending_api_key_unlock.json`,
then asks you to restart the broker or container. This file is deliberately
under the persisted broker credential directory, not `BITWARDENCLI_APPDATA_DIR`,
so it survives restart when that credential directory is mounted. Startup logs
whether it found the request, forces API-key login, unlocks, verifies the
session, stores only the verified session in
`broker_credentials/bitwarden/session`, and deletes the pending request after
verification succeeds. The pending file temporarily contains plaintext API-key
and master-password material, so keep the broker credential directory private.
The saved-request page shows the expiration in your browser timezone when
available, or the saved `user.timezone` preference supplied to the broker as
`DEFAULT_REQUEST_TIMEZONE` when browser timezone detection is unavailable.

There is no `/auth bitwarden web` flow in this project.

## Credential-State Backend

Bitwarden can also store broker credential-state payloads for GitHub token auth
and Google `gog` token/client JSON imports:

```yaml
security_layer:
  credential_state_backend:
    mode: credential_source
    source: bitwarden
    namespace: agent-credential-store
    fallback_to_files: false
```

The broker stores items with stable names such as
`agent-credential-store :: github :: token` and updates them through
broker-side `bw`. The broker-side Bitwarden CLI must already be authenticated
and unlocked before credential-state writes can succeed.

Bitwarden is also the default write target for newly collected workflow
credentials in `security_layer.credential_catalog`. When an agent asks to store
a named credential or to complete a signup fallback, secret fields are submitted
through a broker form or generated inside the broker and written to Bitwarden;
agents see only metadata such as the credential key, username, and field names.
Agents can start from built-in credential templates, then save only approved
non-secret template variants for reuse. If a secret already exists in a broker
env var or Docker secret, the agent registers that env var name; it does not
copy the secret into the template or catalog YAML.

## API Key

Create a personal API key in the Bitwarden web vault:

1. Open the Bitwarden web vault for the same server the broker should use.
2. Go to account settings and open the Security or API Key section.
3. Copy the personal API key client ID and client secret.
4. Paste those values into the broker form, or configure `BW_CLIENTID` and
   `BW_CLIENTSECRET` in the broker environment.

## Supported Broker Actions

- `credential.search`
- `credential.get_metadata`
- `credential.use_for_action`

The supported target for secret use is GitHub auth login through the broker.

## Verification

1. Submit broker-side API key login and unlock through `/auth bitwarden`.
2. Run `/auth status`.
3. Ask the agent to search Bitwarden metadata for a non-sensitive item.
4. Confirm results do not include passwords, tokens, TOTP seeds, or secure note contents.
