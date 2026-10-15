"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const assets = path.join(__dirname, "../assistant_gateway/onboarding_web/assets");

async function main() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, reducedMotion: "reduce" });
  const requests = [], errors = [];
  let events = [], nextCursor = 1;
  const sessions = Array.from({ length: 50 }, (_, index) => ({
    session_key: `session_${index}`, title: `Thread ${index}`,
    updated_at: "2026-09-12T00:00:00Z",
  }));
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem("ua.webchat.ui.history_user.selected", "session_17");
  });
  await page.route("http://history.test/**", async route => {
    const url = new URL(route.request().url());
    const json = body => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/web-chat/session") {
      return json({ canonical_user_id: "history_user", assistant_name: "Test Agent", permission_csrf_token: "test" });
    }
    if (url.pathname === "/api/web-chat/settings") return json({ assistant_name: "Test Agent", commands: [] });
    if (url.pathname === "/api/web-chat/sessions") return json({ sessions, event_cursor: "1" });
    if (url.pathname === "/api/web-chat/events/stream") {
      const messages = events;
      events = [];
      return route.fulfill({ contentType: "text/event-stream", body: "data: " + JSON.stringify({ status: "ok", messages, next_cursor: String(nextCursor++), transient_actions: [] }) + "\n\n" });
    }
    const match = url.pathname.match(/\/sessions\/(session_\d+)\/messages$/);
    if (match) {
      requests.push(url.pathname + url.search);
      const end = Number(url.searchParams.get("before") || 5000);
      const messages = Array.from({ length: 50 }, (_, index) => {
        const sequence = end - 49 + index;
        return { message_id: `${match[1]}_${sequence}`, session_key: match[1],
          session_sequence: sequence, event_sequence: sequence, role: "user",
          content: `Message ${sequence}: ${"History content for scrolling. ".repeat(6)}`,
          created_at_utc: "2026-09-12T00:00:00Z" };
      });
      return json({ messages, before_cursor: String(end - 50), has_more: end > 50 });
    }
    if (url.pathname.startsWith("/api/")) return json({});
    const file = path.join(assets, url.pathname === "/webchat" ? "chat.html" : path.basename(url.pathname));
    return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404 });
  });
  try {
    await page.goto("http://history.test/webchat");
    await page.waitForFunction(() => document.querySelectorAll("[data-message-id]").length === 50);
    assert.deepEqual(requests, ["/api/web-chat/sessions/session_17/messages?limit=50"]);
    const list = page.locator("#message-list");
    assert.ok(await list.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight < 2));

    // An upward scroll must prepend one bounded page and retain its visible anchor.
    const anchor = await list.evaluate(el => {
      el.scrollTop = 120;
      const top = el.getBoundingClientRect().top;
      const row = [...el.querySelectorAll("[data-message-id]")].find(row => row.getBoundingClientRect().bottom > top);
      return { id: row.dataset.messageId, offset: row.getBoundingClientRect().top - top };
    });
    await page.waitForFunction(() => document.querySelectorAll("[data-message-id]").length === 100);
    const offset = await list.evaluate((el, id) => {
      const row = [...el.querySelectorAll("[data-message-id]")].find(row => row.dataset.messageId === id);
      return row.getBoundingClientRect().top - el.getBoundingClientRect().top;
    }, anchor.id);
    assert.ok(Math.abs(offset - anchor.offset) < 2, `Anchor moved ${offset - anchor.offset}px`);
    assert.equal(requests.length, 2, "scrolling must not drain the whole transcript");

    // A new event for another thread updates unread state without moving the reader.
    const beforeEvent = await list.evaluate(el => el.scrollTop);
    events = [{ message_id: "background_reply", session_key: "session_3",
      session_sequence: 5001, event_sequence: 5001, role: "assistant", content: "New reply" }];
    await page.waitForFunction(() => [...document.querySelectorAll(".thread-row.has-unread")]
      .some(row => row.textContent.includes("Thread 3")));
    assert.equal(await list.evaluate(el => el.scrollTop), beforeEvent);
    assert.equal(requests.length, 2);

    await page.getByRole("button", { name: "Thread 3", exact: true }).click();
    await page.waitForFunction(() => document.querySelector('[data-message-id="session_3_5000"]'));
    assert.equal(requests.length, 3);
    assert.equal(await page.locator("[data-message-id]").count(), 50);
    assert.equal(requests.at(-1), "/api/web-chat/sessions/session_3/messages?limit=50");
    assert.deepEqual(errors, []);
    console.log("Web Chat: active-only hydration, upward pagination, anchor preservation and background unread checks passed.");
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
