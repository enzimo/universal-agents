(function () {
  "use strict";

  // The onboarding intro's title, "Universal Agents" (owner's requests,
  // 2026-10-04; the title was "Welcome!" until 2026-10-08), and Web Chat's
  // sign-in greeting, "Good Morning, Alice!" (owner's request, 2026-10-08).
  //
  // The word is written into a hidden canvas in the page's font and read
  // back as a mask. A mesh of slightly irregular triangles covers the word's
  // box and first shows as a blurred glass slab in the theme's glass color.
  // Letter by letter, from left to right, the slab's triangles lift off and
  // turn in 3D around axes of their own, softly blurred while they turn,
  // and come together into the letter as sharp facets of that glass, each
  // tilted a little differently so it catches the light, with soft glints in
  // the person's theme colors. Triangles outside the letters turn away and
  // fade. Once a letter has fully formed, it crossfades into the intro's
  // word style (the theme's glass fill, mostly opaque, with the 1 px
  // outline) and a flat drifting triangle mesh in the theme colors plays
  // inside it, the same mesh a sent chat message shows (UAMeshGlyph,
  // UAMotion.palette), slowing and fading.
  //
  // Once the whole word is in, random letters keep flashing a new flat mesh
  // (its own pattern and colors) that slowly fades, one every
  // FLASH_GAP_MIN_MS..FLASH_GAP_MAX_MS, so a few letters always glow, each at
  // a different stage, until the intro is dismissed (owner's request,
  // 2026-10-08). A letter lights again only once its last mesh has faded.
  //
  // History: the first version kept the shimmering facets as the end result
  // (too busy); the next crossfaded the whole word at the end (letters
  // reached the mesh too late); the next dropped the turning facets (the
  // owner missed them). This one turns them in per letter and hands each
  // letter to the mesh as soon as it has formed. Until 2026-10-08 the word
  // then stayed still.
  //
  // A longer text can come as `lines`: they share one line when that fits
  // the room the page gives (`maxWidth`) and otherwise go one per line, each
  // centered, so a long greeting puts the name on a second line instead of
  // shrinking. The letters still form in reading order, line by line, and
  // the flashes move around the whole text. `skipForming()` shows the formed
  // text at once and keeps the flashes going (a key or click on the sign-in
  // page).
  //
  // The triangles turn in 3D as glass_mesh.js describes.
  //
  // Tuning values are named here; the owner tunes them by eye.

  var CELL_EM = 0.16;            // triangle spacing, in units of the font size
  var JITTER = 0.36;             // how far points stray from their grid cell
  var TILT = 0.34;               // largest facet tilt (normal x/y before normalizing)
  // How fast the word forms: the six forming times below are divided by it
  // (owner's request, 2026-10-09: about 40% faster than the times as
  // written). The glow after each letter and the later flashes keep their pace.
  var FORMING_SPEED = 1.4;
  function forming(ms) { return ms / FORMING_SPEED; }
  var SLAB_IN_MS = forming(500);       // the blurred slab fades in
  var SLAB_BLUR_PX = 10;               // blur of the slab (CSS filter on its canvas)
  var FORMING_BLUR_PX = 3;             // blur of the triangles while they turn
  // A blur spreads to about three times its radius. Each blurred canvas gets
  // a transparent margin that wide, so its blurred edge stays inside the
  // canvas. WebKit repaints only a canvas's own box when its drawing
  // changes, so blur that spread past the box stayed on screen as a faint
  // shadow framing the word after the slab had gone (owner's report,
  // 2026-10-12).
  var SLAB_MARGIN_PX = SLAB_BLUR_PX * 3;
  var FORMING_MARGIN_PX = FORMING_BLUR_PX * 3;
  var FIRST_LETTER_MS = forming(500);  // from the slab appearing to the first letter's turns
  var LETTER_STEP_MS = forming(200);   // between one letter's turns and the next's
  var LETTER_SPREAD_MS = forming(450); // a letter's triangles start within this window
  var TURN_MS = forming(800);          // one triangle's turn into place
  var LETTER_IN_MS = forming(450);     // a formed letter crossfades into the word style
  var INNER_MESH_IN_MS = 250;    // a letter's flat mesh fades in
  var INNER_MESH_MS = 2400;      // ...then slows and fades over this long
  var INNER_CELL_PX = 40;        // the flat mesh's triangle spacing
  var FLASH_IN_MS = 800;         // a random letter's later flash fades in (gently: 250 ms was jarring)
  var FLASH_MS = 3000;           // ...then slows and fades; the flash's whole life
  var FLASH_GAP_MIN_MS = 500;    // time from one flash's start to the next,
  var FLASH_GAP_MAX_MS = 1100;   // ...picked at random; FLASH_MS / 800 ms ≈ 4 glow at once
  var WORD_ALPHA = 0.9;          // opacity of the facets
  var EDGE_ALPHA = 0.1;          // faint lines between settled facets
  var FRAME_INTERVAL_MS = 16;

  // The mesh, facet shading and the theme's glass colors (glass_mesh.js,
  // loaded before this file). The glass, edges and glints follow the
  // person's theme and mode, and change with it while the word shows
  // (owner's request, 2026-10-09; the glass was a fixed teal).
  var glass = window.UAGlass;
  if (!glass) {
    // Without the shared glass the intro shows its title as styled text.
    window.console.warn("ua.intro_word: glass_mesh.js is not loaded; the title shows as text.");
    return;
  }
  var clamp01 = glass.clamp01;
  var easeOut = glass.easeOut;
  var smoothstep = glass.smoothstep;
  var turn = glass.turn;
  var rgba = glass.rgba;
  var pickColor = glass.pick;
  var turnedCorners = glass.turnedCorners;
  var path = glass.path;
  var mulberry32 = glass.mulberry32;

  // `options.text`: one line. `options.lines`: parts that share one line
  // when it fits in `options.maxWidth` (px, or a function returning it,
  // read once the fonts are ready) and otherwise go one per line.
  function create(title, options) {
    var settings = options || {};
    var lines = Array.isArray(settings.lines) && settings.lines.length
      ? settings.lines.map(String)
      : [settings.text || title.textContent || ""];
    var text = lines.join(" ");
    var label = document.createElement("span");
    label.className = "ua-intro-word-text";
    label.textContent = text;
    // Back to front: the blurred slab, the triangles while they turn (softly
    // blurred), the settled facets (sharp), and the formed word (styled
    // letters and their flat meshes) with the outline filter. The settled
    // facets stay out of the outlined canvas: the filter strengthens faint
    // alpha, so the facets of a letter still forming got a flickering rim
    // before the letter had formed (owner's report, 2026-10-11).
    var slab = document.createElement("canvas");
    slab.className = "ua-intro-word-slab";
    slab.setAttribute("aria-hidden", "true");
    var forming = document.createElement("canvas");
    forming.className = "ua-intro-word-forming";
    forming.setAttribute("aria-hidden", "true");
    var settledView = document.createElement("canvas");
    settledView.className = "ua-intro-word-settled";
    settledView.setAttribute("aria-hidden", "true");
    var word = document.createElement("canvas");
    word.className = "ua-intro-word-facets";
    word.setAttribute("aria-hidden", "true");
    title.textContent = "";
    [label, slab, forming, settledView, word].forEach(function (node) { title.appendChild(node); });

    var slabContext = slab.getContext("2d");
    var formingContext = forming.getContext("2d");
    var settledContext = settledView.getContext("2d");
    var wordContext = word.getContext("2d");
    if (!slabContext || !formingContext || !settledContext || !wordContext) {
      // No 2D canvas: the word shows as styled text.
      title.textContent = text;
      return {
        play: function (done) { if (done) done(); },
        showStill: function () {},
        resize: function () {},
        skipForming: function () {},
        stop: function () {},
        inspect: function () { return null; },
      };
    }
    title.classList.add("has-polygon-word");

    var scene = null;
    var started = 0;
    var running = false;
    var frameId = 0;
    var lastFrame = 0;
    var onDone = null;
    var doneCalled = false;
    // Set by stop(); a play() still waiting for the fonts then never starts.
    var stopped = false;
    // Set by skipForming(); a play() still waiting for the fonts then starts
    // with the text already formed.
    var skipped = false;
    var still = false;

    function layer(width, height, dpr) {
      var canvas = document.createElement("canvas");
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      var context = canvas.getContext("2d");
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      return { canvas: canvas, context: context };
    }

    // The finished word in the intro's word fill (--ua-intro-word-fill-start
    // and -end, which follow the theme's glass), trimmed to the letters.
    function paintStyled(styled, mask, width, height) {
      var rootStyle = window.getComputedStyle(document.documentElement);
      var gradient = styled.context.createLinearGradient(0, 0, width, height);
      gradient.addColorStop(0, rootStyle.getPropertyValue("--ua-intro-word-fill-start").trim() || "rgba(23, 52, 64, 0.88)");
      gradient.addColorStop(1, rootStyle.getPropertyValue("--ua-intro-word-fill-end").trim() || "rgba(11, 21, 27, 0.84)");
      styled.context.save();
      styled.context.setTransform(1, 0, 0, 1, 0, 0);
      styled.context.clearRect(0, 0, styled.canvas.width, styled.canvas.height);
      styled.context.drawImage(mask.canvas, 0, 0);
      styled.context.restore();
      styled.context.globalCompositeOperation = "source-in";
      styled.context.fillStyle = gradient;
      styled.context.fillRect(0, 0, width, height);
      styled.context.globalCompositeOperation = "source-over";
    }

    // The person changed their theme (or previews one) while the word shows:
    // the glass, glints, edges and word fill take the new colors without
    // restarting the forming. Letters already glowing keep their colors.
    function themeChanged() {
      if (stopped || !scene) return;
      scene.theme = glass.colors();
      scene.triangles.forEach(function (t) { t.glint = pickColor(scene.theme.glints, t.roll); });
      paintStyled(scene.styled, scene.mask, scene.width, scene.height);
      if (!running) drawStill();
    }
    document.addEventListener("ua:appearance-change", themeChanged);

    function build() {
      var style = window.getComputedStyle(title);
      var fontSize = parseFloat(style.fontSize) || 96;
      var font = (style.fontWeight || "800") + " " + fontSize + "px " + (style.fontFamily || "sans-serif");
      var letterSpacing = style.letterSpacing || "0px";
      var dpr = Math.min(2, window.devicePixelRatio || 1);
      var measure = document.createElement("canvas").getContext("2d");
      measure.font = font;
      if ("letterSpacing" in measure) measure.letterSpacing = letterSpacing;
      var cell = fontSize * CELL_EM;
      var pad = cell * 1.5;
      var lineHeight = Math.ceil(fontSize * 1.18);
      // One line when it fits the room given, otherwise one line per part.
      var room = typeof settings.maxWidth === "function" ? settings.maxWidth() : settings.maxWidth;
      var oneLineWidth = Math.ceil(measure.measureText(text).width) + pad * 2;
      var drawn = lines.length > 1 && room > 0 && oneLineWidth > room ? lines : [text];
      var lineWidths = drawn.map(function (line) { return Math.ceil(measure.measureText(line).width); });
      var width = Math.max.apply(null, lineWidths) + pad * 2;
      var height = lineHeight * drawn.length + pad * 2;
      // Each line centered, with the band of the box its letters own: the
      // first and last bands reach the box's top and bottom edges.
      var rowsOfText = drawn.map(function (line, row) {
        return {
          text: line,
          x: pad + (width - pad * 2 - lineWidths[row]) / 2,
          baseline: pad + row * lineHeight + fontSize * 0.92,
          top: row === 0 ? 0 : pad + row * lineHeight,
          bottom: row === drawn.length - 1 ? height : pad + (row + 1) * lineHeight,
        };
      });

      [[slab, SLAB_MARGIN_PX], [forming, FORMING_MARGIN_PX], [settledView, 0], [word, 0]].forEach(function (entry) {
        var canvas = entry[0];
        var margin = entry[1];
        canvas.width = Math.round((width + margin * 2) * dpr);
        canvas.height = Math.round((height + margin * 2) * dpr);
        canvas.style.width = width + margin * 2 + "px";
        canvas.style.height = height + margin * 2 + "px";
        canvas.style.left = -margin + "px";
        canvas.style.top = -margin + "px";
      });
      slab.style.filter = "blur(" + SLAB_BLUR_PX + "px)";
      forming.style.filter = "blur(" + FORMING_BLUR_PX + "px)";
      title.style.width = width + "px";
      title.style.height = height + "px";

      // The letters as a mask: which triangles belong to the word, and the
      // shape that settled facets and flat meshes are trimmed to.
      var mask = layer(width, height, dpr);
      mask.context.font = font;
      if ("letterSpacing" in mask.context) mask.context.letterSpacing = letterSpacing;
      mask.context.fillStyle = "#fff";
      rowsOfText.forEach(function (line) { mask.context.fillText(line.text, line.x, line.baseline); });
      var pixels = mask.context.getImageData(0, 0, mask.canvas.width, mask.canvas.height).data;
      function inside(x, y) {
        var px = Math.round(x * dpr);
        var py = Math.round(y * dpr);
        if (px < 0 || py < 0 || px >= mask.canvas.width || py >= mask.canvas.height) return false;
        return pixels[(py * mask.canvas.width + px) * 4 + 3] > 96;
      }

      var styled = layer(width, height, dpr);
      paintStyled(styled, mask, width, height);

      // Each letter's cell of the text: its line's band, from where it
      // starts to where the next starts (a line's first and last letters
      // reach the box's edges); when its triangles start turning; and the
      // canvas its flat meshes are drawn on (lit by lightLetter once the
      // triangles are known). Letters form in reading order.
      var theme = glass.colors();
      var letters = [];
      var turnsFrom = FIRST_LETTER_MS;
      rowsOfText.forEach(function (line) {
        var characters = Array.from(line.text);
        characters.forEach(function (character, index) {
          var left = index === 0 ? 0 : line.x + measure.measureText(characters.slice(0, index).join("")).width;
          var right = index === characters.length - 1
            ? width
            : line.x + measure.measureText(characters.slice(0, index + 1).join("")).width;
          if (!character.trim()) return;
          letters.push({
            index: letters.length,
            left: left,
            right: right,
            top: line.top,
            bottom: line.bottom,
            turnsFrom: turnsFrom,
            formed: turnsFrom + TURN_MS,
            meshCanvas: document.createElement("canvas"),
            glow: null,
          });
          turnsFrom += LETTER_STEP_MS;
        });
      });
      // The letter whose cell holds (x, y): the line by y, then the letter by x.
      function letterAt(x, y) {
        var rowLetter = null;
        for (var index = 0; index < letters.length; index += 1) {
          var letter = letters[index];
          if (y >= letter.bottom) continue;
          if (rowLetter && letter.top !== rowLetter.top) break;
          rowLetter = letter;
          if (x < letter.right) return letter;
        }
        return rowLetter || letters[letters.length - 1];
      }

      // The triangles: each starts in the slab and turns in (or away) within
      // its letter's window.
      var random = mulberry32(0x5eed + text.length * 7919);
      var triangles = glass.mesh(width, height, cell, JITTER, TILT, random).map(function (t) {
        var cx = t.center[0];
        var cy = t.center[1];
        var samples = [[cx, cy]];
        t.corners.forEach(function (p, i) {
          var q = t.corners[(i + 1) % 3];
          samples.push(p, [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2], [(p[0] + cx) / 2, (p[1] + cy) / 2]);
        });
        var owner = letterAt(cx, cy);
        var start = (owner ? owner.turnsFrom : FIRST_LETTER_MS) + random() * LETTER_SPREAD_MS;
        var inWord = samples.some(function (sample) { return inside(sample[0], sample[1]); });
        if (owner && inWord) owner.formed = Math.max(owner.formed, start + TURN_MS);
        t.letter = owner;
        t.inWord = inWord;
        t.start = start;
        t.glint = pickColor(theme.glints, t.roll);
        t.slabShade = 0.85 + random() * 0.3;
        return t;
      });

      var lastFormed = letters.reduce(function (latest, letter) { return Math.max(latest, letter.formed); }, 0);
      var lastTurn = triangles.reduce(function (latest, t) { return Math.max(latest, t.start + TURN_MS); }, 0);
      scene = {
        width: width,
        height: height,
        lines: drawn.length,
        dpr: dpr,
        letters: letters,
        triangles: triangles,
        mask: mask,
        styled: styled,
        settled: { canvas: settledView, context: settledContext },
        inner: layer(width, height, dpr),
        theme: theme,
        glowRandom: mulberry32(0x1d2c3b + text.length * 131),
        // The word is complete once the last letter has crossfaded in, and
        // the forming is over once every triangle has finished turning too;
        // from then on only the styled word and the flashes are drawn.
        complete: lastFormed + LETTER_IN_MS,
        formingOver: Math.max(lastTurn, lastFormed + LETTER_IN_MS),
        formingCleared: false,
        // The random flashes start once the word is complete.
        nextFlash: lastFormed + LETTER_IN_MS,
      };
    }

    // Lights `letter` with a new flat mesh (its own pattern and colors) from
    // `start`: it fades in over `inMs` and has slowed and faded by `lifeMs`.
    function lightLetter(letter, start, inMs, lifeMs) {
      if (!window.UAMeshGlyph) return;
      var s = scene;
      var random = s.glowRandom;
      var width = letter.right - letter.left;
      var height = letter.bottom - letter.top;
      var glyph = window.UAMeshGlyph.create(letter.meshCanvas, {
        size: { width: width, height: height },
        columns: Math.max(2, Math.round(width / INNER_CELL_PX)),
        rows: Math.max(2, Math.round(height / INNER_CELL_PX)),
        seed: Math.floor(random() * 4294967296),
        jitter: 0.38,
        drift: 9,
      });
      var fills = [];
      var shades = [];
      for (var triangle = 0; triangle < glyph.triangleCount(); triangle += 1) {
        shades.push(0.06 + random() * 0.22);
        fills.push(pickColor(s.theme.glints, random()));
      }
      letter.glow = {
        glyph: glyph,
        fills: fills,
        shades: shades,
        line: s.theme.glints[0].rgb,
        start: start,
        inMs: inMs,
        lifeMs: lifeMs,
        // Where in its drift the mesh starts, so flashes do not move alike.
        phase: random() * 4000,
      };
    }

    function glowing(letter, at) {
      return Boolean(letter.glow) && at >= letter.glow.start && at < letter.glow.start + letter.glow.lifeMs;
    }

    // Starts the flashes due by `elapsed`: each lights a random letter that
    // is not glowing. After a long pause (a hidden tab gets no frames) the
    // missed flashes are skipped rather than all started at once.
    function flashLetters(elapsed) {
      var s = scene;
      if (elapsed - s.nextFlash > FLASH_MS) s.nextFlash = elapsed;
      while (elapsed >= s.nextFlash) {
        var at = s.nextFlash;
        var dark = s.letters.filter(function (letter) { return !glowing(letter, at); });
        if (dark.length) lightLetter(dark[Math.floor(s.glowRandom() * dark.length)], at, FLASH_IN_MS, FLASH_MS);
        s.nextFlash = at + FLASH_GAP_MIN_MS + s.glowRandom() * (FLASH_GAP_MAX_MS - FLASH_GAP_MIN_MS);
      }
    }

    function letterIn(letter, elapsed) {
      return smoothstep(0, LETTER_IN_MS, elapsed - letter.formed);
    }

    // The slab, and the triangles turning in or away.
    function drawForming(elapsed) {
      var s = scene;
      // Word coordinates, shifted into each blurred canvas's margin.
      slabContext.setTransform(s.dpr, 0, 0, s.dpr, SLAB_MARGIN_PX * s.dpr, SLAB_MARGIN_PX * s.dpr);
      slabContext.clearRect(-SLAB_MARGIN_PX, -SLAB_MARGIN_PX, s.width + SLAB_MARGIN_PX * 2, s.height + SLAB_MARGIN_PX * 2);
      formingContext.setTransform(s.dpr, 0, 0, s.dpr, FORMING_MARGIN_PX * s.dpr, FORMING_MARGIN_PX * s.dpr);
      formingContext.clearRect(-FORMING_MARGIN_PX, -FORMING_MARGIN_PX, s.width + FORMING_MARGIN_PX * 2, s.height + FORMING_MARGIN_PX * 2);
      var settled = s.settled.context;
      settled.setTransform(s.dpr, 0, 0, s.dpr, 0, 0);
      settled.clearRect(0, 0, s.width, s.height);
      var slabIn = smoothstep(0, SLAB_IN_MS, elapsed);

      s.triangles.forEach(function (t) {
        var progress = clamp01((elapsed - t.start) / TURN_MS);
        // In the slab until it lifts off.
        var slabAlpha = slabIn * (1 - smoothstep(0, 0.3, progress));
        if (slabAlpha > 0.003) {
          path(slabContext, t.corners);
          slabContext.fillStyle = rgba(s.theme.base.map(function (value) { return value * t.slabShade; }), 0.8 * slabAlpha);
          slabContext.fill();
        }
        if (progress <= 0) return;
        var eased = easeOut(progress);
        if (!t.inWord) {
          // Turns away and fades.
          var away = eased * Math.PI * 0.6 * t.direction;
          var fade = (1 - eased) * 0.7;
          if (fade <= 0.003) return;
          path(formingContext, turnedCorners(t, away, 1));
          formingContext.fillStyle = rgba(glass.shade(s.theme.base, turn(t.normal, t.axis, away), t.glint, s.theme.light), fade * WORD_ALPHA);
          formingContext.fill();
          return;
        }
        // Turns into place, from edge-on to flat, softly blurred while it
        // turns and sharp once settled, until its letter hands over.
        var handover = 1 - (t.letter ? letterIn(t.letter, elapsed) : 0);
        if (handover <= 0.003) return;
        var angle = (1 - eased) * Math.PI * 0.5 * t.direction;
        var lift = 1 + 0.12 * Math.sin(Math.PI * progress);
        var corners = turnedCorners(t, angle, lift);
        var color = glass.shade(s.theme.base, turn(t.normal, t.axis, angle), t.glint, s.theme.light);
        var appear = smoothstep(0, 0.35, progress);
        var settle = smoothstep(0.55, 1, progress);
        if (settle < 1) {
          path(formingContext, corners);
          formingContext.fillStyle = rgba(color, appear * (1 - settle) * WORD_ALPHA);
          formingContext.fill();
        }
        if (settle > 0) {
          path(settled, corners);
          settled.fillStyle = rgba(color, settle * handover * WORD_ALPHA);
          settled.fill();
          if (progress >= 1) {
            settled.strokeStyle = rgba(s.theme.edge, EDGE_ALPHA * handover);
            settled.lineWidth = 0.6;
            settled.stroke();
          }
        }
      });

      // Settled facets keep to the letters.
      settled.save();
      settled.setTransform(1, 0, 0, 1, 0, 0);
      settled.globalCompositeOperation = "destination-in";
      settled.drawImage(s.mask.canvas, 0, 0);
      settled.restore();
    }

    // The formed word: letters crossfading into the word style, and the flat
    // meshes inside formed letters.
    function drawWord(elapsed) {
      var s = scene;
      wordContext.setTransform(1, 0, 0, 1, 0, 0);
      wordContext.clearRect(0, 0, word.width, word.height);
      wordContext.setTransform(s.dpr, 0, 0, s.dpr, 0, 0);
      s.letters.forEach(function (letter) {
        var arrived = letterIn(letter, elapsed);
        if (arrived <= 0) return;
        wordContext.save();
        wordContext.beginPath();
        wordContext.rect(letter.left, letter.top, letter.right - letter.left, letter.bottom - letter.top);
        wordContext.clip();
        wordContext.globalAlpha = arrived;
        wordContext.drawImage(s.styled.canvas, 0, 0, s.width, s.height);
        wordContext.restore();
      });

      var inner = s.inner.context;
      inner.setTransform(1, 0, 0, 1, 0, 0);
      inner.clearRect(0, 0, s.inner.canvas.width, s.inner.canvas.height);
      var drewMesh = false;
      s.letters.forEach(function (letter) {
        var glow = letter.glow;
        var since = glow ? elapsed - glow.start : 0;
        if (!glow || since <= 0 || since >= glow.lifeMs) return;
        var progress = since / glow.lifeMs;
        var strength = smoothstep(0, glow.inMs, since) * (1 - progress * progress);
        var motion = Math.pow(1 - progress, 2);
        var time = (since + glow.phase) * (1 - progress * 0.5);
        glow.glyph.draw(time * 3, motion, function (triangle) {
          return { fill: rgba(glow.fills[triangle], glow.shades[triangle] * strength) };
        }, rgba(glow.line, 0.3 * strength));
        inner.drawImage(letter.meshCanvas, Math.round(letter.left * s.dpr), Math.round(letter.top * s.dpr));
        drewMesh = true;
      });
      if (!drewMesh) return;
      // Keep the meshes inside the letters.
      inner.save();
      inner.globalCompositeOperation = "destination-in";
      inner.drawImage(s.mask.canvas, 0, 0);
      inner.restore();
      wordContext.setTransform(1, 0, 0, 1, 0, 0);
      wordContext.drawImage(s.inner.canvas, 0, 0);
    }

    // Clears the slab, the turning triangles and the settled facets.
    function clearForming() {
      [[slabContext, slab], [formingContext, forming], [scene.settled.context, settledView]].forEach(function (entry) {
        entry[0].setTransform(1, 0, 0, 1, 0, 0);
        entry[0].clearRect(0, 0, entry[1].width, entry[1].height);
      });
    }

    // The finished word alone: the styled letters, as without motion.
    function drawStill() {
      clearForming();
      wordContext.setTransform(1, 0, 0, 1, 0, 0);
      wordContext.clearRect(0, 0, word.width, word.height);
      wordContext.drawImage(scene.styled.canvas, 0, 0);
    }

    // Runs until stop(): the flashes go on for as long as the intro shows.
    function frame(time) {
      frameId = 0;
      if (!running) return;
      var elapsed = time - started;
      if (time - lastFrame >= FRAME_INTERVAL_MS) {
        lastFrame = time;
        if (elapsed < scene.formingOver) {
          drawForming(elapsed);
        } else if (!scene.formingCleared) {
          scene.formingCleared = true;
          clearForming();
        }
        if (elapsed >= scene.complete) flashLetters(elapsed);
        drawWord(elapsed);
        if (elapsed >= scene.complete && !doneCalled) {
          doneCalled = true;
          if (onDone) onDone();
        }
      }
      frameId = window.requestAnimationFrame(frame);
    }

    function ready() {
      return document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve();
    }

    return {
      // Forms the word letter by letter; `done` runs once every letter is in.
      play: function (done) {
        onDone = done || null;
        ready().then(function () {
          if (stopped) return;
          build();
          if (still) {
            drawStill();
            return;
          }
          // Each letter's first flat mesh starts as soon as it has formed.
          scene.letters.forEach(function (letter) {
            lightLetter(letter, letter.formed, INNER_MESH_IN_MS, INNER_MESH_MS);
          });
          running = true;
          started = window.performance.now() - (skipped ? scene.formingOver : 0);
          lastFrame = 0;
          frameId = window.requestAnimationFrame(frame);
        });
      },
      // The finished word at once, without motion.
      showStill: function () {
        // Freeze synchronously, including a play waiting for fonts to load.
        still = true;
        running = false;
        if (frameId) window.cancelAnimationFrame(frameId);
        frameId = 0;
        ready().then(function () {
          if (stopped) return;
          build();
          drawStill();
        });
      },
      // Re-measure the current font and available width without restarting
      // forming, replaying completion, or disturbing a visible sign-in form.
      resize: function () {
        if (stopped || !scene) return;
        build();
        if (!running) {
          drawStill();
          return;
        }
        scene.letters.forEach(function (letter) {
          lightLetter(letter, letter.formed, INNER_MESH_IN_MS, INNER_MESH_MS);
        });
        lastFrame = 0;
      },
      // Shows the formed text at once; the flashes go on as after forming.
      skipForming: function () {
        skipped = true;
        if (running && scene) started = Math.min(started, window.performance.now() - scene.formingOver);
      },
      // Ends the motion for good; the intro calls it once it is dismissed.
      stop: function () {
        stopped = true;
        running = false;
        document.removeEventListener("ua:appearance-change", themeChanged);
        if (frameId) window.cancelAnimationFrame(frameId);
        frameId = 0;
      },
      // For tests and tuning: the scene's size and timing once built.
      inspect: function () {
        if (!scene) return null;
        return {
          letters: scene.letters.length,
          triangles: scene.triangles.length,
          inWord: scene.triangles.filter(function (t) { return t.inWord; }).length,
          lettersFormedMs: scene.letters.map(function (letter) { return letter.formed; }),
          completeMs: scene.complete,
          formingOverMs: scene.formingOver,
          // Indices of the letters whose flat mesh shows at `ms`.
          glowingAt: function (ms) {
            return scene.letters.filter(function (letter) { return glowing(letter, ms); })
              .map(function (letter) { return letter.index; });
          },
          width: scene.width,
          height: scene.height,
          lines: scene.lines,
          // Each letter's cell, for checking the layout.
          cells: scene.letters.map(function (letter) {
            return { left: letter.left, right: letter.right, top: letter.top, bottom: letter.bottom };
          }),
        };
      },
    };
  }

  window.UAIntroWord = { create: create };
}());
