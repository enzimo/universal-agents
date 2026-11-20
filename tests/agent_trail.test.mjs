// Web Chat's agent trail (assistant_gateway/onboarding_web/assets/agent_trail.mjs): A4, A5, A6 colours.
import assert from "node:assert/strict";
import { test } from "node:test";

import { AGENT_TYPE_COLORS, TEAM_LEAD_COLOR, agentColor } from "../assistant_gateway/onboarding_web/assets/activity_mesh.mjs";
import {
  PERSON_COLOR,
  RESTING_CONTRAST,
  TRAIL_BACKGROUNDS,
  TRAIL_LIMIT,
  WORKING_CONTRAST,
  contrastRatio,
  relativeLuminance,
  renderTrail,
  trailAgentNames,
  trailColors,
  trailKey,
  trailView,
  workingLabel,
} from "../assistant_gateway/onboarding_web/assets/agent_trail.mjs";
import { fakeDocument } from "./fake_dom.mjs";

const P = { by: "person" };
const LEAD = { by: "agent", agent_type: "TeamArchitect", lead: true, name: "Team lead" };
const WEB = { by: "agent", agent_type: "WebResearchAgent", lead: false, name: "Web Research Agent" };
const MEM = { by: "agent", agent_type: "MemReviewAgent", lead: false, name: "Memory Review Agent" };
const working = (...agents) => agents.map(({ agent_type, lead, name }) => ({ agent_type, lead, name }));
const thread = (entries, { older = false, work = [], agents = [] } = {}) => ({
  trail: { entries, has_older: older, agents }, working: work,
});
const shape = view => view.triangles.map(t => (t.who === "person" ? "P" : t.who === "lead" ? "L" : t.agentType[0])
  + (t.working ? "*" : "") + (t.hollow ? "o" : ""));

test("no turns: nothing, unless the row is busy, then one pulsing team-lead triangle", () => {
  assert.deepEqual(shape(trailView(thread([]))), []);
  assert.deepEqual(shape(trailView({})), [], "a thread drawn before the server's row has no trail");
  const busy = trailView(thread([]), { busy: true });
  assert.deepEqual(shape(busy), ["L*"]);
  assert.equal(workingLabel(busy), "Working: team lead");
});

test("one turn and six turns draw every triangle, alternating up and down, none hollow", () => {
  assert.deepEqual(shape(trailView(thread([P]))), ["P"]);
  const six = trailView(thread([P, LEAD, WEB, P, LEAD, MEM]));
  assert.deepEqual(shape(six), ["P", "L", "W", "P", "L", "M"]);
  assert.deepEqual(six.triangles.map(t => t.down), [false, true, false, true, false, true]);
  assert.equal(six.triangles.length, TRAIL_LIMIT);
});

test("seven and twenty turns show six and the leftmost hollow and dotted", () => {
  for (const turns of [7, 20]) {
    const view = trailView(thread([LEAD, P, LEAD, WEB, P, LEAD], { older: turns > TRAIL_LIMIT }));
    assert.deepEqual(shape(view), ["Lo", "P", "L", "W", "P", "L"], `${turns} turns`);
    assert.equal(view.triangles.filter(t => t.hollow).length, 1);
  }
});

test("working agents of the current request pulse, older requests' triangles stay still", () => {
  const view = trailView(thread([P, LEAD, WEB, P, LEAD, WEB], { work: working(WEB) }), { busy: true });
  assert.deepEqual(shape(view), ["P", "L", "W", "P", "L", "W*"]);
  assert.equal(workingLabel(view), "Working: Web Research Agent");
  const both = trailView(thread([P, LEAD, WEB], { work: working(LEAD, WEB) }));
  assert.deepEqual(shape(both), ["P", "L*", "W*"]);
  assert.equal(workingLabel(both), "Working: team lead, Web Research Agent");
});

test("a window with no person entry is all the current request", () => {
  const many = ["A", "B", "C", "D", "E"].map(name => ({ by: "agent", agent_type: name + "Agent", lead: false, name }));
  const view = trailView(thread([LEAD, ...many], { older: true, work: working(many[0], many[4]) }), { busy: true });
  assert.deepEqual(shape(view), ["Lo", "A*", "B", "C", "D", "E*"]);
});

test("a busy row with nobody reported pulses the current request's team lead, else adds one", () => {
  const lead = trailView(thread([P, LEAD, WEB, P, LEAD]), { busy: true });
  assert.deepEqual(shape(lead), ["P", "L", "W", "P", "L*"]);
  // The lead's triangle of the current request is cut off: a pulsing one is added and six are kept.
  const many = ["A", "B", "C", "D", "E"].map(name => ({ by: "agent", agent_type: name + "Agent", lead: false, name }));
  const cut = trailView(thread(many.concat([MEM]), { older: true }), { busy: true });
  assert.deepEqual(shape(cut), ["Bo", "C", "D", "E", "M", "L*"]);
  assert.equal(workingLabel(cut), "Working: team lead");
  // Not busy and nobody works: nothing pulses.
  assert.deepEqual(shape(trailView(thread([P, LEAD]))), ["P", "L"]);
});

test("right after this page sends, the new turn shows and its team lead pulses, not the finished request's (C10)", () => {
  const finished = thread([P, LEAD, WEB]);
  const sent = trailView(finished, { busy: true, sentBeforeTrail: true });
  assert.deepEqual(shape(sent), ["P", "L", "W", "P", "L*"]);
  const full = trailView(thread([P, LEAD, P, LEAD, P, LEAD], { older: true }), { busy: true, sentBeforeTrail: true });
  assert.deepEqual(shape(full), ["Po", "L", "P", "L", "P", "L*"]);
  assert.notEqual(trailKey(finished), trailKey(thread([P, LEAD, WEB, P, LEAD])), "the server's new trail ends it");
});

test("colours: the person grey, the team lead gold, other types the activity mesh's colour", () => {
  const view = trailView(thread([P, LEAD, WEB, { by: "agent", agent_type: "Custom", lead: false, name: "Custom" }]));
  assert.deepEqual(view.triangles.map(t => t.color), [PERSON_COLOR, TEAM_LEAD_COLOR, agentColor("WebResearchAgent"),
    agentColor("Custom")]);
});

function hslHex(hue, saturation, lightness) {
  const chroma = (1 - Math.abs(2 * lightness - 1)) * saturation;
  const x = chroma * (1 - Math.abs((hue / 60) % 2 - 1));
  const m = lightness - chroma / 2;
  const rgb = [[chroma, x, 0], [x, chroma, 0], [0, chroma, x], [0, x, chroma], [x, 0, chroma], [chroma, 0, x]][Math.floor(hue / 60) % 6];
  return "#" + rgb.map(value => Math.round((value + m) * 255).toString(16).padStart(2, "0")).join("");
}

test("every hue the mesh can give and every fixed colour keeps 3:1 resting and 4.5:1 working in both themes (C04)", () => {
  const colors = [PERSON_COLOR, TEAM_LEAD_COLOR, ...Object.values(AGENT_TYPE_COLORS)];
  for (let hue = 0; hue < 360; hue += 1) colors.push(hslHex(hue, 0.7, 0.62));
  for (const color of colors) {
    const fitted = trailColors(color);
    for (const theme of ["dark", "light"]) {
      const background = TRAIL_BACKGROUNDS[theme];
      const resting = contrastRatio(relativeLuminance(fitted[theme].resting), background);
      const lit = contrastRatio(relativeLuminance(fitted[theme].working), background);
      assert.ok(resting >= RESTING_CONTRAST - 0.05 && resting >= 3, `${color} ${theme} resting ${resting}`);
      assert.ok(lit >= WORKING_CONTRAST, `${color} ${theme} working ${lit}`);
      assert.ok(lit - resting >= 1, `${color} ${theme}: working stands out from resting without motion`);
    }
  }
});

test("the drawn triangles carry both themes' colours, their direction, hollow and working marks", () => {
  const document = fakeDocument();
  document.createElementNS = (namespace, tagName) => document.createElement(tagName);
  const view = trailView(thread([LEAD, P, LEAD, WEB, P, LEAD], { older: true, work: working(LEAD) }));
  const holder = renderTrail(document, view);
  assert.equal(holder.getAttribute("aria-hidden"), "true");
  assert.equal(holder.children.length, 6);
  const classes = holder.children.map(svg => svg.getAttribute("class"));
  assert.deepEqual(classes, ["trail-tri is-hollow", "trail-tri is-down", "trail-tri", "trail-tri is-down",
    "trail-tri", "trail-tri is-down is-working"]);
  assert.deepEqual(holder.children.map(svg => svg.getAttribute("data-who")), ["lead", "person", "lead", "agent", "person", "lead"]);
  const lit = trailColors(TEAM_LEAD_COLOR);
  assert.equal(holder.children[5].getAttribute("style"), `--tri-light: ${lit.light.working}; --tri-dark: ${lit.dark.working}`);
  assert.equal(holder.children[0].getAttribute("style"), `--tri-light: ${lit.light.resting}; --tri-dark: ${lit.dark.resting}`);
});

test("the hover card names every agent type that worked in the thread", () => {
  const agents = [{ agent_type: "TeamArchitect", lead: true, name: "Team lead" }, { agent_type: "WebResearchAgent", name: "Web Research Agent" }];
  assert.equal(trailAgentNames(thread([P], { agents })), "Team lead, Web Research Agent");
  assert.equal(trailAgentNames(thread([])), "");
});
