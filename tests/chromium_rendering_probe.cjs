const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const base = process.argv[2].replace(/\/$/, "");
const assets = path.resolve(__dirname, "../assistant_gateway/onboarding_web/assets");

// The dark background draws on its own layer (UAMetallicMesh.canvas) above
// the page canvas, which the light theme keeps. Counting draw calls on that
// layer works for both drawings: WebGL (full) and 2D (basic).
async function countDarkDraws(page) {
  await page.evaluate(() => {
    window.darkDraws = 0;
    const count = (proto, name) => {
      if (!proto || !proto[name]) return;
      const original = proto[name];
      proto[name] = function (...args) {
        if (this.canvas === UAMetallicMesh.canvas) window.darkDraws++;
        return original.apply(this, args);
      };
    };
    count(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype, "drawArrays");
    count(CanvasRenderingContext2D.prototype, "fillRect");
  });
}

async function checkDocs(browser) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  let documents = 0;
  page.on("request", request => {
    if (request.isNavigationRequest() && request.frame() === page.mainFrame()) documents++;
  });
  await page.goto(base + "/docs/");
  await page.waitForFunction(() => window.UAMetallicMesh && window.UAAppearance && UAMetallicMesh.lab.stats());
  const original = await page.evaluate(() => {
    window.originalCanvas = document.querySelector(".ua-doc-static-mesh");
    window.darkLayer = UAMetallicMesh.canvas;
    return {
      mode: document.documentElement.dataset.colorMode,
      layerShown: getComputedStyle(darkLayer).display !== "none",
      layerAbovePage: originalCanvas.nextElementSibling === darkLayer,
      faces: UAMetallicMesh.lab.stats().triangles,
    };
  });
  assert.equal(original.mode, "dark");
  assert.ok(original.layerShown, "the dark background must show on the first visit");
  assert.ok(original.layerAbovePage, "the dark layer must sit directly above the page canvas");
  assert.ok(original.faces > 20, "the dark background must build its mesh on the first visit");
  await countDarkDraws(page);
  await page.waitForTimeout(500);
  assert.equal(await page.evaluate(() => window.darkDraws), 0, "Docs must not continuously repaint");
  await page.locator(".md-nav--primary").getByRole("link", { name: "Quickstart", exact: true }).click();
  await page.waitForURL("**/docs/quickstart/");
  await page.goBack();
  await page.waitForURL("**/docs/");
  assert.equal(documents, 1, "Docs navigation must use Material instant navigation at the served port");
  assert.ok(await page.evaluate(() => originalCanvas === document.querySelector(".ua-doc-static-mesh")));
  assert.ok(await page.evaluate(() => darkLayer === UAMetallicMesh.canvas));
  const switches = await page.evaluate(() => {
    const shown = () => getComputedStyle(UAMetallicMesh.canvas).display !== "none";
    const light = UAAppearance.current(); light.mode = "light";
    UAAppearance.setUserPreference(light);
    const firstLight = originalCanvas.toDataURL();
    const lightShowsDark = shown();
    UAAppearance.setUserPreference(light);
    const repeatedLight = originalCanvas.toDataURL();
    const dark = UAAppearance.current(); dark.mode = "dark";
    UAAppearance.setUserPreference(dark);
    const darkShowsDark = shown();
    UAAppearance.setUserPreference(light);
    return [firstLight === repeatedLight, !lightShowsDark, darkShowsDark, originalCanvas.toDataURL() === firstLight];
  });
  assert.deepEqual(switches, [true, true, true, true], "real theme switches must show the right layer and keep the light drawing");
  await page.close();
}

async function checkRuntimeMotion(browser) {
  const page = await browser.newPage({ viewport: { width: 800, height: 600 } });
  await page.route(base + "/webchat", route => route.fulfill({ contentType: "text/html", body:
    '<html data-color-mode="dark"><body><canvas data-ua-metallic-mesh></canvas><script src="/docs/assets/appearance.js"></script><script src="/docs/assets/metallic_mesh.js"></script></body></html>' }));
  await page.goto(base + "/webchat");
  await page.waitForFunction(() => window.UAMetallicMesh && UAMetallicMesh.lab.world());
  const clocks = () => page.evaluate(() => {
    const world = UAMetallicMesh.lab.world();
    return { mesh: world.time, reflection: world.shimmer };
  });
  const before = await clocks();
  await page.waitForTimeout(400);
  const moving = await clocks();
  assert.ok(moving.mesh > before.mesh && moving.reflection > before.reflection, "Web Chat must retain configured animation");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.waitForTimeout(100);
  const reduced = await clocks();
  await page.waitForTimeout(400);
  assert.deepEqual(await clocks(), reduced, "reduced motion must stop the dark background");
  await page.close();
}

async function checkScheduleAssignments(browser) {
  const page = await browser.newPage();
  const html = fs.readFileSync(path.join(assets, "chat.html"), "utf8").replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, "");
  await page.route("http://schedule-ui.test/**", route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/") return route.fulfill({ contentType: "text/html", body: html });
    const file = path.join(assets, path.basename(pathname));
    return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404 });
  });
  await page.goto("http://schedule-ui.test/");
  await page.evaluate(async () => {
    document.getElementById("chat-auth").hidden = true;
    document.getElementById("objectives-app").hidden = false;
    const { createRuntimeViews } = await import("/chat_runtime_views.mjs");
    const uiElement = (tag, className, text) => {
      const element = document.createElement(tag);
      element.className = className || "";
      element.textContent = text || "";
      return element;
    };
    const views = createRuntimeViews({
      sharedState: { objectivesContent: document.getElementById("objectives-content") },
      operations: { uiElement, timestampElement: () => null },
      runtime_viewsState: {}, lifecycle: {}, requestClient: {},
    });
    views.renderSchedules([
      { schedule_id: "daily-research", schedule_name: "Daily research", enabled: true, management_class: "user", worker_assignment: { agent_type: "WebResearchAgent", agent_id: null, automatic_creation: "enabled" } },
      { schedule_id: "pending-assignment", schedule_name: "Pending worker reassignment", enabled: false, management_class: "user", worker_assignment: { agent_type: "Worker".repeat(35), agent_id: "worker-123", automatic_creation: "disabled", reassignment_pending: true, desired_agent_type: "PersonalAdminAgent" } },
      { schedule_id: "older-schedule", schedule_name: "Existing schedule", enabled: true, management_class: "user", worker_assignment: { agent_type: "<img src=x onerror=alert(1)>", agent_id: null, automatic_creation: "not_configured" } },
      { schedule_id: "memory-review", schedule_name: "Memory review", enabled: true, management_class: "system", worker_assignment: { agent_type: "MemReviewAgent", agent_id: null, review_agent_type: "MemReviewAgent", automatic_creation: "default_policy" } },
    ]);
  });
  assert.equal(await page.locator(".objective-card").count(), 4);
  const content = await page.locator("#objectives-content").innerText();
  for (const expected of ["Assigned agent type: WebResearchAgent", "Enabled by TeamLead", "Disabled by TeamLead", "Not configured", "Pending agent type: PersonalAdminAgent", "Specific agent: worker-123"]) {
    assert.ok(content.includes(expected), expected);
  }
  assert.equal(await page.locator(".objective-card img").count(), 0, "assignment values must remain escaped text");
  // Cards are ordered by next run time, then schedule id; find the memory-review card by its name.
  const reviewContent = await page.locator(".objective-card", { hasText: "Memory review" }).innerText();
  for (const expected of ["Review agent: MemReviewAgent", "Managed by: Local scheduler", "Agent startup: Allowed by default policy"]) {
    assert.ok(reviewContent.includes(expected), expected);
  }
  assert.ok(!reviewContent.includes("Automatic creation"));
  for (const mode of ["dark", "light"]) {
    await page.evaluate(value => { document.documentElement.dataset.colorMode = value; }, mode);
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 900 });
      const geometry = await page.locator(".objective-card").evaluateAll(cards => cards.map(card => ({ width: card.clientWidth, scroll: card.scrollWidth })));
      assert.ok(geometry.every(card => card.scroll <= card.width + 1), `${mode}/${width}: long assignment text must stay inside its card`);
      if (process.env.UA_BROWSER_ARTIFACT_DIR) {
        await page.screenshot({ path: path.join(process.env.UA_BROWSER_ARTIFACT_DIR, `schedule-workers-${mode}-${width}.png`), fullPage: true });
      }
    }
  }
  await page.close();
}

async function checkSettings(browser) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const html = fs.readFileSync(path.join(assets, "chat.html"), "utf8").replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, "");
  await page.route("http://ui.test/**", route => {
    const pathname = new URL(route.request().url()).pathname;
    const file = path.join(assets, path.basename(pathname));
    if (pathname === "/") return route.fulfill({ contentType: "text/html", body: html });
    return fs.existsSync(file) ? route.fulfill({ path: file }) : route.fulfill({ status: 404 });
  });
  await page.goto("http://ui.test/");
  for (const script of ["action_feedback.js", "pagination.js", "task_controls.js"]) {
    await page.addScriptTag({ path: path.join(assets, script) });
  }
  // Load the shipped Settings module so the check follows the real Settings layout.
  await page.evaluate(async () => {
    window.pending = [];
    window.fetch = url => new Promise(resolve => pending.push({ url: String(url), resolve }));
    // Opening Team also reads the procedure lists; answer only the team read.
    window.answerTeam = body => {
      const index = pending.findIndex(entry => entry.url.startsWith("/api/web-chat/team?"));
      pending.splice(index, 1)[0].resolve(new Response(JSON.stringify(body)));
    };
    const { createSettingsControls } = await import("/settings_controls.mjs");
    window.controls = createSettingsControls({ returnFocus: document.getElementById("settings-button") });
    controls.open("team", "test-session");
  });
  await page.waitForTimeout(250);
  const top = () => page.locator("#settings-team-content").evaluate(node => node.getBoundingClientRect().top);
  const loadingTop = await top();
  await page.evaluate(() => answerTeam({ members: [] }));
  await page.waitForFunction(() => document.getElementById("settings-team-status").hidden);
  assert.equal(await top(), loadingTop);
  await page.evaluate(() => { window.teamNode = document.getElementById("settings-team-content").firstChild; });
  await page.locator("#settings-appearance-tab").click();
  await page.locator("#settings-team-tab").click();
  assert.equal(await top(), loadingTop, "Settings must reserve space for refresh messages");
  await page.evaluate(() => answerTeam({ members: [] }));
  await page.waitForFunction(() => document.getElementById("settings-team-status").hidden);
  assert.ok(await page.evaluate(() => teamNode === document.getElementById("settings-team-content").firstChild));
  await page.close();
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    await checkDocs(browser);
    await checkRuntimeMotion(browser);
    await checkSettings(browser);
    await checkScheduleAssignments(browser);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
