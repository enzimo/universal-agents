// Popups opening and closing in glass (panel_motion.js, glass_mesh.js), run
// against the small DOM stand-in with a fake 2D canvas and a fake clock.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { byClass, fakeDocument } from "./settings_fake_dom.mjs";

const ASSETS = new URL("../assistant_gateway/onboarding_web/assets/", import.meta.url);
const GLASS = readFileSync(new URL("glass_mesh.js", ASSETS), "utf8");
const PANEL = readFileSync(new URL("panel_motion.js", ASSETS), "utf8");
const RECT = { left: 100, top: 80, width: 600, height: 400 };

function fakeContext(canvas) {
  return {
    fillStyle: "", strokeStyle: "", lineWidth: 1,
    setTransform() {}, clearRect() { canvas.fills = []; }, beginPath() {}, moveTo() {}, lineTo() {}, closePath() {},
    stroke() {},
    fill() { canvas.fills.push(this.fillStyle); },
  };
}

function page({ motion = true, glass = { base: "#1c4f63", edge: "#62d5f3" }, rect = RECT } = {}) {
  const document = fakeDocument();
  document.readyState = "complete";
  const create = document.createElement.bind(document);
  document.createElement = tagName => {
    const element = create(tagName);
    element.style = {};
    element.getBoundingClientRect = () => ({ ...rect });
    if (tagName === "canvas") {
      element.fills = [];
      element.getContext = () => fakeContext(element);
    }
    return element;
  };
  const frames = [];
  const motionListeners = [];
  const observers = [];
  const timers = new Map();
  const warnings = [];
  let nextTimer = 1;
  let clock = 0;
  const state = { motion, glass };
  const window = Object.assign(new EventTarget(), {
    document,
    devicePixelRatio: 1,
    // A laptop-sized browser window: the 600x400 popup covers about a fifth of it.
    innerWidth: 1440,
    innerHeight: 900,
    performance: { now: () => clock },
    // The interface glass comes from CSS (--ua-glass-base and --ua-glass-edge).
    getComputedStyle: () => ({
      borderTopLeftRadius: "14px",
      backgroundColor: "rgba(0, 0, 0, 0.62)",
      getPropertyValue: name => ({ "--ua-glass-base": state.glass.base, "--ua-glass-edge": state.glass.edge })[name] || "",
    }),
    requestAnimationFrame: callback => { frames.push(callback); return frames.length; },
    cancelAnimationFrame() {},
    console: { warn: message => warnings.push(message) },
    setTimeout: (callback, delay) => { timers.set(nextTimer, { callback, at: clock + delay }); return nextTimer++; },
    clearTimeout: id => timers.delete(id),
    MutationObserver: class {
      constructor(callback) { this.callback = callback; observers.push(this); }
      observe() {}
    },
    UAMotion: {
      enabled: () => state.motion,
      palette: () => [{ rgb: [60, 212, 104], weight: 1 }],
      onChange: listener => motionListeners.push(listener),
    },
    UAAppearance: { current: () => ({ mode: "dark", dark: { lightX: -7, lightY: -5 } }) },
  });
  const context = vm.createContext({ window, document, Math, Object, Array, Map, Set, String, Number });
  vm.runInContext(GLASS, context);
  vm.runInContext(PANEL, context);
  return {
    window, document, state, observers, warnings,
    motion: window.UAPanelMotion,
    advanceTo(ms) {
      clock = ms;
      frames.splice(0).forEach(callback => callback(clock));
    },
    // Time passes with no animation frames (a browser that stopped painting).
    passWithoutFrames(ms) {
      clock = ms;
      [...timers].filter(([, timer]) => timer.at <= clock).forEach(([id, timer]) => {
        timers.delete(id);
        timer.callback();
      });
    },
    pendingTimers: () => timers.size,
    motionChanged() { motionListeners.forEach(listener => listener()); },
  };
}

// A setup-style popup: `.setup-overlay > .setup-dialog` with some content.
function setupPopup(env, { hidden = false } = {}) {
  const overlay = env.document.createElement("div");
  overlay.className = "setup-overlay";
  overlay.hidden = hidden;
  const dialog = env.document.createElement("section");
  dialog.className = "setup-dialog";
  const heading = env.document.createElement("h2");
  heading.textContent = "Connect Discord";
  dialog.append(heading);
  overlay.append(dialog);
  env.document.body.append(overlay);
  return { overlay, dialog };
}

const effectLayers = node => byClass(node, "ua-panel-effect");
const parseRgba = text => text.match(/[\d.]+/g).map(Number);

test("a popup forms from triangles coming into focus, the breaking run backwards", () => {
  const env = page();
  const { overlay, dialog } = setupPopup(env);
  env.motion.sync();
  const [layer] = effectLayers(overlay);
  assert.ok(layer, "the effect sits in the overlay, above the popup");
  assert.equal(layer.style.left, "68px", "with room around the popup for drifting triangles");
  assert.equal(layer.style.width, "664px");
  assert.ok(dialog.classList.contains("ua-panel-opening"), "the popup's own surface waits under the glass");
  const levels = byClass(layer, "ua-panel-glass");
  assert.equal(levels.length, 5);
  const count = () => levels.map(level => level.fills.length);

  // Nothing at first; then triangles arrive blurred, each at its own time.
  assert.deepEqual(count(), [0, 0, 0, 0, 0]);
  env.advanceTo(300);
  const early = count();
  assert.ok(early[3] + early[4] > 0, `the first triangles arrive blurred: ${early}`);
  assert.ok(early[0] < 20, `few are in focus yet: ${early}`);

  // Toward the end most are in focus, in the interface's glass color.
  env.advanceTo(env.motion.inspect().closingMaxMs - 60);
  const late = count();
  assert.ok(late[0] > 100, `most are sharp: ${late}`);
  const [r, g, b] = parseRgba(levels[0].fills[0]);
  assert.ok(b > r && g > r, `teal interface glass, got ${levels[0].fills[0]}`);
  assert.ok(dialog.classList.contains("ua-panel-opening"), "the popup waits until the glass has formed");

  // Once formed, the popup returns under the glass as the glass fades.
  env.advanceTo(env.motion.inspect().closingMaxMs + 10);
  assert.ok(dialog.classList.contains("ua-panel-revealing"));
  assert.ok(!dialog.classList.contains("ua-panel-opening"));
  assert.ok(Number(layer.style.opacity) < 1);
  env.advanceTo(env.motion.inspect().openingMs + 20);
  assert.equal(effectLayers(overlay).length, 0, "the effect is gone");
  assert.ok(!dialog.classList.contains("ua-panel-revealing"));
  assert.equal(env.motion.inspect().effects, 0);
});

test("closing breaks the popup's glass into triangles above the page after the page removed it", () => {
  const env = page();
  const { overlay } = setupPopup(env);
  env.motion.sync();
  env.advanceTo(2000);
  overlay.remove();
  env.motion.sync();
  const [layer] = effectLayers(env.document.body);
  assert.ok(layer.classList.contains("is-closing"));
  assert.equal(layer.style.top, "48px", "where the popup was, with room around it");
  assert.equal(layer.style.height, "464px");
  const [backdrop] = byClass(env.document.body, "ua-panel-backdrop");
  assert.equal(backdrop.style.background, "rgba(0, 0, 0, 0.62)", "the backdrop fades out under the glass");

  // Five levels of blur; at first the whole popup is sharp glass.
  const levels = byClass(layer, "ua-panel-glass");
  assert.equal(levels.length, 5);
  assert.deepEqual(levels.map(level => level.style.filter || ""), ["", "blur(2px)", "blur(4.5px)", "blur(8px)", "blur(13px)"]);
  assert.ok(levels[0].fills.length > 100);
  assert.equal(levels[4].fills.length, 0);

  // Each triangle goes at its own rate: midway, some are still sharp while
  // others have blurred far.
  env.advanceTo(2000 + 500);
  assert.ok(levels[1].fills.length > 0 && levels[3].fills.length + levels[4].fills.length > 0,
    `spread across the blur levels: ${levels.map(level => level.fills.length)}`);
  assert.equal(Number(backdrop.style.opacity), 0);

  env.advanceTo(2000 + env.motion.inspect().closingMaxMs + 20);
  assert.equal(effectLayers(env.document.body).length, 0);
  assert.equal(byClass(env.document.body, "ua-panel-backdrop").length, 0);
});

test("hiding an overlay closes its popup, and showing it again opens it", () => {
  const env = page();
  const { overlay } = setupPopup(env, { hidden: true });
  env.motion.sync();
  assert.equal(env.motion.inspect().open, 0, "a hidden overlay is not open");
  overlay.hidden = false;
  env.motion.sync();
  assert.equal(env.motion.inspect().open, 1);
  env.advanceTo(2000);
  overlay.hidden = true;
  env.motion.sync();
  assert.equal(env.motion.inspect().open, 0);
  assert.equal(effectLayers(env.document.body).length, 1, "the closing glass plays");
});

test("closing a popup while it opens ends the opening at once", () => {
  const env = page();
  const { overlay, dialog } = setupPopup(env);
  env.motion.sync();
  env.advanceTo(120);
  overlay.hidden = true;
  env.motion.sync();
  assert.equal(effectLayers(overlay).length, 0, "the opening effect is gone");
  assert.ok(!dialog.classList.contains("ua-panel-opening"));
  assert.equal(effectLayers(env.document.body).length, 1, "the closing glass plays");
  assert.equal(env.motion.inspect().effects, 1);
});

test("native dialogs get the glass inside them and close through open", () => {
  const env = page();
  const dialog = env.document.createElement("dialog");
  env.document.body.append(dialog);
  env.motion.sync();
  assert.equal(effectLayers(dialog).length, 0, "a closed dialog plays nothing");
  dialog.open = true;
  env.motion.sync();
  const [layer] = effectLayers(dialog);
  assert.ok(layer.classList.contains("is-inside"), "inside the dialog, which sits above the page");
  assert.equal(layer.style.left, undefined, "it fills the dialog through CSS");
  env.advanceTo(2000);
  dialog.open = false;
  env.motion.sync();
  assert.ok(effectLayers(env.document.body).some(node => node.classList.contains("is-closing")));
});

test("a popup still shows when the browser delivers no animation frames", () => {
  const env = page();
  const { overlay, dialog } = setupPopup(env);
  env.motion.sync();
  assert.ok(dialog.classList.contains("ua-panel-opening"));
  env.passWithoutFrames(400);
  assert.ok(dialog.classList.contains("ua-panel-opening"), "not before the safety time");
  env.passWithoutFrames(env.motion.inspect().openingMs + env.motion.inspect().safetyMs + 10);
  assert.ok(!dialog.classList.contains("ua-panel-opening"), "the popup shows");
  assert.equal(effectLayers(overlay).length, 0);
  assert.match(env.warnings[0], /no animation frames arrived; ended the opening effect/);
});

test("an effect that ends normally clears its safety timer", () => {
  const env = page();
  setupPopup(env);
  env.motion.sync();
  assert.equal(env.pendingTimers(), 1);
  env.advanceTo(env.motion.inspect().openingMs + 20);
  assert.equal(env.pendingTimers(), 0);
  assert.equal(env.warnings.length, 0);
});

test("without interface motion popups open and close at once", () => {
  const env = page({ motion: false });
  const { overlay, dialog } = setupPopup(env);
  env.motion.sync();
  assert.equal(effectLayers(overlay).length, 0);
  assert.ok(!dialog.classList.contains("ua-panel-opening"));
  overlay.remove();
  env.motion.sync();
  assert.equal(effectLayers(env.document.body).length, 0);
  assert.equal(env.motion.inspect().effects, 0);
});

test("a large window such as Settings opens and closes without glass", () => {
  // 1400x820 of a 1440x900 window: well over the large-window share.
  const env = page({ rect: { left: 20, top: 40, width: 1400, height: 820 } });
  const { overlay, dialog } = setupPopup(env);
  env.motion.sync();
  assert.equal(env.motion.inspect().open, 1, "it is still watched");
  assert.equal(effectLayers(overlay).length, 0, "no opening glass");
  assert.ok(!dialog.classList.contains("ua-panel-opening"), "it shows at once");
  env.advanceTo(2000);
  overlay.remove();
  env.motion.sync();
  assert.equal(effectLayers(env.document.body).length, 0, "no closing glass");
  assert.equal(byClass(env.document.body, "ua-panel-backdrop").length, 0);
  assert.equal(env.motion.inspect().effects, 0);
});

test("the large-window share is measured against the browser window, not a fixed size", () => {
  const env = page();
  // The same 600x400 popup fills most of a phone-sized window.
  env.window.innerWidth = 620;
  env.window.innerHeight = 480;
  const { overlay } = setupPopup(env);
  env.motion.sync();
  assert.equal(effectLayers(overlay).length, 0, "large on a small screen: no glass");
  overlay.remove();
  env.motion.sync();
  // Back in a laptop-sized window it is small and plays.
  env.window.innerWidth = 1440;
  env.window.innerHeight = 900;
  const second = setupPopup(env);
  env.motion.sync();
  assert.equal(effectLayers(second.overlay).length, 1);
  assert.equal(env.motion.inspect().largeWindowShare, 0.6);
});

test("turning motion off ends every effect at once", () => {
  const env = page();
  const { overlay, dialog } = setupPopup(env);
  env.motion.sync();
  env.advanceTo(100);
  env.state.motion = false;
  env.motionChanged();
  assert.equal(effectLayers(overlay).length, 0);
  assert.ok(!dialog.classList.contains("ua-panel-opening"), "the popup shows normally");
  assert.equal(env.motion.inspect().effects, 0);
});

test("the watcher looks again only when a popup could have opened or closed", () => {
  const env = page();
  const [observer] = env.observers;
  assert.ok(observer, "the page is watched");
  const { overlay } = setupPopup(env);
  // The chat transcript changing does not make it look.
  const message = env.document.createElement("article");
  observer.callback([{ type: "childList", addedNodes: [message], removedNodes: [] }]);
  assert.equal(env.motion.inspect().open, 0);
  // The overlay's hidden attribute or its arrival does.
  observer.callback([{ type: "attributes", target: overlay }]);
  assert.equal(env.motion.inspect().open, 1);
  const holder = env.document.createElement("div");
  const dialog = env.document.createElement("dialog");
  dialog.open = true;
  holder.append(dialog);
  env.document.body.append(holder);
  observer.callback([{ type: "childList", addedNodes: [holder], removedNodes: [] }]);
  assert.equal(env.motion.inspect().open, 2, "a dialog inside added content counts");
});

test("the glass is the interface glass of the current color mode", () => {
  const firstFill = layer => byClass(layer, "ua-panel-glass").flatMap(level => level.fills)[0];
  const env = page({ glass: { base: "#5a1010", edge: "#ff8080" } });
  const first = setupPopup(env);
  env.motion.sync();
  env.advanceTo(600);
  const red = firstFill(effectLayers(first.overlay)[0]);
  const [r1, g1, b1] = parseRgba(red);
  assert.ok(r1 > g1 * 2 && r1 > b1 * 2, `the CSS glass color, got ${red}`);

  // The mode (and so the CSS glass) changes: the next popup uses the new glass.
  env.state.glass = { base: "#10205a", edge: "#8080ff" };
  const second = setupPopup(env);
  env.motion.sync();
  env.advanceTo(600 + 600);
  const blue = firstFill(effectLayers(second.overlay)[0]);
  const [r2, g2, b2] = parseRgba(blue);
  assert.ok(b2 > r2 * 2 && b2 > g2, `the new glass, got ${blue}`);
});

test("the effects' glass is the interface's frosted glass in each color mode, as the word always was", () => {
  const css = readFileSync(new URL("styles.css", ASSETS), "utf8");
  const block = selector => css.slice(css.indexOf(selector + " {"), css.indexOf("}", css.indexOf(selector + " {")));
  const light = block(":root");
  const dark = block(':root[data-color-mode="dark"]');
  // The word's own colors (owner's decision, 2026-10-09: back to these).
  assert.match(light, /--ua-glass-start: rgba\(255, 255, 255, 0\.78\);/);
  assert.match(light, /--ua-glass-outline: rgba\(7, 89, 133, 0\.75\);/);
  assert.match(dark, /--ua-glass-start: rgba\(23, 52, 64, 0\.88\);/);
  assert.match(dark, /--ua-glass-outline: rgba\(98, 213, 243, 0\.75\);/);
  // The facets and the word fill read the same glass.
  for (const mode of [light, dark]) {
    assert.match(mode, /--ua-glass-base: #[0-9a-f]{6};/);
    assert.match(mode, /--ua-glass-edge: #[0-9a-f]{6};/);
    assert.match(mode, /--ua-intro-word-fill-start: var\(--ua-glass-start\);/);
  }
});
