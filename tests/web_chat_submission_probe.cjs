"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const assets = path.join(__dirname, "../assistant_gateway/onboarding_web/assets");

async function main() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, reducedMotion: "reduce" });
  page.setDefaultTimeout(10000);
  const errors = [], sends = [];
  const sessions = [{ session_key: "original", title: "Earlier request", conversation_id: "original_conversation" }];
  let creations = 0, failCreation = true, failSend = true;
  page.on("pageerror", error => errors.push(error.message));
  await page.route("http://submission.test/**", async route => {
    const request = route.request(), url = new URL(request.url());
    const json = body => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/web-chat/session") {
      return json({ canonical_user_id: "example-user", assistant_name: "Nova", permission_csrf_token: "test" });
    }
    if (url.pathname === "/api/web-chat/settings") return json({ assistant_name: "Nova", commands: [] });
    if (url.pathname === "/api/web-chat/sessions") {
      if (request.method() !== "POST") return json({ sessions, event_cursor: "1" });
      creations++;
      if (failCreation) {
        failCreation = false;
        return route.fulfill({ status: 503, body: "Cannot create the chat yet" });
      }
      const session = { session_key: `separate_${creations}`, conversation_id: `conversation_${creations}` };
      sessions.push(session);
      return json({ session });
    }
    if (url.pathname.endsWith("/messages")) return json({ messages: [], has_more: false });
    if (url.pathname === "/api/web-chat/events/stream") {
      return route.fulfill({ contentType: "text/event-stream", body:
        'data: {"status":"ok","messages":[],"next_cursor":"1","transient_actions":[]}\n\n' });
    }
    if (url.pathname === "/api/web-chat/chat") {
      const body = request.postDataBuffer().toString();
      const field = name => body.match(new RegExp(`name="${name}"\\r\\n\\r\\n([^\\r]*)`))?.[1];
      const send = request.headers()["content-type"].startsWith("application/json")
        ? JSON.parse(body) : { session_key: field("session_key"), client_request_id: field("client_request_id"), message: field("message") };
      sends.push({ ...send, body });
      if (failSend) { failSend = false; return route.abort("connectionreset"); }
      if (send.message === "Another request") {
        return json({ session_key: send.session_key, messages: [
          { message_id: "accepted-later", client_request_id: send.client_request_id, session_sequence: 1, role: "user", content: send.message },
        ] });
      }
      return json({ session_key: send.session_key, messages: [
        { message_id: "accepted", client_request_id: send.client_request_id, session_sequence: 2, role: "user", content: send.message },
        { message_id: "answer", session_sequence: 3, role: "assistant", content: "Reply in the separate chat" },
      ] });
    }
    if (url.pathname.startsWith("/api/")) return json({});
    const file = path.join(assets, url.pathname === "/webchat" ? "chat.html" : path.basename(url.pathname));
    return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404 });
  });
  try {
    await page.goto("http://submission.test/webchat");
    const input = page.locator("#message-input"), separate = page.getByRole("switch", { name: "Separate request" });
    await page.getByRole("button", { name: "Earlier request", exact: true }).waitFor();
    assert.equal(await separate.isChecked(), false);
    await input.fill("New research request");
    await page.locator("#message-attachment-input").setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("Research notes") });
    await separate.focus();
    await page.keyboard.press("Space");
    assert.equal(await separate.isChecked(), true, "keyboard operates the switch");
    await page.getByRole("button", { name: "Send message", exact: true }).click();
    await page.getByRole("button", { name: "Retry", exact: true }).waitFor();
    assert.equal(await input.inputValue(), "New research request");
    assert.equal(await page.locator(".composer-attachment").count(), 1);
    assert.equal(await separate.isChecked(), true);
    assert.equal(sends.length, 0);
    await page.getByRole("button", { name: "Retry", exact: true }).click();
    await page.getByRole("button", { name: "New research request", exact: true }).waitFor();
    await page.getByRole("button", { name: "Retry", exact: true }).waitFor();
    assert.equal(await separate.isChecked(), false);
    assert.equal(await input.inputValue(), "");
    assert.equal(sends.length, 1);
    assert.equal(sends[0].session_key, "separate_2");
    assert.ok(sends[0].body.includes('filename="notes.txt"'));
    assert.ok(sends[0].body.includes("Research notes"));
    // A later send and its acknowledgement must not erase the failed upload's Retry.
    await input.fill("Another request");
    await page.getByRole("button", { name: "Send message", exact: true }).click();
    await page.locator('[data-message-id="accepted-later"]').waitFor();
    assert.equal(await page.getByRole("button", { name: "Retry", exact: true }).count(), 1,
      "the failed upload stays retryable after another message is accepted");
    // A new draft is independent of retrying the previous, frozen message.
    await input.fill("Draft for later");
    await page.getByRole("button", { name: "Retry", exact: true }).click();
    await page.getByText("Reply in the separate chat", { exact: true }).waitFor();
    assert.equal(sends.length, 3);
    assert.equal(creations, 2, "Retry never creates another destination");
    assert.equal(sends[2].client_request_id, sends[0].client_request_id);
    assert.equal(sends[2].session_key, sends[0].session_key);
    assert.ok(sends[2].body.includes("Research notes"));
    assert.equal(await input.inputValue(), "Draft for later");
    assert.equal(await page.locator('[data-message-id="accepted"]').count(), 1);
    await page.getByRole("button", { name: "Earlier request", exact: true }).click();
    assert.equal(await page.getByText("Reply in the separate chat", { exact: true }).count(), 0);
    assert.equal(await page.getByText("New research request", { exact: true }).count(), 1, "only the other chat's sidebar name remains");
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 900 });
      for (const mode of ["light", "dark"]) {
        await page.evaluate(mode => document.documentElement.dataset.colorMode = mode, mode);
        assert.equal(await separate.isVisible(), true);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      }
    }
    // Reload restores cached sidebar metadata and must initialize fresh retry state.
    await page.reload();
    await page.getByRole("button", { name: "Earlier request", exact: true }).waitFor();
    await input.fill("Another request");
    await page.getByRole("button", { name: "Send message", exact: true }).click();
    await page.locator('[data-message-id="accepted-later"]').waitFor();
    assert.equal(sends.length, 4);
    assert.equal(sends[3].session_key, "original");
    assert.deepEqual(errors, []);
    console.log("Separate request: keyboard, text/files, failed creation, stable Retry, reply routing and narrow-screen themes passed.");
  } finally { await browser.close(); }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
