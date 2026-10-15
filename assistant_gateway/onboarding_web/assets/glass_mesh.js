(function () {
  "use strict";

  // Glass triangles shared by interface effects: the intro's polygon word and
  // the sign-in greeting (intro_word.js), and popups opening and closing
  // (panel_motion.js). One implementation so the effects look alike
  // (owner's request, 2026-10-09).
  //
  // A mesh of slightly irregular triangles covers a box; each triangle is a
  // facet of glass with a small tilt of its own that can turn in 3D around
  // an axis of its own. A 3D turn is drawn in 2D: the triangle's corners
  // keep their position along the turning axis and close in across it by
  // the cosine of the angle, and the facet is shaded with its turned normal.
  //
  // Colors are read when an effect starts (and again when the appearance
  // changes). The glass and its edge are the interface's frosted glass
  // (--ua-glass-base and --ua-glass-edge in styles.css, per color mode), so
  // effects look like the panes and stand out on any background (owner's
  // decision, 2026-10-09: glass tinted like the background did not). The
  // person's theme comes in through the glints (UAMotion.palette), the light
  // position of their dark background, and the accent of the brackets.

  if (window.UAGlass) return;

  var FALLBACK_BASE = [17, 38, 47];
  var FALLBACK_EDGE = [98, 213, 243];
  var GLINT = 0.42;              // strength of the theme-colored glints

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

  function clamp01(value) { return Math.max(0, Math.min(1, value)); }
  function easeOut(value) { return 1 - Math.pow(1 - value, 3); }
  function smoothstep(edge0, edge1, value) {
    var t = clamp01((value - edge0) / (edge1 - edge0));
    return t * t * (3 - 2 * t);
  }

  function normalize(v) {
    var length = Math.hypot(v[0], v[1], v[2]) || 1;
    return [v[0] / length, v[1] / length, v[2] / length];
  }

  function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

  // Turns `n` by `angle` around the in-plane unit axis `axis` (Rodrigues).
  function turn(n, axis, angle) {
    var a = [axis[0], axis[1], 0];
    var cos = Math.cos(angle);
    var sin = Math.sin(angle);
    var cross = [a[1] * n[2] - a[2] * n[1], a[2] * n[0] - a[0] * n[2], a[0] * n[1] - a[1] * n[0]];
    var along = dot(a, n) * (1 - cos);
    return [
      n[0] * cos + cross[0] * sin + a[0] * along,
      n[1] * cos + cross[1] * sin + a[1] * along,
      n[2] * cos + cross[2] * sin + a[2] * along,
    ];
  }

  function rgba(rgb, alpha) {
    return "rgba(" + Math.round(rgb[0]) + ", " + Math.round(rgb[1]) + ", " + Math.round(rgb[2]) + ", "
      + clamp01(alpha).toFixed(3) + ")";
  }

  function hexRgb(hex) {
    if (!/^#[0-9a-f]{6}$/i.test(String(hex || ""))) return null;
    return [1, 3, 5].map(function (offset) { return parseInt(hex.slice(offset, offset + 2), 16); });
  }

  // Where the person's background light sits, as a direction toward the
  // viewer's side; a light up and to the left otherwise.
  function lightDirection() {
    var appearance = window.UAAppearance && window.UAAppearance.current ? window.UAAppearance.current() : null;
    var dark = appearance && appearance.mode === "dark" ? appearance.dark : null;
    var x = dark ? Math.max(-1.5, Math.min(1.5, dark.lightX / 100)) : -0.3;
    var y = dark ? Math.max(-1.5, Math.min(1.5, dark.lightY / 100)) : -0.5;
    return normalize([x * 0.6, y * 0.6, 1]);
  }

  // The glass: `base` and `edge` as [r, g, b] (the interface glass of the
  // current color mode), `glints` as the theme palette's [{ rgb, weight }],
  // and the `light` direction.
  function colors() {
    var root = window.getComputedStyle(document.documentElement);
    var glints = window.UAMotion && window.UAMotion.palette ? window.UAMotion.palette() : [];
    var base = hexRgb(root.getPropertyValue("--ua-glass-base").trim());
    var edge = hexRgb(root.getPropertyValue("--ua-glass-edge").trim());
    return {
      base: base || FALLBACK_BASE.slice(),
      edge: edge || FALLBACK_EDGE.slice(),
      glints: glints.length ? glints : [{ rgb: (edge || FALLBACK_EDGE).slice(), weight: 1 }],
      light: lightDirection(),
    };
  }

  // A color from the palette in proportion to the weights, for `roll` in 0..1.
  function pick(palette, roll) {
    var total = palette.reduce(function (sum, entry) { return sum + entry.weight; }, 0);
    var target = roll * total;
    for (var index = 0; index < palette.length; index += 1) {
      target -= palette[index].weight;
      if (target < 0) return palette[index].rgb;
    }
    return palette[palette.length - 1].rgb;
  }

  // Light reflected by a facet with normal `n`: the glass color, brighter
  // where the facet faces the light, plus a soft glint in its theme color.
  function shade(base, n, glint, light) {
    if (n[2] < 0) return base.map(function (value) { return value * 0.55; });
    var diffuse = Math.max(0, dot(n, light));
    var half = normalize([light[0], light[1], light[2] + 1]);
    var glare = Math.pow(Math.max(0, dot(n, half)), 28);
    return base.map(function (value, channel) {
      return Math.min(255, value * (0.65 + 0.7 * diffuse) + glint[channel] * GLINT * glare);
    });
  }

  // Corners of a triangle turned by `angle` around its own axis, lifted
  // slightly toward the viewer by `lift`.
  function turnedCorners(t, angle, lift) {
    var cos = Math.cos(angle);
    return t.corners.map(function (p) {
      var dx = p[0] - t.center[0];
      var dy = p[1] - t.center[1];
      var along = dx * t.axis[0] + dy * t.axis[1];
      var px = dx - t.axis[0] * along;
      var py = dy - t.axis[1] * along;
      return [
        t.center[0] + (t.axis[0] * along + px * cos) * lift,
        t.center[1] + (t.axis[1] * along + py * cos) * lift,
      ];
    });
  }

  function path(context, corners) {
    context.beginPath();
    context.moveTo(corners[0][0], corners[0][1]);
    context.lineTo(corners[1][0], corners[1][1]);
    context.lineTo(corners[2][0], corners[2][1]);
    context.closePath();
  }

  // Triangles covering a `width` x `height` box in cells of about `cell` px.
  // Inner points stray from their grid cell by up to `jitter` of a cell;
  // points on the edges stay on them, so the mesh fills the box exactly.
  // Each triangle gets `corners`, `center`, a turning `axis`, a `direction`,
  // a tilted `normal` (largest tilt `tilt`) and a `roll` in 0..1 for picking
  // its glint, all from `random`, so a seed gives the same mesh again.
  function mesh(width, height, cell, jitter, tilt, random) {
    var columns = Math.max(2, Math.round(width / cell));
    var rows = Math.max(2, Math.round(height / cell));
    var cellW = width / columns;
    var cellH = height / rows;
    var points = [];
    for (var row = 0; row <= rows; row += 1) {
      for (var column = 0; column <= columns; column += 1) {
        var edgeX = column === 0 || column === columns;
        var edgeY = row === 0 || row === rows;
        points.push([
          column * cellW + (edgeX ? 0 : (random() - 0.5) * 2 * jitter * cellW),
          row * cellH + (edgeY ? 0 : (random() - 0.5) * 2 * jitter * cellH),
        ]);
      }
    }
    var stride = columns + 1;
    var triangles = [];
    function add(a, b, c) {
      var corners = [points[a], points[b], points[c]];
      var axisAngle = random() * Math.PI;
      triangles.push({
        corners: corners,
        center: [
          (corners[0][0] + corners[1][0] + corners[2][0]) / 3,
          (corners[0][1] + corners[1][1] + corners[2][1]) / 3,
        ],
        axis: [Math.cos(axisAngle), Math.sin(axisAngle)],
        direction: random() < 0.5 ? -1 : 1,
        normal: normalize([(random() - 0.5) * 2 * tilt, (random() - 0.5) * 2 * tilt, 1]),
        roll: random(),
      });
    }
    for (var r = 0; r < rows; r += 1) {
      for (var c = 0; c < columns; c += 1) {
        var a = r * stride + c;
        var b = a + 1;
        var d = a + stride;
        var e = d + 1;
        if ((r + c) % 2 === 0) {
          add(a, b, e);
          add(a, e, d);
        } else {
          add(a, b, d);
          add(b, e, d);
        }
      }
    }
    return triangles;
  }

  window.UAGlass = {
    mulberry32: mulberry32,
    clamp01: clamp01,
    easeOut: easeOut,
    smoothstep: smoothstep,
    normalize: normalize,
    turn: turn,
    rgba: rgba,
    colors: colors,
    pick: pick,
    shade: shade,
    turnedCorners: turnedCorners,
    path: path,
    mesh: mesh,
  };
}());
