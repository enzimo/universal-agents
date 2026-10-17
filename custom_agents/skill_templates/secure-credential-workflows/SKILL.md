---
name: secure-credential-workflows
description: Use for credentialed tools, secure_cli, credential catalog work, password-manager storage, account links, brokered auth, secure HTTP requests, OpenShell credential leases, signup flows, or GitHub/Google/Bitwarden/Home Assistant credential workflows.
---

# Secure Credential Workflows

Use this skill before acting on requests involving credentials, authentication,
password managers, brokered CLI/API access, `secure_cli`, credential collection
forms, account links, credential deletion, signups, OpenShell credential leases,
or catalog-managed tokens.

For a goal that also needs tool access or subsequent work, first map the complete
flow with `submit_access_workflow` as described in `runtime-operations-workflows`.
Access workflows (`fn:access_workflows`) and `propose_access_change`
(`fn:access_proposals`) load on request: load them with
`use_tools(tool_ids=['fn:access_workflows', 'fn:access_proposals'])` first.
Keep account links and later reviews on that workflow. Reuse an existing account
link; a later setup timeout does not justify linking the account again.

## Core Rules

- Never ask the user to paste passwords, tokens, generated secrets, TOTP seeds,
  or credential files into chat, task messages, shell commands, Python, or
  normal tool parameters.
- Use broker-owned secure credential collection for user-entered secrets. The
  terminal structured output must contain a
  `secure_credential_collection_request` action with the matching payload.
- Only TeamArchitect has the `secure_credential_collection_request` and
  `signup_automation_request` actions and `set_up_tools`. As a specialist,
  create a `manager_escalation` action with
  `escalation_type=needs_user_input` that names the service and what the task
  needs it for; TeamArchitect sets it up with the person and answers.
- The gateway creates secure broker form URLs only after it receives that
  structured request. Never answer with a normal `task_response` claiming a form
  is ready.
- Do not use `credential_catalog.credential.collect` to start user credential
  entry. That action stores values after the broker has safely received them.
- Do not use `signup.prepare` as a form trigger. It only validates a signup
  proposal and cannot create or display a form.
- Credentialed CLIs that authenticate through web flows, including `gh`, `gog`,
  and `bw`, remain broker-owned. The shell policy blocks those CLI entrypoints
  before execution, including nested shell attempts. Use `secure_cli` for
  metadata and broker actions instead of raw CLI commands.
- The shell policy also blocks explicit localhost and loopback targets. Use
  `secure_cli`, `secure_requests`, or another broker-owned tool for broker,
  Sealbox, NATS, Postgres, and similar local-service access instead of trying an
  alternate shell executable.
- Credential operations always apply to the registered user attached to the
  active task or gateway session. Never add `canonical_user_id`, `owner_id`, or
  another user's channel identity to model-authored tool params. If the broker
  reports that trusted owner context is missing, stop and report that identity
  routing problem instead of searching a global store.
- Treat `canonical_user_required` or "active registered canonical user" as a
  runtime identity propagation failure, not missing credentials or expired
  provider authentication. Do not recommend re-authentication. For scheduled
  work, report the schedule/job id and ask the operator to verify its persisted
  owner and fired-task gateway context.
- The same logical key may exist for multiple users. A successful lookup,
  collection, update, API hydration, auth flow, or deletion concerns only the
  current user's owner scope.
- Do not repeat an identical broker validation failure. If the broker reports
  an invalid or missing `credential_key` even though the intended key contains
  only supported characters, inspect the emitted tool payload. Put
  `credential_key` inside `secure_cli.params`, correct any missing or misnested
  fields, and retry once with a materially different payload.
- When a trusted credential, signup, deletion, or tool setup result resumes
  the task, describe the outcome in short, natural language. Name the service,
  tool, or affected agent when that context is available. Do not quote machine
  status identifiers, reason codes, credential keys, link ids, broker metadata,
  or routine secret-handling assurances in the user-facing answer. Do not
  repeat a form, signup, sign-in or account link step that the structured
  result says already completed.

## Tool Setup: Sign-ins, Account Links and Team Sign-in Apps

Tools that act with an account must be set up once for the person the task
works for:

- **Sign-in**: GitHub (`cli:github`), Google Workspace (`cli:google`) and X
  (`cli:x`) use each person's own sign-in. The broker uses it only for work for
  that person, never for anyone else; agents never see its tokens and never
  ask for them in chat. A person's own account link for these tools is refused
  (`account_link_uses_sign_in`).
- **Team sign-in app**: Google and X sign people in through the team's sign-in
  app (the Google OAuth client, the X app), which a team administrator or owner
  sets up once (`/auth google team_client`, `/auth x team_client`). A person
  may also paste their own Google client on the Google sign-in page.
- **Account link**: tools marked `linked_credential` in the tool catalog
  (`cli:huggingface`, web API tools such as `http:github` or an
  operator-defined `http:portainer`, MCP connections whose configuration
  declares credential slots, `mcp:<connection_name>`) use an account link: which
  stored credential the broker uses for that tool, for one person or for the
  team. The broker's form finishes the job in one step: the person picks a
  stored credential that fits, or types a new one that the broker saves in the
  owner's credentials and links at once. X is the one sign-in tool that also
  takes the team's linked account as a fallback; GitHub and Google have no team
  account, and the GitHub API (`http:github`) takes only each person's own
  account link.
- When a tool is used, the broker picks the person's own link first, then the
  team's while the team shares the service (for X: the person's own sign-in
  first, then the team's link while the team shares X; for an MCP connection:
  the connection owner's own link first, then the team's while shared).
- **Shared or private**: for each service that takes both a person's and the
  team's account, the team chooses. Shared: the team's account serves anyone
  without their own. Private: the team's account is never used, not even for
  work that serves no person, so each person's work needs their own sign-in or
  link, and a team account link is refused. X starts private; Exa, Brave
  Search, Hugging Face and new web API tools and MCP servers start shared;
  the team's own Crawl4AI server (`http:crawl4ai`) takes only the team's token
  and is always shared. `/accounts` shows the choices; only administrators and
  owners change them (`/accounts share <tool id>`, `/accounts private <tool
  id>`). For a private service, offer only the person's own account and never
  request the team's: a `propose_access_change` account link or an access
  workflow step for the team's account of a private service is refused. When
  the person asks why, say that the team keeps the service private and that
  an administrator decides whether to share it.
  `set_up_tools` already asks only for the accounts that would be used.
  Nobody passes a credential key or link id to a tool; X and Hugging Face
  actions take no account selector, and web requests carry no `${...}`
  placeholders.
- Setting a tool up grants nothing: whether an agent or person may use the tool
  still comes from their grants. A missing account and a denied tool are
  different problems; a new link cannot fix a denial, and an approval cannot
  supply a missing account.

Trusted code keeps each person's last known setup state per tool (`ready` with
how and when it last worked, `needs_setup` with the missing step and who can
do it, or `unknown`) from every broker call of the tool, every setup page and
`/link`, every `set_up_tools` check, and a periodic re-check of ready tools.
TeamLead's context shows the person's states in one line ("Tool setup for the
person you work for: GitHub ready (as exampleuser, worked today); Google needs the
person's sign-in"). A tool listed as `ready` is used without any check. A call
that fails for a setup reason flips the state by itself.

TeamLead (TeamArchitect) makes setup effortless, in one call, without asking
the person whether to start (owner's decision 2026-09-28): the approval cards
and setup pages are where the person agrees, so asking first is a wasted round
trip. A question about what TeamLead can access ("What repos can you see in
GitHub?") counts as a request to set up and answer. For a tool listed as
needing setup or unknown, or not listed, and whenever a call is refused for a
missing sign-in or account (`auth_required`, `account_link_missing`,
`x_account_missing`, `google_oauth_client_missing`):

1. A specialist does not ask the person itself. It escalates to TeamLead with
   `manager_escalation`, quoting the tool id from the error. Keep the existing
   parent-child task relationship; a task-routing failure is not a missing
   account.
2. TeamLead calls `set_up_tools` immediately with the tool ids, the worker's
   `agent_type` when it will delegate, the `level` the work needs (read by
   default) and the request in a few plain words as `purpose` ("list the repos
   you can access in GitHub"); `extra_steps` adds TeamLead's own steps to the
   workflow. Trusted code then does the rest:
   - checks access for TeamLead, the worker and the person; the person's
     sign-in or linked account; the team's sign-in app; and the tool's catalog
     `setup` data: steps done outside Universal Agents (such as creating a
     token on the service's site) and tools to set up first;
   - an administrator's step for a person who is not one (the team's sign-in
     app, missing access for the person, a block, an administrator's setup
     step): nothing is asked or opened, and the call returns
     `needs_administrator` with plain `say` lines. Tell the person and end the
     task;
   - asks for TeamLead's missing access (the person decides in the Inbox) and
     hands the worker its access when TeamLead may (otherwise the worker asks
     for it when it starts);
   - opens every sign-in, account link, team sign-in app page and outside step
     in the person's chat at once, in order where one needs another (the
     team's sign-in app before the sign-in, an outside step before the account
     link, a prerequisite tool before the tool);
   - two or more things to do: one access workflow page with a step for each
     and a final work step; one chat message links it before any page opens.
     The steps update
     by themselves, and the person marks outside steps done there;
   - pauses the task until everything is done, offers an expired page again
     by itself (at most three pages per tool and kind), and stops when the
     person closes a page or denies a request.
3. The call returns per tool `ready` and `what_happened` ("Signed in to GitHub
   as exampleuser."). `ready`: do the work, starting with a
   read-only check such as `auth.status` before a write. `not_ready`: tell the person plainly what is
   not set up and that they can do it any time with `/link <tool id>`; never
   ask again for something they closed or denied. When it returns a
   `workflow`, trusted code has closed its setup steps; mark its work step
   (`work_step_id`) running with `update_access_workflow`, and complete it
   when the person has the answer.

The person may also finish a setup with `/link` or `/auth ... team_client`
while the task waits; the run hears about it the same way. If the wrong
account was used (for example `auth.status` returns another username), call
`set_up_tools` again after the person unlinks it; a new link replaces the
owner's earlier link for that tool. Never put a credential key or value,
token, password, header value, endpoint, or environment material anywhere. The
account link form takes a new credential when none is stored yet; collect one
beforehand with a `secure_credential_collection_request` (with
`continue_task=true`, and wait for `credential_stored`) only when it must come
from a template the form cannot take, such as X's OAuth file (`xurl_oauth2`).

People can also set tools up themselves with one command for every tool's
account: `/link <tool id>` for their own sign-in or account, `/link <tool id>
team` for the team's (administrators only), and `/auth <provider> team_client`
for the team's sign-in app. The chat where they ran it gets one message saying
how it ended: linked or signed in, closed, expired, or not confirmed (then they
check Settings → Access → By Tool, or `/auth status` for sign-ins).

Model provider keys (the keys agents' model requests use) are not tool
credentials and never pass through you: an administrator saves each one for
its endpoint address with `/auth model`, which tests the key before storing it
and needs no working model. Anyone can save their own key for an endpoint
the team uses, or their own model (any provider and address) for all their
work, with `/auth model me`; agents then use it only for that person's work.
Administrators decide whether a person's work may use the team's keys
(`/user team-keys <usr_...> on|off`), and the team's model-key sharing
(`/accounts share model`, `/accounts private model`; shared until someone
chooses) decides it for people added from then on; someone without them and
without their
own model is told "You haven't set up your own model yet. Use /auth model me."
and their requests do not run; the message also names `/setup`, which lists
everything they still set up for their own work (a newly added person sees
that list on their first message). When a person reports that a model key was
rejected, asks to change one, or gets that message, tell them that: their own
key or model with `/auth model me`, the team's with `/auth model`
(administrators). Never ask for a key, never
put one in a file, setting or message, and never collect one with a credential
request. `/auth status` shows each endpoint's key state, including an
environment variable such as `LITELLM_API_KEY` overriding the saved key (then
the operator removes it from `team.env` and restarts the container).

For an installed MCP server that declares credential slots, the account link is
for `mcp:<connection_name>`. After a completed setup, retry: the server's tools
load by themselves on the next step. Do not create a duplicate connection or
edit YAML. Treat inventory `active` as installation state and
`operator_configured` as the configuration's baseline, not proof of successful
authentication.

The broker checks every `secure_cli` and MCP call against the access your
agent type holds and the access of the person the task is for. A refused call
returns `status: "access_denied"` with `tool_id` (for example `cli:google`,
`cli:github`, `mcp:<connection>`, or `http:<name>`), `operation`, `level`,
`reason`, and `next_step`. Call `request_access` with that `tool_id` and
`level` and a plain reason people will read; do not choose a
broader tool or level yourself. When TeamLead may hand the access on, you get
it at once. Otherwise your task pauses until a person decides in the Inbox,
and the call returns the decision. Never ask people to grant access in chat.

Retry only the same non-secret broker operation, and only when the result says
`granted`. An approval for reading only does not cover a write action. After
`denied`, do not retry or use another tool to get around it. Access does not
replace the broker's own checks: account links, credential ownership, provider
validation, action allowlists, expiry, and redaction still apply. With
authorization off, agents do not have the access tools; use the tool normally
(it is one of your default tools) and rely on the same broker checks.

## Discovering Stored Credentials

When the user asks what credentials are stored, available, or configured:

1. When a `credential_key` is known, when a recent
   `SecureCredentialCollectionResult` returned `credential_stored`, or when the
   user names a stored workflow credential, inspect catalog metadata first with
   `secure_cli` provider `credential_catalog` action `credential.get_metadata`.
2. Treat the current user's Sealbox tenant as the default live credential store
   for broad inventory, but
   do not use `sealbox.credential.search` as proof that a known catalog-managed
   credential is missing.
3. API tokens, OAuth client secrets, service-account files, and other custom
   secret fields may be stored as Sealbox `secret` records and referenced only
   through the catalog. They will not necessarily appear in Sealbox
   username/password `credential.search` results.
4. Use `secure_cli` with `provider="sealbox"` and
   `action="credential.search"` for metadata-only discovery of live
   username/password credential records when no catalog key is known.
5. Do not infer the answer from another user's result, Bitwarden status, memory, filesystem scans,
   environment variables, or credential templates alone.

## New Named Credentials

When a workflow needs a new named credential:

1. Use `secure_cli` provider `credential_catalog` template actions first:
   `template.list`, `template.get`, and `template.render_request`.
2. Prefer a repo built-in or approved template when one fits.
3. You may augment non-secret template details for the specific use case before
   prompting the user.
4. Propose the service, URL/domain, metadata-visible username/email, credential
   key, purpose, store target, field names, and source type. Use the field
   names the tool needs, so the account link form can offer the credential.
5. If required choices are missing or need approval, ask through a terminal
   `task_query` action.
6. Storing the credential does not link it. When a tool needs it, TeamArchitect
   requests an account link afterwards (see Tool Setup). A credential that
   only one tool's account needs, such as an API token, can instead be typed
   straight into the account link form.
- When the user has already chosen an API/token credential and identifies the
  service as GitHub, use the built-in `github_pat` template directly. Do not
  ask whether they want a GitHub username/password credential unless they
  explicitly change the credential kind away from API/token.

## Updating Or Repairing Credentials

- First call `credential_catalog.credential.get_metadata` for the named
  `credential_key` when available.
- Reuse the same `credential_key` in the secure credential collection request so
  the broker edits the existing password-manager item and refreshes catalog
  metadata.
- If no catalog metadata exists but the user names the exact key to repair,
  reuse that key.
- Do not invent a new credential key unless the user asks for a separate
  credential.
- Bootstrap credentials already supplied through `team.env`, a broker env file,
  or Docker secrets should be registered by user-named env var with
  `credential_catalog.credential.register_existing_source`; do not scan env
  vars or ask the user to paste the secret.

## Deleting Credentials

When the user asks to delete a credential:

1. Do not tell them to use the Sealbox CLI.
2. Do not attempt a direct destructive `secure_cli` action.
3. Identify the credential with `sealbox.credential.search` and, when available,
   `credential_catalog.credential.get_metadata`.
4. Return a terminal structured action with
   `action_type="secure_credential_collection_request"` and
   `secure_credential_collection_request.flow="credential_deletion"`.
5. Include the stable `credential_key`.
6. If the credential is only known from live Sealbox search metadata, include
   `source="sealbox"` and `source_item_id` from the Sealbox credential ref.

The gateway will show a broker-hosted deletion confirmation form, and the broker
will dispatch the current Sealbox all-version delete command after user
confirmation.

## Signups

For new service signups, attempt approved signup automation when practical. Stop
automation and request manual signup through the secure broker form if CAPTCHA,
bot detection, MFA, passkey-only registration, payment or identity verification,
blocked browser automation, repeated unexplained submission failure, or extra
human terms confirmation appears.

## OpenShell Credential Use

- OpenShell credential use is per use. Code in the OpenShell sandbox receives a
  linked account's values only as environment variables, and only when the
  tool's account names environment variables for its credential fields.
- Request a lease with `credential_catalog.credential.bind_for_openshell` and
  `params={"tool_id": "<tool id>"}`, for example `http:github`. The optional
  param is `provider_type` (default `generic`). The broker gives the values to
  the sandbox of the person the task works for; it picks that sandbox itself.
  This needs full access to that tool. The broker uses the tool's account link (the
  person's own first, then the team's while the team shares the tool) and
  returns a `lease_id` and the variable names, never the values.
- `account_link_missing` means no account is linked for that tool; follow
  Tool Setup. `account_link_has_no_environment` means the linked account
  names no environment variables, so there is nothing to give the sandbox. Use
  brokered web requests instead, or ask TeamLead to have the operator add
  environment variables to the tool definition and then link the account again.
- Release the lease with `credential_catalog.credential.release_openshell_binding`
  and its `lease_id` when finished.
- Never retrieve the password into shell, Python, task text, or normal tool
  params.

## Brokered HTTP Requests

For broker-proxied HTTP calls, prefer `secure_cli` provider `secure_requests`.
Use it like a brokered `requests` library:

- Pass method, URL, headers, query params, JSON/body, and timeout.
- Each request belongs to the web API tool (`http:*`) in the tool catalog that
  covers its host. The broker refuses a host that no web API tool covers
  (`access_unlisted_host`). Ask TeamLead to have the operator define a web API
  tool for that service, or use the web research tools for public pages.
- If that tool needs an account, the broker adds the linked credential where
  the tool definition says (a header or a query value) and sends it only to the
  tool's hosts. Requests carry no credentials and no `${...}` placeholders: the
  broker refuses placeholders (`api_request_placeholders_unsupported`) and
  refuses a header or query value that the linked account already sets
  (`api_request_header_reserved`).
- `account_link_missing` means no account is linked for that tool; follow
  Tool Setup.
- Redirects are not followed, so credentials never reach another host. A 3xx
  response includes `redirect_location`; make a new request to it, which the
  broker checks again.
- The broker returns only redacted response content.
- Do not use the Bitwarden provider, `bw`, shell, or Python to fetch or decrypt
  catalog-managed tokens.

The operator defines web API tools in the instance's `control/access/tools/`
folder: their hosts, any named routes with their levels, and which credential
field goes into which header or query value. Agents cannot define or change
them. `credential.api_request` behaves the same way as `secure_requests`.
Brokered web requests do not need `credential.bind_for_openshell`.

### Porkbun API

Porkbun accepts its keys in the `X-API-Key` and `X-Secret-API-Key` headers, so
the broker can add them. A credentialed Porkbun call needs:

- a web API tool for `api.porkbun.com` that the operator defined (for example
  `http:porkbun`), which sends the credential's `api_key` field in `X-API-Key`
  and its `secret_key` field in `X-Secret-API-Key`;
- a stored Porkbun credential with those fields, for example catalog key
  `porkbun_api`; and
- an account link from that credential to the tool.

1. Inspect the stored credential's metadata with this exact shape:

   ```json
   {
     "provider": "credential_catalog",
     "action": "credential.get_metadata",
     "params": {"credential_key": "porkbun_api"},
     "reason": "Inspect the stored Porkbun credential metadata."
   }
   ```

2. Call the current Porkbun API host through `secure_requests`. Use
   `json_body` for the request object so provider-compatible tool schemas do not
   confuse it with the boolean `json` option on `google.gog.exec`. The domain
   belongs in the `checkDomain/<domain>` URL path. Do not use `/domain/check`
   and do not move the domain into a `json_body.domain` field. Do not put keys
   in the body; the broker adds them as headers:

   ```json
   {
     "provider": "secure_requests",
     "action": "post",
     "params": {
       "url": "https://api.porkbun.com/api/json/v3/domain/checkDomain/example.com",
       "json_body": {},
       "timeout": 30
     },
     "reason": "Check example.com availability with the linked Porkbun account."
   }
   ```

   `access_unlisted_host` means no web API tool covers `api.porkbun.com`, and
   `account_link_missing` means no account is linked to it. Do not repeat the
   call; follow Brokered HTTP Requests or Tool Setup.

Use `https://api.porkbun.com`, not the retired `https://porkbun.com` API host.
Availability responses report `response.avail` as `yes` or `no`. The same
response contains the registration `price`, `firstYearPromo`, `regularPrice`,
`premium`, `minDuration`, and `additional.renewal` / `additional.transfer`
pricing. Use those fields for a single-domain availability-and-pricing request;
do not fetch the entire pricing catalog.

If the user asks for TLD catalog pricing independent of one domain, call the
public `POST /api/json/v3/pricing/get` endpoint with
`json_body: {"tlds": ["xyz"]}`. It does not require credentials. Never omit
`tlds` unless the full catalog is actually needed, because the unfiltered
response is large enough to exceed normal tool-result budgets.

A successful catalog lookup shows that the credential is stored, not that it is
linked to a tool.

## `secure_cli` Provider And Action Contract

Every call has top-level `provider`, `action`, `params`, and a concise non-secret
`reason`. Provider arguments belong inside `params`; never put a user/owner id or
raw credential in any field.

- `github` actions: `gh.exec`, `auth.status`, `auth.logout`, `auth.login.credential`,
  `repo.view`, `issue.list`, `issue.view`, `issue.create`, `issue.comment`,
  `pr.list`, `pr.view`, `pr.create`, `pr.comment`, `workflow.run.list`,
  `project.item.list`, `api.get`, and `api.post`.
  - If an action reports `auth_required` (`user_action` `/link cli:github`),
    the person has not signed in: follow Tool Setup (TeamLead calls
    `set_up_tools`; a specialist escalates). The person
    may also sign in themselves with `/link cli:github` for browser/MFA
    sign-in, `/link cli:github token` to paste a token, or `/link cli:github web-all`
    only after explicit approval for high-privilege scope. Never run or
    recommend shell `gh auth login`.
  - Use `gh.exec` with `{"argv": [...]}` only for a needed non-auth command
    without a first-class action. `argv` starts after the `gh` executable and is
    never a shell string. Put options after the full command
    (`["issue", "list", "--repo", "o/r"]`) and pass each short flag and its value
    as separate arguments (`"-X", "GET"`, not `"-XGET"`). File flags such as
    `--body-file`/`-F` accept only `-` with the text in `params.stdin`, and field
    values must not start with `@`. The broker disables prompts and rejects
    auth/config, extension/alias, secret/key, interactive-browser, local-copy,
    and non-GET/POST API calls.
  - `project.item.list` takes `owner`, `number` or `project_number`, and optional
    `limit`; use it first for Projects v2 boards. For uncovered Projects v2
    queries use `api.post` with `path="graphql"`; do not retry REST project paths
    or shell `gh project`.
  - `api.get` accepts only `path`. `api.post` accepts `path` plus optional JSON
    `body` and always uses POST. Do not send `method`, `field`, or argv-style
    params. For PATCH-only operations use an appropriate GraphQL mutation or
    report that the broker surface lacks the operation.
  - Issue/PR view and comment actions use `repo`, `number`, and comment `body`;
    `issue_number`, `pr_number`, and `pull_request_number` are accepted aliases.
  - `auth.login.credential` fetches the provider secret inside the broker and
    never returns it.
- `google` actions: `gog.exec`, `gmail_search`, `gmail_read`, `gmail_send`,
  `calendar_list`, `calendar_create`, `drive_list`, `drive_export`,
  `docs_export`, and `sheets_read`. Use `gog.exec` with `{"argv": [...]}` for a
  policy-approved non-auth command without a first-class action. It runs argv
  directly. Put options after the full command (`["gmail", "messages",
  "search", "from:x", "--max", "5"]`; only `--account` and output-format flags
  may come first) and pass short flags separately. Local files (`drive upload`,
  `--attach`, `--body-file`, `--out`, `--out-dir`, `@file` JSON) must be in this
  task's folders: the person's folder (relative paths start there) or the
  shared `team/` and `agents/` folders. The broker copies them through a private
  folder, so links to other places are refused; outputs appear at the path you
  gave, listed in `local_files_written`. It refuses auth, credential, config,
  service-account, impersonation, verbose, self-update, local server, hook and
  whole-folder commands (`drive changes poll/serve`, `drive sync`,
  `appscript pull`, `update`, `mcp`, `backup`), and options such as `--home`,
  `--quota-project` or `--access-token`. Read-level commands run with gog's
  `--readonly`; `request blocked by --readonly` means the command needs write
  access.
  - Google and GitHub run only under the requesting person's own sign-in; the
    broker never uses another person's or an environment token. `auth_required`
    means that person has not signed in, and `google_oauth_client_missing` that
    the team's Google sign-in app is not set up (an administrator's step:
    `/auth google team_client`; the person may instead paste their own Desktop
    app client on the sign-in page). Follow Tool Setup: `set_up_tools`
    finds which it is and who can fix it. The person may also sign in themselves
    with `/link cli:google` (or `/link cli:google json_token` to import a gog
    token).
    `google_keyring_password_missing` is an operator setting
    (`GOG_KEYRING_PASSWORD`), not something a sign-in fixes.
- `bitwarden`, `onepassword`, `keepass`, and `vault` actions:
  `credential.search`, `credential.get_metadata`, and
  `credential.use_for_action`. Searches and reads return names, refs,
  usernames, URLs, and field labels only. Use `credential.use_for_action` or a
  target action such as `github.auth.login.credential`; never request a raw
  secret.
- `sealbox` actions: `credential.search`, `credential.get_metadata`, and
  `credential.use_for_action`. Search params are optional `query`, `key` or
  `name`, `username`, and `limit`. After search, metadata lookup accepts
  `item_id` or `item_name`/key. Search covers username/password credential
  records, not every catalog-backed Sealbox `secret` record.
- `secure_requests` actions: `request`, `get`, `post`, `put`, `patch`, `delete`,
  and `head`. Pass method/URL/headers/query params/JSON or body/timeout as
  appropriate. The host must belong to a web API tool (`http:*`); if that tool
  needs an account, the broker adds the linked credential. Never send a
  `credential_key` or `${...}` placeholder. `get`, `post`, and the other method
  actions always send that method; use `request` to choose one. Redirects are
  returned with `redirect_location`, not followed. The broker returns redacted
  response content.
- `credential_catalog` actions: `credential.collect`,
  `credential.get_metadata`, `credential.register_existing_source`,
  `template.list`, `template.get`, `template.render_request`,
  `template.save_approved_variant`, `credential.bind_for_openshell`,
  `credential.release_openshell_binding`, `credential.api_request`,
  `signup.prepare`, `signup.execute_approved`, and
  `signup.manual_fallback_session`.
  - Common collection templates include `generic_username_password`,
    `generic_signup_username_password`, `generic_api_token`,
    `generic_oauth_client_secret`, and `generic_service_account_file`.
    `credential.collect` is the post-form storage action; `signup.prepare` only
    validates. A user-facing form requires the terminal
    `secure_credential_collection_request` structured action.
  - New instances default to `write_source="sealbox"`: username/password fields
    become Sealbox credential records, custom secret fields become Sealbox
    secrets, and the catalog retains only source refs.
  - `credential.register_existing_source` maps a catalog `field_name` to a
    password-manager source `item_id` or `item_name` and `field_ref`. A spaced
    manager item name such as `team-nats-agents :: default :: home_assistant`
    is valid as `item_name`, but do not reuse it as the catalog key. `::` is
    valid inside a catalog key; ` :: ` with spaces is not.
  - `credential.bind_for_openshell` takes the `tool_id` whose linked account
    code should use and returns a `lease_id`; release it afterward with
    `credential.release_openshell_binding` and that `lease_id`. See OpenShell
    Credential Use.
  - `credential.api_request` follows the same web API tool, account link, and
    no-placeholder rules as `secure_requests`.

The broker validates the provider/action pair and returns the valid action list
on an invalid action. Change an invalid payload materially before retrying.

## GitHub

For credentialed GitHub work, use `secure_cli` with `provider="github"` instead
of shelling out to `gh`.

- Prefer first-class broker actions when they exist.
- When no first-class action exists, use `action="gh.exec"` with an `argv` list
  containing arguments after the `gh` executable; do not pass a shell string.
- The broker executes argv without a shell and blocks auth/config/secret/local
  file escape paths. Put options after the full command and pass each short
  flag and its value as separate arguments.
- For GitHub Projects v2 / kanban item listing, use
  `action="project.item.list"` with `owner`, `project_number` or `number`, and
  optional `limit`.
- Do not try REST `/projects` endpoints for Projects v2.
- Do not retry raw `gh project ...` from shell.
- Use `action="api.post"` with `path="graphql"` only for Projects operations not
  covered by a first-class broker action or safe `gh.exec`.

## Home Assistant

- Home Assistant username/password is for browser login automation, not direct
  REST API password grants.
- For Home Assistant API work, use `home_assistant_api_token` or another API
  token template with credential key `home_assistant_token`.
- Call `/api/` or other Home Assistant REST endpoints through
  `secure_requests.get` or `secure_requests.request` with no credential in the
  request. This needs a web API tool for the Home Assistant host that the
  operator defined to send the token as `Authorization: Bearer <token>`, and an
  account link from the stored token to that tool (see Tool Setup).
- For site login, use the `home_assistant_login` or
  `generic_username_password` template. Code in OpenShell receives login values
  only through `credential.bind_for_openshell` (see OpenShell Credential Use).


## X and Hugging Face

Use the `xurl` and `hf-cli` skills for their exact broker request formats and
limits.
`xurl` provides `auth.status` and `post.create`; `huggingface` provides
`auth.status`, `file.download`, `file.upload`, and `model.info`. They take no
credential key or other account selector. For X the broker uses the person's
own X sign-in (`/link cli:x`) first, then the account linked to `cli:x` for the
team, only while the team shares X (X starts private). For Hugging Face it uses
the account linked to `cli:huggingface`, the person's own link first, then the
team's while the team shares Hugging Face. Never hydrate their credentials into
the agent's own shell.
When X has neither, the call fails with `x_account_missing` (`user_action`
`/link cli:x`): follow Tool Setup. TeamLead offers the person's own X sign-in
(`kind: sign_in`), or, only while the team shares X and for an intentionally
shared team account, a team account
link for `cli:x` (`kind: account_link`, `owner: team`, administrators only; the
form takes the `oauth_json` file, or collect it first with the `xurl_oauth2`
template). A Hugging Face token can be typed straight into the account link
form for `cli:huggingface`.
Reconcile unknown write outcomes before another publish or upload.

## Credentials for scheduled work

Use `inspect_scheduled_worker` to identify the current job and its declared tool
needs. Treat prospective readiness as provisional when an account link or
broker validation depends on concrete runtime input. Send missing tool or
account needs to TeamLead through `manager_escalation`; TeamLead uses the
collection and Tool Setup steps above. Never store credential keys
or values with the schedule. Keep a schedule-specific tool approval separate
from the account link and verify the concrete broker operation after repair.
