# xAI Grok OAuth

Universal Agents can use an xAI account OAuth grant for model calls by setting
`llm.provider: xai-oauth`. This is separate from xAI API-key authentication:
the provider does not read `XAI_API_KEY`. If no model id is configured, it
uses `grok-4.5`.

The OAuth device flow returns rotating access and refresh tokens. Universal
Agents stores them in an instance-owned, permission-restricted auth file and
refreshes them before model requests. Tokens are never written to
`instance_config.yaml`, `config.yaml`, generated `team.env`, or browser state.

## Connect During Browser Onboarding

1. In **Runtime Basics**, select **xAI Grok OAuth**.
2. Keep the default `grok-4.5`, or enter another xAI model id.
3. Select **Connect xAI**.
4. Approve the displayed device code on the xAI page. If the page does not
   open automatically, use the displayed URL and user code.
5. Wait for onboarding to report that the credentials were saved.
6. Select **Test Connection**, then complete onboarding.

The onboarding server owns the device code, polling task, and saved tokens.
The browser receives only the verification URL, user code, expiry countdown,
and redacted status.

## Connect an Existing Instance

Run the built-in login command from the Universal Agents checkout:

```bash
./.venv/bin/python assistant_cli.py xai-oauth-login \
  --config /path/to/instance/instance_config.yaml
```

The command opens the xAI verification page, prints a manual URL and code,
waits for approval, and writes the instance-owned auth store. Use
`--no-browser` on a headless host. The default approval timeout is 900 seconds;
change it with `--timeout`.

If the instance config has not been created yet, select the future instance
root directly:

```bash
./.venv/bin/python assistant_cli.py xai-oauth-login \
  --instance-root /path/to/future-instance
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe assistant_cli.py xai-oauth-login `
  --config C:\path\to\instance\instance_config.yaml
```

Use the canonical provider and default model in YAML:

```yaml
llm:
  provider: xai-oauth
  model_id: grok-4.5
  xai_oauth:
    base_url: https://api.x.ai/v1
    auth_store_path: null
    refresh_skew_seconds: 3600
    refresh_timeout_seconds: 20
```

`auth_store_path: null` resolves to
`<instance_root>/auth/xai_oauth_auth.json` for an onboarded instance. A custom
relative path also resolves under the instance root. OAuth and inference URLs
must use HTTPS on `x.ai` or an `x.ai` subdomain.

## Check or Refresh Status

Status output contains only safe metadata:

```bash
./.venv/bin/python assistant_cli.py xai-oauth-status \
  --config /path/to/instance/instance_config.yaml
```

Add `--json` for machine-readable output. Add `--refresh` to force a token
refresh while checking status:

```bash
./.venv/bin/python assistant_cli.py xai-oauth-status \
  --config /path/to/instance/instance_config.yaml \
  --refresh --json
```

The status command never prints an access or refresh token.

## Docker-Backed Instances

An instance config created for Docker commonly stores an auth path below
`/data`. When running the login command on the host, translate that container
path to the directory mounted from the host:

```bash
HOST_INSTANCE_ROOT="$PWD/docker-instance"

./.venv/bin/python assistant_cli.py xai-oauth-login \
  --config "$HOST_INSTANCE_ROOT/instance_config.yaml" \
  --host-data-root "$HOST_INSTANCE_ROOT" \
  --container-data-root /data
```

This writes to the host file backing
`/data/auth/xai_oauth_auth.json`. The runtime must be able to update that file
because xAI can rotate refresh tokens. No xAI helper binary or additional
container port is required for normal model calls.

An explicit command-local path is also available when path translation is not
appropriate:

```bash
./.venv/bin/python assistant_cli.py xai-oauth-login \
  --config "$HOST_INSTANCE_ROOT/instance_config.yaml" \
  --auth-store-path "$HOST_INSTANCE_ROOT/auth/xai_oauth_auth.json"
```

The override changes only that command; it does not edit the YAML.

## Ephemeral Access Tokens

Trusted automation may supply an already-issued OAuth access token through:

```dotenv
XAI_OAUTH_ACCESS_TOKEN=
```

For Docker secrets or mounted files, prefer:

```dotenv
XAI_OAUTH_ACCESS_TOKEN_FILE=/run/secrets/xai_oauth_access_token
```

The direct env token takes precedence over the owned auth store and cannot be
refreshed by Universal Agents. Replace it before expiry. `XAI_API_KEY` is not a
fallback for `xai-oauth`, which prevents an unrelated API key from silently
changing the selected authentication method.

Non-secret settings have matching overrides:

- `XAI_OAUTH_BASE_URL`
- `XAI_OAUTH_AUTH_STORE_PATH`
- `XAI_OAUTH_REFRESH_SKEW_SECONDS`
- `XAI_OAUTH_REFRESH_TIMEOUT_SECONDS`

## Runtime Behavior

- The provider uses Strands `OpenAIResponsesModel` against xAI's Responses
  endpoint with server-side response state disabled.
- Credentials are resolved for every model request, so a long-running process
  can refresh tokens without rebuilding the agent.
- The owned store and lock file use owner-only permissions where the platform
  supports POSIX modes. Writes are atomic, and concurrent agents double-check
  expiry under the lock before refreshing.
- A configured one-hour proactive refresh window is reduced to two minutes for
  short-lived tokens so a newly issued token is not refreshed immediately.
- `temperature` and `max_output_tokens` are forwarded when configured.
  `reasoning_effort` is forwarded only for the verified Grok model families;
  `max` and `ultra` are mapped to xAI's `high` value.
- Primary agents, specialist teams, failover targets, LLM compaction, and LLM
  conversation summaries share the same xAI OAuth settings.

## Troubleshooting

**Credentials are unavailable**

Run `xai-oauth-status`, then repeat `xai-oauth-login` for the same config and
path mapping. Ensure the runtime user can read and update the auth store.

**The device code expired**

Start `xai-oauth-login` again. Device codes are intentionally short-lived and
cannot be resumed after expiry.

**Refresh reports an account tier or API entitlement denial**

An HTTP 403 is reported separately from an expired login. Repeating the OAuth
flow will not add an xAI API entitlement that the account does not have; check
the account's model/API access before logging in again.

**Refresh is rate limited**

HTTP 429 is retryable. Wait and retry status or the model request rather than
discarding the stored refresh token.

**A custom endpoint is rejected**

OAuth bearer material is deliberately pinned to HTTPS `x.ai` origins. Use the
documented xAI endpoints rather than an arbitrary OpenAI-compatible proxy for
this provider.

**The connection test fails immediately after login**

Confirm the selected model exists for the authenticated account and try the
default `grok-4.5`. The OAuth login can succeed even when the account lacks the
required API/model entitlement.
