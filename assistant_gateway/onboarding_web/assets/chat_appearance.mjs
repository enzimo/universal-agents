/** Existing chat appearance view behavior with explicit state and callback dependencies. */
export function createAppearance({ appearanceState, sharedState, operations, lifecycle, requestClient }) {
function clamp(value, min, max) {
    if (Number.isNaN(value)) {
      return min;
    }
    return Math.max(min, Math.min(max, value));
  }

function normalizeHexColor(value, fallback) {
    if (/^#[0-9a-f]{6}$/i.test(value || "")) {
      return value;
    }
    return fallback;
  }

function hexToRgb(hex) {
    var normalized = normalizeHexColor(hex, "#ffffff");
    return [
      parseInt(normalized.slice(1, 3), 16),
      parseInt(normalized.slice(3, 5), 16),
      parseInt(normalized.slice(5, 7), 16),
    ];
  }

function loadNumberSetting(key, fallback, min, max) {
    var stored = parseInt(window.localStorage.getItem(key) || "", 10);
    if (Number.isNaN(stored)) {
      return fallback;
    }
    return clamp(stored, min, max);
  }

function loadAppearanceSettings() {
    if (window.UAAppearance && typeof window.UAAppearance.current === "function") {
      var shared = window.UAAppearance.current().light;
      return {
        lightColors: {
          blue: shared.colors[0],
          yellow: shared.colors[1],
          green: shared.colors[2],
          red: shared.colors[3],
        },
        lightDiffusion: shared.diffusion,
        polygonOpacityLow: shared.opacityLow,
        polygonOpacityHigh: shared.opacityHigh,
        polygonLineColor: shared.lineColor,
        polygonLineOpacity: shared.lineOpacity,
        polygonSeed: shared.seed,
        backgroundDynamic: shared.dynamic,
      };
    }
    var lightColors = {};
    appearanceState.lightDefinitions.forEach(function (light) {
      lightColors[light.key] = normalizeHexColor(
        window.localStorage.getItem("ua.preview.light." + light.key),
        light.defaultColor
      );
    });
    var opacityLow = loadNumberSetting("ua.preview.polygon.opacity.low", 20, 0, 100);
    var opacityHigh = loadNumberSetting("ua.preview.polygon.opacity.high", 70, 0, 100);
    if (opacityLow > opacityHigh) {
      opacityLow = opacityHigh;
    }
    return {
      lightColors: lightColors,
      lightDiffusion: loadNumberSetting("ua.preview.light.diffusion", 58, 0, 100),
      polygonOpacityLow: opacityLow,
      polygonOpacityHigh: opacityHigh,
      polygonLineColor: normalizeHexColor(window.localStorage.getItem("ua.preview.polygon.line.color"), "#202426"),
      polygonLineOpacity: loadNumberSetting("ua.preview.polygon.line.opacity", 8, 0, 100),
      polygonSeed: 4147,
      backgroundDynamic: window.localStorage.getItem("ua.preview.background.dynamic") !== "false",
    };
  }

function setSettingsAppearanceStatus(message) {
    appearanceState.settingsAppearanceStatus.textContent = String(message || "");
    appearanceState.settingsAppearanceStatus.hidden = !message;
    appearanceState.settingsAppearanceStatus.classList.toggle("is-error", Boolean(message));
  }

// Saves a person's appearance on the server. Saves run one after another so
// an earlier answer can never overwrite a later choice. Rejects with a
// message for the person when the server refuses.
function putSettingsAppearance(appearance) {
    var request = appearanceState.settingsAppearanceMutationChain
      .catch(function () { return null; })
      .then(function () {
        return requestClient.fetch("/api/web-chat/appearance", {
          method: "PUT",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-UA-Permission-CSRF": sharedState.runtimeControlsCsrfToken,
          },
          body: JSON.stringify({ appearance: window.UAAppearance.saved(appearance) }),
        });
      })
      .then(function (response) {
        if (!response.ok) {
          operations.handleUnauthorizedResponse(response);
          throw new Error("Theme changes could not be saved. Try again.");
        }
        return response.json();
      });
    appearanceState.settingsAppearanceMutationChain = request;
    return request;
  }

// The Color Theme choice applies at once and is saved right away.
function saveSettingsAppearance(next) {
    if (!window.UAAppearance) {
      return;
    }
    var normalized = window.UAAppearance.setUserPreference(next);
    setSettingsAppearanceStatus("");
    putSettingsAppearance(normalized).catch(function (error) {
      setSettingsAppearanceStatus(error.message || "Theme changes could not be saved. Try again.");
    });
  }

// Done in Customize Theme: the edits become the person's preference only
// once the server has saved them; a failure keeps the panel open.
function saveCustomizedAppearance(next) {
    var normalized = window.UAAppearance.normalize(next, window.UAAppearance.baseline());
    return putSettingsAppearance(normalized).then(function () {
      window.UAAppearance.setUserPreference(normalized);
      window.UAAppearance.endPreview();
      setSettingsAppearanceStatus("");
    });
  }

function renderSettingsAppearance() {
    if (!window.UAAppearance || !appearanceState.settingsAppearanceContent) {
      return;
    }
    var next = window.UAAppearance.current();
    appearanceState.settingsAppearanceContent.replaceChildren();

    var themeSection = document.createElement("section");
    themeSection.className = "settings-appearance-mode settings-appearance-summary";
    var themeHeading = document.createElement("h4");
    themeHeading.textContent = "Color Theme";
    var themeOptions = document.createElement("div");
    themeOptions.className = "theme-mode-options settings-theme-mode-options";
    [
      { value: "dark", label: "Dark metallic", copy: "Near-black metal facets" },
      { value: "light", label: "Diffuse light", copy: "Illuminated color mesh" },
    ].forEach(function (definition) {
      var label = document.createElement("label");
      label.className = "theme-mode-option" + (next.mode === definition.value ? " is-active" : "");
      var input = document.createElement("input");
      input.type = "radio";
      input.name = "settings-color-theme";
      input.value = definition.value;
      input.checked = next.mode === definition.value;
      lifecycle.listen(input, "change", function () {
        next.mode = definition.value;
        saveSettingsAppearance(next);
        renderSettingsAppearance();
      });
      var title = document.createElement("strong");
      title.textContent = definition.label;
      var copy = document.createElement("span");
      copy.textContent = definition.copy;
      label.appendChild(input);
      label.appendChild(title);
      label.appendChild(copy);
      themeOptions.appendChild(label);
    });
    themeSection.appendChild(themeHeading);
    themeSection.appendChild(themeOptions);

    var customize = document.createElement("button");
    customize.className = "button primary settings-customize-theme";
    customize.type = "button";
    customize.textContent = "Customize Theme";
    lifecycle.listen(customize, "click", function () {
      if (!window.UAAppearanceCustomizer) {
        return;
      }
      window.UAAppearanceCustomizer.open({
        appearance: window.UAAppearance.current(),
        interfaceElement: appearanceState.chatPreview,
        returnFocus: customize,
        preview: window.UAAppearance.preview,
        save: saveCustomizedAppearance,
        discard: function () { window.UAAppearance.endPreview(); },
        onClose: renderSettingsAppearance,
      });
    });
    themeSection.appendChild(customize);
    appearanceState.settingsAppearanceContent.appendChild(themeSection);
    appearanceState.settingsAppearanceContent.appendChild(renderAnimationSetting(next));
  }

// Interface animation: panels, message effects and the activity mesh. It
// applies and saves at once, like the color theme.
function renderAnimationSetting(next) {
    var section = document.createElement("section");
    section.className = "settings-appearance-mode settings-animation-setting";
    var heading = document.createElement("h4");
    heading.id = "settings-animation-label";
    heading.textContent = "Interface Animation";
    var copy = document.createElement("p");
    copy.className = "settings-animation-copy";
    copy.textContent = "Panels that open with a short fade, message effects and the moving team activity mesh. Your system's reduced-motion setting always turns them off.";
    var toggle = document.createElement("label");
    toggle.className = "toggle";
    var input = document.createElement("input");
    input.type = "checkbox";
    input.setAttribute("role", "switch");
    input.setAttribute("aria-labelledby", "settings-animation-label");
    input.checked = next.animation !== false;
    var knob = document.createElement("span");
    lifecycle.listen(input, "change", function () {
      next.animation = input.checked;
      saveSettingsAppearance(next);
    });
    toggle.appendChild(input);
    toggle.appendChild(knob);
    var row = document.createElement("div");
    row.className = "settings-animation-row";
    row.appendChild(heading);
    row.appendChild(toggle);
    section.appendChild(row);
    section.appendChild(copy);
    return section;
  }

function applyRuntimeAppearance(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }
    if (window.UAAppearance && typeof window.UAAppearance.setBaseline === "function") {
      var sharedAppearance = window.UAAppearance.setBaseline(payload);
      payload = {
        light_colors: sharedAppearance.light.colors,
        light_diffusion: sharedAppearance.light.diffusion,
        polygon_opacity_low: sharedAppearance.light.opacityLow,
        polygon_opacity_high: sharedAppearance.light.opacityHigh,
        polygon_line_color: sharedAppearance.light.lineColor,
        polygon_line_opacity: sharedAppearance.light.lineOpacity,
        polygon_seed: sharedAppearance.light.seed,
        dynamic_background: sharedAppearance.light.dynamic,
      };
    }
    if (Array.isArray(payload.light_colors)) {
      appearanceState.lightDefinitions.forEach(function (light, index) {
        if (payload.light_colors[index]) {
          sharedState.appearance.lightColors[light.key] = normalizeHexColor(payload.light_colors[index], sharedState.appearance.lightColors[light.key]);
        }
      });
    }
    if (typeof payload.light_diffusion === "number") {
      sharedState.appearance.lightDiffusion = clamp(payload.light_diffusion, 0, 100);
    }
    if (typeof payload.polygon_opacity_low === "number") {
      sharedState.appearance.polygonOpacityLow = clamp(payload.polygon_opacity_low, 0, 100);
    }
    if (typeof payload.polygon_opacity_high === "number") {
      sharedState.appearance.polygonOpacityHigh = clamp(payload.polygon_opacity_high, 0, 100);
    }
    if (payload.polygon_line_color) {
      sharedState.appearance.polygonLineColor = normalizeHexColor(payload.polygon_line_color, sharedState.appearance.polygonLineColor);
    }
    if (typeof payload.polygon_line_opacity === "number") {
      sharedState.appearance.polygonLineOpacity = clamp(payload.polygon_line_opacity, 0, 100);
    }
    if (typeof payload.polygon_seed === "number") {
      sharedState.appearance.polygonSeed = clamp(payload.polygon_seed, 1, 4294967295);
    }
    if (typeof payload.dynamic_background === "boolean") {
      sharedState.appearance.backgroundDynamic = payload.dynamic_background;
    }
    appearanceState.lightPaneCacheKey = "";
  }

function backgroundIsDynamic() {
    var lightMode = !window.UAAppearance
      || window.UAAppearance.current().mode === "light";
    return lightMode && sharedState.appearance.backgroundDynamic && !appearanceState.prefersReducedMotion;
  }

function resizeCanvas() {
    var scale = window.devicePixelRatio || 1;
    var width = window.innerWidth;
    var height = window.innerHeight;
    if (width === appearanceState.lastWidth && height === appearanceState.lastHeight) {
      return;
    }
    appearanceState.lastWidth = width;
    appearanceState.lastHeight = height;
    appearanceState.canvas.width = Math.floor(width * scale);
    appearanceState.canvas.height = Math.floor(height * scale);
    appearanceState.canvas.style.width = width + "px";
    appearanceState.canvas.style.height = height + "px";
    appearanceState.ctx.setTransform(scale, 0, 0, scale, 0, 0);
    appearanceState.meshPoints = createMeshPoints(width, height);
  }

function createMeshPoints(width, height) {
    var points = [];
    var seedPhase = Number(sharedState.appearance.polygonSeed || 4147) * 0.00091;
    appearanceState.meshColumns = width < 760 ? 6 : 10;
    appearanceState.meshRows = width < 760 ? 8 : 7;
    for (var row = 0; row <= appearanceState.meshRows; row += 1) {
      for (var column = 0; column <= appearanceState.meshColumns; column += 1) {
        var pinX = column === 0 || column === appearanceState.meshColumns;
        var pinY = row === 0 || row === appearanceState.meshRows;
        points.push({
          x: column * width / appearanceState.meshColumns + (pinX ? 0 : Math.sin(column * 4.1 + row + seedPhase) * 26),
          y: row * height / appearanceState.meshRows + (pinY ? 0 : Math.cos(row * 3.7 + column - seedPhase * 1.7) * 24),
          pinX: pinX,
          pinY: pinY,
          phase: column * 0.7 + row * 0.9 + seedPhase,
        });
      }
    }
    return points;
  }

function draw(time) {
    if (window.UAAppearance && window.UAAppearance.current().mode !== "light") {
      return;
    }
    resizeCanvas();
    drawDiffuseLightPane();
    appearanceState.ctx.save();
    appearanceState.ctx.globalCompositeOperation = "multiply";
    for (var row = 0; row < appearanceState.meshRows; row += 1) {
      for (var column = 0; column < appearanceState.meshColumns; column += 1) {
        var index = row * (appearanceState.meshColumns + 1) + column;
        var a = morphedPoint(appearanceState.meshPoints[index], time);
        var b = morphedPoint(appearanceState.meshPoints[index + 1], time);
        var c = morphedPoint(appearanceState.meshPoints[index + appearanceState.meshColumns + 1], time);
        var d = morphedPoint(appearanceState.meshPoints[index + appearanceState.meshColumns + 2], time);
        drawTriangle(a, b, c, row, column, 0);
        drawTriangle(b, d, c, row, column, 1);
      }
    }
    appearanceState.ctx.restore();
    var lineColor = hexToRgb(sharedState.appearance.polygonLineColor);
    appearanceState.ctx.strokeStyle = "rgba(" + lineColor[0] + ", " + lineColor[1] + ", " + lineColor[2] + ", " + (sharedState.appearance.polygonLineOpacity / 100).toFixed(3) + ")";
    appearanceState.ctx.lineWidth = 1;
    if (sharedState.appearance.polygonLineOpacity > 0) {
      for (var lineRow = 0; lineRow <= appearanceState.meshRows; lineRow += 1) {
        for (var lineColumn = 0; lineColumn <= appearanceState.meshColumns; lineColumn += 1) {
          var current = morphedPoint(appearanceState.meshPoints[lineRow * (appearanceState.meshColumns + 1) + lineColumn], time);
          var right = lineColumn < appearanceState.meshColumns ? morphedPoint(appearanceState.meshPoints[lineRow * (appearanceState.meshColumns + 1) + lineColumn + 1], time) : null;
          var below = lineRow < appearanceState.meshRows ? morphedPoint(appearanceState.meshPoints[(lineRow + 1) * (appearanceState.meshColumns + 1) + lineColumn], time) : null;
          if (right) {
            drawLine(current, right);
          }
          if (below) {
            drawLine(current, below);
          }
        }
      }
    }
    if (backgroundIsDynamic()) {
      lifecycle.frame(draw);
    }
  }

function drawDiffuseLightPane() {
    var colorValues = appearanceState.lightDefinitions.map(function (light) {
      return sharedState.appearance.lightColors[light.key] || light.defaultColor;
    });
    var cacheKey = [
      Math.floor(appearanceState.lastWidth),
      Math.floor(appearanceState.lastHeight),
      sharedState.appearance.lightDiffusion,
      colorValues.join(","),
    ].join("|");
    if (cacheKey !== appearanceState.lightPaneCacheKey) {
      renderLightPane(colorValues);
      appearanceState.lightPaneCacheKey = cacheKey;
    }
    appearanceState.ctx.drawImage(appearanceState.lightPaneCanvas, 0, 0, appearanceState.lastWidth, appearanceState.lastHeight);
  }

function renderLightPane(colorValues) {
    var paneWidth = Math.max(96, Math.min(260, Math.floor(appearanceState.lastWidth / 5)));
    var paneHeight = Math.max(72, Math.min(180, Math.floor(appearanceState.lastHeight / 5)));
    var colors = colorValues.map(hexToRgb);
    var diffusionRatio = sharedState.appearance.lightDiffusion / 100;
    var exponent = 1 + Math.pow(1 - diffusionRatio, 2) * 22;
    appearanceState.lightPaneCanvas.width = paneWidth;
    appearanceState.lightPaneCanvas.height = paneHeight;
    var imageData = appearanceState.lightPaneCtx.createImageData(paneWidth, paneHeight);
    var data = imageData.data;
    for (var y = 0; y < paneHeight; y += 1) {
      var ny = paneHeight <= 1 ? 0 : y / (paneHeight - 1);
      for (var x = 0; x < paneWidth; x += 1) {
        var nx = paneWidth <= 1 ? 0 : x / (paneWidth - 1);
        var output;
        if (sharedState.appearance.lightDiffusion === 0) {
          output = colors[(ny < 0.5 ? 0 : 2) + (nx < 0.5 ? 0 : 1)];
        } else {
          var weights = [
            Math.pow((1 - nx) * (1 - ny), exponent),
            Math.pow(nx * (1 - ny), exponent),
            Math.pow((1 - nx) * ny, exponent),
            Math.pow(nx * ny, exponent),
          ];
          var total = weights[0] + weights[1] + weights[2] + weights[3] || 1;
          output = [0, 0, 0];
          for (var i = 0; i < colors.length; i += 1) {
            output[0] += colors[i][0] * weights[i] / total;
            output[1] += colors[i][1] * weights[i] / total;
            output[2] += colors[i][2] * weights[i] / total;
          }
        }
        var offset = (y * paneWidth + x) * 4;
        data[offset] = Math.round(output[0]);
        data[offset + 1] = Math.round(output[1]);
        data[offset + 2] = Math.round(output[2]);
        data[offset + 3] = 255;
      }
    }
    appearanceState.lightPaneCtx.putImageData(imageData, 0, 0);
  }

function morphedPoint(point, time) {
    var motionTime = appearanceState.polygonMotion.polygonTime(time);
    var drift = backgroundIsDynamic() ? Math.sin(motionTime * 0.00016 + point.phase) * 10 : 0;
    return {
      x: point.pinX ? point.x : point.x + drift,
      y: point.pinY ? point.y : point.y + (backgroundIsDynamic() ? Math.cos(motionTime * 0.00014 + point.phase) * 10 : 0),
    };
  }

function drawTriangle(a, b, c, row, column, variant) {
    var shade = 72 + ((row * 17 + column * 29 + variant * 23) % 132);
    var opacityLow = sharedState.appearance.polygonOpacityLow / 100;
    var opacityHigh = sharedState.appearance.polygonOpacityHigh / 100;
    var tone = ((row * 7 + column * 11 + variant * 5) % 17) / 16;
    var alpha = opacityLow + tone * (opacityHigh - opacityLow);
    appearanceState.ctx.beginPath();
    appearanceState.ctx.moveTo(a.x, a.y);
    appearanceState.ctx.lineTo(b.x, b.y);
    appearanceState.ctx.lineTo(c.x, c.y);
    appearanceState.ctx.closePath();
    appearanceState.ctx.fillStyle = "rgba(" + shade + ", " + shade + ", " + shade + ", " + alpha.toFixed(3) + ")";
    appearanceState.ctx.fill();
  }

function drawLine(a, b) {
    appearanceState.ctx.beginPath();
    appearanceState.ctx.moveTo(a.x, a.y);
    appearanceState.ctx.lineTo(b.x, b.y);
    appearanceState.ctx.stroke();
  }
return { applyRuntimeAppearance, renderAnimationSetting, backgroundIsDynamic, clamp, createMeshPoints, draw, drawDiffuseLightPane, drawLine, drawTriangle, hexToRgb, loadAppearanceSettings, loadNumberSetting, morphedPoint, normalizeHexColor, putSettingsAppearance, renderLightPane, renderSettingsAppearance, resizeCanvas, saveCustomizedAppearance, saveSettingsAppearance, setSettingsAppearanceStatus };
}
