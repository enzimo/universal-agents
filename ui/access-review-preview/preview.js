/* Design fixture only. No authority, persistence, network calls, or live accounts. */
"use strict";
const $ = (id) => document.getElementById(id);
const proposal = {
  outcome: "Set up X account access for the communications workflow",
  target_agent_type: "AcmeCommunicationsAgent",
  steps: [
    { id: "account", kind: "credential_selection", provider: "xurl" },
    {
      id: "access",
      kind: "capability_access",
      capability_id: "builtin:x_posting",
      operations: ["auth.status", "post.create"],
      requires: ["account"],
    },
  ],
  requested_scope: "agent",
  requested_duration: "until_removed",
};
// This is the shape of a projected review, not an agent-authored executable graph.
// Production validation and execution are specified in unified_access_workflow.md.
const review = {
  beneficiary: "AcmeCommunicationsAgent",
  requester: "TeamArchitect",
  nodes: [
    {
      id: "account",
      slot: "account",
      kind: "account",
      title: "X account",
      description:
        "Use an account you choose. Passwords and tokens stay protected.",
    },
    {
      id: "check",
      slot: "capability",
      kind: "choice",
      title: "Check account",
      description:
        "Check which X account is connected and whether it is signed in.",
      operation: "auth.status",
      requested: true,
      requires: ["account"],
    },
    {
      id: "post",
      slot: "capability",
      kind: "choice",
      title: "Publish text posts",
      description: "Publish new text posts to the selected X account.",
      operation: "post.create",
      requested: true,
      requires: ["account"],
    },
    {
      id: "enable",
      slot: "setup",
      kind: "required",
      title: "Enable access",
      description:
        "Make the selected tools available. Restart all agents to apply the change; running work may be interrupted.",
      requires_selected: ["check", "post"],
    },
    {
      id: "ready",
      slot: "setup",
      kind: "result",
      title: "Ready for work",
      description: "Keep this setup for future requests.",
      requires: ["enable"],
    },
  ],
  edges: [
    ["account", "check"],
    ["account", "post"],
    ["check", "enable"],
    ["post", "enable"],
    ["enable", "ready"],
  ],
};
const help = {
  plan: [
    "About this plan",
    [
      "Why this is needed",
      "The agent has asked for X access. The system puts account selection, capabilities, and required setup into this one review.",
    ],
    [
      "What the arrows mean",
      "Each connected step needs the step before it. The two capability branches are independent choices; both need an account.",
    ],
    [
      "Your decision",
      "Approve the selected access once. Completed steps stay complete if remaining setup needs to resume.",
    ],
  ],
  account: [
    "Connect an X account",
    [
      "Why this is needed",
      "X actions need an account. An existing matching connection can be reused without signing in again.",
    ],
    [
      "What it allows",
      "Only the selected actions, for the recipient and duration shown. Choosing an account alone does not grant access.",
    ],
    [
      "Credentials stay protected",
      "The credential broker keeps passwords and tokens. The agent receives a reference that the broker checks whenever it is used.",
    ],
  ],
  check: [
    "Check account",
    [
      "What it allows",
      "Read the connected account identity and sign-in status. This does not read your timeline or publish anything.",
    ],
    [
      "What it needs",
      "A valid connection to the selected account. Posting is not a prerequisite.",
    ],
    [
      "If you leave this out",
      "This request will not authorize account-status checks.",
    ],
  ],
  post: [
    "Publish text posts",
    [
      "What it allows",
      "Create new text posts on the selected account. This does not include deleting posts, reading your timeline, or arbitrary CLI commands.",
    ],
    [
      "Choose how future posts work",
      "Ask before each post reviews new content without repeating setup. Allow without asking again permits future text posts within the account, recipient, and duration shown.",
    ],
    [
      "What it needs",
      "A valid account connection and the selected posting permission. No post is queued in this example.",
    ],
  ],
  enable: [
    "Enable selected access",
    [
      "Why this is needed",
      "The agent needs the selected tools installed in its available tool set, with access limited to your choices.",
    ],
    [
      "What happens after approval",
      "The system connects the selected permissions, applies the tool change, and restarts all agents because this registered tool requires it. Running work may be interrupted.",
    ],
    [
      "If setup is interrupted",
      "The system keeps completed steps and resumes the remaining work under valid approval. A changed effect needs a review of that change.",
    ],
  ],
  ready: [
    "Ready for work",
    [
      "What this means",
      "The selected setup has finished. Future tasks can reuse access for its approved duration.",
    ],
    [
      "Future actions",
      "Your posting rule still applies. This setup request contains no post to publish.",
    ],
    [
      "Change your mind",
      "The planned Permissions view lets you edit or remove ongoing access. Removal stops future uses; it cannot undo a post already published.",
    ],
  ],
  duration: [
    "Access duration",
    [
      "Until removed",
      "The selected account access and capabilities persist across tasks and restarts until you edit or remove them.",
    ],
    [
      "Other choices",
      "For 30 days expires the selected access after 30 days. For this task ends it when the task finishes.",
    ],
    [
      "Sign-in may still expire",
      "An account provider can revoke a login or require sign-in again. The short lifetime of a review form is separate from access duration.",
    ],
  ],
  future: [
    "Future posting permission",
    [
      "Ask before each post",
      "Keep account setup ready, then review each new post's content. You will not be asked to connect the account or enable the same tools again.",
    ],
    [
      "Allow without asking again",
      "Explicitly permit this recipient to publish future text posts on this account for the selected duration. Production must check these limits on every use and offer this choice only when policy permits.",
    ],
    [
      "A post already reviewed",
      "If a specific post is included and approved with setup, the runtime can publish that unchanged post without a second approval.",
    ],
  ],
};
let state;
let runSequence = 0;
let infoTrigger;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}
function makeNode(spec) {
  const node = element(
    "section",
    "node" + (spec.kind === "result" ? " ready-node" : ""),
  );
  node.id = "node-" + spec.id;
  const header = element("div", "node-header");
  if (spec.kind === "choice") {
    const label = element("label");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.id = "choice-" + spec.id;
    checkbox.dataset.choice = spec.id;
    label.append(checkbox, element("span", "node-title", spec.title));
    header.append(label);
  } else header.append(element("strong", "node-title", spec.title));
  const info = element("button", "info", "i");
  info.type = "button";
  info.dataset.info = spec.id;
  info.setAttribute("aria-label", "About " + spec.title.toLowerCase());
  header.append(info);
  node.append(header, element("p", "", spec.description));
  if (spec.kind === "account") {
    const select = element("select");
    select.id = "account";
    select.setAttribute("aria-label", "Select X account");
    for (const [value, text] of [
      ["", "Choose an account"],
      ["team", "@acme · Team account"],
      ["private", "@jordan · Personal account"],
    ]) {
      const option = element("option", "", text);
      option.value = value;
      select.append(option);
    }
    node.append(select);
  }
  if (spec.requires)
    node.append(
      element(
        "p",
        "dependency",
        spec.id === "ready"
          ? "Requires: completed setup"
          : "Requires: an X account",
      ),
    );
  if (spec.requires_selected)
    node.append(
      element(
        "p",
        "dependency",
        "Requires: an account and at least one selected capability",
      ),
    );
  node.append(element("span", "state"));
  $(spec.slot + "-slot").append(node);
}

function reset() {
  runSequence += 1;
  const scenario = $("scenario").value;
  state = {
    scenario,
    account: scenario === "new" ? "" : "team",
    selected: new Set(["check", "post"]),
    phase: scenario === "interrupted" ? "interrupted" : "review",
    step: "",
    scope: "agent",
    duration: "persistent",
    posting: "ask",
  };
  $("account").value = state.account;
  $("scope").value = state.scope;
  $("duration").value = state.duration;
  $("posting-rule").value = state.posting;
  update();
}
function nodeState(id, text, done = false) {
  const status = $("node-" + id).querySelector(".state");
  status.textContent = (done ? "✓ " : "") + text;
  status.dataset.state = done ? "done" : "pending";
}
function accountName() {
  return state.account === "private" ? "@jordan" : "@acme";
}
function hasReusableAccount() {
  return state.account === "team" && state.scenario !== "new";
}
function lifetime() {
  return {
    persistent: "until removed",
    month: "for 30 days",
    task: "for this task",
  }[state.duration];
}
function update() {
  $("recipient-name").textContent =
    state.scope === "team" ? "The Acme team" : review.beneficiary;
  const editable = state.phase === "review";
  const completed = state.phase === "complete";
  const selectedCount = state.selected.size;
  for (const spec of review.nodes.filter((n) => n.kind === "choice")) {
    const selected = state.selected.has(spec.id);
    $("choice-" + spec.id).checked = selected;
    $("node-" + spec.id).classList.toggle("selected", selected);
    $("node-" + spec.id).classList.toggle("omitted", !selected);
    nodeState(
      spec.id,
      !selected
        ? "Not selected"
        : state.phase === "rejected"
          ? "Not applied"
          : completed
            ? "Allowed"
            : state.phase === "review"
              ? "Selected"
              : "Approved",
      selected && completed,
    );
  }
  const connected =
    hasReusableAccount() ||
    (Boolean(state.account) && (state.step !== "" || completed));
  nodeState(
    "account",
    hasReusableAccount()
      ? "Connected · reused"
      : connected
        ? "Connected"
        : state.account
          ? "Ready to connect"
          : "Needs your choice",
    connected,
  );
  nodeState(
    "enable",
    completed
      ? "Applied"
      : state.phase === "interrupted"
        ? "Interrupted · approval kept"
        : state.step === "enable"
          ? "Applying selected access…"
          : state.phase === "rejected"
            ? "Not applied"
            : "Included in approval",
    completed,
  );
  nodeState(
    "ready",
    state.phase === "rejected"
      ? "Request closed"
      : completed
        ? "Setup complete"
        : state.step === "restart"
          ? "Restarting agents…"
          : "After setup completes",
    completed,
  );
  $("scope").querySelector('[value="team"]').disabled =
    state.account === "private";
  $("scope-help").textContent =
    state.account === "private"
      ? "Personal credentials cannot be shared with the team here."
      : state.scope === "team"
        ? "Shared access applies to current team members, including members added later."
        : "Other agents do not receive access.";
  $("posting-field").hidden = !state.selected.has("post");
  $("posting-help").textContent =
    state.posting === "ask"
      ? "You review new post content. Account setup is reused."
      : "Future text posts can be published without another approval.";
  $("wide-access").hidden =
    !state.selected.has("post") || state.posting !== "allow";
  $("wide-access").textContent =
    `Permits future text posts to ${state.account ? accountName() : "the chosen account"} by ${state.scope === "team" ? "team members" : review.beneficiary} ${lifetime()}.`;
  $("selection-count").textContent =
    `${selectedCount} of 2 capabilities selected`;
  const effects = [];
  if (state.account && selectedCount)
    effects.push(
      `${hasReusableAccount() ? "Reuse" : "Connect"} ${accountName()} for ${state.scope === "team" ? "the whole team" : "this agent"} ${lifetime()}.`,
    );
  if (state.selected.has("check"))
    effects.push("Check the connected account and sign-in status.");
  if (state.selected.has("post"))
    effects.push(
      state.posting === "ask"
        ? "Enable posting; ask before each new post."
        : "Allow future text posts without asking again.",
    );
  if (selectedCount)
    effects.push("Enable selected tools and restart all agents.");
  $("effects").replaceChildren(
    ...effects.map((text) => element("li", "", text)),
  );
  for (const input of document.querySelectorAll(".review select,.review input"))
    input.disabled = !editable;
  $("approve").disabled = editable
    ? !state.account || !selectedCount
    : state.phase !== "interrupted";
  $("reject").disabled = !editable && state.phase !== "interrupted";
  const phaseCopy = {
    review: [
      "Needs your review",
      "Approve selected",
      "Approval will allow",
      "Your choices have not been submitted.",
    ],
    interrupted: [
      "Setup interrupted",
      "Resume setup",
      "Already approved",
      "Account connected. Tool setup needs to resume. No new approval needed in this example.",
    ],
    applying: [
      "Applying your decision",
      "Applying…",
      "Your approved access",
      "Previewing setup progress. No live changes are made.",
    ],
    complete: [
      "Access ready",
      "Access ready",
      "Selected access",
      "Preview complete. In the product, this receipt links to your access in Permissions.",
    ],
    rejected: [
      "Request rejected",
      "Request closed",
      "Not applied by this request",
      "Request closed in this preview. Existing account access remains unchanged.",
    ],
  }[state.phase];
  $("workflow-status").textContent = phaseCopy[0];
  $("approve").textContent = phaseCopy[1];
  $("summary-label").textContent = phaseCopy[2];
  $("receipt").textContent = phaseCopy[3];
  $("approve").setAttribute("aria-busy", String(state.phase === "applying"));
  $("reject").textContent =
    state.phase === "interrupted" ? "Cancel remaining setup" : "Reject request";
  $("decision-help").textContent =
    state.phase === "interrupted"
      ? "Resume the remaining step under the existing valid approval."
      : !selectedCount
        ? "Select at least one capability, or reject the request."
        : !state.account
          ? "Choose an account before approving."
          : "One decision covers the selected access and required setup.";
  $("flow-note").textContent =
    state.phase === "rejected"
      ? "This request is closed. Completed connections have not been removed."
      : completed
        ? "Selected access is ready. The posting rule you chose still applies."
        : state.phase === "interrupted"
          ? "The connection succeeded. Tool setup was interrupted; resume that step without reconnecting."
          : state.phase === "applying"
            ? "The system is applying the selected plan, keeping a checkpoint after each step."
            : !selectedCount
              ? "Both capabilities are excluded. No new access will be granted."
              : hasReusableAccount()
                ? "The existing account connection will be reused. You do not need to sign in again."
                : "Choose an account here, then approve the selected setup in one decision.";
  requestAnimationFrame(drawEdges);
}

function drawEdges() {
  const bounds = $("map").getBoundingClientRect();
  const paths = review.edges.map(([from, to]) => {
    const a = $("node-" + from).getBoundingClientRect(),
      b = $("node-" + to).getBoundingClientRect();
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const omitted = [from, to].some(
      (id) => ["check", "post"].includes(id) && !state.selected.has(id),
    );
    path.setAttribute("class", "edge" + (omitted ? " omitted" : ""));
    path.setAttribute("marker-end", "url(#arrow)");
    if (from === "enable") {
      const x = a.x + a.width / 2 - bounds.x;
      path.setAttribute(
        "d",
        `M${x} ${a.bottom - bounds.y} V${b.top - bounds.y - 3}`,
      );
    } else {
      const x1 = a.right - bounds.x,
        x2 = b.left - bounds.x - 3;
      const y1 = a.y + a.height / 2 - bounds.y,
        y2 = b.y + b.height / 2 - bounds.y;
      const middle = (x1 + x2) / 2;
      path.setAttribute(
        "d",
        `M${x1} ${y1} C${middle} ${y1},${middle} ${y2},${x2} ${y2}`,
      );
    }
    return path;
  });
  $("edges").replaceChildren(...paths);
}

function openInfo(button) {
  const [title, ...sections] = help[button.dataset.info];
  infoTrigger = button;
  $("info-heading").textContent = title;
  $("info-content").replaceChildren(
    ...sections.map(([heading, text]) => {
      const section = element("section", "popup-section");
      section.append(element("h3", "", heading), element("p", "", text));
      return section;
    }),
  );
  const dialog = $("info-dialog");
  dialog.showModal();
  const anchor = button.getBoundingClientRect();
  const box = dialog.getBoundingClientRect();
  dialog.style.left = `${Math.max(16, Math.min(anchor.right - box.width, innerWidth - box.width - 16))}px`;
  dialog.style.top = `${Math.max(16, Math.min(anchor.bottom + 9, innerHeight - box.height - 16))}px`;
  $("close-info").focus();
}
async function applySelection() {
  if ($("approve").disabled) return;
  const thisRun = ++runSequence;
  state.phase = "applying";
  update();
  for (const step of ["account", "enable", "restart"]) {
    await new Promise((resolve) => setTimeout(resolve, 650));
    if (thisRun !== runSequence) return;
    state.step = step;
    update();
  }
  state.phase = "complete";
  update();
}
review.nodes.forEach(makeNode);
$("proposal-json").textContent = JSON.stringify(proposal, null, 2);
$("review-json").textContent = JSON.stringify(
  {
    beneficiary: review.beneficiary,
    requester: review.requester,
    operations: ["auth.status", "post.create"],
    existing_connection: "reusable",
    required_setup: ["capability attachment", "all-agent restart"],
    edges: review.edges,
    decision: "one exact selected plan",
  },
  null,
  2,
);
document.addEventListener("click", (event) => {
  const info = event.target.closest("[data-info]");
  if (info) openInfo(info);
});
$("close-info").addEventListener("click", () => $("info-dialog").close());
$("info-dialog").addEventListener("close", () => infoTrigger?.focus());
// Keep Tab within the popup even when its close button is the only focus target.
$("info-dialog").addEventListener("keydown", (event) => {
  if (event.key === "Tab") {
    event.preventDefault();
    $("close-info").focus();
  }
});
document.querySelector(".review").addEventListener("change", (event) => {
  if (state.phase !== "review") return;
  const input = event.target;
  if (input.dataset.choice)
    input.checked
      ? state.selected.add(input.dataset.choice)
      : state.selected.delete(input.dataset.choice);
  if (input.id === "account") {
    state.account = input.value;
    if (state.account === "private" && state.scope === "team") {
      state.scope = "agent";
      $("scope").value = "agent";
    }
  }
  if (input.id === "scope") state.scope = input.value;
  if (input.id === "duration") state.duration = input.value;
  if (input.id === "posting-rule") state.posting = input.value;
  update();
});
$("approve").addEventListener("click", applySelection);
$("reject").addEventListener("click", () => {
  if (!$("reject").disabled) {
    state.phase = "rejected";
    update();
  }
});
$("scenario").addEventListener("change", reset);
$("reset").addEventListener("click", reset);
$("theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme === "dark";
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  $("theme").textContent = dark ? "Dark theme" : "Light theme";
});
new ResizeObserver(drawEdges).observe($("map"));
window.addEventListener("resize", () => {
  if ($("info-dialog").open) $("info-dialog").close();
});
reset();
