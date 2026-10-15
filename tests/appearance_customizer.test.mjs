// Customize Theme shows edits without saving them: Done saves, while Cancel,
// the close button and Escape put the opening appearance back. Runs the real
// appearance.js and appearance_customizer.js in a small DOM stand-in.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { byClass, byTag, fakeDocument, settle } from "./settings_fake_dom.mjs";

const ASSETS = new URL("../assistant_gateway/onboarding_web/assets/", import.meta.url);
const APPEARANCE = readFileSync(new URL("appearance.js", ASSETS), "utf8");
const CUSTOMIZER = readFileSync(new URL("appearance_customizer.js", ASSETS), "utf8");

function page() {
  const document = fakeDocument();
  const rootAttributes = new Map();
  const rootStyles = new Map();
  document.documentElement = {
    setAttribute: (name, value) => rootAttributes.set(name, value),
    getAttribute: name => rootAttributes.get(name) || null,
    style: { setProperty: (name, value) => rootStyles.set(name, value) },
    styles: rootStyles,
  };
  const window = {
    document,
    location: { pathname: "/" },
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
  };
  const context = { window, document, CustomEvent, Promise, JSON, Number, String, Math, Array, Object };
  vm.runInNewContext(APPEARANCE, context);
  vm.runInNewContext(CUSTOMIZER, context);
  return { window, document, appearance: window.UAAppearance, customizer: window.UAAppearanceCustomizer };
}

function open(env, overrides = {}) {
  const calls = { previews: [], saves: [], discards: [], closes: 0 };
  const opening = env.appearance.current();
  env.customizer.open({
    appearance: opening,
    preview: next => calls.previews.push(next),
    save: next => { calls.saves.push(next); },
    discard: original => calls.discards.push(original),
    onClose: () => { calls.closes += 1; },
    ...overrides,
  });
  const panel = byClass(env.document.body, "theme-customizer")[0];
  return { calls, opening, panel };
}

function slider(panel, label) {
  const control = byClass(panel, "appearance-control")
    .find(node => byTag(node, "strong")[0]?.textContent === label);
  assert.ok(control, `no control labeled ${label}`);
  return byTag(control, "input")[0];
}

function move(input, value) {
  input.value = String(value);
  input.dispatchEvent(new Event("input"));
}

function button(panel, label) {
  return byTag(panel, "button").find(node => node.textContent === label);
}

const isOpen = env => byClass(env.document.body, "theme-customizer").length === 1;

test("an edit is only shown, and Cancel puts the opening appearance back", () => {
  const env = page();
  const { calls, opening, panel } = open(env);
  move(slider(panel, "Light softness"), 80);
  move(slider(panel, "Light left or right"), -40);

  assert.equal(calls.previews.length, 2);
  assert.equal(calls.previews[1].dark.lightSoftness, 80);
  assert.equal(calls.previews[1].dark.lightX, -40);
  assert.equal(calls.saves.length, 0);

  button(panel, "Cancel").click();
  assert.equal(isOpen(env), false);
  assert.deepEqual(calls.discards, [opening]);
  assert.equal(calls.saves.length, 0);
  assert.equal(calls.closes, 1);
});

test("Done saves the edits once and closes without restoring", async () => {
  const env = page();
  const { calls, panel } = open(env);
  move(slider(panel, "Light up or down"), 35);
  button(panel, "Done").click();
  await settle();

  assert.equal(calls.saves.length, 1);
  assert.equal(calls.saves[0].dark.lightY, 35);
  assert.equal(calls.discards.length, 0);
  assert.equal(isOpen(env), false);
});

test("a failed save keeps the panel open with the error, and Escape then restores", async () => {
  const env = page();
  const { calls, panel } = open(env, {
    save: () => Promise.reject(new Error("Theme changes could not be saved. Try again.")),
  });
  move(slider(panel, "Light softness"), 10);
  const done = button(panel, "Done");
  done.click();
  assert.equal(done.textContent, "Saving...");
  assert.equal(done.disabled, true);
  await settle();

  assert.equal(isOpen(env), true);
  const status = byClass(panel, "theme-customizer-status")[0];
  assert.equal(status.hidden, false);
  assert.equal(status.getAttribute("role"), "alert");
  assert.match(status.textContent, /could not be saved/);
  assert.equal(done.disabled, false);
  assert.equal(done.textContent, "Done");

  env.document.dispatchEvent(Object.assign(new Event("keydown"), { key: "Escape" }));
  assert.equal(isOpen(env), false);
  assert.equal(calls.discards.length, 1);
});

test("the close button discards edits like Cancel", () => {
  const env = page();
  const { calls, panel } = open(env);
  move(slider(panel, "Light strength"), 90);
  byClass(panel, "settings-close-button")[0].click();
  assert.equal(isOpen(env), false);
  assert.equal(calls.discards.length, 1);
  assert.equal(calls.saves.length, 0);
});

test("a preview shows above the saved preference until it ends", () => {
  const { appearance } = page();
  appearance.setUserPreference({ mode: "dark", dark: { lightSoftness: 20 } });
  appearance.preview({ mode: "dark", dark: { lightSoftness: 90 } });
  assert.equal(appearance.current().dark.lightSoftness, 90);

  // A preference reload during the preview does not replace what is shown.
  appearance.setUserPreference({ mode: "dark", dark: { lightSoftness: 30 } });
  assert.equal(appearance.current().dark.lightSoftness, 90);

  appearance.endPreview();
  assert.equal(appearance.current().dark.lightSoftness, 30);
});

test("Share settings text round-trips and reads a config file excerpt", () => {
  const env = page();
  const { appearance } = env;
  const tuned = appearance.normalize({ mode: "dark", dark: { lightX: -35, lightWander: true, accents: { gold: { color: "#123456", share: 20 } } } });
  const text = appearance.settingsText(tuned);
  assert.match(text, /^# Universal Agents theme/);
  assert.match(text, /\n {2}light_x: -35\n/);
  // Accents come last, one line each, as in config.example.yaml.
  assert.match(text, /\n {2}accents:\n {4}gold: \{color: "#123456", share: 20\}\n {4}bronze: \{color: "#[0-9a-f]{6}", share: \d+\}\n$/);
  // Values come from the page's own script context, so compare them as JSON.
  const plain = value => JSON.parse(JSON.stringify(value));
  assert.deepEqual(plain(appearance.normalize(appearance.readSettingsText(text))), plain(tuned));

  const excerpt = [
    "ui:",
    "  onboarding:",
    "    appearance:",
    "      color_mode: dark   # the theme",
    "      dark:",
    "        surface_color: \"#2a6d22\"",
    "        light_softness: 70",
    "        accents:",
    "          gold: {color: \"#414016\", share: 12}",
  ].join("\n");
  const read = appearance.normalize(appearance.readSettingsText(excerpt));
  assert.equal(read.dark.surfaceColor, "#2a6d22");
  assert.equal(read.dark.lightSoftness, 70);
  assert.deepEqual(plain(read.dark.accents.gold), { color: "#414016", share: 12 });

  assert.throws(() => appearance.readSettingsText("hello: world"), /no theme settings/);
  assert.throws(() => appearance.readSettingsText("not yaml at all"), /could not be read/);
});

test("Apply previews pasted settings, and a bad paste shows an error", () => {
  const env = page();
  const { calls, panel } = open(env);
  const box = byClass(panel, "theme-customizer-share-text")[0];
  box.value = "dark:\n  light_softness: 90\n";
  button(panel, "Apply").click();
  assert.equal(calls.previews.at(-1).dark.lightSoftness, 90);
  assert.equal(slider(panel, "Light softness").value, "90");
  assert.equal(calls.saves.length, 0);

  const refreshed = byClass(panel, "theme-customizer-share-text")[0];
  refreshed.value = "nonsense";
  button(panel, "Apply").click();
  const status = byClass(panel, "theme-customizer-status")[0];
  assert.equal(status.hidden, false);
  assert.match(status.textContent, /could not be read/);
});

test("Copy selects the text when the clipboard is not available", () => {
  const env = page();
  const { panel } = open(env);
  const box = byClass(panel, "theme-customizer-share-text")[0];
  let selected = false;
  box.select = () => { selected = true; };
  button(panel, "Copy").click();
  assert.ok(selected);
  assert.match(byClass(panel, "theme-customizer-share-note")[0].textContent, /Ctrl\+C/);
});

test("a dark section pasted on its own is read, in saved or browser names", () => {
  const { appearance } = page();
  const plain = value => JSON.parse(JSON.stringify(value));
  const fromLab = appearance.readSettingsText(JSON.stringify({ surfaceColor: "#2a6d22", lightWander: true, lightWanderDistance: 133 }));
  const lab = plain(appearance.normalize(fromLab));
  assert.equal(lab.mode, "dark");
  assert.equal(lab.dark.surfaceColor, "#2a6d22");
  assert.equal(lab.dark.lightWanderDistance, 133);
  const fromYaml = plain(appearance.normalize(appearance.readSettingsText("light_x: -7\nlight_softness: 37\n")));
  assert.equal(fromYaml.dark.lightX, -7);
  assert.equal(fromYaml.dark.lightSoftness, 37);
});

test("Interface animation and the motion accent follow the appearance", () => {
  const { appearance, document } = page();
  assert.equal(document.documentElement.getAttribute("data-ui-motion"), "on");
  // The accent keeps the gold tint's hue but is light enough to see as a line.
  appearance.setUserPreference({ mode: "dark", dark: { accents: { gold: { color: "#464020", share: 10 } } } });
  const accent = document.documentElement.styles.get("--ua-motion-accent");
  const [r, g, b] = [1, 3, 5].map(offset => parseInt(accent.slice(offset, offset + 2), 16));
  assert.ok(r > 150 && g > 140 && b < g, `light olive-gold accent, got ${accent}`);
  appearance.setUserPreference({ mode: "dark", animation: false });
  assert.equal(document.documentElement.getAttribute("data-ui-motion"), "off");
  assert.equal(appearance.saved(appearance.current()).interface_animation, false);
  // Light mode takes the first (blue) light's hue, dark enough to read on light glass.
  appearance.setUserPreference({ mode: "light" });
  assert.equal(document.documentElement.styles.get("--ua-motion-accent"), "#082f73");
  appearance.setUserPreference({ mode: "light", light: { colors: ["#ffd60a", "#ffd60a", "#20f05a", "#ff3030"] } });
  const lightAccent = document.documentElement.styles.get("--ua-motion-accent");
  const hsl = hexHsl(lightAccent);
  assert.ok(Math.abs(hsl[0] - hexHsl("#ffd60a")[0]) < 2, `keeps the light's hue, got ${lightAccent}`);
  assert.ok(hsl[2] >= 0.21 && hsl[2] <= 0.35, `dark enough to read, got ${lightAccent}`);
});

function hexHsl(hex) {
  const [r, g, b] = [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16) / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const lightness = (max + min) / 2;
  const delta = max - min;
  if (!delta) return [0, 0, lightness];
  const saturation = delta / (1 - Math.abs(2 * lightness - 1));
  const hue = max === r ? ((g - b) / delta) % 6 : max === g ? (b - r) / delta + 2 : (r - g) / delta + 4;
  return [(hue * 60 + 360) % 360, saturation, lightness];
}

test("motion meshes take the person's theme colors, lifted to show on dark panes", () => {
  const { appearance } = page();
  appearance.setUserPreference({
    mode: "dark",
    dark: { metalColor: "#015b1a", accents: { gold: { color: "#464020", share: 10 }, bronze: { color: "#00751d", share: 39 } } },
  });
  const palette = JSON.parse(JSON.stringify(appearance.motionPalette()));
  assert.deepEqual(palette.map(entry => entry.weight), [51, 10, 39], "metal, then gold and bronze at their shares");
  const lightness = hex => {
    const [r, g, b] = [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16) / 255);
    return (Math.max(r, g, b) + Math.min(r, g, b)) / 2;
  };
  palette.forEach(entry => assert.ok(lightness(entry.color) >= 0.57, `${entry.color} is light enough`));
  const [metal] = palette;
  const [r, g, b] = [1, 3, 5].map(offset => parseInt(metal.color.slice(offset, offset + 2), 16));
  assert.ok(g > r && g > b, `the metal stays green, got ${metal.color}`);

  appearance.setUserPreference({ mode: "light" });
  assert.deepEqual(
    JSON.parse(JSON.stringify(appearance.motionPalette())).map(entry => entry.color),
    appearance.current().light.colors,
  );
});
