# GitHub CLI

GitHub uses the broker's `gh` with each person's own sign-in. Agents use your
GitHub sign-in only for the work they do for you; an agent working for someone
else uses that person's sign-in, or none.

The broker keeps each person's sign-in in their own place: the token in the
credential store (Sealbox by default), and `gh`'s settings in a private folder
per person under `broker_credentials/users/`. It does not read GitHub tokens
from environment variables: startup stops if `GH_TOKEN`, `GITHUB_TOKEN`,
`GH_ENTERPRISE_TOKEN` or `GITHUB_ENTERPRISE_TOKEN` is still set, and names
`/link cli:github` as the replacement. A request that works for nobody in
particular cannot use GitHub. GitHub sign-ins are personal: there is no team
GitHub account, so `/link cli:github team` only says so. The GitHub API tool
(`http:github`, for agents' web requests to `api.github.com`) is personal too:
link your own token with `/link http:github`; `/link http:github team` replies
"GitHub API uses only each person's own account."

In Web Chat, the authentication notice has an **Open** button. Select it to
open the secure broker form in a new tab or window. The long authentication URL
is hidden behind that link; credentials entered in the form are not sent to
the agent. The link disappears when you dismiss it or refresh Web Chat. Run the
command again if you need a new link. The CLI prints a URL you can open instead.

## Browser Auth

This is the default: TeamLead's setup page and plain `/link cli:github` start
it (`/link cli:github web` does the same).

```text
/link cli:github
```

The broker starts `gh auth login --web` and shows the browser/MFA steps. This
flow requests `repo`, `read:org`, `gist`, and `read:project` so GitHub Projects
v2 board reads work after login. The broker stores the resulting sign-in as
yours: in the credential store, or with file-backed broker state in your own
`gh` folder.

Select **Open** on the broker page to visit GitHub. The one-time code changes
from **Wait...** to the code you need, and its copy button becomes available.
The page checks progress in place, keeping the controls stable while you
authorize. **Broker CLI Output** contains optional troubleshooting details.

For broad agent GitHub presence management:

```text
/link cli:github web-all
```

Use `web-all` only when you intentionally want the broker to request its full
high-privilege GitHub OAuth scope profile.

## Token Import

If you prefer to paste a personal access token, use:

```text
/link cli:github token
```

Paste a token into the broker form. The broker checks it with GitHub and stores
it as yours. The token should include scopes matching the work you want the
agent to perform. For Projects v2 reads, include `read:project`.

## Supported Broker Actions

- `auth.status`
- `auth.logout`
- `auth.login.credential`
- `repo.view`
- `issue.list`
- `issue.view`

Additional actions may be added to the broker as explicit allowlisted actions.

## Verification

1. Run `/link cli:github`.
2. Complete GitHub browser/MFA auth.
3. Run `/auth status`; it asks GitHub whether your sign-in works.
4. Ask the agent to view a repository or list issues.
5. For Projects v2 access, verify the token includes `read:project`.

There is no shared broker `gh` sign-in to check by hand: each person's
sign-in is separate, and `/auth status` checks your own.

## Commands through `gh.exec`

The broker checks every `gh.exec` command before it runs. Credential,
configuration, secret, key, extension and local-copy commands are refused
(`auth`, `config`, `secret`, `ssh-key`, `extension`, `repo clone`,
`pr checkout`, `release create/upload/download`). Options go after the full
command, and each short option and its value are separate arguments
(`-X GET`, not `-XGET`). File options (`--body-file`/`-F`, `--notes-file`,
`--input`) accept only `-` with the text sent as standard input, and field
values must not start with `@`, because both would read files on the broker
host. `gh api` accepts only GET and POST for now.

## Logout

The agent can request `github.auth.logout` through the broker. This removes the
broker-owned local `gh` auth entry; it does not revoke the token server-side.
