# Docker Runtime

## Recommended Installer

For a first installation, use the bundled image installer:

```bash
curl -fsSL https://raw.githubusercontent.com/enzimo/universal-agents/v0.9.8/install.sh -o ua-install.sh
bash ua-install.sh --image ghcr.io/enzimo/universal-agents:0.9.8
```

It publishes host port `18090` to container port `18090`, keeps
durable data under `~/.universal-agents/data`, and starts browser onboarding
automatically when no instance exists. See [Quickstart](quickstart.md).

The build and raw-container workflows below are intended for development and
custom deployments.

Both images include the pinned X and Hugging Face CLIs and their agent skills.
See [X and Hugging Face](hub-cli.md) for broker/Sealbox setup, supported operations,
and credential isolation.

The project has two maintained Docker image types:

- **Standard image**: application runtime only. Use this when the message broker and storage are external.
- **Bundle image**: can include a local message broker, PostgreSQL with pgvector, sqlite tooling, and selected credential CLIs.

## Build the Standard Image

```bash
docker build -t team-assistant .
```

With OpenShell tooling:

```bash
docker build \
  --build-arg INSTALL_OPENSHELL=true \
  --build-arg OPENSHELL_VERSION=latest \
  -t team-assistant:openshell .
```

With `kubectl` for the execution controller:

```bash
docker build \
  --build-arg INSTALL_KUBECTL=true \
  --build-arg KUBECTL_VERSION=v1.34.1 \
  -t team-assistant:kubernetes .
```

The build downloads the official binary and verifies its published sha256
checksum. OpenSSH client support is included in both maintained images.

With password-manager CLIs:

```bash
docker build \
  --build-arg INSTALL_CREDENTIAL_CLIS=bw,op \
  -t team-assistant:credentials .
```

Supported credential CLI values include `bw`, `op`, `keepassxc`, `vault`,
`all`, and `none`.

Node.js/npm are optional. Enable them only when an integration requires a Node
runtime, such as Photon iMessage or the Bitwarden npm fallback:

```bash
docker build \
  --build-arg INSTALL_NODEJS=true \
  -t team-assistant:nodejs .
```

## Build with the Helper

```bash
./scripts/build_docker_image.sh team-assistant:latest
./scripts/build_docker_image.sh --openshell team-assistant:openshell
./scripts/build_docker_image.sh --nodejs team-assistant:nodejs
./scripts/build_docker_image.sh --credential-clis bw,op team-assistant:credentials
./scripts/build_docker_image.sh --no-cache team-assistant:clean
```

## Build the Bundle Image

```bash
docker build -f Dockerfile.bundle -t team-assistant-bundle .
```

With explicit bundled services and credential CLIs:

```bash
./scripts/build_docker_image.sh \
  --bundle \
  --nodejs \
  --postgres \
  --sqlite \
  --credential-clis bw,op,keepassxc,vault \
  team-assistant-bundle:full
```

Both maintained image types default to installing only Sealbox as the
credential-source CLI. When builds explicitly select `bw`, Bitwarden CLI
`2025.10.0` is installed from the pinned official zip on amd64. On arm64, that
version has no durable official standalone archive, so the Bitwarden fallback
uses npm. Builds that select `bw` and need that npm fallback fail unless
Node.js/npm were explicitly enabled with `--nodejs` or
`--build-arg INSTALL_NODEJS=true`; alternatively provide `BW_FILE_URL_ARM64` for
a matching standalone archive.

When SQLite tooling is selected, both images install `sqlite3`, `libsqlite3-0`
and `libsqlite3-dev` from the configured Debian Trixie repositories. Debian
replaces older package revisions on its live mirrors, so the default does not
pin a revision such as `3.46.1-7+deb13u1`. Python and sqlite-vec still undergo
the image's extension-loading smoke check.

`SQLITE_VERSION` remains an optional exact build argument, also forwarded by
`build_docker_image.sh`. Use it only when that revision is available for the
target architecture in the configured repositories. An unavailable explicit
pin fails the build; it never silently selects a different version. For a
`Version ... for sqlite3 was not found` error, remove an obsolete override
and rebuild from updated source. Builds use current Debian revisions by default,
so rebuilding later may include newer distribution fixes.

`INSTALL_CREDENTIAL_CLIS` contains CLI names such as `sealbox`, not secret values.
Docker's `SecretsUsedInArgOrEnv` naming warning for that selector is unrelated
to an APT version failure. Never pass actual credentials through build arguments.

Photon iMessage requires Node.js 24.5 or newer. The Dockerfiles always include
the small Photon sidecar source tree, but they install its npm dependencies only
when Node.js is enabled with `--nodejs` / `INSTALL_NODEJS=true`. Browser
onboarding disables the Photon setup option when a compatible Node.js runtime
is absent. Maintained images install the official Node.js 24.20.0 archive for
their architecture, verify its pinned SHA-256 checksum, and use Node's native
WebSocket implementation for Spectrum's inbound Fusor stream.

### Certificate trust

All image stages, including the MCP image, disable the compromised
`emSign ECC Root CA - G3` certificate in Debian's system store and rebuild both
the CA bundle and hashed certificate directory. The shared fingerprint list is
`data/security/distrusted_roots.json`. Other roots, including other emSign
certificates, remain trusted. Rebuild and recreate existing containers to apply
this change.

Python's certifi and uv have separate bundles, updated through their pinned
dependencies. Pip's vendored bundle is another independent store; correcting
Debian's store does not replace it.

Photon removes the same root from Node's effective default certificates before
loading Spectrum. It retains private CAs supplied through `NODE_EXTRA_CA_CERTS`.
An explicit `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` bundle must be readable, contain
valid certificates and exclude the distrusted root, or Photon refuses startup.
Photon also refuses OpenSSL CA mode, whose roots Node cannot enumerate safely;
use bundled or system CA mode with `NODE_EXTRA_CA_CERTS` for private roots.

### Where the bundle's settings come from

Every process reads the saved `instance_config.yaml` in the instance root, the
same file the settings pages and TeamLead's settings changes write. The bundle
adds its own services when processes load that file, without writing them
anywhere: its NATS server (`NATS_URL`, `NATS_SERVERS`, credentials) and, when
the settings use PostgreSQL, its own database server
(`TEAM_BUNDLED_POSTGRES_ADDRESS`, host and port only; the connection string
itself stays in `POSTGRES_DSN` or its Docker secret). A setting saved while the
team runs takes effect as it says: at once, or after the restart it asks for,
without restarting the container.

Earlier releases wrote a copy to `services/runtime/instance_config.bundle.yaml`
at container start and ran every process from it. That file is no longer read
or written; the bundle logs `event=bundle_runtime_config_copy_ignored` while it
exists, and you can delete it.

## Static Docs Build

Both Dockerfiles build this browser documentation during image creation:

```bash
mkdocs build --config-file /app/mkdocs.yml --site-dir /app/user_docs/site --strict
```

At runtime, the gateway website app serves the generated files from
`user_docs/site` together with generated-site pages and embedded site chat.
`assistant_cli.py run-user-docs` no longer exists. Rebuild the Docker image
after editing `user_docs_source/`.

For a local GitHub Pages-equivalent artifact:

```bash
UA_DOCS_SITE_URL=https://enzimo.github.io/universal-agents/ \
UA_DOCS_HOMEPAGE=https://enzimo.github.io/universal-agents/ \
./.venv/bin/python scripts/build_user_docs.py \
  --site-dir user_docs/pages \
  --github-pages
```

The Pages mode preserves the embedded documentation defaults, but rewrites the
standalone Docker Compose guide and its links so they work below the repository
Pages path.

## GitHub Actions Publishing

The repository includes these publication workflows:

- `publish-container.yml` publishes the complete multi-platform bundle to the
  current repository's GHCR namespace for each release tag. A stable tag
  `vX.Y.Z` publishes `X.Y.Z`, `X.Y`, `X` and `latest`. A pre-release tag (beta,
  release candidate or nightly) publishes only its exact version, such as
  `0.9.2-beta.1`, and never moves `latest`.
- `publish-release.yml` creates the GitHub Release for each pushed tag. Betas
  and release candidates are marked as pre-releases and include no setup-guide
  attachment, because the tracked guide installs the last stable version.
- `nightly.yml` publishes a nightly pre-release of `main` each day `main`
  changed. Nightlies are for testing only.
- `publish-docs.yml` validates the strict documentation build on pull requests,
  `main` and stable release tags, and deploys it when started by hand through
  the `github-pages` environment.

After importing the repository into GitHub, select **GitHub Actions** in
**Settings → Pages**. After the first container publication, confirm the GHCR
package is public; the recommended installer intentionally pulls without
registry credentials.

## Onboard in Docker

```bash
mkdir -p "$PWD/docker-instance"
docker run --rm -it \
  -v "$PWD/docker-instance:/data" \
  team-assistant onboard
```

The default in-container instance root is `/data`. The host directory then
contains `instance_config.yaml`, `team.env`, `control/`, `dbs/`, `state/`, and
`workspaces/` ([Instance Folders](configuration.md#instance-folders)). An
instance created by an earlier version still has a `workspace/` folder; the
container refuses to start until you run the move commands it prints.

Stopping the container during browser onboarding also stops any model
connection tests in progress. You do not need to wait for a slow provider to
answer before stopping. Onboarding accepts two model connection tests at a
time; if both are running, wait for one to finish before starting another.

### NATS 2.15 upgrades

The container images and native installer include NATS Server 2.15.1. NATS
2.15 adds a default limit of 1,000 consumers per stream. A team uses several
consumers per agent, and object-store watchers use consumers too. Existing
stream configuration may still report `max_consumers: -1` while the server
enforces its default, so check capacity before upgrading a large team. Raise
the limit in the NATS stream, account or server configuration when needed.
The startup consumer report checks account usage; it does not report this
per-stream limit.

For an external NATS cluster, follow the
[NATS 2.15 upgrade guide](https://docs.nats.io/release-notes/upgrade-to-2.15).
It recommends upgrading older 2.14 clusters to at least 2.14.7 before 2.15,
postponing stream and consumer scaling or moves until every node is upgraded,
and downgrading only to 2.14.7 or later. The included single-server deployment
does not perform a rolling cluster upgrade.

### Keep the databases on local disk

The SQLite databases in `dbs/` belong on a disk local to the host, not on a
network share such as NFS. SQLite's documentation warns against network file
systems: their locking and syncing are slow or unreliable, and the write-ahead
log mode Universal Agents uses works only while every process using a database
runs on the same host. On an NFS share a single database read has
been measured waiting 30 seconds, and the gateway's chat apps and Web Chat wait
with it. Each process keeps one connection open to every database it uses, which
avoids most re-reads, but writes still go to the share.

When the rest of the instance must stay on a share, mount a local volume over
just the databases:

```bash
docker run --rm -d \
  --name team-assistant \
  -v /mnt/share/instance:/data \
  -v team-assistant-dbs:/data/dbs \
  team-assistant
```

Copy the existing `dbs/` folder, including every `-wal` and `-shm` file, into the
new volume while the container is stopped. A slow database call is logged as
`event=sqlite_open_slow` or `event=sqlite_statement_slow` with the database,
the thread, `on_event_loop` and the process (`pid` and `program`);
`on_event_loop=true` means it held up the gateway's chat apps and Web Chat too.

Before each start's services run, the container prepares the shared manager
database (`event=sqlite_database_prepared`), so the services never create it
at the same moment. Without that step, a disk that takes over half a second per
sync made their first opens fail with "database is locked" and the container
stopped. If the setup itself fails, the container refuses to start and logs
`event=manager_database_prepare_failed` with the database and the error.
Preparation makes a harmless write on every start so a previous interrupted
setup or transaction cannot leave the first slow write to competing services.
It preserves the stored records and database version.

The container also sets up the team's message streams in NATS before the
services start (`event=nats_streams_prepared`, with the stream names). The
gateway creates them, and the team lead and the agents only check once that
they exist. Without that step, on a new team, after an upgrade that adds or
changes a stream, or (with `transport.storage: memory`) on any start of the
container's own NATS server, the team lead could check before the gateway was
ready and stop with "Stream
TEAM_..._WORK is missing; start the topology owner first", and every service
stopped with it. If the streams cannot be set up, for example because NATS
cannot be reached or refuses a change to an existing stream, the container
refuses to start and logs `event=nats_streams_prepare_failed` with the error.
Invalid effective settings, including a malformed override in `team.env`,
also refuse startup with that event. A failure or interruption during this
step stops the NATS, Sealbox and OpenShell services the launcher started, so
a corrected launch can start them again. If cleanup itself fails, the log
also reports `event=nats_streams_prepare_cleanup_failed`; every service stop
is still attempted and the original preparation error is preserved.
When NATS cannot be reached, that refusal comes after the NATS client's own
retries, about two minutes.

When Kubernetes profiles are enabled, the execution artifact directory must be
backed by the same PVC that the profile names. Mount it at
`/data/state/execution_artifacts` in the controller. Use a second PVC for workload data; do not mount the artifact
PVC read-write into workload containers.

## Run Headless

```bash
docker run --rm -d \
  --name team-assistant \
  -v "$PWD/docker-instance:/data" \
  -p 18090:18090 \
  -p 18600:18600 \
  team-assistant
```

Forward channel-specific ports too. For WhatsApp, publish the configured webhook
port. For generated sites, publish the website app port. For local
broker auth links, publish the secure tool broker port.

## Reverse Proxies and Docker Swarm

For a TLS-terminating reverse proxy, set `network.public_origin` to the public
HTTPS origin and set the website and broker public ports to `null`. Keep their
bind ports configured for the private container network. Traefik can then route
Web Chat, docs, assets, and `/api/...` to the website listener while routing
`/auth/...`, `/credential-collection/...`, and `/account-link/...` to the secure broker.

See [Reverse Proxy and Docker Swarm](reverse-proxy.md) for the complete
`instance_config.yaml` settings, Swarm labels, onboarding-port considerations,
security boundaries, and verification steps.

### Remove abandoned JetStream consumers in Docker Swarm

Older releases left generated agent consumers in NATS after every restart. If
the account reaches its consumer limit, park the service as shown above and run
the bundled reconciler inside the task container. The command starts only the
local NATS service, reads the existing JetStream data, and stops NATS before it
returns.

Use the reconciler shipped with the same container image as its `/app` runtime
modules. After updating the reconciler implementation, rebuild and redeploy the
image before running cleanup; copying only the script into an older image can
leave it using older classification code.

Images keep only compiled Python files, so start the reconciler as a module
(`python -m scripts.reconcile_nats_consumers`). `docker exec` runs in `/app`,
the image's working folder, where Python finds it. A command that names
`/app/scripts/reconcile_nats_consumers.py` fails because that file is not in
the image.

Start with the read-only report:

```bash
docker exec --user 0 "$UA_CONTAINER" \
  python -m scripts.reconcile_nats_consumers \
  --config /data/instance_config.yaml \
  --start-local-nats
```

Then apply the exact generated-id candidates:

```bash
docker exec --user 0 "$UA_CONTAINER" \
  python -m scripts.reconcile_nats_consumers \
  --config /data/instance_config.yaml \
  --start-local-nats \
  --apply \
  --confirm-runtime-stopped
```

Apply mode protects live consumers, configured stable agent ids, team-lead
routes, worker queues, gateway routes, malformed records, and records younger
than one hour. A consumer is live while a process is asking it for messages
or holds messages it has not acknowledged yet. Add `--verbose` to print every classification. For an external
NATS deployment, omit `--start-local-nats`; the command uses `NATS_URL` or
`NATS_SERVERS` and the configured credentials. Before scanning, the command
prints the resolved team id, subject prefix, and exact stream names. Stop if
those values do not match the deployed team.

Newly created process consumers are deleted during graceful shutdown. NATS
also expires them after
`transport.process_consumer_inactive_threshold_seconds`, so this manual command
is only needed for records created by older releases.

Every start runs the same read-only report before any agent starts, and its
lines appear in the container log. What the report finds stays in its own
lines, for example the warning `event=nats_consumer_capacity_high` when the
team uses 90 percent or more of the account's consumer limit, and the agents
start. When the report cannot run at all, the log shows one error,
`event=nats_consumer_preflight_not_run`, with a `reason`:

| `reason` | Meaning |
| --- | --- |
| `program_missing` | The image does not contain the report. Rebuild it from a maintained Dockerfile. |
| `start_failed` | Python could not start the report; `detail` gives the operating-system error. |
| `timed_out` | No result after 30 seconds, for example from a NATS server that accepts connections but stops answering. |
| `exited_with_error` | The report stopped with an error. Its `event=nats_consumer_reconciliation_failed` line just above gives the cause in `error=`, for example a refused connection when NATS is not running or a wrong address, or `Authorization Violation` for a wrong user or password. |

The report tries each NATS server twice, a second apart, then stops with the
server's own error, so a refused connection delays the start by about a
second per server.

Agents still start without the report. Fix the cause, then run the read-only
report above by hand.

## OpenAI Codex OAuth in Docker

The `openai-codex` LLM provider uses Codex OAuth credentials. Normal LLM calls
do not require the `codex` binary in the container and do not require an
additional port. Provide credentials by mounting a file-backed Codex CLI home or
by importing that auth into the instance-owned Universal Agents auth store.

Read-only mounted Codex home:

```bash
UA_CODEX_HOME="$PWD/.ua-codex-bootstrap"
mkdir -p "$UA_CODEX_HOME"
CODEX_HOME="$UA_CODEX_HOME" codex login --device-auth -c 'cli_auth_credentials_store="file"'

docker run --rm -d \
  --name team-assistant \
  -v "$PWD/docker-instance:/data" \
  -v "$UA_CODEX_HOME:/codex-home:ro" \
  -e LLM_PROVIDER=openai-codex \
  -e LLM_MODEL_ID=gpt-5.5 \
  -e OPENAI_CODEX_SHARED_HOME=/codex-home \
  team-assistant
```

Durable imported auth store:

```bash
./.venv/bin/python assistant_cli.py openai-codex-import \
  --config "$PWD/docker-instance/instance_config.yaml" \
  --codex-home "$UA_CODEX_HOME" \
  --host-data-root "$PWD/docker-instance" \
  --container-data-root /data
```

Use `OPENAI_CODEX_ACCESS_TOKEN_FILE` for short-lived automation tokens when a
secret-file workflow is more appropriate than mounting `auth.json`. See
[OpenAI Codex OAuth](auth/openai-codex.md) for macOS, Linux, and Windows setup
commands.

## xAI Grok OAuth in Docker

The `xai-oauth` LLM provider calls xAI directly with a refreshable OAuth
bearer; no helper binary or extra container port is required. New browser
onboarding can complete the device flow before writing the instance. For an
existing Docker-backed instance, authenticate on the host and map the
container-owned auth path into its bind mount:

```bash
./.venv/bin/python assistant_cli.py xai-oauth-login \
  --config "$PWD/docker-instance/instance_config.yaml" \
  --host-data-root "$PWD/docker-instance" \
  --container-data-root /data
```

Then configure `llm.provider: xai-oauth`. If `llm.model_id` is blank,
Universal Agents selects `grok-4.5`. The command writes the rotating tokens to
the host directory backing `/data/auth/xai_oauth_auth.json`; do not mount that
file read-only. Check it without displaying bearer material:

```bash
./.venv/bin/python assistant_cli.py xai-oauth-status \
  --config "$PWD/docker-instance/instance_config.yaml" \
  --host-data-root "$PWD/docker-instance" \
  --container-data-root /data
```

For ephemeral automation, mount a secret file and set
`XAI_OAUTH_ACCESS_TOKEN_FILE`. The `xai-oauth` provider never falls back to
`XAI_API_KEY`. See [xAI Grok OAuth](auth/xai-oauth.md) for the complete flow.

## Useful Environment Variables

| Variable | Purpose |
| --- | --- |
| `TEAM_DATA_ROOT` | Mounted data root, default `/data` |
| `TEAM_INSTANCE_ROOT` | Explicit instance root |
| `TEAM_INSTANCE_CONFIG` | Explicit `instance_config.yaml` path |
| `TEAM_ASSISTANT_ENV_FILE` | Explicit env file path |
| `SECURE_TOOL_BROKER_ENV_FILE` | Broker-only credential env file |
| `OPENAI_CODEX_SHARED_HOME` | Container path to a mounted file-backed Codex CLI home containing `auth.json` |
| `OPENAI_CODEX_AUTH_STORE_PATH` | Optional Universal Agents-owned Codex auth store path |
| `OPENAI_CODEX_ACCESS_TOKEN_FILE` | Secret-file path for an ephemeral Codex access token |
| `CODEX_ACCESS_TOKEN_FILE` | Codex CLI-compatible token secret-file alias for the `openai-codex` provider |
| `XAI_OAUTH_AUTH_STORE_PATH` | Optional xAI OAuth instance-owned token-store path override |
| `XAI_OAUTH_ACCESS_TOKEN_FILE` | Secret-file path for an ephemeral xAI OAuth access token |
| `*_FILE` | Docker secret-style secret file indirection |

Every secret can come from a Docker secret file: set `<NAME>_FILE` to the
mounted file (for example `WEB_CHAT_AUTH_TOKEN_FILE=/run/secrets/web_chat_token`)
instead of setting `<NAME>`. The file's contents become the value, with the
trailing newline removed. The supported names are listed once, in
`docker/secret_env_vars.yaml`; the tables under
[Which process receives each secret](#which-process-receives-each-secret) show
every one. Google tools take no credentials file from the environment: each
person signs in with `/link cli:google`, and startup refuses
`GOOGLE_APPLICATION_CREDENTIALS` (and its `_FILE` form).

A secret file beats a direct value from `team.env`, so a value onboarding wrote
to `team.env` does not hide the Docker secret. Before it starts anything, the
container entrypoint stops with an error when the container environment sets
both `<NAME>` and `<NAME>_FILE`, or when a `<NAME>_FILE` points to a missing
file. Variables named in
configuration (for example a custom `web_chat_token_env_var`, or a
`${NAME}` reference in an MCP server's settings) also accept `<NAME>_FILE`,
but for those names a direct value wins when both are set, so set only one.

For the task-context keys, only the two signing keys are needed: the supervisor
derives the verification keys (`ACCESS_CONTEXT_VERIFY_KEYS`) from them and gives
each signing key only to the process that signs with it. Get the values for the
secret files from `python -m scripts.generate_internal_credentials --instance-name
<name> --print`, and do not also keep the keys in `team.env`; a secret file
takes precedence over a `team.env` value.

### Which process receives each secret

The container runs several processes, and each one receives only the secrets
it uses. The same list, `docker/secret_env_vars.yaml`, names the processes
for each secret; the launcher removes every other listed secret, and its
`<NAME>_FILE` variable, from each process it starts. A restart requested while
the team runs starts the process with the same limited set.

<!-- BEGIN GENERATED: secret variables (scripts/export_secret_env_docs.py) -->
This part is generated from `docker/secret_env_vars.yaml`; edit that file and
run `.venv/bin/python scripts/export_secret_env_docs.py` instead of editing it here.

The processes named in the tables:

| Process | What it is |
| --- | --- |
| `gateway` | The gateway (Web Chat, chat channels, access Inbox). |
| `manager` | The team lead; agents it starts at run time are threads inside this process. |
| `agent` | Every other agent process started from the roster. |
| `secure_broker` | The secure tool broker, which runs credentialed tools and MCP connections. |
| `activity_collector` | The activity collector, which records and shows agent activity. |
| `execution_controller` | The execution controller for managed jobs. |
| `adapter_host` | The external adapter host for chat channels. |
| `consumer_check` | The NATS consumer check run before agents start. |
| `openshell_gateway` | The managed openshell-gateway for sandboxes. |
| `nats` | The local nats-server; it receives no listed variable. |
| `openshell_cli` | The openshell command the supervisor runs; it receives no listed variable. |
| `sealbox` | The managed sealbox-server; it receives no listed variable. |
| `postgres` | The bundled Postgres server and its tools; they receive no listed variable. |

The *Secret file* column says how `<NAME>_FILE` works: *contents* means
the file's contents become the value, and *none* means there is no file
form; the entry only limits which processes receive the variable. A name
ending in `*` covers every variable that starts with the text before it.

#### Message broker and storage

| Variable | Processes that receive it | Secret file | What it is |
| --- | --- | --- | --- |
| `NATS_USER` | `gateway`, `manager`, `agent`, `secure_broker`, `activity_collector`, `execution_controller`, `adapter_host`, `consumer_check` | contents | User name for the NATS message broker; every process that sends or receives team messages. |
| `NATS_PASSWORD` | `gateway`, `manager`, `agent`, `secure_broker`, `activity_collector`, `execution_controller`, `adapter_host`, `consumer_check` | contents | Password for the NATS message broker; every process that sends or receives team messages. |
| `POSTGRES_DSN` | `gateway`, `manager`, `agent`, `secure_broker`, `activity_collector`, `execution_controller`, `adapter_host`, `consumer_check` | contents | PostgreSQL connection string, including its password; every process that stores team records. |

#### Model providers and embeddings

| Variable | Processes that receive it | Secret file | What it is |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | `manager`, `agent` | contents | Overrides the key saved with /auth model for OpenAI (api.openai.com); the team lead and agents. |
| `OPENAI_CODEX_ACCESS_TOKEN` | `manager`, `agent` | contents | Short-lived ChatGPT sign-in token for the openai-codex model provider; the team lead and agents. |
| `CODEX_ACCESS_TOKEN` | `manager`, `agent` | contents | The same ChatGPT sign-in token under the Codex CLI's name; the team lead and agents. |
| `XAI_API_KEY` | `manager`, `agent`, `gateway`, `adapter_host` | contents | Overrides the key saved with /auth model for xAI (api.x.ai), for Grok models (provider xai) and voice messages; the team lead and agents, the gateway and adapter host. |
| `XAI_OAUTH_ACCESS_TOKEN` | `manager`, `agent` | contents | Short-lived xAI sign-in token for the xai-oauth model provider; the team lead and agents. |
| `GEMINI_API_KEY` | `manager`, `agent` | contents | Overrides the key saved with /auth model for Google Gemini; the team lead and agents. |
| `ANTHROPIC_API_KEY` | `manager`, `agent` | contents | Overrides the key saved with /auth model for Anthropic (api.anthropic.com); the team lead and agents. |
| `LITELLM_API_KEY` | `manager`, `agent` | contents | Overrides the key saved with /auth model for the OpenAI-compatible model endpoint; the team lead and agents. |
| `EMBEDDING_API_KEY` | `manager`, `agent` | contents | Overrides the key saved with /auth model for the embedding service used by memory search; the team lead and agents. |

#### Web Chat and chat channels

| Variable | Processes that receive it | Secret file | What it is |
| --- | --- | --- | --- |
| `WEB_CHAT_AUTH_TOKEN` | `gateway` | contents | Token that protects Web Chat sign-in; only the gateway checks it. |
| `ADAPTER_HOST_AUTH_TOKEN` | `gateway`, `adapter_host` | contents | Token the gateway and the external adapter host use to trust each other. |
| `TELEGRAM_BOT_TOKEN` | `gateway`, `adapter_host` | contents | Telegram bot token for the Telegram channel; whichever of the gateway or adapter host runs it. |
| `DISCORD_BOT_TOKEN` | `gateway`, `adapter_host` | contents | Discord bot token for the Discord channel; whichever of the gateway or adapter host runs it. |
| `TWILIO_ACCOUNT_SID` | `gateway`, `adapter_host` | contents | Twilio account id for the WhatsApp channel, used together with TWILIO_AUTH_TOKEN. |
| `TWILIO_AUTH_TOKEN` | `gateway`, `adapter_host` | contents | Twilio auth token for the WhatsApp channel. |
| `PHOTON_PROJECT_ID` | `gateway`, `adapter_host` | contents | Photon project id for the Photon channel, used together with PHOTON_PROJECT_SECRET. |
| `PHOTON_PROJECT_SECRET` | `gateway`, `adapter_host` | contents | Photon project secret for the Photon channel. |
| `PHOTON_SIDECAR_TOKEN` | `gateway`, `adapter_host` | contents | Token between the gateway or adapter host and the Photon sidecar program it starts. |

#### Activity collector

| Variable | Processes that receive it | Secret file | What it is |
| --- | --- | --- | --- |
| `ACTIVITY_COLLECTOR_USER_TOKEN` | `gateway`, `activity_collector` | contents | Token the gateway uses to show people their activity pages. |
| `ACTIVITY_COLLECTOR_ADMIN_TOKEN` | `gateway`, `activity_collector` | contents | Token the gateway uses to show administrators every activity page. |
| `ACTIVITY_COLLECTOR_INGEST_TOKEN` | `manager`, `agent`, `activity_collector` | contents | Token the team lead and agents use to send activity events to the collector. |

#### Internal credentials

| Variable | Processes that receive it | Secret file | What it is |
| --- | --- | --- | --- |
| `SECURE_TOOL_BROKER_TOKEN` | `secure_broker` | contents | The broker's root token; every other process that calls the broker gets its own token derived from it. |
| `ACCESS_GATEWAY_SIGNING_KEY` | `gateway` | contents | Private key the gateway uses to sign the task context of each person's request. |
| `ACCESS_SCHEDULER_SIGNING_KEY` | `manager` | contents | Private key the team lead's scheduler uses to sign scheduled runs. |
| `ACCESS_CONTEXT_VERIFY_KEYS` | `gateway`, `manager`, `agent`, `secure_broker`, `activity_collector`, `execution_controller`, `adapter_host` | contents | Public keys every Universal Agents process uses to check signed task contexts. |
| `OPENSHELL_SSH_HANDSHAKE_SECRET` | `openshell_gateway` | contents | Secret the managed openshell-gateway uses for its SSH connections to sandboxes. |

#### Credentials of the tools the secure tool broker runs

| Variable | Processes that receive it | Secret file | What it is |
| --- | --- | --- | --- |
| `BW_SESSION` | `secure_broker` | contents | Unlocked session key for the Bitwarden CLI (bw) the broker runs. |
| `BW_CLIENTID` | `secure_broker` | contents | Bitwarden API key id the broker uses to sign in the Bitwarden CLI. |
| `BW_CLIENTSECRET` | `secure_broker` | contents | Bitwarden API key secret the broker uses to sign in the Bitwarden CLI. |
| `BW_PASSWORD` | `secure_broker` | contents | Bitwarden master password the broker uses to unlock the vault. |
| `GOG_KEYRING_PASSWORD` | `secure_broker` | contents | Password for the file keyring of the Google Workspace CLI (gog) the broker runs. |
| `OP_SERVICE_ACCOUNT_TOKEN` | `secure_broker` | contents | 1Password service account token for the 1Password CLI (op) the broker runs. |
| `OP_CONNECT_TOKEN` | `secure_broker` | contents | 1Password Connect server token for the 1Password CLI (op) the broker runs. |
| `KEEPASSXC_PASSWORD` | `secure_broker` | contents | Password of the KeePass database the broker opens with keepassxc-cli. |
| `KEEPASS_PASSWORD` | `secure_broker` | contents | Password of the KeePass database under its shorter name, for the broker's KeePass tool. |
| `VAULT_TOKEN` | `secure_broker` | contents | HashiCorp Vault token for the Vault CLI the broker runs. |

#### Cloud provider credentials

| Variable | Processes that receive it | Secret file | What it is |
| --- | --- | --- | --- |
| `AWS_ACCESS_KEY_ID` | `manager`, `agent` | contents | AWS access key id for Amazon Bedrock models; the team lead and agents. |
| `AWS_SECRET_ACCESS_KEY` | `manager`, `agent` | contents | AWS secret access key for Amazon Bedrock models; the team lead and agents. |
| `AWS_SESSION_TOKEN` | `manager`, `agent` | contents | Temporary AWS session token for Amazon Bedrock models; the team lead and agents. |

#### Broker tool settings and sign-in state

| Variable | Processes that receive it | Secret file | What it is |
| --- | --- | --- | --- |
| `OP_SESSION_*` | `secure_broker` | none | 1Password CLI sign-in sessions, one variable per account. |
| `BITWARDENCLI_APPDATA_DIR` | `secure_broker` | none | Folder where the Bitwarden CLI keeps its data. |
| `BITWARDEN_SERVER_URL` | `secure_broker` | none | Address of a self-hosted Bitwarden server. |
| `BW_SERVER_URL` | `secure_broker` | none | Address of a self-hosted Bitwarden server, under its shorter name. |
| `GH_CONFIG_DIR` | `secure_broker` | none | Folder for the GitHub CLI's settings and sign-in. |
| `GH_HOST` | `secure_broker` | none | GitHub Enterprise Server host name for the GitHub CLI. |
| `GITHUB_CONFIG_DIR` | `secure_broker` | none | Folder for the GitHub CLI's settings and sign-in, under its other name. |
| `OP_CONNECT_HOST` | `secure_broker` | none | Address of the 1Password Connect server. |
| `OP_ACCOUNT` | `secure_broker` | none | 1Password account the 1Password CLI uses. |
| `OP_CONFIG_DIR` | `secure_broker` | none | Folder for the 1Password CLI's settings. |
| `KEEPASSXC_DATABASE` | `secure_broker` | none | Path of the KeePass database file. |
| `KEEPASS_DATABASE` | `secure_broker` | none | Path of the KeePass database file, under its shorter name. |
| `KEEPASSXC_KEY_FILE` | `secure_broker` | none | Path of the KeePass key file. |
| `KEEPASS_KEY_FILE` | `secure_broker` | none | Path of the KeePass key file, under its shorter name. |
| `KEEPASSXC_NO_PASSWORD` | `secure_broker` | none | Switch for a KeePass database that has no password. |
| `KEEPASS_NO_PASSWORD` | `secure_broker` | none | Switch for a KeePass database that has no password, under its shorter name. |
| `VAULT_ADDR` | `secure_broker` | none | Address of the HashiCorp Vault server. |
| `VAULT_KV_MOUNT` | `secure_broker` | none | Name of the Vault key-value store the broker reads. |
| `SEALBOX_URL` | `secure_broker` | none | Address of the Sealbox server. |
| `SEALBOX_TOKEN` | `secure_broker` | none | Sealbox access token; the Sealbox CLI reads its own SEALBOX_TOKEN_FILE, which the broker points at each owner's file. |
| `SEALBOX_PUBLIC_KEY` | `secure_broker` | none | Sealbox public key; the Sealbox CLI reads its own SEALBOX_PUBLIC_KEY_FILE. |
| `SEALBOX_PRIVATE_KEY` | `secure_broker` | none | Sealbox private key; the Sealbox CLI reads its own SEALBOX_PRIVATE_KEY_FILE. |
| `SEALBOX_OUTPUT_FORMAT` | `secure_broker` | none | Output format the Sealbox CLI uses. |
| `SEALBOX_CLI_PATH` | `secure_broker` | none | Path of the Sealbox CLI program. |
| `SEALBOX_SERVER_PATH` | `secure_broker` | none | Path of the sealbox-server program. |
| `SEALBOX_BIND_HOST` | `secure_broker` | none | Address the managed sealbox-server listens on. |
| `SEALBOX_BIND_PORT` | `secure_broker` | none | Port the managed sealbox-server listens on. |
| `SEALBOX_STORE_PATH` | `secure_broker` | none | Folder where the managed sealbox-server keeps its data. |
| `SEALBOX_ROOT_TOKEN_FILE` | `secure_broker` | none | Path of the managed sealbox-server's root token file. |
| `SEALBOX_API_VERSION` | `secure_broker` | none | Sealbox API version the broker uses. |
| `SEALBOX_READY_TIMEOUT_SECONDS` | `secure_broker` | none | How long the broker waits for the managed sealbox-server to start. |
| `SECURE_TOOL_BROKER_SEALBOX_ENABLED` | `secure_broker` | none | Switch that turns on Sealbox as the broker's credential store. |
| `SECURE_TOOL_BROKER_SEALBOX_MANAGE_SERVICE` | `secure_broker` | none | Switch that lets the broker start and stop its own sealbox-server. |
| `SECURE_TOOL_BROKER_SEALBOX_AUTO_INITIALIZE` | `secure_broker` | none | Switch that lets the broker set up a new Sealbox store on first start. |
| `SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR` | `secure_broker` | none | Folder for each owner's Sealbox key files. |
| `SECURE_TOOL_BROKER_CREDENTIALS_DIR` | `secure_broker` | none | Folder for the broker's per-owner tool sign-in files. |
| `SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE` | `secure_broker` | none | Where the broker keeps tool sign-in state. |
| `SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE` | `secure_broker` | none | Credential store the broker uses for tool sign-in state. |
| `SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_NAMESPACE` | `secure_broker` | none | Name prefix for the broker's tool sign-in state in that store. |
| `SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_FALLBACK_TO_FILES` | `secure_broker` | none | Switch that lets the broker keep tool sign-in state in files when the store is unavailable. |
<!-- END GENERATED: secret variables -->

The local `nats-server`, the managed `sealbox-server`, the bundled Postgres
and the `openshell` commands the launcher runs receive none of these. The
Photon sidecar receives only its own Photon values, the program search path,
locale, time zone, temporary directory, and proxy and certificate settings.
Agents the team lead creates while it runs work inside the team lead's
process and share its secrets.

Two consequences to check when you upgrade:

- A variable name you chose in configuration (a custom
  `web_chat_token_env_var`, `interactive_apps.adapter_host.token_env_var`,
  `activity_collector.ingest_token_env_var` or Photon `*_env_var`) goes to the
  same processes as the default name it replaces.
- An MCP server setting that uses `${NAME}` is filled in by the secure broker,
  so `NAME` must be a secret the broker receives. For a listed secret the
  broker does not receive (for example `${XAI_API_KEY}`), the connection fails
  and the error names the variable; add `secure_broker` to that variable's
  `receivers` in `docker/secret_env_vars.yaml`. Unlisted variables reach every
  process. A key only one MCP server uses is better kept in the credential
  store: declare a credential slot (`credential_injections`) and link the key
  with `/link mcp:<name> team`, as the shipped Crawl4AI connection does.
- The Exa, Brave Search and Crawl4AI keys are not environment variables. They
  are the team's linked accounts in the credential store, and startup refuses
  `EXA_API_KEY`, `BRAVE_SEARCH_API_KEY` and `CRAWL4AI_API_TOKEN` (and their
  `*_FILE` forms). Onboarding stores the Exa and Brave keys typed there; link
  the Crawl4AI token with `/link http:crawl4ai team` and
  `/link mcp:crawl4ai team`.

These limits stop one process from disclosing another process's secrets by
accident, for example in a log or through a program it starts. They do not
stop a determined reader: every process runs as the same Linux user, so code
running outside the sandbox could read another process's environment. The
code sandbox is what prevents that for agent code.

The managed `openshell-gateway` reads its SSH handshake secret from the
`OPENSHELL_SSH_HANDSHAKE_SECRET` environment variable rather than a command-line
flag, because every process can read another process's command line. When the
variable is not set, the launcher generates a secret once and keeps it in
`<instance_root>/runtime/openshell/ssh_handshake_secret` (readable only by its
owner).

## Secrets from the Environment

By default onboarding keeps secrets in `<instance_root>/team.env`: it writes
the file, generates internal credentials into it, and the launcher reads it.
When every secret already reaches the container as an environment variable or
Docker secret, as in Docker Swarm, turn that off so no second, different copy
of the secrets is created:

- Web onboarding: on **Let's Begin**, turn on **Secrets come from the
  environment (Docker secrets). Don't write team.env.**
- `assistant_cli.py onboard`: pass `--secrets-from-environment`.
- Onboarding's **Import a team export** asks on its own page, starting from the
  old instance's choice
  ([Import during onboarding](team-transfer.md#import-during-onboarding)).
- Either way the choice is stored in `instance_config.yaml` as
  `launch.secrets_source: environment` (the default is `env_file`), so later
  onboarding runs and restarts keep it. A base `config.yaml` can set the same
  key to make it the starting value.

With `environment`:

- Onboarding writes no `team.env` and generates no values. An uploaded `.env`
  may still fill in form fields, but it is not saved; the page says so.
  Secrets typed into the form are not saved either.
- Onboarding reads what it needs itself, such as the database connection used
  to create the first user, from the environment.
- After you finalize, onboarding lists every secret this configuration needs
  that the environment lacks, by name, with what it is for and how to create
  it. It never shows values. **Launch** stays off until they are present; add
  them to the service and the restarted container starts the team.
- The launcher reads no env file. If a `team.env` is left in the instance
  folder, it logs one warning naming the file and ignores it. An explicit
  `--env` (or `TEAM_ASSISTANT_ENV_FILE`) is refused.

The secrets a configuration needs are the two task-context signing keys, the
activity collector tokens, `WEB_CHAT_AUTH_TOKEN`, the broker token
(`SECURE_TOOL_BROKER_TOKEN`; the secure tool broker always runs, and without
the token the launcher refuses to start and names it), `POSTGRES_DSN`
for PostgreSQL storage, the chosen model provider's API key (OpenAI,
Anthropic or Google AI; OAuth providers keep their own token store), and the
tokens of enabled chat channels. The local message broker makes and stores
its own user and password when `NATS_USER` and `NATS_PASSWORD` are not set.

Print new internal values without writing any file. The broker token and the
Google keyring password are always included; add `--storage-backend postgres`
or `--include-nats-credentials` to match your configuration (onboarding's list
shows the exact command):

```bash
docker run --rm --entrypoint python team-assistant \
  -m scripts.generate_internal_credentials --instance-name <name> --print
```

Each signing key can also be created straight into a Docker secret with one
command:

```bash
docker run --rm --entrypoint python team-assistant -c \
  'from access_control.identity import ISSUER_GATEWAY, generate_signing_key; print(generate_signing_key(ISSUER_GATEWAY)[0], end="")' \
  | docker secret create ua_access_gateway_signing_key -
docker run --rm --entrypoint python team-assistant -c \
  'from access_control.identity import ISSUER_SCHEDULER, generate_signing_key; print(generate_signing_key(ISSUER_SCHEDULER)[0], end="")' \
  | docker secret create ua_access_scheduler_signing_key -
```

Then give the service each secret and point `<NAME>_FILE` at it, for example
`--secret ua_access_gateway_signing_key` with
`ACCESS_GATEWAY_SIGNING_KEY_FILE=/run/secrets/ua_access_gateway_signing_key`.

## Runtime Checks

After starting the container:

```bash
docker logs team-assistant
docker exec team-assistant python -m assistant_cli --help
```

Verify:

- Logs show manager, gateway, activity collector, website app, and any enabled
  adapter-host process started. Enabled execution control also starts the
  execution controller.
- Installer deployments use `http://127.0.0.1:18090/`; raw Docker examples
  publish the website by default at `http://127.0.0.1:18090/`.
- Append `/docs/sites/` to the applicable website origin to open the
  generated-site index.
- `/docs` works from chat.
- `/auth status` answers (the secure tool broker always runs).
- Logs show `event=nats_consumer_reconciliation_complete` before the agents
  start, and no `event=nats_consumer_preflight_not_run` error
  ([JetStream consumer report](#remove-abandoned-jetstream-consumers-in-docker-swarm)).
- Channel adapters log their startup and accepted inbound messages.
- `python -m assistant_cli execution-job-list --config ...` can read the
  controller's team-scoped inventory.

## Common Failures

- Missing instance: run `docker run ... onboard` first.
- The team lead or an agent stops with "Stream ... is missing; start the topology owner first": the container sets up the streams before any service starts, so they were removed from NATS while the runtime ran, for example by a restart of an external NATS server with `transport.storage: memory`. Restart the container to set them up again.
- Multiple instances under `/data`: set `TEAM_INSTANCE_ROOT` or `TEAM_INSTANCE_CONFIG`.
- Generated site links unreachable: publish the website port and set `network.public_origin` plus `interactive_apps.plugins.builtin.internal_website.settings.public_port` to browser-reachable values.
- Auth links unreachable: publish the broker port and set `network.public_origin` plus `security_layer.broker_public_port` to browser-reachable values.
- WhatsApp webhook rejected: verify `whatsapp.public_base_url`, webhook path, and Twilio signature validation.
- Telegram groups quiet: disable BotFather Group Privacy when ordinary group mentions should reach the bot.
- Shell or Python commands refused because Landlock "cannot keep sandboxed code off the network": the kernel (in Docker Desktop and Colima, the one in their Linux virtual machine) is older than Linux 6.7, and a block has turned internet access off for this work. Update the kernel, or lift the block if the work may use the internet ([Code Execution Sandbox](configuration.md#code-execution-sandbox)).
- An agent needs a folder outside the instance: mount it into the container, then have an administrator register the path inside the container as a [data folder](configuration.md#data-folders) with `/access folders add <name> <path>` (no restart needed).
- Kubernetes execution unavailable: rebuild with `INSTALL_KUBECTL=true`, verify
  the namespace and service account, and confirm the artifact PVC exposes the
  controller's content-addressed store.
