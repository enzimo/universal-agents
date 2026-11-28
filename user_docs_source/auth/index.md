# Authentication

This section covers secure **tool-broker** authentication for GitHub, Google
Workspace tools, X, and password-manager CLIs. The secure broker runs every
sign-in; the agent never sees the tokens.

Model providers are set up in [Model Providers](../model-providers.md).
Their keys are saved per endpoint with `/auth model` (administrators and
owners); OpenAI Codex and xAI Grok sign-ins have their own pages below.

## Commands

`/link <tool_id>` is the one command for a tool's account. For GitHub, Google
and X it starts your own sign-in; for other tools it links a stored account
(see [Account links](../permissions.md#account-links)).

```text
/link cli:github              GitHub browser sign-in with MFA
/link cli:github token        paste a GitHub token instead
/link cli:github web-all      browser sign-in with the full high-privilege scope list
/link cli:google              Google browser sign-in
/link cli:google json_token   import a gog token
/link cli:x                   X browser sign-in
/link cli:x team              link the team's X account (administrators only, once the team shares X)
/accounts                     whose account agents use for each service: shared or private
```

`/auth` keeps what is not a tool's account:

```text
/auth                         your sign-in status
/auth status                  the same, plus the team's model keys
/auth model                   test and save the team's model keys (administrators; others get their own page)
/auth model me                test and save your own model keys, used only for your work
/auth google team_client      the team's Google sign-in app (administrators only)
/auth x team_client           the team's X sign-in app (administrators only)
/auth bitwarden
/auth onepassword
/auth keepass
/auth vault
```

`/auth status` lists github, google, x, bitwarden, onepassword, keepass and
vault, then each model endpoint: whether a key is saved, whether an
environment variable overrides it, and when a provider last rejected a key.
It never shows a key. Bitwarden, 1Password, KeePassXC and Vault stay under `/auth` because they
are password managers the broker reads saved credentials from, not accounts a
tool acts with.

The old `/auth github` and `/auth google` sign-ins moved to `/link cli:github`
and `/link cli:google`. Typing them now shows the `/auth` usage text, which
points to `/link <tool_id>`.

## Provider Pages

- Model providers: [OpenAI Codex OAuth](openai-codex.md) and
  [xAI Grok OAuth](xai-oauth.md)
- Secure tool broker:
  - [GitHub CLI](gh.md)
  - [Google gog](gog.md)
  - [Bitwarden CLI](bw.md)
  - [1Password CLI](onepassword.md)
  - [KeePassXC CLI](keepass.md)
  - [HashiCorp Vault](vault.md)

## Whose sign-in an agent uses

GitHub and Google use each person's own sign-in: an agent working for you uses
yours and never anyone else's. There is no team account for them, so
`/link cli:github team` and `/link cli:google team` only reply that sign-ins
are personal.

X uses your own sign-in first (`/link cli:x`). The team's linked X account is
used for people without one only after an administrator shares X with
`/accounts share cli:x` and links it with `/link cli:x team`; X is private
until then. With neither, an X request fails with `x_account_missing`.

`/auth google team_client` and `/auth x team_client` (administrators and owners
only) store the team's sign-in app that people sign in through with their own
accounts; storing it gives nobody access to anyone's data.

Hugging Face and the web API and MCP tools use account links: link your own
account with `/link cli:huggingface`, and a team administrator can link the
team's with `/link cli:huggingface team`. The broker uses your own link first,
then the team's while the team shares the service. `/accounts` shows which
services the team shares and which it keeps private (see
[Shared or private](../permissions.md#shared-or-private)). See
[X and Hugging Face](../hub-cli.md).

A tool that uses no account replies "<Tool> does not use an account, so there
is nothing to link." Neither a sign-in nor a link gives anybody access to a
tool; access still comes from grants.

None of these tools reads tokens from environment variables. Startup stops if
an old token variable such as `GH_TOKEN`, `GOG_REFRESH_TOKEN` or `HF_TOKEN` is
still set, and says which command replaces it.

## Temporary credentials

When you save a password, token, or auth file in Sealbox through the broker's
secure credential form, you can tick **Delete automatically** and choose when it
expires: a preset (**In 15 minutes**, **In 1 hour**, **In 1 day**, **In 7 days**,
**In 1 month**, **In 3 months**) or **Choose a date and time** in your local
time (at least 5 minutes from now). If you asked an agent for a temporary
credential, the form starts ticked with that time pre-filled, and you can adjust
or untick it before submitting.

Once the expiry time passes:

- The credential stops working immediately everywhere in the broker (API calls,
  CLIs, MCP servers, OpenShell leases, credential lookups, and exports).
- The broker's maintenance janitor runs every 6 hours, deletes all Sealbox
  versions and the catalog entry, revokes any account or gate links that used
  that credential, and sends one Inbox notice per revoked link with the exact
  `/link` command to link a replacement (to you for your own link, or to team
  administrators for team or gate links). No notice is sent if no link used it.
- Replacing a temporary credential through the secure form with **Delete
  automatically** unticked saves a lasting credential and clears its expiry.
- Saving over an existing credential through the secure form under a different
  layout or moving it to another source permanently deletes every version of
  the earlier stored Sealbox record(s); direct agent saves cannot drop earlier
  stored records without your form submission. If the form shows a red
  "cleanup pending" warning after saving, your new credential is already stored
  and working, and the broker queued the earlier record(s) for automatic
  deletion within 6 hours.

An agent working for you can also save a temporary secret it already holds in a
file in your workspace (such as an auth file written during a sandbox sign-in or
a temporary password written to your `scratch/` folder) directly into your
Sealbox credential store through `credential.capture_file`:

- The broker reads a secret already on disk (such as a sandbox sign-in auth
  file) directly so its value never passes through a tool parameter or reply.
  The one owner-accepted exception is a temporary password or token the agent
  already holds as text: the agent may write it once to your `scratch/` folder
  with the file tool and capture it immediately with `credential.capture_file`
  (which deletes the file); agents still never pass secret values in
  `credential.capture_file`, `credential.collect`, other broker tool
  parameters, or replies.
- Agent-captured credentials always expire (between 5 minutes and 3 months
  ahead) and obey the same immediate expiry checks and 6-hour janitor cleanup as
  form-entered temporary credentials.
- The broker deletes the source file right after saving succeeds (leaving it in
  place if the save is refused, fails, or the file changed after reading, and
  reporting if it was already gone).
- An agent can only capture into your own personal credential store, never the
  team's, and `credential.capture_file` refuses to overwrite a credential you
  entered yourself, an external-source credential, or a team credential.
  Agent-callable `credential.collect` and `credential.register_existing_source`
  also enforce the same rule over existing keys: no field may be added,
  removed, or changed between secret and metadata, only existing non-secret
  metadata values may change, and every existing secret field either stays
  untouched (agents may omit it from the request, or in
  `credential.register_existing_source` repeat its exact existing environment
  variable or password-manager reference) or, in `credential.collect` only,
  rotates to a store-generated value on its existing stored record. Agents
  cannot rotate a secret that was registered from an existing source, and
  cannot change a credential an agent captured from a file except by capturing
  a new file. Agent-callable saves refuse agent-submitted secret values and
  never let an agent name where a secret is stored: no Sealbox record names
  (`item_id`, `sealbox_key`), no Bitwarden item names on `credential.collect`,
  no Sealbox records at all on `credential.register_existing_source`, and no
  password-manager item or Sealbox record that another saved credential uses.
  This keeps an agent from overwriting your stored sign-ins or another
  credential's secret (only your secure forms, account-link forms,
  `credential.capture_file`, and store-native secret generation can store a
  secret). Every agent write records agent provenance rather than your
  approval.
- You receive one Inbox notice naming the agent type, credential display name,
  and deletion time (with no secret value). Operators can turn off capture
  notices with `security_layer.credential_catalog.notify_on_agent_capture: false`.

## What Is Sent to the Agent

Credential material is submitted directly to the secure broker. The agent sees
status and metadata, not raw secrets.

Broker auth forms are short-lived browser pages owned by the broker. They are
separate from generated sites and should be used whenever a setup
step needs tokens, OAuth payloads, API keys, or password-manager sessions.

Search results from password managers may include names, usernames, URLs,
domains, folder names, item ids, and secret field labels. They must not include
passwords, tokens, TOTP seeds, secure note contents, or session values.

## General Verification

1. Run `/auth status`.
2. Confirm enabled providers report either authenticated, configured, or a clear setup action. GitHub
   and Google report authenticated only when the service accepted your sign-in just then. X shows
   `signed_in` with your username when you signed in (the broker checks it with X when an agent
   uses it); otherwise it says how to sign in, or that the team has no X sign-in app yet.
3. Sign in with `/link <tool_id>`, or set up a password manager with its `/auth` command.
4. Retry `/auth status`.
5. Run a read-only action first before using write or login automation.
