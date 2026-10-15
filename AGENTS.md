# Universal Agents

This file is the top-level operating contract for agents working in this repo.
Keep it short, actionable, and stable. Project detail belongs in the project
reference docs such as `README.md`, `QUICKSTART.md`, and
`README_ARCHITECTURE.md`.

## 1. Change Process

Let me know when backward compatibility is impacted but do not preserve backward compatibility. Remove obsolete paths instead of adding compatibility layers, fallbacks or migrations.

### Communicate clearly with the user

- Apply plain language to all user communication: explanations, plans, progress updates, questions, error messages, UI labels, documentation, and completion reports.
- Before you start, say in a line what you're about to do; brief updates while you work help the user follow along. Close with a short recap that stands on its own — what you found, what you did, and what's next — so a reader who only sees the last message has the full picture.
- Lead with the goal or outcome. Say who is doing what, why it matters, and what the user needs to do, if anything. Describe concrete behavior instead of naming an abstract concept and expecting the user to decode it.
- Avoid jargon and engineering metaphors such as "surface", "landed", "envelope", "plumbing", and "wired up". Name the actual thing or action: "available tools", "settings page", "connected the scheduler to the worker", or "delegation boundaries". Choose words that explain the meaning in context; do not merely substitute another obscure term.
- Remove all mannered prose.
- Be precise about progress. Say "implemented", "tested", "committed", "merged", or "deployed" only when that specific step happened. "The fix landed" does not tell the user whether their running instance has changed.
- Use technical terms when they help the user understand or act. Explain unfamiliar terms on first use with a short definition or concrete example. Include exact code or configuration names when needed to locate something, alongside a plain-language explanation.
- Explain blockers as a specific missing condition and a next step. For example: "The lead has no approved rules for granting workers tool access. It can prepare those rules for your review." Do not make the user translate an internal error or design the technical solution themselves.
- Keep sentences direct and focused. Break complicated behavior into understandable steps, preserve important distinctions, and omit implementation details that do not help the user assess the result or make a decision.

### Questions + Research, Impact, Design + Structure Outline, Implement

Follow the general **Questions + Research, Impact, Design + Structure Outline, Implement** pattern when asked to create any new features or code for the user. Individual steps may vary in complexity and may require multiple iterations (loops) depending on the nature of the request.

Before making a change to an existing implementation, ask yourself why the current implementation exists the way it does. After you answer that question, go back and ask it again to see if you have missed anything. Trace the full extent of existing logic and consult any architecture documents so that you deeply understand the rationale and the impact of the changes you are about to make. In situations where the existing logic is complicated or touches many parts of the system, break down your implementation into multiple phases so that the impact of change in each phase is manageable, tracktable and well understood.

For trivial, low-risk, tightly scoped changes such as small doc fixes, obvious typo corrections, or similarly simple edits, agents may compress this process and proceed without waiting for explicit user acknowledgment between each stage. For all other changes, keep the full pattern and user verification steps intact.

After initial request, examine the repo and **make sure you understand the user's intent**. You need to show the user in your thought process that you have grasped the scope and intent of the request and thought through the design. Get back to the user with any additional questions that need to be asked (but don't pester the user if the task is straightforward). Then perform any additional research within the repo, as well as, online, until you have a good understanding of the impacts of the changes and clearly state them to the user. Present the user with a detailed **design** and a **structured outline** of the implementation and re-evaluate against prior stated impact to make sure the design has not changed the stated impact. Iterate with the user on any points of clarification or redesign. Make sure your understanding of the user's intent (and not just the mechanical nature of the change) is **crystal clear** before proceeding.

Determine whether confidence for this project should lean primarily on unit tests, integration tests, live tests, or a deliberate mix of them.

When the project is still being established, proactively recommend an appropriate testing emphasis based on the system's shape and failure modes. Favor stronger integration or live coverage when important behavior depends on external services, persistence, routing, async workflows, deployment wiring, or other runtime interactions that unit tests may not reveal well.

For each change, define a verification plan that matches that strategy. Generate the most relevant unit tests and any integration / live tests that thoroughly verify the new functionality where practical. Finally, implement the changes exactly as designed. Run the relevant tests at the end, to make sure the implementation is successful and behaves as intended, and document important coverage gaps when full automation is not yet feasible.

### Plan before acting
- For any non-trivial task, form a short execution plan and a verification plan before editing.
- Re-plan when new evidence invalidates the current approach.
- Prefer simple, durable solutions over clever patches.

## Intent is king
- Make sure you execute to intent rather than to your written plan. If you find during implementation of the plan that it is drifting from the intent, stop and adjust your approach. Executing to intent is **paramount**. We must make sure to verify that the intent is clear and whenever there are deviations in code or plan, to communicate with the user to clarify intent and to change the plan + code to execute to intent.

### Fix root causes
- Treat recovered anomalies as defects to investigate, not acceptable outcomes.
- Do not stop at fallback logic if the underlying cause is still unknown.
- If a fallback must remain, keep it explicitly secondary, emit a warning with useful identifiers, and add tests that prove both the primary path and the warning path.
- If testing reveals unanticipated or cascading effects, do not just patch the first visible symptom and continue silently.
- Instead, document what changed, what failed, what secondary effects appeared, and what you believe the causal chain is, then present that report to the user for verification or correction before continuing with broader corrective changes.

### Verify before calling work done
- Do not mark work complete without evidence.
- Run the most relevant tests, checks, or realistic manual validation you can for the change.
- Run linting, compiler, static analysis or any other available tools and examine their output. Fix obvious bugs. When in doubt, ask the user for input on how to resolve issues before fixing.
- Compare expected versus actual behavior and resolve mismatches before finishing.
- Surface to the user any tests that are trivial or can be consolidated into a simpler set without losing much coverage. E.g. if you can remove 20% of tests but only lose 2% coverage, it's likely a good tradeoff.

### Keep documentation synchronized
- When behavior, architecture, configuration, or operator workflows change, update the relevant docs in the same change.
- After making code changes, always review whether the change affects any skill instructions or bundled skill resources. Update the applicable workspace skills, canonical shared skills such as `../ua-skills`, and tracked runtime skill templates before calling the work complete; if no skill adjustment is needed, make that an explicit completion-check decision rather than skipping the review.
- Remove stale instructions, dead compatibility notes, and obsolete knobs instead of letting them accumulate.
- Always document rationale for current architectural and implementation approaches when they are being written. Include enough detail so that future review of the architecture and code can map existing structure to rationale.

### Write code that is easy to operate
- Prefer clear, maintainable implementations over clever or highly compressed code.
- Actively identify opportunities for abstraction that replaces hard-wired implementation with flexible, modular and common components. Point out opportunities to consolidate code and generate sensible abstractions to the user as you find them, even if they are not directly related to current effort.
- Avoid hardcoding strings when the same string is being reused in code (e.g. in `equals` logic etc). Prefer a common string library that defines the string once and then reuses it across other code blocks.
- Follow existing project patterns unless there is a concrete reason to change them.
- Keep modules and functions focused; split work when responsibilities start to blur.
- Keep cognitive complexity low using the project guidance in `CONTRIBUTING.md`;
  treat the Complexipy changed-function report as an advisory review signal,
  and explain cases where protocol or state-machine complexity is intentional.
- Add comments to help a future reader understand intent, invariants, or function / intended behavior.
- Make failure paths observable with structured logging that includes enough context to diagnose production issues.
- Consider the larger scope when making changes and look for opportunities to optimize or consolidate code.
- Keep runtime behavior deterministic where practical.
- Avoid silent fallbacks or hidden repair logic; if the runtime has to compensate for anomalous input or state, log it clearly.
- Do not just write a patch to an issue without first understanding whether the patch only masks the underlying problem. Think deeply about **why** the problem exists in the first place and write code that addresses the root cause.
- Remove stale code, outdated compatibility paths, and dead configuration when they no longer serve the current architecture.
- Keep tests aligned with real behavior, especially when changing persisted schemas, message contracts, or recovery flows.
- Review your implementation one more time critically after the original pass and before PR. Pay attention to edge cases or multi-step (e.g. circular dependencies, infinite resource blocks etc) failures. Fix any issues or edge-cases that you uncover.

### Writing Pull (Merge) Requests

- When writing a PR clearly state:
    - The observed problem, bug or requested feature
    - The proposed solution in words
    - The solution as implemented in code (high-level architecture)
    - The tests (unit, integration etc) created to validate the solution and to validate that the solution completely covers the requested wrok

- Do not include any extraneous information e.g.:
    - "Generated with Claude Code"

### Capture durable lessons in the right place
- Convert repeated mistakes or tricky repo-specific constraints into documentation, tests, or automation.
- Prefer reusable system improvements over one-off reminders in task output.

### Plan carefully to utilize any fitting existing facilities / libraries
- Do not reinvent a feature or process if it can be created with the help of existing functionality inside of the already-used libraries. E.g. For working with agents, always consult the strands sdk documentation first to see if it can be utilized when dealing with tools, turn taking, telemetry etc.

## 2. Project-Specific Guidance

### Core invariants
- This project is a NATS/JetStream-based multi-agent system built around the AWS Strands Agents SDK.
- Inter-agent communication must use structured Pydantic message models serialized as JSON. Do not introduce free-form agent-to-agent text channels.
- Use `logging`, not `print`, for runtime output.
- Durable work is pulled, never pushed (ADR-019, `custom_transport.nats_pull`): every durable NATS consumer is a pull consumer, and a worker asks for a task only while it can start it. Never add push consumers or deliver groups for work, or subscribe to NATS outside the transport adapter. Workers for waiting delegated tasks are started by the worker pool controller (`custom_agents.worker_pool_controller`), not by a model (TeamArchitect may still start agents deliberately with `instantiate_agent`), and a delegated task is stopped at the worker holding it or withdrawn from its queue (`delegated_work_runtime.stop_delegated_task`).
- Strands-exposed tools belong in the documented tool surface and should use the project’s established Strands tool patterns.
- Agent-run shell and Python commands must go through `custom_tools.code_sandbox` (or the person's own sandbox from `custom_tools.openshell_sandboxes`), and what a command reaches comes only from `custom_agents.code_access`: folder mounts from both sides' access, the network only when both sides hold internet access (`fn:internet`), and running outside the sandbox only at `full` on `code:unconfined` (jobs on host execution profiles too, `runtime_hosting.authorization.require_host_execution`). Never add a code-execution path that bypasses these. Landlock does not cover UDP, so every internal service must require credentials that sandboxed code cannot read, and no secret may appear on a process command line.
- Every callable operation needs a level in the tool catalog (`access_catalog`, `ua_definitions/access`); a new tool, broker action, CLI command or route adds its level in the same change, and unclassified operations need `full`. The broker must never run a CLI argument list other than the one `secure_tool_broker.cli_passthrough` checked.
- Derive every instance path from `runtime_settings.instance_layout.InstanceLayout`; never add a location setting. Agent tools resolve folders per task through `custom_tools.workspace_scope` (the person's folder plus shared `team/` and `agents/`); nothing under the root outside `workspaces/` is agent-reachable except read-only skill folders. Control files change only through typed services.
- Every agent-side file open goes through `custom_tools.file_access`; never open a path an agent named by name. Trusted code never follows links in agent-writable folders: broker and gateway code opens such files with `access_control.safe_open`.
- Folders are access targets (`access_control.folders`), named `team/…`, `agents/…`, `data/<name>/…` or `users/<folder>/…`, never by host path. Only its owner shares a person's folder or files, and only with other people (`assistant_gateway.file_sharing`); agent grants on it are refused. Folders outside the instance reach agents only as data folders that administrators register in the access store (`access_control.data_folders`), never through configuration.
- Access decisions go through the access service (`access_control.decisions.AccessService`, real Cedar): the agent side, then the person the task works for. Grants serve one person's work: the agent side counts only grants for the task's person (`GrantV1.for_person_id`; `None` only for standard grants, administrators' `--everyone` grants and system work), and a request for a person who holds the access is theirs alone to decide. Every agent function-tool call is decided by the one hook in `custom_agents.access_runtime`; broker actions and MCP calls by `secure_tool_broker.access_gate`. Do not add ad hoc admin, owner or role checks. Whose private records a request may touch is decided only in `access_control.ownership`, with the person from the signed context (`custom_agents.access_runtime.verified_person_id`) or the signed-in session. People acting in the gateway's own controls are checked with `assistant_gateway.access_setup.PersonChangeCheck`. Who administers people (roles, owner implies admin, the last owner, blocking a person) is decided only in `access_control.people`; people stop access with blocks (`AccessRequests.block`), not the old emergency deny.
- A person's departure (disable, remove, restore) runs only through `assistant_gateway.application.person_lifecycle` and the manager's `PersonStandingCommandV1` handler (`custom_agents.person_departure_runtime`, gateway-signed administrator context only, idempotent, acting on the people copy); held data is moved, never copied, out of `workspaces/` and team schedules move to an administrator, never with the person.
- Access follows the work (`AccessService.agent_grants`): an agent's access is its role's standard access, administrators' team defaults and its task's access, plus the lasting access of whom it works for: the team lead's per-person access (in all its work for that person, scheduled runs too) and, in a scheduled job's run, the schedule's (`SubjectKind.SCHEDULE`; a job run is a person's schedule a worker type runs, schedule id only from `task_identity.job_schedule_id`; lead-run schedules and system timers work like a person's request). Workers keep no lasting access for a person's work (`AccessService.worker_lasting_refusal`); a worker's task outside a job run gets what the team lead may hand on inside the decision (`AccessService.hand_on_for_task`); built-in system agent types come only from the release definitions (`access_control.system_agents`, read by `entities.agent_role_resolver`); "one task" is the person's request (signed context id, `AgentPrincipal.work_id`). Access is a level on a whole tool, never one operation (blocks may name one; a block naming an operation the catalog no longer has blocks the whole tool, `access_control.stale_blocks`), and one entry per tool per holder and person's work (`access_control.grant_entries`): broader replaces narrower, removal takes the whole entry. Never add lasting worker grants, per-operation grants or a tool that gives agents access.
- Agents ask for access only through `request_access` (`access_control.requests`), and TeamLead proposes what nobody can hand on (new web API tools, grants, blocks, data folders, account links) only through `propose_access_change` (`access_control.proposals`, typed items, one administrator review): trusted code hands a worker's request down, for its task, when the team lead holds an automatic delegable grant covering it, otherwise a person decides in the gateway's access Inbox (`assistant_gateway.access_inbox`). The waiting task pauses with a local Strands interrupt, never a question to the requester, and resumes from the stored decision; `AccessRequestResolvedV1` carries ids only. Agents never approve requests, and notices never grant anything.
- Access workflows (`custom_agents.access_workflows`) are a view over access requests and grant nothing: requests reach a step only through the `RequestWatcher` port of `AccessRequests` (the parent's step only through the shared verified signed task context), a denied request closes its step, and the page decides through the access Inbox.
- Staged platform changes (runtime settings including SOUL.md and the roster, restarts, specialists, team skills, hosted MCP servers, managed execution services and SSH typing) are applied only through `apply_change` (`access_control.changes`, adapters in `custom_agents.change_approvals`). With authorization on, a person approves the exact plan digest in the Inbox and trusted code applies it once; agents never hold `full` on platform tools. The one exception is model choice (`custom_agents.model_choice`): a runtime settings plan that changes only an agent type's provider (one the instance is already set up for), model, reasoning effort or summary, context window, or image, audio and file input, is the `model_choice` change on `fn:model_choice`, which only administrators hold (`owner_scope: administrators`, Cedar `guard:admin-only-tools`); it applies at once (`held_access`) when TeamLead and the person it works for both hold it, and the person gets a notice; anything else in the plan, team-wide defaults, the request API mode, endpoints and credentials keep the Inbox review. `confirmed` is conversational agreement and counts only with authorization off. Change services describe and apply plans; they never decide authority.
- Changes the gateway applies at the decision (file deliveries, access proposals) are `AccessRequests.appliers`: the applier re-checks the stored plan digest and the reviewer's authority and records the outcome with its effects so a failure leaves nothing applied; deciding them without their applier is refused.
- An agent's tool list follows its grants (`custom_agents.tool_sync`), never fixed tool groups or restart-to-attach; the catalog's `on_request` groups are shown only after the agent loads them for its task with `use_tools` (`custom_agents.tool_loading`), within the model's `max_tools_per_request`, and loading never gives access. Agent definitions name catalog tool ids as `default_tools` (`custom_agents.default_tools`); they grant nothing and, with authorization off, are the agent's tools beside its role's standard ones. Agents reach a missing tool with `find_tools` and `request_access`, and a service the catalog lacks with `propose_access_change`. An installed MCP server is the team tool `mcp:<name>` (tool set `mcp_servers`): installing it is an access-proposal item an administrator approves (`mcp_connections(action="propose")`, `access_control.proposals`, default rows in `access_control.mcp_servers`), its tools load before model calls, never at startup, from grants (with authorization off, from the records meant for the agent's type) through one loader (`custom_agents.mcp_server_tools`) that retries a server that does not answer only within `mcp.load_retry_window_minutes` and then tells administrators, and there are no per-agent-type MCP attachments or owner-only rules; never add them back.
- Treat system-managed schedule control state as a mandatory authorization boundary: the store changes it only with an exact `custom_agents.schedule_management` permit, and missing provenance is system/unclassified and fails closed. Agents change schedules through one path (`custom_agents.system_schedule_changes`): a person's own schedule changes after the per-call hook and the owner check, with no permit; a system-managed one is staged as the exact `system_schedule` change (platform `fn:system_schedules`) and applied only through `apply_change`, which rebuilds it, checks its digests and issues a `person_approval` (or `agreement`) permit. Objective tools use `held_access` permits, gateway commands name their person only through the gateway-signed context (`custom_agents.manager_control_runtime.verified_command_person`) and the manager decides their system schedule changes with it (the `PersonChangeCheck` rule, `system_schedule`), and scheduler maintenance and trusted runtime installation keep their own modes. Agent objective cascades never bypass this, and agents never forge gateway manager-control messages.
- Route agent-authored new specialist creation and removal through `custom_agents.generated_agent_provisioning` (staged plans applied with `apply_change`); accept only behavioral fields and catalog tool ids as default tools. `authorization.enabled: false` must remain fully operational: the person's conversational agreement applies bounded settings, specialists and skills, without weakening local guards.
- Trust who a task works for only from its signed task context or its schedule run identity, checked by the one resolver `access_control.task_identity.resolve_task_identity`: the gateway signs people's tasks and the manager's scheduler signs scheduled runs; a worker process without the key gives a run its schedule's manager-signed permit (one per schedule, no roles, `custom_agents.schedule_permits`) and asks the manager with its claim only when there is none (`custom_agents.scheduled_run_contexts`); never give agent processes the key. The owner's current standing and roles come from the gateway's copy of the team's people (`access_control.people_directory`; read live for permits, cached 30 seconds; a disabled or removed owner's schedules do not run); never trust person, team or role fields from message or request bodies. Every agent broker request (actions and MCP calls) goes through `secure_tool_broker.access_gate` in both authorization modes; each supervised process calls the broker with its own derived token (`access_control.caller_tokens`), and only the gateway reaches sign-in, credential and account-link forms.
- Credentials are chosen by account links (`AccountLinkV1` in `storage.access_repository`): per tool and owner (team or person), created by a person in the broker's labeled form. The team's account serves people only while the team shares that service (`access_control.account_sharing`, changed by administrators with `/accounts`); every place that picks or reports an account asks `TeamAccountSharing`, and GitHub, Google and the GitHub API are always personal. Agents never select credential keys, send credential placeholders, or carry binding references, and a link never grants access by itself. Tools with `account: personal_login` (`cli:github`, `cli:google`) run only under the task person's own stored sign-in (`/link <tool>`, the one command for every tool's account; sign-in tools are listed in `secure_tools.auth_methods.SIGN_IN_TOOLS`), never without a person; X (`cli:x`) uses the person's own browser sign-in first and the team's account link only as the fallback while the team shares X (it starts private), and its team app is an administrator's setting (`/auth x team_client`); gog runs in a fresh private home with an allowlisted environment (`secure_tool_broker.google_workspace`), and the team's Google OAuth client is an administrator's setting (`/auth google team_client`). No tool credential comes from environment variables; startup refuses the removed ones (`runtime_settings.security.REMOVED_TOOL_CREDENTIAL_ENV_VARS`).
- Model provider keys belong to an endpoint address (`model_providers.endpoints`) and are stored in the broker's credential store (`secure_tool_broker.model_keys`, saved with `/auth model` only after a live test; onboarding and the team transfer store keys tested when first saved); the runtime gets them only through `custom_agents.model_keys`, which sends a key only to its own address. Their environment variables (`LITELLM_API_KEY` and others) override stored keys but never silently: the override is logged, reported to the broker by name and shown to people. A person's own key (`/auth model me`) is released only for a task whose verified signed context names them, never falls back to the team's key, and never triggers team-wide failover. Never hand a model object a fixed key; the key routes serve only processes that run agents, plus the gateway for the xAI key Grok voice uses, and never agent tools. Onboarding hands typed model keys to the store, never to `team.env`. Whether a person's work may use the team's keys and a person's own model live in `access_control.person_settings` (access store): the broker refuses team keys (environment overrides included) for people without them, and a person's own model is swapped in per turn by `custom_agents.person_model_runtime`, never written into the agent's model fields.
- A team moves to a new version only through the team transfer (`team_transfer`): each version exports its own state into versioned sections with tracked schemas, and a version imports its own section versions and the previous ones through one conversion step per section, the one deliberate compatibility layer. The first import writes into a fresh instance; later reviewed batches require its receipt and the exact same signed backup, skip imported identities and keep existing destination records. Schedules travel through editable Markdown and each selected schedule needs confirmation after its last edit in the browser review, which the operator's loopback page and onboarding's Import share (`team_transfer.review_session`). Noncredential batches require a stopped runtime; credentials go through the running broker, and onboarding's Import leaves them encrypted for the first start (`team_transfer.credential_handover`), never with the passphrase. Import keeps ids, checks every record with this version's own loaders and models, and refuses rather than drops what it cannot accept. Validation reads stores without initialization; failed later batches require restoring the pre-batch backup. A change to a persisted record a section carries updates that section, its schema and, when the format changes, its conversion step in the same change. The broker's stored credentials are exported and stored again only by the broker, through the transfer routes only the `operator` caller reaches; host commands have no catalog level, like other operator commands.
- Knowledge base writes go through `knowledge_base.service.KnowledgeService` with the actor built from verified identity only (`custom_tools.providers.knowledge.knowledge_actor`): `personal/` is always the verified person's own space, an agent working for a person only proposes team changes (pending contributions, published by the curator working for that person or by the person), credentials and identity numbers are refused by `knowledge_base.sensitive_content`, and per-agent write limits are checked inside the store's write transaction (`storage.knowledge_repository`); never add a knowledge write path that skips them.
- Route team skill installs and removals through `custom_agents.skill_installation` and `custom_agents.skill_uninstallation` (scanned, digest-bound plans applied with `apply_change`); removal requires an attested managed install and a recoverable atomic move. Team skills are team-owned (`fn:team_skills`): an administrator may let the team lead manage them, and every change then sends administrators a notice. Route unknown-operation proposals and execution through `custom_agents.installed_permissions`; agent proposals are inert prose/handler ids, trusted code plus registry metadata own schemas/impact/risk, administrators install definitions in the gateway, and each call is decided by the access hook as `procedure:<handler>`.

### Architecture and boundaries
- Onboarding imports persist their encrypted credential handover before clearing the unfinished-import marker. The launcher completes that broker import before initializing agents or the gateway; retryable failures keep the handover and stop startup, while terminal refusals are reported to the exporting owner.
- Record major subsystems, external integrations, ownership boundaries, and places where coupling is intentional.

### Design steering
- The design of all UI elements must be consistent within the project. Before generating or adjusting UI elements, read the `ui/DESIGN.md` document and make sure to adhere to the instructions and aesthetics captured there. If the user instructs you to change the design or create new UI elements, confirm whether the DESIGN.md file should be updated to capture user design choices.

### Testing strategy
- Record which test types this project should lean on, what must be covered by integration or live tests, and any required test environments or fixtures. Be diligent in executing all tests and do not skip live tests unless they have a dependency that you cannot fulfill.
- For all live tests attempt to utilize the `config.yaml` and `debug.env`, if they exist. If those fail, surface the failure to the user and attempt to utilize other means for live testing.

### Debugging strategy
- When I ask you to debug an issue, do not just assume you know the answer or that the answer you find online is sufficient. Do not rush to fix the bug. Instead take care to replicate the exact issue first, and if necessary, iterate on the testing probes such that you catch the exact bug and know exactly why it is happening. Fix the bug in the most efficient way possible, making sure that the fix fits in well with the architecture and is not a one-off. Try not to disrupt any other logic. At the end, report to me the root cause and the fix you applied.

### Code preferences
- Record repo-specific implementation preferences beyond the general guidance above, such as framework patterns, naming conventions, or observability expectations.
- New operator/runtime parameters must be represented in `config.yaml` and `instance_config.yaml` schemas and documented there first. Environment variables may remain override, secret, or child-process transport mechanisms, but they must not be the only durable configuration surface.
- In `config.example.yaml`, prefer inline comments on the same line as the
  parameter. Use comments above the YAML node only for whole-section
  explanations.

### Other Guidance
- Never put personal details in tracked files or commit metadata: no real names, handles, assistant names, emails, chat or account IDs, domains, machine or runner names, host paths, instance IDs, or copied chats, emails and logs. Use the placeholders and rules in `CONTRIBUTING.md` ("Personal details"), and summarize real incidents instead of copying them.
- Canonical shared workspace skills normally live in the sibling `../ua-skills` repository; check and update them when runtime or operator behavior changes.
- Use the workspace `.venv` for Python commands whenever it exists
  (`.venv/bin/python`, `.venv/bin/pytest`, etc.); do not infer dependency
  state from the global Python environment.
- When your task completes, take a moment to review the requirements.txt and the lock file and suggest any improvements / updates to the user (e.g. newer library versions).
- Do **not** put architectural details into QUICKSTART.md. It is purely designed to give users a quick overview and a fast way to start the framework.

### Release process
- Make a release only when the user explicitly asks for one. `VERSION` is the
  canonical public version and must use `MAJOR.MINOR.PATCH` without a leading
  `v`.
- Interpret release requests as semantic-version increments from the current
  `VERSION`: patch increments `X.Y.N` to `X.Y.(N+1)`; minor increments
  `X.N.Y` to `X.(N+1).0`; major increments `N.X.Y` to `(N+1).0.0`. Do not infer
  a prerelease or skip a number unless the user explicitly requests it.
- Start from a clean, up-to-date `main` with all intended changes merged.
  Confirm that neither the local repository nor `origin` already has the
  target `vX.Y.Z` tag. Never move or reuse a published release tag; correct a
  bad release with a new patch version.
- Synchronize the new version in `VERSION`, the current-version banner in
  `README.md`, `[workspace.package].version` in `Cargo.toml`, the root package
  entry in `Cargo.lock`, and the Photon sidecar's `package.json` plus both root
  entries in `package-lock.json`, and the install instructions: the
  `install.sh` tag and image tag in `QUICKSTART.md`,
  `user_docs_source/quickstart.md` and `user_docs_source/docker.md`, and the
  runtime release in `docker-compose-setup.html`. Use `npm version X.Y.Z
  --no-git-tag-version` from the sidecar directory and refresh the Cargo
  lockfile with Cargo instead of hand-editing generated lock metadata.
- Repository setup-guide download links use `docker-compose-setup.html` from
  `main`, so install corrections merged after a release reach new readers.
  Release attachments are snapshots of their tagged source.
  Setup-guide attachments belong only to stable releases; the tracked guide
  still installs the last stable version on pre-release commits.
- Review changes since the previous `v*` tag and identify the most noteworthy
  user-facing features, fixes, security changes, breaking changes, migrations,
  and operator actions. Ensure pull-request labels support the categories in
  `.github/release.yml`; generated GitHub notes are the public release notes,
  so do not bury important upgrade information in an unlabeled or vague PR.
- Run the full release verification appropriate to the change, including the
  synchronized-version tests, CI workflow tests, strict documentation build,
  Python tests, Cargo checks/tests, and Photon sidecar tests. Do not tag a commit
  with failing or skipped-required release checks.
- Commit the synchronized version as `Release vX.Y.Z`, push `main`, then create
  an annotated signed tag `vX.Y.Z` on that exact commit and push the tag. If
  signing is unavailable, stop and ask the user rather than silently creating
  an unsigned release tag.
- A pushed `vX.Y.Z` tag must trigger all three release surfaces:
  `.github/workflows/publish-docs.yml`,
  `.github/workflows/publish-container.yml`, and
  `.github/workflows/publish-release.yml`. The release workflow validates that
  the tag matches `VERSION`, archives every tracked file under a versioned root
  directory, and publishes the resulting `.zip` plus its SHA-256 checksum to a
  GitHub Release with generated categorized notes.
- Public releases must publish an image attestation. If the repository is
  private and the GitHub plan does not support attestations, obtain an explicit
  per-release user waiver and set the repository variable
  `PRIVATE_RELEASE_ATTESTATION_WAIVER` to the exact `vX.Y.Z` tag. The container
  workflow must emit a visible warning for that exact-tag waiver; never use a
  blanket or silent attestation skip. The only standing waiver is
  `PRIVATE_NIGHTLY_ATTESTATION_WAIVER=true`, set by the owner, covering
  nightly pre-releases only, with a warning on every waived run.
- Pre-release tags lead up to the next release after `VERSION`:
  `vX.Y.Z-beta.N` and `vX.Y.Z-rc.N` (tagged by a person, only when the user
  asks) and `vX.Y.Z-nightly.YYYYMMDDHHMM` (created by `nightly.yml`). They
  publish GitHub pre-releases and exact-version images only, never `latest`
  or the docs site, and are never proposed for the desktop compatibility list.
  A stable release's container workflow opens that proposal automatically.
  See `docs/LOCAL_CI.md` for the rules, secrets and manual fallback.

### Change-specific requirements
- If you change SQLite-backed stores, persisted schemas, or DB-owned models, update the affected tests and fixtures in the same change so validation exercises the real schema.

## 3. What Belongs Where
- Keep this file focused on cross-project behavior and repo-level operating rules.
- Keep high-level orientation in this repo: `README.md`, `QUICKSTART.md`, and `README_ARCHITECTURE.md`.
- Use `ua_definitions/` for checked-in Universal Agents definition catalogs, including agent definitions, agent prompt fragments, and credential templates. Do not place these under `data/`, because Docker deployments may mount over `/data`.
- Use `data/` for checked-in sample data, fixtures, static assets, schemas, seeds, or other project data files that are part of the repository contract.
- `AGENTS.md` is a **living** document. Update it with project-specific guidance as appropriate but keep it brief and to the point.

### Additional Info

- In addition to this file, read and carefully follow `../ua-architecture/AGENTS.md`.

### RTK shell requirement

- Route every shell command through RTK. Prefer the matching wrapper, such as
  `rtk git`, `rtk grep`, `rtk read`, `rtk find`, `rtk diff`, `rtk pytest`,
  `rtk ruff`, `rtk mypy`, or `rtk cargo`.
- Preserve the repository `.venv` requirement. Put `$PWD/.venv/bin` first on
  `PATH` when an RTK wrapper needs a Python executable from the virtual
  environment.
- Use `rtk proxy <command> ...` for an unsupported command. Use
  `rtk proxy sh -c '<commands>'` for a compound command or pipeline without a
  direct wrapper.
- Do not add an RTK prefix to commands passed to Universal Agents' own `shell`
  tool; its process boundary applies RTK automatically.
- This requirement applies to shell execution, not first-class editor, browser,
  image, collaboration, or other non-shell tools.
- If an RTK wrapper behaves differently from the underlying command, inspect
  `rtk rewrite '<command>'` and use the narrowest correct wrapper or proxy form.
