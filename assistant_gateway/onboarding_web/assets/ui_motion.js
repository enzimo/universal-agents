(function () {
  "use strict";

  // Interface motion shared by Web Chat and onboarding: whether motion is
  // allowed, a small drifting triangle mesh drawn on a 2D canvas
  // (UAMeshGlyph), and the one-time arrival effects of new chat messages.
  //
  // Motion is meant to feel alive but never jarring: slow, low-contrast and,
  // where possible, played once. It stops entirely when the person's system
  // asks for reduced motion or when they turn Interface animation off in
  // Appearance (appearance.js sets data-ui-motion="off" on the root).

  if (window.UAMotion) return;

  var reducedMotion = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : { matches: false, addEventListener: function () {} };

  function enabled() {
    return !reducedMotion.matches
      && document.documentElement.getAttribute("data-ui-motion") !== "off";
  }

  // The accent color for motion (lines, brackets, sweeps, meshes), which
  // appearance.js derives from the theme. Returned as [r, g, b].
  var FALLBACK_ACCENT = [240, 163, 90];

  function accent() {
    var raw = window.getComputedStyle(document.documentElement)
      .getPropertyValue("--ua-motion-accent").trim().toLowerCase();
    if (!/^#[0-9a-f]{6}$/.test(raw)) return FALLBACK_ACCENT.slice();
    return [1, 3, 5].map(function (offset) { return parseInt(raw.slice(offset, offset + 2), 16); });
  }

  function hexRgb(hex) {
    return [1, 3, 5].map(function (offset) { return parseInt(hex.slice(offset, offset + 2), 16); });
  }

  // The person's theme colors for small meshes (UAAppearance.motionPalette),
  // as [{ rgb, weight }]; the accent alone when the palette is unavailable.
  function palette() {
    var source = window.UAAppearance && typeof window.UAAppearance.motionPalette === "function"
      ? window.UAAppearance.motionPalette()
      : [];
    var colors = source.filter(function (entry) {
      return entry && /^#[0-9a-f]{6}$/i.test(entry.color) && entry.weight > 0;
    }).map(function (entry) { return { rgb: hexRgb(entry.color), weight: entry.weight }; });
    return colors.length ? colors : [{ rgb: accent(), weight: 1 }];
  }

  // A color from the palette in proportion to the weights, for `roll` in 0..1.
  function pick(colors, roll) {
    var total = colors.reduce(function (sum, entry) { return sum + entry.weight; }, 0);
    var target = roll * total;
    for (var index = 0; index < colors.length; index += 1) {
      target -= colors[index].weight;
      if (target < 0) return colors[index].rgb;
    }
    return colors[colors.length - 1].rgb;
  }

  function rgba(rgb, alpha) {
    return "rgba(" + rgb[0] + ", " + rgb[1] + ", " + rgb[2] + ", " + Math.max(0, Math.min(1, alpha)).toFixed(3) + ")";
  }

  function mulberry32(seed) {
    var state = seed >>> 0;
    return function () {
      state = (state + 0x6d2b79f5) >>> 0;
      var t = state;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // ---------- triangle mesh glyph ----------
  //
  // A grid of points, nudged off the grid by a seeded amount, with each cell
  // split into two triangles along alternating diagonals. Edge points stay on
  // the canvas border so the mesh always fills its box. Interior points sway
  // on their own slow rhythms; `motion` (0..1) scales the sway, so a caller
  // can let the mesh slow down and stop.
  function createMeshGlyph(canvas, options) {
    var settings = Object.assign({ columns: 8, rows: 2, seed: 1, jitter: 0.32, drift: 4 }, options || {});
    var context = canvas.getContext("2d");
    var width = 0;
    var height = 0;
    var points = [];
    var triangles = [];

    // `options.size` ({ width, height } in CSS pixels) lays out an offscreen
    // canvas that has no box of its own on the page.
    function layout() {
      var rect = settings.size || canvas.getBoundingClientRect();
      var dpr = Math.min(2, window.devicePixelRatio || 1);
      width = Math.max(1, rect.width);
      height = Math.max(1, rect.height);
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      var random = mulberry32(settings.seed);
      var cellW = width / settings.columns;
      var cellH = height / settings.rows;
      points = [];
      for (var row = 0; row <= settings.rows; row += 1) {
        for (var column = 0; column <= settings.columns; column += 1) {
          var pinX = column === 0 || column === settings.columns;
          var pinY = row === 0 || row === settings.rows;
          points.push({
            x: column * cellW + (pinX ? 0 : (random() - 0.5) * settings.jitter * 2 * cellW),
            y: row * cellH + (pinY ? 0 : (random() - 0.5) * settings.jitter * 2 * cellH),
            pinX: pinX,
            pinY: pinY,
            phase: random() * Math.PI * 2,
            rate: 0.00035 + random() * 0.0004,
          });
        }
      }
      triangles = [];
      var stride = settings.columns + 1;
      for (var r = 0; r < settings.rows; r += 1) {
        for (var c = 0; c < settings.columns; c += 1) {
          var a = r * stride + c;
          var b = a + 1;
          var d = a + stride;
          var e = d + 1;
          if ((r + c) % 2 === 0) {
            triangles.push([a, b, e], [a, e, d]);
          } else {
            triangles.push([a, b, d], [b, e, d]);
          }
        }
      }
    }

    function position(point, time, motion) {
      var sway = settings.drift * motion;
      return {
        x: point.pinX ? point.x : point.x + Math.sin(time * point.rate + point.phase) * sway,
        y: point.pinY ? point.y : point.y + Math.cos(time * point.rate * 0.9 + point.phase) * sway,
      };
    }

    function corners(index, time, motion) {
      return triangles[index].map(function (pointIndex) { return position(points[pointIndex], time, motion); });
    }

    function centroid(index, time, motion) {
      var c = corners(index, time, motion);
      return { x: (c[0].x + c[1].x + c[2].x) / 3, y: (c[0].y + c[1].y + c[2].y) / 3 };
    }

    // Triangle indices ordered from the middle of the mesh outward.
    function orderFromCenter() {
      return triangles.map(function (_, index) {
        var center = centroid(index, 0, 0);
        return { index: index, distance: Math.hypot((center.x - width / 2) / width, (center.y - height / 2) / height) };
      }).sort(function (left, right) { return left.distance - right.distance; })
        .map(function (entry) { return entry.index; });
    }

    // `styleFor(index)` returns { fill, glow } (CSS colors; glow optional).
    function draw(time, motion, styleFor, lineColor) {
      context.clearRect(0, 0, width, height);
      triangles.forEach(function (_, index) {
        var style = styleFor(index);
        if (!style || !style.fill) return;
        var c = corners(index, time, motion);
        context.beginPath();
        context.moveTo(c[0].x, c[0].y);
        context.lineTo(c[1].x, c[1].y);
        context.lineTo(c[2].x, c[2].y);
        context.closePath();
        context.shadowColor = style.glow || "transparent";
        context.shadowBlur = style.glow ? 10 : 0;
        context.fillStyle = style.fill;
        context.fill();
      });
      context.shadowBlur = 0;
      if (!lineColor) return;
      context.beginPath();
      triangles.forEach(function (_, index) {
        var c = corners(index, time, motion);
        context.moveTo(c[0].x, c[0].y);
        context.lineTo(c[1].x, c[1].y);
        context.lineTo(c[2].x, c[2].y);
        context.closePath();
      });
      context.strokeStyle = lineColor;
      context.lineWidth = 1;
      context.stroke();
    }

    // The triangle under (x, y) in CSS pixels of the canvas, or -1.
    function hit(x, y, time, motion) {
      for (var index = 0; index < triangles.length; index += 1) {
        var c = corners(index, time, motion);
        var d1 = (x - c[1].x) * (c[0].y - c[1].y) - (c[0].x - c[1].x) * (y - c[1].y);
        var d2 = (x - c[2].x) * (c[1].y - c[2].y) - (c[1].x - c[2].x) * (y - c[2].y);
        var d3 = (x - c[0].x) * (c[2].y - c[0].y) - (c[2].x - c[0].x) * (y - c[0].y);
        var negative = d1 < 0 || d2 < 0 || d3 < 0;
        var positive = d1 > 0 || d2 > 0 || d3 > 0;
        if (!(negative && positive)) return index;
      }
      return -1;
    }

    layout();
    return {
      layout: layout,
      draw: draw,
      hit: hit,
      orderFromCenter: orderFromCenter,
      triangleCount: function () { return triangles.length; },
      size: function () { return { width: width, height: height }; },
    };
  }

  // ---------- message arrivals ----------
  //
  // A message the person sends pops in over a drifting triangle mesh in the
  // person's theme colors (metal mostly, gold and bronze at their shares; the
  // light colors in the light theme) that slows to a stop as it fades (about
  // 2 seconds); a new
  // reply fades in under one faint scan line. Chat re-renders its whole
  // transcript on every update, so each arrival is remembered by a stable key
  // (the browser's request id for a sent message, the message id for a reply)
  // and a re-render mid-effect continues it rather than restarting it. Only
  // messages created moments ago arrive; loading history never plays effects.
  //
  // A sent message's mesh canvas moves to the re-rendered bubble with its
  // drawing intact. A fresh canvas would stay blank until its next animation
  // frame, so the mesh blinked out and back in when the server confirmed the
  // message partway through the fade.

  var ARRIVAL_WINDOW_MS = 15000;
  // The reply time is also the scan line's crossing (styles.css reads it as
  // --ua-arrive-duration); 1170 ms is 30% slower than the first 900 ms, which
  // was too quick to notice (owner's request, 2026-10-10).
  var ARRIVAL_DURATION_MS = { sent: 2200, reply: 1170 };
  var ARRIVAL_MEMORY = 400;
  var arrivals = new Map();
  // Sent-message mesh canvases still playing, by arrival key.
  var meshes = new Map();

  function rememberArrival(key, start) {
    arrivals.set(key, start);
    if (arrivals.size > ARRIVAL_MEMORY) arrivals.delete(arrivals.keys().next().value);
  }

  function arrive(element, key, kind, createdAt) {
    var duration = ARRIVAL_DURATION_MS[kind];
    if (!element || !key || !duration) return;
    var now = window.performance.now();
    var start = arrivals.get(key);
    if (start === undefined) {
      var created = Date.parse(createdAt || "");
      var fresh = Number.isFinite(created) && Math.abs(Date.now() - created) <= ARRIVAL_WINDOW_MS;
      start = fresh && enabled() ? now : -Infinity;
      rememberArrival(key, start);
    }
    var elapsed = now - start;
    if (!(elapsed < duration) || !enabled()) return;
    var className = "ua-arrive-" + kind;
    element.classList.add(className);
    element.style.setProperty("--ua-arrive-offset", (-elapsed).toFixed(0) + "ms");
    element.style.setProperty("--ua-arrive-duration", duration + "ms");
    if (kind === "sent") meshBurst(element, key, start, duration);
    // The effect's styles (such as clipping the bubble) end with it.
    window.setTimeout(function () { element.classList.remove(className); }, duration - elapsed);
  }

  function meshBurst(element, key, start, duration) {
    var playing = meshes.get(key);
    if (playing) {
      element.prepend(playing);
      return;
    }
    var canvas = document.createElement("canvas");
    canvas.className = "ua-arrive-mesh";
    canvas.setAttribute("aria-hidden", "true");
    element.prepend(canvas);
    meshes.set(key, canvas);
    function finish() {
      canvas.remove();
      meshes.delete(key);
    }
    // Measure once the bubble is laid out (it is appended after arrive()).
    function measure(now) {
      if (now - start >= duration) {
        finish();
        return;
      }
      if (!canvas.isConnected) {
        // The chat was switched away; it may come back before the effect ends.
        window.requestAnimationFrame(measure);
        return;
      }
      var rect = canvas.getBoundingClientRect();
      var glyph = createMeshGlyph(canvas, {
        columns: Math.max(3, Math.round(rect.width / 46)),
        rows: Math.max(2, Math.round(rect.height / 38)),
        seed: Math.floor(start) || 1,
        jitter: 0.38,
        drift: 9,
      });
      var colors = palette();
      var random = mulberry32(Math.floor(start) ^ 0x9e3779b9);
      var shades = [];
      var fills = [];
      for (var index = 0; index < glyph.triangleCount(); index += 1) {
        shades.push(0.06 + random() * 0.22);
        fills.push(pick(colors, random()));
      }
      var line = colors[0].rgb;
      // Runs until the effect ends, even while a re-render has the canvas
      // out of the page for a moment, so a moved canvas keeps fading.
      function frame(time) {
        var progress = Math.min(1, (time - start) / duration);
        if (progress >= 1) {
          finish();
          return;
        }
        if (canvas.isConnected) {
          var motion = Math.pow(1 - progress, 2);
          var fade = 1 - progress * progress;
          // Time advances more slowly as the mesh settles, so it glides to a stop.
          var meshTime = start + (time - start) * (1 - progress * 0.5);
          glyph.draw(meshTime * 3, motion, function (index) {
            return { fill: rgba(fills[index], shades[index] * fade) };
          }, rgba(line, 0.3 * fade));
        }
        window.requestAnimationFrame(frame);
      }
      // Draw now, in the frame that measured, so the mesh shows at once.
      frame(now);
    }
    window.requestAnimationFrame(measure);
  }

  // ---------- onboarding intro ----------
  //
  // When onboarding opens on its Welcome page, the background fills in
  // (dark: faces fade in from the middle outward; light: the canvas fades
  // in), then the title "Universal Agents" appears (assembled from polygons
  // by intro_word.js where it is loaded) with a smaller "Click to Begin". A
  // click, Enter, Space or Escape fades the intro out and the onboarding
  // window (`options.shell`) in, then runs `options.onFinish`. Without motion
  // the same screen shows at once, without the fades. Web Chat's sign-in
  // greeting (chat_sign_in.mjs) uses the same background fill and outline.
  var INTRO_FILL_MS = 1800;
  var INTRO_TITLE_AT_MS = 1400;
  var INTRO_BEGIN_AT_MS = 2100;
  var INTRO_LEAVE_MS = 450;
  // The intro's title (owner's request, 2026-10-08; it was "Welcome!").
  var INTRO_TITLE = "Universal Agents";
  // With the polygon word (intro_word.js): it starts while the background is
  // still filling in, and "Click to Begin" follows once it is complete.
  var INTRO_WORD_AT_MS = 500;
  var INTRO_BEGIN_AFTER_WORD_MS = 250;

  var SVG_NS = "http://www.w3.org/2000/svg";
  var INTRO_OUTLINE_FILTER_ID = "ua-intro-outline";
  var INTRO_OUTLINE_WIDTH = 1;

  function svgElement(name, attributes, children) {
    var element = document.createElementNS(SVG_NS, name);
    Object.keys(attributes).forEach(function (key) { element.setAttribute(key, attributes[key]); });
    (children || []).forEach(function (child) { element.appendChild(child); });
    return element;
  }

  // The outline of the intro's title: a ring around each letter's outer shape in
  // --ua-intro-word-outline, drawn under the letters. The letters' alpha is
  // first made solid, so their see-through fill neither thins the ring nor
  // lets it show inside them, then grown by the outline width; the ring is
  // the grown shape minus the letters. Unlike -webkit-text-stroke it never
  // traces the overlapping strokes inside a letter.
  function ensureIntroOutline() {
    if (document.getElementById(INTRO_OUTLINE_FILTER_ID)) return;
    var flood = svgElement("feFlood", { result: "color", style: "flood-color: var(--ua-intro-word-outline)" });
    var filter = svgElement("filter", { id: INTRO_OUTLINE_FILTER_ID, x: "-5%", y: "-10%", width: "110%", height: "120%" }, [
      svgElement("feComponentTransfer", { in: "SourceAlpha", result: "solid" }, [
        svgElement("feFuncA", { type: "linear", slope: "4", intercept: "0" }),
      ]),
      svgElement("feMorphology", { in: "solid", operator: "dilate", radius: String(INTRO_OUTLINE_WIDTH), result: "grown" }),
      svgElement("feComposite", { in: "grown", in2: "solid", operator: "out", result: "ring" }),
      flood,
      svgElement("feComposite", { in: "color", in2: "ring", operator: "in", result: "outline" }),
      svgElement("feMerge", {}, [
        svgElement("feMergeNode", { in: "outline" }),
        svgElement("feMergeNode", { in: "SourceGraphic" }),
      ]),
    ]);
    var svg = svgElement("svg", { width: "0", height: "0", "aria-hidden": "true", focusable: "false", style: "position: absolute" }, [
      svgElement("defs", {}, [filter]),
    ]);
    document.body.appendChild(svg);
  }

  // Fills the background in, as the intro starts: the dark mesh's faces fade
  // in from the middle outward; the light background fades in.
  function fillBackground() {
    var dark = document.documentElement.getAttribute("data-color-mode") !== "light";
    if (dark && window.UAMetallicMesh && typeof window.UAMetallicMesh.reveal === "function") {
      window.UAMetallicMesh.reveal(INTRO_FILL_MS);
      return;
    }
    document.documentElement.classList.add("ua-intro-filling");
    window.setTimeout(function () {
      document.documentElement.classList.remove("ua-intro-filling");
    }, INTRO_FILL_MS);
  }

  function playIntro(options) {
    var shell = options.shell;
    if (!shell || document.querySelector(".ua-intro")) return;
    ensureIntroOutline();
    var animated = enabled();
    var overlay = document.createElement("section");
    overlay.className = "ua-intro" + (animated ? " is-animated" : " show-title show-begin");
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "ua-intro-title");
    var title = document.createElement("h1");
    title.id = "ua-intro-title";
    title.className = "ua-intro-title";
    title.textContent = INTRO_TITLE;
    var begin = document.createElement("button");
    begin.type = "button";
    begin.className = "ua-intro-begin";
    begin.textContent = "Click to Begin";
    overlay.appendChild(title);
    overlay.appendChild(begin);
    document.body.appendChild(overlay);
    shell.classList.add("is-intro-hidden");
    shell.setAttribute("inert", "");
    var timers = [];
    var finished = false;
    // The word assembled from polygons where onboarding loads intro_word.js;
    // styled text otherwise.
    var polygonWord = window.UAIntroWord ? window.UAIntroWord.create(title, { text: INTRO_TITLE }) : null;

    function showBegin() {
      if (finished) return;
      overlay.classList.add("show-begin");
      begin.focus();
    }

    function finish() {
      if (finished) return;
      finished = true;
      timers.forEach(window.clearTimeout);
      document.removeEventListener("keydown", onKey);
      overlay.classList.add("is-leaving");
      shell.removeAttribute("inert");
      shell.classList.remove("is-intro-hidden");
      if (animated) shell.classList.add("ua-intro-enter");
      window.setTimeout(function () {
        if (polygonWord) polygonWord.stop();
        overlay.remove();
        shell.classList.remove("ua-intro-enter");
      }, animated ? INTRO_LEAVE_MS + 300 : 0);
      if (options.focusTarget && typeof options.focusTarget.focus === "function") options.focusTarget.focus();
      if (typeof options.onFinish === "function") options.onFinish();
    }

    function onKey(event) {
      if (event.key === "Escape") finish();
    }

    overlay.addEventListener("click", finish);
    document.addEventListener("keydown", onKey);
    if (animated) {
      fillBackground();
      if (polygonWord) {
        timers.push(window.setTimeout(function () {
          overlay.classList.add("show-title");
          polygonWord.play(function () {
            timers.push(window.setTimeout(showBegin, INTRO_BEGIN_AFTER_WORD_MS));
          });
        }, INTRO_WORD_AT_MS));
      } else {
        timers.push(window.setTimeout(function () { overlay.classList.add("show-title"); }, INTRO_TITLE_AT_MS));
        timers.push(window.setTimeout(showBegin, INTRO_BEGIN_AT_MS));
      }
    } else {
      if (polygonWord) polygonWord.showStill();
      begin.focus();
    }
  }

  // ---------- Web Chat sign-in greeting ----------
  //
  // The intro's background fill and polygon title (intro_word.js), but in the
  // page as its heading and with no "Click to Begin" (owner's request,
  // 2026-10-08): `options.lines` (one line when it fits `options.maxWidth`,
  // otherwise one per line) form in `options.heading`; `options.stage` gets
  // `show-title` when the title shows; the sign-in window (`options.shell`)
  // waits hidden and fades in under the title once it has formed, then
  // focuses `options.focusTarget`. Any key or click shows the formed title
  // and the window at once; Enter, Space and Escape do nothing else, so they
  // never submit the form, while other keys reach the focused field.
  // `options.shellShown` keeps the window as it is (the greeting changes
  // while the window is open). The title keeps flashing until `stop()`.
  // Without motion the formed title and the window show at once.
  var GREETING_SKIP_KEYS = ["Enter", " ", "Escape"];

  function playGreeting(options) {
    var heading = options.heading;
    var stage = options.stage;
    var shell = options.shell;
    ensureIntroOutline();
    var animated = enabled();
    heading.textContent = options.lines.join(" ");
    stage.classList.remove("show-title");
    var word = window.UAIntroWord
      ? window.UAIntroWord.create(heading, { lines: options.lines, maxWidth: options.maxWidth })
      : null;
    var timers = [];
    var wordStarted = false;
    var shellShown = Boolean(options.shellShown);
    var stopped = false;
    shell.classList.remove("ua-intro-enter");
    if (!shellShown) {
      shell.classList.add("is-intro-hidden");
      shell.setAttribute("inert", "");
    }

    function showTitle() {
      stage.classList.add("show-title");
    }

    function stopListening() {
      document.removeEventListener("keydown", skip);
      document.removeEventListener("click", skip);
    }

    function showShell() {
      if (shellShown || stopped) return;
      shellShown = true;
      stopListening();
      shell.removeAttribute("inert");
      shell.classList.remove("is-intro-hidden");
      if (animated) {
        shell.classList.add("ua-intro-enter");
        timers.push(window.setTimeout(function () { shell.classList.remove("ua-intro-enter"); }, INTRO_LEAVE_MS + 300));
      }
      if (options.focusTarget && typeof options.focusTarget.focus === "function") options.focusTarget.focus();
    }

    function playWord() {
      if (stopped) return;
      wordStarted = true;
      showTitle();
      word.play(function () {
        timers.push(window.setTimeout(showShell, INTRO_BEGIN_AFTER_WORD_MS));
      });
    }

    function skip(event) {
      if (shellShown || stopped) return;
      if (event && event.type === "keydown" && GREETING_SKIP_KEYS.indexOf(event.key) >= 0) event.preventDefault();
      timers.forEach(window.clearTimeout);
      timers = [];
      if (word) {
        if (!wordStarted) playWord();
        word.skipForming();
      } else {
        showTitle();
      }
      showShell();
    }

    function resizeWord() {
      if (word) word.resize();
    }

    function motionChanged() {
      if (stopped || enabled() || !animated) return;
      animated = false;
      timers.forEach(window.clearTimeout);
      timers = [];
      shell.classList.remove("ua-intro-enter");
      showTitle();
      if (word) word.showStill();
      showShell();
    }

    window.addEventListener("resize", resizeWord);
    reducedMotion.addEventListener("change", motionChanged);
    document.addEventListener("ua:appearance-change", motionChanged);

    if (animated) {
      fillBackground();
      if (!shellShown) {
        document.addEventListener("keydown", skip);
        document.addEventListener("click", skip);
      }
      if (word) {
        timers.push(window.setTimeout(playWord, INTRO_WORD_AT_MS));
      } else {
        timers.push(window.setTimeout(showTitle, INTRO_TITLE_AT_MS));
        timers.push(window.setTimeout(showShell, INTRO_BEGIN_AT_MS));
      }
    } else {
      showTitle();
      if (word) word.showStill();
      showShell();
    }

    return {
      // Ends the motion and the key and click listeners for good.
      stop: function () {
        stopped = true;
        timers.forEach(window.clearTimeout);
        timers = [];
        stopListening();
        shell.classList.remove("ua-intro-enter");
        window.removeEventListener("resize", resizeWord);
        reducedMotion.removeEventListener("change", motionChanged);
        document.removeEventListener("ua:appearance-change", motionChanged);
        if (word) word.stop();
      },
    };
  }

  window.UAMotion = {
    enabled: enabled,
    playIntro: playIntro,
    playGreeting: playGreeting,
    fillBackground: fillBackground,
    ensureIntroOutline: ensureIntroOutline,
    accent: accent,
    rgba: rgba,
    palette: palette,
    arrive: arrive,
    onChange: function (listener) {
      reducedMotion.addEventListener("change", listener);
      document.addEventListener("ua:appearance-change", listener);
    },
  };
  window.UAMeshGlyph = { create: createMeshGlyph };
}());
