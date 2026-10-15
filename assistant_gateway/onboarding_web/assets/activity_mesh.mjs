/**
 * Web Chat's activity mesh: a small triangle mesh in the sidebar, above the
 * account row, where each triangle can stand for an agent at work. With
 * nothing running it is calm and grey; when the team lead works, the middle
 * triangle glows gold, and each busy worker lights a triangle of its own in
 * its type's color. It shows the whole team's work, as the Settings Team tab
 * does (owner's decision, 2026-10-04).
 *
 * Busy state comes from the team's live status (team_activity.mjs), which
 * the gateway adds to every live page (about every two seconds). Agents
 * broadcast it when work starts and ends
 * (UniversalAgent._announce_execution_state). Settings → Team and the Tasks
 * view read the same copy, so the page asks the gateway once for all three.
 */

const FADE_MS = 700;
const FRAME_INTERVAL_MS = 50;
const BUSY_STATUS = "busy";
const TEAM_LEAD_COLOR = "#f6c84c";
// The calm mesh's color where glass_mesh.js is not loaded.
const IDLE_RGB = [150, 165, 175];

// Built-in agent types have fixed colors; other types get a stable color
// from their name.
export const AGENT_TYPE_COLORS = Object.freeze({
  TeamArchitect: TEAM_LEAD_COLOR,
  WebResearchAgent: "#62d5f3",
  MemReviewAgent: "#5ee08a",
  InternalImprovementAgent: "#b18cff",
});

function hash(text) {
  let value = 2166136261;
  for (const character of String(text)) {
    value ^= character.charCodeAt(0);
    value = Math.imul(value, 16777619);
  }
  return value >>> 0;
}

function hslHex(hue, saturation, lightness) {
  const chroma = (1 - Math.abs(2 * lightness - 1)) * saturation;
  const x = chroma * (1 - Math.abs((hue / 60) % 2 - 1));
  const m = lightness - chroma / 2;
  const sector = Math.floor(hue / 60) % 6;
  const rgb = [[chroma, x, 0], [x, chroma, 0], [0, chroma, x], [0, x, chroma], [x, 0, chroma], [chroma, 0, x]][sector];
  return "#" + rgb.map(value => Math.round((value + m) * 255).toString(16).padStart(2, "0")).join("");
}

export function agentColor(namedAgentType) {
  return AGENT_TYPE_COLORS[namedAgentType] || hslHex(hash(namedAgentType) % 360, 0.7, 0.62);
}

function hexRgb(hex) {
  return [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16));
}

/**
 * Picks a triangle for each busy agent. The team lead takes the middle;
 * a worker keeps the triangle its id hashes to (the next free one when that
 * is taken), so lights do not jump around as other agents start and stop.
 */
export function assignSlots(busy, order, held = new Map()) {
  const slots = new Map();
  const taken = new Set();
  const lead = busy.find(member => member.is_team_lead);
  if (lead && order.length) {
    slots.set(lead.agent_id, order[0]);
    taken.add(order[0]);
  }
  const outer = order.slice(1);
  busy.filter(member => member !== lead).forEach(member => {
    const kept = held.get(member.agent_id);
    if (kept !== undefined && outer.includes(kept) && !taken.has(kept)) {
      slots.set(member.agent_id, kept);
      taken.add(kept);
      return;
    }
    if (!outer.length) return;
    const start = hash(member.agent_id) % outer.length;
    for (let step = 0; step < outer.length; step += 1) {
      const slot = outer[(start + step) % outer.length];
      if (!taken.has(slot)) {
        slots.set(member.agent_id, slot);
        taken.add(slot);
        return;
      }
    }
  });
  return slots;
}

export function createActivityMesh({ browser, lifecycle, host, teamActivity }) {
  const document = browser.document;
  const canvas = document.createElement("canvas");
  canvas.className = "chat-activity-canvas";
  canvas.setAttribute("role", "img");
  host.append(canvas);
  const tooltip = document.createElement("div");
  tooltip.className = "thread-title-tooltip chat-activity-tooltip";
  tooltip.setAttribute("role", "tooltip");
  tooltip.hidden = true;
  document.body.append(tooltip);

  let glyph = null;
  let order = [];
  let shades = [];
  // agent id -> { slot, color, label, on, changedAt }
  const lights = new Map();
  let running = false;
  let unsubscribe = null;
  let frameId = 0;
  let lastFrame = 0;
  // The calm mesh takes the theme's glass edge (glass_mesh.js), so it suits
  // the person's colors (owner's request, 2026-10-09; it was a fixed grey).
  // Busy agents keep their type's color, which says who is working.
  let idleRgb = themeIdleRgb();

  function now() { return browser.performance.now(); }

  function themeIdleRgb() {
    return browser.UAGlass ? browser.UAGlass.colors().edge : IDLE_RGB;
  }

  function ensureGlyph() {
    if (glyph) return glyph;
    glyph = browser.UAMeshGlyph.create(canvas, { columns: 9, rows: 2, seed: 7121, jitter: 0.3, drift: 2.5 });
    order = glyph.orderFromCenter();
    shades = order.map((_, index) => ((index * 7919) % 13) / 13);
    return glyph;
  }

  function intensity(light, time) {
    const progress = Math.min(1, (time - light.changedAt) / FADE_MS);
    return light.on ? progress : 1 - progress;
  }

  function draw(time) {
    const mesh = ensureGlyph();
    const animated = browser.UAMotion.enabled();
    const bySlot = new Map();
    lights.forEach(light => bySlot.set(light.slot, light));
    const pulse = animated ? 0.85 + 0.15 * Math.sin(time / 420) : 1;
    mesh.draw(animated ? time : 0, animated ? 1 : 0, index => {
      const light = bySlot.get(index);
      const level = light ? intensity(light, animated ? time : Infinity) : 0;
      if (level > 0.001) {
        return {
          fill: browser.UAMotion.rgba(light.rgb, (0.35 + 0.5 * level) * pulse),
          glow: browser.UAMotion.rgba(light.rgb, 0.55 * level),
        };
      }
      return { fill: browser.UAMotion.rgba(idleRgb, 0.05 + shades[index] * 0.08) };
    }, browser.UAMotion.rgba(idleRgb, 0.16));
  }

  function settled(time) {
    for (const light of lights.values()) {
      if (time - light.changedAt < FADE_MS) return false;
    }
    return true;
  }

  function frame(time) {
    frameId = 0;
    if (!running) return;
    if (time - lastFrame >= FRAME_INTERVAL_MS) {
      lastFrame = time;
      draw(time);
      for (const [id, light] of lights) {
        if (!light.on && time - light.changedAt >= FADE_MS) lights.delete(id);
      }
    }
    if (browser.UAMotion.enabled() || !settled(time)) frameId = lifecycle.frame(frame);
  }

  function schedule() {
    if (running && !frameId && !document.hidden) frameId = lifecycle.frame(frame);
  }

  function describe() {
    const working = [...lights.values()].filter(light => light.on).map(light => light.label);
    canvas.setAttribute("aria-label", working.length
      ? "Team activity: " + working.join(", ") + " working."
      : "Team activity: nobody is working.");
  }

  function apply(members) {
    const busy = members.filter(member => member.status && member.status.key === BUSY_STATUS);
    ensureGlyph();
    const held = new Map([...lights].map(([id, light]) => [id, light.slot]));
    const slots = assignSlots(busy, order, held);
    const time = now();
    busy.forEach(member => {
      const existing = lights.get(member.agent_id);
      const color = member.is_team_lead ? TEAM_LEAD_COLOR : agentColor(member.named_agent_type);
      // Name the agent by its class (e.g. WebResearchAgent): `name` falls back to
      // the agent id, which means nothing to people (owner's request 2026-10-09).
      const label = member.named_agent_type;
      if (existing && existing.on) {
        existing.slot = slots.get(member.agent_id);
        return;
      }
      lights.set(member.agent_id, {
        slot: slots.get(member.agent_id),
        rgb: hexRgb(color),
        label,
        on: true,
        changedAt: time,
      });
    });
    const busyIds = new Set(busy.map(member => member.agent_id));
    lights.forEach((light, id) => {
      if (light.on && !busyIds.has(id)) {
        light.on = false;
        light.changedAt = time;
      }
    });
    describe();
    schedule();
  }

  /** A team update: only a change in who is busy redraws (last-seen times are not shown here). */
  function receive(members, { changed }) {
    if (running && changed) apply(members || []);
  }

  function showTooltip(event) {
    if (!glyph) return;
    const rect = canvas.getBoundingClientRect();
    const index = glyph.hit(event.clientX - rect.left, event.clientY - rect.top,
      browser.UAMotion.enabled() ? now() : 0, browser.UAMotion.enabled() ? 1 : 0);
    const light = [...lights.values()].find(entry => entry.slot === index && entry.on);
    if (!light) {
      tooltip.hidden = true;
      return;
    }
    tooltip.textContent = light.label + " is working";
    tooltip.hidden = false;
    tooltip.style.left = Math.min(event.clientX + 12, browser.innerWidth - tooltip.offsetWidth - 12) + "px";
    tooltip.style.top = Math.max(12, event.clientY - tooltip.offsetHeight - 10) + "px";
  }

  lifecycle.listen(canvas, "pointermove", showTooltip);
  lifecycle.listen(canvas, "pointerleave", () => { tooltip.hidden = true; });
  lifecycle.listen(document, "visibilitychange", () => {
    if (!document.hidden) schedule();
  });
  lifecycle.listen(browser, "resize", () => {
    if (!glyph) return;
    glyph.layout();
    draw(now());
  });
  browser.UAMotion.onChange(() => {
    idleRgb = themeIdleRgb();
    if (!running) return;
    draw(now());
    schedule();
  });

  return {
    start() {
      if (running) return;
      running = true;
      host.hidden = false;
      describe();
      draw(now());
      unsubscribe = teamActivity.subscribe(receive);
      schedule();
    },
    stop() {
      running = false;
      if (unsubscribe) unsubscribe();
      unsubscribe = null;
      if (frameId) lifecycle.cancelFrame(frameId);
      frameId = 0;
      lights.clear();
      tooltip.hidden = true;
      host.hidden = true;
    },
  };
}
