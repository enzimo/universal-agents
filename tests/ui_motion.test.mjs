// Interface motion (ui_motion.js) and Web Chat's activity mesh
// (activity_mesh.mjs), run against a small DOM stand-in.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { createPageLifecycle } from "../assistant_gateway/onboarding_web/assets/browser_lifecycle.mjs";
import { createTeamActivity } from "../assistant_gateway/onboarding_web/assets/team_activity.mjs";
import {
  AGENT_TYPE_COLORS, agentColor, assignSlots, createActivityMesh,
} from "../assistant_gateway/onboarding_web/assets/activity_mesh.mjs";
import { fakeDocument } from "./settings_fake_dom.mjs";

const ASSETS = new URL("../assistant_gateway/onboarding_web/assets/", import.meta.url);
const UI_MOTION = readFileSync(new URL("ui_motion.js", ASSETS), "utf8");

function fakeCanvasContext() {
  const calls = [];
  const record = name => (...args) => calls.push([name, ...args]);
  return {
    calls,
    setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, closePath() {},
    fill: record("fill"), stroke: record("stroke"),
    set fillStyle(value) { calls.push(["fillStyle", value]); },
    set strokeStyle(value) {}, set lineWidth(value) {}, set shadowColor(value) {}, set shadowBlur(value) {},
  };
}

/** A browser stand-in with ui_motion.js loaded; `motion` sets data-ui-motion. */
function page({ motion = "on", reduced = false } = {}) {
  const document = fakeDocument();
  const rootAttributes = new Map([["data-ui-motion", motion], ["data-color-mode", "dark"]]);
  const listeners = new Map();
  document.documentElement = {
    getAttribute: name => rootAttributes.get(name) || null,
    setAttribute: (name, value) => rootAttributes.set(name, value),
    classList: { add() {}, remove() {} },
  };
  document.hidden = false;
  document.querySelector = selector => document.body.querySelector(selector);
  const create = document.createElement.bind(document);
  document.createElement = tagName => {
    const element = create(tagName);
    const styles = new Map();
    element.style = { setProperty: (name, value) => styles.set(name, value), getPropertyValue: name => styles.get(name) || "" };
    if (tagName === "canvas") {
      element.context = fakeCanvasContext();
      element.getContext = () => element.context;
      element.getBoundingClientRect = () => ({ left: 0, top: 0, width: 240, height: 52 });
    }
    return element;
  };
  const media = new EventTarget();
  media.matches = reduced;
  const frames = [];
  let clock = 1000;
  const window = {
    document,
    innerWidth: 1280,
    devicePixelRatio: 1,
    console: { warn() {} },
    performance: { now: () => clock },
    matchMedia: () => media,
    getComputedStyle: () => ({ getPropertyValue: name => (name === "--ua-motion-accent" ? "#b4a65e" : "") }),
    requestAnimationFrame: callback => { frames.push(callback); return frames.length; },
    cancelAnimationFrame() {},
    setTimeout: (callback, delay) => setTimeout(callback, Math.min(delay, 5)),
    clearTimeout,
    setInterval: () => 0,
    clearInterval() {},
    AbortController,
    addEventListener(type, handler) { listeners.set(type, handler); },
    removeEventListener(type, handler) { if (listeners.get(type) === handler) listeners.delete(type); },
  };
  window.UAMetallicMesh = { reveals: [], reveal(duration) { this.reveals.push(duration); } };
  window.UAAppearance = { motionPalette: () => [{ color: "#3cd468", weight: 60 }, { color: "#c9bb57", weight: 40 }] };
  vm.runInNewContext(UI_MOTION, { window, document, Date, Math, Number, Map, Object, String });
  return {
    window, document, frames,
    resize() { listeners.get("resize")?.(); },
    reduce() { media.matches = true; media.dispatchEvent(new Event("change")); },
    advance(ms) { clock += ms; },
    runFrames() { frames.splice(0).forEach(callback => callback(clock)); },
  };
}

test("a just-sent message arrives once, and a re-render continues the effect", () => {
  const env = page();
  const bubble = env.document.createElement("article");
  env.document.body.append(bubble);
  env.window.UAMotion.arrive(bubble, "sent:req-1", "sent", new Date().toISOString());
  assert.ok(bubble.classList.contains("ua-arrive-sent"));
  assert.equal(bubble.style.getPropertyValue("--ua-arrive-offset"), "0ms");

  // The transcript re-renders 500 ms later: the new bubble continues 500 ms in.
  env.advance(500);
  const again = env.document.createElement("article");
  const offsets = [];
  again.style = { setProperty: (name, value) => offsets.push([name, value]) };
  env.window.UAMotion.arrive(again, "sent:req-1", "sent", new Date().toISOString());
  assert.deepEqual(offsets.filter(([name]) => name === "--ua-arrive-offset"), [["--ua-arrive-offset", "-500ms"]]);

  // After the effect has ended, re-renders show the message plainly.
  env.advance(5000);
  const later = env.document.createElement("article");
  env.window.UAMotion.arrive(later, "sent:req-1", "sent", new Date().toISOString());
  assert.equal(later.classList.contains("ua-arrive-sent"), false);
});

test("a re-render during the sent mesh's fade keeps the drawn mesh, so it never blinks out", () => {
  const env = page();
  const list = env.document.createElement("div");
  env.document.body.append(list);
  const bubble = env.document.createElement("article");
  list.append(bubble);
  env.window.UAMotion.arrive(bubble, "sent:req-2", "sent", new Date().toISOString());
  const mesh = bubble.firstChild;
  assert.equal(mesh.className, "ua-arrive-mesh");
  env.runFrames();
  assert.ok(mesh.context.calls.some(([name]) => name === "fill"), "the first frame draws as soon as the bubble is measured");

  // Partway through the fade the transcript is rebuilt (as when the server
  // confirms the message): the new bubble gets the mesh already drawn, with
  // no empty frame before its next draw.
  env.advance(900);
  env.runFrames();
  list.replaceChildren();
  const again = env.document.createElement("article");
  list.append(again);
  env.window.UAMotion.arrive(again, "sent:req-2", "sent", new Date().toISOString());
  assert.equal(again.firstChild, mesh, "the same drawn canvas moves to the new bubble");
  assert.equal(again.children.filter(child => child.className === "ua-arrive-mesh").length, 1);
  const drawsBefore = mesh.context.calls.length;
  env.advance(16);
  env.runFrames();
  assert.ok(mesh.context.calls.length > drawsBefore, "the mesh keeps fading in the new bubble");

  // When the effect ends, the mesh goes and a later re-render adds none.
  env.advance(2000);
  env.runFrames();
  assert.equal(mesh.isConnected, false);
  const later = env.document.createElement("article");
  list.replaceChildren(later);
  env.window.UAMotion.arrive(later, "sent:req-2", "sent", new Date().toISOString());
  assert.equal(later.firstChild, null);
});

test("a reply's scan line lasts as long as its arrival", () => {
  const env = page();
  const bubble = env.document.createElement("article");
  env.window.UAMotion.arrive(bubble, "reply:new-1", "reply", new Date().toISOString());
  assert.equal(bubble.style.getPropertyValue("--ua-arrive-duration"), "1170ms");
});

test("loading history never plays arrival effects", () => {
  const env = page();
  const bubble = env.document.createElement("article");
  env.window.UAMotion.arrive(bubble, "reply:old", "reply", "2026-01-01T00:00:00Z");
  assert.equal(bubble.classList.contains("ua-arrive-reply"), false);
});

test("Interface Animation off and reduced motion both stop arrivals", () => {
  for (const options of [{ motion: "off" }, { reduced: true }]) {
    const env = page(options);
    assert.equal(env.window.UAMotion.enabled(), false);
    const bubble = env.document.createElement("article");
    env.window.UAMotion.arrive(bubble, "reply:new", "reply", new Date().toISOString());
    assert.equal(bubble.classList.contains("ua-arrive-reply"), false);
  }
});

test("the team lead takes the middle and workers keep their triangles", () => {
  const order = [5, 4, 6, 3, 7, 2, 8];
  const lead = { agent_id: "lead", is_team_lead: true };
  const research = { agent_id: "research-1", is_team_lead: false };
  const memory = { agent_id: "memory-1", is_team_lead: false };
  const first = assignSlots([lead, research], order);
  assert.equal(first.get("lead"), 5);
  const held = new Map(first);
  const second = assignSlots([memory, lead, research], order, held);
  assert.equal(second.get("research-1"), first.get("research-1"), "a working agent keeps its triangle");
  assert.notEqual(second.get("memory-1"), second.get("research-1"));
  assert.equal(new Set(second.values()).size, 3);
});

test("built-in agent types have fixed colors and others a stable one", () => {
  assert.equal(agentColor("TeamArchitect"), AGENT_TYPE_COLORS.TeamArchitect);
  assert.equal(agentColor("WebResearchAgent"), "#62d5f3");
  assert.match(agentColor("CodeWritingAgent"), /^#[0-9a-f]{6}$/);
  assert.equal(agentColor("CodeWritingAgent"), agentColor("CodeWritingAgent"));
});

test("the activity mesh shows the shared team status and describes who is working", async () => {
  const env = page();
  const host = env.document.createElement("div");
  host.hidden = true;
  env.document.body.append(host);
  const members = [
    { agent_id: "lead", name: "TeamArchitect-ab12", named_agent_type: "TeamArchitect", is_team_lead: true, status: { key: "busy" } },
    { agent_id: "research-1", name: "WebResearchAgent-cd34", named_agent_type: "WebResearchAgent", is_team_lead: false, status: { key: "busy" } },
    { agent_id: "memory-1", name: "MemReviewAgent-ef56", named_agent_type: "MemReviewAgent", is_team_lead: false, status: { key: "available" } },
  ];
  const teamActivity = createTeamActivity();
  teamActivity.publish({ members });
  const lifecycle = createPageLifecycle(env.window);
  const mesh = createActivityMesh({ browser: env.window, lifecycle, host, teamActivity });
  mesh.start();
  assert.equal(host.hidden, false);
  const canvas = host.children[0];
  assert.equal(canvas.getAttribute("aria-label"), "Team activity: TeamArchitect, WebResearchAgent working.");

  // Once faded in, the lit triangles use the team lead's gold and the research blue.
  env.advance(1000);
  env.runFrames();
  const fills = canvas.context.calls.filter(call => call[0] === "fillStyle").map(call => call[1]);
  assert.ok(fills.some(fill => fill.startsWith("rgba(246, 200, 76,")), "gold team lead");
  assert.ok(fills.some(fill => fill.startsWith("rgba(98, 213, 243,")), "blue web research");

  teamActivity.publish({ members: members.map(member => ({ ...member, status: { key: "available" } })) });
  assert.equal(canvas.getAttribute("aria-label"), "Team activity: nobody is working.");

  mesh.stop();
  assert.equal(host.hidden, true);
  teamActivity.publish({ members });
  assert.equal(canvas.getAttribute("aria-label"), "Team activity: nobody is working.", "a stopped mesh ignores updates");
  lifecycle.dispose();
});

test("the calm activity mesh takes the theme's glass edge instead of a fixed grey", async () => {
  const env = page();
  env.window.UAGlass = { colors: () => ({ edge: [200, 40, 40] }) };
  const host = env.document.createElement("div");
  env.document.body.append(host);
  const teamActivity = createTeamActivity();
  const lifecycle = createPageLifecycle(env.window);
  const mesh = createActivityMesh({ browser: env.window, lifecycle, host, teamActivity });
  mesh.start();
  const canvas = host.children[0];
  const fills = canvas.context.calls.filter(call => call[0] === "fillStyle").map(call => call[1]);
  assert.ok(fills.length > 0 && fills.every(fill => fill.startsWith("rgba(200, 40, 40,")), `theme-colored idle mesh: ${fills[0]}`);
  mesh.stop();
  lifecycle.dispose();
});

test("the shared team status reaches every view once, says when only last-seen times moved, and survives a broken view", () => {
  const teamActivity = createTeamActivity();
  const seen = [];
  const warn = console.warn;
  console.warn = () => {};
  try {
    teamActivity.subscribe(() => { throw new Error("broken view"); });
    const stop = teamActivity.subscribe((members, { changed }) => seen.push([members.length, changed]));
    assert.deepEqual(seen, [], "nothing to show before the first update");
    assert.equal(teamActivity.current(), null);
    const lead = { agent_id: "lead", status: { key: "busy" }, last_seen_at: "2026-10-04T12:00:00Z" };
    teamActivity.publish({ members: [lead] });
    teamActivity.publish({ members: [{ ...lead, last_seen_at: "2026-10-04T12:00:02Z" }] });
    teamActivity.publish(null);
    assert.deepEqual(seen, [[1, true], [1, false]]);
    const late = [];
    teamActivity.subscribe(members => late.push(members.length));
    assert.deepEqual(late, [1], "a view that opens later gets the current status at once");
    stop();
    teamActivity.publish({ members: [] });
    assert.equal(seen.length, 2, "an unsubscribed view gets nothing");
  } finally {
    console.warn = warn;
  }
});

test("the onboarding intro fills the background, then a click brings the window in", async () => {
  const env = page();
  const shell = env.document.createElement("main");
  const next = env.document.createElement("button");
  env.document.body.append(shell, next);
  let finished = 0;
  env.window.UAMotion.playIntro({ shell, focusTarget: next, onFinish: () => { finished += 1; } });
  const overlay = env.document.body.querySelector(".ua-intro");
  assert.ok(overlay, "the intro shows");
  assert.equal(overlay.querySelector("h1").textContent, "Universal Agents");
  assert.equal(overlay.querySelector("button").textContent, "Click to Begin");
  assert.ok(shell.classList.contains("is-intro-hidden"));
  assert.equal(shell.getAttribute("inert"), "");
  // The title is outlined by an SVG filter that traces only the letters'
  // outer shape (-webkit-text-stroke marked the inside of the "e").
  const filter = env.document.body.querySelector("filter");
  assert.equal(filter && filter.getAttribute("id"), "ua-intro-outline", "the outline filter exists");
  const primitives = (node) => node.children.flatMap(child => [child.tagName.toLowerCase(), ...primitives(child)]);
  assert.deepEqual(primitives(filter).filter(name => name.startsWith("fe")), [
    "fecomponenttransfer", "fefunca", "femorphology", "fecomposite", "feflood", "fecomposite", "femerge", "femergenode", "femergenode",
  ]);
  assert.deepEqual(env.window.UAMetallicMesh.reveals, [1800], "the dark background fills in");
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.ok(overlay.classList.contains("show-title") && overlay.classList.contains("show-begin"));

  overlay.click();
  overlay.click();
  assert.equal(finished, 1, "the page hears once that the intro was dismissed");
  assert.equal(shell.classList.contains("is-intro-hidden"), false);
  assert.equal(shell.hasAttribute("inert"), false);
  assert.ok(next.focused > 0, "focus moves to onboarding");
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(env.document.body.querySelector(".ua-intro"), null, "the intro is removed");
});

test("without motion the intro shows at once and Escape dismisses it", () => {
  const env = page({ reduced: true });
  const shell = env.document.createElement("main");
  env.document.body.append(shell);
  env.window.UAMotion.playIntro({ shell });
  const overlay = env.document.body.querySelector(".ua-intro");
  assert.ok(overlay.classList.contains("show-title") && overlay.classList.contains("show-begin"));
  assert.deepEqual(env.window.UAMetallicMesh.reveals, [], "no fill-in animation");
  env.document.dispatchEvent(Object.assign(new Event("keydown"), { key: "Escape" }));
  assert.equal(shell.classList.contains("is-intro-hidden"), false);
});

test("a sent message's mesh is drawn in the person's theme colors", () => {
  const env = page();
  const bubble = env.document.createElement("article");
  env.document.body.append(bubble);
  env.window.UAMotion.arrive(bubble, "sent:req-colors", "sent", new Date().toISOString());
  const canvas = bubble.children[0];
  assert.equal(canvas.className, "ua-arrive-mesh");
  env.runFrames(); // lays the mesh out
  env.advance(100);
  env.runFrames(); // draws a frame
  const fills = canvas.context.calls.filter(call => call[0] === "fillStyle").map(call => call[1]);
  assert.ok(fills.length > 0);
  assert.ok(fills.every(fill => fill.startsWith("rgba(60, 212, 104,") || fill.startsWith("rgba(201, 187, 87,")),
    `only theme colors, got ${[...new Set(fills.map(fill => fill.slice(0, 16)))]}`);
  assert.ok(fills.some(fill => fill.startsWith("rgba(201, 187, 87,")), "the gold share shows");
});

// Web Chat's sign-in greeting (UAMotion.playGreeting).
function greetingPage(options) {
  const env = page(options);
  const stage = env.document.createElement("div");
  const heading = env.document.createElement("h1");
  const shell = env.document.createElement("div");
  const field = env.document.createElement("input");
  stage.append(heading);
  shell.append(field);
  env.document.body.append(stage, shell);
  const words = [];
  env.window.UAIntroWord = {
    create(title, wordOptions) {
      const word = { title, options: wordOptions, plays: 0, skips: 0, stills: 0, stopped: false };
      Object.assign(word, {
        play(done) { word.plays += 1; word.done = done; },
        skipForming() { word.skips += 1; },
        showStill() { word.stills += 1; },
        stop() { word.stopped = true; },
        resize() { word.resizes = (word.resizes || 0) + 1; },
      });
      words.push(word);
      return word;
    },
  };
  const keydown = key => {
    const event = new Event("keydown", { cancelable: true });
    event.key = key;
    env.document.dispatchEvent(event);
    return event;
  };
  return { ...env, stage, heading, shell, field, words, keydown };
}

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

test("the sign-in greeting fills the background, forms, then brings the window in", async () => {
  const env = greetingPage();
  const maxWidth = () => 640;
  env.window.UAMotion.playGreeting({
    stage: env.stage, heading: env.heading, shell: env.shell, focusTarget: env.field,
    lines: ["Good Morning,", "Alice!"], maxWidth,
  });
  assert.deepEqual(env.window.UAMetallicMesh.reveals, [1800], "the dark background fills in");
  assert.ok(env.shell.classList.contains("is-intro-hidden"), "the window waits");
  assert.equal(env.shell.getAttribute("inert"), "");
  assert.equal(env.document.body.querySelector("filter").getAttribute("id"), "ua-intro-outline");
  const [word] = env.words;
  assert.deepEqual(word.options.lines, ["Good Morning,", "Alice!"]);
  assert.equal(word.options.maxWidth, maxWidth);
  await wait(20);
  assert.equal(word.plays, 1);
  assert.ok(env.stage.classList.contains("show-title"));
  assert.ok(env.shell.classList.contains("is-intro-hidden"), "still forming");
  word.done();
  await wait(20);
  assert.equal(env.shell.classList.contains("is-intro-hidden"), false, "the window fades in once formed");
  assert.equal(env.shell.hasAttribute("inert"), false);
  assert.equal(env.field.focused, 1, "the token field gets focus");
  assert.equal(env.keydown("a").defaultPrevented, false, "keys go on to the field");
});

test("a key or click skips the forming; Enter, Space and Escape never reach the form", async () => {
  for (const key of ["Enter", " ", "Escape"]) {
    const env = greetingPage();
    env.window.UAMotion.playGreeting({
      stage: env.stage, heading: env.heading, shell: env.shell, focusTarget: env.field, lines: ["Good Evening!"],
    });
    assert.equal(env.keydown(key).defaultPrevented, true, `${JSON.stringify(key)} only skips`);
    const [word] = env.words;
    assert.equal(word.plays, 1, "the word starts at once");
    assert.equal(word.skips, 1, "already formed");
    assert.equal(env.shell.classList.contains("is-intro-hidden"), false);
    assert.equal(env.field.focused, 1);
    assert.equal(env.keydown("Enter").defaultPrevented, false, "once shown, Enter submits as usual");
  }
  const typed = greetingPage();
  typed.window.UAMotion.playGreeting({
    stage: typed.stage, heading: typed.heading, shell: typed.shell, focusTarget: typed.field, lines: ["Good Evening!"],
  });
  assert.equal(typed.keydown("x").defaultPrevented, false, "a typed letter reaches the now-focused field");
  assert.equal(typed.field.focused, 1);

  const clicked = greetingPage();
  clicked.window.UAMotion.playGreeting({
    stage: clicked.stage, heading: clicked.heading, shell: clicked.shell, focusTarget: clicked.field, lines: ["Good Evening!"],
  });
  clicked.document.dispatchEvent(new Event("click"));
  assert.equal(clicked.shell.classList.contains("is-intro-hidden"), false);
  await wait(20);
  assert.equal(clicked.words[0].plays, 1, "the skipped word's timer does not start it again");
});

test("without motion the formed greeting and the window show at once", () => {
  const env = greetingPage({ reduced: true });
  env.window.UAMotion.playGreeting({
    stage: env.stage, heading: env.heading, shell: env.shell, focusTarget: env.field, lines: ["Good Morning!"],
  });
  assert.deepEqual(env.window.UAMetallicMesh.reveals, [], "no fill-in");
  assert.equal(env.words[0].stills, 1);
  assert.ok(env.stage.classList.contains("show-title"));
  assert.equal(env.shell.classList.contains("is-intro-hidden"), false);
  assert.equal(env.field.focused, 1);
});

test("a greeting replayed with the window open leaves it open, and stop ends it", async () => {
  const env = greetingPage();
  const greeting = env.window.UAMotion.playGreeting({
    stage: env.stage, heading: env.heading, shell: env.shell, focusTarget: env.field,
    lines: ["Good Morning!"], shellShown: true,
  });
  assert.equal(env.shell.classList.contains("is-intro-hidden"), false);
  assert.equal(env.shell.hasAttribute("inert"), false);
  assert.equal(env.keydown("Enter").defaultPrevented, false, "keys are not taken over");
  greeting.stop();
  assert.equal(env.words[0].stopped, true);
  await wait(20);
  assert.equal(env.words[0].plays, 0, "a stopped greeting never starts forming");

  const hidden = greetingPage();
  hidden.window.UAMotion.playGreeting({
    stage: hidden.stage, heading: hidden.heading, shell: hidden.shell, focusTarget: hidden.field, lines: ["Good Morning!"],
  }).stop();
  assert.equal(hidden.keydown("Enter").defaultPrevented, false, "stop removes the key listener");
});


test("greeting resize preserves the open form, and stop removes entrance and listeners", () => {
  const env = greetingPage();
  const greeting = env.window.UAMotion.playGreeting({
    stage: env.stage, heading: env.heading, shell: env.shell, focusTarget: env.field, lines: ["Good Morning!"],
  });
  env.keydown("Escape");
  assert.ok(env.shell.classList.contains("ua-intro-enter"));
  env.resize();
  assert.equal(env.words[0].resizes, 1);
  assert.equal(env.words[0].plays, 1);
  assert.equal(env.shell.hasAttribute("inert"), false);
  greeting.stop();
  assert.equal(env.shell.classList.contains("ua-intro-enter"), false);
  env.resize();
  env.reduce();
  assert.equal(env.words[0].resizes, 1);
  assert.equal(env.words[0].stills, 0, "a stopped greeting has no preference listener");
});

test("turning motion off during forming or after reveal shows a still word immediately", async () => {
  for (const revealed of [false, true]) {
    const env = greetingPage();
    const greeting = env.window.UAMotion.playGreeting({
      stage: env.stage, heading: env.heading, shell: env.shell, focusTarget: env.field, lines: ["Good Morning!"],
    });
    if (revealed) env.keydown("Escape");
    env.reduce();
    assert.equal(env.words[0].stills, 1);
    assert.equal(env.shell.hasAttribute("inert"), false);
    assert.equal(env.shell.classList.contains("is-intro-hidden"), false);
    assert.equal(env.shell.classList.contains("ua-intro-enter"), false);
    await wait(20);
    assert.equal(env.words[0].plays, revealed ? 1 : 0, "the delayed start is cancelled");
    greeting.stop();
  }
});
