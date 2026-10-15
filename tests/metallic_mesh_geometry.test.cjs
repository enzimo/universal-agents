"use strict";

// Runs the dark background renderer against a minimal browser stand-in and
// checks the mesh it builds. Without WebGL the renderer draws the basic
// background, which uses the same mesh, so these checks cover both drawings.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const RENDERER = fs.readFileSync(
  path.join(__dirname, "..", "assistant_gateway", "onboarding_web", "assets", "metallic_mesh.js"),
  "utf8"
);

// Limits from ui/DESIGN.md ("Faces are broad plates ... no face may be
// disproportionately large"). Motion is allowed a little more slack because
// points move between the checks that add and remove points.
const STILL_LIMITS = { narrowestDeg: 25, neighborRatio: 2.5, overMedian: 6 };
const MOVING_LIMITS = { narrowestDeg: 20, neighborRatio: 3, overMedian: 6.5 };

function fake2d() {
  const gradient = { addColorStop() {} };
  return {
    setTransform() {}, fillRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
    closePath() {}, fill() {}, stroke() {},
    createLinearGradient() { return gradient; },
  };
}

function fakeCanvas() {
  return {
    style: {},
    width: 0,
    height: 0,
    parentNode: { removeChild() {} },
    setAttribute() {},
    addEventListener() {},
    getContext(type) { return type === "2d" ? fake2d() : null; },
  };
}

function loadRenderer({ width, height, dark }) {
  const warnings = [];
  const host = fakeCanvas();
  host.classList = { contains: () => false };
  host.insertAdjacentElement = () => {};
  const listeners = {};
  const window = {
    innerWidth: width,
    innerHeight: height,
    devicePixelRatio: 1,
    performance: { now: () => 0 },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    addEventListener() {},
    requestAnimationFrame: () => 1,
    cancelAnimationFrame() {},
    getComputedStyle: () => ({ position: "fixed", top: "0px", right: "0px", bottom: "0px", left: "0px", zIndex: "0" }),
    UAAppearance: { current: () => ({ mode: "dark", dark: dark || {} }) },
  };
  const document = {
    documentElement: { getAttribute: () => "dark", hasAttribute: () => false },
    querySelector: () => host,
    getElementById: () => null,
    createElement: () => fakeCanvas(),
    addEventListener(type, handler) { listeners[type] = handler; },
  };
  window.document = document;
  const context = {
    window,
    document,
    console: { warn: (...args) => warnings.push(args.join(" ")), log() {}, error() {} },
    Math, Number, JSON, Map, Set, Object, Array, String, Float32Array, Promise,
  };
  vm.runInNewContext(RENDERER, context, { filename: "metallic_mesh.js" });
  const api = window.UAMetallicMesh;
  api.lab.freeze(true);
  return { api, warnings, listeners };
}

function angle(p, q, r) {
  const ux = q.x - p.x;
  const uy = q.y - p.y;
  const vx = r.x - p.x;
  const vy = r.y - p.y;
  const cos = (ux * vx + uy * vy) / Math.hypot(ux, uy) / Math.hypot(vx, vy);
  return Math.acos(Math.max(-1, Math.min(1, cos))) * 180 / Math.PI;
}

function faceStats(world, width, height) {
  const P = world.points;
  const area = (t) => Math.abs((P[t[1]].x - P[t[0]].x) * (P[t[2]].y - P[t[0]].y)
    - (P[t[1]].y - P[t[0]].y) * (P[t[2]].x - P[t[0]].x)) / 2;
  const owners = new Map();
  world.tris.forEach((t, i) => {
    for (let c = 0; c < 3; c += 1) {
      const a = t[(c + 1) % 3];
      const b = t[(c + 2) % 3];
      const key = a < b ? `${a},${b}` : `${b},${a}`;
      if (!owners.has(key)) owners.set(key, []);
      owners.get(key).push(i);
    }
  });
  const areas = world.tris.map(area);
  const visible = [];
  world.tris.forEach((t, i) => {
    const cx = (P[t[0]].x + P[t[1]].x + P[t[2]].x) / 3;
    const cy = (P[t[0]].y + P[t[1]].y + P[t[2]].y) / 3;
    if (cx > 0 && cy > 0 && cx < width && cy < height) visible.push(i);
  });
  let neighborRatio = 0;
  let narrowestDeg = 180;
  visible.forEach((i) => {
    const t = world.tris[i];
    const near = [];
    for (let c = 0; c < 3; c += 1) {
      const a = t[(c + 1) % 3];
      const b = t[(c + 2) % 3];
      owners.get(a < b ? `${a},${b}` : `${b},${a}`).forEach((j) => { if (j !== i) near.push(areas[j]); });
    }
    near.sort((x, y) => x - y);
    neighborRatio = Math.max(neighborRatio, areas[i] / near[Math.floor(near.length / 2)]);
    const [a, b, c] = t.map((k) => P[k]);
    narrowestDeg = Math.min(narrowestDeg, angle(a, b, c), angle(b, c, a), angle(c, a, b));
  });
  const sorted = visible.map((i) => areas[i]).sort((x, y) => x - y);
  return {
    visible: visible.length,
    narrowestDeg,
    neighborRatio,
    overMedian: sorted[sorted.length - 1] / sorted[Math.floor(sorted.length / 2)],
  };
}

function assertWithin(stats, limits, label) {
  assert.ok(stats.visible > 8, `${label}: too few faces (${stats.visible})`);
  assert.ok(stats.narrowestDeg >= limits.narrowestDeg, `${label}: narrowest corner ${stats.narrowestDeg.toFixed(1)}°`);
  assert.ok(stats.neighborRatio <= limits.neighborRatio, `${label}: face ${stats.neighborRatio.toFixed(2)}x its neighbors`);
  assert.ok(stats.overMedian <= limits.overMedian, `${label}: largest face ${stats.overMedian.toFixed(2)}x the median`);
}

const VIEWPORTS = [[1536, 960], [1280, 800], [2560, 1440], [390, 844]];
// MESH_SEEDS=<count> checks that many layouts per window size instead.
const SEEDS = process.env.MESH_SEEDS
  ? Array.from({ length: Number(process.env.MESH_SEEDS) }, (_, index) => 1000 + index * 7919)
  : [9187, 7932, 110879, 71284, 118798, 4242];

test("every layout is made of broad, evenly sized faces", () => {
  // MESH_STATS=1 prints the worst values per window size, for tuning.
  const summary = [];
  for (const [width, height] of VIEWPORTS) {
    const worst = { narrowestDeg: 180, neighborRatio: 0, overMedian: 0, faces: [], medians: [] };
    for (const seed of SEEDS) {
      const { api } = loadRenderer({ width, height, dark: { seed } });
      const stats = faceStats(api.lab.world(), width, height);
      worst.narrowestDeg = Math.min(worst.narrowestDeg, stats.narrowestDeg);
      worst.neighborRatio = Math.max(worst.neighborRatio, stats.neighborRatio);
      worst.overMedian = Math.max(worst.overMedian, stats.overMedian);
      worst.faces.push(stats.visible);
      worst.medians.push(stats.overMedian);
      if (!process.env.MESH_STATS) assertWithin(stats, STILL_LIMITS, `${width}x${height} seed ${seed}`);
    }
    const medians = worst.medians.sort((x, y) => x - y);
    summary.push(`${width}x${height}: narrowest ${worst.narrowestDeg.toFixed(1)}°, `
      + `neighbors ${worst.neighborRatio.toFixed(2)}x, largest/median typical ${medians[Math.floor(medians.length / 2)].toFixed(2)}x `
      + `worst ${worst.overMedian.toFixed(2)}x, faces ${Math.min(...worst.faces)}-${Math.max(...worst.faces)}`);
  }
  if (process.env.MESH_STATS) console.log(summary.join("\n"));
});

test("faces stay broad and evenly sized while the mesh moves", () => {
  for (const seed of [110879, 4242]) {
    const { api } = loadRenderer({ width: 1536, height: 960, dark: { seed, meshSpeed: 200 } });
    for (let step = 0; step < 6; step += 1) {
      api.lab.advance(20);
      assertWithin(faceStats(api.lab.world(), 1536, 960), MOVING_LIMITS, `seed ${seed} after ${(step + 1) * 20}s`);
    }
    assert.ok(api.lab.stats().flips > 0, "the mesh should reconnect while it moves");
  }
});

test("mesh speed and reflection speed drive separate clocks", () => {
  const meshOnly = loadRenderer({ width: 1280, height: 800, dark: { lightMotion: false } }).api;
  const before = meshOnly.lab.world().points.map((p) => [p.x, p.y]);
  meshOnly.lab.advance(5);
  assert.equal(meshOnly.lab.world().shimmer, 0);
  assert.ok(meshOnly.lab.world().time > 4.9);
  assert.ok(meshOnly.lab.world().points.some((p, i) => p.x !== before[i][0] || p.y !== before[i][1]));

  const lightOnly = loadRenderer({ width: 1280, height: 800, dark: { meshMotion: false } }).api;
  const still = lightOnly.lab.world().points.map((p) => [p.x, p.y]);
  lightOnly.lab.advance(5);
  assert.equal(lightOnly.lab.world().time, 0);
  assert.ok(lightOnly.lab.world().shimmer > 4.9);
  assert.ok(lightOnly.lab.world().points.every((p, i) => p.x === still[i][0] && p.y === still[i][1]));
});

test("without WebGL2 the basic background is drawn and a warning names the reason", () => {
  const { api, warnings } = loadRenderer({ width: 1280, height: 800 });
  assert.equal(api.lab.mode(), "basic");
  assert.ok(warnings.some((message) => message.includes("WebGL2 is unavailable")));
});

test("settings are filled from the defaults and clamped to their ranges", () => {
  const { api } = loadRenderer({ width: 1280, height: 800 });
  const normalized = api.normalize({
    facetSize: 9999, lightX: -999, lightY: 999, lightSoftness: 150, detail: "ultra", accents: { gold: { share: 90 } },
  });
  const defaults = api.defaults();
  assert.equal(normalized.facetSize, 520);
  assert.equal(normalized.lightX, -300);
  assert.equal(normalized.lightY, 300);
  assert.equal(normalized.lightSoftness, 100);
  assert.equal(normalized.detail, "full");
  assert.equal(normalized.accents.gold.share, 50);
  assert.equal(normalized.accents.bronze.share, defaults.accents.bronze.share);
  assert.equal(normalized.surfaceColor, defaults.surfaceColor);
});

test("the light sits where its left/right and up/down settings place it", () => {
  const width = 1200;
  const height = 800;
  const { api } = loadRenderer({ width, height, dark: { lightX: 100, lightY: -50 } });
  const { light } = api.lab.light();
  assert.ok(Math.abs(light.center[0] - width) < 1, `x ${light.center[0]}`);
  assert.ok(Math.abs(light.center[1] - height * 0.25) < 1, `y ${light.center[1]}`);
  api.update(Object.assign(api.lab.settings(), { lightX: 0, lightY: 0 }));
  const centered = api.lab.light().light;
  assert.ok(Math.hypot(centered.center[0] - width / 2, centered.center[1] - height / 2) < 1);
  // Over the center the strip lies level instead of turning with a direction.
  assert.ok(Math.abs(centered.axisU[1]) < 1e-9);
});

test("a softer light is larger, dimmer and blurrier", () => {
  const { api } = loadRenderer({ width: 1280, height: 800, dark: { lightSoftness: 25 } });
  const sharp = api.lab.light().light;
  api.update(Object.assign(api.lab.settings(), { lightSoftness: 75 }));
  const soft = api.lab.light().light;
  assert.ok(Math.abs(soft.size[0] / sharp.size[0] - 4) < 1e-9, "50 more softness is four times the size");
  assert.ok(Math.abs(sharp.color[0] / soft.color[0] - 16) < 1e-9, "brightness falls with the area");
  assert.equal(sharp.blur, 0);
  assert.ok(soft.blur > 0);
});

test("a wandering light roams from its resting place and back again", () => {
  const width = 1200;
  const height = 800;
  const { api } = loadRenderer({ width, height, dark: { lightX: 0, lightY: 0, lightWander: true, lightWanderDistance: 100 } });
  api.lab.freeze(false);
  const start = api.lab.light().light.center;
  // The path starts at the resting place (the small sway and drift are 3%).
  assert.ok(Math.hypot(start[0] - width / 2, start[1] - height / 2) < 80, `start ${start}`);
  let farthest = 0;
  for (let step = 0; step < 12; step += 1) {
    api.lab.advance(10);
    const center = api.lab.light().light.center;
    farthest = Math.max(farthest, Math.hypot(center[0] - width / 2, center[1] - height / 2));
    assert.ok(Math.abs(center[0] - width / 2) <= width, "stays within the wander distance");
  }
  assert.ok(farthest > width * 0.15, `the light should roam; it reached ${farthest.toFixed(0)} px`);

  api.update(Object.assign(api.lab.settings(), { lightWander: false }));
  const resting = api.lab.light().light.center;
  assert.ok(Math.hypot(resting[0] - width / 2, resting[1] - height / 2) < 80, "without wandering it stays put");
});
