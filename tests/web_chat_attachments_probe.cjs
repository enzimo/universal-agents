// Headless Chromium check for Web Chat attachment responses (C05 + C12).
//   node web_chat_attachments_probe.cjs <origin> <token>
// Verifies against the real /api/web-chat/attachments/{id} route:
// - On a page with the real Web Chat CSP: PNG <img>, WAV <audio>, and WebM <video>
//   load; <script src> and <link rel="stylesheet"> pointing at attachments fail and
//   never run.
// - Opening an attachment URL directly as its own page: PNG displays with
//   naturalWidth > 0 in a sandboxed opaque origin; WAV, WebM, SVG, and forged HTML
//   download instead of rendering a dead player or running scripts.
const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const [origin, token] = process.argv.slice(2);
const REAL = "/api/web-chat/attachments/";

async function loadElement({ tag, url }) {
  return await new Promise((resolve) => {
    let element;
    const done = (state) => {
      const info = { state };
      if (tag === "img") info.naturalWidth = element.naturalWidth;
      if (tag === "audio" || tag === "video") info.duration = element.duration;
      resolve(info);
    };
    if (tag === "link") {
      element = document.createElement("link");
      element.rel = "stylesheet";
      element.href = url;
    } else {
      element = document.createElement(tag);
      element.src = url;
    }
    const okEvent = tag === "audio" || tag === "video" ? "loadedmetadata" : "load";
    element.addEventListener(okEvent, () => done("loaded"));
    element.addEventListener("error", () => done("error"));
    setTimeout(() => done("timeout"), 8000);
    (tag === "script" || tag === "link" ? document.head : document.getElementById("rail")).appendChild(element);
  });
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const report = {};
  try {
    const context = await browser.newContext({ acceptDownloads: true });
    const authRes = await context.request.post(`${origin}/api/web-chat/auth`, {
      data: { token },
    });
    assert.equal(authRes.status(), 200, "web chat auth failed");

    const page = await context.newPage();
    await page.goto(`${origin}/probe/page`, { waitUntil: "load" });

    // 1. Elements embedded on a page carrying the real Web Chat page policy.
    report.on_page = {
      img_png: await page.evaluate(loadElement, { tag: "img", url: `${REAL}att_png` }),
      audio_wav: await page.evaluate(loadElement, { tag: "audio", url: `${REAL}att_wav` }),
      video_webm: await page.evaluate(loadElement, { tag: "video", url: `${REAL}att_webm` }),
      script_js: await page.evaluate(loadElement, { tag: "script", url: `${REAL}att_js` }),
      script_ran: await page.evaluate(() => window.__attachment_script_ran === true),
      style_css: await page.evaluate(loadElement, { tag: "link", url: `${REAL}att_css` }),
    };

    assert.equal(report.on_page.img_png.state, "loaded");
    assert.ok(report.on_page.img_png.naturalWidth > 0);
    assert.equal(report.on_page.audio_wav.state, "loaded");
    assert.ok(report.on_page.audio_wav.duration > 0);
    assert.equal(report.on_page.video_webm.state, "loaded");
    assert.ok(report.on_page.video_webm.duration > 0);
    assert.equal(report.on_page.script_js.state, "error");
    assert.equal(report.on_page.script_ran, false);
    assert.equal(report.on_page.style_css.state, "error");

    // 2. Direct navigation to each attachment URL as its own page.
    report.own_page = {};
    const pngTab = await context.newPage();
    await pngTab.goto(`${origin}${REAL}att_png`, { waitUntil: "load" });
    report.own_page.png = await pngTab.evaluate(() => {
      const img = document.querySelector("img");
      return {
        page_origin: self.origin,
        naturalWidth: img ? img.naturalWidth : 0,
      };
    });
    await pngTab.close();
    assert.equal(report.own_page.png.page_origin, "null");
    assert.ok(report.own_page.png.naturalWidth > 0);

    for (const [id, expectedFilename] of [
      ["att_wav", "tone.wav"],
      ["att_webm", "clip.webm"],
      ["att_svg", "vector.svg"],
      ["att_html", "fake.png"],
    ]) {
      const tab = await context.newPage();
      const downloadPromise = tab.waitForEvent("download", { timeout: 5000 });
      await tab.goto(`${origin}${REAL}${id}`, { waitUntil: "load" }).catch(() => {});
      const download = await downloadPromise;
      const suggested = download.suggestedFilename();
      await tab.close();
      report.own_page[id] = { downloaded_as: suggested };
      assert.equal(suggested, expectedFilename, `expected ${id} to download as ${expectedFilename}`);
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(report));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
