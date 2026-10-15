(function () {
  "use strict";

  if (window.UAAppearance) return;

  var DEFAULTS = {
    mode: "dark",
    // Interface animation (Web Chat and onboarding motion); the system's
    // reduced-motion setting turns it off as well.
    animation: true,
    light: {
      colors: ["#082f73", "#ffd60a", "#20f05a", "#ff3030"],
      diffusion: 58,
      opacityLow: 20,
      opacityHigh: 70,
      lineColor: "#202426",
      lineOpacity: 8,
      seed: 4147,
      dynamic: true,
    },
    // Mirrors ui.onboarding.appearance.dark in default_values.py.
    dark: {
      detail: "full",
      surfaceColor: "#007007",
      metalColor: "#015b1a",
      facetSize: 230,
      facetDepth: 80,
      chromeFaces: 0,
      shine: 51,
      edgeHighlights: 31,
      surfaceGrain: 24,
      lightX: -7,
      lightY: -5,
      lightSoftness: 37,
      lightMotion: true,
      lightWander: false,
      lightWanderDistance: 133,
      lightSpeed: 110,
      meshMotion: true,
      meshSpeed: 100,
      seed: 9187,
      accents: {
        gold: { color: "#464020", share: 10 },
        bronze: { color: "#00751d", share: 39 },
      },
    },
  };
  // Dark background settings: browser name, saved (snake_case) name, CSS
  // variable and kind. This one table drives normalizing, the CSS baseline
  // that static pages read, the root CSS variables and the saved payload.
  // The CSS names must match DARK_CSS_VARIABLES in
  // runtime_settings/appearance.py, which writes them for Docs and broker
  // pages; a test compares the two.
  var DARK_FIELDS = [
    { name: "detail", saved: "detail", css: "--ua-dark-detail", kind: "choice", choices: ["full", "basic"] },
    { name: "surfaceColor", saved: "surface_color", css: "--ua-dark-surface", kind: "color" },
    { name: "metalColor", saved: "metal_color", css: "--ua-dark-metal", kind: "color" },
    { name: "facetSize", saved: "facet_size", css: "--ua-dark-facet-size", kind: "number", min: 140, max: 520 },
    { name: "facetDepth", saved: "facet_depth", css: "--ua-dark-facet-depth", kind: "number", min: 0, max: 100 },
    { name: "chromeFaces", saved: "chrome_faces", css: "--ua-dark-chrome-faces", kind: "number", min: 0, max: 20 },
    { name: "shine", saved: "shine", css: "--ua-dark-shine", kind: "number", min: 0, max: 100 },
    { name: "edgeHighlights", saved: "edge_highlights", css: "--ua-dark-edge-highlights", kind: "number", min: 0, max: 100 },
    { name: "surfaceGrain", saved: "surface_grain", css: "--ua-dark-surface-grain", kind: "number", min: 0, max: 100 },
    { name: "lightX", saved: "light_x", css: "--ua-dark-light-x", kind: "number", min: -300, max: 300 },
    { name: "lightY", saved: "light_y", css: "--ua-dark-light-y", kind: "number", min: -300, max: 300 },
    { name: "lightSoftness", saved: "light_softness", css: "--ua-dark-light-softness", kind: "number", min: 0, max: 100 },
    { name: "lightMotion", saved: "light_motion", css: "--ua-dark-light-motion", kind: "boolean" },
    { name: "lightWander", saved: "light_wander", css: "--ua-dark-light-wander", kind: "boolean" },
    { name: "lightWanderDistance", saved: "light_wander_distance", css: "--ua-dark-light-wander-distance", kind: "number", min: 0, max: 150 },
    { name: "lightSpeed", saved: "light_speed", css: "--ua-dark-light-speed", kind: "number", min: 10, max: 400 },
    { name: "meshMotion", saved: "mesh_motion", css: "--ua-dark-mesh-motion", kind: "boolean" },
    { name: "meshSpeed", saved: "mesh_speed", css: "--ua-dark-mesh-speed", kind: "number", min: 10, max: 400 },
    { name: "seed", saved: "seed", css: "--ua-dark-seed", kind: "number", min: 1, max: 4294967295 },
  ];
  // Accent metals; each has a tint color and a share of faces (percent).
  // CSS variables are --ua-dark-<name> and --ua-dark-<name>-share.
  var DARK_ACCENTS = ["gold", "bronze"];
  var ACCENT_SHARE_RANGE = [0, 50];
  var baseline = clone(DEFAULTS);
  var active = clone(DEFAULTS);
  var userPreference = null;
  // Unsaved appearance shown while Customize Theme is open. It sits above the
  // saved preference, so preference reloads cannot overwrite it and ending
  // the preview shows the saved appearance again.
  var previewAppearance = null;
  var appliedAppearance = "";
  var announcedPreference = null;

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function objectOrEmpty(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function firstDefined(source, names, fallback) {
    for (var index = 0; index < names.length; index += 1) {
      if (source[names[index]] !== undefined && source[names[index]] !== null) {
        return source[names[index]];
      }
    }
    return fallback;
  }

  function clampNumber(value, fallback, min, max, integer) {
    var parsed = Number(value);
    if (!Number.isFinite(parsed)) {
      parsed = fallback;
    }
    parsed = Math.max(min, Math.min(max, parsed));
    return integer ? Math.round(parsed) : parsed;
  }

  function color(value, fallback) {
    var rendered = String(value || "").trim().toLowerCase();
    return /^#[0-9a-f]{6}$/.test(rendered) ? rendered : fallback;
  }

  function booleanValue(value, fallback) {
    return typeof value === "boolean" ? value : fallback;
  }

  function normalizeAccent(source, fallback) {
    var candidate = objectOrEmpty(source);
    return {
      color: color(candidate.color, fallback.color),
      share: clampNumber(candidate.share, fallback.share, ACCENT_SHARE_RANGE[0], ACCENT_SHARE_RANGE[1], true),
    };
  }

  function normalizeDarkField(field, value, fallback) {
    if (field.kind === "color") return color(value, fallback);
    if (field.kind === "boolean") return booleanValue(value, fallback);
    if (field.kind === "choice") return field.choices.indexOf(value) >= 0 ? value : fallback;
    return clampNumber(value, fallback, field.min, field.max, true);
  }

  function normalizeLight(source, fallback) {
    var candidate = objectOrEmpty(source);
    var rawColors = firstDefined(candidate, ["colors", "light_colors"], fallback.colors);
    var colors = Array.isArray(rawColors) ? rawColors.slice(0, 4) : fallback.colors.slice();
    while (colors.length < 4) {
      colors.push(fallback.colors[colors.length]);
    }
    colors = colors.map(function (value, index) {
      return color(value, fallback.colors[index]);
    });
    var opacityLow = clampNumber(
      firstDefined(candidate, ["opacityLow", "polygon_opacity_low"], fallback.opacityLow),
      fallback.opacityLow,
      0,
      100,
      true
    );
    var opacityHigh = clampNumber(
      firstDefined(candidate, ["opacityHigh", "polygon_opacity_high"], fallback.opacityHigh),
      fallback.opacityHigh,
      0,
      100,
      true
    );
    return {
      colors: colors,
      diffusion: clampNumber(
        firstDefined(candidate, ["diffusion", "light_diffusion"], fallback.diffusion),
        fallback.diffusion,
        0,
        100,
        true
      ),
      opacityLow: Math.min(opacityLow, opacityHigh),
      opacityHigh: Math.max(opacityLow, opacityHigh),
      lineColor: color(
        firstDefined(candidate, ["lineColor", "polygon_line_color"], fallback.lineColor),
        fallback.lineColor
      ),
      lineOpacity: clampNumber(
        firstDefined(candidate, ["lineOpacity", "polygon_line_opacity"], fallback.lineOpacity),
        fallback.lineOpacity,
        0,
        100,
        true
      ),
      seed: clampNumber(
        firstDefined(candidate, ["seed", "polygon_seed"], fallback.seed),
        fallback.seed,
        1,
        4294967295,
        true
      ),
      dynamic: booleanValue(
        firstDefined(candidate, ["dynamic", "dynamic_background"], fallback.dynamic),
        fallback.dynamic
      ),
    };
  }

  // Accepts browser (camelCase) or saved (snake_case) names.
  function normalizeDark(source, fallback) {
    var candidate = objectOrEmpty(source);
    var rawAccents = objectOrEmpty(candidate.accents);
    var dark = { accents: {} };
    DARK_FIELDS.forEach(function (field) {
      var value = firstDefined(candidate, [field.name, field.saved], fallback[field.name]);
      dark[field.name] = normalizeDarkField(field, value, fallback[field.name]);
    });
    DARK_ACCENTS.forEach(function (name) {
      dark.accents[name] = normalizeAccent(rawAccents[name], fallback.accents[name]);
    });
    return dark;
  }

  // Dark settings under their saved (snake_case) names, for the server.
  function savedDark(dark) {
    var normalized = normalizeDark(dark, DEFAULTS.dark);
    var saved = {};
    DARK_FIELDS.forEach(function (field) {
      saved[field.saved] = normalized[field.name];
    });
    // Last, as in config.example.yaml and the settings model.
    saved.accents = {};
    DARK_ACCENTS.forEach(function (name) {
      saved.accents[name] = clone(normalized.accents[name]);
    });
    return saved;
  }

  // A whole appearance under its saved (snake_case) names: the shape of
  // ui.onboarding.appearance in instance_config.yaml and of a person's saved
  // Web Chat appearance.
  function saved(appearance) {
    var normalized = normalize(appearance, DEFAULTS);
    return {
      color_mode: normalized.mode,
      light_colors: normalized.light.colors.slice(),
      light_diffusion: normalized.light.diffusion,
      polygon_opacity_low: normalized.light.opacityLow,
      polygon_opacity_high: normalized.light.opacityHigh,
      polygon_line_color: normalized.light.lineColor,
      polygon_line_opacity: normalized.light.lineOpacity,
      polygon_seed: normalized.light.seed,
      dynamic_background: normalized.light.dynamic,
      interface_animation: normalized.animation,
      dark: savedDark(normalized.dark),
    };
  }

  function normalize(candidate, fallback) {
    var source = objectOrEmpty(candidate);
    var base = fallback ? clone(fallback) : clone(DEFAULTS);
    var explicitLight = objectOrEmpty(source.light);
    var explicitDark = objectOrEmpty(source.dark);
    return {
      mode: String(firstDefined(source, ["mode", "color_mode"], base.mode)).toLowerCase() === "light"
        ? "light"
        : "dark",
      light: normalizeLight(
        Object.keys(explicitLight).length ? explicitLight : source,
        base.light
      ),
      dark: normalizeDark(explicitDark, base.dark),
      animation: booleanValue(
        firstDefined(source, ["animation", "interface_animation"], base.animation),
        base.animation !== false
      ),
    };
  }

  function cssSetting(name) {
    return window.getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
  }

  function cssNumber(name, fallback, percentage) {
    var raw = cssSetting(name);
    var parsed = Number(raw);
    if (!raw || !Number.isFinite(parsed)) {
      return fallback;
    }
    return percentage && parsed <= 1 ? parsed * 100 : parsed;
  }

  // Docs and broker pages receive the saved dark settings as CSS variables.
  function cssDarkBaseline() {
    var dark = { accents: {} };
    DARK_FIELDS.forEach(function (field) {
      var raw = cssSetting(field.css);
      if (!raw) return;
      if (field.kind === "boolean") dark[field.saved] = raw === "true";
      else if (field.kind === "number") dark[field.saved] = Number(raw);
      else dark[field.saved] = raw;
    });
    DARK_ACCENTS.forEach(function (name) {
      var accent = {};
      var tint = cssSetting("--ua-dark-" + name);
      var share = cssSetting("--ua-dark-" + name + "-share");
      if (tint) accent.color = tint;
      if (share) accent.share = Number(share);
      dark.accents[name] = accent;
    });
    return dark;
  }

  function cssBaseline() {
    var defaults = clone(DEFAULTS);
    return {
      color_mode: cssSetting("--ua-runtime-color-mode") || defaults.mode,
      light: {
        colors: [
          cssSetting("--ua-doc-light-blue") || defaults.light.colors[0],
          cssSetting("--ua-doc-light-yellow") || defaults.light.colors[1],
          cssSetting("--ua-doc-light-green") || defaults.light.colors[2],
          cssSetting("--ua-doc-light-red") || defaults.light.colors[3],
        ],
        diffusion: cssNumber("--ua-doc-light-diffusion", defaults.light.diffusion, false),
        opacityLow: cssNumber("--ua-doc-polygon-opacity-low", defaults.light.opacityLow, true),
        opacityHigh: cssNumber("--ua-doc-polygon-opacity-high", defaults.light.opacityHigh, true),
        lineColor: cssSetting("--ua-doc-polygon-line-color") || defaults.light.lineColor,
        lineOpacity: cssNumber("--ua-doc-polygon-line-opacity", defaults.light.lineOpacity, true),
        seed: cssNumber("--ua-light-mesh-seed", defaults.light.seed, false),
        dynamic: cssSetting("--ua-light-dynamic") !== "false",
      },
      dark: cssDarkBaseline(),
    };
  }

  function hexToHsl(hex) {
    var rgb = [1, 3, 5].map(function (offset) { return parseInt(hex.slice(offset, offset + 2), 16) / 255; });
    var max = Math.max(rgb[0], rgb[1], rgb[2]);
    var min = Math.min(rgb[0], rgb[1], rgb[2]);
    var lightness = (max + min) / 2;
    var delta = max - min;
    if (!delta) return [0, 0, lightness];
    var saturation = delta / (1 - Math.abs(2 * lightness - 1));
    var hue = max === rgb[0] ? ((rgb[1] - rgb[2]) / delta) % 6
      : max === rgb[1] ? (rgb[2] - rgb[0]) / delta + 2
        : (rgb[0] - rgb[1]) / delta + 4;
    return [(hue * 60 + 360) % 360, saturation, lightness];
  }

  function hslToHex(hsl) {
    var chroma = (1 - Math.abs(2 * hsl[2] - 1)) * hsl[1];
    var x = chroma * (1 - Math.abs((hsl[0] / 60) % 2 - 1));
    var m = hsl[2] - chroma / 2;
    var sector = Math.floor(hsl[0] / 60) % 6;
    var rgb = [[chroma, x, 0], [x, chroma, 0], [0, chroma, x], [0, x, chroma], [x, 0, chroma], [chroma, 0, x]][sector];
    return "#" + rgb.map(function (value) {
      return ("0" + Math.round((value + m) * 255).toString(16)).slice(-2);
    }).join("");
  }

  // The accent for interface motion. Dark mode takes the hue of the theme's
  // gold metal tint and raises its lightness and saturation enough to read as
  // a thin line over dark panes (the owner chose "match the theme",
  // 2026-10-04; tints are often too dark to see as lines). Light mode takes
  // the hue of the person's first (blue) light, kept dark enough to read on
  // light glass (owner's request, 2026-10-09; it was a fixed ocean blue).
  function motionAccent(appearance) {
    if (appearance.mode === "light") {
      var light = hexToHsl(appearance.light.colors[0]);
      return hslToHex([light[0], within(light[1], 0.5, 0.9), within(light[2], 0.22, 0.34)]);
    }
    var hsl = hexToHsl(appearance.dark.accents.gold.color);
    return hslToHex([hsl[0], Math.max(hsl[1], 0.5), Math.max(hsl[2], 0.62)]);
  }

  function within(value, low, high) {
    return Math.min(high, Math.max(low, value));
  }

  // Colors for the small meshes of interface motion (a sent message's
  // arrival mesh), so they look like the person's background rather than a
  // foreign color (owner's request, 2026-10-04). Dark mode: the metal color
  // with the gold and bronze tints at their shares of the plates, each kept
  // in hue but raised to a lightness that shows over dark panes. Light mode:
  // the four light colors, equally. Returns [{ color: "#rrggbb", weight }].
  var MOTION_PALETTE_LIGHTNESS = 0.58;

  function liftForMotion(hex) {
    var hsl = hexToHsl(hex);
    return hslToHex([hsl[0], Math.min(hsl[1], 0.75), Math.max(hsl[2], MOTION_PALETTE_LIGHTNESS)]);
  }

  function motionPalette(appearance) {
    var normalized = normalize(appearance, DEFAULTS);
    if (normalized.mode === "light") {
      return normalized.light.colors.map(function (color) { return { color: color, weight: 1 }; });
    }
    var accents = normalized.dark.accents;
    var accentShare = DARK_ACCENTS.reduce(function (sum, name) { return sum + accents[name].share; }, 0);
    var palette = [{ color: liftForMotion(normalized.dark.metalColor), weight: Math.max(0, 100 - accentShare) }];
    DARK_ACCENTS.forEach(function (name) {
      if (accents[name].share > 0) {
        palette.push({ color: liftForMotion(accents[name].color), weight: accents[name].share });
      }
    });
    return palette;
  }

  function applyRoot(appearance) {
    var root = document.documentElement;
    root.setAttribute("data-color-mode", appearance.mode);
    root.setAttribute("data-ui-motion", appearance.animation ? "on" : "off");
    root.style.setProperty("--ua-motion-accent", motionAccent(appearance));
    root.style.setProperty("--ua-runtime-color-mode", appearance.mode);
    root.style.setProperty("--ua-doc-light-blue", appearance.light.colors[0]);
    root.style.setProperty("--ua-doc-light-yellow", appearance.light.colors[1]);
    root.style.setProperty("--ua-doc-light-green", appearance.light.colors[2]);
    root.style.setProperty("--ua-doc-light-red", appearance.light.colors[3]);
    root.style.setProperty("--ua-doc-light-diffusion", String(appearance.light.diffusion));
    root.style.setProperty("--ua-doc-polygon-opacity-low", (appearance.light.opacityLow / 100).toFixed(3));
    root.style.setProperty("--ua-doc-polygon-opacity-high", (appearance.light.opacityHigh / 100).toFixed(3));
    root.style.setProperty("--ua-doc-polygon-line-color", appearance.light.lineColor);
    root.style.setProperty("--ua-doc-polygon-line-opacity", (appearance.light.lineOpacity / 100).toFixed(3));
    root.style.setProperty("--ua-light-mesh-seed", String(appearance.light.seed));
    root.style.setProperty("--ua-light-dynamic", String(appearance.light.dynamic));
    DARK_FIELDS.forEach(function (field) {
      root.style.setProperty(field.css, String(appearance.dark[field.name]));
    });
    DARK_ACCENTS.forEach(function (name) {
      root.style.setProperty("--ua-dark-" + name, appearance.dark.accents[name].color);
      root.style.setProperty("--ua-dark-" + name + "-share", String(appearance.dark.accents[name].share));
    });
  }

  function announce() {
    var serialized = JSON.stringify(active);
    var hasPreference = Boolean(userPreference);
    if (serialized === appliedAppearance && hasPreference === announcedPreference) return;
    // Polls and auth refreshes often return identical preferences. Reapplying
    // them invalidates Chromium's frosted layers and restarts mesh renderers.
    if (serialized !== appliedAppearance) applyRoot(active);
    appliedAppearance = serialized;
    announcedPreference = hasPreference;
    document.dispatchEvent(new CustomEvent("ua:appearance-change", {
      detail: { appearance: clone(active), hasUserPreference: hasPreference },
    }));
  }

  function recompute() {
    var shown = previewAppearance || userPreference;
    active = shown ? normalize(shown, baseline) : clone(baseline);
    announce();
    return clone(active);
  }

  function setBaseline(candidate) {
    baseline = normalize(candidate, DEFAULTS);
    return recompute();
  }

  function setUserPreference(candidate) {
    userPreference = normalize(candidate, baseline);
    return recompute();
  }

  function clearUserPreference() {
    userPreference = null;
    return recompute();
  }

  function preview(candidate) {
    previewAppearance = normalize(candidate, baseline);
    return recompute();
  }

  function endPreview() {
    previewAppearance = null;
    return recompute();
  }

  function loadServerPreference() {
    if (!window.fetch) {
      return Promise.resolve(clone(active));
    }
    return window.fetch("/api/web-chat/appearance", {
      cache: "no-store",
      credentials: "same-origin",
    }).then(function (response) {
      if (response.status === 401 || response.status === 403 || response.status === 404) {
        return null;
      }
      if (!response.ok) {
        throw new Error("Appearance settings are unavailable.");
      }
      return response.json();
    }).then(function (payload) {
      userPreference = payload && payload.appearance
        ? normalize(payload.appearance, baseline)
        : null;
      return recompute();
    });
  }

  // ---------- settings text ----------
  //
  // One text form for theme settings everywhere (Customize Theme's Share
  // settings and the background lab): YAML that fits under
  // ui.onboarding.appearance in instance_config.yaml. The reader also takes
  // JSON, a larger piece of the config file around the appearance section,
  // or a dark section on its own (saved or browser names). The reader understands the YAML this writer produces and
  // what people copy from a config file: nested "key: value" lines, quoted
  // or plain values, comments, and one-line [lists] and {mappings}.

  var SETTINGS_TEXT_HEADER = [
    "# Universal Agents theme. To use it in instance_config.yaml, replace the",
    "# lines under `appearance:` with these, moving every line right by the same",
    "# number of spaces so each starts two spaces right of `appearance:`. Or",
    "# paste it into Share settings and choose Apply.",
  ];

  function yamlScalar(value) {
    if (typeof value === "string") return JSON.stringify(value);
    if (Array.isArray(value)) return "[" + value.map(yamlScalar).join(", ") + "]";
    return String(value);
  }

  function isPlainMapping(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
  }

  // A small mapping of plain values (an accent's color and share) goes on
  // one line, as in config.example.yaml: fewer nested lines leave fewer
  // indents to get wrong when the text is moved into a config file.
  function inlineMapping(value) {
    var keys = Object.keys(value);
    if (keys.length > 3 || keys.some(function (key) { return isPlainMapping(value[key]); })) return null;
    return "{" + keys.map(function (key) { return key + ": " + yamlScalar(value[key]); }).join(", ") + "}";
  }

  function yamlLines(mapping, indent) {
    var lines = [];
    Object.keys(mapping).forEach(function (key) {
      var value = mapping[key];
      var inline = isPlainMapping(value) ? inlineMapping(value) : null;
      if (inline) {
        lines.push(indent + key + ": " + inline);
      } else if (isPlainMapping(value)) {
        lines.push(indent + key + ":");
        lines = lines.concat(yamlLines(value, indent + "  "));
      } else {
        lines.push(indent + key + ": " + yamlScalar(value));
      }
    });
    return lines;
  }

  function settingsText(appearance) {
    return SETTINGS_TEXT_HEADER.concat(yamlLines(saved(appearance), "")).join("\n") + "\n";
  }

  function settingsTextError() {
    return new Error("This text could not be read as theme settings. Paste the text Copy produced, or the appearance section of instance_config.yaml.");
  }

  // Removes a comment: a # outside quotes that starts the line or follows a space.
  function withoutComment(line) {
    var quote = "";
    for (var index = 0; index < line.length; index += 1) {
      var character = line[index];
      if (quote) {
        if (character === "\\" && quote === "\"") index += 1;
        else if (character === quote) quote = "";
      } else if (character === "\"" || character === "'") {
        quote = character;
      } else if (character === "#" && (index === 0 || /\s/.test(line[index - 1]))) {
        return line.slice(0, index);
      }
    }
    return line;
  }

  // Splits "a, b, c" at commas outside quotes and brackets.
  function splitFlow(text) {
    var parts = [];
    var depth = 0;
    var quote = "";
    var start = 0;
    for (var index = 0; index < text.length; index += 1) {
      var character = text[index];
      if (quote) {
        if (character === "\\" && quote === "\"") index += 1;
        else if (character === quote) quote = "";
      } else if (character === "\"" || character === "'") {
        quote = character;
      } else if (character === "[" || character === "{") {
        depth += 1;
      } else if (character === "]" || character === "}") {
        depth -= 1;
      } else if (character === "," && depth === 0) {
        parts.push(text.slice(start, index));
        start = index + 1;
      }
    }
    parts.push(text.slice(start));
    return parts.map(function (part) { return part.trim(); }).filter(Boolean);
  }

  function splitKey(text) {
    var match = /^("[^"]*"|'[^']*'|[^:]+):(?:\s+|$)([\s\S]*)$/.exec(text.trim());
    if (!match) throw settingsTextError();
    return { key: match[1].replace(/^["']|["']$/g, "").trim(), value: match[2].trim() };
  }

  function yamlValue(text) {
    var value = text.trim();
    if (value.charAt(0) === "[") {
      if (value.charAt(value.length - 1) !== "]") throw settingsTextError();
      return splitFlow(value.slice(1, -1)).map(yamlValue);
    }
    if (value.charAt(0) === "{") {
      if (value.charAt(value.length - 1) !== "}") throw settingsTextError();
      var mapping = {};
      splitFlow(value.slice(1, -1)).forEach(function (entry) {
        var pair = splitKey(entry);
        mapping[pair.key] = yamlValue(pair.value);
      });
      return mapping;
    }
    if (value.charAt(0) === "\"") {
      try { return JSON.parse(value); } catch (error) { throw settingsTextError(); }
    }
    if (value.charAt(0) === "'") return value.slice(1, -1).replace(/''/g, "'");
    if (value === "true" || value === "false") return value === "true";
    if (value === "null" || value === "~" || value === "") return null;
    if (/^-?\d+(\.\d+)?$/.test(value)) return Number(value);
    return value;
  }

  function readYaml(text) {
    var root = {};
    var stack = [{ indent: -1, mapping: root }];
    text.split(/\r?\n/).forEach(function (rawLine) {
      var line = withoutComment(rawLine).replace(/\s+$/, "");
      if (!line.trim() || line.trim() === "---") return;
      var indent = line.length - line.replace(/^ +/, "").length;
      while (stack.length > 1 && indent <= stack[stack.length - 1].indent) stack.pop();
      var pair = splitKey(line);
      var parent = stack[stack.length - 1].mapping;
      if (pair.value === "") {
        parent[pair.key] = {};
        stack.push({ indent: indent, mapping: parent[pair.key] });
      } else {
        parent[pair.key] = yamlValue(pair.value);
      }
    });
    return root;
  }

  // A dark section pasted on its own, such as the dark background lab's
  // settings: it holds dark settings and none of the whole appearance's.
  function isDarkSection(value) {
    if ("dark" in value || "color_mode" in value || "mode" in value) return false;
    return DARK_FIELDS.some(function (field) { return field.name in value || field.saved in value; });
  }

  // The theme settings in pasted text, under their saved names. Accepts the
  // appearance section itself or a larger piece of the config file around it.
  function readSettingsText(text) {
    var trimmed = String(text || "").trim();
    if (!trimmed) throw new Error("Paste theme settings first.");
    var parsed;
    if (trimmed.charAt(0) === "{") {
      try { parsed = JSON.parse(trimmed); } catch (error) { throw settingsTextError(); }
    } else {
      parsed = readYaml(trimmed);
    }
    ["ui", "onboarding", "appearance"].forEach(function (key) {
      if (parsed && parsed[key] && typeof parsed[key] === "object") parsed = parsed[key];
    });
    if (parsed && typeof parsed === "object" && isDarkSection(parsed)) {
      parsed = { dark: parsed };
    }
    var known = ["color_mode", "mode", "dark", "light", "light_colors", "light_diffusion", "interface_animation"];
    if (!parsed || typeof parsed !== "object" || !known.some(function (key) { return key in parsed; })) {
      throw new Error("This text has no theme settings. Paste the text Copy produced, or the appearance section of instance_config.yaml.");
    }
    return parsed;
  }

  window.UAAppearance = {
    defaults: function () { return clone(DEFAULTS); },
    baseline: function () { return clone(baseline); },
    current: function () { return clone(active); },
    normalize: normalize,
    saved: saved,
    motionPalette: function () { return motionPalette(active); },
    settingsText: settingsText,
    readSettingsText: readSettingsText,
    savedDark: savedDark,
    setBaseline: setBaseline,
    setUserPreference: setUserPreference,
    clearUserPreference: clearUserPreference,
    preview: preview,
    endPreview: endPreview,
    hasUserPreference: function () { return Boolean(userPreference); },
    loadServerPreference: loadServerPreference,
  };

  baseline = normalize(cssBaseline(), DEFAULTS);
  active = clone(baseline);
  recompute();
  if (/\/(?:webchat|config-editor|docs)(?:\/|$)/.test(window.location.pathname)) {
    loadServerPreference().catch(function () {
      return null;
    });
  }
})();
