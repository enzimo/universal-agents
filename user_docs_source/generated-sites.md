# Generated Sites

Generated sites are browser pages the agent can create when information is
easier to use visually than in a chat thread. Every generated site has embedded
chat and uses the same saved visual theme as the rest of the website.

## Site Types

The agent chooses the content shape before publishing:

- **Article** is the default for explanations, reports, reference pages,
  collections, and other content whose sections can be read independently.
  Useful optional elements include content sections, key points, highlight
  cards, callouts, and references.
- **Comparison** is for evaluating alternatives such as products, vehicles,
  services, destinations, or plans. Useful optional elements include option
  cards, criteria tables, pros and cons, best-fit notes, recommendations, and
  sources.
- **How-to** is for explicitly requested instructions or genuinely ordered
  user actions where sequence affects success. Useful optional elements include
  prerequisites, materials, tools, safety notes, ordered steps, verification
  checks, and browser-local progress checkboxes.
- **App** is for interactive pages where browser-local state or progress is
  part of the value, such as workout trackers, habit boards, practice logs, or
  goal dashboards. Useful optional elements include stat cards, counters,
  progress trackers, task or habit checklists, action links, and supporting
  content sections.

These elements are building blocks, not required template sections. Generated
sites omit empty elements and should not add filler merely to populate a
layout.

Interactive state is stored in the browser for that page. It is useful for
personal progress tracking, but it is not a durable source of record for
team-wide status unless the agent also records the state somewhere else.

## When to Ask for One

Ask for a generated site when a visual artifact would be easier to scan, use,
or revisit than a chat response:

- A side-by-side vehicle, product, or service comparison.
- A research brief or reference page.
- A setup, repair, troubleshooting, or migration walkthrough.
- An interactive workout, habit, study, maintenance, or goal tracker.

Examples:

```text
Create a comparison site for these two cars with tradeoffs and a recommendation.
Create a walkthrough site for configuring GitHub authentication and verifying it.
Create an app site to track my weekly workouts against these goals.
```

## Opening Sites

When the agent publishes a site, it returns a browser URL. The generated
site index is available at:

```text
/docs/sites/
```

The index lists only the pages made for you, and only when you are signed in to
Web Chat in the same browser; otherwise it shows a sign-in link.

Anyone who has a page's link can open that page, so you can open it from a chat
message on any device without signing in. Page links end with a random part so
they cannot be guessed, but treat a link like the page itself: share it only
with people who may read the page.

The default website host is configured under `interactive_apps.plugins.builtin.internal_website.settings`.
The browser-facing URL derives from `network.public_origin` plus
`interactive_apps.plugins.builtin.internal_website.settings.public_port`, commonly:

```text
http://127.0.0.1:18090
```

If the URL does not open, confirm the gateway website app is running and that
the configured host and port are reachable from your browser.

The generated-site index shows each page's human-readable title. The underlying
link still contains the complete site id so conversation provenance, bookmarks,
embedded chat, and regeneration-in-place continue to use the canonical URL.

## Where Pages Are Kept

A page is written into `sites/<site_id>/` of the folder of the person it was
made for (`users/<person>/sites/`, or `agents/sites/` when maintenance work
made it for no one), next to that person's other files. The website keeps its
own record of each page in `state/published_sites/`, with a fingerprint
(SHA-256) of every file, and serves a page only while its files still match
that record. If a page file is edited, replaced, or deleted afterwards, the
page stops opening ("not found") instead of showing something the publishing
tool did not write. Ask the agent to publish the page again to change it.

When you ask the agent to add content to an existing generated site, it can
regenerate that same page in place by reusing the prior `site_id` or `site_url`.
The browser URL stays the same; refresh the page to see the latest generated
content. Only a page made for you can be regenerated for you.

## Embedded Chat

The embedded chat panel sends follow-up questions back through the gateway,
as you. It works only for the person the page was made for, signed in to Web
Chat in the same browser; anyone else who opens the page sees a sign-in link
instead, so a shared link never lets someone talk to the agents in your name.
When the generated page includes the original session and conversation
metadata, website chat can continue the same task context. For pages generated
from Telegram, Discord, WhatsApp, or another channel, the gateway may also
mirror the web follow-up and reply back to the original channel when that
channel is still attached.

Use embedded chat for page questions and progress updates. Use the broker forms
that `/link` and `/auth` open for secrets.

Use the pop-out button in the chat header when you need more room. It docks the
chat across the bottom of the page so longer replies and draft follow-ups are
easier to read.

## Sensitive Values

Generated sites are not the place to paste tokens, passwords,
session keys, or private credential files. For sensitive setup, use the secure
broker flow:

```text
/auth status
/link cli:github
/link cli:google
/auth bitwarden
```

Broker auth forms submit credential material directly to the secure broker.
The agent sees provider status and safe metadata, not the raw secret.

## Troubleshooting

- No generated site URL: ask the agent to publish or regenerate the site.
- `/docs/sites/` is empty or asks you to sign in: sign in to Web Chat in the
  same browser. The index lists only pages made for you; each page is in
  `sites/` of your folder, with its record in `state/published_sites` under
  the instance root.
- A page that used to open is "not found": its file was changed or removed
  after it was published. Ask the agent to publish it again.
- Site opens but chat asks you to sign in: sign in to Web Chat as the person
  the page was made for, then send again.
- Site opens but chat fails otherwise: confirm the internal website app is
  running with the gateway, because generated-site chat is routed through that
  shared host.
- URL points at the wrong host: update `network.public_origin` and
  `interactive_apps.plugins.builtin.internal_website.settings.public_port`, then regenerate the site.
