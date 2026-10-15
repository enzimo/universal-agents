import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { createRuntimeViews } from "../assistant_gateway/onboarding_web/assets/chat_runtime_views.mjs";

function node(text = "", className = "") {
  return {
    textContent: text, className, childNodes: [], scrollTop: 0,
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute() {}, removeAttribute() {},
    appendChild(child) { this.childNodes.push(child); },
    replaceChildren(...children) {
      this.childNodes = children.flatMap(child => child.fragment ? child.childNodes : [child]);
    },
  };
}

function fixture() {
  // chat.html loads action_feedback.js as a classic script before this module;
  // loading the real helper also exercises Refresh-button restoration.
  globalThis.window = {};
  vm.runInNewContext(
    readFileSync(new URL("../assistant_gateway/onboarding_web/assets/action_feedback.js", import.meta.url), "utf8"),
    { window: globalThis.window },
  );
  globalThis.document = { createDocumentFragment: () => ({ ...node(), fragment: true }) };
  const sharedState = {
    authSessionActive: true, runtimeControlsSessionKey: "session-a", runtimeControlsView: "objectives",
    objectivesContent: node(), objectivesUpdatedAt: node(), objectivesSnapshotSignature: "",
    runtimeControlSnapshots: new Map(),
  };
  const runtime_viewsState = Object.fromEntries([
    "objectivesRefresh", "objectivesStatus", "runtimeObjectivesTab", "runtimeSchedulesTab", "runtimeMcpTab",
  ].map(key => [key, node()]));
  const requests = [];
  const views = createRuntimeViews({
    sharedState, runtime_viewsState, lifecycle: {},
    requestClient: { fetch(url) { return new Promise((resolve, reject) => requests.push({ url, resolve, reject })); } },
    operations: { parseApiResponse: value => value, formatShortLocalTimestamp: () => "now", timestampElement: () => null, uiElement: (_tag, className, text) => node(text, className) },
  });
  return { sharedState, runtime_viewsState, requests, views };
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test("schedules sort by next fire with undated entries last without mutating the response", () => {
  const { views, sharedState } = fixture();
  const schedules = [
    { schedule_id: "later", next_fire_at: "2026-09-23T03:00:00Z" },
    { schedule_id: "undated", next_fire_at: null },
    { schedule_id: "sooner", next_fire_at: "2026-09-22T03:00:00Z" },
    { schedule_id: "invalid", next_fire_at: "unknown" },
  ];
  schedules.forEach(schedule => { schedule.management_class = "user"; schedule.available_actions = []; schedule.worker_assignment = {}; });
  views.renderSchedules(schedules);
  const cards = sharedState.objectivesContent.childNodes.map(textTree);
  assert.match(cards[0], /sooner/);
  assert.match(cards[1], /later/);
  assert.equal(schedules[0].schedule_id, "later");
});

function textTree(element) {
  return [element.textContent, ...element.childNodes.map(textTree)].join(" ");
}

test("schedule cards distinguish assigned type, specific identity, and saved automatic creation policy", () => {
  const { views } = fixture();
  const schedule = {
    schedule_id: "job", enabled: false, management_class: "user", available_actions: [],
    worker_assignment: { agent_type: "Worker", agent_id: "worker-123", automatic_creation: "enabled", desired_agent_type: "Replacement", reassignment_pending: true },
  };
  const content = textTree(views.scheduleCard(schedule));
  assert.match(content, /Disabled/);
  assert.match(content, /Assigned agent type: Worker/);
  assert.match(content, /Specific agent: worker-123/);
  assert.match(content, /Automatic creation: Enabled by TeamLead/);
  assert.match(content, /Pending agent type: Replacement/);
  assert.match(content, /subject to current access and launch limits/);
  for (const [status, label] of [["not_configured", "Not configured"], ["disabled", "Disabled by TeamLead"]]) {
    const ordinary = textTree(views.scheduleCard({ ...schedule, worker_assignment: { agent_type: null, agent_id: null, automatic_creation: status } }));
    assert.ok(ordinary.includes("Automatic creation: " + label));
    assert.match(ordinary, /Assigned agent type: Not specified/);
    assert.doesNotMatch(ordinary, /Specific agent|Pending agent type/);
  }
});

test("memory review cards show the local scheduler and default startup permission", () => {
  const { views } = fixture();
  for (const enabled of [true, false]) {
    const content = textTree(views.scheduleCard({
      schedule_id: "memory-review", enabled, management_class: "system", available_actions: [],
      worker_assignment: { agent_type: "MemReviewAgent", agent_id: null, review_agent_type: "MemReviewAgent", automatic_creation: "default_policy" },
    }));
    assert.match(content, /Review agent: MemReviewAgent/);
    assert.match(content, /Managed by: Local scheduler/);
    assert.match(content, /Agent startup: Allowed by default policy/);
    assert.match(content, /subject to current access and launch limits/);
    assert.doesNotMatch(content, /Automatic creation|Not configured|Assigned agent type/);
    assert.match(content, enabled ? /Enabled/ : /Disabled/);
  }
});

test("a stuck schedule says it is not running, since when, and what to do", () => {
  const { views } = fixture();
  const stuck = {
    schedule_id: "nightly", schedule_name: "crypto-research", enabled: true, management_class: "user",
    available_actions: [],
    worker_assignment: {
      agent_type: "BackgroundOperationsAgent", agent_id: null, automatic_creation: "default_policy",
      not_running_since: "2026-10-03T08:45:00Z",
      not_running_explanation: "Ask your assistant to review this schedule; it can show you the details.",
    },
  };
  const content = textTree(views.scheduleCard(stuck));
  assert.match(content, /Not running/);
  assert.doesNotMatch(content, /\bEnabled\b(?! by)/);
  assert.match(content, /Not running since its last due time\./);
  assert.match(content, /Ask your assistant to review this schedule/);
  // The callout grid has two columns (icon, text): both lines must share the
  // text block, or the explanation is squeezed to one letter per line.
  const callout = views.scheduleCard(stuck).childNodes.find(child => /schedule-not-running/.test(child.className));
  assert.deepEqual(callout.childNodes.map(child => child.className), ["warning-icon", "schedule-not-running-text"]);
  assert.equal(callout.childNodes[1].childNodes.length, 2);
  const healthy = textTree(views.scheduleCard({ ...stuck, worker_assignment: { ...stuck.worker_assignment, not_running_since: null } }));
  assert.match(healthy, /Enabled/);
  assert.doesNotMatch(healthy, /Not running/);
  const paused = textTree(views.scheduleCard({ ...stuck, paused_until: "2026-10-05T00:00:00Z" }));
  assert.match(paused, /Paused/);
});

test("tab refreshes preserve the existing nodes and visited views", async () => {
  const { sharedState, requests, views } = fixture();
  const first = views.loadObjectives();
  requests[0].resolve({ objectives: [] });
  await first;
  const original = sharedState.objectivesContent.childNodes[0];
  views.selectRuntimeControlsView("objectives");
  assert.equal(sharedState.objectivesContent.childNodes[0], original, "clicking the selected tab must not blank it");
  requests[1].resolve({ objectives: [] });
  await settle();
  sharedState.objectivesContent.scrollTop = 120;
  views.selectRuntimeControlsView("schedules");
  requests[2].resolve({ schedules: [] });
  await settle();
  views.selectRuntimeControlsView("objectives");
  assert.equal(sharedState.objectivesContent.childNodes[0], original, "returning to a visited tab must restore its content immediately");
  assert.equal(sharedState.objectivesContent.scrollTop, 120);
  requests[3].resolve({ objectives: [] });
  await settle();
  assert.equal(sharedState.objectivesContent.childNodes[0], original);
});

test("a response belongs to the requested tab and the latest tab is then fetched", async () => {
  const { sharedState, requests, views } = fixture();
  const first = views.loadObjectives();
  views.selectRuntimeControlsView("schedules");
  requests[0].resolve({ objectives: [] });
  await first;
  await settle();
  assert.equal(requests.length, 2, "switching during a request must queue the selected view");
  assert.match(requests[1].url, /\/schedules\?/);
  requests[1].resolve({ schedules: [] });
  await settle();
  assert.match(sharedState.objectivesContent.childNodes[0].textContent, /No schedules/);
});

test("an old session cannot populate content or finish a new session's request", async () => {
  const { sharedState, requests, views } = fixture();
  const oldRequest = views.loadObjectives();
  sharedState.runtimeControlsSessionKey = "session-b";
  sharedState.objectivesRequestInFlight = false;
  const newRequest = views.loadObjectives();
  requests[0].resolve({ objectives: [] });
  await oldRequest;
  assert.equal(sharedState.objectivesContent.childNodes.length, 0);
  assert.equal(sharedState.objectivesRequestInFlight, true);
  requests[1].resolve({ objectives: [] });
  await newRequest;
  assert.equal(sharedState.objectivesRequestInFlight, false);
});

test("identical MCP refreshes preserve their controls", () => {
  const { sharedState, views } = fixture();
  views.renderMcp({ mcp_connections: [], hosted_mcp_servers: [] });
  const original = sharedState.objectivesContent.childNodes[0];
  views.renderMcp({ mcp_connections: [], hosted_mcp_servers: [] });
  assert.equal(sharedState.objectivesContent.childNodes[0], original);
});

test("confirmation expires while a cached tab is detached", () => {
  let click;
  let timeout;
  globalThis.window = {};
  const views = createRuntimeViews({
    sharedState: {}, runtime_viewsState: {}, requestClient: {},
    operations: { uiElement: (_tag, _class, text) => ({ ...node(text), dataset: {}, isConnected: false }) },
    lifecycle: { listen: (_node, _event, callback) => { click = callback; }, timeout: callback => { timeout = callback; } },
  });
  const button = views.runtimeActionButton("Delete", "delete", () => assert.fail("confirmation must not execute the action"), true);
  click();
  assert.equal(button.textContent, "Confirm delete");
  timeout();
  assert.equal(button.textContent, "Delete");
  assert.equal(button.dataset.confirmed, "");
});
