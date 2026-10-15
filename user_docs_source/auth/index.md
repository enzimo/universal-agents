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
