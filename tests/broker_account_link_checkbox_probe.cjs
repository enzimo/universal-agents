const assert = require("node:assert/strict");
const { chromium } = require("playwright");
// The account link page has two forms, each with its own consent checkbox:
// link a stored credential, or save a new credential and link it.
const CONFIRMATIONS = [
  /Link this credential to Portainer as described above/,
  /Save this credential and link it to Portainer as described above/,
];
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const response = await page.goto(process.argv[2]);
    assert.equal(response.status(), 200);
    for (const width of [390, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      for (const theme of ["light", "dark"]) {
        await page.evaluate(theme => document.documentElement.dataset.colorMode = theme, theme);
        const labels = page.locator("label.broker-confirmation");
        assert.equal(await labels.count(), CONFIRMATIONS.length);
        for (const [index, expected] of CONFIRMATIONS.entries()) {
          const label = labels.nth(index);
          const checkbox = label.locator('input[type="checkbox"]');
          const text = label.locator("span");
          assert.match(await text.innerText(), expected);
          const box = await checkbox.boundingBox(), words = await text.boundingBox();
          assert.ok(box.width <= 24 && box.height <= 24, JSON.stringify(box));
          assert.ok(words.x >= box.x + box.width, JSON.stringify({ box, words }));
          assert.ok(Math.abs(box.y - words.y) < 8);
          assert.ok(words.x + words.width <= width);
          const color = await text.evaluate(node => getComputedStyle(node).color);
          assert.equal(color, theme === "dark" ? "rgb(237, 243, 240)" : "rgb(16, 32, 28)");
          await checkbox.uncheck();
          assert.equal(await checkbox.evaluate(node => node.form.checkValidity()), false);
          await text.click();
          assert.equal(await checkbox.isChecked(), true);
          await checkbox.focus();
          await page.keyboard.press("Space");
          assert.equal(await checkbox.isChecked(), false);
        }
        // New credential values start masked and the Show control never submits.
        const secret = page.locator('input[name="field__api_token"]');
        assert.equal(await secret.getAttribute("type"), "password");
        await page.locator('[data-secret-toggle][aria-controls="field__api_token"]').click();
        assert.equal(await secret.getAttribute("type"), "text");
        await page.locator('[data-secret-toggle][aria-controls="field__api_token"]').click();
        assert.equal(await secret.getAttribute("type"), "password");
      }
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
