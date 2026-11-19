// Drives the real Web Chat page with broker cards in headless Chromium
// (tests/test_broker_card_browser.py starts the broker, the chat preview and,
// for the path-routed deployment, the proxy). Prints the checks it passed as JSON.
//   node broker_card_browser_runner.cjs '{"base": "<chat origin>", "typed": "<value>"}'
const {chromium} = require("playwright");

const CARD = (index) => `[data-item-id="tool_setup:preview-broker-page-${index}"]`;
const FRAME = "iframe.broker-card-frame";

(async () => {
  const {base, typed} = JSON.parse(process.argv[2]);
  const passed = [];
  const check = (condition, name) => {
    if (!condition) throw new Error(`failed: ${name}`);
    passed.push(name);
  };
  const browser = await chromium.launch({headless: true});
  try {
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
    await page.goto(`${base}/webchat`);
    await page.waitForFunction((frame) => document.querySelectorAll(frame).length === 3, FRAME, {timeout: 30000});

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
    const readable = await page.$$eval(FRAME, (frames, source) => {
      const test = new Function("frame", `return (${source})(frame);`);
      return frames.map((frame) => test(frame));
    }, unreadable.toString());
    check(readable.every(Boolean), "chat page cannot read the card's document");

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
      await page.locator("#message-input").fill(text);
      await page.locator("#message-form button[type=submit]").click();
      await page.waitForFunction((count) => document.querySelectorAll(".message-bubble").length > count, before, {timeout: 15000});
    }
    const sameFrame = await page.evaluate((selector) => document.querySelector(selector) === window.__liveFrame, `${CARD(0)} ${FRAME}`);
    const kept = await card.locator("input[type=password]").inputValue();
    check(sameFrame && kept === typed, "typed value kept across two chat updates in the same frame");

    await card.locator("button[type=submit]", {hasText: "Store Credential"}).click();
    await page.waitForFunction(
      (selector) => (document.querySelector(selector) || {}).textContent === "Done. This secure page is closed.",
      CARD(0),
      {timeout: 15000},
    );
    check(true, "submission from the frame reached the broker and the card shrank to Done");

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

    await page.waitForFunction(
      ([selector, frame]) => {
        const node = document.querySelector(selector);
        return node && !node.querySelector(frame) && node.textContent.includes("expired");
      },
      [CARD(2), FRAME],
      {timeout: 60000},
    );
    check(true, "an expired card lost its frame");

    await page.locator("#new-thread-button").click();
    await page.waitForFunction((frame) => document.querySelectorAll(frame).length === 0, FRAME, {timeout: 15000});
    check(true, "a thread switch removed the remaining frame");

    await page.locator(".thread-row:not(.is-active) .thread-button").first().click();
    await page.waitForFunction((frame) => document.querySelectorAll(frame).length === 1, FRAME, {timeout: 15000});
    await page.locator("#settings-button").click();
    await page.locator("#settings-logout-button").click();
    await page.waitForFunction((frame) => document.querySelectorAll(frame).length === 0, FRAME, {timeout: 15000});
    check(true, "sign-out removed every frame");

    console.log(JSON.stringify(passed));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
