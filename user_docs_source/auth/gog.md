# Google gog

Google Workspace access uses `gog` through the secure broker. Each person signs
in with their own Google account, and agents use that sign-in only for the work
they do for that person.

## How it works for each person

- **Your own sign-in.** `/link cli:google` stores your Google sign-in in the
  credential store (Sealbox by default) as yours. An agent working for you uses
  it; an agent working for someone else never can. There is no team Google
  account: `/link cli:google team` replies that sign-ins are personal and that
  administrators set the team's Google sign-in app with
  `/auth google team_client`.
- **A private gog folder for every run.** Each `gog` command runs in a new,
  empty folder inside your private broker credentials folder, with only your
  sign-in in it. The folder is deleted when the command ends. `gog`'s keyring
  there is protected by `GOG_KEYRING_PASSWORD`, which the operator sets for the
  broker.
- **No sign-in, no Google.** If you have not signed in, an agent's Google
  request is refused with "Sign in with /link cli:google"; nothing runs.
- **The Google sign-in app.** Google sign-ins go through an OAuth client (a
  Google Cloud "Desktop app"). A team administrator stores the team's app once
  with `/auth google team_client`. Anyone may use their own app instead by
  pasting its Desktop app OAuth client JSON on the `/link cli:google` sign-in
  page. The app only identifies the program you sign in through; it gives
  nobody access to anyone's Google data.
- **No environment variables.** The broker does not read Google tokens or
  OAuth clients from environment variables. Startup stops if an old one
  (`GOG_CLIENT_SECRET`, `GOG_REFRESH_TOKEN`, `GOOGLE_OAUTH_ACCESS_TOKEN`,
  `GOOGLE_OAUTH_REFRESH_TOKEN`, `GOG_TOKEN_FILE`, `GOG_SESSION_FILE`,
  `GOOGLE_SESSION_FILE`, `GOG_TOKEN_IMPORT_FILE`, `GOG_CLIENT_ID`,
  `GOG_OAUTH_CLIENT_CREDENTIALS_FILE`) is still set, and names the command that
  replaces it. The same goes for a Google service-account key file
  (`GOOGLE_APPLICATION_CREDENTIALS`, or its `_FILE` form): it would be one
  Google identity for the whole team, so startup refuses it and each person
  signs in with `/link cli:google` instead.

## The team's Google sign-in app (administrators)

```text
/auth google team_client
```

Only a team administrator or owner can open this form; the broker checks the
role the gateway reports for you. Paste the Desktop app OAuth client JSON (see
[Get the Desktop App OAuth Client JSON](#get-the-desktop-app-oauth-client-json)).
The broker checks that it is a Desktop app client and keeps it in the team's
part of the credential store. From then on, people who have no app of their
own sign in through it. Replacing the team's app means sign-ins made through the
old app must be made again.

## Browser Flow

Use:

```text
/link cli:google
```

This is the main way to sign in to Google.

The authorization page keeps an inactive **Wait...** button until the Google
URL is available, then enables **Open** to launch Google in a new tab. Status
checks update the existing page without replacing the button or redirect form.
Redirect troubleshooting and raw CLI output are collapsed by default.

### Publish the OAuth App Before Authorizing

For an External OAuth app, Google expires refresh tokens after 7 days while
the consent screen publishing status is **Testing**. Before starting the broker
flow:

1. Open Google Cloud Console and select the project that owns the OAuth client.
2. Open **APIs & Services > OAuth consent screen**.
3. Under **Publishing status**, click **Publish App** and confirm.
4. Confirm the status is **In Production**.

Personal-use apps can remain unverified, although Google may show an
unverified-app warning during login. In Production removes the fixed 7-day
Testing lifetime; it does not make a refresh token permanent. Password changes,
manual revocation, prolonged inactivity, and other account security events can
still invalidate it.

The broker page starts `gog auth add --manual` in a private folder for your
sign-in. It uses your own Google sign-in app if you stored one, otherwise the
team's. To use your own app, paste its Desktop app OAuth client JSON into the
optional field; the broker keeps it as yours. If neither exists, the page asks
you to paste one or to ask a team administrator to run
`/auth google team_client`. The JSON is an OAuth client ID for a Desktop app,
not a service account key.

When Google accepts the sign-in, the broker stores it as yours in the credential
store and deletes the private folder.

### Get the Desktop App OAuth Client JSON

1. Open [Google Cloud Console > APIs & Services > Credentials](https://console.cloud.google.com/apis/credentials).
2. Select or create the Google Cloud project that should own the OAuth client.
3. Configure the OAuth consent screen if Google prompts for it, then publish the app so its status is **In Production** before authorizing.
4. Click **Create Credentials**.
5. Choose **OAuth client ID**.
6. Set **Application type** to **Desktop app**.
7. Name the client and click **Create**.
8. Click **Download JSON**.
9. Paste the full JSON only into the broker page, not chat.
10. Enable the APIs needed by selected services, such as Gmail API, Google Calendar API, Google Drive API, Google Docs API, and Google Sheets API.

Do not paste a **Web application** OAuth client JSON into this flow. The current
`gog` manual flow chooses a dynamic localhost or 127.0.0.1 loopback redirect
URI for each authorization, so the broker requires a Desktop app client. A Web
application client produces Google's `Error 400: redirect_uri_mismatch`.

After Google consent, the browser may land on a localhost URL that does not
load. Copy the full localhost URL from the address bar and paste it back into
the broker page. This manual handoff completes the same OAuth code exchange as
an automatic desktop callback; it does not shorten the refresh token lifetime.
The broker invokes `gog` with `--force-consent` so Google can issue a fresh
refresh token.

## Token JSON Import

Use:

```text
/link cli:google json_token
```

On a trusted machine with `gog` installed:

```bash
gog auth add you@example.com --services=all --force-consent
gog auth tokens export you@example.com --out gog-token.json --overwrite
```

Publish the OAuth consent screen before running `gog auth add`. Importing a
token does not change its lifetime: a token minted while an External app was in
Testing still expires after 7 days. Re-authorize after publishing to obtain a
new long-lived token.

Paste the exported JSON into the broker form. A token works only with the
OAuth client that issued it. If you did not sign in through the team's app,
paste that app's OAuth client ID JSON into the same form; the broker keeps it as
your own app. The broker imports the token in a private folder, asks Google
whether it works (`gog auth list --check`), and only then stores it as yours.

## Your own Google sign-in app

To use your own Desktop app OAuth client instead of the team's, paste its JSON
into the optional field on the `/link cli:google` sign-in page (or the
`/link cli:google json_token` import page). The broker keeps it as yours: your
sign-ins then go through it; nobody else's do. A sign-in made through a
different app must be made again with `/link cli:google`.

## Checking your sign-in

`/auth status` asks Google whether your stored sign-in still works (the same
check as `gog auth list --check`). It reports Google as authenticated only when
Google accepted it just then; a stored sign-in that Google refuses is reported
as needing `/link cli:google` again.

## When browser authentication fails

The failure page explains recognized CLI errors and suggests a next step.
For example, a rejected authorization code calls for a fresh `/link cli:google`
sign-in, a rejected Google sign-in app calls for a team administrator to check
`/auth google team_client` (or for your own app), and a keyring-password error
requires an operator to set `GOG_KEYRING_PASSWORD` for the broker. An exit code alone does not identify the cause; unknown failures say so.

Open **Technical Details** for the diagnostic reference, failed stage, reason
code, exit code, and elapsed time. Give the reference to your operator, who can
find the matching `event=broker_auth_process_failed` entry in secure-broker logs.
The log records metadata, not CLI output, OAuth URLs, or submitted codes.
The **Broker CLI Output** section remains available on the secure page. Do not
paste that whole section into chat, since it can contain authentication links.

### Renamed Google accounts

If `gog` reports `authorized as old@example.com, expected new@example.com`,
Google returned an email different from the one entered in the broker form.
Google can keep returning the previous address to an existing linked app after
you rename the same Google account. See
[Google's renamed-account troubleshooting](https://support.google.com/accounts/answer/16521710?hl=en).

If the reported address belongs to your renamed account, start a fresh
`/link cli:google` sign-in and enter that address in the email field. Select the same
Google account during consent. Use that authenticated address for explicit
`gog` account selection too. This does not rename your Google account back.
If the reported address belongs to a different account, restart and select
the intended account instead. The broker does not treat arbitrary addresses
as aliases or bypass `gog`'s account check.

To try using the new address with this OAuth app, follow Google's guidance to
remove that app's existing connection and authorize again with the new address.
Removing the connection interrupts its existing access. Reusing the old
redirect URL will not fix the mismatch; complete a new consent flow.

## Expired or Revoked Tokens

If Google returns `invalid_grant` or says the refresh token expired or was
revoked, run `/link cli:google` to obtain a fresh token. When the failure occurs
roughly 7 days after authorization, check whether the OAuth app is still in
Testing; publish it to In Production before re-authorizing. Other revocation
causes remain possible, so a seven-day failure is a strong diagnostic signal,
not the only explanation.

If Google reports `unauthorized_client`, your stored sign-in was made through
a different Google sign-in app than the one set up now; sign in again with
`/link cli:google`.

## Supported Broker Actions

- `gmail_search`
- `gmail_read`
- `gmail_send`
- `calendar_list`
- `calendar_create`
- `drive_list`
- `drive_export`
- `docs_export`
- `sheets_read`
- `gog.exec` for controlled non-auth `gog` argv passthrough, such as Google
  Keep commands.

The broker checks every `gog` command before it runs:

- Credential, configuration, self-update, local-server, local-hook and
  whole-folder commands are refused (`auth`, `config`, `login`, `mcp`,
  `backup`, `update`, `drive changes poll/serve`, `drive sync`,
  `appscript pull`, `docs comments poll`), and so are options such as `--home`,
  `--access-token`, `--service-account`, `--quota-project` and `--verbose`,
  wherever they appear.
- Options go after the full command (`gmail messages search QUERY --max 5`);
  only `--account` and output-format options may come first.
- Local files (`drive upload`, `--attach`, `--body-file`, `--out`,
  `--out-dir`, `@file` JSON) must be in the requesting person's folder or the
  shared `team/` and `agents/` folders. The broker copies them through a
  private folder, so links pointing elsewhere are refused and `gog` never opens
  an agent-chosen path. Commands run from an empty private folder, so files
  written without an explicit output path are discarded.
- The broker also passes the refused commands to `gog --disable-commands`,
  the one command it checked to `gog --enable-commands-exact`, and, for
  read-level commands, `--readonly`, which makes `gog` block any request that
  changes data. `gog` itself therefore refuses anything else as a second
  check. (`maps places search` reads through a request `--readonly` does not
  recognise, so it runs without it.) The container image ships gog 0.41.

## Verification

1. A team administrator runs `/auth google team_client` once (or you paste your
   own app's JSON on the sign-in page in the next step).
2. Run `/link cli:google` (preferred) or `/link cli:google json_token`, and
   complete the broker page.
3. Run `/auth status`; Google shows as authenticated with your account.
4. Ask the agent to list calendars or search Gmail with a narrow read-only query.
5. If Google returns `accessNotConfigured`, enable the named API in the OAuth project and retry after propagation.
6. For sends, shares, deletes, or broad exports, confirm the intended action before proceeding.
