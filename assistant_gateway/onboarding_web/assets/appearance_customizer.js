(function () {
  "use strict";

  var activeCustomizer = null;

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function controlHeader(labelText, valueText) {
    var header = document.createElement("span");
    header.className = "appearance-control-header";
    var label = document.createElement("strong");
    label.textContent = labelText;
    // An <output> is labelable and would become the outer label's control before
    // the range, color, or checkbox input that follows it.
    var value = document.createElement("span");
    value.className = "appearance-control-value";
    value.textContent = valueText;
    header.appendChild(label);
    header.appendChild(value);
    return { element: header, value: value };
  }

  function rangeControl(options) {
    var control = document.createElement("label");
    control.className = "appearance-control";
    var header = controlHeader(options.label, options.format(options.value));
    var input = document.createElement("input");
    input.type = "range";
    input.className = "single-range";
    input.min = String(options.min);
    input.max = String(options.max);
    input.step = String(options.step || 1);
    input.value = String(options.value);
    input.disabled = Boolean(options.disabled);
    input.addEventListener("input", function () {
      var value = Number(input.value);
      options.update(value);
      header.value.textContent = options.format(value);
      options.changed();
    });
    control.appendChild(header.element);
    control.appendChild(input);
    return control;
  }

  function colorControl(options) {
    var control = document.createElement("label");
    control.className = "appearance-control theme-customizer-color";
    var header = controlHeader(options.label, options.value.toLowerCase());
    var input = document.createElement("input");
    input.type = "color";
    input.className = "light-input line-color-input";
    input.value = options.value;
    input.setAttribute("aria-label", options.label + " color");
    input.addEventListener("input", function () {
      var value = String(input.value || options.value).toLowerCase();
      options.update(value);
      header.value.textContent = value;
      options.changed();
    });
    control.appendChild(header.element);
    control.appendChild(input);
    return control;
  }

  function toggleControl(options) {
    var control = document.createElement("label");
    control.className = "appearance-control toggle-control";
    var header = controlHeader(
      options.label,
      options.value ? options.onLabel : options.offLabel
    );
    var toggle = document.createElement("span");
    toggle.className = "toggle";
    var input = document.createElement("input");
    input.type = "checkbox";
    input.checked = options.value;
    input.disabled = Boolean(options.disabled);
    input.setAttribute("role", "switch");
    input.setAttribute("aria-label", options.label);
    var knob = document.createElement("span");
    input.addEventListener("change", function () {
      options.update(input.checked);
      header.value.textContent = input.checked ? options.onLabel : options.offLabel;
      options.changed(true);
    });
    toggle.appendChild(input);
    toggle.appendChild(knob);
    control.appendChild(header.element);
    control.appendChild(toggle);
    return control;
  }

  function sectionHeading(title, copy) {
    var heading = document.createElement("div");
    heading.className = "theme-customizer-section-heading";
    var titleElement = document.createElement("h2");
    titleElement.textContent = title;
    heading.appendChild(titleElement);
    if (copy) {
      var copyElement = document.createElement("p");
      copyElement.textContent = copy;
      heading.appendChild(copyElement);
    }
    return heading;
  }

  function nextSeed(seed) {
    return ((Number(seed || 1) * 1664525 + 1013904223) >>> 0) || 1;
  }

  function percent(value) {
    return value + "%";
  }

  // A light position from the screen center, such as "40% left" or "Center".
  function offset(value, negative, positive) {
    if (value === 0) return "Center";
    return Math.abs(value) + "% " + (value < 0 ? negative : positive);
  }

  function speed(value) {
    return (value / 100).toFixed(2) + "\u00d7";
  }

  function appendDarkRanges(container, dark, definitions, changed) {
    definitions.forEach(function (definition) {
      container.appendChild(rangeControl({
        label: definition[0],
        value: dark[definition[1]],
        min: definition[2],
        max: definition[3],
        step: definition[4],
        format: definition[5],
        update: function (value) { dark[definition[1]] = value; },
        changed: changed,
      }));
    });
  }

  function appendDarkControls(container, appearance, changed, rerender) {
    var dark = appearance.dark;
    var basic = dark.detail === "basic";
    function changedAndRerender() {
      changed();
      rerender();
    }

    container.appendChild(sectionHeading(
      "Background detail",
      "Full draws moving polished metal with the graphics card. Basic draws a still version without it."
    ));
    container.appendChild(toggleControl({
      label: "Background detail",
      value: !basic,
      onLabel: "Full",
      offLabel: "Basic",
      update: function (value) { dark.detail = value ? "full" : "basic"; },
      changed: changedAndRerender,
    }));

    container.appendChild(sectionHeading(
      "Surface",
      "Set the metal, the dark surroundings it reflects, and the shape of the plates."
    ));
    container.appendChild(colorControl({
      label: "Surface color",
      value: dark.surfaceColor,
      update: function (value) { dark.surfaceColor = value; },
      changed: changed,
    }));
    container.appendChild(colorControl({
      label: "Metal color",
      value: dark.metalColor,
      update: function (value) { dark.metalColor = value; },
      changed: changed,
    }));
    appendDarkRanges(container, dark, [
      ["Facet size", "facetSize", 140, 520, 5, function (value) { return value + " px"; }],
      ["Facet depth", "facetDepth", 0, 100, 1, percent],
      ["Chrome faces", "chromeFaces", 0, 20, 1, percent],
    ], changed);

    container.appendChild(sectionHeading(
      "Light",
      "Place the studio light and set how soft it is. 100% left, right, up or down reaches the screen edge; more places the light off screen, where it lights mostly the plate edges."
    ));
    appendDarkRanges(container, dark, [
      ["Light strength", "shine", 0, 100, 1, percent],
      ["Light left or right", "lightX", -300, 300, 5, function (value) { return offset(value, "left", "right"); }],
      ["Light up or down", "lightY", -300, 300, 5, function (value) { return offset(value, "up", "down"); }],
      ["Light softness", "lightSoftness", 0, 100, 1, percent],
      ["Edge highlights", "edgeHighlights", 0, 100, 1, percent],
      ["Brushed grain", "surfaceGrain", 0, 100, 1, percent],
    ], changed);

    container.appendChild(sectionHeading(
      "Metal accents",
      "Tint a share of the plates gold or bronze. The tint shows where the metal is lit."
    ));
    ["gold", "bronze"].forEach(function (name) {
      var accent = dark.accents[name];
      var title = name.charAt(0).toUpperCase() + name.slice(1);
      var group = document.createElement("div");
      group.className = "theme-customizer-accent";
      group.appendChild(colorControl({
        label: title,
        value: accent.color,
        update: function (value) { accent.color = value; },
        changed: changed,
      }));
      group.appendChild(rangeControl({
        label: title + " faces",
        value: accent.share,
        min: 0,
        max: 50,
        format: percent,
        update: function (value) { accent.share = value; },
        changed: changed,
      }));
      container.appendChild(group);
    });

    container.appendChild(sectionHeading(
      "Motion",
      basic
        ? "The basic background stays still."
        : "Reflection speed moves the light and tilts the plates; a wandering light also roams the screen. Mesh speed moves and reconnects the plates."
    ));
    container.appendChild(toggleControl({
      label: "Moving light",
      value: dark.lightMotion,
      onLabel: "Moving",
      offLabel: "Still",
      update: function (value) { dark.lightMotion = value; },
      changed: changedAndRerender,
    }));
    container.appendChild(rangeControl({
      label: "Reflection speed",
      value: dark.lightSpeed,
      min: 10,
      max: 400,
      step: 5,
      format: speed,
      update: function (value) { dark.lightSpeed = value; },
      changed: changed,
      disabled: basic || !dark.lightMotion,
    }));
    container.appendChild(toggleControl({
      label: "Wandering light",
      value: dark.lightWander,
      onLabel: "Wandering",
      offLabel: "Stays in place",
      update: function (value) { dark.lightWander = value; },
      changed: changedAndRerender,
      disabled: basic || !dark.lightMotion,
    }));
    container.appendChild(rangeControl({
      label: "Wander distance",
      value: dark.lightWanderDistance,
      min: 0,
      max: 150,
      step: 5,
      format: percent,
      update: function (value) { dark.lightWanderDistance = value; },
      changed: changed,
      disabled: basic || !dark.lightMotion || !dark.lightWander,
    }));
    container.appendChild(toggleControl({
      label: "Moving mesh",
      value: dark.meshMotion,
      onLabel: "Moving",
      offLabel: "Still",
      update: function (value) { dark.meshMotion = value; },
      changed: changedAndRerender,
    }));
    container.appendChild(rangeControl({
      label: "Mesh speed",
      value: dark.meshSpeed,
      min: 10,
      max: 400,
      step: 5,
      format: speed,
      update: function (value) { dark.meshSpeed = value; },
      changed: changed,
      disabled: basic || !dark.meshMotion,
    }));
  }

  function appendLightControls(container, appearance, changed, rerender) {
    container.appendChild(sectionHeading(
      "Diffuse lights",
      "Mix four colors behind the translucent polygon mesh."
    ));
    ["Blue light", "Yellow light", "Green light", "Red light"].forEach(function (name, index) {
      container.appendChild(colorControl({
        label: name,
        value: appearance.light.colors[index],
        update: function (value) { appearance.light.colors[index] = value; },
        changed: changed,
      }));
    });
    container.appendChild(rangeControl({
      label: "Light diffusion",
      value: appearance.light.diffusion,
      min: 0,
      max: 100,
      format: function (value) { return value + "%"; },
      update: function (value) { appearance.light.diffusion = value; },
      changed: changed,
    }));

    container.appendChild(sectionHeading(
      "Polygon finish",
      "Adjust the face opacity, seams, and background movement."
    ));
    container.appendChild(rangeControl({
      label: "Minimum polygon opacity",
      value: appearance.light.opacityLow,
      min: 0,
      max: 100,
      format: function (value) { return value + "%"; },
      update: function (value) {
        appearance.light.opacityLow = Math.min(value, appearance.light.opacityHigh);
      },
      changed: function () {
        changed();
        rerender();
      },
    }));
    container.appendChild(rangeControl({
      label: "Maximum polygon opacity",
      value: appearance.light.opacityHigh,
      min: 0,
      max: 100,
      format: function (value) { return value + "%"; },
      update: function (value) {
        appearance.light.opacityHigh = Math.max(value, appearance.light.opacityLow);
      },
      changed: function () {
        changed();
        rerender();
      },
    }));
    container.appendChild(colorControl({
      label: "Polygon line color",
      value: appearance.light.lineColor,
      update: function (value) { appearance.light.lineColor = value; },
      changed: changed,
    }));
    container.appendChild(rangeControl({
      label: "Polygon line opacity",
      value: appearance.light.lineOpacity,
      min: 0,
      max: 100,
      format: function (value) { return value + "%"; },
      update: function (value) { appearance.light.lineOpacity = value; },
      changed: changed,
    }));
    container.appendChild(toggleControl({
      label: "Background motion",
      value: appearance.light.dynamic,
      onLabel: "Moving",
      offLabel: "Static",
      update: function (value) { appearance.light.dynamic = value; },
      changed: changed,
    }));
  }

  // Closes the panel. Without `saved` the edits are discarded: the page is
  // asked to show the appearance the panel opened with again.
  function closeCustomizer(saved) {
    if (!activeCustomizer) {
      return;
    }
    var session = activeCustomizer;
    activeCustomizer = null;
    document.removeEventListener("keydown", session.escapeHandler);
    session.root.remove();
    document.body.classList.remove("is-theme-customizing");
    if (session.interfaceElement) {
      session.interfaceElement.hidden = session.interfaceWasHidden;
      session.interfaceElement.setAttribute(
        "aria-hidden",
        session.interfaceWasHidden ? "true" : "false"
      );
    }
    if (saved !== true && typeof session.discard === "function") {
      session.discard(clone(session.original));
    }
    if (typeof session.onClose === "function") {
      session.onClose();
    }
    if (session.returnFocus && typeof session.returnFocus.focus === "function") {
      session.returnFocus.focus();
    }
  }

  function cancelCustomizer() {
    if (activeCustomizer && !activeCustomizer.saving) {
      closeCustomizer(false);
    }
  }

  function actionButton(className, label, onClick) {
    var button = document.createElement("button");
    button.className = className;
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", onClick);
    return button;
  }

  // Changes only show while the panel is open. `options.preview(next)` shows
  // an edit, `options.save(next)` keeps it when the person chooses Done (it
  // may return a promise; a rejection keeps the panel open with the error),
  // and `options.discard(original)` restores the opening appearance on
  // Cancel, the close button or Escape.
  function openCustomizer(options) {
    if (!window.UAAppearance) {
      return null;
    }
    closeCustomizer(false);
    var appearance = window.UAAppearance.normalize(
      options.appearance,
      window.UAAppearance.defaults()
    );
    var root = document.createElement("section");
    root.className = "theme-customizer";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-labelledby", "theme-customizer-title");

    var panel = document.createElement("aside");
    panel.className = "theme-customizer-panel";
    var header = document.createElement("header");
    header.className = "theme-customizer-header";
    var heading = document.createElement("div");
    var kicker = document.createElement("p");
    kicker.className = "page-kicker";
    kicker.textContent = "Customize Theme";
    var title = document.createElement("h1");
    title.id = "theme-customizer-title";
    title.textContent = appearance.mode === "dark"
      ? "Dark Metallic"
      : "Diffuse Light";
    var copy = document.createElement("p");
    copy.textContent = "Tune the background while the interface stays out of the way. Done saves your changes; Cancel puts the theme back.";
    heading.appendChild(kicker);
    heading.appendChild(title);
    heading.appendChild(copy);
    var closeButton = actionButton("settings-close-button", "×", cancelCustomizer);
    closeButton.setAttribute("aria-label", "Cancel and close theme customizer");
    header.appendChild(heading);
    header.appendChild(closeButton);

    var scroll = document.createElement("div");
    scroll.className = "theme-customizer-scroll";
    var actions = document.createElement("footer");
    actions.className = "theme-customizer-actions";
    var status = document.createElement("div");
    status.className = "warning-callout theme-customizer-status";
    status.setAttribute("role", "alert");
    status.hidden = true;
    var statusIcon = document.createElement("span");
    statusIcon.className = "warning-icon";
    statusIcon.textContent = "!";
    var statusText = document.createElement("p");
    status.appendChild(statusIcon);
    status.appendChild(statusText);
    var regenerate = actionButton("button ghost theme-customizer-regenerate", "Regenerate Mesh", function () {
      if (appearance.mode === "dark") {
        appearance.dark.seed = nextSeed(appearance.dark.seed);
      } else {
        appearance.light.seed = nextSeed(appearance.light.seed);
      }
      changed();
    });
    var cancel = actionButton("button ghost", "Cancel", cancelCustomizer);
    var done = actionButton("button primary", "Done", save);
    actions.appendChild(status);
    actions.appendChild(regenerate);
    actions.appendChild(cancel);
    actions.appendChild(done);
    panel.appendChild(header);
    panel.appendChild(scroll);
    panel.appendChild(actions);
    root.appendChild(panel);

    var session = {
      original: clone(appearance),
      saving: false,
      escapeHandler: function (event) {
        if (event.key === "Escape") {
          cancelCustomizer();
        }
      },
      interfaceElement: options.interfaceElement || null,
      interfaceWasHidden: Boolean(options.interfaceElement && options.interfaceElement.hidden),
      discard: options.discard,
      onClose: options.onClose,
      returnFocus: options.returnFocus || document.activeElement,
      root: root,
    };
    activeCustomizer = session;

    function showError(message) {
      statusText.textContent = message;
      status.hidden = !message;
    }

    function changed() {
      showError("");
      refreshShareText();
      if (typeof options.preview === "function") {
        options.preview(clone(appearance));
      }
    }

    function setSaving(saving) {
      session.saving = saving;
      [closeButton, regenerate, cancel, done, copyButton, applyButton].forEach(function (button) {
        button.disabled = saving;
      });
      done.textContent = saving ? "Saving..." : "Done";
      if (saving) {
        done.setAttribute("aria-busy", "true");
      } else {
        done.removeAttribute("aria-busy");
      }
    }

    function save() {
      if (session.saving) {
        return;
      }
      showError("");
      setSaving(true);
      Promise.resolve()
        .then(function () {
          return typeof options.save === "function" ? options.save(clone(appearance)) : null;
        })
        .then(function () {
          if (activeCustomizer === session) {
            closeCustomizer(true);
          }
        })
        .catch(function (error) {
          if (activeCustomizer !== session) {
            return;
          }
          setSaving(false);
          showError((error && error.message) || "Theme changes could not be saved. Try again.");
        });
    }

    // Share settings: the theme as text to copy out, or pasted text to try.
    var shareText = document.createElement("textarea");
    shareText.className = "textarea theme-customizer-share-text";
    shareText.spellcheck = false;
    shareText.rows = 8;
    shareText.setAttribute("aria-label", "Theme settings text");
    var shareNote = document.createElement("p");
    shareNote.className = "theme-customizer-share-note";
    shareNote.setAttribute("role", "status");
    var copyButton = actionButton("button ghost", "Copy", copySettings);
    var applyButton = actionButton("button ghost", "Apply", applySettings);

    function shareSection() {
      var section = document.createElement("div");
      section.className = "theme-customizer-share";
      section.appendChild(sectionHeading(
        "Share settings",
        "Copy these settings to keep them or use them elsewhere, for example under ui.onboarding.appearance in instance_config.yaml. To try settings, paste them here and choose Apply."
      ));
      section.appendChild(shareText);
      var buttons = document.createElement("div");
      buttons.className = "theme-customizer-share-actions";
      buttons.appendChild(copyButton);
      buttons.appendChild(applyButton);
      section.appendChild(buttons);
      section.appendChild(shareNote);
      return section;
    }

    function refreshShareText() {
      shareText.value = window.UAAppearance.settingsText(appearance);
      shareNote.textContent = "";
    }

    // The clipboard is only open to secure pages (https or localhost); on
    // others the text is selected so the person can copy it.
    function copySettings() {
      showError("");
      var text = shareText.value;
      function selectForCopy() {
        shareText.focus();
        if (typeof shareText.select === "function") shareText.select();
        shareNote.textContent = "The text is selected. Copy it with Ctrl+C (Cmd+C on a Mac).";
      }
      if (window.navigator && window.navigator.clipboard && window.isSecureContext) {
        window.navigator.clipboard.writeText(text).then(function () {
          shareNote.textContent = "Copied.";
        }, selectForCopy);
      } else {
        selectForCopy();
      }
    }

    function applySettings() {
      shareNote.textContent = "";
      var parsed;
      try {
        parsed = window.UAAppearance.readSettingsText(shareText.value);
      } catch (error) {
        showError(error.message);
        return;
      }
      var next = window.UAAppearance.normalize(parsed, appearance);
      appearance.mode = next.mode;
      appearance.light = next.light;
      appearance.dark = next.dark;
      renderControls();
      changed();
      shareNote.textContent = "Applied. Done saves these settings; Cancel puts the theme back.";
    }

    function renderControls() {
      scroll.replaceChildren();
      title.textContent = appearance.mode === "dark" ? "Dark Metallic" : "Diffuse Light";
      if (appearance.mode === "dark") {
        appendDarkControls(scroll, appearance, changed, renderControls);
      } else {
        appendLightControls(scroll, appearance, changed, renderControls);
      }
      scroll.appendChild(shareSection());
      refreshShareText();
    }

    if (session.interfaceElement) {
      session.interfaceElement.hidden = true;
      session.interfaceElement.setAttribute("aria-hidden", "true");
    }
    document.body.classList.add("is-theme-customizing");
    document.body.appendChild(root);
    document.addEventListener("keydown", session.escapeHandler);
    renderControls();
    closeButton.focus();
    return {
      close: cancelCustomizer,
      current: function () { return clone(appearance); },
    };
  }

  window.UAAppearanceCustomizer = {
    open: openCustomizer,
    // Closes an open panel without saving (sign-out, for example).
    close: function () { closeCustomizer(false); },
  };
}());
