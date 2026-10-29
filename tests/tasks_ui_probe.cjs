// Exercise the shipped Settings page in Chromium with controlled HTTP responses.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

(async () => {
  const assets = path.resolve(__dirname, "../assistant_gateway/onboarding_web/assets");
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [], writes = [];
  let tasks = Array.from({ length: 23 }, (_, i) => ({ root_task_id: `task_${i}`, state: i ? "waiting" : "in_progress",
    is_current: i === 0, queue_position: i || null, problem_statement: i ? `Queued work ${i}` : "Current work <img src=x onerror=alert(1)>", blocker: i ? "Waiting for review" : null }));
  let failRead = false, holdRead = false, releaseRead, holdWrite = false, releaseWrite, loseWriteReply = false;
  tasks[0].is_parallel = tasks[1].is_parallel = true;
  tasks[3].is_parallel = true;
  tasks[3].queue_position = null;
  let finishedReads = 0;
  tasks[1].problem_statement = "Long maintenance instructions <img src=x> ".repeat(100);
  tasks[1].blocker = "Worker wait refreshed at 100%. ".repeat(300);
  tasks[1].latest_activity = "Repeated diagnostic update. ".repeat(100);
  page.on("pageerror", error => errors.push(error.message));
  await page.route("http://tasks.test/**", async route => {
    const url = new URL(route.request().url());
    const json = value => route.fulfill({ json: value });
    if (url.pathname === "/api/web-chat/session") return json({ canonical_user_id: "owner", permission_csrf_token: "csrf" });
    if (url.pathname === "/api/web-chat/settings") return json({ assistant_name: "Test", commands: [] });
    if (url.pathname === "/api/web-chat/sessions") return json({ sessions: [{ session_key: "session", updated_at: "2026-09-20T12:00:00Z" }], event_cursor: "1" });
    // The Tasks view's agent list follows the team status on each live page (team_activity.mjs).
    if (url.pathname === "/api/web-chat/events/stream") return route.fulfill({ contentType: "text/event-stream",
      body: "data: " + JSON.stringify({ status: "ok", messages: [], next_cursor: "1", transient_actions: [],
        team_activity: { members: [{ agent_id: "lead", name: "Lead", named_agent_type: "TeamArchitect" }] } }) + "\n\n" });
    if (url.pathname === "/api/web-chat/tasks") {
      if (url.searchParams.get("view") === "finished") {
        finishedReads++;
        const offset = Number(url.searchParams.get("offset"));
        assert.equal(url.searchParams.get("limit"), "10");
        const rows = Array.from({ length: 12 }, (_, i) => ({ root_task_id: `finished_${i}`,
          state: i === 0 ? "failed" : "completed", is_current: false, problem_statement: `Overnight task ${i}`,
          owner_agent_id: "stopped-worker", owner_agent_name: "Past worker", answer: "Result <img src=x onerror=alert(1)>" }));
        return json({ tasks: rows.slice(offset, offset + 10), total: rows.length, current_count: 0, queued_count: 0, inventory_digest: "b".repeat(64) });
      }
      if (holdRead) await new Promise(resolve => { releaseRead = resolve; });
      if (failRead) return route.fulfill({ status: 503, json: { message: "TeamLead unavailable" } });
      const offset = Number(url.searchParams.get("offset"));
      assert.equal(url.searchParams.get("limit"), "20");
      return json({ tasks: tasks.slice(offset, offset + 20), total: tasks.length, current_count: tasks.filter(t => t.is_current).length,
        queued_count: tasks.filter(t => !t.is_current).length, inventory_digest: "a".repeat(64) });
    }
    if (url.pathname === "/api/web-chat/tasks/control") {
      const body = route.request().postDataJSON();
      assert.equal(route.request().headers()["x-ua-permission-csrf"], "csrf");
      assert.equal(body.inventory_digest, "a".repeat(64));
      writes.push(body);
      if (holdWrite) await new Promise(resolve => { releaseWrite = resolve; });
      tasks = tasks.filter(task => body.action === "purge" ? task.is_current : task.root_task_id !== body.task_id);
      if (loseWriteReply) return route.fulfill({ status: 503, json: { message: "The result is not confirmed; the request may still be processing." } });
      return json({ success: true, message: "Tasks removed.", cancellation_receipts: [] });
    }
    if (url.pathname.startsWith("/api/")) return json({ messages: [], total: 0, next_cursor: "1" });
    const file = path.join(assets, url.pathname === "/webchat" ? "chat.html" : path.basename(url.pathname));
    return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404 });
  });
  try {
    await page.goto("http://tasks.test/webchat");
    await page.locator(".thread-row").waitFor();
    await page.locator("#settings-button").click();
    await page.locator("#settings-tasks-tab").click();
    const finished = page.locator(".tasks-finished");
    await finished.waitFor();
    assert.equal(await finished.evaluate(element => element.open), false);
    assert.equal(finishedReads, 0, "Collapsed history does not load or poll");
    await finished.locator(":scope > summary").focus();
    await page.keyboard.press("Enter");
    await finished.locator(".task-item").first().waitFor();
    assert.equal(await finished.locator(".task-item").count(), 10);
    assert.match(await finished.innerText(), /failed/);
    assert.equal(await finished.locator(".task-item button").count(), 0);
    await finished.locator(".task-result summary").first().click();
    assert.match(await finished.locator("details").first().innerText(), /Result <img/);
    assert.equal(await finished.locator("img").count(), 0);
    await finished.getByRole("button", { name: "Next page", exact: true }).click();
    await finished.getByText("Overnight task 10", { exact: true }).waitFor();
    assert.equal(await finished.locator(".task-item").count(), 2);
    const panel = page.locator('.tasks-agent-group[data-agent-id="lead"]');
    await panel.locator('[aria-current="page"]').filter({ hasText: /^1$/ }).waitFor();
    assert.equal(await panel.locator(".task-item").count(), 20);
    assert.equal(await panel.locator("img").count(), 0, "Task text must not become HTML");
    assert.equal(await page.locator("#settings-tasks-panel > .tasks-content > .tasks-agent-group").first().getAttribute("data-agent-id"), "lead");
    const longTask = panel.locator(".task-item").nth(1);
    assert.equal(await panel.locator(".task-item h4").first().textContent(), "Current work · Parallel");
    assert.equal(await longTask.locator("h4").textContent(), "Queue position 1 · Parallel");
    assert.equal(await panel.locator(".task-item h4").nth(2).textContent(), "Queue position 2");
    assert.equal(await panel.locator(".task-item h4").nth(3).textContent(), "Waiting work · Parallel");
    assert.deepEqual(await longTask.locator(".task-text > p").evaluateAll(rows => rows.map(row => row.textContent.length <= 280)), [true, true, true]);
    await longTask.getByText("Full description", { exact: true }).click();
    assert.equal(await longTask.locator(".task-full-text").first().textContent(), tasks[1].problem_statement);
    tasks[1].latest_activity_at = "2026-09-21T01:00:00Z";
    holdRead = true;
    const waitingRefresh = page.waitForRequest(request => request.url().includes("/tasks?") && request.url().includes("view=active"));
    const refreshButton = panel.getByRole("button", { name: "Refresh", exact: true });
    const oldWidth = (await refreshButton.boundingBox()).width;
    await refreshButton.click();
    await waitingRefresh;
    assert.equal(await refreshButton.getAttribute("aria-busy"), "true");
    assert.match(await refreshButton.getAttribute("class"), /is-refreshing/);
    assert.equal((await refreshButton.boundingBox()).width, oldWidth);
    assert.equal(await refreshButton.evaluate(button => getComputedStyle(button, "::after").animationIterationCount), "infinite");
    assert.doesNotMatch(await panel.innerText(), /Loading tasks/);
    holdRead = false; releaseRead();
    await page.waitForFunction(() => !document.querySelector('[data-agent-id="lead"] .tasks-refresh').hasAttribute("aria-busy"));
    assert.equal(await longTask.locator("details").first().evaluate(element => element.open), true);
    for (const theme of ["light", "dark"]) {
      await page.evaluate(theme => document.documentElement.dataset.colorMode = theme, theme);
      await page.setViewportSize({ width: 390, height: 844 });
      assert.ok(await longTask.locator(".task-full-text").first().evaluate(element => element.clientHeight <= 256 && element.scrollHeight > element.clientHeight));
      await longTask.getByText("Full description", { exact: true }).click();
      await longTask.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `/tmp/ua-compact-task-${theme}.png` });
      assert.ok((await longTask.boundingBox()).height < 500, "Long task previews stay compact on mobile: " + (await longTask.boundingBox()).height);
      await longTask.getByText("Full description", { exact: true }).click();
    }
    await longTask.getByText("Full description", { exact: true }).click();
    await page.setViewportSize({ width: 1280, height: 900 });
    await finished.locator(":scope > summary").click();
    assert.equal(await finished.evaluate(element => element.open), false);
    const bottomTask = panel.locator(".task-item").last();
    await bottomTask.getByRole("button", { name: "Remove", exact: true }).click();
    assert.equal(await bottomTask.getByRole("button", { name: "Confirm removal", exact: true }).count(), 1,
      "Removal confirmation must stay inside the selected task row");
    await bottomTask.getByRole("button", { name: "Cancel", exact: true }).click();
    assert.ok(await bottomTask.getByRole("button", { name: "Remove", exact: true }).evaluate(button => button === document.activeElement));
    await panel.getByRole("button", { name: "Next page", exact: true }).click();
    await panel.locator('[aria-current="page"]').filter({ hasText: /^2$/ }).waitFor();
    assert.equal(await panel.locator(".task-item").count(), 3);
    await panel.getByRole("button", { name: "Remove", exact: true }).first().click();
    assert.equal(writes.length, 0, "Removal requires confirmation");
    await panel.getByRole("button", { name: "Cancel", exact: true }).click();
    await panel.locator(".list-pagination").scrollIntoViewIfNeeded();
    for (const theme of ["light", "dark"]) {
      await page.evaluate(theme => { document.documentElement.dataset.colorMode = theme; }, theme);
      const fills = await panel.locator(".list-pagination").evaluate(nav => [
        getComputedStyle(nav.querySelector('[aria-current="page"]')).backgroundColor,
        getComputedStyle(nav.querySelector('button:not([aria-current])')).backgroundColor,
      ]);
      assert.notEqual(fills[0], fills[1], "The current page stays visibly selected in both themes");
    }
    assert.equal(writes.length, 0);
    await panel.getByRole("button", { name: "Remove", exact: true }).first().click();
    holdWrite = true;
    await panel.getByRole("button", { name: "Confirm removal", exact: true }).click();
    await panel.getByRole("button", { name: "Removing…", exact: true }).waitFor();
    assert.ok(await panel.getByRole("button", { name: "Purge this queue", exact: true }).isDisabled());
    assert.equal(writes.length, 1);
    holdWrite = false; releaseWrite();
    await panel.getByText("Tasks removed.", { exact: true }).waitFor();
    assert.equal(writes[0].task_id, "task_20");
    loseWriteReply = true;
    await panel.getByRole("button", { name: "Purge this queue", exact: true }).click();
    await panel.getByRole("button", { name: "Confirm purge", exact: true }).click();
    await panel.locator('[aria-current="page"]').filter({ hasText: /^1$/ }).waitFor();
    assert.equal(await panel.locator(".task-item").count(), 1);
    await panel.getByText(/The result is not confirmed.*latest task state/).waitFor();
    assert.ok(await panel.locator(".tasks-confirmation").isHidden());
    assert.equal(writes.length, 2, "A lost purge reply must refresh state without repeating the mutation");
    loseWriteReply = false;
    assert.ok(await panel.getByRole("button", { name: "Purge this queue", exact: true }).isDisabled());
    await panel.getByRole("button", { name: "Stop and remove", exact: true }).waitFor();
    failRead = true;
    await panel.getByRole("button", { name: "Refresh", exact: true }).click();
    await panel.getByText("TeamLead unavailable", { exact: true }).waitFor();
    assert.equal(await panel.locator(".task-item").count(), 1, "Read failure preserves the visible list");
    failRead = false;
    tasks = Array.from({ length: 123 }, (_, i) => ({ root_task_id: `task_${i}`, state: "waiting",
      is_current: false, queue_position: i + 1, problem_statement: `Queued work ${i}` }));
    await panel.getByRole("button", { name: "Refresh", exact: true }).click();
    await panel.getByRole("button", { name: "Go to page 5", exact: true }).waitFor();
    assert.equal(await panel.locator(".pagination-ellipsis").count(), 1);
    assert.equal(await panel.getByRole("button", { name: /^Go to page / }).count(), 5);
    assert.equal(await panel.getByRole("button", { name: "Next page", exact: true }).innerText(), "");
    await panel.getByRole("button", { name: "Last page", exact: true }).click();
    await panel.locator('[aria-current="page"]').filter({ hasText: /^7$/ }).waitFor();
    assert.equal(await panel.locator(".task-item").count(), 3);
    assert.match(await panel.locator(".task-item").first().innerText(), /task_120/);
    assert.ok(await panel.getByRole("button", { name: "Last page", exact: true }).isDisabled());
    await panel.getByRole("button", { name: "First page", exact: true }).click();
    await panel.locator('[aria-current="page"]').filter({ hasText: /^1$/ }).waitFor();
    await panel.getByRole("button", { name: "Go to page 3", exact: true }).click();
    await panel.locator('[aria-current="page"]').filter({ hasText: /^3$/ }).waitFor();
    assert.match(await panel.locator(".task-item").first().innerText(), /task_40/);
    holdRead = true;
    const pagingRead = page.waitForRequest(request => request.url().includes("/tasks?") && request.url().includes("offset=60"));
    await panel.getByRole("button", { name: "Go to page 4", exact: true }).click();
    await pagingRead;
    assert.equal(await panel.locator(".list-pagination button:enabled").count(), 0, "Paging locks while the requested page loads");
    holdRead = false; releaseRead();
    await panel.locator('[aria-current="page"]').filter({ hasText: /^4$/ }).waitFor();
    await page.waitForFunction(() => document.activeElement.matches('[aria-current="page"]'));
    assert.equal(await panel.locator(".pagination-ellipsis").count(), 2);
    // A read already in flight must not detach a newly opened confirmation.
    holdRead = true;
    const refreshRead = page.waitForRequest(request => request.url().includes("/tasks?"));
    await panel.getByRole("button", { name: "Refresh", exact: true }).click();
    await refreshRead;
    await panel.locator(".task-item").last().getByRole("button", { name: "Remove", exact: true }).click();
    tasks[79].problem_statement = "Changed during confirmation";
    const refreshed = page.waitForResponse(response => response.url().includes("/tasks?"));
    holdRead = false; releaseRead();
    await refreshed;
    await panel.getByRole("button", { name: "Refresh", exact: true }).isEnabled();
    assert.equal(await panel.locator(".task-item").last().getByRole("button", { name: "Confirm removal", exact: true }).count(), 1);
    await panel.getByRole("button", { name: "Cancel", exact: true }).click();
    await page.screenshot({ path: "/tmp/ua-tasks-desktop.png" });
    await page.setViewportSize({ width: 390, height: 844 });
    await panel.locator(".task-item").last().getByRole("button", { name: "Remove", exact: true }).click();
    const mobileConfirm = panel.locator(".task-item").last().getByRole("button", { name: "Confirm removal", exact: true });
    const bounds = await mobileConfirm.boundingBox();
    assert.ok(bounds.y >= 0 && bounds.y + bounds.height <= 844, "The inline confirmation stays in view on mobile");
    await page.screenshot({ path: "/tmp/ua-tasks-mobile.png" });
    await panel.getByRole("button", { name: "Cancel", exact: true }).click();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    assert.equal(overflow, false, "Tasks must fit a mobile viewport");
    await panel.locator(".list-pagination").scrollIntoViewIfNeeded();
    assert.ok(await panel.locator(".list-pagination").evaluate(nav => nav.scrollWidth <= nav.clientWidth), "Page controls wrap within the mobile panel");
    assert.ok(await panel.locator(".list-pagination").evaluate(nav => nav.getBoundingClientRect().height <= 80), "Mobile pagination stays compact instead of stacking full-width buttons");
    await page.screenshot({ path: "/tmp/ua-task-pagination-mobile.png" });
    await page.setViewportSize({ width: 1280, height: 900 });
    holdRead = true;
    await panel.getByRole("button", { name: "Refresh", exact: true }).click();
    await page.waitForTimeout(100);
    await page.locator("#settings-team-tab").click();
    holdRead = false; releaseRead();
    await page.waitForTimeout(100);
    assert.ok(await panel.isHidden(), "A late task response must not switch tabs");
    assert.deepEqual(errors, []);
    console.log("PASS: task paging, confirmations, CSRF, busy state, purge, current-work preservation, errors, mobile layout and stale responses");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
