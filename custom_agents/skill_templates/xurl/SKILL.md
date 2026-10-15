---
name: xurl
description: Publishes text posts and checks account identity on X using the xurl CLI through Universal Agents' credential broker. Use for X posting requests, not web search or other social networks.
---

# X posting

Use `secure-credential-workflows` for credential collection and account links,
and `shell-execution-workflows` for ordinary command execution.

X is the catalog tool `cli:x`: brokered `auth.status` (read) and `post.create`
(write). An agent uses it once it holds access: checking the account needs
`cli:x` at `read`, posting needs `write`. If `secure_cli` or `cli:x` is not in
your tools, call `find_tools` and then `request_access`; a tool you are given
appears on your next model call, with no restart. Giving access needs no
credential step. The broker posts with the person's own X sign-in first. It
uses the team's X account linked to the `cli:x` tool, for people who have not
signed in, only while the team shares X. X is private until an administrator
or owner shares it (`/accounts share cli:x`); while it is private, each
person's work needs their own sign-in, work that serves no person cannot use
X, and `/link cli:x team` is refused. `/accounts` shows whether the team shares
X. Each person signs in with their own X account by running `/link cli:x`
themselves, which opens the broker's X sign-in page; once X is shared, only a
team administrator or owner links the team's account (`/link cli:x team`).
People never link a personal X account. X credentials never come from
environment variables. Apart from the sign-in command, do not ask the user to
type slash commands. If the catalog lacks `cli:x`, report the runtime version
gap; do not invent a registration request.

For a setup-and-post goal (TeamLead loads access workflows first with
`use_tools(tool_ids=['fn:access_workflows'])`), publish a complete `submit_access_workflow` plan before
asking for anything. Include the X account (the person's own sign-in, or, only while the team
shares X, an `account` step for the team's account link), account verification (`tool_access` `cli:x` at `read`), the exact post review and
publication (`cli:x` at `write`), and a check of its returned URL. Follow the workflow section of `runtime-operations-workflows`. Ask
for independent staged changes together with `request_access_workflow_approvals`,
then apply each approved one with `apply_change`; keep dependent or
later-produced content on that same page. Do not recreate completed setup or ask
the user to type commands that give access.

1. Identify the intended X account and the exact post content from the task.
   Publish only within the user's requested scope.
2. Check broker authentication before asking for new credentials. Once you
   hold `cli:x` at `read`, call `secure_cli` with provider `xurl`, action
   `auth.status`, and `params={}`. Do not pass `credential_key` or any other
   account selector; the broker refuses them and picks the account itself
   (the person's sign-in, else the team's link while the team shares X).
   Verify the returned username matches the intended account.
3. If the broker reports `x_account_missing`, the person has not signed in to
   X and no team X account is in use (X is private, or shared without a linked
   team account). TeamLead calls `set_up_tools` for
   `cli:x` without asking first: it opens the sign-in (after the team's X app
   when that is missing and the person is an administrator; otherwise it says
   that an administrator must run `/auth x team_client`), and the task resumes
   when the person finished; then check `auth.status` again. The person may also run `/link cli:x`
   themselves. Only when the team shares X and its X account is intended does
   TeamLead send a team account link for `cli:x` (kind `account_link`,
   `owner: "team"`, administrators only; a specialist escalates to TeamLead).
   While X is private, never request the team's account: say that the team
   keeps X private, so the person's own sign-in is needed, and that an
   administrator decides whether to share X (`/accounts share cli:x`).
   If the team has no stored X credential yet, TeamLead first renders the
   `xurl_oauth2` credential template using
   `credential_catalog.template.render_request`, sends the structured
   `secure_credential_collection_request` through the gateway, and links the
   stored credential afterwards. Never request a personal account link for
   `cli:x`. Never request OAuth values, read token files, run `xurl token`, or
   import tokens through chat or an agent shell.
4. Publish with this exact broker request:

   ```json
   {"provider":"xurl","action":"post.create","params":{"text":"The approved post text."},"reason":"Publish the requested update to the linked X account."}
   ```

5. Report the returned post URL and ID as completion evidence. A draft or a
   successful authentication check is not publication.

When a broker result has `failure_stage=credential_resolution`, read its
`error_code`, `required_field`, `action_owner`, and `next_action`. If
`provider_operation_attempted=false`, explain that the tool operation was not
attempted; do not claim the external service rejected authentication. A missing
field means the linked credential is incompatible (X needs secret
`oauth_json`; Hugging Face needs secret `api_token`). Ask TeamLead to have the
owner store a matching credential through the secure form and link it to the
tool. Source configuration, read failures, or empty values go to TeamLead for
diagnosis and operator help when needed. Correct only issues within existing
authority; these diagnostics grant no permission. Preserve completed setup,
never guess credential keys or repeat account links, access requests, or restart
indiscriminately, and do not retry the unchanged request when
`retry_safe=false`. Never request or transmit secrets through chat.

On `outcome_unknown`, reconcile the account's posts before another publish.
Do not automatically retry; a timeout can follow a successful remote post.
On `account_busy`, wait until the current account operation completes before
checking status again. On auth failure, the account in use needs repair: a
person renews their own sign-in with `/link cli:x`, and a team administrator
repairs the team's account by storing it again through the secure form under
the same key. When the
call is refused (`access_denied`), call `request_access` with the `tool_id`,
`operation`, and `level` it names and a plain reason; retry only when the
result says `granted`.

If `auth.status` returns a different username than intended, the wrong account
is in use. The person signs in again with `/link cli:x` using the intended
account, or, when the team shares X and its account is intended and wrong,
TeamLead requests a new team account link for `cli:x`, which replaces the old
one. Do not guess
another credential.

Keep credentials in Sealbox or the configured broker store. The broker hydrates
xurl in a private temporary home, saves refreshed OAuth state (a person's
sign-in under that person, the team's account under the team), and removes
temporary material after use. Installing this skill does not grant posting rights.
Use the broker for authenticated work even though `xurl` is on PATH.

Read [the command reference](references/cli-reference.md) for version and operator
setup details. Media posts, replies, threads, and deletion have no broker action
in this version; do not substitute an arbitrary credentialed shell command.

For a complete communications workflow, include research, draft preparation,
the X account step, publication review and result verification in one
access plan. Leave X posting access for a person to decide; TeamLead does not
hand it on to workers as routine access. Research access the worker already
holds may be reused; neither a sign-in nor an account link approves a future
post. Do not regenerate completed setup solely because publication needs a
later review.
