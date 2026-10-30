"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const assets = path.join(__dirname, "../assistant_gateway/onboarding_web/assets");
const LOCKED_NOTE = "Available after this chat replies.";
const LOCKED_REFUSAL = "This chat is working on a request. You can change Run concurrently after it replies.";

function deferred() {
  let resolve;
  const promise = new Promise(yes => { resolve = yes; });
  return { promise, resolve };
}

// A Web Chat page against a scripted server. `server.sessions` and the hooks
// on `server` decide what each request gets back.
async function openChat(browser, sessions, { openRequests = [] } = {}) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, reducedMotion: "reduce" });
  page.setDefaultTimeout(10000);
  const server = { sessions, sends: [], patches: [], creations: 0, errors: [], holdSend: null, failSend: false,
    refusePatch: null };
  page.on("pageerror", error => server.errors.push(error.message));
  await page.route("http://submission.test/**", async route => {
    const request = route.request(), url = new URL(request.url());
    const json = body => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/web-chat/session") {
      return json({ canonical_user_id: "example-user", assistant_name: "Nova", permission_csrf_token: "test" });
    }
    if (url.pathname === "/api/web-chat/settings") return json({ assistant_name: "Nova", commands: [] });
    if (url.pathname === "/api/web-chat/sessions") {
      if (request.method() !== "POST") return json({ sessions: server.sessions, event_cursor: "1", threads_revision: "r1" });
      server.creations++;
      const session = { session_key: `created_${server.creations}`, conversation_id: `conversation_${server.creations}`,
        parallel_thread: false };
      server.sessions.push(session);
      return json({ session });
    }
    if (request.method() === "PATCH" && url.pathname.startsWith("/api/web-chat/sessions/")) {
      const body = JSON.parse(request.postData());
      server.patches.push({ session_key: decodeURIComponent(url.pathname.split("/").pop()), ...body });
      if (server.refusePatch) {
        const text = server.refusePatch;
        server.refusePatch = null;
        return route.fulfill({ status: 409, contentType: "text/plain", body: text });
      }
      const session = server.sessions.find(item => item.session_key === server.patches.at(-1).session_key);
      session.parallel_thread = body.parallel_thread;
      return json({ status: "ok", ...body });
    }
    if (url.pathname.endsWith("/messages")) return json({ messages: url.pathname.includes("/original/") ? [
      { message_id: "earlier", session_sequence: 1, role: "assistant", content: "Earlier conversation context" },
    ] : [], has_more: false });
    if (url.pathname === "/api/web-chat/events/stream") {
      const frame = { status: "ok", messages: [], next_cursor: "1", transient_actions: [], threads_revision: "r1",
        busy_session_keys: [], open_request_session_keys: openRequests };
      return route.fulfill({ contentType: "text/event-stream", body: `data: ${JSON.stringify(frame)}\n\n` });
    }
    if (url.pathname === "/api/web-chat/chat") {
      const body = request.postDataBuffer().toString();
      const field = name => body.match(new RegExp(`name="${name}"\\r\\n\\r\\n([^\\r]*)`))?.[1];
      const send = request.headers()["content-type"].startsWith("application/json")
        ? JSON.parse(body) : { session_key: field("session_key"), client_request_id: field("client_request_id"), message: field("message") };
      server.sends.push({ ...send, body });
      if (server.holdSend) await server.holdSend.promise;
      if (server.failSend) { server.failSend = false; return route.abort("connectionreset"); }
      if (send.message === "Another request") {
        return json({ session_key: send.session_key, messages: [
          { message_id: "accepted-later", client_request_id: send.client_request_id, session_sequence: 4, role: "user", content: send.message },
        ] });
      }
      return json({ session_key: send.session_key, messages: [
        { message_id: "accepted", client_request_id: send.client_request_id, session_sequence: 2, role: "user", content: send.message },
        { message_id: "answer", session_sequence: 3, role: "assistant", content: "Reply with the earlier context" },
      ] });
    }
    if (url.pathname.startsWith("/api/")) return json({});
    const file = path.join(assets, url.pathname === "/webchat" ? "chat.html" : path.basename(url.pathname));
    return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404 });
  });
  await page.goto("http://submission.test/webchat");
  const controls = {
    input: page.locator("#message-input"),
    toggle: page.getByRole("switch", { name: "Run concurrently" }),
    note: page.locator("#separate-request-message"),
    send: page.getByRole("button", { name: "Send message", exact: true }),
    retry: page.getByRole("button", { name: "Retry", exact: true }),
  };
  return { page, server, ...controls };
}

async function aThreadKeepsItsSettingThroughSendsAndRetry(browser) {
  const chat = await openChat(browser, [
    { session_key: "original", title: "Earlier request", conversation_id: "original_conversation", parallel_thread: false },
    { session_key: "other", title: "Other chat", conversation_id: "other_conversation", parallel_thread: true },
  ]);
  const { page, server, input, toggle, note, send, retry } = chat;
  await page.getByRole("button", { name: "Earlier request", exact: true }).waitFor();
  await page.getByText("Earlier conversation context", { exact: true }).waitFor();
  assert.equal(await toggle.isChecked(), false);

  // The switch saves the selected thread's setting at once, from the keyboard too.
  await toggle.focus();
  await page.keyboard.press("Space");
  await page.waitForFunction(() => document.getElementById("separate-request-toggle").checked);
  assert.deepEqual(server.patches.at(-1), { session_key: "original", parallel_thread: true, conversation_id: "original_conversation" });
  await page.getByRole("button", { name: "Other chat", exact: true }).click();
  assert.equal(await toggle.isChecked(), true, "each thread shows its own saved setting");
  await page.getByRole("button", { name: "Earlier request", exact: true }).click();
  await page.getByText("Earlier conversation context", { exact: true }).waitFor();
  assert.equal(await toggle.isChecked(), true);

  // While a message is on its way the switch is locked, and it keeps its position.
  server.holdSend = deferred();
  server.failSend = true;
  await input.fill("New research request");
  await page.locator("#message-attachment-input").setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("Research notes") });
  await send.click();
  await page.waitForFunction(() => document.getElementById("separate-request-toggle").disabled);
  assert.equal(await note.textContent(), LOCKED_NOTE);
  assert.equal(await toggle.isChecked(), true);
  server.holdSend.resolve();
  server.holdSend = null;
  await retry.waitFor();
  assert.equal(await toggle.isDisabled(), false, "a failed send leaves the thread free to change");
  assert.equal(await toggle.isChecked(), true, "the thread still shows Run concurrently after a failed send");
  assert.equal(await note.isHidden(), true);
  assert.equal(server.sends.length, 1);
  assert.equal(server.sends[0].session_key, "original");
  assert.equal(server.creations, 0, "a reply keeps the selected conversation");
  assert.ok(!server.sends[0].body.includes("parallel_thread"), "a message carries no setting");
  assert.ok(server.sends[0].body.includes('filename="notes.txt"'));
  assert.equal(await page.getByText("Earlier conversation context", { exact: true }).count(), 1);

  // A refused change puts the saved position back and says why, in red.
  server.refusePatch = LOCKED_REFUSAL;
  await toggle.click();
  await page.locator("#separate-request-message.is-error").waitFor();
  assert.equal(await note.textContent(), `Run concurrently was not changed. ${LOCKED_REFUSAL}`);
  assert.equal(await note.getAttribute("role"), "alert");
  assert.equal(await toggle.isChecked(), true);

  // The person picks the state before Retry: here they turn it off first.
  await toggle.click();
  await page.waitForFunction(() => !document.getElementById("separate-request-toggle").checked);
  assert.equal(server.patches.at(-1).parallel_thread, false);
  assert.equal(await note.isHidden(), true, "a new attempt replaces the earlier error");
  await input.fill("Another request");
  await send.click();
  await page.locator('[data-message-id="accepted-later"]').waitFor();
  assert.equal(await retry.count(), 1, "the failed upload stays retryable after another message is accepted");
  await input.fill("Draft for later");
  await retry.click();
  await page.getByText("Reply with the earlier context", { exact: true }).waitFor();
  assert.equal(server.sends.length, 3);
  assert.equal(server.creations, 0, "Retry never creates another destination");
  assert.equal(server.sends[2].client_request_id, server.sends[0].client_request_id);
  assert.equal(server.sends[2].session_key, "original");
  assert.ok(server.sends[2].body.includes("Research notes"));
  assert.equal(await input.inputValue(), "Draft for later");
  assert.equal(await toggle.isChecked(), false, "sending never moves the switch");

  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 900 });
    for (const mode of ["light", "dark"]) {
      await page.evaluate(mode => document.documentElement.dataset.colorMode = mode, mode);
      assert.equal(await toggle.isVisible(), true);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    }
  }
  // A reload shows the saved settings again and starts fresh retry state.
  await page.reload();
  await page.getByRole("button", { name: "Earlier request", exact: true }).waitFor();
  assert.equal(await toggle.isChecked(), false);
  await input.fill("Another request");
  await send.click();
  await page.locator('[data-message-id="accepted-later"]').waitFor();
  assert.equal(server.sends.at(-1).session_key, "original");
  assert.deepEqual(server.errors, []);
  await page.close();
}

async function anOpenRequestLocksTheSwitch(browser) {
  const chat = await openChat(browser, [
    { session_key: "busy", title: "Busy chat", conversation_id: "busy_conversation", parallel_thread: true },
  ], { openRequests: ["busy"] });
  const { page, server, toggle, note } = chat;
  await page.getByRole("button", { name: "Busy chat", exact: true }).waitFor();
  await page.waitForFunction(() => document.getElementById("separate-request-toggle").disabled);
  assert.equal(await toggle.isChecked(), true);
  assert.equal(await note.textContent(), LOCKED_NOTE);
  await page.setViewportSize({ width: 390, height: 900 });
  assert.equal(await note.isVisible(), true);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.deepEqual(server.patches, []);
  assert.deepEqual(server.errors, []);
  await page.close();
}

async function aFirstMessageCreatesTheChatWithTheSwitchsSetting(browser) {
  const chat = await openChat(browser, []);
  const { page, server, input, toggle, send } = chat;
  await toggle.waitFor();
  await toggle.click();
  assert.equal(await toggle.isChecked(), true);
  assert.deepEqual(server.patches, [], "nothing to save until the chat exists");
  await input.fill("Plan the trip");
  await send.click();
  await page.getByText("Reply with the earlier context", { exact: true }).waitFor();
  assert.equal(server.creations, 1);
  assert.deepEqual(server.patches, [{ session_key: "created_1", parallel_thread: true, conversation_id: "conversation_1" }]);
  assert.equal(server.sends[0].session_key, "created_1");
  assert.equal(await toggle.isChecked(), true, "the new chat keeps Run concurrently");
  assert.deepEqual(server.errors, []);
  await page.close();
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  try {
    await aThreadKeepsItsSettingThroughSendsAndRetry(browser);
    await anOpenRequestLocksTheSwitch(browser);
    await aFirstMessageCreatesTheChatWithTheSwitchsSetting(browser);
    console.log("Run concurrently: per-thread setting, keyboard, lock while sending or open, kept after a failed send, refusal, Retry, first chat and narrow-screen themes passed.");
  } finally { await browser.close(); }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
