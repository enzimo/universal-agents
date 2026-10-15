// Motion lab controls. The popups here are opened and closed the way the
// product's pages do it, so panel_motion.js's watcher plays the same glass:
// the setup popup is added and removed (as onboarding's guides and Web
// Chat's Share and Send are), the large popup is shown and hidden with
// `hidden` (as Web Chat's Settings is), and the native dialog uses showModal
// and close (as the workflow page's dialogs do).
(function () {
  "use strict";

  var PRESETS = {
    default: { metal: "#015b1a", surface: "#007007", gold: "#464020", blue: "#082f73" },
    teal: { metal: "#1c4f63", surface: "#0d3a4a", gold: "#8a6a2a", blue: "#075985" },
    amber: { metal: "#6b3d0e", surface: "#4a2a08", gold: "#a07020", blue: "#8a4b00" },
    violet: { metal: "#3b2466", surface: "#2a1850", gold: "#6a4a9a", blue: "#4b2a8a" },
  };
  var LIGHT_REST = ["#ffd60a", "#20f05a", "#ff3030"];
  var theme = { mode: "dark", animation: true, metal: PRESETS.default.metal, surface: PRESETS.default.surface,
    gold: PRESETS.default.gold, blue: PRESETS.default.blue };

  function byId(id) { return document.getElementById(id); }

  function applyTheme() {
    window.UAAppearance.setUserPreference({
      mode: theme.mode,
      animation: theme.animation,
      dark: { metalColor: theme.metal, surfaceColor: theme.surface, accents: { gold: { color: theme.gold, share: 10 } } },
      light: { colors: [theme.blue].concat(LIGHT_REST) },
    });
    byId("metal").value = theme.metal;
    byId("gold").value = theme.gold;
    byId("blue-light").value = theme.blue;
    byId("animation").checked = theme.animation;
    showSwatches();
  }

  // The colors the effects read right now, so a theme change can be checked.
  function showSwatches() {
    var root = window.getComputedStyle(document.documentElement);
    var rows = [
      ["--ua-glass-base", "glass"],
      ["--ua-glass-edge", "glass edge"],
      ["--ua-motion-accent", "accent (brackets)"],
      ["--ua-glass-start", "word fill"],
    ];
    var host = byId("swatches");
    host.replaceChildren();
    rows.forEach(function (row) {
      var value = root.getPropertyValue(row[0]).trim();
      var swatch = document.createElement("span");
      swatch.className = "lab-swatch";
      swatch.style.background = value;
      var label = document.createElement("code");
      label.textContent = row[1] + " " + value;
      host.append(swatch, label);
    });
  }

  function press(group, button) {
    group.querySelectorAll("button").forEach(function (item) {
      item.setAttribute("aria-pressed", String(item === button));
    });
  }

  // ---------- Popups ----------

  function openSetupPopup() {
    var existing = document.querySelector(".setup-overlay.lab-setup");
    if (existing) existing.remove();
    var overlay = document.createElement("div");
    overlay.className = "setup-overlay lab-setup";
    var dialog = document.createElement("section");
    dialog.className = "setup-dialog lab-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    var title = document.createElement("h2");
    title.textContent = "Connect Discord";
    var copy = document.createElement("p");
    copy.textContent = "Use Discord when you want the assistant reachable in DMs, server channels, or threads through a bot account.";
    var steps = document.createElement("ol");
    ["Create an application in the Discord developer portal.", "Add a bot and copy its token.",
      "Paste the token below and choose who may talk to the assistant."].forEach(function (text) {
      var item = document.createElement("li");
      item.textContent = text;
      steps.append(item);
    });
    var actions = document.createElement("div");
    actions.className = "lab-dialog-actions";
    var close = document.createElement("button");
    close.type = "button";
    close.textContent = "Close";
    close.addEventListener("click", function () { overlay.remove(); });
    actions.append(close);
    dialog.append(title, copy, steps, actions);
    overlay.append(dialog);
    overlay.addEventListener("click", function (event) { if (event.target === overlay) overlay.remove(); });
    document.body.append(overlay);
    window.requestAnimationFrame(function () { overlay.classList.add("is-open"); });
    close.focus();
  }

  var large = byId("large-overlay");
  function openLarge() { large.hidden = false; byId("close-large").focus(); }
  function closeLarge() { large.hidden = true; }

  function playIntro() {
    window.UAMotion.playIntro({ shell: document.querySelector(".lab-shell") });
  }

  // ---------- Wiring ----------

  byId("open-setup").addEventListener("click", openSetupPopup);
  byId("open-large").addEventListener("click", openLarge);
  byId("close-large").addEventListener("click", closeLarge);
  large.addEventListener("click", function (event) { if (event.target === large) closeLarge(); });
  byId("open-native").addEventListener("click", function () { byId("native-dialog").showModal(); });
  byId("play-intro").addEventListener("click", playIntro);
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    if (!large.hidden) closeLarge();
    var setup = document.querySelector(".setup-overlay.lab-setup");
    if (setup) setup.remove();
  });

  byId("speeds").addEventListener("click", function (event) {
    var button = event.target.closest("button[data-speed]");
    if (!button) return;
    window.UALabClock.setSpeed(Number(button.dataset.speed));
    press(byId("speeds"), button);
  });
  byId("modes").addEventListener("click", function (event) {
    var button = event.target.closest("button[data-mode]");
    if (!button) return;
    theme.mode = button.dataset.mode;
    press(byId("modes"), button);
    applyTheme();
  });
  byId("presets").addEventListener("click", function (event) {
    var button = event.target.closest("button[data-preset]");
    if (!button) return;
    var preset = PRESETS[button.dataset.preset];
    theme.metal = preset.metal;
    theme.surface = preset.surface;
    theme.gold = preset.gold;
    theme.blue = preset.blue;
    applyTheme();
  });
  byId("metal").addEventListener("input", function (event) { theme.metal = event.target.value; applyTheme(); });
  byId("gold").addEventListener("input", function (event) { theme.gold = event.target.value; applyTheme(); });
  byId("blue-light").addEventListener("input", function (event) { theme.blue = event.target.value; applyTheme(); });
  byId("animation").addEventListener("change", function (event) { theme.animation = event.target.checked; applyTheme(); });

  applyTheme();
}());
