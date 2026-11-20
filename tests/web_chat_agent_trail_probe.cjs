// Browser checks for Web Chat's agent trail against the aesthetic preview (A4-A7, plan review C04, C07, C17):
//   node web_chat_agent_trail_probe.cjs <base url> [screenshot folder]
// The preview serves rows with trails, two agents working, a busy row nobody has
// reported on, twenty turns, a Needs you row and specialists in blue, violet,
// yellow and lime (scripts/preview_web_chat_threads.py).
// - Contrast: every triangle against the pixels drawn behind it (the trail hidden,
//   the box captured), in normal, hovered, active and Needs you rows, light and
//   dark, with motion off so the mesh holds still. At least 3:1 everywhere.
// - The backgrounds measured there must lie inside the range agent_trail.mjs
//   fits its colours to (TRAIL_BACKGROUNDS), so a later change to the list's
//   backing cannot silently undo the contrast.
// - Motion: the working triangles' glow pulses only with motion allowed and
//   Interface Animation on; otherwise it is steady.
// - The row says who works in words; the hover card's footer has the trail and names.
// Prints the cases it checked, with the measurements, as JSON; throws on a failure.
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { chromium } = require("playwright");

const SESSION_COOKIE = { name: "ua_web_chat_session", value: "dev-preview-session" };
const ROW = key => `.thread-row[data-session-key="website:web-chat:${key}"]`;

async function openChat(browser, baseUrl, scheme, options = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 880 }, colorScheme: scheme,
    reducedMotion: "reduce", ...options });
  await context.addCookies([{ ...SESSION_COOKIE, url: baseUrl }]);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(String(error)));
  await page.goto(baseUrl + "/webchat");
  await page.waitForSelector(".thread-section");
  await page.evaluate(mode => window.UAAppearance.preview({ ...window.UAAppearance.current(), mode }), scheme);
  // The first live read brings who works now and the busy marks.
  await page.waitForSelector(".trail-tri.is-working");
  await page.waitForTimeout(400);
  return { context, page, errors };
}

const channel = value => { const v = value / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
const luminance = ([r, g, b]) => 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
const contrast = (a, b) => { const [hi, lo] = [a, b].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };

// Each triangle of one row against the pixels behind it: the row's trail is
// hidden (its place kept) by a style rule, which a redraw of the list by a
// live read keeps, then each triangle's box is captured and every pixel read.
async function measureRow(page, selector) {
  const triangles = await page.evaluate(sel => {
    const row = document.querySelector(sel);
    const found = [...row.querySelectorAll(".trail-tri")].map(svg => {
      const box = svg.getBoundingClientRect();
      return { who: svg.dataset.who, hollow: svg.classList.contains("is-hollow"),
        working: svg.classList.contains("is-working"),
        color: getComputedStyle(svg).color.match(/[\d.]+/g).slice(0, 3).map(Number),
        clip: { x: box.x, y: box.y, width: box.width, height: box.height } };
    });
    const hide = Object.assign(document.createElement("style"), { id: "probe-hide-trail" });
    hide.textContent = sel + " .thread-trail { visibility: hidden !important; }";
    document.head.appendChild(hide);
    return found;
  }, selector);
  // Two frames: the rule is painted before the first capture (review C20).
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const measured = [];
  for (const triangle of triangles) {
    const pixels = await capture(page, triangle);
    const backgrounds = pixels.map(luminance);
    const own = luminance(triangle.color);
    measured.push({ who: triangle.who, hollow: triangle.hollow, working: triangle.working,
      worst: Math.min(...backgrounds.map(background => contrast(own, background))),
      darkest: Math.min(...backgrounds), brightest: Math.max(...backgrounds) });
  }
  await page.evaluate(() => document.getElementById("probe-hide-trail").remove());
  return measured;
}

// The pixels behind one triangle. A capture holding the triangle's own colour
// means the hidden trail was still drawn: it is captured again after another
// frame, never counted as a measurement (review C20).
async function capture(page, triangle) {
  const same = pixel => pixel.every((value, index) => Math.abs(value - triangle.color[index]) <= 2);
  for (let attempt = 1; ; attempt += 1) {
    const pixels = await readPixels(page, triangle.clip);
    if (!pixels.some(same)) return pixels;
    assert.ok(attempt < 5, `the trail stayed drawn while hidden: ${JSON.stringify(triangle)}`);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  }
}

async function readPixels(page, clip) {
    const png = await page.screenshot({ clip });
    return page.evaluate(async data => {
      const image = new Image();
      image.src = "data:image/png;base64," + data;
      await image.decode();
      const canvas = Object.assign(document.createElement("canvas"), { width: image.width, height: image.height });
      const context = canvas.getContext("2d");
      context.drawImage(image, 0, 0);
      const bytes = context.getImageData(0, 0, image.width, image.height).data;
      const sample = [];
      for (let index = 0; index < bytes.length; index += 4) sample.push([bytes[index], bytes[index + 1], bytes[index + 2]]);
      return sample;
    }, png.toString("base64"));
}

const round = value => Math.round(value * 1000) / 1000;

async function main() {
  const baseUrl = process.argv[2];
  const shots = process.argv[3] || ".";
  const { TRAIL_BACKGROUNDS } = await import(pathToFileURL(path.join(__dirname,
    "../assistant_gateway/onboarding_web/assets/agent_trail.mjs")).href);
  const browser = await chromium.launch();
  const checked = [];
  const report = {};
  try {
    for (const scheme of ["light", "dark"]) {
      const { context, page, errors } = await openChat(browser, baseUrl, scheme);
      await page.mouse.move(900, 600);
      const states = {};
      states.needsYou = await measureRow(page, ROW("portainer"));
      states.normal = [];
      for (const key of ["weekly", "vectors", "onboarding", "backup", "offsite", "contracts"]) {
        states.normal.push(...await measureRow(page, ROW(key)));
      }
      await page.hover(ROW("onboarding") + " .thread-button");
      await page.waitForTimeout(150);
      states.hovered = await measureRow(page, ROW("onboarding"));
      await page.mouse.move(900, 600);
      await page.click(ROW("backup") + " .thread-button");
      await page.mouse.move(900, 600);
      await page.waitForTimeout(300);
      states.active = await measureRow(page, ROW("backup"));
      const all = Object.values(states).flat();
      report[scheme] = {
        worst: round(Math.min(...all.map(item => item.worst))),
        backgrounds: [round(Math.min(...all.map(item => item.darkest))), round(Math.max(...all.map(item => item.brightest)))],
        byState: Object.fromEntries(Object.entries(states).map(([state, items]) => [state,
          round(Math.min(...items.map(item => item.worst)))])),
      };
      for (const [state, items] of Object.entries(states)) {
        assert.ok(items.length > 0, `${scheme} ${state}: triangles measured`);
        for (const item of items) assert.ok(item.worst >= 3, `${scheme} ${state} ${JSON.stringify(item)}`);
      }
      assert.ok(states.normal.some(item => item.hollow) && states.normal.some(item => item.who === "person"));
      // C17: the colours are fitted to the worst background of each theme's range.
      if (scheme === "dark") assert.ok(report.dark.backgrounds[1] <= TRAIL_BACKGROUNDS.dark, JSON.stringify(report.dark));
      else assert.ok(report.light.backgrounds[0] >= TRAIL_BACKGROUNDS.light, JSON.stringify(report.light));

      // Who works, in words, and the busy row nobody has reported on pulses the team lead.
      const said = await page.evaluate(rows => rows.map(sel => document.querySelector(sel + " .sr-only").textContent),
        [ROW("vectors"), ROW("contracts")]);
      assert.deepEqual(said, ["Working: Web Research Agent, Internal Improvement Agent", "Working: team lead"]);
      // Line 2 says who works in words while someone works (lead ruling on C22), cut with its full value as a
      // title; a waiting row keeps its waiting reason, now beside six triangles without being cut (C21).
      const lines = await page.evaluate(rows => rows.map(sel => {
        const line = document.querySelector(sel + " .thread-detail");
        const text = line.querySelector(".thread-detail-text");
        return { kind: line.className, text: text.textContent, title: line.title, hidden: text.getAttribute("aria-hidden"),
          triangles: line.querySelectorAll(".trail-tri").length, cut: text.scrollWidth > text.clientWidth };
      }), [ROW("vectors"), ROW("contracts"), ROW("github")]);
      assert.deepEqual(lines.map(line => [line.text, line.title, line.hidden]), [
        ["Web Research Agent and Internal Improvement Agent working",
          "Working: Web Research Agent, Internal Improvement Agent", "true"],
        ["Team lead working", "Working: team lead", "true"],
        ["Finish the setup page", "A setup page in this chat is waiting for you to finish it.", null],
      ]);
      assert.ok(lines[0].kind.includes("is-working") && lines[2].kind.includes("is-waiting"));
      assert.equal(lines[2].triangles, 6);
      assert.equal(lines[2].cut, false, "the longest waiting label fits beside a full trail");
      const pulsing = await page.evaluate(sel => [...document.querySelectorAll(sel + " .trail-tri.is-working")]
        .map(svg => svg.dataset.who), ROW("contracts"));
      assert.deepEqual(pulsing, ["lead"]);
      // Motion off (this context reduces motion): a steady glow, no animation.
      const still = await page.evaluate(() => [...document.querySelectorAll(".trail-tri.is-working")].map(svg => {
        const style = getComputedStyle(svg);
        return [style.animationName, style.filter.includes("drop-shadow")];
      }));
      assert.ok(still.length && still.every(([name, glow]) => name === "none" && glow), JSON.stringify(still));
      // The glow is not cut off by the second line (C17 review note): the line does not clip.
      assert.equal(await page.evaluate(sel => getComputedStyle(document.querySelector(sel + " .thread-detail")).overflow,
        ROW("vectors")), "visible");

      await page.locator(ROW("vectors")).screenshot({ path: path.join(shots, `agent-trail-working-row-${scheme}.png`) });
      await page.screenshot({ path: path.join(shots, `agent-trail-list-${scheme}.png`), clip: { x: 0, y: 0, width: 330, height: 760 } });
      // A7: the hover card's footer shows the trail and who worked in the thread.
      await page.hover(ROW("offsite") + " .thread-button");
      await page.waitForSelector(".thread-preview-footer .thread-trail");
      const footer = await page.evaluate(() => {
        const node = document.querySelector(".thread-preview-footer");
        return { triangles: node.querySelectorAll(".trail-tri").length, text: node.querySelector(".thread-preview-count").textContent };
      });
      assert.equal(footer.triangles, 6);
      assert.match(footer.text, /^Team lead, Web Research Agent, Memory Review Agent, Ledger Keeper · \d+ turns?$/);
      await page.screenshot({ path: path.join(shots, `agent-trail-hover-${scheme}.png`), clip: { x: 0, y: 0, width: 760, height: 880 } });
      assert.deepEqual(errors, []);
      await context.close();
    }
    checked.push("contrast (worst pixel, 3:1 or more, backgrounds inside the fitted range): " + JSON.stringify(report));
    checked.push("rows say who works, in words on line 2 and to screen readers; a busy row nobody reported on pulses the team lead; the longest waiting label fits beside six triangles");
    checked.push("motion off: steady glow; the second line does not clip it");
    checked.push("hover card footer: the trail and the agent types that worked");

    // C07: the pulse runs only with motion allowed and Interface Animation on.
    const { context, page, errors } = await openChat(browser, baseUrl, "dark", { reducedMotion: "no-preference" });
    const animation = () => page.evaluate(() => [...new Set([...document.querySelectorAll(".trail-tri.is-working")]
      .map(svg => getComputedStyle(svg).animationName))]);
    await page.evaluate(() => { document.documentElement.dataset.uiMotion = "on"; });
    assert.deepEqual(await animation(), ["trail-pulse"]);
    await page.evaluate(() => { document.documentElement.dataset.uiMotion = "off"; });
    assert.deepEqual(await animation(), ["none"]);
    assert.deepEqual(errors, []);
    await context.close();
    checked.push("pulse: on with motion allowed and Interface Animation on, off when either is off");
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(checked));
}

main().catch(error => {
  console.error(error);
  process.exit(1);
});
