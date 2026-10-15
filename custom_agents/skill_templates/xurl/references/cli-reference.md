# X CLI reference

Reviewed against xdevplatform/xurl v1.3.1, MIT licensed:
https://github.com/xdevplatform/xurl/tree/v1.3.1

Use `xurl --version` and `xurl --help` for diagnostics. The upstream forms
`xurl post "text"` and `xurl -X POST /2/tweets -d '{"text":"text"}'` demonstrate
syntax; Universal Agents executes authenticated posting through `secure_cli`.

Setup requires an X developer app with OAuth 2.0 user authorization, and API
credits. A team administrator stores the team's X app once with
`/auth x team_client`: its OAuth 2.0 client ID, the client secret only for a
"Web App" (a "Native App" has none), and the callback address registered in the
X developer portal. The suggested callback is the broker's own
`<broker public address>/auth/x/callback`, so sign-ins finish by themselves;
`http://localhost:8080/callback` also works, with the person pasting the
address into the sign-in page. The broker asks for `tweet.read tweet.write
users.read offline.access`. Each person then signs in with `/link cli:x`; the
broker keeps that sign-in under the person and saves its refreshes.

The team's own X account, used for people who have not signed in, is an
account link that only an administrator creates (`/link cli:x team`). It is
used only while the team shares X: X starts private, an administrator shares
it with `/accounts share cli:x`, and `/link cli:x team` is refused until then.
A team account linked earlier is kept, unused, while X is private. To
produce its credential, complete `xurl auth oauth2` or its `--headless` flow on
a trusted operator computer. Keep the client secret out of command history and
agent context. The operator can export one account from the resulting local
auth store using the UA checkout:

```bash
.venv/bin/python -m scripts.export_xurl_credential --app APP --username HANDLE --output /private/location/x-oauth.json
```

Paste that file's content into the team account link form for `cli:x` (or
store it first through the `xurl_oauth2` secure credential form and pick it
there), then remove the exported copy. Do not share a rotating refresh token between independently
running xurl installations. Reauthorize when deliberately changing custody.
The broker needs its credential directory persisted and its configured
credential-state backend reachable. Sealbox is the default.

Regenerate/review this reference and the executable pins together on upgrades.
