/* Standalone design simulation. No credentials, provider calls, or authority. */
"use strict";
const $ = (id) => document.getElementById(id);
const steps = [
  {
    id: "access",
    title: "Choose access",
    mode: "Review with you",
    detail:
      "Choose the account capabilities to allow in one review. The existing X connection will be reused.",
    info: "This decision covers the selected account, tool setup, invocation policy, and required restart. It does not publish a post.",
  },
  {
    id: "setup",
    title: "Prepare tools",
    mode: "Automatic after approval",
    detail:
      "Apply the selected capability access and the approved agent restart.",
    info: "The system performs the exact setup you approved and checkpoints its result. It will not ask for the same setup again.",
  },
  {
    id: "check",
    title: "Check account",
    mode: "Automatic after approval",
    detail: "Check that the connected account is the expected account.",
    info: "This uses the account-status capability from the first review. It is omitted if you did not select that capability.",
  },
  {
    id: "draft",
    title: "Prepare an update",
    mode: "Automatic",
    detail:
      "Prepare the first text post for your review. Drafting does not publish anything.",
    info: "The exact draft is not known at the start of this example, so publication needs a later decision. The user can ask for changes in this workflow's conversation.",
  },
  {
    id: "review",
    title: "Review the post",
    mode: "Review with you",
    detail: "Review the exact text and destination before publication.",
    info: "This approves one new external effect: publish this exact draft to this account. Earlier account and setup approvals remain valid.",
  },
  {
    id: "publish",
    title: "Publish approved post",
    mode: "Automatic after approval",
    detail:
      "Publish only the exact text and account approved in the previous step.",
    info: "The final action rechecks current authority and the approved content. An uncertain provider result would pause for reconciliation instead of posting twice.",
  },
];
const exampleDraft =
  "A quick update from Acme: we’re preparing something new. More details soon.";
let state,
  generation = 0,
  focusBeforeDialog,
  diagnosticEvents = [];

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text) node.textContent = text;
  return node;
}
function addActivity(text, code = "workflow_progress") {
  diagnosticEvents.push({
    time: new Date().toISOString(),
    code,
    step: steps[state.current]?.id || null,
    phase: state.phase,
    summary: text,
  });
  if (diagnosticEvents.length > 100) diagnosticEvents.shift();
  const list = $("activity-list");
  list.append(el("li", "", text));
  while (list.children.length > 20) list.firstElementChild.remove();
  list.scrollTop = list.scrollHeight;
}
function message(role, text) {
  const entry = el("div", "chat-entry " + role);
  entry.append(
    el("strong", "", role === "user" ? "You" : "Nova · Simulated reply"),
    el("p", "", text),
  );
  $("messages").append(entry);
  while ($("messages").children.length > 20)
    $("messages").firstElementChild.remove();
  $("messages").scrollTop = $("messages").scrollHeight;
}
function reset() {
  generation += 1;
  for (const dialog of document.querySelectorAll("dialog[open]"))
    dialog.close();
  state = {
    phase: "idle",
    current: -1,
    statuses: steps.map(() => "pending"),
    selected: new Set(["check", "post"]),
    approved: false,
    posted: false,
    account: "@acme",
    draftReady: $("draft-mode").value === "ready",
    publishNow: true,
    postApproved: false,
    failure: $("failure-mode").value,
    hasFailed: false,
    publicationUncertain: false,
  };
  diagnosticEvents = [];
  $("activity-list").replaceChildren(
    el("li", "", "Workflow prepared. Waiting for you to start."),
  );
  $("messages").replaceChildren();
  message(
    "assistant",
    state.draftReady
      ? "The account is connected and the draft is ready. You can approve setup and this exact post together, or leave publication out."
      : "The account is connected. Start the workflow to choose access, then I will pause when the first draft needs review.",
  );
  $("question").value = "";
  render();
}
function render() {
  $("step-list").replaceChildren(
    ...steps.map((step, i) => {
      const row = el("li", "flow-step");
      row.dataset.state = state.statuses[i];
      const heading = el("div", "step-heading");
      const info = el("button", "info", "i");
      info.type = "button";
      info.dataset.stepInfo = String(i);
      info.setAttribute("aria-label", "About " + step.title.toLowerCase());
      heading.append(el("span", "step-name", step.title), info);
      row.append(
        el(
          "span",
          "step-number",
          state.statuses[i] === "complete" ? "✓" : String(i + 1),
        ),
        heading,
        el(
          "span",
          "step-state",
          {
            pending:
              state.postApproved && step.id === "review"
                ? "Included in your approval"
                : step.mode,
            waiting: "Waiting for you",
            running: "Running…",
            complete: "Completed",
            omitted: "Not selected",
            stopped: "Not performed",
            failed: "Needs attention",
          }[state.statuses[i]],
        ),
      );
      return row;
    }),
  );
  const current = steps[state.current];
  $("flow-status").textContent = {
    idle: "Ready to start",
    running: "Running",
    waiting: "Waiting for you",
    complete: "Complete",
    stopped: "Stopped",
    failed: "Needs attention",
  }[state.phase];
  $("start-flow").disabled = state.phase !== "idle";
  $("stop-flow").disabled = !["waiting", "running", "failed"].includes(
    state.phase,
  );
  $("start-flow").textContent =
    state.phase === "idle"
      ? "Start workflow"
      : state.phase === "complete"
        ? "Workflow complete"
        : "Workflow started";
  $("open-step").hidden = state.phase !== "waiting";
  $("open-step").textContent =
    state.current === 0 ? "Review account access" : "Review this post";
  $("current-title").textContent =
    state.phase === "idle"
      ? "Before You Start"
      : state.phase === "complete"
        ? "Workflow Complete"
        : state.phase === "stopped"
          ? "Workflow Stopped"
          : current.title;
  $("current-description").textContent =
    state.phase === "idle"
      ? state.draftReady
        ? "The account connection and draft are ready. Review all known access and publication steps in one decision, then watch them execute."
        : "First choose access. The system will prepare the tools and draft, then pause to review the new content."
      : state.phase === "complete"
        ? state.posted
          ? "Preview finished: access was prepared and the approved example post was published in simulation."
          : "The selected access is ready. Current publication was left out of this workflow."
        : state.phase === "stopped"
          ? "Unstarted actions are stopped. Previously completed access remains available."
          : state.phase === "failed"
            ? "The workflow has paused. Completed work is kept; dependent steps will not run until this issue is resolved."
            : current.detail;
  const detailTitle =
    state.phase === "idle"
      ? state.draftReady
        ? "Approve known steps together"
        : "Two places need your input"
      : state.phase === "waiting"
        ? "Your decision is needed"
        : state.phase === "running"
          ? "Why this step is running"
          : "Completed work is kept";
  const detailText =
    state.phase === "idle"
      ? state.draftReady
        ? "The full draft can be reviewed now. If you include it, there will be no second approval for the same post."
        : "The future draft is not known yet, so its publication needs a later review. Starting alone grants no access."
      : state.phase === "waiting"
        ? state.current === 0
          ? "Review the selected capabilities and setup. You can close the dialog to ask questions here."
          : "Account setup is already approved. This decision is only about publishing the new draft."
        : state.phase === "running"
          ? current.info
          : state.approved
            ? "Your access decision is recorded. Future requests reuse the connection and selected setup; the chosen posting rule still applies."
            : "No new access was approved. The existing connection was not removed.";
  $("current-detail").replaceChildren(
    el("strong", "", detailTitle),
    el("p", "", detailText),
  );
  const done = state.statuses.filter((s) => s === "complete").length,
    omitted = state.statuses.filter((s) => s === "omitted").length;
  $("progress-count").textContent =
    `${done} of 6 steps complete${omitted ? ` · ${omitted} not selected` : ""}`;
  $("flow-receipt").textContent =
    state.phase === "waiting"
      ? "Paused for your decision. Closing the dialog does not reject or approve."
      : state.phase === "complete"
        ? "Preview complete. No real account, permission, or post was changed."
        : "Starting runs the plan. Each approval applies only to the effect shown.";
  renderNextSteps();
  renderFailure();
}
function renderNextSteps() {
  const upcoming = steps
    .filter(
      (_, index) =>
        index > state.current &&
        !["omitted", "stopped", "complete"].includes(state.statuses[index]),
    )
    .slice(0, 2);
  const stopped = ["complete", "stopped"].includes(state.phase);
  $("next-steps").replaceChildren(
    ...(stopped || !upcoming.length
      ? [
          el(
            "li",
            "",
            state.phase === "failed"
              ? "No retry will occur until the uncertain result is checked."
              : "No further steps are queued.",
          ),
        ]
      : upcoming.map((step) =>
          el(
            "li",
            "",
            `${step.title}. ${state.phase === "failed" ? "Blocked until the failed step is resolved." : step.id === "review" && state.postApproved ? "Already included in your decision." : step.mode + "."}`,
          ),
        )),
  );
}
function renderFailure() {
  $("failure-panel").hidden = state.phase !== "failed";
  if (state.phase !== "failed") return;
  const unknown = state.failure === "unknown";
  $("failure-explanation").textContent = unknown
    ? "The posting service timed out after receiving the request. The post may already exist. Publishing again could create a duplicate."
    : "The tool setup service did not respond. Your access decision and existing account connection are retained; posting has not started.";
  const rows = unknown
    ? [
        [
          "Completed",
          "Account connection, tools, account check, and draft approval.",
        ],
        ["Uncertain", "Whether the approved post was published."],
        [
          "Next action",
          "Check the provider's result before considering a retry.",
        ],
        ["Error reference", "preview_provider_result_unknown"],
      ]
    : [
        ["Completed", "Account connection and your access decision."],
        [
          "Still waiting",
          "Tool setup, account check, and any selected publication.",
        ],
        [
          "Next action",
          "Retry only tool setup under the existing valid approval.",
        ],
        ["Error reference", "preview_setup_service_unavailable"],
      ];
  $("failure-facts").replaceChildren(
    ...rows.flatMap(([key, value]) => [el("dt", "", key), el("dd", "", value)]),
  );
  $("recover-step").textContent = unknown
    ? "Check publication result"
    : "Retry failed step";
}
function facts(rows) {
  const list = el("dl", "approval-facts");
  for (const [key, value] of rows)
    list.append(el("dt", "", key), el("dd", "", value));
  return list;
}
function showReview() {
  if (state.phase !== "waiting") return;
  const access = state.current === 0;
  $("dialog-heading").textContent = access
    ? "Allow Selected X Access"
    : "Publish This Post?";
  $("dialog-description").textContent = access
    ? state.draftReady
      ? "Approve selected access and required setup. You can also include the exact post below in this one decision."
      : "One decision covers selected access and required setup. The draft is not ready yet; you will review its exact content later."
    : "Your account connection and setup are already approved. This decision publishes only the exact text below.";
  const fields = $("dialog-fields");
  fields.replaceChildren();
  fields.append(
    facts([
      ["Agent", "AcmeCommunicationsAgent"],
      ["Account", state.account + " · Existing connection"],
      [
        access ? "Access duration" : "Approval scope",
        access ? "Until removed · This agent only" : "This exact post, once",
      ],
    ]),
  );
  if (access) {
    for (const [id, title, description] of [
      [
        "check",
        "Check account",
        "Check the account identity and sign-in status.",
      ],
      [
        "post",
        "Prepare posting access",
        "Keep posting ready; ask before publishing new content.",
      ],
    ]) {
      const label = el("label", "approval-choice"),
        input = el("input");
      input.type = "checkbox";
      input.dataset.operation = id;
      input.checked = state.selected.has(id);
      const text = el("span", "", title);
      text.append(el("small", "", description));
      label.append(input, text);
      fields.append(label);
    }
    fields.append(
      el(
        "p",
        "restart-impact",
        "Required setup: enable the selected tools and restart all agents. Running work may be interrupted.",
      ),
    );
    if (state.draftReady) {
      fields.append(
        el("span", "dialog-draft-label", "Exact post to " + state.account),
        el("blockquote", "post-draft", exampleDraft),
      );
      const label = el("label", "approval-choice"),
        input = el("input");
      input.type = "checkbox";
      input.id = "include-post";
      input.checked = state.publishNow && state.selected.has("post");
      input.disabled = !state.selected.has("post");
      label.append(
        input,
        el(
          "span",
          "",
          "Publish this exact post after setup. Include it in this approval.",
        ),
      );
      fields.append(label);
    }
  } else fields.append(el("blockquote", "post-draft", exampleDraft));
  $("approve-step").textContent = access
    ? "Approve selected steps"
    : "Approve & publish";
  $("approve-step").disabled = access && !state.selected.size;
  $("reject-step").textContent = access ? "Reject access" : "Do not publish";
  focusBeforeDialog = $("open-step");
  $("step-dialog").showModal();
  $("close-dialog").focus();
}
function stop(reason) {
  generation += 1;
  if ($("step-dialog").open) $("step-dialog").close();
  state.phase = "stopped";
  state.statuses = state.statuses.map((status) =>
    ["complete", "omitted"].includes(status) ? status : "stopped",
  );
  addActivity(reason);
  render();
  if (state.publicationUncertain) {
    $("current-description").textContent =
      "The workflow is stopped, but the provider's publication result is still unknown. Check that result before any future publish attempt.";
    message(
      "assistant",
      "Stopped further work. The previous post may already exist; its outcome is still unknown. Download the diagnostics and check the provider result before retrying publication.",
    );
  } else
    message(
      "assistant",
      state.approved
        ? "Stopped the remaining work. The account setup you approved is kept; no post was published."
        : "Stopped this request. No new access was granted, and the existing connection is unchanged.",
    );
}
async function advance() {
  const token = generation;
  while (++state.current < steps.length) {
    const step = steps[state.current];
    if (
      (step.id === "check" && !state.selected.has("check")) ||
      (["draft", "review", "publish"].includes(step.id) &&
        (!state.selected.has("post") ||
          (state.draftReady && !state.publishNow)))
    ) {
      state.statuses[state.current] = "omitted";
      addActivity(`${step.title}: omitted by your selection.`);
      continue;
    }
    if (
      (step.id === "draft" && state.draftReady) ||
      (step.id === "review" && state.postApproved)
    ) {
      state.statuses[state.current] = "complete";
      addActivity(`${step.title}: reused from the initial review.`);
      render();
      continue;
    }
    if (["access", "review"].includes(step.id)) {
      state.phase = "waiting";
      state.statuses[state.current] = "waiting";
      render();
      addActivity(`${step.title}: waiting for your decision.`);
      showReview();
      return;
    }
    state.phase = "running";
    state.statuses[state.current] = "running";
    render();
    addActivity(`${step.title}: started.`);
    await new Promise((resolve) => setTimeout(resolve, 1200));
    if (token !== generation) return;
    if (
      !state.hasFailed &&
      ((step.id === "setup" && state.failure === "setup") ||
        (step.id === "publish" && state.failure === "unknown"))
    ) {
      state.hasFailed = true;
      state.publicationUncertain = step.id === "publish";
      state.phase = "failed";
      state.statuses[state.current] = "failed";
      addActivity(
        step.id === "setup"
          ? "Tool setup did not respond. Dependent steps paused."
          : "Provider result unknown. No automatic publish retry.",
        step.id === "setup"
          ? "setup_service_unavailable"
          : "provider_result_unknown",
      );
      render();
      return;
    }
    state.statuses[state.current] = "complete";
    if (step.id === "publish") state.posted = true;
    addActivity(`${step.title}: completed in preview.`);
    render();
  }
  state.phase = "complete";
  render();
  message(
    "assistant",
    state.posted
      ? "The preview flow is complete. Setup was reused, and only the reviewed draft was published in simulation."
      : "Selected access is ready. Posting was left out, so I did not request a draft approval.",
  );
}
async function recoverStep() {
  if (state.phase !== "failed") return;
  if (state.failure === "setup") {
    state.statuses[state.current] = "pending";
    state.current -= 1;
    addActivity(
      "Retrying only failed tool setup under existing approval.",
      "retry_setup",
    );
    advance();
    return;
  }
  const token = generation;
  state.phase = "running";
  render();
  $("current-description").textContent =
    "Checking whether the approved post already exists. No new post is being submitted.";
  addActivity(
    "Checking the uncertain provider outcome.",
    "reconcile_provider_result",
  );
  await new Promise((resolve) => setTimeout(resolve, 1200));
  if (token !== generation) return;
  state.posted = true;
  state.publicationUncertain = false;
  state.statuses[state.current] = "complete";
  state.phase = "complete";
  addActivity(
    "Provider confirmed the existing post. No duplicate was created in this simulation.",
    "provider_result_confirmed",
  );
  render();
}
function downloadLogs() {
  // Export only structured fixture diagnostics. Never collect DOM text or chat.
  const diagnostic = {
    schema_version: 1,
    design_preview: true,
    workflow_id: "preview-x-access",
    plan_revision: "example-1",
    exported_at: new Date().toISOString(),
    phase: state.phase,
    publication_outcome: state.publicationUncertain
      ? "unknown"
      : state.posted
        ? "confirmed"
        : "not_published",
    steps: steps.map((step, index) => ({
      id: step.id,
      status: state.statuses[index],
    })),
    events: diagnosticEvents,
    excluded: [
      "credentials",
      "bearer links",
      "post content",
      "conversation text",
    ],
  };
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(diagnostic, null, 2)], {
      type: "application/json",
    }),
  );
  const link = el("a");
  link.href = url;
  link.download = "workflow-diagnostics-preview.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function answerQuestion(question) {
  message("user", question);
  if (/approved/i.test(question))
    message(
      "assistant",
      state.approved
        ? `You approved ${[...state.selected].map((x) => (x === "check" ? "account checks" : "posting setup with review before each post")).join(" and ")} for this agent, plus the required restart. ${state.publicationUncertain ? "The provider's publication result is unknown. The post may already exist." : state.posted ? "The exact draft was also approved and published in simulation." : state.postApproved ? "This exact draft is approved for publication; it has not been published yet." : "No post has been approved or published."}`
        : "No new access has been approved. Starting the workflow, asking questions, or opening a dialog does not grant access.",
    );
  else if (/why|explain|needed/i.test(question))
    message(
      "assistant",
      stepExplanation(Math.max(0, Math.min(state.current, steps.length - 1))),
    );
  else
    message(
      "assistant",
      "This preview records your question without changing approval. In the live workflow, the team lead would answer here or prepare a revised draft. Use the review controls when you are ready to decide.",
    );
}
$("start-flow").addEventListener("click", () => {
  if (state.phase === "idle") {
    addActivity("You started the workflow.");
    advance();
  }
});
$("stop-flow").addEventListener("click", () =>
  stop("You stopped the remaining workflow."),
);
$("open-step").addEventListener("click", showReview);
$("close-dialog").addEventListener("click", () => $("step-dialog").close());
$("step-dialog").addEventListener("close", () => focusBeforeDialog?.focus());
$("dialog-fields").addEventListener("change", (event) => {
  const id = event.target.dataset.operation;
  if (id) {
    event.target.checked ? state.selected.add(id) : state.selected.delete(id);
    $("approve-step").disabled = !state.selected.size;
    if ($("include-post")) {
      $("include-post").disabled = !state.selected.has("post");
      if (!state.selected.has("post")) {
        $("include-post").checked = false;
        state.publishNow = false;
      }
    }
  }
  if (event.target.id === "include-post")
    state.publishNow = event.target.checked;
});
$("approve-step").addEventListener("click", () => {
  if (state.phase !== "waiting" || $("approve-step").disabled) return;
  const access = state.current === 0;
  $("step-dialog").close();
  state.statuses[state.current] = "complete";
  if (access) {
    state.approved = true;
    state.postApproved =
      state.draftReady && state.publishNow && state.selected.has("post");
  } else state.postApproved = true;
  addActivity(
    access
      ? state.postApproved
        ? "You approved selected access, setup, and the exact post together."
        : "You approved selected access and required setup."
      : "You approved the exact draft for publication.",
    "user_decision_recorded",
  );
  advance();
});
$("reject-step").addEventListener("click", () =>
  stop(
    state.current === 0
      ? "You rejected access."
      : "You declined publication. Completed setup is retained.",
  ),
);
$("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (question) {
    answerQuestion(question);
    $("question").value = "";
  }
});
document.addEventListener("click", (event) => {
  const quick = event.target.closest("[data-question]");
  if (quick) answerQuestion(quick.dataset.question);
  const info = event.target.closest("[data-step-info]");
  if (info) {
    const index = Number(info.dataset.stepInfo),
      step = steps[index];
    $("step-info-heading").textContent = step.title;
    $("step-info-description").textContent = stepExplanation(index);
    $("step-info").showModal();
  }
});
function stepExplanation(index) {
  if (state.draftReady && steps[index].id === "draft")
    return "The exact draft is already prepared. You can review it with setup, and the system reuses that unchanged draft afterward.";
  if (state.draftReady && steps[index].id === "review")
    return "Include this exact draft in your initial approval to publish without a second prompt. If you leave publication out, the system will not ask for it again in this workflow.";
  return steps[index].info;
}
$("close-step-info").addEventListener("click", () => $("step-info").close());
for (const dialog of document.querySelectorAll("dialog"))
  dialog.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const controls = [
      ...dialog.querySelectorAll("button:not(:disabled),input:not(:disabled)"),
    ];
    const index = controls.indexOf(document.activeElement);
    if (event.shiftKey && index <= 0) {
      event.preventDefault();
      controls.at(-1).focus();
    } else if (!event.shiftKey && index === controls.length - 1) {
      event.preventDefault();
      controls[0].focus();
    }
  });
$("theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme === "dark";
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  $("theme").textContent = dark ? "Dark theme" : "Light theme";
});
$("reset").addEventListener("click", reset);
$("draft-mode").addEventListener("change", reset);
$("failure-mode").addEventListener("change", reset);
$("recover-step").addEventListener("click", recoverStep);
$("download-logs").addEventListener("click", downloadLogs);
$("failure-logs").addEventListener("click", downloadLogs);
reset();
