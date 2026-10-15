(function () {
  "use strict";

  if (window.UA_DOC_STATIC_MESH && typeof window.UA_DOC_STATIC_MESH.draw === "function") {
    window.UA_DOC_STATIC_MESH.draw();
    return;
  }

  var canvas = document.querySelector("canvas.ua-doc-static-mesh") || document.createElement("canvas");
  var ctx = canvas.getContext("2d");
  var lightCanvas = document.createElement("canvas");
  var lightCtx = lightCanvas.getContext("2d");
  var triangles = [];
  var lastWidth = 0;
  var lastHeight = 0;
  var lastSeed = 0;
  var lightCacheKey = "";
  var renderedKey = "";

  if (!ctx || !lightCtx) {
    return;
  }

  canvas.className = "ua-doc-static-mesh";
  canvas.setAttribute("aria-hidden", "true");
  canvas.setAttribute("data-ua-metallic-mesh", "");
  document.documentElement.classList.add("ua-doc-mesh-ready");

  function insertCanvas() {
    if (document.body && canvas.parentElement !== document.body) {
      document.body.insertBefore(canvas, document.body.firstChild);
    }
  }

  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function cssNumber(name, fallback) {
    var parsed = parseFloat(cssVar(name, String(fallback)));
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function normalizeAlpha(value, fallback) {
    var parsed = Number.isFinite(value) ? value : fallback;
    return parsed > 1 ? Math.max(0, Math.min(1, parsed / 100)) : Math.max(0, Math.min(1, parsed));
  }

  function hexToRgb(hex) {
    var value = String(hex || "").trim();
    if (!/^#[0-9a-f]{6}$/i.test(value)) {
      value = "#202426";
    }
    return [
      parseInt(value.slice(1, 3), 16),
      parseInt(value.slice(3, 5), 16),
      parseInt(value.slice(5, 7), 16),
    ];
  }

  function seededRandom(seed) {
    var value = seed >>> 0;
    return function () {
      value = (value * 1664525 + 1013904223) >>> 0;
      return value / 4294967296;
    };
  }

  function createTriangles(width, height, seed) {
    var random = seededRandom((seed ^ 0x5f3759df) >>> 0);
    var cols = width < 700 ? 7 : 13;
    var rows = height < 700 ? 7 : 10;
    var cellWidth = width / cols;
    var cellHeight = height / rows;
    var grid = [];
    var y;
    var x;

    for (y = 0; y <= rows; y += 1) {
      var line = [];
      for (x = 0; x <= cols; x += 1) {
        var edgeX = x === 0 || x === cols;
        var edgeY = y === 0 || y === rows;
        line.push({
          x: x * cellWidth + (edgeX ? 0 : (random() - 0.5) * cellWidth * 0.72),
          y: y * cellHeight + (edgeY ? 0 : (random() - 0.5) * cellHeight * 0.72),
        });
      }
      grid.push(line);
    }

    var nextTriangles = [];
    for (y = 0; y < rows; y += 1) {
      for (x = 0; x < cols; x += 1) {
        var a = grid[y][x];
        var b = grid[y][x + 1];
        var c = grid[y + 1][x];
        var d = grid[y + 1][x + 1];
        if (random() > 0.5) {
          nextTriangles.push({ points: [a, b, d], tone: random() });
          nextTriangles.push({ points: [a, d, c], tone: random() });
        } else {
          nextTriangles.push({ points: [a, b, c], tone: random() });
          nextTriangles.push({ points: [b, d, c], tone: random() });
        }
      }
    }
    return nextTriangles;
  }

  function renderLightPane(colors, diffusion, width, height) {
    var paneWidth = Math.max(96, Math.min(260, Math.floor(width / 5)));
    var paneHeight = Math.max(72, Math.min(180, Math.floor(height / 5)));
    var rgb = colors.map(hexToRgb);
    var diffusionRatio = Math.max(0, Math.min(100, diffusion)) / 100;
    var exponent = 1 + Math.pow(1 - diffusionRatio, 2) * 22;
    var imageData;
    var data;

    lightCanvas.width = paneWidth;
    lightCanvas.height = paneHeight;
    imageData = lightCtx.createImageData(paneWidth, paneHeight);
    data = imageData.data;

    for (var y = 0; y < paneHeight; y += 1) {
      var ny = paneHeight <= 1 ? 0 : y / (paneHeight - 1);
      for (var x = 0; x < paneWidth; x += 1) {
        var nx = paneWidth <= 1 ? 0 : x / (paneWidth - 1);
        var output;
        if (diffusion <= 0) {
          output = rgb[(ny < 0.5 ? 0 : 2) + (nx < 0.5 ? 0 : 1)];
        } else {
          var weights = [
            Math.pow((1 - nx) * (1 - ny), exponent),
            Math.pow(nx * (1 - ny), exponent),
            Math.pow((1 - nx) * ny, exponent),
            Math.pow(nx * ny, exponent),
          ];
          var total = weights[0] + weights[1] + weights[2] + weights[3] || 1;
          output = [0, 0, 0];
          for (var i = 0; i < rgb.length; i += 1) {
            output[0] += rgb[i][0] * weights[i] / total;
            output[1] += rgb[i][1] * weights[i] / total;
            output[2] += rgb[i][2] * weights[i] / total;
          }
        }
        var offset = (y * paneWidth + x) * 4;
        data[offset] = Math.round(output[0]);
        data[offset + 1] = Math.round(output[1]);
        data[offset + 2] = Math.round(output[2]);
        data[offset + 3] = 255;
      }
    }
    lightCtx.putImageData(imageData, 0, 0);
  }

  function draw() {
    insertCanvas();
    if (window.UAAppearance && window.UAAppearance.current().mode === "dark") {
      renderedKey = "";
      return;
    }
    var width = window.innerWidth || document.documentElement.clientWidth || 1024;
    var height = window.innerHeight || document.documentElement.clientHeight || 768;
    var scale = window.devicePixelRatio || 1;
    var colors = [
      cssVar("--ua-doc-light-blue", "#082f73"),
      cssVar("--ua-doc-light-yellow", "#ffd60a"),
      cssVar("--ua-doc-light-green", "#20f05a"),
      cssVar("--ua-doc-light-red", "#ff3030"),
    ];
    var diffusion = cssNumber("--ua-doc-light-diffusion", 58);
    var opacityLow = normalizeAlpha(cssNumber("--ua-doc-polygon-opacity-low", 0.2), 0.2);
    var opacityHigh = normalizeAlpha(cssNumber("--ua-doc-polygon-opacity-high", 0.7), 0.7);
    var lineOpacity = normalizeAlpha(cssNumber("--ua-doc-polygon-line-opacity", 0.08), 0.08);
    var meshSeed = Math.max(1, Math.floor(cssNumber("--ua-light-mesh-seed", 4147)));
    var lineColor = hexToRgb(cssVar("--ua-doc-polygon-line-color", "#202426"));
    var lightKey = [width, height, diffusion, colors.join(",")].join("|");
    var drawKey = [
      width,
      height,
      scale,
      diffusion,
      opacityLow,
      opacityHigh,
      lineOpacity,
      lineColor.join(","),
      meshSeed,
      colors.join(","),
    ].join("|");

    if (drawKey === renderedKey && canvas.width > 0 && canvas.height > 0) {
      return;
    }

    if (width !== lastWidth || height !== lastHeight || meshSeed !== lastSeed) {
      lastWidth = width;
      lastHeight = height;
      lastSeed = meshSeed;
      triangles = createTriangles(width, height, meshSeed);
      canvas.width = Math.floor(width * scale);
      canvas.height = Math.floor(height * scale);
      canvas.style.width = width + "px";
      canvas.style.height = height + "px";
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
      lightCacheKey = "";
    }

    if (lightKey !== lightCacheKey) {
      renderLightPane(colors, diffusion, width, height);
      lightCacheKey = lightKey;
    }

    ctx.clearRect(0, 0, width, height);
    ctx.drawImage(lightCanvas, 0, 0, width, height);

    ctx.save();
    ctx.globalCompositeOperation = "multiply";
    triangles.forEach(function (triangle) {
      var shade = Math.floor(86 + triangle.tone * 116);
      var alpha = opacityLow + (1 - triangle.tone) * (opacityHigh - opacityLow);
      ctx.beginPath();
      triangle.points.forEach(function (point, index) {
        if (index === 0) {
          ctx.moveTo(point.x, point.y);
        } else {
          ctx.lineTo(point.x, point.y);
        }
      });
      ctx.closePath();
      ctx.fillStyle = "rgba(" + shade + ", " + shade + ", " + shade + ", " + alpha.toFixed(3) + ")";
      ctx.fill();
    });
    ctx.restore();

    if (lineOpacity > 0) {
      ctx.save();
      ctx.strokeStyle = "rgba(" + lineColor[0] + ", " + lineColor[1] + ", " + lineColor[2] + ", " + lineOpacity.toFixed(3) + ")";
      ctx.lineWidth = 1;
      triangles.forEach(function (triangle) {
        ctx.beginPath();
        triangle.points.forEach(function (point, index) {
          if (index === 0) {
            ctx.moveTo(point.x, point.y);
          } else {
            ctx.lineTo(point.x, point.y);
          }
        });
        ctx.closePath();
        ctx.stroke();
      });
      ctx.restore();
    }
    renderedKey = drawKey;
  }

  window.UA_DOC_STATIC_MESH = { draw: draw };
  window.addEventListener("resize", draw);
  document.addEventListener("ua:appearance-change", function (event) {
    var next = event.detail && event.detail.appearance;
    if (!next || next.mode !== "light") {
      // The metallic renderer takes ownership of the shared canvas in dark mode.
      renderedKey = "";
      return;
    }
    draw();
  });
  // The next script initializes the metallic renderer synchronously. Make its
  // canvas available even while the HTML parser is still loading the document.
  insertCanvas();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", draw);
  } else {
    draw();
  }
})();
