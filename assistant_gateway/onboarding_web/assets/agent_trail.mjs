// Web Chat's agent trail: a short row of triangles on each thread row's second
// line, one per turn, oldest first: the person's turns in grey, the team lead
// in its gold and every other agent type in the activity mesh's colour for it
// (activity_mesh.mjs). The triangles of the agents working on the thread's
// request now pulse; that replaces the busy dot (owner's request, 2026-11-19).
//
// The gateway sends the trail with the thread list (`trail`: at most six
// entries plus `has_older`) and who works now with the list and every live
// read (`working`; web_chat_threads.py). This module turns them into
// triangles; chat.js draws them before the snippet and the hover card in its
// footer (chat_thread_preview.mjs).
//
// Rules (owner's rule, 2026-11-20, and plan reviews C04, C10):
// - At most six triangles, counting the ones working now. When older turns
//   exist the leftmost is drawn hollow with a dotted outline.
// - Working triangles are the agent entries of the thread's current request
//   (after the last person entry in the row, or the whole row when it holds
//   none: every request starts with one) whose type works now.
// - A row that is busy (this page's own reply wait or the server's busy mark),
//   or that the server says someone works on, always pulses something: the
//   team lead's triangle of the current request, else a team-lead triangle
//   added at the end. The words keep the server's names of who works. Right after this page sends,
//   while the trail has not changed since, the new person's turn and a pulsing
//   team-lead triangle are added (`sentBeforeTrail`), so the pulse never sits
//   on the request that already finished.
// - Colours keep at least 3:1 against every row background in both themes
//   (`trailColors`): resting triangles are moved to about 3.2:1, a quieter
//   look, and working ones to at least 4.5:1 with a glow. Lightness is moved
//   within the colour's own hue, so a person-made specialist of any hue gets
//   there too (the mesh gives such types a hue from their name).

import { agentColor, TEAM_LEAD_COLOR } from "./activity_mesh.mjs";

export const TRAIL_LIMIT = 6;
export const BY_PERSON = "person";
export const BY_AGENT = "agent";
export const TEAM_LEAD_NAME = "Team lead";
// The person's triangles: a neutral grey, moved into the band like the others.
export const PERSON_COLOR = "#96a5af";
// Each theme's worst row background, as relative luminance (plan review C17).
// A triangle is lighter than a dark row, so its contrast is lowest on the
// brightest dark background: the raised row (#173440) and the Needs you box
// over the dark glass measure about 0.03 to 0.04. It is darker than a light
// row, so its contrast is lowest on the darkest light background: the pale
// backing over the mesh measures about 0.70 to 0.74 behind the trail. Each
// value keeps a margin past what was measured, and the browser probe fails
// when a measured background falls outside it (tests/web_chat_agent_trail_probe.cjs).
export const TRAIL_BACKGROUNDS = Object.freeze({ dark: 0.05, light: 0.6 });
export const RESTING_CONTRAST = 3.2;
export const WORKING_CONTRAST = 4.5;
const SVG_NS = "http://www.w3.org/2000/svg";
const UP_POINTS = "7.5,0.75 14.25,11.25 0.75,11.25";
const DOWN_POINTS = "0.75,0.75 14.25,0.75 7.5,11.25";

function channel(value) {
  const c = value / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

export function relativeLuminance(hex) {
  const [r, g, b] = [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16));
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

export function contrastRatio(first, second) {
  const [high, low] = first > second ? [first, second] : [second, first];
  return (high + 0.05) / (low + 0.05);
}

function hexHsl(hex) {
  const [r, g, b] = [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16) / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const lightness = (max + min) / 2;
  if (max === min) return [0, 0, lightness];
  const delta = max - min;
  const saturation = delta / (1 - Math.abs(2 * lightness - 1));
  const hue = max === r ? ((g - b) / delta) % 6 : max === g ? (b - r) / delta + 2 : (r - g) / delta + 4;
  return [(hue * 60 + 360) % 360, saturation, lightness];
}

function hslHex(hue, saturation, lightness) {
  const chroma = (1 - Math.abs(2 * lightness - 1)) * saturation;
  const x = chroma * (1 - Math.abs((hue / 60) % 2 - 1));
  const m = lightness - chroma / 2;
  const sector = Math.floor(hue / 60) % 6;
  const rgb = [[chroma, x, 0], [x, chroma, 0], [0, chroma, x], [0, x, chroma], [x, 0, chroma], [chroma, 0, x]][sector];
  return "#" + rgb.map(value => Math.round(Math.min(1, Math.max(0, value + m)) * 255).toString(16).padStart(2, "0")).join("");
}

// The lightness, in the colour's own hue and saturation, where its contrast
// with the theme's worst background reaches `target`. Luminance grows with
// lightness, so the contrast is enough above it in dark mode and below it in
// light mode.
function lightnessFor(hue, saturation, theme, target) {
  const background = TRAIL_BACKGROUNDS[theme];
  const enough = lightness => contrastRatio(relativeLuminance(hslHex(hue, saturation, lightness)), background) >= target;
  let [low, high] = [0, 1];
  for (let step = 0; step < 24; step += 1) {
    const middle = (low + high) / 2;
    const towardBackground = theme === "dark" ? !enough(middle) : enough(middle);
    if (towardBackground) low = middle;
    else high = middle;
  }
  // The side that has the contrast, so rounding never lands just short of it.
  return theme === "dark" ? high : low;
}

function fitted(hex, theme, target, { rest }) {
  const [hue, saturation, lightness] = hexHsl(hex);
  const edge = lightnessFor(hue, saturation, theme, target);
  if (rest) return hslHex(hue, saturation, edge);
  const keeps = theme === "dark" ? lightness >= edge : lightness <= edge;
  return keeps ? hex : hslHex(hue, saturation, edge);
}

// A triangle's colours in both themes: resting (about 3.2:1) and working (at
// least 4.5:1, its own colour when that already reaches it).
export function trailColors(hex) {
  return {
    dark: { resting: fitted(hex, "dark", RESTING_CONTRAST, { rest: true }), working: fitted(hex, "dark", WORKING_CONTRAST, { rest: false }) },
    light: { resting: fitted(hex, "light", RESTING_CONTRAST, { rest: true }), working: fitted(hex, "light", WORKING_CONTRAST, { rest: false }) },
  };
}

export function entryColor(entry) {
  if (entry.by === BY_PERSON) return PERSON_COLOR;
  return entry.lead ? TEAM_LEAD_COLOR : agentColor(entry.agent_type);
}

const PERSON_ENTRY = Object.freeze({ by: BY_PERSON });
const LEAD_ENTRY = Object.freeze({ by: BY_AGENT, agent_type: "", lead: true, name: TEAM_LEAD_NAME, provisional: true });

function entriesOf(thread) {
  const trail = thread && thread.trail;
  const entries = trail && Array.isArray(trail.entries) ? trail.entries : [];
  return {
    entries: entries.filter(entry => entry && (entry.by === BY_PERSON || entry.by === BY_AGENT)).map(entry => ({ ...entry })),
    older: Boolean(trail && trail.has_older),
  };
}

function capped(entries, older) {
  return entries.length > TRAIL_LIMIT ? { entries: entries.slice(-TRAIL_LIMIT), older: true } : { entries, older };
}

// The string chat.js keeps at send time to tell whether the trail changed since (`sentBeforeTrail`).
export function trailKey(thread) {
  return JSON.stringify((thread && thread.trail) || null);
}

// The row's triangles (module comment). `busy`: the row would have shown the
// busy dot. `sentBeforeTrail`: this page sent a message and the trail has not
// changed since.
export function trailView(thread, { busy = false, sentBeforeTrail = false } = {}) {
  let { entries, older } = entriesOf(thread);
  if (sentBeforeTrail) entries.push({ ...PERSON_ENTRY }, { ...LEAD_ENTRY });
  ({ entries, older } = capped(entries, older));
  const working = Array.isArray(thread && thread.working) ? thread.working : [];
  const types = new Set(working.filter(agent => !agent.lead).map(agent => agent.agent_type));
  const leadWorks = working.some(agent => agent.lead);
  const lastPerson = entries.map(entry => entry.by).lastIndexOf(BY_PERSON);
  const current = lastPerson + 1;
  let names = working.map(agent => String(agent.name || agent.agent_type || ""));
  entries.forEach((entry, index) => {
    if (index < current || entry.by !== BY_AGENT || entry.provisional) return;
    entry.working = entry.lead ? leadWorks : types.has(entry.agent_type);
  });
  if (sentBeforeTrail) entries[entries.length - 1].working = true;
  // A busy row, or one the server says someone works on whose triangle is not
  // in the row, always pulses something (plan review C19).
  if ((busy || working.length > 0) && !entries.some(entry => entry.working)) {
    const lead = entries.findLastIndex((entry, index) => index >= current && entry.by === BY_AGENT && entry.lead);
    if (lead >= 0) {
      entries[lead].working = true;
    } else {
      entries.push({ ...LEAD_ENTRY, working: true });
      ({ entries, older } = capped(entries, older));
    }
  }
  if (!names.length && entries.some(entry => entry.working)) {
    names = [...new Set(entries.filter(entry => entry.working).map(entry => String(entry.name || TEAM_LEAD_NAME)))];
  }
  return {
    triangles: entries.map((entry, index) => ({
      who: entry.by === BY_PERSON ? "person" : entry.lead ? "lead" : "agent",
      agentType: entry.by === BY_AGENT ? String(entry.agent_type || "") : "",
      name: entry.by === BY_AGENT ? String(entry.name || "") : "",
      color: entryColor(entry),
      down: index % 2 === 1,
      hollow: older && index === 0,
      working: Boolean(entry.working),
    })),
    older,
    workingNames: names,
  };
}

// "Working: team lead, Web Research Agent" for the row's name, or "".
export function workingLabel(view) {
  if (!view.workingNames.length) return "";
  return "Working: " + view.workingNames.map(name => (name === TEAM_LEAD_NAME ? "team lead" : name)).join(", ");
}

// "Team lead, Web Research Agent": every agent type that worked in the thread (the hover card's footer).
export function trailAgentNames(thread) {
  const agents = thread && thread.trail && Array.isArray(thread.trail.agents) ? thread.trail.agents : [];
  return agents.map(agent => String(agent.name || agent.agent_type || "")).filter(Boolean).join(", ");
}

// The triangles as small inline SVGs in one hidden-from-readers span: the row
// says who works in words (workingLabel), so the shapes are decoration.
export function renderTrail(document, view) {
  const holder = document.createElement("span");
  holder.className = "thread-trail";
  holder.setAttribute("aria-hidden", "true");
  view.triangles.forEach(triangle => {
    const colors = trailColors(triangle.color);
    const tone = triangle.working ? "working" : "resting";
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "trail-tri" + (triangle.down ? " is-down" : "") + (triangle.working ? " is-working" : "")
      + (triangle.hollow ? " is-hollow" : ""));
    svg.setAttribute("viewBox", "0 0 15 12");
    svg.setAttribute("data-who", triangle.who);
    svg.setAttribute("style", "--tri-light: " + colors.light[tone] + "; --tri-dark: " + colors.dark[tone]);
    const polygon = document.createElementNS(SVG_NS, "polygon");
    polygon.setAttribute("points", triangle.down ? DOWN_POINTS : UP_POINTS);
    svg.appendChild(polygon);
    holder.appendChild(svg);
  });
  return holder;
}
