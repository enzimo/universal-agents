// The onboarding intro's title, "Universal Agents" (intro_word.js), formed letter by
// letter, run with a fake 2D canvas whose "letters" are a few vertical bars.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { fakeDocument, settle } from "./settings_fake_dom.mjs";

const SOURCE = readFileSync(new URL("../assistant_gateway/onboarding_web/assets/intro_word.js", import.meta.url), "utf8");
const GLASS = readFileSync(new URL("../assistant_gateway/onboarding_web/assets/glass_mesh.js", import.meta.url), "utf8");
const FONT_SIZE = 128;
const TITLE = "Universal Agents";

function fakeContext(canvas, log) {
  return {
    font: "",
    letterSpacing: "0px",
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
    globalCompositeOperation: "source-over",
    setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, closePath() {},
    globalAlpha: 1,
    save() {}, restore() {}, stroke() {}, fillRect() {}, rect() {},
    clip() { log.clips += 1; },
    createLinearGradient() { return { addColorStop() {} }; },
    fill() {
      log.fills += 1;
      log.fillStyles.push(this.fillStyle);
      canvas.fills = (canvas.fills || 0) + 1;
      canvas.paints = (canvas.paints || 0) + 1;
    },
    drawImage(source) {
      log.draws += 1;
      if (source && source.isInnerMesh) log.innerDraws += 1;
      canvas.paints = (canvas.paints || 0) + 1;
    },
    fillText(text) { log.texts.push(text); },
    measureText(text) { return { width: text.length * FONT_SIZE * 0.6 }; },
    // Letters: bars 30 px wide every 70 px, across the middle of the canvas.
    getImageData(x, y, width, height) {
      const data = new Uint8ClampedArray(width * height * 4);
      for (let py = Math.round(height * 0.3); py < Math.round(height * 0.7); py += 1) {
        for (let px = 0; px < width; px += 1) {
          if (px % 70 < 30) data[(py * width + px) * 4 + 3] = 255;
        }
      }
      return { data };
    },
  };
}

function page({ motion = true, glass = null } = {}) {
  const document = fakeDocument();
  const log = { fills: 0, fillStyles: [], draws: 0, texts: [], innerDraws: 0, meshFrames: [], clips: 0 };
  const create = document.createElement.bind(document);
  document.createElement = tagName => {
    const element = create(tagName);
    element.style = {};
    if (tagName === "canvas") {
      element.context = fakeContext(element, log);
      element.getContext = () => element.context;
    }
    return element;
  };
  document.fonts = { ready: Promise.resolve() };
  const frames = [];
  let clock = 0;
  const window = {
    document,
    devicePixelRatio: 1,
    performance: { now: () => clock },
    getComputedStyle: () => ({
      fontSize: FONT_SIZE + "px", fontWeight: "880", fontFamily: "Inter", letterSpacing: "2px",
      // The interface glass (styles.css); empty unless a test sets it.
      getPropertyValue: name => (glass ? ({ "--ua-glass-base": glass.base, "--ua-glass-edge": glass.edge })[name] || "" : ""),
    }),
    requestAnimationFrame: callback => { frames.push(callback); return frames.length; },
    cancelAnimationFrame() {},
    UAMotion: { enabled: () => motion, palette: () => [{ rgb: [60, 212, 104], weight: 1 }] },
    UAAppearance: { current: () => ({ mode: "dark", dark: { lightX: -7, lightY: -5 } }) },
    // The chat bubble's mesh (ui_motion.js); records each frame's motion and strength.
    UAMeshGlyph: {
      create(canvas) {
        canvas.isInnerMesh = true;
        return {
          triangleCount: () => 12,
          draw(time, motion, styleFor) { log.meshFrames.push({ motion, fill: styleFor(0).fill }); },
        };
      },
    },
  };
  const context = vm.createContext({ window, document, Math, Promise, Object });
  vm.runInContext(GLASS, context);
  vm.runInContext(SOURCE, context);
  return {
    window, document, log,
    advanceTo(ms) {
      clock = ms;
      frames.splice(0).forEach(callback => callback(clock));
    },
  };
}

test("a letter still forming gets no outline: its settled facets stay off the outlined canvas", async () => {
  const env = page();
  const title = env.document.createElement("h1");
  title.textContent = TITLE;
  env.document.body.append(title);
  const word = env.window.UAIntroWord.create(title, { text: TITLE });
  word.play(() => {});
  await settle();
  const canvas = name => title.children.find(child => child.className === name);
  const outlined = canvas("ua-intro-word-facets");
  const settledFacets = canvas("ua-intro-word-settled");
  assert.ok(outlined && settledFacets, "the settled facets have their own canvas");
  const children = title.children.map(child => child.className);
  assert.ok(children.indexOf("ua-intro-word-settled") < children.indexOf("ua-intro-word-facets"), "under the formed word");

  // The outline filter strengthens faint alpha, so anything drawn on its
  // canvas before a letter has formed shows as a flickering rim (2026-10-11).
  env.advanceTo(word.inspect().lettersFormedMs[0] - 100);
  // Copying the mask also counts as a paint, even if no facets were drawn.
  assert.ok(settledFacets.fills > 0, "facets have settled into the first letter");
  assert.equal(outlined.paints || 0, 0, "nothing reaches the outlined canvas before a letter has formed");
});

test("the blurred layers keep their blur inside their own canvas", async () => {
  const env = page();
  const title = env.document.createElement("h1");
  title.textContent = TITLE;
  env.document.body.append(title);
  const word = env.window.UAIntroWord.create(title, { text: TITLE });
  word.play(() => {});
  await settle();
  const canvas = name => title.children.find(child => child.className === name);
  const width = parseFloat(title.style.width);
  const height = parseFloat(title.style.height);
  // WebKit repaints only a canvas's own box when its drawing changes, so a
  // blur spreading past the box left a shadow framing the word (2026-10-12).
  // Each blurred canvas has a margin of three blur radii around the word.
  for (const [name, blur] of [["ua-intro-word-slab", 10], ["ua-intro-word-forming", 3]]) {
    const layer = canvas(name);
    const margin = blur * 3;
    assert.equal(layer.style.filter, `blur(${blur}px)`, name);
    assert.equal(layer.style.left, `${-margin}px`, name);
    assert.equal(layer.style.top, `${-margin}px`, name);
    assert.equal(parseFloat(layer.style.width), width + margin * 2, name);
    assert.equal(parseFloat(layer.style.height), height + margin * 2, name);
  }
  // The sharp layers stay exactly over the word.
  for (const name of ["ua-intro-word-settled", "ua-intro-word-facets"]) {
    assert.equal(canvas(name).style.left, "0px", name);
    assert.equal(parseFloat(canvas(name).style.width), width, name);
  }
});

test("triangles form each letter, which then hands over to its flat mesh", async () => {
  const env = page();
  const title = env.document.createElement("h1");
  title.textContent = TITLE;
  env.document.body.append(title);
  const word = env.window.UAIntroWord.create(title, { text: TITLE });
  let done = 0;
  word.play(() => { done += 1; });
  await settle();
  const shape = word.inspect();
  assert.equal(shape.letters, 15, "one handover per letter; the space forms none");
  // The owner's pace (2026-10-09): about 40% faster than the earlier 4.6–5 s.
  assert.ok(shape.completeMs > 3000 && shape.completeMs <= 3600, `formed in about 3.5 s, got ${shape.completeMs} ms`);
  assert.ok(shape.triangles > 200, `a fine mesh, got ${shape.triangles}`);
  assert.ok(shape.inWord > 0 && shape.inWord < shape.triangles, "the letters keep only some triangles");
  assert.deepEqual(env.log.texts, [TITLE], "the letters are written once, as the mask");
  // Letters form from left to right.
  const formed = shape.lettersFormedMs;
  assert.ok(formed.every((time, index) => index === 0 || time > formed[index - 1]), `left to right: ${formed}`);
  // The real word stays readable for screen readers; the canvases are decoration.
  assert.equal(title.children[0].textContent, TITLE);
  assert.ok(title.classList.contains("has-polygon-word"));
  assert.ok(title.children.slice(1).every(canvas => canvas.getAttribute("aria-hidden") === "true"));

  // Before the first letter has formed: triangles turn, no styled letter, no flat mesh.
  env.advanceTo(formed[0] - 100);
  assert.ok(env.log.fills > 0, "triangles are turning");
  assert.equal(env.log.clips, 0, "no letter has handed over yet");
  assert.equal(env.log.meshFrames.length, 0, "no flat mesh yet");

  // The first letter hands over while later letters are still forming.
  env.advanceTo((formed[0] + formed[1]) / 2);
  assert.equal(env.log.clips, 1, "only the first letter has handed over");
  assert.ok(env.log.meshFrames.length > 0, "its flat mesh has started");
  assert.equal(done, 0);

  // Letters are drawn in order, so the frame's first mesh is the first letter's.
  const before = env.log.meshFrames.length;
  env.advanceTo(formed[0] + 1500);
  assert.ok(env.log.meshFrames[before].motion < 0.6, "the flat meshes slow down");

  env.advanceTo(shape.completeMs + 20);
  assert.equal(done, 1, "complete once every letter is in");
  assert.ok(env.log.meshFrames.every(frame => frame.fill.startsWith("rgba(60, 212, 104,")), "in the theme colors");

  // Then random letters keep flashing a new mesh that slowly fades: a few
  // glow at any moment, and over time the flashes reach most of the word.
  const lit = new Set();
  let fewest = Infinity;
  let most = 0;
  for (let ms = shape.formingOverMs + 3000; ms <= shape.formingOverMs + 30000; ms += 100) {
    const drawnBefore = env.log.meshFrames.length;
    env.advanceTo(ms);
    const glowing = shape.glowingAt(ms);
    assert.equal(env.log.meshFrames.length - drawnBefore, glowing.length, "each glowing letter's mesh is drawn");
    glowing.forEach(index => lit.add(index));
    fewest = Math.min(fewest, glowing.length);
    most = Math.max(most, glowing.length);
  }
  assert.ok(fewest >= 2 && most <= 6, `a few letters glow at once: ${fewest}..${most}`);
  assert.ok(lit.size >= 12, `the flashes move around the word: ${lit.size} letters`);
  assert.equal(done, 1, "the completion callback runs once");

  // Dismissing the intro stops them.
  word.stop();
  const drawn = env.log.meshFrames.length;
  env.advanceTo(shape.formingOverMs + 32000);
  assert.equal(env.log.meshFrames.length, drawn, "the animation has stopped");
});

test("the glass is the interface glass and follows a color mode change while the word shows", async () => {
  const glass = { base: "#5a1010", edge: "#ff8080" };
  const env = page({ glass });
  const title = env.document.createElement("h1");
  title.textContent = TITLE;
  env.document.body.append(title);
  const word = env.window.UAIntroWord.create(title, { text: TITLE });
  word.play(() => {});
  await settle();
  const rgb = style => style.match(/[\d.]+/g).slice(0, 3).map(Number);
  // The slab, in the interface glass (--ua-glass-base).
  env.advanceTo(300);
  const red = env.log.fillStyles.map(rgb);
  assert.ok(red.length > 0 && red.every(([r, g, b]) => r >= g && r >= b), "red glass while forming");

  // The color mode changes (or a theme preview): the glass follows.
  Object.assign(glass, { base: "#10205a", edge: "#8080ff" });
  env.document.dispatchEvent(new Event("ua:appearance-change"));
  env.log.fillStyles.length = 0;
  env.advanceTo(400);
  const blue = env.log.fillStyles.map(rgb);
  assert.ok(blue.length > 0 && blue.some(([r, , b]) => b > r * 2), "blue glass after the change");
  word.stop();
});

test("an intro dismissed before its fonts are ready never starts", async () => {
  const env = page();
  const title = env.document.createElement("h1");
  env.document.body.append(title);
  const word = env.window.UAIntroWord.create(title, { text: TITLE });
  word.play();
  word.stop();
  await settle();
  env.advanceTo(10000);
  assert.equal(word.inspect(), null, "the word was never built");
  assert.equal(env.log.meshFrames.length, 0, "no frames are drawn");
});

test("without motion the finished word is drawn at once", async () => {
  const env = page({ motion: false });
  const title = env.document.createElement("h1");
  env.document.body.append(title);
  const word = env.window.UAIntroWord.create(title, { text: TITLE });
  word.showStill();
  await settle();
  assert.equal(word.inspect().letters, 15);
  // The styled word only: copied onto the canvas, with no slab and no frames.
  assert.ok(env.log.draws > 0, "the styled word is drawn");
  assert.equal(env.log.fills, 0, "no slab triangles");
  assert.equal(env.log.meshFrames.length, 0, "no inner mesh");
});

// Web Chat's sign-in greeting comes as parts that share one line when it fits.
const GREETING = ["Good Afternoon,", "Alexandra!"];

test("a greeting too wide for its room puts the name on a second line", async () => {
  const env = page();
  const title = env.document.createElement("h1");
  env.document.body.append(title);
  const word = env.window.UAIntroWord.create(title, { lines: GREETING, maxWidth: () => 800 });
  word.play();
  await settle();
  const shape = word.inspect();
  assert.equal(shape.lines, 2);
  assert.deepEqual(env.log.texts, GREETING, "each line is written into the mask");
  assert.equal(title.children[0].textContent, GREETING.join(" "), "screen readers hear the whole greeting");
  assert.equal(shape.letters, 24, "the space forms no letter");
  // The first line's letters own the top band and the name's the bottom band.
  const first = shape.cells.slice(0, 14);
  const second = shape.cells.slice(14);
  assert.ok(first.every(cell => cell.top === 0 && cell.bottom === first[0].bottom));
  assert.ok(second.every(cell => cell.top === first[0].bottom && cell.bottom === shape.height));
  assert.equal(first[0].left, 0, "a line's first letter reaches the left edge");
  assert.equal(second[0].left, 0);
  assert.equal(second.at(-1).right, shape.width, "a line's last letter reaches the right edge");
  // Letters form in reading order, the name after the greeting.
  const formed = shape.lettersFormedMs;
  assert.ok(formed.every((time, index) => index === 0 || time > formed[index - 1]), `reading order: ${formed}`);

  // Each letter's flash is drawn inside its own cell.
  env.advanceTo(shape.completeMs + 20);
  assert.equal(env.log.clips, 24, "every letter has handed over in its cell");
});

test("a greeting that fits its room stays on one line", async () => {
  const env = page();
  const title = env.document.createElement("h1");
  env.document.body.append(title);
  const word = env.window.UAIntroWord.create(title, { lines: GREETING, maxWidth: 5000 });
  word.showStill();
  await settle();
  assert.equal(word.inspect().lines, 1);
  assert.deepEqual(env.log.texts, [GREETING.join(" ")]);
});

test("skipping shows the formed word at once and keeps the flashes going", async () => {
  const env = page();
  const title = env.document.createElement("h1");
  env.document.body.append(title);
  const word = env.window.UAIntroWord.create(title, { lines: GREETING, maxWidth: 800 });
  let done = 0;
  word.play(() => { done += 1; });
  // Skipped while the fonts are still loading: the word starts formed.
  word.skipForming();
  await settle();
  const shape = word.inspect();
  env.advanceTo(20);
  assert.equal(done, 1, "complete on the first frame");
  assert.equal(env.log.clips, shape.letters, "every letter shows in the word style");
  // Long after the first meshes have faded, new flashes are still drawn.
  for (let ms = 100; ms <= 6000; ms += 100) env.advanceTo(ms);
  const drawn = env.log.meshFrames.length;
  for (let ms = 6100; ms <= 8000; ms += 100) env.advanceTo(ms);
  assert.ok(env.log.meshFrames.length > drawn, "letters keep flashing");
  word.stop();

  // Skipped part way through forming.
  const later = page();
  const laterTitle = later.document.createElement("h1");
  later.document.body.append(laterTitle);
  const laterWord = later.window.UAIntroWord.create(laterTitle, { text: TITLE });
  let laterDone = 0;
  laterWord.play(() => { laterDone += 1; });
  await settle();
  later.advanceTo(600);
  assert.equal(laterDone, 0);
  laterWord.skipForming();
  later.advanceTo(620);
  assert.equal(laterDone, 1, "formed at once after the skip");
  laterWord.stop();
});


test("reflow changes line layout while preserving progress and one completion callback", async () => {
  const env = page();
  const title = env.document.createElement("h1");
  let room = 5000;
  const word = env.window.UAIntroWord.create(title, { lines: GREETING, maxWidth: () => room });
  let done = 0;
  word.play(() => done++);
  await settle();
  env.advanceTo(1000);
  room = 800;
  word.resize();
  assert.equal(word.inspect().lines, 2);
  env.advanceTo(word.inspect().completeMs + 20);
  assert.equal(done, 1);
  room = 5000;
  word.resize();
  assert.equal(word.inspect().lines, 1);
  env.advanceTo(word.inspect().completeMs + 100);
  assert.equal(done, 1);
  word.stop();
});

test("showStill cancels forming and flashes, including a play awaiting fonts", async () => {
  for (const started of [false, true]) {
    const env = page();
    const title = env.document.createElement("h1");
    const word = env.window.UAIntroWord.create(title, { text: TITLE });
    let done = 0;
    word.play(() => done++);
    if (started) { await settle(); env.advanceTo(1000); }
    word.showStill();
    await settle();
    const draws = env.log.draws;
    const meshes = env.log.meshFrames.length;
    env.advanceTo(12000);
    env.advanceTo(13000);
    assert.equal(env.log.draws, draws);
    assert.equal(env.log.meshFrames.length, meshes);
    assert.equal(done, 0);
    word.resize();
    assert.ok(env.log.draws > draws, "a static word can still resize");
    word.stop();
  }
});
