# Onboarding

Onboarding creates a durable agent instance with runtime config, team control
files, and an env file for secrets.

## Outputs

An onboarded instance normally contains:

```text
<instance_root>/
├── team.env
├── instance_config.yaml
├── onboarding_reports/
├── control/
│   ├── TEAM_AGENTS.yaml
│   ├── TEAM_TEMPLATES.yaml
│   ├── LLM_OVERRIDES.yaml
│   └── SOUL.md
├── dbs/
├── state/
└── workspaces/
    ├── users/
    ├── team/
    └── agents/
```

`instance_config.yaml` is the runtime configuration for gateway commands and
headless launch. `team.env` stores generated internal credentials and operator
secrets. Control files hold TeamArchitect behavior and model/team guidance;
optional onboarding user context is stored in owner-scoped user memory. See
[Instance Folders](configuration.md#instance-folders) for what each folder holds
and who can reach it.

## Interactive Onboarding

Start with the [Quickstart](quickstart.md) or download and open the
[offline setup guide](/docker-compose-setup.html). The guide covers Colima on
macOS, WSL 2 and Docker Desktop on Windows, and Docker Engine on Linux. Choose
your data folder and ports before installing; the defaults work for a first instance.

Open [http://127.0.0.1:18090/](http://127.0.0.1:18090/) after startup.
The installer keeps instance data under `~/.universal-agents/data`.
For deployments with separate SearXNG and Crawl4AI services, see the
[Docker reference](docker.md).

During onboarding, choose the new-user path for a fresh instance or the UA
migration path when you already have `instance_config.yaml`, `config.yaml`, or
`team.env` files to import. To move a whole team (people, agent types,
schedules, memories, knowledge, sign-ins and keys), choose **Import a team
export** instead: see [Import during onboarding](team-transfer.md#import-during-onboarding).

The import fills in the form only. It reads the settings the form asks for
from a file of any earlier version and lists what it left out: settings this
version removed, the old instance's folder and name (the new instance keeps
its own), and sections onboarding never imports, such as access settings, MCP
servers and agent types. Set those up again in the new instance. A file that
is not valid YAML is reported in plain words.

The Secrets page also asks who uses the accounts you set up. **Share
accounts with people you add** is on by default; below it, each service has
its own toggle: on means shared (everyone's agents may use your account
unless a person links their own), off means private (only your own work uses
it, and people you add link their own). The model keys start shared, as do
Exa, Brave Search and Hugging Face; X starts private; GitHub and Google are
always private, because they use each person's own sign-in; Crawl4AI is
always shared, because it is the team's own server. Turn the card off to keep
everything else for your own work. Review shows the choices,
and you can change them later with `/accounts` (see
[Shared or private](permissions.md#shared-or-private)). The choices are
recorded once, when the assistant first starts, so a later `/accounts` change
is never undone by a restart.

The Exa and Brave Search keys you type during onboarding are not written to
`team.env`. Onboarding keeps them in a private file under `state/` that no
agent can reach, and when the assistant starts, the gateway saves them in the
credential store (`http:exa` and `http:brave-search`) and deletes the file: as
the team's keys for a shared service, and as your own for a private one.
Administrators then see an Inbox notice for the team's keys, and you see one
for your own. If they cannot be
saved (for example the secure tool broker is not running yet), they stay in
that file and are tried again at the next start. A `team.env` you import keeps
its other values, but its `EXA_API_KEY` and `BRAVE_SEARCH_API_KEY` are handed
over the same way, and a `CRAWL4AI_API_TOKEN` is left out: link it with
`/link http:crawl4ai team` and `/link mcp:crawl4ai team`. To change a key
later, send `/link http:exa team` or `/link http:brave-search team` for the
team's, or `/link http:exa` for your own. The `assistant_cli.py onboard`
wizard does not ask about sharing, so every service starts where the table in
[Shared or private](permissions.md#shared-or-private) says.

When every secret reaches the container as an environment variable or Docker
secret (for example Docker Swarm), turn on **Secrets come from the environment
(Docker secrets). Don't write team.env.** on the same page. See
[Secrets from the Environment](docker.md#secrets-from-the-environment).

The Communication page validates enabled channels before you leave it. For
example, if Signal is enabled, the registered Signal account/number is required;
either fill that field or turn Signal off before moving to Review.

The repo-local CLI wizard remains available for development and non-Docker
launches:

```bash
.venv/bin/python assistant_cli.py onboard
```

Useful prompts and decisions:

- Instance name and instance root.
- User display name and timezone.
- Agent name, purpose, response style, and autonomy.
- Preferred delivery channel for proactive messages.
- Message broker, storage, LLM, embedding, voice, activity collector, and security layer settings.
- Whether to enable Telegram, WhatsApp, Discord, Signal, Photon/iMessage, and secure CLI broker support.
- Which [model provider](model-providers.md) to use. Browser onboarding supports
  OpenAI, OpenAI Codex OAuth, xAI Grok OAuth, Anthropic, Google AI,
  OpenRouter, and OpenAI-compatible endpoints. Fresh setups select OpenRouter
  and pre-fill DeepSeek V4.1 Flash as `deepseek/deepseek-v4.1-flash`.
- Whether the chosen model accepts images, audio, and native file/document
  inputs. These are independent capability switches; files include text and
  Markdown as well as supported PDF, office, spreadsheet, CSV, and HTML
  document blocks.
- Provider-specific reasoning effort and the context-window token size. The
  reasoning field is editable because providers use different values; the
  context window must be at least 1024 tokens when set.

## Non-Interactive Onboarding

Use this for scripts, repeatable local setup, or Docker automation:

```bash
.venv/bin/python assistant_cli.py onboard \
  --non-interactive \
  --instance-name nova \
  --instance-root ./instances/nova \
  --display-name "Nova" \
  --timezone America/Chicago \
  --model-provider litellm \
  --model-id gpt-5 \
  --security-layer-mode broker
```

Add `--secrets-from-environment` when every secret comes from environment
variables or Docker secrets: onboarding then writes no `team.env`, generates
nothing, and prints any required secret the environment lacks. It cannot be
combined with `--base-env-file`, `--generate-internal-credentials` or
`--rotate-internal-credentials`.

Add channel flags only for services you intend to run:

```bash
--enable-telegram --telegram-allow-chat-id 123456789
--enable-whatsapp --whatsapp-public-base-url https://example.ngrok.app
--enable-discord --discord-command-guild-id 123456789012345678
--enable-signal --signal-account +15551234567
--enable-photon --photon-project-id <project_id>
```

## Base Config and Env

Onboarding may copy non-secret defaults from a generic `config.yaml`:

```bash
.venv/bin/python assistant_cli.py onboard \
  --base-config config.yaml \
  --base-env-file debug.env
```

The command line and the web onboarding page copy the same settings from it,
including the model and embedding settings, `gateway.users`, `tool_policy` and
`execution_control`. Running `onboard --base-config` again on an existing
instance reapplies the base config's team id, storage, model, embedding,
agent and agent-type, workspace, delivery, network, activity collector, voice,
internal website, `ui` and `execution_control` settings. It keeps the
instance's own security layer, other chat channels, MCP profiles,
`gateway.users` and `tool_policy`.

`--base-config` takes a `config.yaml`. Given an `instance_config.yaml`, it stops
and says so: use the web onboarding import to carry an old instance's
settings into a new one, or run `onboard` with `--instance-root` pointing at the
existing instance to change it.

Precedence:

1. optional `--env`
2. optional `--base-config`
3. interactive answers or explicit flags
4. generated `<instance_root>/team.env`, seeded by optional `--base-env-file`
   (not written when `launch.secrets_source` is `environment`)

## Secrets

`launch.secrets_source` in `instance_config.yaml` says where secrets come
from: `env_file` (default) keeps them in `team.env`; `environment` reads them
only from environment variables and Docker secrets, and onboarding writes no
`team.env` ([Secrets from the Environment](docker.md#secrets-from-the-environment)).

Model keys are not among them: the key you test on the Runtime Basics page,
and the embedding and xAI voice keys, are saved in the secure broker's
credential store when the assistant first starts (in both modes), and
administrators change them later with `/auth model`. Setting
`LITELLM_API_KEY`, `OPENAI_API_KEY` and the other model key variables still
works, but they then override the saved keys (see
[Model Keys](model-providers.md#model-keys)).

The first authorized model-key save creates the team's Sealbox tenant. The
gateway removes the private onboarding handover only after the broker reports
that the key was saved. If storage fails, it retains the handover for the next
start and reports the failure. Repair an existing tenant's missing or damaged
files from a backup, then restart the assistant to retry the handover.

Keep these in `team.env`, Docker secrets, or ambient process env:

```dotenv
POSTGRES_DSN=
TELEGRAM_BOT_TOKEN=
DISCORD_BOT_TOKEN=
PHOTON_PROJECT_ID=
PHOTON_PROJECT_SECRET=
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_WHATSAPP_FROM=whatsapp:+15550001111
ACTIVITY_COLLECTOR_USER_TOKEN=
ACTIVITY_COLLECTOR_ADMIN_TOKEN=
SECURE_TOOL_BROKER_TOKEN=
```

Do not store these secrets in `instance_config.yaml`, control or workspace files,
or chat messages.

Onboarding always generates `SECURE_TOOL_BROKER_TOKEN`, the secure tool
broker's secret. The broker always runs, because every credentialed tool call
(web research, GitHub, Google, Hugging Face, X and MCP servers) goes through
it, so onboarding has no switch that turns it off. The supervisor gives each
other process its own token made from it. If no token is set, the launcher
stops before starting anything and prints the command that adds the token to
`team.env`. Onboarding also writes the task-context signing keys
(`ACCESS_GATEWAY_SIGNING_KEY`, `ACCESS_SCHEDULER_SIGNING_KEY`,
`ACCESS_CONTEXT_VERIFY_KEYS`) that sign the record of whom each task works for.
Do not edit or copy them. See
[Tool access and account links](permissions.md#tool-access-and-account-links).

xAI Grok OAuth and OpenAI Codex OAuth use their own instance-owned auth stores
or ephemeral access-token files, not `/auth`. Model API keys are saved with
`/auth model`. See [Model Providers](model-providers.md).

## First Verification

After onboarding:

```bash
.venv/bin/python assistant_cli.py chat \
  --config <instance_root>/instance_config.yaml \
  --env <instance_root>/team.env
```

Then verify:

- The CLI starts without config migration errors.
- The gateway can connect to the message broker.
- A simple message receives a manager response.
- `control/SOUL.md` matches the intended TeamArchitect behavior, and the
  optional onboarding context is visible only in the bootstrap user's memory.
- `/docs` in chat returns the user-docs URL.
- `/auth status` reports broker status when secure CLI is enabled.

## Rerunning Onboarding

Rerunning onboarding reconciles existing config rather than replacing it
wholesale. Existing valid values are preserved unless the rerun supplies a
specific flag. Each run writes a value-free report under
`<instance_root>/onboarding_reports/`.

Use `--rotate-internal-credentials` only when you intentionally want new
generated broker, activity collector, or secure-tool tokens. It also replaces
the task-context signing keys, so rotate while the instance is stopped. A rerun
without that option adds missing internal credentials, such as the signing keys
of an instance created by an earlier version, and keeps existing values.
