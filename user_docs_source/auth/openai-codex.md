# OpenAI Codex OAuth

Universal Agents can use `llm.provider: openai-codex` to call the Codex
Responses backend with ChatGPT/Codex OAuth credentials instead of an OpenAI API
key. The runtime does not need the `codex` binary for normal LLM calls; it needs
valid Codex OAuth auth material.

The Codex subscription backend supports `llm.reasoning_effort` and the optional
provider-generated `llm.reasoning_summary` (`auto`, `concise`, or `detailed`).
Universal Agents sends them as `reasoning.effort` and `reasoning.summary`.
For GPT-5 and o-series models, it also preserves the provider's complete
Responses output items between tool-loop turns, including encrypted reasoning
content. This keeps reasoning continuity without exposing reasoning text and
avoids Strands' unsupported `reasoningContent` replay path.
The backend does not accept the public Responses API's `max_output_tokens` or
`temperature` request parameters, so Universal Agents omits those two settings
for `openai-codex` and logs a warning naming them. The backend's model/server
output limit applies instead. If one exact Codex model explicitly rejects
`reasoning.summary`, the runtime records a model-scoped
`reasoning_summary: null` constraint in `LLM_OVERRIDES.yaml`, notifies the user,
rebuilds the model, and retries once; unrelated request failures never trigger
that repair.

Preferred credential flow:

1. Create a dedicated file-backed Codex CLI home on the host.
2. Sign in with `codex login --device-auth`.
3. Mount that Codex home read-only into the container, or import it into the
   Universal Agents-owned auth store.

Treat `auth.json` as a secret. It contains access and refresh tokens.

## macOS

Create a dedicated Codex CLI home:

```bash
export UA_CODEX_HOME="$PWD/.ua-codex-bootstrap"
mkdir -p "$UA_CODEX_HOME"
CODEX_HOME="$UA_CODEX_HOME" codex login --device-auth -c 'cli_auth_credentials_store="file"'
test -f "$UA_CODEX_HOME/auth.json"
```

Run with the file-backed Codex home mounted read-only:

```bash
export HOST_ROOT="$PWD/docker-instance"
export INSTANCE_ROOT="/data"
export BUILD_VER="team-assistant:latest"

docker run -it \
  -v "$HOST_ROOT:$INSTANCE_ROOT" \
  -v "$UA_CODEX_HOME:/codex-home:ro" \
  --name ua \
  --rm \
  -p 18090:18090 \
  -p 18600:18600 \
  -e NETWORK_PUBLIC_ORIGIN="http://localhost" \
  -e INTERNAL_WEBSITE_PUBLIC_PORT=18090 \
  -e SECURE_TOOL_BROKER_PUBLIC_PORT=8766 \
  -e LLM_PROVIDER=openai-codex \
  -e LLM_MODEL_ID=gpt-5.5 \
  -e OPENAI_CODEX_SHARED_HOME=/codex-home \
  "$BUILD_VER" run
```

For long-running agents, import the host Codex auth into the instance-owned auth
store:

```bash
./.venv/bin/python assistant_cli.py openai-codex-import \
  --config "$HOST_ROOT/instance_config.yaml" \
  --codex-home "$UA_CODEX_HOME" \
  --host-data-root "$HOST_ROOT" \
  --container-data-root "$INSTANCE_ROOT"
```

## Linux

Create a dedicated Codex CLI home:

```bash
export UA_CODEX_HOME="$PWD/.ua-codex-bootstrap"
mkdir -p "$UA_CODEX_HOME"
CODEX_HOME="$UA_CODEX_HOME" codex login --device-auth -c 'cli_auth_credentials_store="file"'
test -f "$UA_CODEX_HOME/auth.json"
```

Run with the file-backed Codex home mounted read-only:

```bash
export HOST_ROOT="$PWD/docker-instance"
export INSTANCE_ROOT="/data"
export BUILD_VER="team-assistant:latest"

docker run -it \
  -v "$HOST_ROOT:$INSTANCE_ROOT" \
  -v "$UA_CODEX_HOME:/codex-home:ro" \
  --name ua \
  --rm \
  -p 18090:18090 \
  -p 18600:18600 \
  -e NETWORK_PUBLIC_ORIGIN="http://localhost" \
  -e INTERNAL_WEBSITE_PUBLIC_PORT=18090 \
  -e SECURE_TOOL_BROKER_PUBLIC_PORT=8766 \
  -e LLM_PROVIDER=openai-codex \
  -e LLM_MODEL_ID=gpt-5.5 \
  -e OPENAI_CODEX_SHARED_HOME=/codex-home \
  "$BUILD_VER" run
```

Import for durable refresh inside the Universal Agents instance:

```bash
./.venv/bin/python assistant_cli.py openai-codex-import \
  --config "$HOST_ROOT/instance_config.yaml" \
  --codex-home "$UA_CODEX_HOME" \
  --host-data-root "$HOST_ROOT" \
  --container-data-root "$INSTANCE_ROOT"
```

## Windows PowerShell

Create a dedicated Codex CLI home:

```powershell
$UaCodexHome = Join-Path (Get-Location) ".ua-codex-bootstrap"
New-Item -ItemType Directory -Force -Path $UaCodexHome | Out-Null
$env:CODEX_HOME = $UaCodexHome
codex login --device-auth -c 'cli_auth_credentials_store="file"'
Test-Path (Join-Path $UaCodexHome "auth.json")
Remove-Item Env:\CODEX_HOME
```

Run with the file-backed Codex home mounted read-only:

```powershell
$HostRoot = Join-Path (Get-Location) "docker-instance"
$InstanceRoot = "/data"
$BuildVer = "team-assistant:latest"

docker run -it `
  -v "${HostRoot}:${InstanceRoot}" `
  -v "${UaCodexHome}:/codex-home:ro" `
  --name ua `
  --rm `
  -p 18090:18090 `
  -p 18600:18600 `
  -e NETWORK_PUBLIC_ORIGIN="http://localhost" `
  -e INTERNAL_WEBSITE_PUBLIC_PORT=18090 `
  -e SECURE_TOOL_BROKER_PUBLIC_PORT=8766 `
  -e LLM_PROVIDER=openai-codex `
  -e LLM_MODEL_ID=gpt-5.5 `
  -e OPENAI_CODEX_SHARED_HOME=/codex-home `
  "$BuildVer" run
```

Import for durable refresh inside the Universal Agents instance:

```powershell
.\.venv\Scripts\python.exe assistant_cli.py openai-codex-import `
  --config (Join-Path $HostRoot "instance_config.yaml") `
  --codex-home $UaCodexHome `
  --host-data-root $HostRoot `
  --container-data-root $InstanceRoot
```

## Auth Store Choices

Read-only mount:

- Use `OPENAI_CODEX_SHARED_HOME=/codex-home`.
- Mount the host Codex home read-only.
- Universal Agents reads `/codex-home/auth.json`.
- Universal Agents never refreshes or rewrites that shared Codex CLI auth file.
- Refresh the file by running Codex CLI on the host again.

Instance-owned import:

- Use `assistant_cli.py openai-codex-import`.
- The imported store defaults to `<instance_root>/auth/openai_codex_auth.json`
  inside the container.
- When importing from the host for a Docker instance, pass `--host-data-root`
  and `--container-data-root` so `/data/auth/openai_codex_auth.json` maps back
  to the host-mounted directory.
- Universal Agents can refresh this copied store without racing the desktop
  Codex CLI.

Access token:

- Use `OPENAI_CODEX_ACCESS_TOKEN` or `OPENAI_CODEX_ACCESS_TOKEN_FILE` for
  short-lived automation.
- `CODEX_ACCESS_TOKEN` and `CODEX_ACCESS_TOKEN_FILE` are accepted as Codex CLI
  compatibility aliases for this provider.
- Prefer secret files over inline environment variables for persistent Docker
  deployments.
- Codex access tokens are created from the
  [ChatGPT workspace Access tokens page](https://chatgpt.com/admin/access-tokens)
  when your Business or Enterprise workspace allows Codex access-token
  creation. Copy the generated token once, store it as a secret, and rotate it
  on the same schedule as other automation credentials.

Docker secret-file example:

```bash
docker run -it \
  -v "$HOST_ROOT:$INSTANCE_ROOT" \
  -v "$PWD/secrets/codex_access_token:/run/secrets/codex_access_token:ro" \
  -e OPENAI_CODEX_ACCESS_TOKEN_FILE=/run/secrets/codex_access_token \
  -e LLM_PROVIDER=openai-codex \
  -e LLM_MODEL_ID=gpt-5.5 \
  "$BUILD_VER" run
```

## Verify

From the host:

```bash
./.venv/bin/python assistant_cli.py openai-codex-status \
  --config "$HOST_ROOT/instance_config.yaml" \
  --host-data-root "$HOST_ROOT" \
  --container-data-root "$INSTANCE_ROOT"
```

Inside a running container:

```bash
docker exec ua python assistant_cli.py openai-codex-status --config /data/instance_config.yaml
```

## Common Problems

- `Unsupported parameter: max_output_tokens` or `temperature`: upgrade to a
  Universal Agents version that filters unsupported `openai-codex` request
  settings. Removing the configured values is an immediate workaround, but
  the provider-specific filter is the durable fix because generic defaults can
  otherwise restore them.
- `auth.json` is missing: the Codex CLI probably used the OS keychain. Run the
  login command with `CODEX_HOME=<dedicated-dir>` and
  `-c 'cli_auth_credentials_store="file"'`.
- Import writes to `/data/...` on the host: pass `--host-data-root` and
  `--container-data-root`, or pass a host `--auth-store-path`.
- Mounted shared auth expires: refresh it with the host Codex CLI, or import
  into the Universal Agents-owned auth store for long-running agents.
- Windows volume paths fail: use PowerShell `${Variable}` syntax before the
  colon in `-v "${HostRoot}:${InstanceRoot}"`.
