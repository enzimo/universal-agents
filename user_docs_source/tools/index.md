# Tools and Secure Authentication

Credentialed tools are routed through the secure tool broker. The agent sends a
structured provider/action request; the broker runs an allowlisted CLI action
with broker-owned credentials.

## Security Model

- Agents do not receive raw tokens, passwords, session keys, or vault contents.
- Broker forms submit credential material directly to the broker process.
- Password-manager search returns metadata only.
- Secret use happens inside the broker for an allowlisted target action.
- Direct shell attempts for credentialed CLIs may be blocked in broker/openshell runtimes.
- The broker checks which process is calling and the signed record of whom the
  task works for, and refuses web requests to hosts that no web API tool
  covers. See [Tool access and account links](../permissions.md#tool-access-and-account-links).

## Providers

With authorization on, every agent has the web research tools (such as
`exa_search`, `exa_get_contents` and `brave_web_search_artifact`) as part of
[internet access](../permissions.md#internet-access), and `file_read`,
`file_write` and `file_move` among its standard tools; with authorization off,
an agent has the tools its definition lists. File operations stay within the
task's folders, what was shared with the task's person, and any folders the
agent was given (see
[Files and folders](../permissions.md#files-and-folders)). Use the concrete Exa
operation names in tool filters and access requests; the Python module name
`exa` is not a tool name.

Every agent also has `calculator` in both modes: exact and numeric math with
SymPy (arithmetic, solving equations, derivatives, integrals, limits, series and
matrices). It only computes: it opens no files, uses no network and runs no
code. Expressions are limited in size (for example 2,000 characters and exact
powers up to exponent 10,000), and a calculation that runs longer than 10
seconds is stopped with an error the agent can act on.

When authorization is enabled, an agent's tool list follows its access: it
starts with its role's standard tools (including web research, which is part
of internet access) and gets each other tool once it holds access for the
person it works for, on its next step and without a restart. Approving a
generated agent definition does not give access to its default tools. A worker
asks with `request_access`; TeamLead hands the access on when it may, and
otherwise a person decides in the Inbox (see
[How agents get access](../permissions.md#how-agents-get-access)). Restart
existing workers after deploying a tool-catalog correction so they load the
updated tools and invocation rules.

### Tools that load on request

One model request can carry only so many tools: OpenAI models refuse more than
128, and TeamLead's own tools were more than that. So some groups TeamLead uses
now and then are not in its tool list until a task needs them: objectives and
work sessions, capabilities and MCP, runtime settings, heartbeat, solution
variations, team skills, access workflows, access proposals and worker
pools. Plan execution and recovery stay shown.

- TeamLead's prompt lists these groups by name and purpose, which costs far
  less than their full definitions on every call. It loads one with
  `use_tools` (for example `use_tools(tool_ids=['fn:objectives'])`), and
  `find_tools` says which results load this way, listing the tools that match
  the most words of its search first. Asking to load a group that is always
  shown, such as memory, answers that its functions are already in the list.
  Loading never gives access.
- A loaded group leaves the list after 8 model calls without use and when the
  task ends; TeamLead loads it again when it needs it. Objective work sessions
  always have the objectives tools.
- An agent type whose definition names a group in `default_tools` sees it
  while it has access. Defaults do not grant permission.
- Workers may also load tools TeamLead can automatically hand on for their
  task. Loading creates no grant; access is checked when the worker calls a
  function.
- Each model has a cap, `max_tools_per_request` (128 when the model has no
  entry), set under `model_capabilities` in `control/LLM_OVERRIDES.yaml` or in
  `llm.model_capabilities` of `instance_config.yaml`. A load that would go over
  it first unloads the least recently used groups and is refused if it still
  does not fit. Required objective tools and definition defaults stay shown.
  Concurrent loads share the available space. Caps must be whole numbers of
  at least one and follow the model actually selected, including a fallback
  or a person's own model. Override edits apply between turns.
  If an agent's always-shown tools alone are over the cap,
  administrators get an Inbox notice.
- When a task needs more tools at once, TeamLead can ask for a higher cap
  (the runtime setting `model.max_tools_per_request`), for the model it uses
  or for the model a worker type uses. A worker that reaches its cap says so
  in its result, and TeamLead asks for it. The cap belongs to the model, so a
  higher one applies to every agent on that model, in everyone's work:
  an administrator approves it in the Inbox (with authorization off, TeamLead
  asks in the chat). It can only be raised, and not for a person's own model.
  One request with that many tools goes to the model before you are asked
  and again just before it is saved; if the model refuses, nothing changes
  and the reason is shown. The new cap is saved for that model in
  `control/LLM_OVERRIDES.yaml`.

```yaml
# control/LLM_OVERRIDES.yaml
model_capabilities:
  litellm:
    openai/gpt-6-luna:
      max_tools_per_request: 128
```

| Provider | CLI | User-facing setup |
| --- | --- | --- |
| GitHub | `gh` | [GitHub CLI](../auth/gh.md) |
| Google | `gog` | [Google gog](../auth/gog.md) |
| Bitwarden | `bw` | [Bitwarden CLI](../auth/bw.md) |
| 1Password | `op` | [1Password CLI](../auth/onepassword.md) |
| KeePassXC | `keepassxc-cli` | [KeePassXC CLI](../auth/keepass.md) |
| HashiCorp Vault | `vault` | [HashiCorp Vault](../auth/vault.md) |

The providers above use a sign-in or a password manager. `/link <tool_id>` is
the one command for a tool's account. GitHub, Google and X use each person's
own sign-in (`/link cli:github`, `/link cli:google`, `/link cli:x`); X falls
back to the team's linked X account (`/link cli:x team`) once an administrator
shares X with `/accounts share cli:x`. Hugging Face, web APIs
such as `http:github`, and MCP connections with credential slots instead use a
stored credential chosen by an account link: send `/link <tool_id>` in chat.
See [X and Hugging Face](../hub-cli.md) and
[Account links](../permissions.md#account-links).

The web research tools work the same way. `exa_search` and `exa_get_contents`
use `http:exa`, the Brave tools use `http:brave-search`, and
`capture_web_screenshot` uses `http:crawl4ai` (this team's Crawl4AI server).
Each tool sends its request through the broker without a key; the broker adds
the linked key (the person's own, else the team's while the team shares the
service) and sends it only to that service. Onboarding saves the Exa and Brave
keys typed there as the team's keys. A missing key is reported with the
command that fixes it, for example `/link http:exa team`, or `/link http:exa`
when the team keeps Exa private (see
[Shared or private](../permissions.md#shared-or-private)). The keys are never environment variables: startup
refuses `EXA_API_KEY`, `BRAVE_SEARCH_API_KEY` and `CRAWL4AI_API_TOKEN`.

## Broker Status

From chat:

```text
/auth status
```

Sign in to a tool:

```text
/link cli:github
/link cli:github web
/link cli:google
/link cli:google json_token
/link cli:x
```

Save model provider keys (the team's with `/auth model`, administrators and
owners; your own with `/auth model me`; see [Model Keys](../model-providers.md#model-keys)),
store the team's sign-in apps (administrators and owners only) and set up
password managers:

```text
/auth model
/auth model me
/auth google team_client
/auth x team_client
/auth bitwarden
/auth onepassword
/auth keepass
/auth vault
```

## Broker-Only Env

For Docker/headless launch, use a broker-only env file:

```dotenv
BW_SESSION_FILE=/run/secrets/bitwarden_session
OP_SERVICE_ACCOUNT_TOKEN_FILE=/run/secrets/op_service_account_token
KEEPASSXC_DATABASE=/run/secrets/operator.kdbx
KEEPASSXC_PASSWORD_FILE=/run/secrets/keepass_password
VAULT_ADDR=https://vault.example.com
VAULT_TOKEN_FILE=/run/secrets/vault_token
VAULT_KV_MOUNT=secret
```

Then reference it from `team.env`:

```dotenv
SECURE_TOOL_BROKER_ENV_FILE=/data/broker.env
```

GitHub, Google, Hugging Face and X credentials do not belong in this file.
Each person signs in to GitHub, Google and X (`/link cli:github`,
`/link cli:google`, `/link cli:x`) and links a Hugging Face account
(`/link cli:huggingface`); the broker refuses to start while `GH_TOKEN`, `GOG_TOKEN_FILE`, `HF_TOKEN` or
another removed token variable is set.

## Verification

- `/auth status` returns provider status.
- Missing auth produces `auth_required`, not a raw CLI error.
- GitHub `auth.status` succeeds after `/link cli:github web`.
- Google actions succeed after `/link cli:google` or a valid token import
  (`/link cli:google json_token`). Publish an External OAuth app to In Production before authorization to avoid Google's fixed 7-day Testing token lifetime.
- Password-manager `credential.search` returns metadata without secret values.
- Credential-backed GitHub login succeeds without printing the token.
