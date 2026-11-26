// Headless Chromium check for Web Chat attachment responses (C05 + C12).
//   node web_chat_attachments_probe.cjs <origin> <token>
// Verifies against the real /api/web-chat/attachments/{id} route:
// - On a page with the real Web Chat CSP: PNG <img>, WAV <audio>, and WebM <video>
//   load; <script src> and <link rel="stylesheet"> pointing at attachments fail and
//   never run.
// - Opening an attachment URL directly as its own page: PNG displays with
//   naturalWidth > 0 in a sandboxed opaque origin; WAV, WebM, SVG, and forged HTML
//   download instead of rendering a dead player or running scripts.
// - The real /webchat page renders a transcript reply through chat.js ->
//   chat_attachments.mjs: the PNG thumbnail loads and opens in an opaque-origin tab,
//   the WAV and WebM players load, and the SVG and forged HTML show as file chips
//   that download under their own names, with no page script errors.
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

    // 3. Real /webchat page rendering a transcript reply through chat.js -> chat_attachments.mjs.
    const chatPage = await context.newPage();
    const pageErrors = [];
    chatPage.on("pageerror", (error) => pageErrors.push(String(error)));
    await chatPage.goto(`${origin}/webchat`, { waitUntil: "domcontentloaded" });
    await chatPage.waitForSelector(".message-attachments", { timeout: 10000 });

    const webChatState = await chatPage.evaluate(async () => {
      const waitFor = (fn, timeoutMs = 8000) =>
        new Promise((resolve) => {
          const deadline = Date.now() + timeoutMs;
          const poll = () => {
            if (fn() || Date.now() >= deadline) {
              resolve(fn());
              return;
            }
            setTimeout(poll, 50);
          };
          poll();
        });

      const images = Array.from(document.querySelectorAll(".message-image-attachment img"));
      const audios = Array.from(document.querySelectorAll("audio.message-media-attachment"));
      const videos = Array.from(document.querySelectorAll("video.message-media-attachment"));
      const chips = Array.from(document.querySelectorAll("a.message-file-attachment"));

      await waitFor(() => images.every((img) => img.complete && img.naturalWidth > 0));
      await waitFor(() =>
        audios.every((el) => el.readyState >= 1 && Number.isFinite(el.duration) && el.duration > 0)
      );
      await waitFor(() =>
        videos.every((el) => el.readyState >= 1 && Number.isFinite(el.duration) && el.duration > 0)
      );

      return {
        imageAlts: images.map((img) => img.alt),
        imageNaturalWidths: images.map((img) => img.naturalWidth),
        audioDurations: audios.map((el) => el.duration),
        videoDurations: videos.map((el) => el.duration),
        chipLabels: chips.map((chip) => {
          const label = chip.querySelector(".message-file-label");
          return label ? label.textContent.trim() : "";
        }),
      };
    });

    assert.deepEqual(webChatState.imageAlts, ["pic.png"]);
    assert.deepEqual(webChatState.imageNaturalWidths, [8]);
    assert.equal(webChatState.audioDurations.length, 1);
    assert.ok(webChatState.audioDurations[0] > 0);
    assert.equal(webChatState.videoDurations.length, 1);
    assert.ok(webChatState.videoDurations[0] > 0);
    assert.deepEqual(webChatState.chipLabels, ["vector.svg", "fake.png"]);

    // Clicking the raster thumbnail opens an isolated opaque-origin tab.
    const [thumbTab] = await Promise.all([
      context.waitForEvent("page", { timeout: 5000 }),
      chatPage.locator(".message-image-attachment").first().click(),
    ]);
    await thumbTab.waitForLoadState("load");
    const thumbTabInfo = await thumbTab.evaluate(() => {
      const img = document.querySelector("img");
      return {
        page_origin: self.origin,
        naturalWidth: img ? img.naturalWidth : 0,
      };
    });
    await thumbTab.close();
    assert.equal(thumbTabInfo.page_origin, "null");
    assert.equal(thumbTabInfo.naturalWidth, 8);

    // Clicking each non-raster file chip downloads under its filename without running scripts.
    const chipDownloads = {};
    for (const label of ["vector.svg", "fake.png"]) {
      const chip = chatPage
        .locator("a.message-file-attachment")
        .filter({ hasText: label })
        .first();
      const download = await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`timed out downloading ${label}`)), 5000);
        const finish = (dl) => {
          clearTimeout(timer);
          chatPage.off("download", onDownload);
          context.off("page", onPopup);
          resolve(dl);
        };
        const onDownload = (dl) => finish(dl);
        const onPopup = (popup) => {
          popup.once("download", (dl) => {
            popup.close().catch(() => {});
            finish(dl);
          });
        };
        chatPage.on("download", onDownload);
        context.on("page", onPopup);
        chip.click().catch((err) => {
          clearTimeout(timer);
          chatPage.off("download", onDownload);
          context.off("page", onPopup);
          reject(err);
        });
      });
      chipDownloads[label] = download.suggestedFilename();
      assert.equal(chipDownloads[label], label);
    }

    assert.deepEqual(pageErrors, []);
    await chatPage.close();
    report.web_chat = {
      ...webChatState,
      thumb_tab: thumbTabInfo,
      chip_downloads: chipDownloads,
      page_errors: pageErrors,
    };
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(report));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
