// Cold sign-in must restore saved conversations before Settings chooses a session.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

async function checkSignIn(browser, earlySettings) {
  const assets = path.resolve(__dirname, "../assistant_gateway/onboarding_web/assets");
  const context = await browser.newContext({ reducedMotion: "reduce" });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const sessions = [
    { session_key: "first", conversation_id: "conversation_first", title: "First saved chat" },
    { session_key: "selected", conversation_id: "conversation_selected", title: "Selected saved chat" },
  ];
  if (!earlySettings) {
    // A returning browser can display saved metadata before the server replies.
    await page.addInitScript(sessions => {
      localStorage.setItem("ua.webchat.ui.example-user", JSON.stringify(sessions.map(session => ({
        id: session.session_key, sessionKey: session.session_key,
        conversationId: session.conversation_id, title: session.title, persisted: true,
      }))));
      localStorage.setItem("ua.webchat.ui.example-user.selected", "first");
    }, sessions);
  }
  const errors = [], sends = [], pendingCreations = [];
  let signedIn = false, creations = 0, releaseList, listStarted;
  const listing = new Promise(resolve => { listStarted = resolve; });
  page.on("pageerror", error => errors.push(error.message));
  await page.route("http://startup.test/**", async route => {
    const request = route.request(), url = new URL(request.url());
    const json = value => route.fulfill({ json: value });
    const auth = { canonical_user_id: "example-user", assistant_name: "Nova", permission_csrf_token: "test" };
    if (url.pathname === "/api/web-chat/session") return signedIn ? json(auth) : route.fulfill({ status: 401, json: {} });
    if (url.pathname === "/api/web-chat/auth") { signedIn = true; return json(auth); }
    if (url.pathname === "/api/web-chat/settings") return json({ commands: [] });
    if (url.pathname === "/api/web-chat/sessions") {
      if (request.method() === "GET") {
        listStarted();
        await new Promise(resolve => { releaseList = resolve; });
        return json({ sessions, event_cursor: "1", threads_revision: "saved-chats" });
      }
      creations++;
      await new Promise(resolve => { pendingCreations.push(resolve); });
      const session = { session_key: "unwanted", conversation_id: "conversation_unwanted" };
      sessions.push(session);
      return json({ session });
    }
    if (url.pathname.endsWith("/messages")) return json({ messages: [{
      message_id: "history:" + url.pathname, session_sequence: 1, role: "assistant",
      content: url.pathname.includes("/selected/") ? "Existing discussion context" : "Earlier chat",
    }], has_more: false });
    if (url.pathname === "/api/web-chat/chat") {
      const send = request.postDataJSON();
      sends.push(send);
      return json({ session_key: send.session_key, conversation_id: send.conversation_id, messages: [
        { message_id: "accepted", client_request_id: send.client_request_id, session_sequence: 2, role: "user", content: send.message },
        { message_id: "answer", session_sequence: 3, role: "assistant", content: "Reply in the saved discussion" },
      ] });
    }
    if (url.pathname === "/api/web-chat/events/stream") return route.fulfill({ contentType: "text/event-stream",
      body: 'data: {"status":"ok","messages":[],"next_cursor":"1","transient_actions":[]}\n\n' });
    if (url.pathname.startsWith("/api/")) return json({});
    const file = path.join(assets, url.pathname === "/webchat" ? "chat.html" : path.basename(url.pathname));
    return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404 });
  });
  try {
    await page.goto("http://startup.test/webchat");
    await page.locator("#chat-auth-token").fill("fixture-token");
    await page.locator("#chat-auth-form").evaluate(form => form.requestSubmit());
    await listing;
    if (earlySettings) {
      // The shell is already interactive while its first saved-thread lookup waits.
      await page.locator("#settings-button").click();
      releaseList();
    }
    await page.getByRole("button", { name: "Selected saved chat", exact: true }).waitFor();
    if (earlySettings) await page.locator("#settings-dialog-close").evaluate(button => button.click());
    await page.getByRole("button", { name: "Selected saved chat", exact: true }).click();
    await page.getByText("Existing discussion context", { exact: true }).waitFor();
    if (pendingCreations.length) {
      const created = page.waitForResponse(response => response.request().method() === "POST"
        && new URL(response.url()).pathname === "/api/web-chat/sessions");
      pendingCreations.forEach(release => release());
      await created;
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
    }
    assert.equal(creations, 0, "Opening Settings during sign-in must reuse saved chats");
    assert.equal(await page.locator(".thread-row.is-active .thread-button").textContent(), "Selected saved chat");
    assert.equal(await page.getByRole("switch", { name: "Launch parallel thread" }).isChecked(), false);
    await page.locator("#message-input").fill("Continue the existing discussion");
    await page.getByRole("button", { name: "Send message", exact: true }).click();
    await page.getByText("Reply in the saved discussion", { exact: true }).waitFor();
    if (!earlySettings) {
      const listed = page.waitForResponse(response => response.request().method() === "GET"
        && new URL(response.url()).pathname === "/api/web-chat/sessions");
      releaseList();
      await listed;
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
    }
    assert.equal(sends.length, 1);
    assert.equal(sends[0].session_key, "selected");
    assert.equal(sends[0].conversation_id, "conversation_selected");
    assert.equal(await page.locator(".thread-row.is-active .thread-button").textContent(), "Selected saved chat");
    assert.equal(await page.locator(".thread-row").count(), 2);
    assert.deepEqual(errors, []);
    console.log(`PASS: sign-in with ${earlySettings ? "early Settings" : "cached chats and an early reply"} keeps the selected conversation`);
  } finally {
    if (releaseList) releaseList();
    pendingCreations.forEach(release => release());
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    await checkSignIn(browser, false);
    await checkSignIn(browser, true);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
