/**
 * Shared dependency-map renderer for access workflows (chat card and /workflows/<id>).
 *
 * Each step shows the agent-reported state and the access requests linked to
 * it, with their current status from the access store. A waiting request links
 * to its Inbox review. Tool setup pages (sign-ins, account links, the team's
 * sign-in app) show on their steps with the gateway's plain status line; they
 * update when the person finishes a page. All text is set with `textContent`;
 * the pure helpers are exported for node tests.
 */
const records = new WeakMap();

export const STEP_STATE_LABELS = Object.freeze({
  pending: "Upcoming",
  running: "Agent working",
  waiting: "Waiting for a decision",
  complete: "Agent reported complete",
  omitted: "Left out",
  failed: "Failed",
  outcome_unknown: "Result uncertain",
});
export const BLOCKED_LABEL = "Denied: it will not be asked for again";
export const OPEN_REVIEW_LABEL = "Open in Inbox";

const REQUEST_OUTCOME_LABELS = Object.freeze({
  pending: "Waiting for a decision",
  approved: "Approved",
  applied: "Approved and applied",
  apply_failed: "Approved, but applying it failed",
  denied: "Denied",
  withdrawn: "Withdrawn: the task ended first",
  unavailable: "No longer available",
});

function text(value) {
  return value === undefined || value === null ? "" : String(value).trim();
}

/** The status shown for one linked request. An approved change that is not applied yet says so. */
export function requestStatusLabel(request) {
  const outcome = text(request && request.outcome);
  if (outcome === "approved" && text(request.kind) === "change") return "Approved, not applied yet";
  return REQUEST_OUTCOME_LABELS[outcome] || REQUEST_OUTCOME_LABELS.unavailable;
}

/** One line per request: what it is, who asked (for a worker's request) and its status. */
export function requestLineModel(request) {
  const title = text(request && request.title) || "Access request";
  const from = request && request.from_worker && text(request.agent_type) ? `${text(request.agent_type)}: ` : "";
  const reviewUrl = text(request && request.review_url);
  return {
    id: text(request && request.request_id),
    label: `${from}${title}`,
    status: requestStatusLabel(request),
    outcome: text(request && request.outcome) || "unavailable",
    // Only a waiting request links to its review, and only within this site.
    reviewUrl: text(request && request.outcome) === "pending" && isSameOriginPath(reviewUrl) ? reviewUrl : "",
  };
}

function isSameOriginPath(value) {
  const candidate = text(value);
  if (!candidate) return false;
  if (candidate.startsWith("/") && !candidate.startsWith("//")) return true;
  try {
    const origin = globalThis.location ? globalThis.location.origin : "";
    return Boolean(origin) && new URL(candidate).origin === origin;
  } catch {
    return false;
  }
}

/** The step's status line: blocked first, then the request summary, then the reported state. */
export function stepStatusText(step) {
  if (step && step.blocked_by_person) return BLOCKED_LABEL;
  return text(step && step.review_status) || STEP_STATE_LABELS[text(step && step.state)] || text(step && step.state);
}

function element(tag, value, className = "") {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
}

// Layout follows dependency depth, so additional branches need no provider-specific UI.
export function graphColumns(steps) {
  const index = new Map(steps.map((step) => [step.step_id, step]));
  const depth = new Map();
  const visit = (id) => {
    if (!depth.has(id)) depth.set(id, Math.max(-1, ...index.get(id).depends_on.map(visit)) + 1);
    return depth.get(id);
  };
  const columns = [];
  steps.forEach((step) => { const level = visit(step.step_id); (columns[level] ||= []).push(step); });
  return columns;
}

export function drawConnections(map) {
  const record = records.get(map);
  if (!record || !map.isConnected) return;
  map.querySelector("svg")?.remove();
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("workflow-connections");
  svg.setAttribute("width", String(map.scrollWidth));
  svg.setAttribute("height", String(map.scrollHeight));
  svg.setAttribute("aria-hidden", "true");
  const rect = map.getBoundingClientRect();
  record.steps.forEach((step) => {
    const to = map.querySelector(`[data-step-id="${step.step_id}"]`).getBoundingClientRect();
    step.depends_on.forEach((id) => {
      const from = map.querySelector(`[data-step-id="${id}"]`).getBoundingClientRect();
      const x1 = from.right - rect.left + map.scrollLeft, y1 = from.top + from.height / 2 - rect.top;
      const x2 = to.left - rect.left + map.scrollLeft, y2 = to.top + to.height / 2 - rect.top;
      const mid = (x1 + x2) / 2;
      const path = document.createElementNS(svg.namespaceURI, "path");
      path.setAttribute("d", `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2} m-6,-4 l6,4 l-6,4`);
      svg.append(path);
    });
  });
  map.prepend(svg);
}

// Setup page statuses, shown with the request row styles.
const SETUP_ROW_STYLES = Object.freeze({
  waiting: "pending",
  completed: "approved",
  declined: "denied",
  expired: "pending",
  unavailable: "apply_failed",
});

export const MARK_DONE_LABEL = "Mark Done";

/** One line per setup page: what it is, its plain status (from trusted code), and whether it can be marked done. */
export function setupLineModel(setup) {
  const status = text(setup && setup.status) || "unavailable";
  return {
    id: text(setup && setup.setup_request_id),
    label: text(setup && setup.title) || "Setup page",
    status: text(setup && setup.status_text) || "Status not known",
    style: SETUP_ROW_STYLES[status] || "apply_failed",
    // A step done outside Universal Agents waits for the person to mark it done.
    canMarkDone: text(setup && setup.kind) === "external_step" && status === "waiting",
  };
}

/** The setup pages of one step (or those not on a step), as a list; actions.markDone adds Mark Done. */
export function renderSetupList(setups, actions = {}) {
  const list = element("ul", "", "workflow-requests workflow-setups");
  (Array.isArray(setups) ? setups : []).forEach((setup) => {
    const model = setupLineModel(setup);
    const row = element("li", "", `workflow-request is-${model.style}`);
    row.dataset.setupRequestId = model.id;
    row.append(element("span", model.label, "workflow-request-title"), element("span", model.status, "workflow-request-status"));
    if (model.canMarkDone && typeof actions.markDone === "function") {
      const done = element("button", MARK_DONE_LABEL, "button workflow-request-review");
      done.type = "button";
      done.addEventListener("click", () => actions.markDone(model.id, done));
      row.append(done);
    }
    list.append(row);
  });
  return list;
}

/** The linked requests of one step (or the requests not on a step), as a list. */
export function renderRequestList(requests) {
  const list = element("ul", "", "workflow-requests");
  (Array.isArray(requests) ? requests : []).forEach((request) => {
    const model = requestLineModel(request);
    const row = element("li", "", `workflow-request is-${model.outcome}`);
    row.dataset.requestId = model.id;
    row.append(element("span", model.label, "workflow-request-title"), element("span", model.status, "workflow-request-status"));
    if (model.reviewUrl) {
      const link = element("a", OPEN_REVIEW_LABEL, "workflow-request-review");
      link.href = model.reviewUrl;
      row.append(link);
    }
    list.append(row);
  });
  return list;
}

export function renderWorkflowGraph(map, record, info, actions = {}) {
  records.set(map, record);
  map.replaceChildren();
  const columns = graphColumns(record.steps);
  map.classList.toggle("is-long", columns.length > 4);
  columns.forEach((steps, index) => {
    const column = element("div", "", "workflow-column");
    column.append(element("h3", `${index + 1} · ${[...new Set(steps.map((step) => step.kind))].join(" / ")}`, "workflow-column-heading"));
    steps.forEach((step) => {
      const node = element("article", "", "workflow-node");
      node.dataset.stepId = step.step_id;
      node.dataset.state = step.blocked_by_person ? "blocked" : step.state;
      const details = element("button", "i", "workflow-info");
      details.type = "button";
      details.setAttribute("aria-label", `Why ${step.title}`);
      details.addEventListener("click", () => info(step.title, `${step.reason}\n\nDone when: ${step.success_criterion}`));
      const state = step.blocked_by_person ? "blocked" : step.state;
      node.append(element("h3", step.title), details, element("p", step.reason), element("span", stepStatusText(step), `workflow-state ${state}`));
      if (step.summary) node.append(element("p", step.summary));
      if (step.recovery) node.append(element("p", `Next: ${step.recovery}`));
      if (Array.isArray(step.requests) && step.requests.length) node.append(renderRequestList(step.requests));
      if (Array.isArray(step.setups) && step.setups.length) node.append(renderSetupList(step.setups, actions));
      if (step.depends_on.length) node.append(element("p", `Needs: ${step.depends_on.map((id) => record.steps.find((item) => item.step_id === id).title).join(", ")}`, "workflow-prerequisites"));
      if (step.optional) node.append(element("p", "Optional"));
      column.append(node);
    });
    map.append(column);
  });
  requestAnimationFrame(() => drawConnections(map));
}

if (typeof window !== "undefined") {
  window.addEventListener("resize", () => document.querySelectorAll(".workflow-map").forEach(drawConnections));
}
