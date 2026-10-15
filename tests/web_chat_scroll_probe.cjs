"use strict";

/**
 * Chromium probe: a large workflow card must not pull the Web Chat transcript
 * away from the latest message.
 *
 * The workflow card loads its content after it is added to the page. Each
 * transcript render used to rebuild it as a short "Loading…" block, scroll to
 * the bottom, and then let the card grow back, which pushed the latest
 * messages below the visible area. This probe checks the person's view when
 * they send a message, when a reply arrives, and when the card's status
 * changes, both when they follow the latest message and when they have
 * scrolled up to read. Replies never move a reader; sending a message shows it
 * at the bottom even after scrolling up.
 */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const assets = path.join(__dirname, "../assistant_gateway/onboarding_web/assets");
const SESSION = "session_1";
const WORKFLOW_ID = "access_flow_demo";
// The card fills in a little after it is added, like a real network read.
const WORKFLOW_DELAY_MS = 250;
// Long enough for the card to load and every late layout change to finish.
const SETTLE_MS = 900;

function workflowRecord(stepState) {
  const steps = Array.from({ length: 7 }, (_, index) => ({
    step_id: `step_${index}`, title: `Step ${index + 1}`, kind: "access",
    state: index === 0 ? stepState : "pending",
    reason: "This step gives the agent the access it needs. ".repeat(3),
    success_criterion: "The access works.",
    depends_on: index ? [`step_${index - 1}`] : [],
    requests: [], setups: [],
  }));
  return { workflow: { workflow_id: WORKFLOW_ID, title: "Your Access Plan",
    target_agent_type: "Researcher", goal: "Read the shared drive.", steps } };
}

function transcriptMessage(sequence, role, content) {
  return { message_id: `${SESSION}_${sequence}`, session_key: SESSION, session_sequence: sequence,
    event_sequence: sequence, role, content, created_at_utc: "2026-09-12T00:00:00Z" };
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, reducedMotion: "reduce" });
  const errors = [];
  let events = [], nextCursor = 1, sequence = 12, stepState = "running", workflowReads = 0;
  const history = Array.from({ length: 12 }, (_, index) => {
    const number = index + 1;
    if (number === 10) {
      return transcriptMessage(number, "assistant", `Here is the plan: [Open the plan](/workflows/${WORKFLOW_ID})`);
    }
    return transcriptMessage(number, number % 2 ? "user" : "assistant",
      `Message ${number}: ${"Transcript content for scrolling. ".repeat(number > 10 ? 1 : 8)}`);
  });
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem("ua.webchat.ui.scroll_user.selected", "session_1");
  });
  await page.route("http://scroll.test/**", async route => {
    const url = new URL(route.request().url());
    const json = body => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/web-chat/session") {
      return json({ canonical_user_id: "scroll_user", assistant_name: "Test Agent", permission_csrf_token: "test" });
    }
    if (url.pathname === "/api/web-chat/settings") return json({ assistant_name: "Test Agent", commands: [] });
    if (url.pathname === "/api/web-chat/sessions") {
      return json({ sessions: [{ session_key: SESSION, title: "Plan", updated_at: "2026-09-12T00:00:00Z" }],
        event_cursor: "1" });
    }
    if (url.pathname === "/api/web-chat/events/stream") {
      const messages = events;
      events = [];
      return route.fulfill({ contentType: "text/event-stream", body: "data: " + JSON.stringify({
        status: "ok", messages, next_cursor: String(nextCursor++), transient_actions: [] }) + "\n\n" });
    }
    if (url.pathname === `/api/web-chat/workflows/${WORKFLOW_ID}`) {
      workflowReads += 1;
      await new Promise(resolve => setTimeout(resolve, WORKFLOW_DELAY_MS));
      return json(workflowRecord(stepState));
    }
    if (url.pathname === `/api/web-chat/sessions/${SESSION}/messages`) {
      return json({ messages: history, before_cursor: "", has_more: false });
    }
    if (url.pathname === "/api/web-chat/chat") return json({ session_key: SESSION, messages: [] });
    if (url.pathname.startsWith("/api/")) return json({});
    const file = path.join(assets, url.pathname === "/webchat" ? "chat.html" : path.basename(url.pathname));
    return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404 });
  });

  const list = page.locator("#message-list");
  const distanceFromBottom = () => list.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight);
  const settle = () => page.waitForTimeout(SETTLE_MS);
  const replyFromAgent = async text => {
    sequence += 1;
    const id = `${SESSION}_${sequence}`;
    events = [transcriptMessage(sequence, "assistant", text)];
    await page.waitForFunction(messageId => document.querySelector(`[data-message-id="${messageId}"]`), id);
  };
  const expectAtLatest = async step => {
    await settle();
    const distance = await distanceFromBottom();
    assert.ok(distance < 2, `${step}: the view is ${distance}px above the latest message`);
  };
  // The first transcript row the person sees, and how far it sits from the top.
  const visibleAnchor = () => list.evaluate(el => {
    const top = el.getBoundingClientRect().top;
    const row = [...el.querySelectorAll("[data-message-id]")].find(item => item.getBoundingClientRect().bottom > top);
    return { id: row.dataset.messageId, offset: row.getBoundingClientRect().top - top };
  });
  const anchorOffset = id => list.evaluate((el, messageId) => {
    const row = el.querySelector(`[data-message-id="${messageId}"]`);
    return row.getBoundingClientRect().top - el.getBoundingClientRect().top;
  }, id);

  try {
    await page.goto("http://scroll.test/webchat");
    await page.waitForFunction(() => document.querySelector(".workflow-inline-card .workflow-node"));
    const cardHeight = await page.locator(".workflow-inline-card").evaluate(card => card.getBoundingClientRect().height);
    assert.ok(cardHeight > 900, `the probe needs a card taller than the view (${cardHeight}px)`);
    await expectAtLatest("After the transcript opens");

    // Following the latest message: the person's own message stays in view.
    await list.evaluate(el => { el.scrollTop = el.scrollHeight; });
    await settle();
    await page.locator("#message-input").fill("Please continue.");
    await page.getByRole("button", { name: "Send message" }).click();
    await page.waitForFunction(() => [...document.querySelectorAll("[data-message-id]")]
      .some(row => row.textContent.includes("Please continue.")));
    await expectAtLatest("After the person sends a message");

    // Following the latest message: an agent reply stays in view.
    await replyFromAgent("Here is the next part of the answer.");
    await expectAtLatest("After a reply arrives");

    // Following the latest message: a change to the card's status stays in view.
    stepState = "complete";
    await replyFromAgent("The first step is complete.");
    await page.waitForFunction(() => document.querySelector('.workflow-node[data-state="complete"]'));
    await expectAtLatest("After the card's status changes");

    // More replies leave enough transcript below the card to read above the bottom.
    for (let index = 0; index < 6; index += 1) {
      await replyFromAgent(`Longer reply ${index}: ${"More detail about the plan. ".repeat(30)}`);
    }
    await expectAtLatest("After several long replies");

    // Reading earlier messages with the card's end in view: every render
    // rebuilds the card, and the rows the person is reading must stay put.
    const cardBottom = await page.locator(".workflow-inline-card").evaluate(card => {
      const row = card.closest("[data-message-id]");
      return row.offsetTop + row.offsetHeight;
    });
    await list.evaluate((el, bottom) => { el.scrollTop = bottom - 40; }, cardBottom);
    await settle();
    assert.ok(await distanceFromBottom() > 400, "The probe needs a reading position well above the bottom");
    const anchor = await visibleAnchor();
    const readsBefore = workflowReads;
    await replyFromAgent("A reply while the person reads earlier messages.");
    await settle();
    const offset = await anchorOffset(anchor.id);
    assert.ok(Math.abs(offset - anchor.offset) < 2, `The rows the person was reading moved ${offset - anchor.offset}px`);
    assert.ok(await distanceFromBottom() > 100, "A reply must not pull a reading person to the bottom");
    assert.ok(workflowReads > readsBefore, "Each render still refreshes the card's current status");

    // Reading with the card itself in view: it must not move either.
    const cardTop = await page.locator(".workflow-inline-card").evaluate(card => card.closest("[data-message-id]").offsetTop);
    await list.evaluate((el, top) => { el.scrollTop = top + 200; }, cardTop);
    await settle();
    const cardAnchor = await visibleAnchor();
    await replyFromAgent("Another reply while the card is in view.");
    await settle();
    const cardOffset = await anchorOffset(cardAnchor.id);
    assert.ok(Math.abs(cardOffset - cardAnchor.offset) < 2, `The card the person was reading moved ${cardOffset - cardAnchor.offset}px`);

    // Scrolling back to the bottom resumes following the latest message.
    await list.evaluate(el => { el.scrollTop = el.scrollHeight; });
    await settle();
    await replyFromAgent("Back at the latest message.");
    await expectAtLatest("After the person returns to the bottom");

    // Sending while reading earlier messages shows the sent message at the
    // bottom, and the replies that follow keep it in view.
    await list.evaluate((el, top) => { el.scrollTop = top; }, cardTop);
    await settle();
    assert.ok(await distanceFromBottom() > 400, "The person is reading well above the bottom");
    await page.locator("#message-input").fill("One more question.");
    await page.getByRole("button", { name: "Send message" }).click();
    await page.waitForFunction(() => [...document.querySelectorAll("[data-message-id]")]
      .some(row => row.textContent.includes("One more question.")));
    await expectAtLatest("After the person sends a message while scrolled up");
    await replyFromAgent("The answer to one more question.");
    await expectAtLatest("After a reply to the message sent while scrolled up");
    assert.deepEqual(errors, []);
    console.log("Web Chat: the view follows the latest message past a large workflow card and stays put while reading.");
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
