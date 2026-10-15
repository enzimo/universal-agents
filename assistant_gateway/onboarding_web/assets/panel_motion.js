(function () {
  "use strict";

  // Popups open and close in glass (owner's request, 2026-10-09).
  //
  // Closing: the popup goes at once, exactly as the page closes it, and a
  // copy of its glass takes its place for a moment and breaks into
  // triangles (the intro's glass, glass_mesh.js): each drifts a little,
  // turns, blurs and fades at its own rate. A page cannot copy an element's
  // pixels, so the shards show the popup's glass, not its text (owner's
  // choice). The backdrop fades out under them.
  //
  // Opening is the same breaking run backwards (owner's request, 2026-10-09,
  // after a version that stretched a small frame out by its corners did not
  // look right): blurred, drifted triangles appear, each at its own time,
  // and come into focus in place to form the popup's glass; then the popup's
  // own surface and text return under it as the glass fades. The popup works
  // from the first moment: the effect never takes the pointer, and focus
  // goes where the page puts it.
  //
  // Large windows open and close without glass (owner's request,
  // 2026-10-11: the glass did not look good on them). A popup that covers
  // more than LARGE_WINDOW_SHARE of the browser window when it opens or
  // closes appears and goes the way it does without motion (the backdrop's
  // short fade only). On a laptop that is Settings; on a phone, where most
  // popups fill the screen, it is most of them. A size rule rather than a
  // list keeps new large windows plain without changes here.
  //
  // Which popups: every `.setup-dialog` inside a `.setup-overlay` (setup
  // guides, info, Settings, token enrollment, Share and Send) and every
  // native <dialog> (workflow details and review). The pages open and close
  // them in many different ways (adding and removing them, the `hidden`
  // attribute, showModal and close), so one watcher notices the change
  // instead of every popup calling this file; a new popup of either kind
  // gets the effect without changes. The watcher remembers each open
  // popup's place and backdrop, so it can draw the shards after the page
  // has already removed it.
  //
  // The glass is the interface's frosted glass of the current color mode
  // (UAGlass.colors), with glints in the person's theme colors. Without interface motion
  // (Settings > Appearance, or the system's reduced-motion setting) nothing
  // plays and popups appear and go at once. Tuning values are named here.

  if (window.UAPanelMotion) return;
  var glass = window.UAGlass;
  if (!glass) {
    window.console.warn("ua.panel_motion: glass_mesh.js is not loaded; popups open and close without glass.");
    return;
  }

  // Breaking (closing), and forming (opening) as its reverse.
  var SHATTER_DELAY_MS = 120;     // triangles start breaking away within this window
  var SHATTER_MIN_MS = 450;       // each triangle's own life is between these
  var SHATTER_MAX_MS = 900;
  var SHATTER_DRIFT_PX = 14;      // how far a triangle drifts while it goes
  var SHATTER_BLUR_PX = [0, 2, 4.5, 8, 13]; // a triangle blurs through these as it goes
  var SHATTER_ALPHA = 0.85;
  var GLASS_PAD_PX = 32;          // room around the popup for drifting, blurred triangles
  var HANDOVER_MS = 220;          // opening: the formed glass fades as the popup returns under it
  var BACKDROP_OUT_MS = 260;      // closing: the backdrop fades under the breaking glass
  var TRIANGLES = 260;            // about this many triangles, whatever the popup's size
  var MIN_CELL_PX = 26;
  var MAX_CELL_PX = 110;
  var JITTER = 0.34;
  var TILT = 0.34;
  var EDGE_ALPHA = 0.22;          // faint lines between the triangles
  var FRAME_INTERVAL_MS = 16;
  // An effect ends on a timer this long after its own length even when the
  // browser delivers no animation frames (seen 2026-10-09 in a browser that
  // stopped painting a visible page), so a popup never stays hidden.
  var SAFETY_MS = 400;
  var EFFECT_LAYER = "2147483000"; // closing glass sits above everything on the page
  var LARGE_WINDOW_SHARE = 0.6;   // popups covering more of the window than this get no glass

  var OPENING = "ua-panel-opening";
  var REVEALING = "ua-panel-revealing";
  var OVERLAY = "setup-overlay";
  var SETUP_DIALOG = "setup-dialog";

  var tracked = new Map();        // open popup -> { rect, radius, backdrop, opening, observer }
  var effects = new Set();
  var frameId = 0;
  var lastFrame = 0;
  var seed = 0x9a3e1;

  function enabled() {
    return Boolean(window.UAMotion && window.UAMotion.enabled());
  }

  function now() { return window.performance.now(); }

  // ---------- Which popups are open ----------

  function isNative(panel) { return panel.tagName === "DIALOG"; }

  function popups() {
    var body = document.body;
    var list = [];
    Array.prototype.forEach.call(body.querySelectorAll("." + SETUP_DIALOG), function (panel) {
      var overlay = panel.parentNode;
      if (overlay && overlay.classList && overlay.classList.contains(OVERLAY)) list.push(panel);
    });
    Array.prototype.forEach.call(body.querySelectorAll("dialog"), function (panel) { list.push(panel); });
    return list;
  }

  function shown(panel) {
    if (!panel.isConnected) return false;
    if (isNative(panel)) return Boolean(panel.open);
    return !panel.hidden && !panel.parentNode.hidden;
  }

  function isPopupNode(node) {
    if (!node || node.nodeType === 3 || !node.classList) return false;
    return node.tagName === "DIALOG" || node.classList.contains(OVERLAY) || node.classList.contains(SETUP_DIALOG);
  }

  function holdsPopup(node) {
    if (isPopupNode(node)) return true;
    return Boolean(node && node.querySelector
      && (node.querySelector("." + SETUP_DIALOG) || node.querySelector("dialog")));
  }

  // Only changes that can open or close a popup make the watcher look again;
  // the chat transcript changes the page all the time.
  function relevant(record) {
    if (record.type === "attributes") return isPopupNode(record.target);
    return Array.prototype.some.call(record.addedNodes || [], holdsPopup)
      || Array.prototype.some.call(record.removedNodes || [], holdsPopup);
  }

  // Compares the open popups with the ones seen before: new ones open,
  // missing ones close. `quiet` (the page's first look) only remembers them.
  function sync(quiet) {
    var open = popups().filter(shown);
    tracked.forEach(function (state, panel) {
      if (open.indexOf(panel) !== -1) return;
      tracked.delete(panel);
      closed(state);
    });
    open.forEach(function (panel) {
      if (tracked.has(panel)) return;
      var state = remember(panel);
      tracked.set(panel, state);
      if (!quiet) opened(panel, state);
    });
  }

  // ---------- Where each open popup is ----------

  function backdropOf(panel) {
    try {
      var source = isNative(panel)
        ? window.getComputedStyle(panel, "::backdrop")
        : window.getComputedStyle(panel.parentNode);
      return (source && source.backgroundColor) || "";
    } catch (error) {
      return "";
    }
  }

  function measure(panel, state) {
    var box = panel.getBoundingClientRect();
    state.rect = { left: box.left, top: box.top, width: box.width, height: box.height };
  }

  function remember(panel) {
    var style = window.getComputedStyle(panel);
    var state = {
      rect: null,
      radius: parseFloat(style.borderTopLeftRadius) || 12,
      backdrop: backdropOf(panel),
      opening: null,
      observer: null,
    };
    measure(panel, state);
    // Keep the place current while it is open (Settings changes size with
    // its tabs), so the closing glass lands where the popup was.
    if (window.ResizeObserver) {
      state.observer = new window.ResizeObserver(function () { if (panel.isConnected) measure(panel, state); });
      state.observer.observe(panel);
    }
    return state;
  }

  function remeasureAll() {
    tracked.forEach(function (state, panel) { if (shown(panel)) measure(panel, state); });
  }

  // Whether a popup this size covers more than LARGE_WINDOW_SHARE of the
  // browser window. A window of unknown size counts as large, so a popup is
  // never covered by glass larger than the page.
  function large(rect) {
    var area = (window.innerWidth || 0) * (window.innerHeight || 0);
    if (!(area > 0)) return true;
    return (rect.width * rect.height) / area > LARGE_WINDOW_SHARE;
  }

  // Glass plays only with interface motion on, for a popup with a known
  // place that is not a large window.
  function plays(rect) {
    return enabled() && usable(rect) && !large(rect);
  }

  function usable(rect) {
    return Boolean(rect) && rect.width >= 8 && rect.height >= 8;
  }

  // ---------- Running effects ----------

  function frame(time) {
    frameId = 0;
    if (time - lastFrame >= FRAME_INTERVAL_MS) {
      lastFrame = time;
      effects.forEach(function (effect) {
        if (effect.step(time - effect.startedAt)) end(effect);
      });
    }
    if (effects.size) frameId = window.requestAnimationFrame(frame);
  }

  function start(effect) {
    effect.startedAt = now();
    effects.add(effect);
    effect.step(0);
    effect.safety = window.setTimeout(function () {
      if (!effects.has(effect)) return;
      window.console.warn("ua.panel_motion: no animation frames arrived; ended the " + effect.kind
        + " effect so the popup shows. duration_ms=" + effect.duration);
      end(effect);
    }, effect.duration + SAFETY_MS);
    if (!frameId) frameId = window.requestAnimationFrame(frame);
    return effect;
  }

  function end(effect) {
    if (!effects.delete(effect)) return;
    window.clearTimeout(effect.safety);
    effect.finish();
  }

  // Ends every effect at once: motion was turned off or the tab was hidden
  // (a hidden tab gets no frames, so glass would hang until it came back).
  function finishAll() {
    Array.from(effects).forEach(end);
  }

  // ---------- Drawing ----------

  function element(tag, className) {
    var node = document.createElement(tag);
    node.className = className;
    node.setAttribute("aria-hidden", "true");
    return node;
  }

  // A canvas covering the effect box, drawn `pad` px in from its edges;
  // blurred canvases draw at 1x, since the blur hides the detail anyway.
  function canvasLayer(parent, width, height, blurPx, pad) {
    var canvas = element("canvas", "ua-panel-glass");
    var dpr = blurPx > 0 ? 1 : Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.max(1, Math.round(width * dpr));
    canvas.height = Math.max(1, Math.round(height * dpr));
    if (blurPx > 0) canvas.style.filter = "blur(" + blurPx + "px)";
    parent.appendChild(canvas);
    return { canvas: canvas, context: canvas.getContext("2d"), dpr: dpr, pad: pad };
  }

  function clear(layer) {
    if (!layer.context) return;
    layer.context.setTransform(1, 0, 0, 1, 0, 0);
    layer.context.clearRect(0, 0, layer.canvas.width, layer.canvas.height);
    layer.context.setTransform(layer.dpr, 0, 0, layer.dpr, layer.pad * layer.dpr, layer.pad * layer.dpr);
  }

  function fillTriangle(layer, corners, fill, stroke) {
    if (!layer.context) return;
    glass.path(layer.context, corners);
    layer.context.fillStyle = fill;
    layer.context.fill();
    if (stroke) {
      layer.context.strokeStyle = stroke;
      layer.context.lineWidth = 0.6;
      layer.context.stroke();
    }
  }

  // The popup's glass as triangles, each with its own timing from `random`.
  function shards(rect, theme, random) {
    var cell = Math.max(MIN_CELL_PX, Math.min(MAX_CELL_PX, Math.sqrt((rect.width * rect.height) / (TRIANGLES / 2))));
    return glass.mesh(rect.width, rect.height, cell, JITTER, TILT, random).map(function (t) {
      t.glint = glass.pick(theme.glints, t.roll);
      t.shade = 0.85 + random() * 0.3;
      t.wait = random();
      t.life = random();
      var away = [t.center[0] - rect.width / 2, t.center[1] - rect.height / 2];
      var length = Math.hypot(away[0], away[1]) || 1;
      var spread = 0.4 + random() * 0.6;
      var tilt = (random() - 0.5) * 0.8;
      t.drift = [(away[0] / length + tilt) * spread, (away[1] / length - tilt) * spread];
      return t;
    });
  }

  function shifted(corners, dx, dy) {
    return corners.map(function (p) { return [p[0] + dx, p[1] + dy]; });
  }

  // ---------- Glass breaking and forming ----------

  // One popup's glass: its triangles, each with its own start and life on
  // the breaking clock, and `over`, when the last one has gone.
  function shatterPlan(rect, random) {
    var theme = glass.colors();
    var triangles = shards(rect, theme, random);
    var timing = triangles.map(function (t) {
      return { from: t.wait * SHATTER_DELAY_MS, life: SHATTER_MIN_MS + t.life * (SHATTER_MAX_MS - SHATTER_MIN_MS) };
    });
    var over = timing.reduce(function (latest, time) { return Math.max(latest, time.from + time.life); }, 0);
    return { theme: theme, triangles: triangles, timing: timing, over: over };
  }

  // The effect box with one canvas per blur level, `pad` px larger than the
  // popup on every side. Inside a native dialog there is no room around it,
  // so `pad` is 0 there and CSS fills the dialog.
  function shatterLayer(className, rect, pad, placed) {
    var layer = element("div", className);
    if (placed) {
      layer.style.left = rect.left - pad + "px";
      layer.style.top = rect.top - pad + "px";
      layer.style.width = rect.width + pad * 2 + "px";
      layer.style.height = rect.height + pad * 2 + "px";
    }
    var levels = SHATTER_BLUR_PX.map(function (blurPx) {
      return canvasLayer(layer, rect.width + pad * 2, rect.height + pad * 2, blurPx, pad);
    });
    return { element: layer, levels: levels };
  }

  // A triangle at `progress` 0..1 of breaking away: drifting, turning,
  // fading, and drawn on the two blur levels around its progress so the
  // blur grows smoothly.
  function drawShard(theme, levels, t, progress) {
    var alpha = SHATTER_ALPHA * Math.pow(1 - progress, 1.3);
    var eased = glass.easeOut(progress);
    var angle = progress * Math.PI * 0.35 * t.direction;
    var corners = shifted(
      glass.turnedCorners(t, angle, 1 + 0.06 * Math.sin(Math.PI * progress)),
      t.drift[0] * SHATTER_DRIFT_PX * eased,
      t.drift[1] * SHATTER_DRIFT_PX * eased
    );
    var color = glass.shade(theme.base.map(function (v) { return v * t.shade; }), glass.turn(t.normal, t.axis, angle), t.glint, theme.light);
    var position = progress * (levels.length - 1);
    var low = Math.floor(position);
    var high = Math.min(levels.length - 1, low + 1);
    var mix = position - low;
    var edge = low === 0 ? glass.rgba(theme.edge, EDGE_ALPHA * (1 - mix) * (1 - progress)) : null;
    fillTriangle(levels[low], corners, glass.rgba(color, alpha * (1 - mix)), edge);
    if (mix > 0 && high !== low) fillTriangle(levels[high], corners, glass.rgba(color, alpha * mix));
  }

  // The glass at `time` on the breaking clock (0: whole, `over`: gone).
  function drawShatter(plan, levels, time) {
    levels.forEach(clear);
    plan.triangles.forEach(function (t, index) {
      var timing = plan.timing[index];
      var progress = glass.clamp01((time - timing.from) / timing.life);
      if (progress < 1) drawShard(plan.theme, levels, t, progress);
    });
  }

  // ---------- Opening ----------

  function opened(panel, state) {
    if (!plays(state.rect)) return;
    state.opening = start(openEffect(panel, state));
  }

  function openEffect(panel, state) {
    var native = isNative(panel);
    var plan = shatterPlan(state.rect, glass.mulberry32(seed += 0x2f1d));
    var layer = shatterLayer("ua-panel-effect" + (native ? " is-inside" : ""), state.rect, native ? 0 : GLASS_PAD_PX, !native);
    // Inside a native dialog (the top layer, above anything else on the
    // page); otherwise in the overlay, whose box is the whole window.
    (native ? panel : panel.parentNode).appendChild(layer.element);
    panel.classList.add(OPENING);
    var revealed = false;
    return {
      kind: "opening",
      duration: plan.over + HANDOVER_MS,
      step: function (elapsed) {
        // The breaking clock run backwards: the triangles that go last
        // arrive first, blurred and drifted, and come into focus in place.
        drawShatter(plan, layer.levels, Math.max(0, plan.over - elapsed));
        if (!revealed && elapsed >= plan.over) {
          revealed = true;
          panel.classList.remove(OPENING);
          panel.classList.add(REVEALING);
        }
        layer.element.style.opacity = (1 - glass.smoothstep(plan.over, plan.over + HANDOVER_MS, elapsed)).toFixed(3);
        return elapsed >= this.duration;
      },
      finish: function () {
        layer.element.remove();
        panel.classList.remove(OPENING, REVEALING);
        if (state.opening === this) state.opening = null;
      },
    };
  }

  // ---------- Closing ----------

  function closed(state) {
    if (state.observer) state.observer.disconnect();
    if (state.opening) end(state.opening);
    if (!plays(state.rect)) return;
    start(closeEffect(state));
  }

  function closeEffect(state) {
    var plan = shatterPlan(state.rect, glass.mulberry32(seed += 0x51c7));
    var backdrop = null;
    if (state.backdrop && !/rgba\([^)]*,\s*0\)$|transparent/.test(state.backdrop)) {
      backdrop = element("div", "ua-panel-backdrop");
      backdrop.style.background = state.backdrop;
      backdrop.style.zIndex = String(Number(EFFECT_LAYER) - 1);
      document.body.appendChild(backdrop);
    }
    var layer = shatterLayer("ua-panel-effect is-closing", state.rect, GLASS_PAD_PX, true);
    layer.element.style.zIndex = EFFECT_LAYER;
    document.body.appendChild(layer.element);
    return {
      kind: "closing",
      duration: plan.over,
      step: function (elapsed) {
        drawShatter(plan, layer.levels, elapsed);
        if (backdrop) backdrop.style.opacity = (1 - glass.smoothstep(0, BACKDROP_OUT_MS, elapsed)).toFixed(3);
        return elapsed >= plan.over;
      },
      finish: function () {
        layer.element.remove();
        if (backdrop) backdrop.remove();
      },
    };
  }

  // ---------- Watching the page ----------

  var watching = false;

  function watch() {
    if (watching || !document.body) return;
    watching = true;
    sync(true);
    if (window.MutationObserver) {
      new window.MutationObserver(function (records) {
        if (records.some(relevant)) sync(false);
      }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["hidden", "open"] });
    } else {
      window.console.warn("ua.panel_motion: this browser cannot watch the page; popups open and close without glass.");
    }
    window.addEventListener("resize", remeasureAll);
    document.addEventListener("visibilitychange", function () { if (document.hidden) finishAll(); });
    if (window.UAMotion) window.UAMotion.onChange(function () { if (!enabled()) finishAll(); });
  }

  window.UAPanelMotion = {
    watch: watch,
    // Looks for opened and closed popups now (the watcher does this on its own).
    sync: function () { sync(false); },
    finishAll: finishAll,
    // For tests and tuning.
    inspect: function () {
      return {
        open: tracked.size,
        effects: effects.size,
        // The longest an opening or closing can take.
        openingMs: SHATTER_DELAY_MS + SHATTER_MAX_MS + HANDOVER_MS,
        closingMaxMs: SHATTER_DELAY_MS + SHATTER_MAX_MS,
        safetyMs: SAFETY_MS,
        largeWindowShare: LARGE_WINDOW_SHARE,
      };
    },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", watch);
  } else {
    watch();
  }
}());
