// Shipped browser code, real DOM, controlled identity/task/event boundaries.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const assets = path.resolve(__dirname, "../assistant_gateway/onboarding_web/assets");
  const errors = [], reads = [], writes = [];
  let workerUnavailable = false;
  const members = ["worker", "lead"].map(agent_id => ({ agent_id, name: agent_id,
    named_agent_type: agent_id === "lead" ? "TeamArchitect" : "MemReviewAgent",
    is_team_lead: agent_id === "lead", status: { key: "busy", label: "Busy", tone: "warning" },
    available_skills: [], last_seen_at: "2026-09-21T01:00:00Z" }));
  await page.addInitScript(() => {
    window.testHidden = false;
    Object.defineProperty(document, "hidden", { get: () => window.testHidden });
    document.hasFocus = () => !window.testHidden;
    window.notices = [];
    window.permissionRequests = 0;
    window.Notification = class {
      static permission = "default";
      static async requestPermission() { window.permissionRequests++; this.permission = "granted"; return "granted"; }
      constructor(title, options) { this.title = title; this.options = options; window.notices.push(this); }
      close() { this.closed = true; this.onclose?.(); }
    };
    window.EventSource = class extends EventTarget {
      constructor() { super(); window.eventStream = this; }
      close() {}
    };
  });
  page.on("pageerror", error => errors.push(error.message));
  await page.route("https://ui.test/**", async route => {
    const url = new URL(route.request().url());
    const json = body => route.fulfill({ json: body });
    if (url.pathname === "/api/web-chat/session") return json({ canonical_user_id: "owner", permission_csrf_token: "csrf" });
    if (url.pathname === "/api/web-chat/settings") return json({ assistant_name: "Test", commands: [] });
    if (url.pathname === "/api/web-chat/sessions") return json({ sessions: [{ session_key: "session", updated_at: "2026-09-21T01:00:00Z" }], event_cursor: "1" });
    if (url.pathname === "/api/web-chat/team") return json({ members, definitions: [
      { named_agent_type: "MemReviewAgent", display_name: "Memory reviewer", description: "Reviews saved conversations." },
      { named_agent_type: "DormantAgent", display_name: "Dormant specialist", description: "Defined but not currently running. <img src=x>" },
    ], mcp_connections: [], hosted_mcp_servers: [] });
    if (url.pathname === "/api/web-chat/tasks") {
      const agent = url.searchParams.get("agent_id"); reads.push(agent);
      assert.ok(["lead", "worker"].includes(agent));
      if (agent === "worker" && workerUnavailable) return route.fulfill({ status: 503, json: { message: "Worker unavailable" } });
      return json({ tasks: [{ root_task_id: agent + "_task", state: "in_progress", is_current: true,
        problem_statement: agent + " work" }], total: 1, queued_count: 0, current_count: 1,
        inventory_digest: (agent === "lead" ? "a" : "b").repeat(64) });
    }
    if (url.pathname === "/api/web-chat/tasks/control") {
      writes.push(route.request().postDataJSON());
      return json({ success: true, message: "Stopped", cancellation_receipts: [] });
    }
    if (url.pathname.startsWith("/api/")) return json({ messages: [], total: 0, next_cursor: "1" });
    const file = path.join(assets, url.pathname === "/webchat" ? "chat.html" : path.basename(url.pathname));
    return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404 });
  });
  const emit = async (id, level = "normal") => page.evaluate(({ id, level }) => {
    window.eventStream.onmessage({ data: JSON.stringify({ status: "ok", next_cursor: String(id), messages: [{
      session_key: "session", message_id: "message_" + id, session_sequence: id, role: "assistant",
      message_level: level, message_type: "task_response", content: "Private message content",
      created_at: "2026-09-21T01:00:00Z",
    }] }) });
  }, { id, level });
  // The team's status arrives with each live page (team_activity.mjs), not from its own read.
  const emitTeam = async () => page.evaluate(team => {
    window.eventStream.onmessage({ data: JSON.stringify({ status: "ok", next_cursor: "1", messages: [],
      team_activity: { members: team } }) });
  }, members);
  try {
    await page.goto("https://ui.test/webchat");
    await page.locator(".thread-row").waitFor();
    await emitTeam();
    // Busy agents are named by their type, not their id (activity_mesh.mjs).
    assert.equal(await page.locator("#chat-activity canvas").getAttribute("aria-label"),
      "Team activity: MemReviewAgent, TeamArchitect working.", "the sidebar mesh shows the same live status");
    await page.locator("#settings-button").click();
    assert.equal(await page.evaluate(() => permissionRequests), 0, "No automatic browser permission prompt");
    await page.getByRole("switch", { name: "Chat Alerts" }).check();
    await page.locator("#browser-notifications-toggle:checked").waitFor();
    await page.locator("#settings-tasks-tab").click();
    const worker = page.locator('.tasks-agent-group[data-agent-id="worker"]');
    const lead = page.locator('.tasks-agent-group[data-agent-id="lead"]');
    await worker.getByText("worker work", { exact: true }).waitFor();
    await lead.getByText("lead work", { exact: true }).waitFor();
    assert.equal(await page.locator("#settings-tasks-panel > .tasks-content > section").first().getAttribute("data-agent-id"), "lead");
    await worker.getByRole("button", { name: "Stop and remove" }).click();
    await worker.getByRole("button", { name: "Confirm removal" }).click();
    await worker.getByText("Stopped", { exact: true }).waitFor();
    assert.equal(writes[0].agent_id, "worker");
    assert.equal(writes[0].inventory_digest, "b".repeat(64));
    assert.equal(writes[0].task_id, "worker_task");
    workerUnavailable = true;
    await worker.getByRole("button", { name: "Refresh", exact: true }).click();
    await worker.getByText("Worker unavailable").waitFor();
    assert.equal(await lead.locator(".task-item").count(), 1);
    await page.locator("#settings-dialog-close").click();
    await page.locator("#settings-button").click();
    assert.equal(await page.locator("#settings-tasks-tab").getAttribute("aria-selected"), "true");
    await page.locator("#settings-team-tab").click();
    await page.getByText("Dormant specialist", { exact: false }).waitFor();
    assert.equal(await page.locator(".team-classes-table img").count(), 0);
    await page.locator("#settings-dialog-close").click();
    await page.evaluate(() => { testHidden = true; document.dispatchEvent(new Event("visibilitychange")); });
    await emit(2, "attention");
    assert.match(await page.title(), /1 need attention/);
    assert.equal(await page.locator(".thread-row.has-attention").count(), 1);
    assert.equal(await page.evaluate(() => notices.length), 1);
    assert.equal(await page.evaluate(() => notices[0].options.body), "Open Universal Agents to read it.");
    await emit(2, "attention");
    assert.equal(await page.evaluate(() => notices.length), 1, "Replayed events do not alert twice");
    await page.evaluate(() => { testHidden = false; document.dispatchEvent(new Event("visibilitychange")); });
    assert.doesNotMatch(await page.title(), /unread|need attention/);
    await emit(3);
    assert.equal(await page.evaluate(() => notices.length), 1, "Reading the conversation suppresses desktop alerts");
    await page.evaluate(() => { testHidden = true; });
    await emit(4);
    assert.match(await page.title(), /1 unread/);
    await page.evaluate(() => { testHidden = false; notices.at(-1).onclick(); });
    assert.doesNotMatch(await page.title(), /unread/);
    await page.locator("#settings-button").click();
    assert.equal(await page.locator("#settings-team-tab").getAttribute("aria-selected"), "true");
    await page.screenshot({ path: "/tmp/ua-team-classes-desktop.png" });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: "/tmp/ua-team-classes-mobile.png" });
    assert.deepEqual(errors, []);
    console.log("PASS: all-agent tasks, worker controls and isolation, Settings memory, class descriptions, background alerts, replay and tab attention");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
