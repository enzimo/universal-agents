// Browser checks for Web Chat's thread list against the aesthetic preview:
//   node web_chat_thread_list_probe.cjs <base url> [screenshot folder]
// The preview serves sample threads in every section (preview_web_chat_threads.py).
// Checks A1 (layout and rows in view), A8 (menu by right-click, keyboard and long
// press; Rename and Delete), A9 (hover preview) and the review points on narrow
// screens (C22), waiting labels (C24) and the card over the open chat (C30).
// Prints the cases it checked as JSON; throws on a failure.
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright");

const SESSION_COOKIE = { name: "ua_web_chat_session", value: "dev-preview-session" };

async function openChat(browser, baseUrl, viewport, options = {}) {
  const context = await browser.newContext({ viewport, colorScheme: "dark", ...options });
  await context.addCookies([{ ...SESSION_COOKIE, url: baseUrl }]);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(String(error)));
  const writes = [];
  page.on("request", request => {
    if (request.method() !== "GET" && request.url().includes("/api/web-chat/sessions/")) {
      writes.push([request.method(), new URL(request.url()).pathname, request.postData()]);
    }
  });
  await page.goto(baseUrl + "/webchat");
  await page.waitForSelector(".thread-section");
  await page.waitForTimeout(400);
  await page.evaluate(() => window.scrollTo(0, 0));
  return { context, page, errors, writes };
}

// WCAG contrast of two sRGB colours.
const luminance = ([r, g, b]) => {
  const channel = value => { const v = value / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
};
const contrast = (a, b) => { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };

// F8: the worst contrast of each kind of text against the pixels actually
// drawn behind it (the glass over the mesh). For each selector, up to eight
// elements in view are measured: their text (and their children's) is made
// transparent, the box is captured, and every fifth pixel is compared with
// the text colour. Elements scrolled out of the list are skipped.
async function worstContrast(page, selectors) {
  const result = {};
  for (const [name, selector] of Object.entries(selectors)) {
    const boxes = await page.evaluate(sel => {
      const list = document.querySelector("#thread-list").getBoundingClientRect();
      return [...document.querySelectorAll(sel)].filter(node => {
        const box = node.getBoundingClientRect();
        const inList = !node.closest("#thread-list") || (box.top >= list.top && box.bottom <= list.bottom);
        return box.height > 0 && !node.closest("[hidden]") && inList;
      }).slice(0, 8).map(node => {
        const box = node.getBoundingClientRect();
        const color = getComputedStyle(node).color.match(/[\d.]+/g).slice(0, 3).map(Number);
        [node, ...node.querySelectorAll("*")].forEach(part => { part.dataset.probeColor = part.style.color; part.style.color = "transparent"; });
        return { clip: { x: box.x, y: box.y, width: Math.min(box.width, 280), height: box.height }, color };
      });
    }, selector);
    let worst = Infinity;
    for (const { clip, color } of boxes) {
      const png = await page.screenshot({ clip });
      const pixels = await page.evaluate(async data => {
        const image = new Image();
        image.src = "data:image/png;base64," + data;
        await image.decode();
        const canvas = Object.assign(document.createElement("canvas"), { width: image.width, height: image.height });
        const context = canvas.getContext("2d");
        context.drawImage(image, 0, 0);
        const bytes = context.getImageData(0, 0, image.width, image.height).data;
        const sample = [];
        for (let index = 0; index < bytes.length; index += 4 * 5) sample.push([bytes[index], bytes[index + 1], bytes[index + 2]]);
        return sample;
      }, png.toString("base64"));
      for (const pixel of pixels) worst = Math.min(worst, contrast(color, pixel));
    }
    await page.evaluate(sel => document.querySelectorAll(sel).forEach(node => [node, ...node.querySelectorAll("*")]
      .forEach(part => { part.style.color = part.dataset.probeColor || ""; })), selector);
    result[name] = boxes.length ? Math.round(worst * 100) / 100 : null;
  }
  return result;
}

// What lies fully inside the thread list's visible box.
function inView(page) {
  return page.evaluate(() => {
    const list = document.querySelector("#thread-list").getBoundingClientRect();
    const inside = node => {
      const box = node.getBoundingClientRect();
      return box.height > 0 && box.top >= list.top - 1 && box.bottom <= list.bottom + 1;
    };
    return {
      listHeight: Math.round(list.height),
      twoLineRows: [...document.querySelectorAll(".thread-row")].filter(row => inside(row) && row.querySelector(".thread-detail")).length,
      rows: [...document.querySelectorAll(".thread-row")].filter(inside).length,
      headers: [...document.querySelectorAll(".thread-section-header")].filter(inside).map(header => header.querySelector(".thread-section-label").textContent),
      cutWaiting: [...document.querySelectorAll(".thread-detail.is-waiting")].filter(line => line.scrollWidth > line.clientWidth).map(line => line.textContent),
    };
  });
}

(async () => {
  const [baseUrl, shots = "."] = process.argv.slice(2);
  const checked = [];
  const browser = await chromium.launch({ headless: true });
  try {
    // A1: the app fills the window with ~6 px gaps; at least 8 two-line rows
    // and the Snoozed and Dismissed headers in view without scrolling.
    const wide = await openChat(browser, baseUrl, { width: 1280, height: 880 });
    const app = await wide.page.evaluate(() => {
      const box = document.querySelector(".chat-app").getBoundingClientRect();
      const side = document.querySelector(".chat-sidebar").getBoundingClientRect();
      const main = document.querySelector(".chat-main").getBoundingClientRect();
      return { top: box.top, height: box.height, gap: main.left - side.right, scroll: document.documentElement.scrollHeight - innerHeight };
    });
    assert.equal(app.height, 868, "the app fills the window less 6 px on each side");
    assert.equal(app.top, 6);
    assert.equal(Math.round(app.gap), 6);
    const wideView = await inView(wide.page);
    assert.ok(wideView.twoLineRows >= 8, JSON.stringify(wideView));
    for (const header of ["Needs you", "Pinned", "Recent", "Snoozed", "Dismissed"]) assert.ok(wideView.headers.includes(header), header);
    assert.deepEqual(wideView.cutWaiting, [], "waiting labels fit their line (C24)");
    await wide.page.screenshot({ path: path.join(shots, "thread-list-1280x880.png") });
    checked.push(`1280x880: ${wideView.twoLineRows} two-line rows and all five headers in view, app 868 px tall, 6 px gaps`);
    // C19: the light theme's backing behind the list takes none of its height,
    // so the same rows and all five headers fit there too.
    const lightLayout = await openChat(browser, baseUrl, { width: 1280, height: 880 }, { colorScheme: "light", reducedMotion: "reduce" });
    await lightLayout.page.evaluate(() => window.UAAppearance.preview({ ...window.UAAppearance.current(), mode: "light" }));
    await lightLayout.page.waitForTimeout(400);
    assert.equal(await lightLayout.page.evaluate(() => document.documentElement.dataset.colorMode), "light");
    const lightView = await inView(lightLayout.page);
    assert.ok(lightView.twoLineRows >= 8, JSON.stringify(lightView));
    for (const header of ["Needs you", "Pinned", "Recent", "Snoozed", "Dismissed"]) assert.ok(lightView.headers.includes(header), "light: " + header);
    assert.equal(lightView.listHeight, wideView.listHeight, "the list is as tall in light as in dark");
    assert.deepEqual(lightLayout.errors, []);
    await lightLayout.context.close();
    checked.push(`1280x880 light: ${lightView.twoLineRows} two-line rows and all five headers in view, list ${lightView.listHeight} px as in dark`);

    // A8: right-click, arrows into Snooze's choices, Escape back to the row.
    await wide.page.click('.thread-row[data-session-key="website:web-chat:offsite"] .thread-button', { button: "right" });
    assert.equal(await wide.page.locator(".thread-menu").count(), 1);
    assert.deepEqual(await wide.page.locator(".thread-menu .thread-menu-label").allTextContents(),
      ["Pin to top", "Snooze", "Dismiss", "Rename", "Delete"]);
    await wide.page.keyboard.press("ArrowDown");
    await wide.page.keyboard.press("ArrowRight");
    await wide.page.waitForTimeout(150);
    await wide.page.screenshot({ path: path.join(shots, "thread-menu.png") });
    assert.ok(await wide.page.evaluate(() => Boolean(document.activeElement.closest(".thread-menu.is-submenu"))));
    await wide.page.keyboard.press("Escape");
    await wide.page.keyboard.press("Escape");
    assert.equal(await wide.page.evaluate(() => document.activeElement.closest(".thread-row")?.dataset.sessionKey), "website:web-chat:offsite");
    // The keyboard: Shift+F10 on the focused row, then Pin.
    await wide.page.keyboard.press("Shift+F10");
    await wide.page.keyboard.press("Enter");
    await wide.page.waitForTimeout(400);
    assert.equal(await wide.page.locator('.thread-section.is-pinned .thread-row[data-session-key="website:web-chat:offsite"]').count(), 1);
    // Rename and Delete from the menu.
    await wide.page.click('.thread-row[data-session-key="website:web-chat:backup"] .thread-button', { button: "right" });
    await wide.page.click('.thread-menu-item:has-text("Rename")');
    await wide.page.waitForSelector(".thread-rename-input:focus");
    await wide.page.keyboard.press("Control+A");
    await wide.page.keyboard.type("Backup job retries");
    await wide.page.keyboard.press("Enter");
    await wide.page.click('.thread-row[data-session-key="website:web-chat:contracts"] .thread-button', { button: "right" });
    await wide.page.click('.thread-menu-item:has-text("Delete")');
    await wide.page.waitForTimeout(400);
    assert.equal(await wide.page.locator('.thread-row[data-session-key="website:web-chat:contracts"]').count(), 0);
    const writes = wide.writes.map(([method, pathname, body]) => [method, decodeURIComponent(pathname), body && JSON.parse(body)]);
    assert.deepEqual(writes, [
      ["PATCH", "/api/web-chat/sessions/website:web-chat:offsite", { thread_action: "pin" }],
      ["PATCH", "/api/web-chat/sessions/website:web-chat:backup", { session_alias: "Backup job retries" }],
      ["POST", "/api/web-chat/sessions/website:web-chat:contracts/archive", null],
    ]);
    checked.push("menu: right-click, arrows, Escape back to the row, Shift+F10 pin, Rename and Delete");

    // A9: the hover card after ~400 ms with at most 4 two-line turns.
    await wide.page.hover('.thread-row[data-session-key="website:web-chat:weekly"] .thread-button');
    await wide.page.mouse.move(700, 600);
    const hoverTarget = '.thread-row[data-session-key="website:web-chat:offsite"] .thread-button';
    await wide.page.hover(hoverTarget);
    await wide.page.waitForTimeout(250);
    assert.equal(await wide.page.locator(".thread-preview").count(), 0, "nothing before about 400 ms");
    await wide.page.waitForTimeout(600);
    const card = await wide.page.evaluate(() => ({
      turns: [...document.querySelectorAll(".thread-preview-turn")].length,
      lines: [...document.querySelectorAll(".thread-preview-text")].map(text => Math.round(text.getBoundingClientRect().height / parseFloat(getComputedStyle(text).lineHeight))),
      count: document.querySelector(".thread-preview-count")?.textContent,
    }));
    assert.ok(card.turns > 0 && card.turns <= 4, JSON.stringify(card));
    assert.ok(card.lines.every(lines => lines <= 2), JSON.stringify(card));
    // The footer names who worked in the thread before the count (agent trail, A7).
    assert.equal(card.count, "Team lead, Web Research Agent, Memory Review Agent, Ledger Keeper · 4 turns");
    await wide.page.screenshot({ path: path.join(shots, "thread-hover.png") });
    // F7: Escape closes the card while the mouse stays on the row, and it does not come back.
    await wide.page.keyboard.press("Escape");
    assert.equal(await wide.page.locator(".thread-preview").count(), 0, "Escape closes the card");
    await wide.page.mouse.move(...await wide.page.evaluate(selector => {
      const box = document.querySelector(selector).getBoundingClientRect();
      return [box.left + 30, box.top + 12];
    }, hoverTarget));
    await wide.page.waitForTimeout(700);
    assert.equal(await wide.page.locator(".thread-preview").count(), 0, "it stays closed while the pointer is on the row");
    checked.push(`hover: ${card.turns} turns of at most two lines after 400 ms, '${card.count}'; Escape closes it`);

    // C30: clicking a row opens it and no card comes back over it.
    await wide.page.click(hoverTarget);
    await wide.page.waitForTimeout(1000);
    assert.equal(await wide.page.locator(".thread-preview").count(), 0);
    checked.push("no card over the chat just opened");

    // F6: a cut title is readable with the mouse. Another row's card shows it
    // whole; the open chat's row, which has no card, shows it in the tooltip.
    const cutRow = await wide.page.evaluate(() => [...document.querySelectorAll(".thread-row")].find(row => {
      const title = row.querySelector(".thread-title");
      // Not a woke row: opening it clears the Woke pill, and the title may then fit.
      return row.dataset.sessionKey && !row.closest("[hidden]") && !row.classList.contains("is-woke")
        && title && title.scrollWidth > title.clientWidth;
    })?.dataset.sessionKey || "");
    assert.ok(cutRow, "the preview has a row with a cut title");
    // A name longer than one line of the card, as a person may give it (the rename field takes up to 80 characters).
    const longTitle = "Draft the onboarding email for new hires with the links they ask for most";
    const cutSelector = `.thread-row[data-session-key="${cutRow}"] .thread-button`;
    await wide.page.click(cutSelector, { button: "right" });
    await wide.page.click('.thread-menu-item:has-text("Rename")');
    await wide.page.waitForSelector(".thread-rename-input:focus");
    await wide.page.keyboard.press("Control+A");
    await wide.page.keyboard.type(longTitle);
    await wide.page.keyboard.press("Enter");
    await wide.page.waitForTimeout(400);
    // Back to the mouse: a click in the chat ends the keyboard focus the rename left.
    await wide.page.click("#message-list", { position: { x: 20, y: 20 } });
    await wide.page.mouse.move(700, 600);
    await wide.page.hover(cutSelector);
    await wide.page.waitForSelector(".thread-preview-title");
    await wide.page.waitForTimeout(300);
    const cardTitle = await wide.page.evaluate(() => {
      const title = document.querySelector(".thread-preview-title");
      const lineHeight = parseFloat(getComputedStyle(title).fontSize) * 1.6;
      return { text: title.textContent, cut: title.scrollWidth > title.clientWidth, height: Math.round(title.getBoundingClientRect().height), lineHeight };
    });
    assert.equal(cardTitle.text, longTitle);
    assert.equal(cardTitle.cut, false, "the card's title is not cut: " + JSON.stringify(cardTitle));
    assert.ok(cardTitle.height > cardTitle.lineHeight, "it wraps onto more lines: " + JSON.stringify(cardTitle));
    await wide.page.click(cutSelector);
    // Right after the click, with the mouse left in place, no tooltip covers the row (C18).
    await wide.page.waitForTimeout(1500);
    assert.equal(await wide.page.evaluate(() => {
      const tooltip = document.querySelector("#thread-title-tooltip");
      return Boolean(tooltip && !tooltip.hidden);
    }), false, "no tooltip right after opening the chat");
    // The pointer leaves the row and comes back.
    await wide.page.mouse.move(700, 600);
    await wide.page.waitForTimeout(200);
    assert.equal(await wide.page.evaluate(() => {
      const tooltip = document.querySelector("#thread-title-tooltip");
      return Boolean(tooltip && !tooltip.hidden);
    }), false);
    await wide.page.hover(cutSelector);
    await wide.page.waitForTimeout(1300);
    const openTooltip = await wide.page.evaluate(() => {
      const tooltip = document.querySelector("#thread-title-tooltip");
      return tooltip && !tooltip.hidden ? tooltip.textContent : "";
    });
    assert.equal(openTooltip, cardTitle.text, "the open chat's row shows its full title on hover");
    assert.equal(await wide.page.locator(".thread-preview").count(), 0, "and no card");
    checked.push(`full titles with the mouse: the card shows a ${longTitle.length}-character name whole (${cardTitle.height} px tall); the open chat's row shows it in the tooltip once the pointer comes back, not right after the click`);
    assert.deepEqual(wide.errors, []);
    await wide.context.close();

    // F5: a change that fails is shown in the live status line beside the
    // list, in view while the open chat is scrolled to its end; Retry only
    // when the request did not get through, none for a refusal.
    const failing = await openChat(browser, baseUrl, { width: 1280, height: 880 });
    const statusLine = () => failing.page.evaluate(() => {
      const line = document.querySelector("#thread-list-status");
      const box = line.getBoundingClientRect();
      return {
        role: line.getAttribute("role"),
        live: line.getAttribute("aria-live"),
        text: line.querySelector(".thread-list-status-text")?.textContent || "",
        buttons: [...line.querySelectorAll("button")].map(button => button.textContent),
        inView: box.height > 0 && box.top >= 0 && box.bottom <= innerHeight,
        inChat: document.querySelector("#message-list").textContent.includes("was not moved"),
      };
    });
    const pinOnboarding = async () => {
      await failing.page.evaluate(() => { const list = document.querySelector("#message-list"); list.scrollTop = list.scrollHeight; });
      await failing.page.click('.thread-row[data-session-key="website:web-chat:onboarding"] .thread-button', { button: "right" });
      await failing.page.click('.thread-menu-item:has-text("Pin to top")');
      await failing.page.waitForSelector("#thread-list-status.is-error");
    };
    await failing.page.route("**/api/web-chat/sessions/*", route => (
      route.request().method() === "PATCH" ? route.abort("failed") : route.continue()));
    await pinOnboarding();
    const offline = await statusLine();
    assert.deepEqual(offline, { role: "status", live: "polite", text: "\u201cDraft onboarding email for new hires\u201d was not moved. Try again.",
      buttons: ["Retry", "Close"], inView: true, inChat: false }, JSON.stringify(offline));
    await failing.page.click('#thread-list-status button:has-text("Close")');
    await failing.page.unroute("**/api/web-chat/sessions/*");
    await failing.page.route("**/api/web-chat/sessions/*", route => (
      route.request().method() === "PATCH" ? route.fulfill({ status: 400, body: "Thread not found." }) : route.continue()));
    await pinOnboarding();
    const refused = await statusLine();
    assert.deepEqual([refused.text, refused.buttons, refused.inView], [
      "\u201cDraft onboarding email for new hires\u201d was not moved. Thread not found.", ["Close"], true]);
    await failing.page.screenshot({ path: path.join(shots, "thread-status-failure.png") });
    assert.deepEqual(failing.errors, []);
    checked.push("status line: a failed pin shows in view beside the list, role status, Retry only when it did not get through");
    await failing.context.close();

    // F8, C15: text contrast in both themes, measured against what is drawn
    // behind it, with motion off so the mesh holds still. Every kind of text
    // in the list must read at 4.5:1 or more in both: in the light theme it
    // sits on the pale backing the owner chose (2026-11-20).
    const measured = {};
    for (const scheme of ["light", "dark"]) {
      const themed = await openChat(browser, baseUrl, { width: 1280, height: 880 }, { colorScheme: scheme, reducedMotion: "reduce" });
      await themed.page.evaluate(mode => window.UAAppearance.preview({ ...window.UAAppearance.current(), mode }), scheme);
      await themed.page.waitForTimeout(400);
      await themed.page.route("**/api/web-chat/sessions/*", route => (
        route.request().method() === "PATCH" ? route.abort("failed") : route.continue()));
      await themed.page.click('.thread-row[data-session-key="website:web-chat:onboarding"] .thread-button', { button: "right" });
      await themed.page.click('.thread-menu-item:has-text("Pin to top")');
      await themed.page.waitForSelector("#thread-list-status.is-error");
      await themed.page.mouse.move(700, 500);
      await themed.page.waitForTimeout(300);
      await themed.page.screenshot({ path: path.join(shots, `thread-list-${scheme}.png`), clip: { x: 0, y: 0, width: 320, height: 880 } });
      measured[scheme] = await worstContrast(themed.page, {
        waiting: ".thread-detail.is-waiting",
        needsYouHeader: ".thread-section.is-needs-you .thread-section-header",
        status: ".thread-list-status-text",
        header: ".thread-section:not(.is-needs-you) .thread-section-header",
        age: ".thread-age",
        snippet: ".thread-detail:not(.is-waiting)",
        title: ".thread-title",
      });
      assert.equal(await themed.page.evaluate(() => document.documentElement.dataset.colorMode), scheme);
      assert.deepEqual(themed.errors, []);
      await themed.context.close();
    }
    for (const scheme of ["light", "dark"]) {
      for (const [name, value] of Object.entries(measured[scheme])) {
        assert.ok(value >= 4.5, `${scheme} ${name}: ${JSON.stringify(measured)}`);
      }
    }
    checked.push("contrast (worst pixel): " + JSON.stringify(measured));

    // F4: after Dismiss, Snooze, Delete and Rename by keyboard, focus is on
    // the row that took the moved row's place (or the renamed row), never
    // on the page body.
    const keys = await openChat(browser, baseUrl, { width: 1280, height: 880 });
    const focusState = () => keys.page.evaluate(() => {
      const shown = [...document.querySelectorAll(".thread-row")].filter(row => row.dataset.sessionKey && !row.closest("[hidden]"));
      const active = document.activeElement;
      const row = active && active.closest ? active.closest(".thread-row") : null;
      return {
        shown: shown.map(item => item.dataset.sessionKey),
        focused: row ? row.dataset.sessionKey : (active === document.body ? "body" : active.id || active.className),
        onButton: Boolean(active && active.classList.contains("thread-button")),
      };
    });
    const menuChoice = async (sessionKey, presses) => {
      await keys.page.focus(`.thread-row[data-session-key="${sessionKey}"] .thread-button`);
      const before = await focusState();
      const index = before.shown.indexOf(sessionKey);
      const expected = before.shown[index + 1] || before.shown[index - 1];
      await keys.page.keyboard.press("Shift+F10");
      for (const key of presses) await keys.page.keyboard.press(key);
      await keys.page.waitForTimeout(600);
      return expected;
    };
    // C16: a mouse Dismiss moves focus too, but no title tooltip pops up over the next row.
    // A sample row whose next row has a cut title, so a tooltip would have something to show.
    const mouseTarget = await keys.page.evaluate(() => {
      const shown = [...document.querySelectorAll(".thread-row")].filter(row => row.dataset.sessionKey && !row.closest("[hidden]"));
      const cut = row => { const title = row && row.querySelector(".thread-title"); return Boolean(title) && title.scrollWidth > title.clientWidth; };
      const index = shown.findIndex((row, at) => !row.dataset.sessionKey.endsWith(":dev") && !row.closest(".is-needs-you") && cut(shown[at + 1]));
      return index >= 0 ? shown[index].dataset.sessionKey : "";
    });
    assert.ok(mouseTarget, "the preview has a row followed by one with a cut title");
    await keys.page.click(`.thread-row[data-session-key="${mouseTarget}"] .thread-button`, { button: "right" });
    await keys.page.click('.thread-menu-item:has-text("Dismiss")');
    await keys.page.mouse.move(700, 600);
    await keys.page.waitForTimeout(1400);
    const mouseTooltip = await keys.page.evaluate(() => {
      const tooltip = document.querySelector("#thread-title-tooltip");
      return { shown: Boolean(tooltip && !tooltip.hidden), focusedRow: document.activeElement.closest(".thread-row")?.dataset.sessionKey || "" };
    });
    assert.deepEqual([mouseTooltip.shown, Boolean(mouseTooltip.focusedRow)], [false, true], "mouse Dismiss: " + JSON.stringify(mouseTooltip));
    // The first sample in Recent (the preview's own chat is not a sample and cannot be moved).
    const dismissTarget = await keys.page.evaluate(() => [...document.querySelectorAll(".thread-section.is-recent .thread-row")]
      .map(row => row.dataset.sessionKey).find(key => key && !key.endsWith(":dev")));
    let expected = await menuChoice(dismissTarget, ["ArrowDown", "ArrowDown", "Enter"]);
    let state = await focusState();
    assert.deepEqual([state.focused, state.onButton], [expected, true], "Dismiss: " + JSON.stringify(state));
    assert.ok(!state.shown.includes(dismissTarget), "the dismissed row is in the collapsed section");
    // Keyboard focus still shows a cut title in full after a second (C16).
    const titleTooltip = () => keys.page.evaluate(() => {
      const title = document.activeElement.querySelector(".thread-title");
      const tooltip = document.querySelector("#thread-title-tooltip");
      return { cut: Boolean(title) && title.scrollWidth > title.clientWidth, shown: Boolean(tooltip && !tooltip.hidden) };
    });
    await keys.page.waitForTimeout(1200);
    const keyboardTooltip = await titleTooltip();
    assert.equal(keyboardTooltip.shown, keyboardTooltip.cut, "keyboard focus: " + JSON.stringify(keyboardTooltip));
    const snoozeTarget = state.focused;
    expected = await menuChoice(snoozeTarget, ["ArrowDown", "ArrowRight", "Enter"]);
    state = await focusState();
    assert.deepEqual([state.focused, state.onButton], [expected, true], "Snooze: " + JSON.stringify(state));
    const renameTarget = state.focused;
    await keys.page.keyboard.press("Shift+F10");
    for (const key of ["ArrowDown", "ArrowDown", "ArrowDown", "Enter"]) await keys.page.keyboard.press(key);
    await keys.page.waitForSelector(".thread-rename-input:focus");
    await keys.page.keyboard.press("Escape");
    await keys.page.waitForTimeout(200);
    state = await focusState();
    assert.deepEqual([state.focused, state.onButton], [renameTarget, true], "Rename, Escape: " + JSON.stringify(state));
    await keys.page.keyboard.press("Shift+F10");
    for (const key of ["ArrowDown", "ArrowDown", "ArrowDown", "Enter"]) await keys.page.keyboard.press(key);
    await keys.page.waitForSelector(".thread-rename-input:focus");
    await keys.page.keyboard.type(" (renamed)");
    await keys.page.keyboard.press("Enter");
    await keys.page.waitForTimeout(500);
    state = await focusState();
    assert.deepEqual([state.focused, state.onButton], [renameTarget, true], "Rename, Enter: " + JSON.stringify(state));
    const deleteTarget = state.focused;
    expected = await menuChoice(deleteTarget, ["End", "Enter"]);
    state = await focusState();
    assert.deepEqual([state.focused, state.onButton], [expected, true], "Delete: " + JSON.stringify(state));
    assert.ok(!state.shown.includes(deleteTarget));
    // F17, C03: in Pick a time, Tab moves through the date field's parts and
    // the buttons and wraps; Shift+Tab inside the field stays in the field;
    // focus never leaves the dialog.
    await keys.page.keyboard.press("Shift+F10");
    await keys.page.keyboard.press("ArrowDown");
    await keys.page.keyboard.press("ArrowRight");
    await keys.page.keyboard.press("End");
    await keys.page.keyboard.press("Enter");
    await keys.page.waitForSelector(".thread-snooze-input:focus");
    const where = () => keys.page.evaluate(() => {
      const active = document.activeElement;
      if (!active.closest(".thread-snooze-dialog")) return "outside";
      return active.classList.contains("thread-snooze-input") ? "field" : active.textContent || active.className;
    });
    const forward = [];
    for (let press = 0; press < 14; press += 1) {
      await keys.page.keyboard.press("Tab");
      forward.push(await where());
    }
    assert.ok(!forward.includes("outside"), "Tab: " + forward.join(","));
    const snoozeAt = forward.indexOf("Snooze");
    assert.ok(forward.indexOf("Cancel") >= 0 && snoozeAt > forward.indexOf("Cancel"), forward.join(","));
    assert.equal(forward[snoozeAt + 1], "field", "Tab after Snooze wraps to the field: " + forward.join(","));
    await keys.page.keyboard.press("Tab");
    assert.equal(await where(), "field");
    await keys.page.keyboard.press("Shift+Tab");
    assert.equal(await where(), "field", "Shift+Tab inside the field stays in it");
    const backward = [];
    for (let press = 0; press < 14; press += 1) {
      await keys.page.keyboard.press("Shift+Tab");
      backward.push(await where());
    }
    assert.ok(!backward.includes("outside") && backward.includes("Snooze"), "Shift+Tab: " + backward.join(","));
    await keys.page.keyboard.press("Escape");
    checked.push("Pick a time: Tab and Shift+Tab move through the field's parts and the buttons and never leave the dialog");
    assert.deepEqual(keys.errors, []);
    checked.push(`keyboard focus: after Dismiss, Snooze and Delete on the next row; after Rename (Enter or Escape) on the renamed row; title tooltip on keyboard focus only (cut title: ${keyboardTooltip.cut})`);
    await keys.context.close();

    // Narrow screens (C22) with touch: rows in view, a long press opens the
    // menu, and touch never shows the hover card.
    const narrow = await openChat(browser, baseUrl, { width: 390, height: 844 }, { hasTouch: true, isMobile: true });
    const narrowView = await inView(narrow.page);
    assert.ok(narrowView.rows >= 2 && narrowView.headers.length >= 1, JSON.stringify(narrowView));
    assert.deepEqual(narrowView.cutWaiting, []);
    const row = await narrow.page.locator(".thread-row .thread-button").first().boundingBox();
    const cdp = await narrow.context.newCDPSession(narrow.page);
    const point = { x: row.x + 40, y: row.y + 10 };
    await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [point] });
    await narrow.page.waitForTimeout(700);
    await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await narrow.page.waitForTimeout(200);
    assert.equal(await narrow.page.locator(".thread-menu").count(), 1, "a long press opens the menu");
    assert.equal(await narrow.page.locator(".thread-preview").count(), 0, "touch never shows the hover card");
    await narrow.page.screenshot({ path: path.join(shots, "thread-list-390x844.png") });
    assert.deepEqual(narrow.errors, []);
    checked.push(`390x844: ${narrowView.rows} rows and ${narrowView.headers.length} headers in view, long press opens the menu`);
    await narrow.context.close();
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(checked));
})().catch(error => { console.error(error); process.exitCode = 1; });
