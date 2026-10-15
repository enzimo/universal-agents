/** Existing app appearance view behavior with explicit state and callback dependencies. */
export function createAppearance({ appearanceState, sharedState, operations, lifecycle, requestClient }) {
function clamp(value, min, max) {
    if (Number.isNaN(value)) {
      return min;
    }
    return Math.max(min, Math.min(max, value));
  }

function seededRandom(seed) {
    var value = seed % 2147483647;
    if (value <= 0) {
      value += 2147483646;
    }
    return function () {
      value = (value * 16807) % 2147483647;
      return (value - 1) / 2147483646;
    };
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

function hexToRgbObject(hex) {
    var rgb = hexToRgb(hex);
    return { r: rgb[0], g: rgb[1], b: rgb[2] };
  }

function lightStorageKey(lightKey) {
    return "ua.preview.light." + lightKey;
  }

function loadNumberSetting(key, fallback, min, max) {
    var stored = parseInt(window.localStorage.getItem(key) || "", 10);
    if (Number.isNaN(stored)) {
      return fallback;
    }
    return clamp(stored, min, max);
  }

function loadNumericSetting(key, fallback, min, max) {
    var stored = Number(window.localStorage.getItem(key) || "");
    if (!Number.isFinite(stored) || window.localStorage.getItem(key) === null) {
      return fallback;
    }
    return clamp(stored, min, max);
  }

function loadOptionalNumberSetting(key) {
    var stored = window.localStorage.getItem(key);
    if (stored === null || stored === "") {
      return "";
    }
    var parsed = Number(stored);
    return Number.isFinite(parsed) ? parsed : "";
  }

function applyAppearanceSettings(appearance) {
    var normalizedAppearance = window.UAAppearance
      ? window.UAAppearance.normalize(appearance, window.UAAppearance.defaults())
      : null;
    if (normalizedAppearance) {
      sharedState.state.colorMode = normalizedAppearance.mode;
      sharedState.state.darkAppearance = normalizedAppearance.dark;
      sharedState.state.lightMeshSeed = normalizedAppearance.light.seed;
    }
    if (Array.isArray(appearance.light_colors)) {
      sharedState.lightDefinitions.forEach(function (light, index) {
        if (appearance.light_colors[index]) {
          setLightColor(light.key, appearance.light_colors[index]);
        }
      });
    }
    if (typeof appearance.light_diffusion === "number") {
      sharedState.state.lightDiffusion = clamp(appearance.light_diffusion, 0, 100);
    }
    if (typeof appearance.polygon_opacity_low === "number") {
      sharedState.state.polygonOpacityLow = clamp(appearance.polygon_opacity_low, 0, 100);
    }
    if (typeof appearance.polygon_opacity_high === "number") {
      sharedState.state.polygonOpacityHigh = clamp(appearance.polygon_opacity_high, 0, 100);
    }
    if (appearance.polygon_line_color) {
      sharedState.state.polygonLineColor = normalizeHexColor(appearance.polygon_line_color, sharedState.state.polygonLineColor);
    }
    if (typeof appearance.polygon_line_opacity === "number") {
      sharedState.state.polygonLineOpacity = clamp(appearance.polygon_line_opacity, 0, 100);
    }
    if (typeof appearance.polygon_seed === "number") {
      sharedState.state.lightMeshSeed = clamp(appearance.polygon_seed, 1, 4294967295);
    }
    if (typeof appearance.dynamic_background === "boolean") {
      sharedState.state.backgroundDynamic = appearance.dynamic_background;
    }
    if (sharedState.state.polygonOpacityLow > sharedState.state.polygonOpacityHigh) {
      sharedState.state.polygonOpacityLow = sharedState.state.polygonOpacityHigh;
    }
    applyBrandColor();
    invalidateLightPaneCache();
    syncAppearancePreview();
  }

function applyBrandColor() {
    var rgb = hexToRgb(appearanceState.brandTextColor);
    var color = "rgb(" + rgb[0] + ", " + rgb[1] + ", " + rgb[2] + ")";
    document.documentElement.style.setProperty(
      "--brand-color",
      color
    );
    document.querySelectorAll(".brand-lockup, .brand-lockup span").forEach(function (element) {
      element.style.color = color;
    });
  }

function setLightColor(lightKey, color) {
    var matchingLight = sharedState.lightDefinitions.find(function (light) {
      return light.key === lightKey;
    });
    if (!matchingLight) {
      return;
    }
    var normalized = normalizeHexColor(color, matchingLight.defaultColor);
    sharedState.state.lightColors[lightKey] = normalized;
    window.localStorage.setItem(lightStorageKey(lightKey), normalized);
    syncAppearancePreview();
    redrawStaticBackground();
  }

function backgroundIsDynamic() {
    return sharedState.state.colorMode === "light" && sharedState.state.backgroundDynamic && !appearanceState.prefersReducedMotion;
  }

function syncAppearancePreview() {
    storeThemeChoices();
    showAppearanceState();
  }

// Shows the onboarding appearance state on the page without storing it.
function showAppearanceState() {
    if (window.UAAppearance && typeof window.UAAppearance.setBaseline === "function") {
      window.UAAppearance.setBaseline(currentAppearanceSettings());
    }
  }

// Keeps the theme mode, mesh seed and dark settings in this browser's draft.
function storeThemeChoices() {
    window.localStorage.setItem("ua.preview.color_mode", sharedState.state.colorMode);
    window.localStorage.setItem("ua.preview.polygon.seed", String(sharedState.state.lightMeshSeed));
    window.localStorage.setItem(
      "ua.preview.appearance.dark",
      JSON.stringify(sharedState.state.darkAppearance)
    );
  }

function redrawStaticBackground() {
    if (!backgroundIsDynamic()) {
      draw(performance.now());
    }
  }

function invalidateLightPaneCache() {
    appearanceState.lightPaneCacheKey = "";
    redrawStaticBackground();
  }

function setPolygonOpacityLow(value) {
    sharedState.state.polygonOpacityLow = clamp(parseInt(value, 10), 0, sharedState.state.polygonOpacityHigh);
    window.localStorage.setItem("ua.preview.polygon.opacity.low", String(sharedState.state.polygonOpacityLow));
    syncAppearancePreview();
    redrawStaticBackground();
  }

function setPolygonOpacityHigh(value) {
    sharedState.state.polygonOpacityHigh = clamp(parseInt(value, 10), sharedState.state.polygonOpacityLow, 100);
    window.localStorage.setItem("ua.preview.polygon.opacity.high", String(sharedState.state.polygonOpacityHigh));
    syncAppearancePreview();
    redrawStaticBackground();
  }

function setLightDiffusion(value) {
    sharedState.state.lightDiffusion = clamp(parseInt(value, 10), 0, 100);
    window.localStorage.setItem("ua.preview.light.diffusion", String(sharedState.state.lightDiffusion));
    syncAppearancePreview();
    redrawStaticBackground();
  }

function setPolygonLineColor(color) {
    sharedState.state.polygonLineColor = normalizeHexColor(color, "#202426");
    window.localStorage.setItem("ua.preview.polygon.line.color", sharedState.state.polygonLineColor);
    syncAppearancePreview();
    applyBrandColor();
    redrawStaticBackground();
  }

function setPolygonLineOpacity(value) {
    sharedState.state.polygonLineOpacity = clamp(parseInt(value, 10), 0, 100);
    window.localStorage.setItem("ua.preview.polygon.line.opacity", String(sharedState.state.polygonLineOpacity));
    syncAppearancePreview();
    applyBrandColor();
    redrawStaticBackground();
  }

function setBackgroundDynamic(enabled) {
    sharedState.state.backgroundDynamic = enabled;
    window.localStorage.setItem("ua.preview.background.dynamic", enabled ? "true" : "false");
    syncAppearancePreview();
    if (!enabled && appearanceState.animationFrameId) {
      lifecycle.cancelFrame(appearanceState.animationFrameId);
      appearanceState.animationFrameId = 0;
    }
    draw(performance.now());
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
    appearanceState.polyTriangles = createPolyTriangles(width, height);
  }

function createMeshPoints(width, height) {
    var random = seededRandom(sharedState.state.lightMeshSeed);
    var points = [];
    var count = width < 700 ? 46 : 82;
    for (var i = 0; i < count; i += 1) {
      points.push({
        x: random() * width,
        y: random() * height,
        drift: 0.4 + random() * 1.5,
        phase: random() * Math.PI * 2,
      });
    }
    return points;
  }

function createPolyTriangles(width, height) {
    var random = seededRandom((sharedState.state.lightMeshSeed ^ 0x5f3759df) >>> 0);
    var cols = width < 700 ? 7 : 13;
    var rows = height < 700 ? 7 : 10;
    var cellWidth = width / cols;
    var cellHeight = height / rows;
    var grid = [];
    for (var row = 0; row <= rows; row += 1) {
      var line = [];
      for (var col = 0; col <= cols; col += 1) {
        var edgeX = col === 0 || col === cols;
        var edgeY = row === 0 || row === rows;
        line.push({
          x: col * cellWidth + (edgeX ? 0 : (random() - 0.5) * cellWidth * 0.72),
          y: row * cellHeight + (edgeY ? 0 : (random() - 0.5) * cellHeight * 0.72),
          pinX: edgeX,
          pinY: edgeY,
          phase: random() * Math.PI * 2,
          speed: 0.00009 + random() * 0.00008,
          morph: (edgeX || edgeY ? 0.10 : 0.34) * Math.min(cellWidth, cellHeight),
        });
      }
      grid.push(line);
    }

    var triangles = [];
    for (var y = 0; y < rows; y += 1) {
      for (var x = 0; x < cols; x += 1) {
        var a = grid[y][x];
        var b = grid[y][x + 1];
        var c = grid[y + 1][x];
        var d = grid[y + 1][x + 1];
        var flip = random() > 0.5;
        var tone = random();
        if (flip) {
          triangles.push({ points: [a, b, d], tone: tone });
          triangles.push({ points: [a, d, c], tone: random() });
        } else {
          triangles.push({ points: [a, b, c], tone: tone });
          triangles.push({ points: [b, d, c], tone: random() });
        }
      }
    }
    return triangles;
  }

function completionRatio() {
    return sharedState.state.furthestPageIndex / Math.max(1, sharedState.pages.length - 1);
  }

function morphedPoint(point, time) {
    if (!backgroundIsDynamic()) {
      return point;
    }
    var motionTime = appearanceState.polygonMotion.polygonTime(time);
    return {
      x: point.pinX ? point.x : point.x + Math.sin(motionTime * point.speed + point.phase) * point.morph,
      y: point.pinY ? point.y : point.y + Math.cos(motionTime * point.speed * 0.82 + point.phase * 1.4) * point.morph,
    };
  }

function drawDiffuseLightPane(time) {
    var colorValues = sharedState.lightDefinitions.map(function (light) {
      return sharedState.state.lightColors[light.key] || light.defaultColor;
    });
    var cacheKey = [
      Math.floor(appearanceState.lastWidth),
      Math.floor(appearanceState.lastHeight),
      sharedState.state.lightDiffusion,
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
    var diffusionRatio = sharedState.state.lightDiffusion / 100;
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
        if (sharedState.state.lightDiffusion === 0) {
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

function drawLowPolyBackground(time, ratio) {
    drawDiffuseLightPane(time);

    var shimmer = backgroundIsDynamic() ? Math.sin(appearanceState.polygonMotion.polygonTime(time) * 0.00018) * 0.035 : 0;
    appearanceState.ctx.save();
    appearanceState.ctx.globalCompositeOperation = "multiply";
    appearanceState.polyTriangles.forEach(function (triangle, index) {
      var morphed = triangle.points.map(function (point) {
        return morphedPoint(point, time);
      });
      var shade = Math.floor(86 + triangle.tone * 116);
      var opacityLow = sharedState.state.polygonOpacityLow / 100;
      var opacityHigh = sharedState.state.polygonOpacityHigh / 100;
      var alpha = clamp(opacityLow + (1 - triangle.tone) * (opacityHigh - opacityLow) + shimmer, opacityLow, opacityHigh);
      appearanceState.ctx.beginPath();
      morphed.forEach(function (point, pointIndex) {
        if (pointIndex === 0) {
          appearanceState.ctx.moveTo(point.x, point.y);
        } else {
          appearanceState.ctx.lineTo(point.x, point.y);
        }
      });
      appearanceState.ctx.closePath();
      appearanceState.ctx.fillStyle = "rgba(" + shade + ", " + shade + ", " + shade + ", " + alpha.toFixed(3) + ")";
      appearanceState.ctx.fill();
    });
    appearanceState.ctx.restore();

    if (sharedState.state.polygonLineOpacity <= 0) {
      return;
    }
    var lineColor = hexToRgb(sharedState.state.polygonLineColor);
    var lineAlpha = sharedState.state.polygonLineOpacity / 100;
    appearanceState.ctx.save();
    appearanceState.ctx.globalCompositeOperation = "source-over";
    appearanceState.ctx.strokeStyle = "rgba(" + lineColor[0] + ", " + lineColor[1] + ", " + lineColor[2] + ", " + lineAlpha.toFixed(3) + ")";
    appearanceState.ctx.lineWidth = 1;
    appearanceState.polyTriangles.forEach(function (triangle) {
      var morphed = triangle.points.map(function (point) {
        return morphedPoint(point, time);
      });
      appearanceState.ctx.beginPath();
      morphed.forEach(function (point, pointIndex) {
        if (pointIndex === 0) {
          appearanceState.ctx.moveTo(point.x, point.y);
        } else {
          appearanceState.ctx.lineTo(point.x, point.y);
        }
      });
      appearanceState.ctx.closePath();
      appearanceState.ctx.stroke();
    });
    appearanceState.ctx.restore();
  }

function drawNetwork(time, ratio) {
    var width = appearanceState.lastWidth;
    var height = appearanceState.lastHeight;
    var maxDistance = width < 700 ? 126 : 156;
    var driftRange = backgroundIsDynamic() ? 10 - ratio * 4 : 0;
    appearanceState.ctx.lineWidth = 1;
    for (var i = 0; i < appearanceState.meshPoints.length; i += 1) {
      var p = appearanceState.meshPoints[i];
      var px = p.x + Math.sin(time * 0.00025 * p.drift + p.phase) * driftRange;
      var py = p.y + Math.cos(time * 0.00022 * p.drift + p.phase) * driftRange;
      for (var j = i + 1; j < appearanceState.meshPoints.length; j += 1) {
        var q = appearanceState.meshPoints[j];
        var qx = q.x + Math.sin(time * 0.00025 * q.drift + q.phase) * driftRange;
        var qy = q.y + Math.cos(time * 0.00022 * q.drift + q.phase) * driftRange;
        var dx = px - qx;
        var dy = py - qy;
        var distance = Math.sqrt(dx * dx + dy * dy);
        if (distance < maxDistance) {
          var alpha = (1 - distance / maxDistance) * (0.055 + ratio * 0.10);
          appearanceState.ctx.strokeStyle = "rgba(211, 255, 247, " + alpha.toFixed(3) + ")";
          appearanceState.ctx.beginPath();
          appearanceState.ctx.moveTo(px, py);
          appearanceState.ctx.lineTo(qx, qy);
          appearanceState.ctx.stroke();
        }
      }
    }
  }

function drawRuntimeNodes(cx, cy, radius, ratio) {
    var page = sharedState.pages[sharedState.state.pageIndex];
    if (page.id !== "status" && page.id !== "standby") {
      return;
    }
    var liveAgents = sharedState.agents.filter(function (agent) {
      return agent[2] || sharedState.state.selectedAgents[agent[0]];
    });
    if (liveAgents.length < 2) {
      liveAgents = sharedState.agents.slice(0, 4);
    }
    var center = [cx, cy];
    var positions = [center];
    for (var i = 1; i < liveAgents.length; i += 1) {
      var angle = -Math.PI / 2 + (i - 1) * Math.PI * 2 / Math.max(1, liveAgents.length - 1);
      positions.push([cx + Math.cos(angle) * radius * 1.07, cy + Math.sin(angle) * radius * 1.07]);
    }
    appearanceState.ctx.save();
    appearanceState.ctx.lineWidth = 2;
    for (var edge = 1; edge < positions.length; edge += 1) {
      appearanceState.ctx.strokeStyle = "rgba(22, 163, 74, " + (0.18 + ratio * 0.32).toFixed(3) + ")";
      appearanceState.ctx.beginPath();
      appearanceState.ctx.moveTo(center[0], center[1]);
      appearanceState.ctx.lineTo(positions[edge][0], positions[edge][1]);
      appearanceState.ctx.stroke();
    }
    positions.forEach(function (position, index) {
      var waiting = page.id === "standby" && index > 1;
      appearanceState.ctx.beginPath();
      appearanceState.ctx.arc(position[0], position[1], index === 0 ? 8 : 6, 0, Math.PI * 2);
      appearanceState.ctx.fillStyle = waiting ? "rgba(183, 121, 31, 0.88)" : "rgba(22, 163, 74, 0.92)";
      appearanceState.ctx.fill();
      appearanceState.ctx.beginPath();
      appearanceState.ctx.arc(position[0], position[1], index === 0 ? 15 : 12, 0, Math.PI * 2);
      appearanceState.ctx.strokeStyle = waiting ? "rgba(183, 121, 31, 0.22)" : "rgba(22, 163, 74, 0.24)";
      appearanceState.ctx.stroke();
    });
    appearanceState.ctx.restore();
  }

function draw(time) {
    appearanceState.animationFrameId = 0;
    if (sharedState.state.colorMode !== "light") {
      return;
    }
    resizeCanvas();
    var ratio = completionRatio();
    if (sharedState.pages[sharedState.state.pageIndex].id === "review") {
      ratio = Math.max(ratio, 0.88);
    }
    if (sharedState.pages[sharedState.state.pageIndex].id === "standby" || sharedState.pages[sharedState.state.pageIndex].id === "status") {
      ratio = 1;
    }

    appearanceState.ctx.clearRect(0, 0, appearanceState.lastWidth, appearanceState.lastHeight);
    drawLowPolyBackground(time, ratio);
    drawNetwork(time, ratio);
    var cx = appearanceState.lastWidth * (appearanceState.lastWidth > 900 ? 0.66 : 0.50);
    var cy = appearanceState.lastHeight * (appearanceState.lastWidth > 900 ? 0.48 : 0.46);
    var radius = Math.min(appearanceState.lastWidth, appearanceState.lastHeight) * (appearanceState.lastWidth > 900 ? 0.19 : 0.25);
    drawRuntimeNodes(cx, cy, radius, ratio);

    if (backgroundIsDynamic()) {
      appearanceState.animationFrameId = lifecycle.frame(draw);
    }
  }

function updateOpacityTrack(track, value) {
    track.style.setProperty("--range-low", sharedState.state.polygonOpacityLow + "%");
    track.style.setProperty("--range-high", sharedState.state.polygonOpacityHigh + "%");
    value.textContent = sharedState.state.polygonOpacityLow + "-" + sharedState.state.polygonOpacityHigh + "%";
  }

function renderOpacityControl() {
    var control = document.createElement("div");
    control.className = "appearance-control";
    var header = document.createElement("div");
    header.className = "appearance-control-header";
    var title = document.createElement("strong");
    title.textContent = "Polygon transparency";
    var value = document.createElement("span");
    header.appendChild(title);
    header.appendChild(value);

    var track = document.createElement("div");
    track.className = "dual-range";
    var lowInput = document.createElement("input");
    lowInput.type = "range";
    lowInput.className = "range-input low";
    lowInput.min = "0";
    lowInput.max = "100";
    lowInput.value = String(sharedState.state.polygonOpacityLow);
    lowInput.setAttribute("aria-label", "Minimum polygon opacity");
    var highInput = document.createElement("input");
    highInput.type = "range";
    highInput.className = "range-input high";
    highInput.min = "0";
    highInput.max = "100";
    highInput.value = String(sharedState.state.polygonOpacityHigh);
    highInput.setAttribute("aria-label", "Maximum polygon opacity");

    lifecycle.listen(lowInput, "input", function () {
      setPolygonOpacityLow(lowInput.value);
      lowInput.value = String(sharedState.state.polygonOpacityLow);
      updateOpacityTrack(track, value);
    });
    lifecycle.listen(highInput, "input", function () {
      setPolygonOpacityHigh(highInput.value);
      highInput.value = String(sharedState.state.polygonOpacityHigh);
      updateOpacityTrack(track, value);
    });

    track.appendChild(lowInput);
    track.appendChild(highInput);
    control.appendChild(header);
    control.appendChild(track);
    updateOpacityTrack(track, value);
    return control;
  }

function renderDiffusionControl() {
    var control = document.createElement("div");
    control.className = "appearance-control";
    var header = document.createElement("div");
    header.className = "appearance-control-header";
    var title = document.createElement("strong");
    title.textContent = "Light diffusion";
    var value = document.createElement("span");
    value.textContent = sharedState.state.lightDiffusion + "%";
    header.appendChild(title);
    header.appendChild(value);

    var input = document.createElement("input");
    input.type = "range";
    input.className = "single-range";
    input.min = "0";
    input.max = "100";
    input.value = String(sharedState.state.lightDiffusion);
    input.setAttribute("aria-label", "Light diffusion");
    lifecycle.listen(input, "input", function () {
      setLightDiffusion(input.value);
      value.textContent = sharedState.state.lightDiffusion + "%";
    });

    control.appendChild(header);
    control.appendChild(input);
    return control;
  }

function renderLineControl() {
    var control = document.createElement("div");
    control.className = "appearance-control line-appearance-control";
    var header = document.createElement("div");
    header.className = "appearance-control-header";
    var title = document.createElement("strong");
    title.textContent = "Polygon lines";
    var value = document.createElement("span");
    value.textContent = sharedState.state.polygonLineOpacity + "%";
    header.appendChild(title);
    header.appendChild(value);

    var body = document.createElement("div");
    body.className = "line-control-body";
    var colorInput = document.createElement("input");
    colorInput.type = "color";
    colorInput.className = "light-input line-color-input";
    colorInput.value = sharedState.state.polygonLineColor;
    colorInput.setAttribute("aria-label", "Polygon line color");
    function syncLineColor() {
      setPolygonLineColor(colorInput.value);
    }
    lifecycle.listen(colorInput, "input", syncLineColor);
    lifecycle.listen(colorInput, "change", syncLineColor);
    var opacityInput = document.createElement("input");
    opacityInput.type = "range";
    opacityInput.className = "single-range";
    opacityInput.min = "0";
    opacityInput.max = "100";
    opacityInput.value = String(sharedState.state.polygonLineOpacity);
    opacityInput.setAttribute("aria-label", "Polygon line opacity");
    lifecycle.listen(opacityInput, "input", function () {
      setPolygonLineOpacity(opacityInput.value);
      value.textContent = sharedState.state.polygonLineOpacity + "%";
    });
    body.appendChild(colorInput);
    body.appendChild(opacityInput);
    control.appendChild(header);
    control.appendChild(body);
    return control;
  }

function renderDynamicControl() {
    var control = document.createElement("label");
    control.className = "appearance-control toggle-control";
    var header = document.createElement("span");
    header.className = "appearance-control-header";
    var title = document.createElement("strong");
    title.textContent = "Dynamic background";
    var value = document.createElement("span");
    value.textContent = sharedState.state.backgroundDynamic ? "On" : "Off";
    header.appendChild(title);
    header.appendChild(value);

    var toggle = document.createElement("span");
    toggle.className = "toggle";
    var input = document.createElement("input");
    input.type = "checkbox";
    input.checked = sharedState.state.backgroundDynamic;
    lifecycle.listen(input, "change", function () {
      setBackgroundDynamic(input.checked);
      value.textContent = sharedState.state.backgroundDynamic ? "On" : "Off";
    });
    var knob = document.createElement("span");
    toggle.appendChild(input);
    toggle.appendChild(knob);
    control.appendChild(header);
    control.appendChild(toggle);
    return control;
  }

function currentAppearanceSettings() {
    var dark = sharedState.state.darkAppearance;
    return {
      color_mode: sharedState.state.colorMode,
      light_colors: sharedState.lightDefinitions.map(function (light) {
        return normalizeHexColor(sharedState.state.lightColors[light.key], light.defaultColor).toLowerCase();
      }),
      light_diffusion: sharedState.state.lightDiffusion,
      polygon_opacity_low: sharedState.state.polygonOpacityLow,
      polygon_opacity_high: sharedState.state.polygonOpacityHigh,
      polygon_line_color: normalizeHexColor(sharedState.state.polygonLineColor, "#202426").toLowerCase(),
      polygon_line_opacity: sharedState.state.polygonLineOpacity,
      polygon_seed: sharedState.state.lightMeshSeed,
      dynamic_background: sharedState.state.backgroundDynamic,
      dark: window.UAAppearance.savedDark(dark),
    };
  }

function setColorMode(mode) {
    sharedState.state.colorMode = mode === "light" ? "light" : "dark";
    syncAppearancePreview();
    if (sharedState.state.colorMode === "light") {
      appearanceState.lastWidth = 0;
      appearanceState.lastHeight = 0;
      draw(performance.now());
    } else if (appearanceState.animationFrameId) {
      lifecycle.cancelFrame(appearanceState.animationFrameId);
      appearanceState.animationFrameId = 0;
    }
    operations.renderCurrentPage();
  }

function renderThemeModeControl() {
    var fieldset = document.createElement("fieldset");
    fieldset.className = "theme-mode-control";
    var legend = document.createElement("legend");
    legend.textContent = "Color Theme";
    fieldset.appendChild(legend);
    var options = document.createElement("div");
    options.className = "theme-mode-options";
    ["dark", "light"].forEach(function (mode) {
      var label = document.createElement("label");
      label.className = "theme-mode-option" + (sharedState.state.colorMode === mode ? " is-active" : "");
      var input = document.createElement("input");
      input.type = "radio";
      input.name = "onboarding-color-mode";
      input.value = mode;
      input.checked = sharedState.state.colorMode === mode;
      lifecycle.listen(input, "change", function () {
        setColorMode(mode);
      });
      var name = document.createElement("strong");
      name.textContent = mode === "dark" ? "Dark metallic" : "Diffuse light";
      var note = document.createElement("span");
      note.textContent = mode === "dark"
        ? "Polished metal plates in a dark studio"
        : "Four-color illuminated polygon mesh";
      label.appendChild(input);
      label.appendChild(name);
      label.appendChild(note);
      options.appendChild(label);
    });
    fieldset.appendChild(options);
    return fieldset;
  }

// Customize Theme shows each edit with previewCustomizerAppearance, which
// changes only the page; Done stores it with saveCustomizerAppearance, and
// Cancel shows the opening appearance again through the preview.
function previewCustomizerAppearance(candidate) {
    if (!window.UAAppearance) {
      return;
    }
    var next = window.UAAppearance.normalize(
      candidate,
      window.UAAppearance.defaults()
    );
    sharedState.state.colorMode = next.mode;
    sharedState.state.darkAppearance = next.dark;
    sharedState.state.lightMeshSeed = next.light.seed;
    sharedState.lightDefinitions.forEach(function (light, index) {
      sharedState.state.lightColors[light.key] = next.light.colors[index];
    });
    sharedState.state.lightDiffusion = next.light.diffusion;
    sharedState.state.polygonOpacityLow = next.light.opacityLow;
    sharedState.state.polygonOpacityHigh = next.light.opacityHigh;
    sharedState.state.polygonLineColor = next.light.lineColor;
    sharedState.state.polygonLineOpacity = next.light.lineOpacity;
    sharedState.state.backgroundDynamic = next.light.dynamic;
    appearanceState.lightPaneCacheKey = "";
    appearanceState.lastWidth = 0;
    appearanceState.lastHeight = 0;
    showAppearanceState();
    if (sharedState.state.colorMode === "light") {
      lifecycle.frame(draw);
    }
  }

function saveCustomizerAppearance(candidate) {
    previewCustomizerAppearance(candidate);
    sharedState.lightDefinitions.forEach(function (light) {
      window.localStorage.setItem(lightStorageKey(light.key), sharedState.state.lightColors[light.key]);
    });
    window.localStorage.setItem("ua.preview.light.diffusion", String(sharedState.state.lightDiffusion));
    window.localStorage.setItem("ua.preview.polygon.opacity.low", String(sharedState.state.polygonOpacityLow));
    window.localStorage.setItem("ua.preview.polygon.opacity.high", String(sharedState.state.polygonOpacityHigh));
    window.localStorage.setItem("ua.preview.polygon.line.color", sharedState.state.polygonLineColor);
    window.localStorage.setItem("ua.preview.polygon.line.opacity", String(sharedState.state.polygonLineOpacity));
    window.localStorage.setItem("ua.preview.background.dynamic", sharedState.state.backgroundDynamic ? "true" : "false");
    storeThemeChoices();
  }
return { applyAppearanceSettings, applyBrandColor, backgroundIsDynamic, clamp, completionRatio, createMeshPoints, createPolyTriangles, currentAppearanceSettings, draw, drawDiffuseLightPane, drawLowPolyBackground, drawNetwork, drawRuntimeNodes, hexToRgb, hexToRgbObject, invalidateLightPaneCache, lightStorageKey, loadNumberSetting, loadNumericSetting, loadOptionalNumberSetting, morphedPoint, normalizeHexColor, previewCustomizerAppearance, redrawStaticBackground, renderDiffusionControl, renderDynamicControl, renderLightPane, renderLineControl, renderOpacityControl, renderThemeModeControl, resizeCanvas, saveCustomizerAppearance, seededRandom, setBackgroundDynamic, setColorMode, setLightColor, setLightDiffusion, setPolygonLineColor, setPolygonLineOpacity, setPolygonOpacityHigh, setPolygonOpacityLow, syncAppearancePreview, updateOpacityTrack };
}
