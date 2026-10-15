// Execute the shipped UI with controlled HTTP responses and a real broker form.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

(async () => {
  const assets = path.join(__dirname, "../assistant_gateway/onboarding_web/assets");
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [], writes = [], sessions = [], chatMessages = [];
  let creations = 0, failSave = false, teamWritable = true;
  let failNextList = false, failListAfterSave = false;
  let holdList = false, releaseList;
  const memories = { personal: [], team: [] };
  function memory(scope, text, tags = []) {
    return { scope, memory_id: memories[scope].length + 1, fact_text: text, tags,
      updated_at_utc: "2026-09-20T12:00:00Z", available_actions: ["edit", "delete"], record_digest: "a".repeat(64) };
  }
  for (let i = 0; i < 23; i++) memories.personal.push(memory("personal", `Fact ${i}`));
  page.on("pageerror", error => errors.push(error.message));
  await page.route("http://ui.test/**", async route => {
    const url = new URL(route.request().url());
    const json = body => route.fulfill({ json: body });
    if (url.pathname === "/broker") return route.fulfill({ path: process.argv[2], contentType: "text/html" });
    if (url.pathname === "/api/web-chat/session") return json({ canonical_user_id: "ui_user", assistant_name: "Test", permission_csrf_token: "csrf" });
    if (url.pathname === "/api/web-chat/settings") return json({ assistant_name: "Test", commands: [] });
    if (url.pathname === "/api/web-chat/chat") {
      chatMessages.push(route.request().postDataJSON());
      return json({ status: "ok", response: "Test reply" });
    }
    if (url.pathname === "/api/web-chat/sessions") {
      if (route.request().method() === "POST") {
        creations++;
        sessions.push({ session_key: `session_${creations}`, updated_at: "2026-09-20T12:00:00Z" });
        return json({ session: sessions.at(-1) });
      }
      return json({ sessions, event_cursor: "1" });
    }
    if (url.pathname === "/api/web-chat/memories") {
      if (holdList) await new Promise(resolve => { releaseList = resolve; });
      if (failNextList) {
        failNextList = false;
        return route.fulfill({ status: 503, json: { message: "Test refresh unavailable" } });
      }
      const scope = url.searchParams.get("scope"), offset = Number(url.searchParams.get("offset"));
      return json({ memories: memories[scope].slice(offset, offset + 20), total: memories[scope].length,
        has_more: offset + 20 < memories[scope].length, can_create: scope === "personal" || teamWritable });
    }
    if (/\/memories\/(personal|team)$/.test(url.pathname)) {
      const scope = url.pathname.split("/").at(-1), body = route.request().postDataJSON();
      assert.equal(route.request().headers()["x-ua-permission-csrf"], "csrf");
      if (failSave) return route.fulfill({ status: 503, json: { message: "Test save unavailable" } });
      writes.push({ scope, body });
      const fact = memory(scope, body.fact_text, body.tags);
      memories[scope].push(fact);
      failNextList = failListAfterSave;
      failListAfterSave = false;
      return json({ memory: fact });
    }
    if (/\/memories\/(personal|team)\/\d+$/.test(url.pathname) && route.request().method() === "DELETE") {
      const parts = url.pathname.split("/"), scope = parts.at(-2), id = Number(parts.at(-1));
      memories[scope] = memories[scope].filter(item => item.memory_id !== id);
      return json({ deleted: true });
    }
    if (url.pathname.startsWith("/api/")) return json({ messages: [], total: 0, next_cursor: "1" });
    const file = path.join(assets, url.pathname === "/webchat" ? "chat.html" : path.basename(url.pathname));
    return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404 });
  });
  try {
    await page.goto("http://ui.test/webchat");
    await page.locator("#chat-app").waitFor();
    await page.waitForTimeout(400);
    assert.equal(creations, 0, "Opening Chat must not create a session");
    assert.equal(await page.locator(".thread-row").count(), 0);
    await page.locator("#new-thread-button").click();
    await page.locator(".thread-row").waitFor();
    assert.equal(creations, 1);
    await page.reload();
    await page.locator(".thread-row").waitFor();
    assert.equal(creations, 1, "Reloading an existing conversation must not create one");
    await page.locator("#settings-button").click();
    await page.locator("#settings-memories-tab").click();
    const panel = page.locator("#settings-memories-panel");
    await panel.locator('[aria-current="page"]').filter({ hasText: /^1$/ }).waitFor();
    holdList = true;
    const read = page.waitForRequest(request => new URL(request.url()).pathname === "/api/web-chat/memories");
    const refresh = panel.getByRole("button", { name: "Refresh", exact: true });
    const width = await refresh.evaluate(button => button.clientWidth);
    await refresh.click();
    await read;
    assert.equal(await refresh.getAttribute("aria-busy"), "true");
    assert.ok(await page.locator("#settings-memories-status").isHidden());
    for (const theme of ["dark", "light"]) {
      await page.evaluate(theme => document.documentElement.dataset.colorMode = theme, theme);
      assert.equal(await refresh.evaluate(button => button.clientWidth), width);
      assert.equal(await refresh.evaluate(button => getComputedStyle(button).color), "rgba(0, 0, 0, 0)");
      assert.equal(await refresh.evaluate(button => getComputedStyle(button, "::after").animationIterationCount), "infinite");
    }
    await page.emulateMedia({ reducedMotion: "reduce" });
    assert.equal(await refresh.evaluate(button => getComputedStyle(button, "::after").animationName), "none");
    await page.emulateMedia({ reducedMotion: "no-preference" });
    holdList = false; releaseList();
    await page.waitForFunction(() => !document.getElementById("settings-memories-refresh").hasAttribute("aria-busy"));
    // Overlapping list reads must not clear each other's busy indicator.
    assert.ok(await refresh.evaluate(button => {
      const first = UAActionFeedback.beginRefresh(button), second = UAActionFeedback.beginRefresh(button);
      first(); first();
      const stillBusy = button.disabled && button.getAttribute("aria-busy") === "true";
      second();
      return stillBusy && !button.disabled && !button.hasAttribute("aria-busy");
    }));
    await panel.getByRole("button", { name: "Next page", exact: true }).click();
    await panel.locator('[aria-current="page"]').filter({ hasText: /^2$/ }).waitFor();
    for (const scope of ["personal", "team"]) {
      await page.locator(`#memory-${scope}-tab`).click();
      await panel.getByRole("button", { name: "New", exact: true }).click();
      const editor = panel.locator(".memory-editor");
      await editor.getByLabel("Memory", { exact: true }).fill(`Created ${scope}`);
      await editor.getByLabel("Tags", { exact: true }).fill("alpha, beta");
      await editor.getByLabel(/^Expires/).fill("2099-01-01T12:00");
      failSave = true;
      await editor.getByRole("button", { name: "Save", exact: true }).click();
      await panel.getByText("Test save unavailable").waitFor();
      assert.equal(await editor.getByLabel("Memory", { exact: true }).inputValue(), `Created ${scope}`);
      failSave = false;
      failListAfterSave = scope === "team";
      await editor.getByRole("button", { name: "Save", exact: true }).click();
      if (scope === "team") {
        await panel.getByText("Memory added, but the list could not be refreshed. Select Refresh to see it.").waitFor();
        assert.equal(await panel.locator(".memory-editor").count(), 0, "A saved form must not allow duplicate submission after a read failure");
        await panel.getByRole("button", { name: "Refresh", exact: true }).click();
      }
      await panel.locator(".memory-fact").filter({ hasText: `Created ${scope}` }).waitFor();
      assert.equal(writes.at(-1).scope, scope);
      assert.deepEqual(writes.at(-1).body.tags, ["alpha", "beta"]);
      assert.ok(writes.at(-1).body.invalid_after_utc.endsWith("Z"));
    }
    teamWritable = false;
    await panel.getByRole("button", { name: "Refresh", exact: true }).click();
    await panel.getByRole("button", { name: "New", exact: true }).waitFor({ state: "hidden" });
    await page.locator("#memory-personal-tab").click();
    await panel.getByRole("button", { name: "New", exact: true }).click();
    await panel.getByRole("button", { name: "Cancel", exact: true }).click();
    assert.equal(await panel.locator(".memory-editor").count(), 0);
    await panel.getByRole("button", { name: "Next page", exact: true }).click();
    for (let i = 0; i < 4; i++) {
      await panel.getByRole("button", { name: "Delete", exact: true }).first().click();
      await panel.getByRole("button", { name: "Confirm delete", exact: true }).click();
      await panel.getByText("Memory deleted. Its audit event remains available for recovery.").waitFor();
    }
    assert.equal(await panel.locator(".memory-item").count(), 20);
    assert.equal(await panel.locator(".list-pagination").count(), 0, "Deleting the last page returns to the remaining page");
    for (const theme of ["light", "dark"]) {
      await page.evaluate(theme => document.documentElement.dataset.colorMode = theme, theme);
      await page.setViewportSize({ width: 390, height: 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({ path: `/tmp/ua-ui-memories-${theme}.png` });
    }
    // An empty composer still starts a conversation when the user sends text.
    sessions.length = 0;
    await page.reload();
    await page.locator("#chat-app").waitFor();
    await page.waitForFunction(() => !document.querySelector(".thread-row"));
    assert.equal(creations, 1);
    await page.locator("#message-input").fill("First message");
    const sent = page.waitForResponse(response => response.url().endsWith("/api/web-chat/chat"));
    await page.getByRole("button", { name: "Send message", exact: true }).click();
    await sent;
    assert.equal(creations, 2);
    assert.equal(chatMessages.at(-1).message, "First message");
    await page.goto("http://ui.test/broker");
    const secret = page.locator("#field__api_key");
    await secret.fill("test-only-secret");
    const toggle = page.locator('[data-secret-toggle][aria-controls="field__api_key"]');
    assert.equal(await secret.getAttribute("type"), "password");
    await toggle.click();
    assert.equal(await secret.getAttribute("type"), "text");
    assert.equal(await secret.inputValue(), "test-only-secret");
    assert.equal(await toggle.getAttribute("aria-pressed"), "true");
    await toggle.focus();
    await page.keyboard.press("Space");
    assert.equal(await secret.getAttribute("type"), "password");
    assert.equal(await page.locator('[data-secret-toggle][aria-controls="field__username"]').count(), 0);
    assert.deepEqual(errors, []);
    console.log("Chat startup, memory creation, failure recovery, page totals, mobile themes and credential reveal passed.");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
