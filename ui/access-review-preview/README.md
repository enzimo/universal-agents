# Visual access review prototype

Interactive design for a single workflow review, with a dependency map,
capability checkboxes, account selection, duration, future-use choices, and
clickable information dialogs. The graph is generated from a fixed example of
a trusted system projection. The proposed agent input is shown separately.

This is not connected to production. Account names are synthetic. Approve,
Reject, and Resume only change in-memory preview state. Reloading resets it.
No account credentials, authorization service, package install, or build is needed.

From the repository root:

```bash
rtk proxy .venv/bin/python -m http.server 18120 --bind 0.0.0.0 --directory ui/access-review-preview
```

Open `http://localhost:18120`. Try deselecting posting, choosing a personal
account, switching recipient/duration, opening an info dialog, and approving or
rejecting. The example selector also shows missing account selection and setup
interrupted after approval. The latter keeps the completed connection and
resumes remaining setup in the same review.

**Open full workflow** leads to `workflow.html`. Start that flow to watch its
simulated steps. The default example approves setup and an exact ready post in
one dialog. Switch Draft to Prepared later to see the workflow pause for the
new content. Close a review to ask questions and reopen it without deciding.
The Test selector demonstrates a recoverable setup failure and an uncertain
publication result. Download logs produces a local JSON file of synthetic
diagnostic events, without draft or conversation content. Nothing is published.

The preview uses the colors, typography, frosted surfaces, and static polygon
background described in `../DESIGN.md`. Its simplified static mesh and local
theme toggle are prototype-only. Production must use the shared appearance
assets, authenticated broker selection, validated server projections, and
durable execution state. No new runtime capability is implemented here.

The implementation design is owned by the sibling architecture repository at
`plans/unified_access_workflow.md`; account links, which replaced the earlier
persistent credential bindings, are described in `plans/unified_access_model.md`.
Persistent access and optional standing
posting permission must be supported by every-use guards before production UI
offers them. All-agent restart is shown because the current X registry entry
declares `restart_scope: all`.

The initial review was checked in Chromium at 390, 768, and 1280 pixels in both
themes, including all info dialogs, focus/Escape, partial selection, account
scope, reject, approve, resume, and reset during simulated execution. Runtime
skills and canonical X guidance were reviewed; they remain unchanged because
these new runtime contracts are proposed rather than implemented. The preview
adds no application dependencies and requires no lockfile changes.

The full workflow was also checked at those widths and in both themes. Checks
covered one-decision setup/publication, later content review, omitted actions
without repeat prompts, conversation that cannot approve, setup retry, uncertain
publication and reconciliation, cancellation, and downloaded diagnostic JSON
that excludes the draft and conversation. No live provider or authorization
integration is claimed by these browser checks.
