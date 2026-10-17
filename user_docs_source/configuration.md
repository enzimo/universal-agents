# Configuration

## Routine database logs

Permission and task-archive repository initialization messages and
`gateway_transcript_page_loaded` history-read events use DEBUG logging. Set
`logging.level: DEBUG` to include them when diagnosing startup or browser
refreshes. Repository initialization checks an existing database as well as a
new one; the message does not mean the database was reset. Initialization
failures remain visible at ERROR.

## Active runtime state and admission

`agent.max_deferred_messages` limits durable inbox admission while an agent is busy
or paused. It defaults to 100 and must be positive. New messages beyond the limit
remain in NATS for redelivery. A duplicate already owned by the inbox can still be
acknowledged. Lowering the limit preserves an existing backlog during recovery,
logs a warning, and prevents new admission until the backlog falls below the limit.

A task NATS delivers a second time runs once. A copy of a task another agent of
the team still holds is dropped while that agent runs, with
`event=task_copy_dropped_held_elsewhere`, and a copy of a task already finished is
dropped with `event=duplicate_archived_delivery_ignored`. When the holder has
stopped, the agent that received the copy runs it and logs
`event=task_custody_takeover`. No setting controls this.

After upgrading to a release with this check, each agent's first start may log one
`event=runtime_snapshot_orphans_dropped` warning per saved snapshot. Earlier
releases left records of finished tasks in saved snapshots; they are dropped when
the snapshot is read. No action is needed.

The active task snapshot layout now uses strict version-3 records and does not read
the previous monolithic layout. Before deploying this change, finish or explicitly
abandon in-flight work and stop affected runtimes. Back up the database before
removing obsolete active task snapshots under operator control. Clearing snapshots
does not recover their work. Archived task history, conversations and independent
schedule/authorization records are outside this layout change.

### Repair an active runtime snapshot upgrade

If config loading first fails with `Unknown memory plugin id(s)` for
`long_term_profile_full`, `memory_graph_retrieval`, `team_profile_summary`, or
`user_profile_summary`, remove those retired settings while the instance is
stopped:

```bash
python -m scripts.retire_memory_plugins --config /data/instance_config.yaml --dry-run
python -m scripts.retire_memory_plugins --config /data/instance_config.yaml --yes
```

This standalone script backs up the original config beside it, then removes
only those plugin entries and their references in `agent.memory.plugins.order`.
It preserves supported plugin settings and does not access stored memories.
The edited YAML is reformatted; original comments remain in the backup. Apply
the same cleanup to the deployment's source config so a redeploy retains it.
Then proceed with snapshot retirement below.

`Unsupported active task storage layout` means a persisted owner still has an
unsupported storage header. Updating authorization settings does not change it.
The wire document can already be version 3 while its database layout is obsolete.
The opposite is also possible: a supported storage header can contain records
that the current runtime rejects. Run the preflight below even when there is no
storage-layout error; it validates the complete snapshot with the same decoder
used during recovery.

If unfinished work must complete, run the previous build to drain it before
upgrading. Otherwise, explicitly abandon that work using the offline command
below. Stop every runtime using the affected manager database and retain a full
database backup before proceeding. The command archives raw snapshots for
inspection; it does not make their tasks resumable in the new build.

Run from the repository or `/app` in a maintenance container with the instance's
usual mounts and secret environment. Use the instance's `team_id` and config path:

```bash
python -m scripts.retire_runtime_snapshots \
  --config /data/instance_config.yaml --team-id TEAM_ID --dry-run
```

The preview lists incompatible owners without printing task contents. Select
the **stored owner IDs from this preview**, which can differ from the new agent
IDs in the startup log. Repeat `--owner-agent-id` for each owner to retire:

```bash
python -m scripts.retire_runtime_snapshots \
  --config /data/instance_config.yaml --team-id TEAM_ID \
  --owner-agent-id STORED_OWNER_ID --reason "Abandon old work for snapshot upgrade" --yes
```

`--config` accepts generic and instance YAML. Use `--env /path/to/team.env` if
the maintenance process needs an env file; mounted secret-file variables also
work. For an explicit SQLite target, replace `--config ...` with
`--sqlite-path /data/dbs/task_history.db`. Use the repository's `.venv`
Python when running outside the container.

Retirement copies each selected header and its child rows into
`retired_task_runtime`, verifies the archive, and removes those active rows in
one transaction. Archive or removal failure rolls back the whole operation.
Fully recoverable snapshots and owners outside the selected team are rejected.
Retirement revalidates the selected owners inside its transaction, so an old
preview cannot authorize removal of work that has since become recoverable.
Deferred messages, cancellation records, task history, conversations, schedules,
and permissions are left intact. This command does not cancel external work or
mark old gateway requests complete. Review unresolved requests after restart
before resubmitting them.

Run the preview again, then restart the new build once the affected team has no
incompatible owners. Do not use `reset_agent_storage.py` for this failure; it
resets the entire store.

The system uses two different YAML configuration tracks plus env files.

## File Roles

| File | Role |
| --- | --- |
| `config.example.yaml` | Checked-in example for the generic `config.yaml` schema |
| `config.yaml` | Optional generic/base manager config, gitignored |
| `instance_config.yaml` | Onboarded instance runtime config |
| `team.env` | Secrets and generated internal credentials for one instance |
| `control/SOUL.md` and other `control/` files | TeamArchitect behavior, model overrides, the startup roster, team templates, agent definitions, and team skills |

`config.yaml` and `instance_config.yaml` have different schemas and migration
histories. Do not copy fields blindly between them.

## Tool allow and block lists

`agent.tool_allowlist` and `agent.tool_blocklist` use exact built-in tool
function names from this release's catalog. An unknown name stops configuration
loading and the error identifies its setting path. Both YAML formats check the
defaults and every `agent_types.<type>.agent` override, including types that are
not running and defaults that another list overrides. This avoids discovering
an invalid list only when a worker first starts.

Correct misspelled or removed names before restarting. For example, the old
`delete_coremem` function is now an action of `memory`, so a function-level block
must name `memory`. These lists cannot select individual actions; use
[access blocks](permissions.md) for operation-specific restrictions. MCP server
tools are not filtered by these lists and their names are refused.

## Instance Folders

Only the instance root is configurable (`instance_root` in
`instance_config.yaml`, `agent.instance_root` in `config.yaml`; `/data` in
Docker). Every other location is derived from it:

```text
<instance_root>/
├── instance_config.yaml, team.env     operator files
├── control/                           team control files
│   ├── SOUL.md, LLM_OVERRIDES.yaml, TEAM_AGENTS.yaml, TEAM_TEMPLATES.yaml
│   ├── agent_definitions/, agent_bundles/ (team_lead_generated/)
│   ├── skills/                        operator-added and external skills
│   ├── access/tools/                  web API tools and command-line tools
│   │                                  the team added
│   └── objective_execution_profiles/
├── dbs/                               task_history.db and other databases
├── state/                             runtime records: tool_results/, uploads/,
│                                      published_sites/ (page records),
│                                      attachment_copies/, model_failover/,
│                                      diagnostics/, execution_artifacts/
├── auth/                              model sign-in tokens (OpenAI Codex, xAI)
├── runtime/                           records trusted services keep:
│                                      mcp-connections/ (installed MCP servers),
│                                      mcp-hosting/ (hosted MCP servers),
│                                      installed-permissions/, and
│                                      skill-installation/ (reviewed skills)
└── workspaces/                        the only area agents work in
    ├── users/<person>/                one person's private folder, with
    │                                  sites/ (published pages) and scratch/
    │                                  (temporary files; scratch/sent/ holds
    │                                  copies of files sent to the person)
    ├── team/                          shared by the whole team; team-shared/ is
    │                                  everyone's common working folder, and
    │                                  administrators may name more when adding
    │                                  someone (/user ... --shared <name>)
    └── agents/                        shared by agents; the working folder
                                       for work that runs for no person
```

Agents work only in `workspaces/` and in the [data folders](#data-folders) an
administrator registers outside the instance. A task that works for a person
uses that person's folder as its default folder and can also use `team/` and
`agents/`; it cannot see anyone else's folder except what that person
[shared](permissions.md#sharing-files-with-people) with the task's person. File tools, shell and Python commands (in
the code sandbox), `notify_user` attachments, research files, and Python
environments all follow this rule, and with authorization on each folder is
also an access decision ([Files and folders](permissions.md#files-and-folders)).
Agents cannot open anything else under the root; they read and change team control files only through TeamArchitect's
typed settings, roster, and installation tools. The one exception is the two skill folders
(`control/skills` and `runtime/skill-installation/installed`): agents can read
a skill's files and run its scripts there but cannot change them. New or
changed team skills go through reviewed installation. Chat uploads are stored in
`state/uploads`; an agent copies one into its task's folder when shell or
Python needs to process it. Files agents attach to messages for people are
copied into `scratch/sent/` of the person's folder before they are sent, and
pages agents publish for a person are written into `sites/` of that folder
([Generated Sites](generated-sites.md)). Old files in every `scratch/` folder
are deleted automatically ([Scratch retention](#scratch-retention)).

### Scratch retention

Agents keep temporary files in `scratch/` of their task's folder
(`users/<person>/scratch/`, or `agents/scratch/` for work for no person), and
the gateway keeps its copies of files sent to a person there too. Each time the
internal-improvement maintenance schedule runs, the manager deletes files in
every `scratch/` folder that were last changed more than
`tool_policy.scratch_retention_days` days ago, removes folders that end up
empty, and never follows links:

```yaml
tool_policy:
  scratch_retention_days: 14   # 0 keeps scratch files
```

The same key is accepted in `instance_config.yaml`. The maintenance run's
report includes how many files it removed. Web Chat shows an attachment whose
copy was removed as no longer available.

A person's folder is named after their user id: generated ids such as
`usr_3f9a1b2c4d5e` are used as they are, and other ids become a readable name
plus a short hash.

### Upgrading from the single workspace folder

Earlier versions kept everything in `<instance_root>/workspace`. Upgrading the
configuration (`assistant_cli.py migrate-config --config <instance_config.yaml>`,
or the prompt on start) drops location settings that still hold their old defaults and stops
with an explanation if one was customized. The MCP folder settings
(`mcp.registry_dir`, `mcp.hosting.definitions_dir`, `mcp.hosting.state_dir`)
are dropped the same way by the upgrade to instance configuration version 12
(`config.yaml` version 10); if you had moved those folders, move their
contents to `runtime/mcp-connections`, `runtime/mcp-hosting/definitions` and
`runtime/mcp-hosting/state` under the instance root, remove the settings, and
upgrade again. Files are never moved for you:
startup refuses to run while old-layout files remain and prints the exact
`mv -n` commands. Existing shared workspace content moves to
`workspaces/team/`, because nothing records which person each file belongs to;
people can move private items into their own folders afterwards. Stop the
instance, run the printed commands, and start again.

## Data Folders

A data folder is a folder outside the instance that agents can be given
access to, such as a shared reports folder or a read-only reference library.
Data folders are not configuration settings: an owner or administrator
registers and removes them while Universal Agents runs, and every process sees
the change without a restart. The list is kept in the access store, in the
manager database.

```text
/access folders add reports /srv/team-reports
/access folders add reference /mnt/reference --read-only
/access folders
/access folders remove reports
```

```bash
.venv/bin/python assistant_cli.py access folders add reports /srv/team-reports \
  --config <instance_root>/instance_config.yaml --by <your user id>
```

The Access page's **Files** tab and an approved TeamLead proposal can register
them too. Agents, grants and blocks name a folder `data/<name>`, never its host
path. The path must be an existing absolute folder (in Docker, the path inside
the container, so mount the host folder there first), and a folder that holds
or sits inside the instance root, `instance_config.yaml` or the secure
broker's credentials folder is refused, because grants and shell commands would
then reach control files and secrets. The same check runs again whenever a
process reads the list; a folder that fails it is left out and logged
(`event=data_folder_refused`).

With authorization on, agents reach a data folder only with a folder grant,
such as `/access grant agent-type:WebResearchAgent data/reports read`. A grant
on a folder covers everything below it, so a grant on `data/reports/2026` gives
only that part. File tools, `notify_user` attachments and shell or Python
commands in the code sandbox follow the same grants. With authorization off,
every registered data folder is open to agents: read-write, or read-only when
registered with `--read-only`. In OpenShell mode, sandboxes hold copies of
only the person's folder, `team/` and `agents/`, so code there does not see
data folders at all. See [Data folders](permissions.md#data-folders) for the
rules and [Files and folders](permissions.md#files-and-folders) for levels,
requests and blocks.

`tool_policy.data_folders` is removed: startup refuses a configuration that
still lists folders there and names the key. Delete it and register each
folder again with `/access folders add`, under the same name so grants that
name `data/<name>` apply to it. Data folders also replace the removed
`tool_policy.restrict_to_workspace` switch. `tool_policy.allow_web_search`
and `tool_policy.allow_shell_commands` are removed too, because web research
is part of internet access and shell tools are given with grants, and so are
the environment variables `TOOL_RESTRICT_TO_WORKSPACE`, `TOOL_DENY_PATHS` and
`TOOL_DENIED_PATHS`. Startup refuses each of them by name; see
[Removed settings](permissions.md#removed-settings).

## Defaults and example values

Omitted settings follow the defaults documented in `config.example.yaml`,
with these exceptions:

- `agent.default_team_id` does not adopt the example's `team_ua` identity.
  Each instance supplies its own team identity.
- `agent.external_mcp_servers` defaults to an empty list. The example's
  Crawl4AI connection requires explicit configuration and review.

Memory hydration supports `coremem`, `usermem`, and `domain_topic_recall`, in that
default order. Configuration now rejects unsupported plugin ids in either
`agent.memory.plugins.order` or the plugin settings, even when an entry or the
plugin manager is disabled. Remove `user_profile_summary`, `team_profile_summary`,
`long_term_profile_full`, and `memory_graph_retrieval` from existing configurations.
They no longer have implementations and are no longer silently discarded. Stored
memories are unaffected.

The website now defaults to port `18090` for both listening and generated
links. If an existing deployment omitted its website ports and relied on the
former `18500` default, configure the desired port explicitly or update its
container and reverse-proxy routing.

## `instance_config.yaml`

Gateway commands and headless launch use the onboarded `instance_config.yaml`.
It owns durable non-secret settings such as:

- Message broker, server URLs, credentials, stream settings, and team id.
- Gateway request timeout and delivery policy.
- Manager and gateway storage backend choices.
- The instance root, from which every folder is derived.
- Tool settings that are not access, such as
  `tool_policy.scratch_retention_days`.
- LLM and embedding defaults.
- Activity collector bind/base URL settings.
- Voice settings.
- Interactive app enablement and allowlists.
- Shared website host settings for docs, skills, and generated sites.
- Secure broker mode, bind settings, and credential-state directory.
- Deterministic execution profiles, controller policy, artifact paths, Kubernetes
  placement, and pinned SSH targets.
- Reviewed GitHub skill-installation scanner and quarantine limits.

`instance_root` is the only location setting. `agent.workspace_dir` and the
former per-file location settings (for example `workspace.skills_dir`,
`workspace.llm_override_path`, the website `sites_dir`, and the MCP folders
`mcp.registry_dir`, `mcp.hosting.definitions_dir` and `mcp.hosting.state_dir`)
were removed; see [Instance Folders](#instance-folders).

### One saved file, and when changes take effect

Every process reads the saved `instance_config.yaml` in the instance root.
The gateway's settings pages and configuration editor save settings there;
TeamLead saves model overrides in `control/LLM_OVERRIDES.yaml`. A change takes effect as
it says: "takes effect now" at once, "at each running agent's next turn" when
that agent starts its next piece of work (model settings for other agent types
or the team's default model), "after a manager restart" (or gateway, or
team-wide) after that in-app restart, with no container restart. On an in-app
restart the launcher recomputes the environment variables it derived from the
saved file. It keeps the variables you set yourself (environment, `team.env`,
Docker secrets): such a variable, for example `LLM_MODEL_ID`, keeps overriding
the saved setting until you remove it. The Docker bundle adds its own NATS and
PostgreSQL details in memory and never writes them to the file.

Model overrides need no restart. A change explicitly targeting TeamLead's own
type applies to it immediately. Changes to another type or the workspace default
reach affected running agents at their next turn, including TeamLead when it
inherits the default. New agents read them at startup. In an agent-type override,
omit a field to inherit its default; set an optional field to `null` to clear it.
For example, `reasoning_summary: null` disables an inherited summary even when
the entry does not name a provider or model. This applies with failover on or off.

### Time zone

`user.timezone` (an IANA name such as `America/Chicago`, asked during
onboarding) is the instance time zone:

- The launcher sets `TZ` to it for itself and every process it starts, so log
  times and local clocks use it. A `TZ` you set in the container's environment
  overrides it for all of them. An unknown name makes the C library fall back
  to UTC without an error, so check the launcher's
  `event=process_timezone` line.
- Requests without a person's own time zone, and cron schedules the runtime
  creates without a person, use it (`DEFAULT_REQUEST_TIMEZONE` overrides it
  for those). A person's own schedules run in the time zone of their profile;
  see [Time zones](scheduled-workers.md#time-zones).

## `team.env`

`launch.secrets_source` in `instance_config.yaml` (or in a base
`config.yaml`, as the starting value for onboarding) says where secrets come
from:

- `env_file` (default): onboarding writes secrets and generated internal
  credentials to `team.env` (`launch.env_file_path`), and the launcher reads
  it.
- `environment`: secrets come only from environment variables and Docker
  secrets (`<NAME>_FILE`). Onboarding writes no `team.env`, generates nothing,
  and lists any required secret the environment lacks; the launcher reads no
  env file and warns once if a `team.env` is still present.
  `launch.env_file_path` must then be empty. See
  [Secrets from the Environment](docker.md#secrets-from-the-environment).

With `env_file`, use `team.env` for secrets and generated internal credentials
(model keys are saved with `/auth model` instead; see below):

```dotenv
POSTGRES_DSN=
TELEGRAM_BOT_TOKEN=
DISCORD_BOT_TOKEN=
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_WHATSAPP_FROM=
SECURE_TOOL_BROKER_TOKEN=
ACTIVITY_COLLECTOR_USER_TOKEN=
ACTIVITY_COLLECTOR_ADMIN_TOKEN=
```

`SECURE_TOOL_BROKER_TOKEN` is the secure tool broker's secret. The supervisor
gives the broker that value and every other process its own token made from
it, so the broker knows which process is calling; the broker never accepts the
plain value. A gateway or agent started by hand makes its own token at start. The internal credentials step also writes the
task-context signing keys (`ACCESS_GATEWAY_SIGNING_KEY`,
`ACCESS_SCHEDULER_SIGNING_KEY`, `ACCESS_CONTEXT_VERIFY_KEYS`), which sign the
record of whom each task works for. Do not edit them. The signing keys can also
come from Docker secrets (`ACCESS_GATEWAY_SIGNING_KEY_FILE`,
`ACCESS_SCHEDULER_SIGNING_KEY_FILE`); the supervisor derives the verification
keys from them. See
[Tool access and account links](permissions.md#tool-access-and-account-links).

Every secret can also come from a Docker secret file: set `<NAME>_FILE` to the
mounted file instead of setting `<NAME>`. A secret file beats a `team.env`
value. [Docker](docker.md#useful-environment-variables) lists the supported
names.
See [Model Providers](model-providers.md) for the complete provider matrix.
For ChatGPT/Codex OAuth-backed model access, see
[OpenAI Codex OAuth](auth/openai-codex.md) for file-backed Codex CLI and Docker
token setup.
For xAI account OAuth-backed Grok access, see
[xAI Grok OAuth](auth/xai-oauth.md). Device login writes rotating credentials
to an instance-owned auth store rather than `team.env` or YAML.

When the external adapter host is enabled, also set
`ADAPTER_HOST_AUTH_TOKEN` (or `ADAPTER_HOST_AUTH_TOKEN_FILE`) to a random value
of at least 32 characters. Keep the value out of YAML.

## Public URLs and Reverse Proxies

`network.public_origin` is the shared browser-facing scheme and hostname for
the website and secure broker. It cannot contain a path. The service-specific
public ports control whether generated links append a port:

- `interactive_apps.plugins.builtin.internal_website.settings.public_port: null` uses the public origin
  as-is for Web Chat, docs, assets, and generated sites.
- `security_layer.broker_public_port: null` uses the same origin for generated
  `/auth/...`, `/credential-collection/...`, and `/account-link/...` broker links.
- Bind ports remain the private listener ports used by Docker or a reverse
  proxy and do not need to match public ports.

An explicit `null` is different from omitting either public-port field. Omission
uses that service's default public port, while `null` keeps the shared origin
unchanged. Generated instance and bundled runtime configs preserve this
distinction.

For Traefik routing, Docker Swarm labels, first-onboarding behavior, and the
supported public broker route split, see
[Reverse Proxy and Docker Swarm](reverse-proxy.md).

## Storage

Storage can be SQLite or Postgres. Postgres-backed instances should keep DSNs in
`team.env` or process env instead of `instance_config.yaml`.

Check:

```bash
.venv/bin/python assistant_cli.py chat \
  --config <instance_root>/instance_config.yaml \
  --env <instance_root>/team.env
```

If storage fails, inspect `POSTGRES_DSN`, `STORAGE_BACKEND`, and any configured
manager or gateway storage blocks.

## Moving memories to a new instance

To move a whole team (settings, people, access, schedules, credentials and
pinned memory) to a new version, use the team transfer instead:
[Moving a team to a new version](team-transfer.md). The commands below move
only pinned memories, and also read instances older than the team transfer.

`assistant_cli.py memory export` and `memory import` copy pinned memories from
an old instance to a new one: the team's memory (`coremem`, the facts every
agent sees) and each person's memory (`usermem`). The learned memory graph is
not included; the new instance builds its own. Both work on SQLite or
PostgreSQL.

You run both commands with the new version. The export only reads the old
instance, and the old instance may run an earlier version:

- It needs only the old database. Name it with `--sqlite <path>` or
  `--postgres-dsn-file <file holding the connection string>`, or give the old
  `--config <instance_config.yaml or config.yaml>` and the export finds the
  database the way the old version did. Earlier versions kept it at
  `<old root>/workspace/dbs/task_history.db`; this version keeps it at
  `<root>/dbs/task_history.db`. The export looks in both places and stops if it
  finds none or both.
- It reads nothing else from the old configuration except the instance name,
  so settings this version removed do not matter.
- The team id comes from the stored team memory. If the database holds team
  memory for several teams, the export asks for `--team-id`. A name or team
  id it cannot tell is written as null in the file; `--instance-name` sets the
  name.
- It never changes the old database, so a read-only copy or mount works, and
  the old instance can keep running. Facts added after the export are not
  moved, so stop the old instance first if people still use it.
- With no choice flags it exports the team's memory and every person's;
  `--team`, `--person <id>` (repeatable) or `--all-people` narrow it. People
  removed from the team are left out, because their memories are held only
  until deletion. Earlier versions did not record removed people, so everyone
  is exported from them.

### With Docker

1. Before you stop the old instance, list its people: an administrator sends
   `/user list` in chat. Note each person's id (for example `usr_old123`).
2. Onboard and start the new instance on its own data volume, sign everyone in
   once, and list its people the same way with `/user list`. Starting the new
   gateway also records its people, which the import checks ids against.
3. Export with the new image, with the old data volume mounted read-only at
   `/old` and the new one at `/data`. Point `--config` at the old
   `instance_config.yaml` (in a subfolder if the old container used
   `TEAM_INSTANCE_ROOT`). The configuration records paths under `/data`; the
   export reads them under `/old`.

   ```bash
   docker run --rm --entrypoint python \
     -v <old-data>:/old:ro -v <new-data>:/data \
     <new-image> -m assistant_cli memory export \
     --config /old/instance_config.yaml --out /data/memories.json
   ```

   For PostgreSQL, give the connection string as a file and run the container
   on a network that reaches the old database server (for example the old
   Compose network):

   ```bash
   docker run --rm --entrypoint python --network <old-network> \
     -v <new-data>:/data -v <secrets>/postgres_dsn:/run/secrets/postgres_dsn:ro \
     <new-image> -m assistant_cli memory export \
     --postgres-dsn-file /run/secrets/postgres_dsn --out /data/memories.json
   ```

   The export prints the database it read, the team id and how many facts it
   wrote for each person id.
4. Check the import in the new container with `--dry-run`, which changes
   nothing. Map each old person id to the new one with `--map-person OLD=NEW`
   (repeatable); a person whose id is the same in both instances needs no
   mapping.

   ```bash
   docker exec <new-container> python -m assistant_cli memory import \
     --config /data/instance_config.yaml --in /data/memories.json \
     --map-person usr_old123=usr_new456 --dry-run
   ```

5. Run the same command without `--dry-run`, then delete the file:
   `docker exec <new-container> rm /data/memories.json`.

`python -m assistant_cli` works whether or not the image keeps its Python
source files. `--entrypoint python` is needed for `docker run` because the
image's entrypoint accepts only `run`, `onboard` and `onboard-web`.

### Without Docker

```bash
.venv/bin/python assistant_cli.py memory export \
  --config <old_root>/instance_config.yaml --out memories.json
.venv/bin/python assistant_cli.py memory import \
  --config <new_root>/instance_config.yaml --in memories.json \
  --map-person usr_old123=usr_new456 --dry-run
```

Then run the import again without `--dry-run`. If the old configuration cannot
be used, name the database instead, for example
`memory export --sqlite <old_root>/workspace/dbs/task_history.db --out memories.json`.

Rules the import follows:

- **People.** Person ids differ between instances. `--map-person OLD=NEW`
  (repeatable) says whose memory each old person's becomes. A person without a
  mapping keeps the same id only if the new instance's people records list them
  and they were not removed. Otherwise their facts are refused and listed.
  Mapped ids are checked against those records once the new gateway has
  started.
- **Team.** The team memory goes to the new instance's team, or to
  `--team-id <team>`.
- **Repeats.** A fact whose text is already in the same memory for the same
  owner is skipped, so running the import again is safe.
- **Limits.** The new instance's limits apply (`agent.memory.coremem.max_facts_per_team`,
  `agent.memory.usermem.max_facts_per_user` and the tag and metadata limits).
  Facts that do not fit are reported, not dropped silently. Facts whose end time
  has passed are skipped.
- **History.** Each imported fact is recorded as created by the memory import,
  with the original author and creation time kept in its metadata under
  `imported_from`.

The import prints counts per team and person and lists every fact it did not
import with the reason. It exits with status 1 when any fact was refused or did
not fit.

The export file is JSON (`format: universal-agents-pinned-memory`,
`schema_version: 1`). It holds the old instance's name and team id (null when
unknown) and each fact's text, tags, metadata, start and end times, author and
times, and its person id for personal facts. It holds no
secrets and no conversation, task or chat ids from the old instance. It does
hold people's memories, so it is written readable only by you and never over an
existing file: keep it private and delete it after the import.

## Message Broker

The manager, gateway, and spawned agents communicate through NATS with
JetStream-backed delivery where configured. Verify:

- `NATS_URL` or `NATS_SERVERS` points to reachable NATS servers.
- `NATS_USER` and `NATS_PASSWORD`, or `NATS_CREDENTIALS_FILE`, match the server.
- The managed local NATS server always requires a user and password. Values
  you set in `NATS_USER`/`NATS_PASSWORD` (directly, in `team.env`, or through
  `*_FILE` secrets) are used unchanged. When you
  leave `NATS_USER` and `NATS_PASSWORD` unset, the first start generates them
  and stores them in `<nats service root>/auth/credentials.json` (readable only
  by the service owner; default `/data/services/nats/auth`). Later starts and
  `scripts/reconcile_nats_consumers.py` reuse that file. The password reaches
  `nats-server` through a private configuration file, not its command line.
  Delete the file only while every runtime process is stopped.
- The configured JetStream stream exists or the runtime is allowed to create
  and reconcile it.
- Bundle mode runs the bundled NATS service unless explicitly configured for
  an external NATS deployment.

Process-scoped consumers use this crash-cleanup delay:

```yaml
transport:
  process_consumer_inactive_threshold_seconds: 300
```

Generated agent mailboxes are deleted during clean shutdown and expire after
this delay when a process crashes. Explicit `agent.agent_id` mailboxes, team
lead routes, named-worker queues, and gateway routes remain persistent. The
runtime requires NATS Server 2.10 or newer for the consumer ownership metadata;
the bundled image includes a newer pinned release.

Every durable consumer is a pull consumer: an agent asks for its next message
when it can act on it. Workers of one type share one queue and ask for a task
only while they are free, so a busy worker never holds tasks another worker
could run. Upgrading from a release that used push consumers replaces them on
the first start (each replacement is logged as `nats_consumer_replaced`);
messages still in the stream are kept and delivered by the new consumers.

## Worker pools

Delegated tasks wait in their worker type's shared queue until a free worker
takes one. The runtime starts workers while tasks wait and no worker is free, up
to each type's pool size, and workers leave again after their idle time. One
pool size per type covers delegated and scheduled work.

```yaml
agent:
  worker_pools:
    enabled: true                     # Start workers automatically for waiting delegated tasks.
    default_max_workers_per_type: 4   # Pool size until TeamArchitect sets another.
    max_workers_per_type_ceiling: 16  # Most workers of one type; TeamArchitect cannot go above it.
    max_total_workers_per_team: 32    # Most workers of the team, however they were started.
    max_launches_per_type_per_hour: 60
    scale_up_wait_seconds: 20         # A task waiting this long with no free worker starts another.
```

TeamArchitect sets pool sizes itself inside the ceilings, for a while or for
good, when it reviews a pool at its limit, a model provider that keeps refusing
calls, or workers that cannot start. Administrators get an Inbox notice of each
change and of problems that keep coming back. Only an operator changes the
ceilings. The full list of settings, with comments, is in `config.example.yaml`.

Upgrading moves `agent.schedule_workers.max_total_workers_per_team` and
`max_launches_per_type_per_hour` to `agent.worker_pools` when they were changed
from their defaults, and drops `max_auto_workers_per_type`,
`max_auto_workers_per_team` and `agent.delegation_assurance`; the configuration
upgrade step does this automatically (instance configuration version 11,
`config.yaml` version 9). Finish in-flight delegated work before upgrading: the
earlier release's pickup checks are not carried over.

## Deterministic execution

`execution_control` configures immutable code jobs and managed services. The
controller stores source, build, revision, occurrence, lease, and service state
outside the agent process. Use a manager schedule for work that needs reasoning.
Use an execution job for fixed Python or single-file Rust code.

An OpenShell profile names the exact sandbox and toolchain. A Kubernetes profile
must specify a runtime image pinned by sha256 digest, artifact helper, namespace,
service account, and artifact PVC. If the program writes state that must survive
pod replacement, configure a different `kubernetes_workload_pvc_name`. The
container receives that volume at `UA_DATA_DIR=/ua/data`; it never receives
write access to promoted artifacts.

For Kubernetes deployments, mount `<instance_root>/state/execution_artifacts`
on the controller so it refers to the same bytes as the profile's artifact PVC.
Set `execution_control.kubernetes.network_policy_enforced: true` only after the
namespace enforces the `universal-agents.io/network` labels.

SSH is disabled by default. Each `execution_control.ssh.targets` item pins the
host, port, username, host-key algorithm and fingerprint, the id of its key
material (`credential_binding_id`), identity file, and known-hosts file. Keep key files outside `workspaces/`.
An optional `remote_persistent_session_name` uses remote `tmux` for explicit
reattachment after a local broker restart.

Execution revisions accept non-secret environment entries only. Names beginning
with `UA_` are reserved. A revision that names credentials
(`broker_capability_binding_ids`) is refused, because secret injection into
deterministic workloads is not installed.

With authorization on, the execution controller checks access with the same
Cedar evaluator and access store as the agents (`authorization.cedar_binary_path`
and the manager storage). Each job, service and SSH session records who allowed
it; see [Features](features.md#deterministic-code-and-managed-services) for who
decides what. A profile with `backend: local` runs code on the controller host
outside any sandbox, so a job on it also needs the job's agent type and person
to hold lasting `full` access to `code:unconfined`, checked when the job is
turned on and before every run
([Shell and Python code](permissions.md#shell-and-python-code)).

Jobs, services and SSH sessions saved before access model 2 name approvals from
the removed permission store. The controller refuses to start while any remain
(`reason_code=old_execution_records_present`). With Universal Agents stopped, run
`assistant_cli.py execution-retire-old-records --config <instance_config.yaml>`.
It disables those jobs, sets those services to stopped (the controller stops their
workloads when it starts) and makes those SSH sessions unusable. Turn jobs on
again and restart services through TeamLead; a service needs a new approval.

## LLM and Embeddings

Non-secret model defaults belong in config:

- `llm.provider`
- `llm.model_id`
- provider-specific non-secret settings under `llm.openai_codex` and
  `llm.xai_oauth`
- OpenAI API-compatible base URL used by `llm.provider: litellm`
- embedding provider/model/api-base/dimensions

Model keys never go in config. Administrators save every model key (the
OpenAI-compatible endpoint, the embedding service, OpenAI, Anthropic, Gemini
and xAI) with `/auth model`,
which tests each key before storing it in the secure broker; agents use a
saved key on their next request, without a restart. These environment
variables override a saved key while they are set, and `/auth model` shows
when they do:

```dotenv
LITELLM_API_KEY=
EMBEDDING_API_KEY=
OPENAI_API_KEY=
GEMINI_API_KEY=
ANTHROPIC_API_KEY=
XAI_API_KEY=
```

See [Model Keys](model-providers.md#model-keys).

Fresh onboarding uses OpenRouter at `https://openrouter.ai/api/v1` and
pre-fills DeepSeek V4.1 Flash as `deepseek/deepseek-v4.1-flash`. In Universal Agents,
`llm.provider: litellm` is equivalent to selecting the OpenAI API-compatible
runtime path; a separate LiteLLM proxy is optional, not required.

Cross-provider model failover is disabled by default and starts with an empty
fallback list. Configure `llm.failover.fallbacks` and explicitly set
`llm.failover.enabled: true` only when automatic switching is appropriate for
the instance. Explicit model changes in `control/LLM_OVERRIDES.yaml`
supersede any active failover lease.

For `llm.provider: xai-oauth`, omit `llm.model_id` to use `grok-4.5` and run
`assistant_cli.py xai-oauth-login --config <instance_config.yaml>` to populate
the instance-owned auth store. `XAI_OAUTH_ACCESS_TOKEN` and its `_FILE` form
are ephemeral overrides; `XAI_API_KEY` belongs to API-key workflows and is not
accepted by the OAuth provider.

If graph memory is enabled, startup fails closed when the configured embedding
profile drifts from the active persisted graph profile.

## Access workflows

Access workflows are the plans the team lead publishes before it asks for
access; you follow them on the workflow page. Two settings bound them:

- `access_workflows.max_steps` (default 32, at most 64): the most steps one
  plan may have.
- `access_workflows.diagnostic_event_limit` (default 1000, 20 to 10000): the
  most events a diagnostic download keeps. It says when older events were left
  out.

These settings used to be `human_interactions.workflow_max_steps` and
`human_interactions.workflow_diagnostic_event_limit`. The `human_interactions`
block is removed; move the two values to the `access_workflows` block and
delete the rest (see
[Removed settings](permissions.md#removed-settings)).

## Authorization

Access control decides which agent may use which tool, for which person. How
it works (levels, grants, requests in the Inbox, blocks) is described in
[Access and approval](permissions.md). Four settings remain, in both
`config.yaml` and `instance_config.yaml`:

```yaml
authorization:
  enabled: true              # false runs trusted single-person mode
  cedar_binary_path: null    # null finds ua-cedar-authorizer
  starter_profile: trusted   # trusted, careful or manual
  presentation:
    mode: friendly           # friendly or technical
```

`authorization.enabled` (default `true`) turns access control on. With
`false`, Universal Agents runs in trusted single-person mode: every tool is
available without a grant, agents do not get the access tools, and nothing
waits in the Inbox. Platform changes, such as bounded runtime settings, new
specialists, team skills, and the new web API tools and data folders of an
access proposal, apply after the person agrees in the conversation. The signed record of whom a task works for, the secure
broker's checks, account links, the code sandbox, protected folders, and each
component's own checks still apply.

`authorization.cedar_binary_path` names the Cedar evaluator program. Agents,
the secure broker, the gateway and the execution controller all use it. With
`null` (the default), Universal Agents looks for `ua-cedar-authorizer` on
`PATH` and then in the Cargo build folder (`target/release`, then
`target/debug`); the Docker image includes it. The Cedar policies and schema
ship in `ua_definitions/access/cedar/` and are not configurable. When the
evaluator fails or reports an error, the operation is refused.

`authorization.starter_profile` chooses TeamLead's standing access for the
first user's work (the instance's user) when authorization is on: `trusted`
(default; full access to the standard tool set, handed on to workers without
asking, with a notice each time), `careful` (read access, handed on; changes
need approval) or `manual` (nothing). It is that user's default: the gateway
applies it at start, as that user, until they choose a profile themselves in
chat, on the Access page or with `assistant_cli.py access starter-profile`.
Until then, changing the setting replaces the grants the earlier profile made
at the next start; after that their own choice stays. It does not apply to anyone else: every other person
chooses their own with `/access profile` or on the Access page, and until they
do, every access for their work is asked for. See
[How agents get access](permissions.md#how-agents-get-access).

`authorization.presentation.mode` controls how much detail a refused or failed
action shows in Web Chat, such as an Access page button or an MCP control.
`friendly` (default) shows a plain-language explanation and the next step.
`technical` adds a stable error code, the HTTP-style status, the operation, and
the failure type. Internal exception text is never shown in either mode. The
setting applies to the whole instance after the gateway restarts and does not
change any access decision.

The settings of the earlier permission system (`authorization.engine`,
`fail_closed`, `policy_path`, `schema_path`, `cedar_request_timeout_seconds`,
`user_approval`, `admin_elevation`, `emergency_deny`, `permission_delegation`,
`shell_command_profiles`, and the top-level `human_interactions` block) are
removed. Startup refuses a configuration that still contains any of them and
names each one; delete them. See
[Removed settings](permissions.md#removed-settings) for what replaces each.

`workspace.skill_installation` controls the TeamArchitect-only reviewed install
pipeline. `enabled` exposes the workflow; `scanner_executable` names the
operator-owned SkillSpector binary (the supported Docker and native installers
put their isolated locked scanner on `PATH`); `scanner_timeout_seconds` limits
one static scan; `safe_max_score` may only narrow SkillSpector's LOW/SAFE band
from 20; `plan_ttl_seconds` (default one day, at most one day) is how long a
scanned plan waits for approval; and
`max_repository_bytes` caps the quarantined checkout. The selected skill has
separate 500-file, 20 MiB total, and 5 MiB per-file limits in the trusted
acquisition boundary.

Team skills belong to the team. With authorization on, TeamArchitect applies
a scanned plan with `apply_change`: a person approves each install, replacement
or removal in the Inbox, unless an administrator granted TeamArchitect full
access to `fn:team_skills` (for example `assistant_cli.py access grant --to
agent-type:TeamArchitect --tool fn:team_skills --level full`); then it applies
changes itself and administrators get a notice for each one. With
authorization off, the person's agreement in chat applies the plan.

TeamArchitect can also propose a new or changed team skill from a draft
folder an agent wrote in its task's folders (`plan_team_skill_change`). The
draft goes through the same quarantine, scan, and approval; replacing an
installed skill shows the added, removed, and changed files and keeps the old
version recoverable. Agents can read installed skills but never write them.

`workspace.external_skill_repositories` remains an operator-owned startup
refresh channel into `control/skills`. Each value is a credential-free public
GitHub repository; the importer discovers skills under directories named
`skills` at any depth. These trusted startup snapshots are distinct from an
agent-requested installation, which always uses quarantine, SkillSpector, and
the reviewed install workflow.

## MCP

An installed MCP server is one tool, `mcp:<name>`, and agents use it when they
hold access to it (see [MCP permissions](permissions.md#mcp-permissions)). Each
MCP operation needs full access until an administrator confirms a lower level,
so full access to the server covers every operation; see
[Access levels](permissions.md#access-levels).

`mcp.enabled: true` enables the secure-broker MCP control/data plane. All
onboarded `agent.external_mcp_servers` and top-level `mcp_profiles` are desired
state, not authorization grants: the runtime reconciles them into the
secret-free registry at `<instance_root>/runtime/mcp-connections` before an
agent starts. The folder is fixed; it is not a setting.
The one-time v4-to-v5 upgrade records the exact existing static baseline;
subsequent new or changed definitions wait for review in Settings before any
DNS or protocol traffic. With authorization off, each agent loads the active
servers meant for it: servers from its own configuration
(`agent.external_mcp_servers`, or `agent_types.<type>.agent.external_mcp_servers`
when its type has its own list, plus any profiles it was started with), and
servers that TeamArchitect installed or that run as managed services whose
definition names its type. With it on, an agent loads a server only while it
holds access to it, and approving a pending server in Settings gives the types
it names that access for everyone's work. In both modes agents load servers
before each step, not when they start: a server approved or installed while an
agent runs is available on its next step, and a server that does not answer
never stops an agent from starting.

An MCP definition cannot run from an ad hoc agent configuration that lacks a
persisted instance configuration and installed registry. That path fails
closed; initialize the instance and approve the reconciled definition instead.

Use Streamable HTTP for new servers. `mcp.legacy_sse.enabled` retains the rare
SSE compatibility path until its 2026-11-12 removal review. Loopback and
private-network targets are denied unless the matching `mcp.network_policy`
exception is explicitly enabled. Prefer an exact
`mcp.network_policy.allowed_private_hosts` entry for an approved internal DNS
name over the broad `allow_private_networks` switch. Exact-host exceptions
still reject loopback, link-local, reserved, multicast, and other hostnames,
and the resolved address remains pinned for the connection. The bundled
Docker configuration allows only `crawl4ai` this way. Do not put secrets in
MCP registry or hosted settings; use an environment reference or an account
link, described below.

Static operator configuration may use environment references such as
`Authorization: "Bearer ${DOCS_SEARCH_TOKEN}"`. The secret-free reference is
included in the installed definition digest, while its value is materialized
only from the secure broker process environment immediately before the
transport is constructed. Missing or empty references fail closed and identify
the variable name without logging its value. A reference `${NAME}` can also
be filled from a Docker secret file named by `NAME_FILE`; a missing or
unreadable file fails closed the same way.

A connection can instead use a credential from the credential store. Declare
where each credential field goes with `credential_injections`:

```yaml
agent:
  external_mcp_servers:
    - name: "docs_search"
      transport: "streamable_http"
      server_url_env: "DOCS_SEARCH_MCP_SERVER_URL"
      tool_name_prefix: "docs_search"
      credential_injections:
        - target_kind: header          # header for HTTP servers, env for stdio servers
          target_name: Authorization
          field_name: api_token        # the stored credential's field
          template: "Bearer {value}"   # optional; the value alone when omitted
```

A connection with credential slots becomes a catalog tool
(`mcp:<connection_name>`) that needs an account link. A team owner or
administrator links the team's account with `/link mcp:docs_search team`, or
TeamArchitect asks for a link when the broker reports that none exists. For a
call, the broker uses the link of the person the task works for first, then
the team's while the team shares the server (see
[Shared or private](permissions.md#shared-or-private); listing the server's
tools, which happens once without a person, uses the link of the person who
proposed it, then the team's); it fills the
slots when it connects, keeps one session per link, and removes any configured
header of the same name. No token is copied into chat or configuration. See
[Account links](permissions.md#account-links). Changing a server's
configuration changes its definition, so it waits for approval again.

After a link is created, the server's tools load on each allowed agent's next
step. When the broker refuses to list a server's tools (no access, no account
linked yet), an agent does not keep asking: it tries again when access changes
(such as a new account link), when the server's definition changes, or when
the agent restarts. When a server does not answer, an agent tries again after
1, 2, 4, 8 and 16 minutes, then every 30 minutes, for up to
`mcp.load_retry_window_minutes` (60 by default, 1 to 10080). Then it stops and
administrators get an Inbox notice naming the server, since when it has not
answered, and the last error; there is one notice per server per day, however
many agents gave up. Check that the server is running, then update or
reinstall it in Settings; agents try again when it changes or when they
restart. Agents see why a server's tools are missing (`load_status` in
`find_tools` and `mcp_connections(action="list")`) and can tell you. An `active`
inventory record describes installation, not a successful connection. An
OpenShell credential lease does not authorize MCP access. Removing the account
link stops further use of the credential, including through cached
connections.

An `external_mcp_servers` entry no longer takes `continue_on_error`: a server
that does not answer never stops an agent from starting. The runtime refuses
a configuration that still has it and names the entry. Deleting the key
changes the server's definition, so the server waits for approval in Settings
again.

`mcp.hosting.enabled` permits trusted data-only hosted definitions. The
definitions and state directories, local bind host, and reserved port range are
operator-owned. Agent proposals cannot contain code, imports, packages,
commands, executables, endpoints, or credentials. Hosted publication is a
separate high-impact authority and remains closed when no trusted ingress and
authentication profile is configured.

## Agent Definitions

Core agent definitions live under `ua_definitions/agent_definitions/`.
Additional shipped roles live in startup-loaded bundles under
`ua_definitions/agent_bundles/`, and remain available by default.

To add your own agent roles, place catalog files or bundle directories under
the team control folder:

```text
control/
  agent_bundles/
    custom_roles/
      agent.yaml
      prompts/
        custom_role.md
```

You can also list extra files or directories under
`agent.agent_definition_paths`; relative paths resolve under `control/`.
Validate definitions before restart:

```bash
.venv/bin/python scripts/validate_agent_catalog.py --config config.yaml
```

Agent YAML lists the tools a type works with under `default_tools`, as tool
catalog ids or tool set ids (`ua_definitions/agent_definitions/builtins.yaml`
has examples); it cannot import arbitrary Python tools. With authorization on,
`default_tools` only tell TeamLead what to give a new type, and the agent's
tools follow the access it holds; with it off, they are the agent's tools. The
removed fields `tool_capabilities` and `capability_registry_ids` are refused
when a definition loads. Runtime/model overrides for a type still belong under
`agent_types.<AgentType>` in config.

TeamArchitect-managed composition lives in `control/TEAM_AGENTS.yaml`. It
records generated agent bundle location, startup-agent overrides, and ephemeral
child-agent TTL policy. Generated definitions are ordinary bundles under
`control/agent_bundles/team_lead_generated/` and are validated
through the same catalog loader, which reads them each time a type is listed or
started. A new generated type is available to the running manager as soon as it
is created, with no restart; startup roster overrides apply the next time the
headless supervisor launches.

TeamArchitect creates or removes a specialist by staging an exact plan and
calling `apply_change`. With authorization enabled, a person approves that plan
in the Inbox; with it off, the person agrees in the conversation.
If the plan or its task ended before the approval, TeamArchitect stages a fresh
plan. Approving a specialist never gives it access to tools or lets it publish
anything; its tool calls are checked like any other agent's (see
[How agents get access](permissions.md#how-agents-get-access)).

## Secure Broker

The secure tool broker always runs: every credentialed tool call (web research
tools, GitHub, Google, Hugging Face, X and MCP servers) goes through it, so
there is no setting that turns it off. `security_layer.mode` is `broker` (the
default) or `openshell`, which also runs agent commands in an OpenShell sandbox.
Startup refuses the removed `security_layer.mode: disabled`,
`security_layer.secure_cli_enabled` and `SECURE_CLI_ENABLED` with a message
that names them; delete them. Keep `security_layer.broker_internal_base_url`
reachable from agent/gateway processes. Browser-facing auth and credential form
links derive from `network.public_origin` plus
`security_layer.broker_public_port`. In Docker, this commonly means:

```yaml
network:
  public_origin: http://127.0.0.1
security_layer:
  broker_internal_base_url: http://127.0.0.1:18600
  broker_public_port: 18600
  broker_bind_host: 0.0.0.0
  broker_bind_port: 18600
  credential_catalog:
    enabled: true
    database_path: credential_catalog.db
    template_overrides_path: credential_template_overrides.yaml
    default_write_source: bitwarden
    openshell_binding_ttl_seconds: 900
    collection_session_ttl_seconds: 900
```

For broker-only credentials, prefer:

```dotenv
SECURE_TOOL_BROKER_ENV_FILE=/data/broker.env
```

and put `BW_SESSION_FILE`, `OP_SERVICE_ACCOUNT_TOKEN_FILE`,
`KEEPASSXC_DATABASE`, `KEEPASSXC_PASSWORD_FILE`, `VAULT_ADDR`,
`VAULT_TOKEN_FILE` and `GOG_KEYRING_PASSWORD_FILE` in that separate file. The
broker maps CLI state such as `GH_CONFIG_DIR`, Bitwarden app data, and
`OP_CONFIG_DIR` under `broker_credentials/`, one folder per person.

GitHub, Google, Hugging Face and X tokens are not configuration: each person
signs in with `/link cli:github`, `/link cli:google` and `/link cli:x`; an
administrator stores the team's sign-in apps with `/auth google team_client` and
`/auth x team_client`; Hugging Face uses account links (`/link cli:huggingface`,
with `team` for the team's account); and X falls back to the team's linked X
account (`/link cli:x team`) only after an administrator shares X with
`/accounts share cli:x` (see [Shared or private](permissions.md#shared-or-private)).
Startup stops while a removed token variable (`GH_TOKEN`,
`GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`,
`GOG_CLIENT_SECRET`, `GOG_REFRESH_TOKEN`, `GOOGLE_OAUTH_ACCESS_TOKEN`,
`GOOGLE_OAUTH_REFRESH_TOKEN`, `GOG_TOKEN_FILE`, `HF_TOKEN`,
`GOOGLE_APPLICATION_CREDENTIALS` and the other old Google file variables, or
their `_FILE` forms) is set. Google tools run with each person's own sign-in
(`/link cli:google`), never a service-account key file.

### Tool setup checks

The runtime remembers, for each person and each tool that acts with an
account, whether it last worked (and how: signed in, or which account is
linked) or what it still needs, so TeamLead does not check the setup every
time it uses a tool. Every call of the tool keeps this up to date. For a tool
that is set up but nobody used for a while, the broker checks again now and
then:

```yaml
security_layer:
  tool_setup_recheck_days: 30   # 0 turns the re-check off
```

Every `tool_setup_recheck_days` days (default 30) since a person last used or
checked a tool, the broker re-checks it with the same read-only checks as
TeamLead's setup check. For GitHub (`gh auth status`) and Google (a token check
with Google) this asks the service itself; for X it checks that the sign-in is
stored (X checks it when an agent posts), and for other tools that an account
is linked (the credential is checked when it is used). When a tool that
worked needs setting up again, the person gets one Inbox notice saying what to
do, such as "Your Google sign-in no longer works. Run /link cli:google, or ask
TeamLead to walk you through it." People who are disabled or removed are
skipped. Restart the broker after changing the setting.

The credential catalog defaults to `<instance_root>/credential_catalog.db`.
The one-time import of an old `credential_bindings.yaml` catalog has been
removed: startup stops while `security_layer.credential_catalog.path`,
`security_layer.credential_catalog.legacy_owner_canonical_user_id`,
`SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_PATH` or
`SECURE_TOOL_BROKER_LEGACY_OWNER_CANONICAL_USER_ID` is set. Delete them.
The catalog stores names, purposes, usernames/emails, and password-manager source refs
for newly collected workflow credentials. Passwords and tokens are submitted
through broker forms and stay in the configured credential store. Which tool
uses a credential, and where the broker puts its values, is an
[account link](permissions.md#account-links), not part of the saved credential.

Agent web requests through the broker (`secure_requests` and
`credential.api_request`) reach only hosts that a
[web API tool](permissions.md#web-api-tools) covers. The broker adds the
linked credential and refuses `credential_key` parameters and `${VAR}`
placeholders. In OpenShell mode, `credential.bind_for_openshell` takes a
`tool_id` and gives the environment-variable (`env`) entries of that tool's
account link, for `openshell_binding_ttl_seconds`, to the sandbox of the person
the task works for; the broker picks that sandbox itself and refuses any
other. A link without
`env` entries is refused (`account_link_has_no_environment`); add `env`
entries to the tool definition and link the account again.
Credential onboarding templates help agents create the right broker form for a
service. Built-in templates live in `ua_definitions/credential_templates/builtins.yaml`;
approved agent-customized variants default to
`<instance_root>/credential_template_overrides.yaml`; see
`ua_definitions/credential_templates/credential_template_overrides.example.yaml` for a
non-secret override example. Existing bootstrap env or Docker-secret
credentials are registered by user-named env var, not copied into the catalog.

To store reusable broker auth payloads in Vault or Bitwarden instead of
long-lived broker files, configure:

```yaml
security_layer:
  credential_state_backend:
    mode: credential_source
    source: vault  # or bitwarden
    namespace: agent-credential-store
    fallback_to_files: false
```

This currently covers GitHub token auth, GitHub browser-auth token mirroring,
and Google `gog` token/client JSON imports or web-auth exports. For Bitwarden,
the broker uses stable item names such as
`agent-credential-store :: github :: token` and requires broker-side `bw` to
already be authenticated/unlocked.

With Sealbox, a missing tenant means the owner has no stored record. Reads
create no tenant; the first authorized write creates it. Existing-tenant
authentication, missing or damaged key files and other storage errors stop
the operation, even with `fallback_to_files: true`. File fallback can supply
an absent record, but cannot repair a Sealbox failure.

## Code Execution Sandbox

Agents with `shell` or `python_env` run every command in a Linux Landlock
sandbox unless `security_layer.mode` is `openshell` (see
[OpenShell mode](#openshell-mode)). What one command can reach follows the
access of the agent and of the person it works for, worked out again for each
command ([Shell and Python code](permissions.md#shell-and-python-code)). A
sandboxed command can:

- read and write the task's folders: the person's own folder (the default
  working folder) and the shared `workspaces/team` and `workspaces/agents`
  folders;
- read or write [data folders](#data-folders), folders the agent was given for
  this person's work, and what other people shared with the person, as far as
  the grants and shares allow: read-write where the agent and the person may
  write, read-only where they may only read;
- read and write a private temporary `HOME`/`TMPDIR` that is deleted after the
  command;
- read operating-system and Python interpreter files, and the team's skill
  folders (to run a skill's bundled scripts);
- use any extra paths you list outside the instance root.

Landlock can only add access, never take part of a folder away, so a folder
with a blocked folder inside it is left out of the command entirely; the
command result's sandbox note says so, and file tools still reach the rest.

It cannot read `team.env`, the instance configuration, control files,
credential files, databases, runtime records, other people's folders, or other
processes' memory and environment. The runtime sets `SKILLS_DIR` to
`control/skills` for each command.
Environment variables that name a path outside these areas (for example a
`GIT_CONFIG_GLOBAL` in your home directory) are removed for the command.
Commands may create links in the folders they can write (Python environments
need them); file tools, attachments and the gateway never follow those links.

**Network.** A command can open TCP connections and listen on a port when the
agent and its person hold [internet access](permissions.md#internet-access),
which every agent and person has until someone blocks it. The level on the
code tool does not matter for the network; installing packages with
`python_env` needs `write` on `code:python` and internet access. When internet
access is blocked, the sandbox closes TCP, which needs Landlock ABI 4 (Linux
6.7 or later). On an older kernel such a command is refused, with a message
saying so, rather than run with the network open; commands with internet
access still run. Landlock does not cover UDP, so commands can still send UDP
traffic such as DNS lookups. Internal services therefore still require
credentials that sandboxed code cannot read. With authorization off, the
network is open.

```yaml
security_layer:
  code_execution:
    sandbox: required        # or "off"
    extra_read_paths: []     # absolute paths code may read
    extra_write_paths: []    # absolute paths code may read and write
```

`required` refuses to run a command when the sandbox cannot be applied. Landlock
needs Linux 5.13 or later with Landlock enabled and a container security profile
that allows its system calls (Docker's default profile does); in Docker Desktop
and Colima, the kernel is the one in their Linux virtual machine. `off` runs
every agent command with the runtime's full filesystem access and an open
network, and logs `event=code_sandbox_disabled`; use it only when every agent
with code execution is fully trusted. With authorization off it is the only way
to run agent code outside the sandbox. With authorization on, one command runs
outside the sandbox when a person approved `full` access to `code:unconfined`
for that agent ([Shell and Python code](permissions.md#shell-and-python-code));
each such command is logged with `event=code_unconfined_run`. Extra paths must
be outside the instance root; the runtime refuses to grant any directory inside
or containing the instance root, and any directory containing the broker
credentials directory.

Command-line tools the team installed through an access proposal
([Command-line tools](permissions.md#command-line-tools-clis)) always run in
this sandbox on the secure broker's host, whatever `sandbox` says here, and
without the extra paths: they reach only system folders and their own private
run folder. On a broker host without Landlock they are refused
(`event=installed_cli_sandbox_unavailable`); the built-in tools are not
affected.

With authorization on, an agent sees `shell` and `python_env` only while it
holds access to `code:shell` or `code:python` for the person it works for now.
With authorization off, the agent types whose definitions list those tools
among their default tools get them.

### OpenShell mode

With `security_layer.mode: openshell`, commands run in
[OpenShell](https://github.com/NVIDIA/OpenShell) sandboxes instead of Landlock.
Each person gets their own sandbox, created the first time their code runs and
holding copies of only their folder and the shared `team/` and `agents/`
folders. Work that runs for no person uses a shared sandbox with the two shared
folders. Because OpenShell copies files in rather than sharing a folder with
the host, one sandbox for everyone would have given each person a copy of
everyone else's folder.

```yaml
security_layer:
  mode: openshell
  openshell:
    sandbox_name: null         # name prefix; null uses team-<instance name>
    policy_file: null          # the OpenShell policy each sandbox gets
    create_if_missing: true    # false: you create each sandbox yourself
    delete_on_shutdown: false  # true: delete this instance's sandboxes at stop
    upload_workspace: true     # copy the folders in when a sandbox is created
```

- Sandboxes are named `<prefix>-p-<part of the person's folder name>-<short
  hash>`, and `<prefix>-shared` for work with no person; the prefix is
  lowercased and cut to 24 characters. With
  `create_if_missing: false`, create each sandbox under that name first, or
  commands for that person are refused.
- Files are copied in once, when a sandbox is created. Files written on the
  host later are not in it, and files code writes inside the sandbox stay
  there; the file tools on the host do not see them.
- The OpenShell policy file decides the network and everything else a sandbox
  can reach. Folder grants, data folders and the network rule above do not
  apply to OpenShell sandboxes, and the skill folders are not copied in.
- `delete_on_shutdown: true` deletes every sandbox whose name starts with the
  prefix when Universal Agents stops.
- A command approved to run outside the sandbox (`code:unconfined`) runs on the
  host, not in OpenShell.

## Activity Collector

The activity collector needs user and admin tokens in env. Verify:

- `ACTIVITY_COLLECTOR_USER_TOKEN` is present.
- `ACTIVITY_COLLECTOR_ADMIN_TOKEN` is present.
- `activity_collector.base_url` is reachable by manager and spawned agents.
- The collector process starts in logs.

## Voice

Voice settings live under `voice`. Grok voice uses the xAI key: save it with
`/auth model` (provider xAI; onboarding does this for the key you type), or
set `XAI_API_KEY`, which overrides the saved key. Voice in a separate adapter
host process reads only `XAI_API_KEY`, because that process has no broker
access.

Voice is disabled when `voice.enabled: false`. Inbound speech-to-text and
outbound text-to-speech are controlled separately.

## Website Host and Generated Sites

The shared website app serves user docs, generated skill docs, generated
article, comparison, how-to, and app sites, plus embedded generated-site chat.
The default settings are:

```yaml
interactive_apps:
  plugins:
    builtin.internal_website:
      settings:
        enabled: true
        bind_host: 0.0.0.0
        bind_port: 18090
        public_port: 18090
        docs_root: null
        skills_dir: null
```

Use `base_url` for the browser-reachable URL. A generated page is written into
`sites/<site_id>/` of the folder of the person it was made for, and a record of
it (which folder, whose, and the SHA-256 of each file) is kept in
`state/published_sites/<site_id>.json`. The website serves a page only through
its record and only while its files still match; see
[Generated Sites](generated-sites.md).

## External Adapter Host

Channel plugins may select `execution: adapter_host` under their registry entry.
Enable `interactive_apps.adapter_host` at the same time. Same-host deployments
should use the default permission-restricted Unix socket. Cross-container or
remote deployments must use mutual TLS and configure both the gateway listener
(`bind_host`/`bind_port`) and the host connection address (`connect_host`). The
TLS client certificate identity must match `host_id` exactly.

The internal Website/Web Chat entry must remain `execution: in_process`. See
[Interactive App Plugins](interactive-app-plugins.md) for registration,
acknowledgement, reconnect, queue, attachment, and health behavior.

## Verification Checklist

- `assistant_cli.py chat` works with `--config` and `--env`.
- `/docs` returns a browser docs URL.
- `/auth status` returns broker status when broker mode is enabled.
- `/docs/sites/` opens the generated-site index when the website app is
  enabled; it lists your pages when you are signed in to Web Chat in the same
  browser.
- Enabled channels start cleanly and receive a test message.
- Proactive delivery goes to the intended default channel.
- Secrets are absent from `instance_config.yaml` and workspace files.
- `assistant_cli.py execution-job-list` returns the team-scoped job inventory.
- Kubernetes profiles can read promoted code but only a separate data PVC is
  writable by the workload.

## Bounded Objective execution

Long-running Objectives can use explicitly activated, resumable work sessions
without changing normal task execution. The capability and its built-in policy
are available by default:

```yaml
workspace:
  objective_execution_enabled: true
  objective_execution_paths:
    - "builtin://objective-execution/default"
```

This loads the profiles but does not time-box any Objective. An Objective becomes
bounded only after you explicitly accept the manager's suggestion or ask it to
enable a named profile in an authenticated request. Each run must activate the
packaged `long-horizon-plan-execution` skill; skill authorization is still
enforced. Set `objective_execution_enabled: false` to remove the capability
globally. A TeamArchitect restart is required after changing these main-config
fields; activating another Objective does not require a restart.

If the built-in profiles do not fit, discuss the intended cadence, material-work
allowance, and all total and checkpoint-reserved budgets with TeamArchitect. It
can stage an immutable custom-profile
proposal within the operator envelope. The proposal is inert until you accept
its exact id and digest in an authenticated request. Choose either:

- an `objective` profile, applied once to one active Objective; or
- a `reusable` profile, installed in your private TeamArchitect profile catalog
  for selection on future Objectives.

Installing a reusable profile does not activate any Objective. Removing it
prevents future selection but leaves already accepted Objective policies
unchanged because every policy stores a profile snapshot. Profile replacement
and Objective reconfiguration reject state drift and require a fresh proposal
if the target changed after the proposal was staged. Reconfiguration updates
the existing protected schedule rather than creating a second recurring job.

Objective execution policies are strict schema-v2 YAML layers. Configure one or
more in an ordered list; later layers add profiles, replace same-named profiles,
and override only the policy fields they declare:

```yaml
workspace:
  objective_execution_paths:
    - "builtin://objective-execution/default"
    - "objective_execution_profiles/nightly.yaml"
    - "objective_execution_profiles/high-context.yaml"
```

Relative paths resolve under `control/`. A referenced file that is
absent emits an `objective_execution_policy_file_missing` warning and is skipped,
so the manager can continue with the remaining layers. A malformed or invalid
file fails startup rather than silently weakening bounds. If no configured file
is available, the manager still starts but bounded Objective execution is
unavailable. Changes to this list or any loaded layer require a manager restart.
The former singular `objective_execution_path` field is removed; replace it with
an array before upgrading to current `config.yaml` schema 7 or
`instance_config.yaml` schema 9.

Ask TeamArchitect to render a profile with
`objective_work_profiles(action="export")`. With explicit confirmation it can save an
inert layer under `objective_execution_profiles/`; the tool cannot edit the main
config, activate the layer, or restart the manager. Add the returned relative
path to `objective_execution_paths` when you want that YAML profile to become an
operator-loaded profile.

Each profile contains its cron cadence and cumulative work-session budgets.
The cadence runs in `cadence_timezone`: `owner` (the default) is the time zone
of the person who owns the Objective, else the instance time zone; any IANA
name such as `Europe/Berlin` fixes it.
No profile budget resets per model turn. `active_seconds` is the total
wall-clock window for one session. `turns` counts model cycles across the
session. `total_tokens` accumulates input plus output tokens across those
cycles, including repeated prompt and tool-schema input. `tool_calls` counts
all tool calls across the session. The checkpoint fields reserve subsets of
those totals, and the runtime withholds them from material work. If a safe boundary is reached
without a checkpoint, it uses the reserve for a narrow checkpoint-only model
invocation and falls back to a deterministic degraded checkpoint only if that
pass cannot persist the handoff. Input, output, total, and cache token usage are
stored separately for tuning. Suggestion rules, the required skill list,
default-profile selection, activity snapshot emission, and checkpoint retention
are policy-wide rather than per-profile.

The same policy file owns `custom_profiles`; these controls are deliberately
separate from the main runtime config. The built-in envelope enables one-time
and reusable proposals, keeps them acceptable for `86400s` (24h), requires at
least `3600s` (60min) between scheduled runs, and allows 50 installed profiles
per user and TeamArchitect. Agent-authored budgets are bounded as follows:

| Budget | Accounting scope | Minimum | Maximum |
| --- | --- | ---: | ---: |
| Active duration | Total wall-clock window per session | `300s` (5min) | `28800s` (8h) |
| Wall-clock checkpoint reserve | Subset of the session window | `60s` (1min) | `3600s` (60min) |
| Model cycles | Cumulative count per session | `2` | `200` |
| Checkpoint-reserved cycles | Subset of session cycles | `1` | `20` |
| Input plus output tokens | Cumulative total per session | `25000` | `3000000` |
| Checkpoint-reserved tokens | Subset of session tokens | `10000` | `500000` |
| Tool calls | Cumulative total per session, not per turn | `2` | `500` |
| Checkpoint-reserved tool calls | Subset of session tool calls | `1` | `5` |

Each reserve must be smaller than its corresponding total. Operators can tune
these fields in a later Objective execution policy layer without changing the
ordinary task-execution defaults. The built-in profiles are:

| Profile | Cadence | Session time total / reserve | Session turns total / reserve | Session token total / reserve | Session tool-call total / reserve |
| --- | --- | --- | --- | --- | --- |
| `nightly` | `0 9 * * *` (09:00 UTC daily) | `1800s` (30min) / `300s` (5min) | `30` / `2` | `1500000` / `150000` | `150` / `5` |
| `deep` | `0 10 * * *` (10:00 UTC daily) | `5400s` (90min) / `300s` (5min) | `50` / `3` | `3000000` / `250000` | `300` / `5` |

After a `limit_*` stop, `objective_work_sessions(action="status")` reports the binding
dimension, work-token overshoot, average tokens per turn, and input/output ratio.
TeamArchitect may use those observations to propose a complete replacement
profile, but it cannot apply wider bounds without the same exact user acceptance
flow.

`agent.max_tool_calls_per_task` defaults to `150` cumulative calls across one
ordinary task-processing invocation. It is not a per-turn allowance.
An accepted Objective work profile is separate: its `tool_calls` total is the
authoritative session limit, while `checkpoint_reserve_tool_calls` holds at most
five attempts for the checkpoint-only `record_objective_work_checkpoint` pass.
The reserve does not fund research, shell, delegation, or other material work.

Objective, Plan, and bounded-session ownership survives TeamArchitect process
replacement. The durable owner is the same-team `TeamArchitect` role, represented
as `manager_scope:<team>:TeamArchitect`, rather than one process id. Startup
atomically adopts older rows from a prior same-team TeamArchitect process. It
does not transfer work across teams, roles, or canonical users. Protected
schedules already claim that team and role, so the replacement TeamArchitect
can fire and resume them without bypassing the schedule authorization boundary.
Authenticated Objective list/control also performs a targeted same-user,
same-team adoption for legacy rows whose old team metadata is absent. This is
observable and collision-safe; it never widens canonical-user, team, or named
role authority. If multiple legacy snapshots map to the same durable record,
reconciliation keeps the uniquely newest snapshot by `updated_at` and then
`created_at`, and archives each superseded snapshot in
`durable_owner_scope_reconciliation_archive` before removing it. An existing
durable record takes precedence. Equally fresh snapshots with different
payloads remain an error so startup cannot silently choose between ambiguous
workflow histories.

Profiles intentionally do not select a model, team, tools, credentials,
authorization, storage, Objective goals, Plan subgoals, or evaluation criteria.
Those remain controlled by their existing runtime, security, and Objective
configuration surfaces. Web Chat's **Runtime Controls** page reads current
Objective state and allowed mutations from the live manager, while Activity
Collector user/admin pages remain read-only history and telemetry projections.
Runtime Controls shows the current goal, Plan subgoals, progress, linked
schedules, work-policy status, and available lifecycle actions. Activity
Collector projections can additionally show the selected profile and its
`configured`, `installed`, or `one_time` source, session budgets, next run, and
recent checkpoints. TeamArchitect's `objective_work_profiles(action="list")` tool shows
the authenticated user's installed catalog and proposal history; those private
proposal records are not projected into the team-wide activity stream.

The built-in `nightly` profile allows 1,800 seconds (30 minutes), reserves 300
seconds (5 minutes) for checkpointing, and permits 1,500,000 cumulative input
and output tokens. The `deep` profile allows 5,400 seconds (90 minutes), reserves
300 seconds (5 minutes), and permits 3,000,000 cumulative tokens. Token usage
includes the system prompt, tool schemas, repeated model-cycle context, and
generated output. A TeamArchitect with a large tool set can therefore consume
tens of thousands of tokens before doing material work. Keep enough token
headroom for the final checkpoint. The runtime finishes an active model or tool
operation before applying a boundary, so actual usage can exceed a configured
limit by the cost of that final operation.
