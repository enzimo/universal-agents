const assert = require("node:assert/strict");
const { chromium } = require("playwright");

(async () => {
  const base = process.argv[2];
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  const errors = [], eventRequests = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => {
    if (request.url().includes("/events/stream")) eventRequests.push(request.url());
  });
  try {
    const auth = await context.request.post(new URL("api/web-chat/auth", base).href,
      { data: { token: "uaweb_test_token" } });
    assert.equal(auth.status(), 200);
    await page.goto(new URL("webchat", base).href);
    await page.getByText("Live updates connected", { exact: true }).waitFor();
    await fetch(new URL("test/notice/while-open", base), { method: "POST" });
    await page.locator("#message-list").getByText("Permission review while-open", { exact: true }).waitFor();
    assert.equal(eventRequests.length, 1, "Updates use one persistent connection");
    await context.setOffline(true);
    await fetch(new URL("test/disconnect", base), { method: "POST" });
    await fetch(new URL("test/notice/while-offline", base), { method: "POST" });
    // Offline emulation alone need not close an already established TCP stream.
    await page.getByText("Updates disconnected. Reconnecting…", { exact: true }).waitFor({ timeout: 25000 });
    await context.setOffline(false);
    await page.locator("#message-list").getByText("Permission review while-offline", { exact: true }).waitFor({ timeout: 25000 });
    assert.equal(await page.locator("#message-list").getByText("Permission review while-open", { exact: true }).count(), 1);
    assert.ok(eventRequests.length >= 2);
    assert.ok(new URL(eventRequests.at(-1)).searchParams.get("after"));
    await page.getByText("Live updates connected", { exact: true }).waitFor();
    await page.locator("#settings-button").click();
    await page.locator("#settings-logout-button").click();
    await page.locator("#chat-auth").waitFor();
    await fetch(new URL("test/notice/after-signout", base), { method: "POST" });
    assert.equal(await page.locator("#message-list").textContent(), "");
    assert.deepEqual(errors, []);
    console.log("Live permission notice, offline replay, deduplication and sign-out passed.");
  } catch (error) {
    console.error({ errors, eventRequests, body: await page.locator("body").innerText() });
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
