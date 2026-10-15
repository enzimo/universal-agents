/**
 * The workflow page (/workflows and /workflows/<id>): follow an agent's access
 * workflow and decide its waiting requests.
 *
 * Every request's status comes from the access service. **Review Ready
 * Requests** shows the same review cards as the Inbox (`access_inbox.mjs`); a
 * choice, or **Approve All** / **Deny All**, is sent to the workflow page API,
 * which decides through the access Inbox. The pure helpers are exported for
 * node tests; the page starts only in a browser.
 */
import { renderRequestList, renderSetupList, renderWorkflowGraph } from "./workflow_graph.mjs";
import { decisionOutcome, renderDecidedCard, renderReviewCard, setCardMessage } from "./access_inbox.mjs";

export const WORKFLOWS_API = "/api/web-chat/workflows";
export const CHOICE_APPROVE = "approve";
export const CHOICE_DENY = "deny";

function text(value) {
  return value === undefined || value === null ? "" : String(value).trim();
}

/** Decisions for every review the person can decide that offers `choice`. */
export function bulkDecisions(reviews, choice) {
  return (Array.isArray(reviews) ? reviews : [])
    .filter((review) => review && review.can_decide === true
      && Array.isArray(review.choices) && review.choices.some((item) => item && item.choice === choice))
    .map((review) => ({ request_id: text(review.request_id), choice }));
}

/** The status line under the review buttons. */
export function reviewStatusText(payload) {
  if (payload && payload.task_status) return "The source task has ended. Open Chat to go over any remaining work.";
  const waiting = Number(payload && payload.waiting_request_count) || 0;
  return waiting
    ? `${waiting} request(s) wait for a decision. Review them together here, or one by one in the Inbox.`
    : "No request waits for a decision now. Upcoming steps stay visible above.";
}

/** The message after a batch: every saved decision, and any that were not saved. */
export function batchResultText(result) {
  const results = Array.isArray(result && result.results) ? result.results : [];
  const saved = results.filter((item) => item && item.saved).length;
  if (result && result.all_saved) return `${saved} decision(s) saved. The agent will be told.`;
  return `${saved} of ${results.length} decisions saved. Refresh to see the others; one may have been decided elsewhere.`;
}

function startWorkflowPage() {
  const byId = (id) => document.getElementById(id);
  const workflowId = decodeURIComponent(location.pathname.split("/")[2] || "");
  const apiRoot = `${WORKFLOWS_API}/${encodeURIComponent(workflowId)}`;
  let csrf = "", reviews = [], busy = false;
  let timer = null, controller = new AbortController(), active = true, eventOffset = 0;
  let lastVersion = "", loadGeneration = 0, listOffset = 0, includeTerminal = false;

  function element(tag, value, className = "") {
    const node = document.createElement(tag);
    node.textContent = value;
    if (className) node.className = className;
    return node;
  }

  async function request(path, payload) {
    const response = await fetch(path, {
      method: payload === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
      signal: controller.signal,
      headers: payload === undefined ? {} : { "Content-Type": "application/json", "X-UA-Permission-CSRF": csrf },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    if (response.status === 401 || response.status === 403) {
      lock();
      throw new Error("Your session ended. Sign in through Chat, then reopen this workflow.");
    }
    const body = await response.json().catch(() => null);
    if (!response.ok || body?.status === "error" || body?.status === "refused") {
      throw new Error(body?.error || body?.message || "The gateway could not complete this request. Refresh before trying again.");
    }
    return body;
  }

  function lock() {
    active = false;
    clearTimeout(timer);
    controller.abort();
    document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
    byId("workflow-detail").hidden = true;
    byId("workflow-list").replaceChildren();
    byId("workflow-map").replaceChildren();
    byId("workflow-credentials").replaceChildren();
    reviews = [];
    csrf = "";
  }

  function info(title, description) {
    byId("workflow-info-title").textContent = title;
    byId("workflow-info-text").textContent = description;
    byId("workflow-info-dialog").showModal();
  }

  function renderCredentials(actions) {
    const nodes = actions.map((action) => {
      const row = element("section", "", "workflow-panel");
      const url = new URL(action.url, location.origin);
      if (!["https:", "http:"].includes(url.protocol)) return element("p", "Account link form unavailable.");
      const link = element("a", action.label, "button primary");
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      row.append(element("h2", "Choose Your Account"), element("p", action.description), link,
        element("p", "Complete the secure form, then return here. Linking an account gives no access by itself."));
      return row;
    });
    byId("workflow-credentials").replaceChildren(...nodes);
  }

  function renderEvents(events, total) {
    const nodes = events.map((event) => {
      const row = element("li", "");
      row.append(element("span", event.message || event.code), element("time", new Date(event.created_at_utc).toLocaleString()));
      return row;
    });
    byId("workflow-events").replaceChildren(...nodes);
    byId("workflow-events-pages").replaceChildren(window.UAPagination.create({
      offset: eventOffset, total, onPage: (offset) => { eventOffset = offset; return refresh(); },
    }));
  }

  function renderUnassigned(items, setups) {
    byId("workflow-unassigned").hidden = !items.length && !setups.length;
    byId("workflow-unassigned-list").replaceChildren(renderRequestList(items), renderSetupList(setups, setupActions));
  }

  // A setup step done outside Universal Agents: the person marks it done here.
  const setupActions = {
    async markDone(setupRequestId, button) {
      if (busy) return;
      busy = true;
      button.disabled = true;
      try {
        await request(`${apiRoot}/setup_done`, { setup_request_id: setupRequestId });
        button.textContent = "Marked Done";
        lastVersion = "";
        await refresh();
      } catch (error) {
        button.disabled = false;
        byId("workflow-connection").textContent = error.message;
        byId("workflow-connection").classList.add("workflow-error");
      } finally {
        busy = false;
      }
    },
  };

  async function refresh() {
    if (!active) return;
    clearTimeout(timer);
    const generation = ++loadGeneration;
    try {
      if (!workflowId) { await loadList(); return; }
      const payload = await request(`${apiRoot}?event_offset=${eventOffset}`);
      if (!active || generation !== loadGeneration) return;
      const record = payload.workflow;
      byId("workflow-title").textContent = record.title;
      byId("workflow-goal").textContent = record.goal;
      byId("workflow-beneficiary").textContent = `For ${record.target_agent_type} · Planned by ${record.requester_agent_type}`;
      byId("workflow-task-status").hidden = !payload.task_status?.recovery;
      byId("workflow-task-status").textContent = payload.task_status ? `${payload.task_status.summary} ${payload.task_status.recovery}` : "";
      byId("workflow-detail").hidden = false;
      const revision = `${record.version}:${JSON.stringify(record.steps.map((step) => [step.review_status, step.requests.map((item) => item.outcome), (step.setups || []).map((item) => item.status)]))}`;
      if (revision !== lastVersion && !byId("workflow-info-dialog").open) { renderWorkflowGraph(byId("workflow-map"), record, info, setupActions); lastVersion = revision; }
      renderUnassigned(payload.requests_not_on_a_step || [], payload.setups_not_on_a_step || []);
      renderEvents(payload.events, payload.event_total);
      renderCredentials(payload.credential_actions || []);
      byId("workflow-review").disabled = !payload.waiting_request_count || Boolean(payload.task_status);
      byId("workflow-review-status").textContent = reviewStatusText(payload);
      byId("workflow-connection").textContent = `Connected · Updated ${new Date().toLocaleTimeString()} · ${record.phase}`;
      byId("workflow-connection").classList.remove("workflow-error");
    } catch (error) {
      if (error.name === "AbortError") return;
      byId("workflow-connection").textContent = `Updates paused. ${error.message}`;
      byId("workflow-connection").classList.add("workflow-error");
    } finally {
      if (active && !document.hidden && generation === loadGeneration) timer = setTimeout(() => refresh(), 5000);
    }
  }

  async function loadList() {
    const payload = await request(`${WORKFLOWS_API}?offset=${listOffset}${includeTerminal ? "&status=resolved" : ""}`);
    const nodes = payload.workflows.map((flow) => {
      const card = element("article", "", "workflow-panel");
      const link = element("a", "Open Workflow", "button primary");
      link.href = `/workflows/${encodeURIComponent(flow.workflow_id)}`;
      card.append(element("h2", flow.title), element("p", `${flow.target_agent_type} · ${flow.phase}`), link);
      return card;
    });
    if (!nodes.length) nodes.push(element("p", includeTerminal ? "No workflows on this page." : "No active workflows."));
    const history = element("button", includeTerminal ? "Show Active Only" : "Include Finished Workflows", "button ghost");
    history.type = "button";
    history.setAttribute("aria-pressed", String(includeTerminal));
    history.onclick = () => { includeTerminal = !includeTerminal; listOffset = 0; refresh(); };
    const paging = window.UAPagination.create({ offset: listOffset, total: payload.total,
      onPage: (offset) => { listOffset = offset; return refresh(); } });
    byId("workflow-list").replaceChildren(history, ...nodes, paging);
    byId("workflow-connection").textContent = "Connected";
  }

  async function decideOne({ review, choice, button, note, group, message, card }) {
    if (busy) return;
    busy = true;
    const buttons = [...group.querySelectorAll("button")];
    buttons.forEach((item) => { item.disabled = true; });
    button.textContent = choice.busyLabel;
    try {
      const result = await request(`${apiRoot}/decisions`, {
        decisions: [{ request_id: review.request_id, choice: choice.choice, note: note.value || "" }],
      });
      const item = result.results[0];
      if (!item || !item.saved) throw new Error(item?.message || "The decision was not saved. Refresh and try again.");
      card.replaceWith(renderDecidedCard(document, {
        id: review.request_id, title: review.title, outcome: decisionOutcome(choice.choice, item.review),
      }));
      await refresh();
    } catch (error) {
      button.textContent = choice.label;
      buttons.forEach((item) => { item.disabled = false; });
      setCardMessage(message, error.message, true);
    } finally { busy = false; }
  }

  async function openReview() {
    if (busy) return;
    busy = true;
    try {
      const payload = await request(`${apiRoot}/review`, {});
      reviews = payload.reviews;
      if (!reviews.length) throw new Error("No request of this plan waits for a decision now. Refresh to see what happened.");
      byId("workflow-review-content").replaceChildren(...reviews.map((review) => renderReviewCard(document, review, {
        onChoose: (chosen) => decideOne(chosen),
        warn: (event, detail) => console.warn(event, detail),
      })));
      const decidable = reviews.filter((review) => review.can_decide).length;
      byId("workflow-approve-all").disabled = !bulkDecisions(reviews, CHOICE_APPROVE).length;
      byId("workflow-deny-all").disabled = !bulkDecisions(reviews, CHOICE_DENY).length;
      byId("workflow-decision-status").textContent = decidable < reviews.length
        ? "Some requests wait for a team administrator; you can see them but not decide them." : "";
      byId("workflow-review-dialog").showModal();
    } catch (error) { byId("workflow-review-status").textContent = error.message; }
    finally { busy = false; }
  }

  async function decideAll(choice) {
    if (busy) return;
    const decisions = bulkDecisions(reviews, choice);
    if (!decisions.length) return;
    busy = true;
    const buttons = [...byId("workflow-review-dialog").querySelectorAll("button")];
    buttons.forEach((button) => { button.disabled = true; });
    try {
      const result = await request(`${apiRoot}/decisions`, { decisions });
      byId("workflow-decision-status").textContent = batchResultText(result);
      if (result.all_saved) byId("workflow-review-dialog").close();
      await refresh();
    } catch (error) { byId("workflow-decision-status").textContent = error.message; }
    finally { busy = false; buttons.forEach((button) => { button.disabled = false; }); }
  }

  byId("workflow-refresh").onclick = () => refresh();
  byId("workflow-about").onclick = () => info("Your Access Plan", "The agent maps the whole goal before asking for access. Arrows show what each step needs first. A plan grants nothing: each request is decided on its own, here or in the Inbox, and a denied step is not asked for again.");
  byId("workflow-review").onclick = openReview;
  byId("workflow-close-review").onclick = () => byId("workflow-review-dialog").close();
  byId("workflow-approve-all").onclick = () => decideAll(CHOICE_APPROVE);
  byId("workflow-deny-all").onclick = () => decideAll(CHOICE_DENY);
  byId("workflow-logs").onclick = async () => {
    try {
      const payload = await request(`${apiRoot}/diagnostics`);
      const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }));
      const link = element("a", ""); link.href = url; link.download = "workflow-diagnostics.json";
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { byId("workflow-review-status").textContent = error.message; }
  };
  document.addEventListener("visibilitychange", () => { clearTimeout(timer); if (!document.hidden && active) refresh(); });
  window.addEventListener("pagehide", () => { clearTimeout(timer); controller.abort(); });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted && active) {
      controller = new AbortController();
      initialize();
    }
  });

  async function initialize() {
    try {
      const session = await request("/api/web-chat/session");
      csrf = session.permission_csrf_token;
      // A preference service error must not make the page unavailable.
      window.UAAppearance.loadServerPreference().catch(() => console.warn("Workflow appearance preference could not be loaded."));
      await refresh();
    } catch (error) { byId("workflow-connection").textContent = error.message; }
  }
  initialize();
}

if (typeof document !== "undefined" && document.body && document.body.classList.contains("workflow-body")) {
  startWorkflowPage();
}
