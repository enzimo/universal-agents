// Drives the real Web Chat page with broker cards in headless Chromium
// (tests/test_broker_card_browser.py starts the broker, the chat preview and
// the proxy that adds the gateway's policy). Prints the checks it passed as JSON.
//   node broker_card_browser_runner.cjs '{"mode": "cards", "base": "<chat origin>", "typed": "<value>", "follow_up": "<page>"}'
//   node broker_card_browser_runner.cjs '{"mode": "sign_in", "base": "<chat origin>"}'
const {chromium} = require("playwright");

const CARD = (index) => `[data-item-id="tool_setup:preview-broker-page-${index}"]`;
const FRAME = "iframe.broker-card-frame";
const OPEN_LINK = ".broker-card-open";
const MESSAGE_BOX = "#message-input";
const DONE = "Done. This secure page is closed.";
// The preview conversation that holds the broker cards. The thread list also has sample threads
// (scripts/preview_web_chat_threads.py) and opens the first of them, so the probe picks this row
// by its session key.
const PREVIEW_THREAD_ROW = '.thread-row[data-session-key="website:web-chat:dev"] .thread-button';

function checker() {
  const passed = [];
  const check = (condition, name) => {
    if (!condition) throw new Error(`failed: ${name}`);
    passed.push(name);
  };
  return {passed, check};
}

async function openChat(browser, base) {
  const context = await browser.newContext({viewport: {width: 1280, height: 900}});
  await context.addCookies([{name: "ua_web_chat_session", value: "dev-preview-session", url: base}]);
  const page = await context.newPage();
  // Record every message a card frame posts to the chat page (opaque origin "null").
  await page.addInitScript(() => {
    window.__cardMessages = [];
    window.addEventListener("message", (event) => {
      if (event.origin === "null") window.__cardMessages.push(JSON.parse(JSON.stringify(event.data)));
    });
  });
  return page;
}

async function openPreviewConversation(page, base) {
  await page.goto(`${base}/webchat`);
  await page.locator(PREVIEW_THREAD_ROW).click();
}

const isActive = (page, selector) =>
  page.evaluate((target) => document.activeElement === document.querySelector(target), selector);

// What has focus inside a card's frame: a button's label or a field's type, or "" when focus is elsewhere.
async function focusedInFrame(page, card) {
  if (!(await isActive(page, `${card} ${FRAME}`))) return "";
  const frame = await (await page.$(`${card} ${FRAME}`)).contentFrame();
  return frame.evaluate(() => {
    const active = document.activeElement;
    if (!active || active === document.body) return "";
    return active.tagName === "BUTTON" ? active.textContent.trim() : active.getAttribute("type") || active.tagName;
  });
}

// One access proposal waiting in the Inbox panel; approving it opens one credential form (F2).
function inboxRoutes(page, followUp) {
  let decided = false;
  const review = {
    request_id: "req-proposal", title: "TeamLead proposes adding Portainer", agent_type: "TeamLead",
    recipient: "TeamLead", target: "http:portainer", target_name: "Portainer", level: "read",
    level_words: "read", lifetime: "until_revoked", lifetime_words: "until someone removes it",
    expires_at_utc: null, reason: "List the stacks.", working_for: "person-1", person_lacks_access: false,
    task_id: "task-1", status: "waiting", created_at_utc: new Date().toISOString(), can_decide: true,
    choices: [{choice: "approve", label: "Approve"}, {choice: "deny", label: "Deny"}],
  };
  return page.route("**/api/web-chat/access-inbox**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/web-chat/access-inbox") {
      return route.fulfill({json: {
        status: "ok", reviews: decided ? [] : [review], notices: [], notices_offset: 0, notices_total: 0,
        notices_all_total: 0, unread_notices: 0,
      }});
    }
    if (url.pathname.endsWith("/requests/req-proposal/decision")) {
      decided = true;
      const frameUrl = new URL(followUp);
      frameUrl.searchParams.set("view", "card");
      return route.fulfill({json: {status: "ok", review: {
        ...review, status: "granted", can_decide: false, choices: [],
        follow_up_links: [{
          label: "Choose the credential for Portainer", url: followUp, frame_url: frameUrl.href,
          expires_at_utc: new Date(Date.now() + 15 * 60 * 1000).toISOString(),
        }],
      }}});
    }
    return route.continue();
  });
}

async function cards(browser, {base, typed, follow_up: followUp}) {
  const {passed, check} = checker();
  const page = await openChat(browser, base);
  await inboxRoutes(page, followUp);
  await openPreviewConversation(page, base);
  await page.waitForFunction((frame) => document.querySelectorAll(frame).length === 4, FRAME, {timeout: 30000});

  const sandboxes = await page.$$eval(FRAME, (frames) => frames.map((frame) => frame.getAttribute("sandbox")));
  check(
    sandboxes.every((tokens) => tokens.includes("allow-scripts") && !tokens.includes("allow-same-origin")),
    "sandboxed without allow-same-origin",
  );

  await page.frameLocator(`${CARD(0)} ${FRAME}`).locator("input[type=password]").waitFor();
  const unreadable = (frame) => {
    try {
      return frame.contentDocument === null && !frame.contentWindow.document;
    } catch (_error) {
      return true;
    }
  };
  const cardsUnreadable = () => page.$$eval(FRAME, (frames, source) => {
    const test = new Function("frame", `return (${source})(frame);`);
    return frames.map((frame) => test(frame));
  }, unreadable.toString());
  check((await cardsUnreadable()).every(Boolean), "chat page cannot read the card's document");

  const unsandboxed = await page.evaluate(async (selector) => {
    const source = document.querySelector(selector).getAttribute("src");
    const frame = document.createElement("iframe");
    frame.src = source;
    document.body.appendChild(frame);
    await new Promise((resolve) => frame.addEventListener("load", resolve, {once: true}));
    let blocked;
    try {
      blocked = frame.contentDocument === null || !frame.contentDocument.querySelector("input");
    } catch (_error) {
      blocked = true;
    }
    frame.remove();
    return blocked;
  }, `${CARD(0)} ${FRAME}`);
  check(unsandboxed, "an unsandboxed frame of the card address cannot be read either");

  const card = page.frameLocator(`${CARD(0)} ${FRAME}`);
  await card.locator("input[type=password]").fill(typed);
  await page.evaluate((selector) => { window.__liveFrame = document.querySelector(selector); }, `${CARD(0)} ${FRAME}`);
  for (const text of ["first update", "second update"]) {
    const before = await page.locator(".message-bubble").count();
    await page.locator(MESSAGE_BOX).fill(text);
    await page.locator("#message-form button[type=submit]").click();
    await page.waitForFunction((count) => document.querySelectorAll(".message-bubble").length > count, before, {timeout: 15000});
  }
  const sameFrame = await page.evaluate((selector) => document.querySelector(selector) === window.__liveFrame, `${CARD(0)} ${FRAME}`);
  const kept = await card.locator("input[type=password]").inputValue();
  check(sameFrame && kept === typed, "typed value kept across two chat updates in the same frame");
  // With the value in the field, the chat page still reads neither the document nor the value.
  const pageText = await page.evaluate(() => document.documentElement.outerHTML);
  check(
    (await cardsUnreadable()).every(Boolean) && !pageText.includes(typed),
    "chat page still cannot read the card or the typed value after typing and two updates",
  );

  // Keyboard (A3): from the message box back through the conversation to the first card.
  await page.focus(MESSAGE_BOX);
  let reached = false;
  for (let step = 0; step < 200 && !reached; step += 1) {
    await page.keyboard.press("Shift+Tab");
    reached = await isActive(page, `${CARD(0)} ${OPEN_LINK}`);
  }
  check(reached, "Shift+Tab from the message box reaches the card's Open in its own tab");
  await page.keyboard.press("Shift+Tab");
  check((await focusedInFrame(page, CARD(0))) === "Decline", "one more Shift+Tab goes into the frame, to Decline");
  let onButton = false;
  for (let step = 0; step < 5 && !onButton; step += 1) {
    await page.keyboard.press("Shift+Tab");
    onButton = (await focusedInFrame(page, CARD(0))) === "Store Credential";
  }
  check(onButton, "Shift+Tab reaches the form's own button");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  check(await isActive(page, `${CARD(0)} ${OPEN_LINK}`), "Tab leaves the frame to Open in its own tab");
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Shift+Tab");
  if ((await focusedInFrame(page, CARD(0))) !== "Store Credential") throw new Error("failed: back on the form's own button");
  await page.keyboard.press("Enter");
  await page.waitForFunction(
    ([selector, done]) => (document.querySelector(selector) || {}).textContent === done,
    [CARD(0), DONE],
    {timeout: 15000},
  );
  check(true, "submission with Enter from the frame reached the broker and the card shrank to Done");
  check(await isActive(page, MESSAGE_BOX), "focus moved to the message box when the card shrank");
  // The update checked below is the 30 second card expiring; it must still be open now.
  if (!(await page.$(`${CARD(2)} ${FRAME}`))) {
    throw new Error("too slow: the card meant to expire after the keyboard checks had already expired; raise EXPIRING_SECONDS");
  }

  const messages = await page.evaluate(() => window.__cardMessages);
  check(
    messages.length > 0
      && messages.every((message) => {
        const keys = Object.keys(message).sort().join(",");
        return message.type === "ua-broker-card" && (keys === "height,type" || keys === "outcome,type");
      })
      && !JSON.stringify(messages).includes(typed),
    "frames posted only height and outcome",
  );

  // The next chat update comes without the test touching the page: a card expires.
  const expired = ([selector, frame]) => {
    const node = document.querySelector(selector);
    return node && !node.querySelector(frame) && node.textContent.includes("expired");
  };
  await page.waitForFunction(expired, [CARD(2), FRAME], {timeout: 60000});
  check(true, "an expired card lost its frame");
  check(await isActive(page, MESSAGE_BOX), "focus stayed on the message box through that chat update");

  if (!(await page.$(`${CARD(3)} ${FRAME}`))) {
    throw new Error("too slow: the card meant to expire with focus had already expired; raise FOCUSED_EXPIRING_SECONDS");
  }
  await page.frameLocator(`${CARD(3)} ${FRAME}`).locator("input[type=password]").focus();
  await page.waitForFunction(expired, [CARD(3), FRAME], {timeout: 60000});
  check(await isActive(page, MESSAGE_BOX), "a card that expired while its frame had focus handed focus to the message box");

  // F2: approve a proposal in the Inbox panel; its credential form becomes a card in the conversation.
  await page.locator("#settings-button").click();
  await page.locator("#settings-inbox-tab").click();
  const inbox = page.locator("#settings-access-inbox");
  await inbox.locator('[data-request-id="req-proposal"] button', {hasText: "Approve"}).click();
  await inbox.locator(".access-review-follow-up-note").waitFor({timeout: 15000});
  const decidedLinks = await inbox.locator('[data-request-id="req-proposal"] a').count();
  check(decidedLinks === 0, "approving a proposal in the Inbox panel says its forms are in the conversation");
  await inbox.locator("button", {hasText: "Show in the conversation"}).click();
  const followUpCard = '[data-item-id^="account_link:req-proposal:"]';
  await page.waitForFunction(
    ([selector, frame]) => Boolean(document.querySelector(`${selector} ${frame}`)), [followUpCard, FRAME], {timeout: 15000},
  );
  await page.frameLocator(`${followUpCard} ${FRAME}`).locator("input[type=password]").waitFor({timeout: 15000});
  const showState = ([selector, frame]) => {
    const overlay = document.getElementById("chat-settings-overlay");
    const box = document.querySelector(selector).getBoundingClientRect();
    const source = document.querySelector(`${selector} ${frame}`).getAttribute("src");
    return {
      settingsHidden: Boolean(overlay && overlay.hidden),
      inView: box.bottom > 0 && box.top < window.innerHeight,
      cardView: new URL(source).searchParams.get("view") === "card",
      ownTab: Boolean(document.querySelector(`${selector} .broker-card-open`)),
    };
  };
  const allShown = (state) => state.settingsHidden && state.inView && state.cardView && state.ownTab;
  try {
    await page.waitForFunction(
      ([source, args]) => {
        const state = new Function("args", `return (${source})(args);`)(args);
        return state.settingsHidden && state.inView && state.cardView && state.ownTab;
      },
      [showState.toString(), [followUpCard, FRAME]],
      {timeout: 5000},
    );
  } catch (_error) {
    // Reported below with what did not hold.
  }
  const shown = await page.evaluate(showState, [followUpCard, FRAME]);
  check(allShown(shown), `Show in the conversation closed Settings and showed the form as a broker card${
    allShown(shown) ? "" : ` ${JSON.stringify(shown)}`}`);

  await page.locator("#new-thread-button").click();
  await page.waitForFunction((frame) => document.querySelectorAll(frame).length === 0, FRAME, {timeout: 15000});
  check(true, "a thread switch removed the remaining frames");

  await page.locator(PREVIEW_THREAD_ROW).click();
  await page.waitForFunction((frame) => document.querySelectorAll(frame).length === 2, FRAME, {timeout: 15000});
  await page.locator("#settings-button").click();
  await page.locator("#settings-logout-button").click();
  await page.waitForFunction((frame) => document.querySelectorAll(frame).length === 0, FRAME, {timeout: 15000});
  check(true, "sign-out removed every frame");
  return passed;
}

async function signIn(browser, {base}) {
  const {passed, check} = checker();
  const page = await openChat(browser, base);
  const statusReads = [];
  page.on("response", async (response) => {
    if (!new URL(response.url()).pathname.endsWith("/status")) return;
    const sent = await response.request().allHeaders();
    const answered = await response.allHeaders();
    statusReads.push({
      status: response.status(), origin: sent.origin || "", allowOrigin: answered["access-control-allow-origin"] || "",
    });
  });
  await openPreviewConversation(page, base);
  await page.waitForFunction((frame) => document.querySelectorAll(frame).length === 1, FRAME, {timeout: 30000});
  await page.frameLocator(`${CARD(0)} ${FRAME}`).locator("body").waitFor();
  await page.waitForFunction(() => (window.__cardMessages || []).some((message) => "height" in message), null, {timeout: 15000});
  check(true, "framed sign-in page loaded");
  // Tell the test the page is up; it then ends the sign-in on the broker.
  console.log("framed sign-in page loaded");
  await page.waitForFunction(
    ([selector, done]) => (document.querySelector(selector) || {}).textContent === done,
    [CARD(0), DONE],
    {timeout: 60000},
  );
  // The frame's opaque origin sends "Origin: null", and the broker's status route answers it.
  check(statusReads.some((read) => read.status === 200 && read.origin === "null" && read.allowOrigin === "null"),
    `the page polled its status from inside the sandboxed frame`);
  check(true, "the card shrank to Done when the sign-in ended");
  return passed;
}

(async () => {
  const options = JSON.parse(process.argv[2]);
  const browser = await chromium.launch({headless: true});
  try {
    const passed = options.mode === "sign_in" ? await signIn(browser, options) : await cards(browser, options);
    console.log(JSON.stringify(passed));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
