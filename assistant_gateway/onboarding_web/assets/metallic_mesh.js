(function () {
  "use strict";

  // Dark metallic background (the single renderer source; Docs, broker pages
  // and the offline setup guide use this same file).
  //
  // The mesh is a faceted 3D heightfield of brushed metal plates. Each pixel
  // reflects a small studio: a dark room plus one rectangular softbox at
  // finite distance. A planar mirror maps a planar softbox projectively, so its soft
  // edges cross each flat face as straight ramps, which is what makes folded
  // sheet metal read as polished. Plate edges carry a narrow chamfer that
  // catches light from the side the edge faces, giving the bright hairlines.

  // Defaults and ranges mirror `ui.onboarding.appearance.dark` in
  // runtime_settings/appearance.py and default_values.py.
  var DEFAULT_SETTINGS = {
    // "full" draws the moving WebGL background; "basic" draws a still 2D one.
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
  };
  var NUMBER_RANGES = {
    facetSize: [140, 520],
    facetDepth: [0, 100],
    chromeFaces: [0, 20],
    shine: [0, 100],
    edgeHighlights: [0, 100],
    surfaceGrain: [0, 100],
    lightX: [-300, 300],
    lightY: [-300, 300],
    lightSoftness: [0, 100],
    lightWanderDistance: [0, 150],
    lightSpeed: [10, 400],
    meshSpeed: [10, 400],
    seed: [1, 4294967295],
  };
  var ACCENT_SHARE_RANGE = [0, 50];
  var FLOATS_PER_VERTEX = 25;
  // The light drifts slowly, so 24 frames per second is visually smooth.
  var FRAME_INTERVAL_MS = 41;
  var MAX_RENDER_PIXELS = 4200000;

  var MODE_FULL = "full";
  var MODE_BASIC = "basic";

  // The page's mesh canvas also serves the light theme, which holds a 2D
  // context on it, and a canvas cannot hand out WebGL after that. The dark
  // background therefore draws on its own layer placed directly above the
  // page canvas, and the page canvas is left to the light theme.
  var host = document.querySelector("[data-ua-metallic-mesh]") || document.getElementById("mesh-canvas");
  if (!host) return;
  if (window.UAMetallicMesh && window.UAMetallicMesh.host === host) return;
  // Docs, generated sites, the config editor and broker pages keep a still
  // background; Web Chat and onboarding move.
  var staticSurface = host.classList.contains("ua-doc-static-mesh");
  var canvas = createLayer();
  var gl = null;
  var context2d = null;
  var mode = "";
  var webglUnavailable = false;
  var contextLost = false;
  var api = null;

  var runtimeAppearance = window.UAAppearance && typeof window.UAAppearance.current === "function"
    ? window.UAAppearance.current()
    : null;
  var settings = normalizeSettings(runtimeAppearance && runtimeAppearance.dark);
  // The dark background shows only in dark mode; the light theme owns the
  // page canvas otherwise.
  var enabled = runtimeAppearance
    ? runtimeAppearance.mode === "dark"
    : document.documentElement.getAttribute("data-color-mode") !== "light";
  var program = null;
  var vao = null;
  var buffer = null;
  var vertexCount = 0;
  var uniforms = {};
  var width = 0;
  var height = 0;
  var dpr = 1;
  var frameHandle = 0;
  var lastFrameTime = 0;
  var frozenTime = null;
  var lastStepTime = 0;
  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  function createLayer() {
    var layer = document.createElement("canvas");
    layer.setAttribute("aria-hidden", "true");
    layer.setAttribute("data-ua-metallic-layer", "");
    var placement = window.getComputedStyle(host);
    ["position", "top", "right", "bottom", "left", "zIndex"].forEach(function (name) {
      layer.style[name] = placement[name];
    });
    layer.style.pointerEvents = "none";
    host.insertAdjacentElement("afterend", layer);
    return layer;
  }

  // ---------- random and noise ----------

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

  function valueNoise(seed) {
    function lattice(ix, iy) {
      var h = Math.imul(ix, 374761393) ^ Math.imul(iy, 668265263) ^ Math.imul(seed, 1442695041);
      h = Math.imul(h ^ (h >>> 13), 1274126177);
      return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
    }
    return function (x, y) {
      var ix = Math.floor(x);
      var iy = Math.floor(y);
      var fx = x - ix;
      var fy = y - iy;
      var sx = fx * fx * (3 - 2 * fx);
      var sy = fy * fy * (3 - 2 * fy);
      var a = lattice(ix, iy);
      var b = lattice(ix + 1, iy);
      var c = lattice(ix, iy + 1);
      var d = lattice(ix + 1, iy + 1);
      return a + (b - a) * sx + (c - a) * sy + (a - b - c + d) * sx * sy;
    };
  }

  function cloneDefaults() {
    return JSON.parse(JSON.stringify(DEFAULT_SETTINGS));
  }

  function clampNumber(value, fallback, range) {
    var numeric = Number(value);
    if (value === undefined || value === null || value === "" || !Number.isFinite(numeric)) return fallback;
    return Math.max(range[0], Math.min(range[1], numeric));
  }

  function normalizeHexColor(value, fallback) {
    var rendered = String(value || "").trim().toLowerCase();
    return /^#[0-9a-f]{6}$/.test(rendered) ? rendered : fallback;
  }

  // Fills missing values from the defaults and clamps the rest, so a partial
  // or out-of-range appearance never breaks the drawing.
  function normalizeSettings(candidate) {
    var source = candidate && typeof candidate === "object" ? candidate : {};
    var next = cloneDefaults();
    next.detail = source.detail === "basic" ? "basic" : "full";
    next.surfaceColor = normalizeHexColor(source.surfaceColor, next.surfaceColor);
    next.metalColor = normalizeHexColor(source.metalColor, next.metalColor);
    Object.keys(NUMBER_RANGES).forEach(function (name) {
      next[name] = clampNumber(source[name], next[name], NUMBER_RANGES[name]);
    });
    next.seed = Math.floor(next.seed);
    next.chromeFaces = Math.round(next.chromeFaces);
    ["lightMotion", "lightWander", "meshMotion"].forEach(function (name) {
      if (typeof source[name] === "boolean") next[name] = source[name];
    });
    var accents = source.accents && typeof source.accents === "object" ? source.accents : {};
    Object.keys(next.accents).forEach(function (name) {
      var accent = accents[name] && typeof accents[name] === "object" ? accents[name] : {};
      next.accents[name].color = normalizeHexColor(accent.color, next.accents[name].color);
      next.accents[name].share = clampNumber(accent.share, next.accents[name].share, ACCENT_SHARE_RANGE);
    });
    return next;
  }

  function hexToLinear(hex) {
    var value = /^#[0-9a-f]{6}$/i.test(hex) ? hex : "#000000";
    return [1, 3, 5].map(function (offset) {
      var c = parseInt(value.slice(offset, offset + 2), 16) / 255;
      return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
  }

  // ---------- geometry ----------

  // Chrome faces are tilted toward the light; steeper tilts look like folds.
  var MAX_CHROME_TILT = 38 * Math.PI / 180;
  // A chrome face is only nudged when it already points nearly at the light.
  // Larger corrections drag its neighbors into the light as well and turn
  // the background busy and bright.
  var MAX_CHROME_NUDGE = 18 * Math.PI / 180;

  // Stable random numbers keyed by point or face identity, so a face keeps
  // its tint, grain and bevels whenever the mesh is rebuilt or re-connected.
  function hashUnit() {
    var h = ((settings.seed >>> 0) ^ 0x9e3779b9) >>> 0;
    for (var i = 0; i < arguments.length; i += 1) {
      h = Math.imul(h ^ (arguments[i] >>> 0), 0x85ebca6b);
      h ^= h >>> 13;
      h = Math.imul(h, 0xc2b2ae35);
      h ^= h >>> 16;
    }
    return (h >>> 0) / 4294967296;
  }

  function facetSizeForViewport() {
    // Narrow screens get proportionally smaller planes so several still fit.
    var fit = Math.sqrt(Math.min(width, height) / 900);
    return settings.facetSize * Math.max(0.6, Math.min(1, fit));
  }

  // Point spacing. The local spacing radius is both the minimum distance
  // between points and the widest gap allowed. It varies only about 2x and
  // changes gradually across the screen, so neighboring faces stay close in
  // size while calmer and busier regions still differ. Any gap wider than the
  // local radius gets a new point at its center, so no face can grow
  // disproportionately large. A filled layout like this also keeps corners
  // wide, so no point ever has to be deleted to fix a narrow face.
  var SPACING_LOW = 0.42;
  var SPACING_HIGH = 1.04;
  // Pushes the size field toward its ends, so there are distinct calm areas
  // of large plates and busier areas of smaller ones rather than mostly
  // middle-sized faces everywhere.
  var SPACING_CONTRAST = 1.35;
  // Diagonal (including the off-screen frame) up to which the size regions
  // keep their base scale.
  var FIELD_REFERENCE_DIAGONAL = 2400;
  var GAP_FILL_RATIO = 0.95;
  // A face may be at most this many times the median area of its neighbors.
  var NEIGHBOR_AREA_LIMIT = 2.2;
  var MAX_GAP_FILL_PASSES = 30;
  // Points keep this many local radii away from the fixed frame, so hull
  // faces never become narrow caps.
  var FRAME_CLEARANCE = 0.5;

  // The size field gives the local spacing radius. It is centered on its
  // own average over the layout, so every layout has a similar overall
  // number of faces and the variety comes from within the screen instead of
  // one seed being all large plates and another all small ones.
  function sizeField(noise, size, bounds) {
    // Regions of large and small plates scale with screens larger than a
    // typical laptop, so a big monitor shows the same amount of variety
    // rather than more extreme contrasts side by side.
    var diagonal = Math.hypot(bounds.maxX - bounds.minX, bounds.maxY - bounds.minY);
    var scale = size * 3.2 * Math.max(1, diagonal / FIELD_REFERENCE_DIAGONAL);
    function raw(x, y, t) {
      return noise(x / scale + t * 0.006, y / scale - t * 0.005);
    }
    var total = 0;
    var samples = 0;
    for (var gx = 0; gx <= 12; gx += 1) {
      for (var gy = 0; gy <= 8; gy += 1) {
        total += raw(
          bounds.minX + (bounds.maxX - bounds.minX) * gx / 12,
          bounds.minY + (bounds.maxY - bounds.minY) * gy / 8,
          0
        );
        samples += 1;
      }
    }
    var offset = 0.5 - total / samples;
    return {
      radius: function (x, y, t) {
        var density = raw(x, y, t) + offset;
        density = Math.max(0, Math.min(1, 0.5 + (density - 0.5) * SPACING_CONTRAST));
        return size * (SPACING_LOW + (SPACING_HIGH - SPACING_LOW) * density);
      },
    };
  }

  function layoutBounds(size) {
    var margin = size * 0.9;
    return { minX: -margin, minY: -margin, maxX: width + margin, maxY: height + margin };
  }

  function insideFrame(bounds, x, y, clearance) {
    return x > bounds.minX + clearance && x < bounds.maxX - clearance
      && y > bounds.minY + clearance && y < bounds.maxY - clearance;
  }

  function circumcircle(a, b, c) {
    var d = 2 * (a.x * (b.y - c.y) + b.x * (c.y - a.y) + c.x * (a.y - b.y));
    if (Math.abs(d) < 1e-9) return null;
    var a2 = a.x * a.x + a.y * a.y;
    var b2 = b.x * b.x + b.y * b.y;
    var c2 = c.x * c.x + c.y * c.y;
    var x = (a2 * (b.y - c.y) + b2 * (c.y - a.y) + c2 * (a.y - b.y)) / d;
    var y = (a2 * (c.x - b.x) + b2 * (a.x - c.x) + c2 * (b.x - a.x)) / d;
    return { x: x, y: y, r: Math.hypot(a.x - x, a.y - y) };
  }

  function generatePoints(random, field, size, bounds) {
    var points = [];
    function add(x, y, frame) {
      points.push({ id: points.length, x: x, y: y, z: 0, frame: frame });
    }

    // Frame points keep the hull rectangular and outside the viewport.
    var frameStep = size * 0.85;
    for (var fx = bounds.minX; fx <= bounds.maxX + 0.1; fx += frameStep) {
      add(fx, bounds.minY, true);
      add(fx, bounds.maxY, true);
    }
    for (var fy = bounds.minY + frameStep; fy < bounds.maxY - 0.1; fy += frameStep) {
      add(bounds.minX, fy, true);
      add(bounds.maxX, fy, true);
    }

    // Variable-radius dart throwing; gap filling completes the layout.
    var area = (bounds.maxX - bounds.minX) * (bounds.maxY - bounds.minY);
    var attempts = Math.ceil(area / (size * size) * 40);
    for (var attempt = 0; attempt < attempts; attempt += 1) {
      var x = bounds.minX + random() * (bounds.maxX - bounds.minX);
      var y = bounds.minY + random() * (bounds.maxY - bounds.minY);
      var radius = field.radius(x, y, 0);
      if (!insideFrame(bounds, x, y, radius * FRAME_CLEARANCE)) continue;
      var blocked = false;
      for (var i = 0; i < points.length && !blocked; i += 1) {
        var dx = points[i].x - x;
        var dy = points[i].y - y;
        blocked = dx * dx + dy * dy < radius * radius;
      }
      if (!blocked) add(x, y, false);
    }
    return points;
  }

  function faceContains(corners, point) {
    var a = signedArea(corners[0], corners[1], point);
    var b = signedArea(corners[1], corners[2], point);
    var c = signedArea(corners[2], corners[0], point);
    return (a >= 0 && b >= 0 && c >= 0) || (a <= 0 && b <= 0 && c <= 0);
  }

  // Where to add points so faces stay in proportion, most urgent first.
  // Two rules: a gap wider than `gapRatio` local radii gets a point at the
  // center of its circle, and a face more than `neighborLimit` times the
  // median area of its neighbors gets a point inside it (its circle's center
  // when that lies in the face, otherwise its centroid). The first bounds
  // faces against the local spacing; the second bounds the size step between
  // neighbors, which the spacing alone does not on large screens. Delaunay
  // circles are empty, so a point at a center keeps its distance from every
  // existing point. Chosen sites are at least a local radius apart.
  function newPointSites(points, tris, field, bounds, t, gapRatio, neighborLimit) {
    var areas = tris.map(function (tri) {
      return Math.abs(signedArea(points[tri[0]], points[tri[1]], points[tri[2]]));
    });
    var owners = edgeOwnersOf(tris);
    function medianNeighborArea(index) {
      var tri = tris[index];
      var near = [];
      for (var corner = 0; corner < 3; corner += 1) {
        var a = tri[(corner + 1) % 3];
        var b = tri[(corner + 2) % 3];
        (owners.get(a < b ? a + "," + b : b + "," + a) || []).forEach(function (owner) {
          if (owner.tri !== index) near.push(areas[owner.tri]);
        });
      }
      if (!near.length) return Infinity;
      near.sort(function (x, y) { return x - y; });
      return near[Math.floor(near.length / 2)];
    }
    var found = [];
    tris.forEach(function (tri, index) {
      var corners = [points[tri[0]], points[tri[1]], points[tri[2]]];
      var circle = circumcircle(corners[0], corners[1], corners[2]);
      if (!circle) return;
      var gapExcess = circle.r / (field.radius(circle.x, circle.y, t) * gapRatio);
      // Faces on the fixed frame lie off screen and are stretched by design.
      var onFrame = corners.some(function (p) { return p.frame; });
      var sizeExcess = onFrame ? 0 : areas[index] / (neighborLimit * medianNeighborArea(index));
      var excess = Math.max(gapExcess, sizeExcess);
      if (excess <= 1) return;
      var site = { x: circle.x, y: circle.y };
      if (sizeExcess > gapExcess && !faceContains(corners, site)) {
        site = {
          x: (corners[0].x + corners[1].x + corners[2].x) / 3,
          y: (corners[0].y + corners[1].y + corners[2].y) / 3,
        };
      }
      var radius = field.radius(site.x, site.y, t);
      if (!insideFrame(bounds, site.x, site.y, radius * FRAME_CLEARANCE)) return;
      found.push({ x: site.x, y: site.y, radius: radius, excess: excess });
    });
    found.sort(function (left, right) { return right.excess - left.excess; });
    var chosen = [];
    found.forEach(function (site) {
      var crowded = chosen.some(function (other) {
        return Math.hypot(other.x - site.x, other.y - site.y) < Math.max(other.radius, site.radius);
      });
      if (!crowded) chosen.push(site);
    });
    return chosen;
  }

  function buildLayout(random, field, size, bounds) {
    var points = generatePoints(random, field, size, bounds);
    for (var pass = 0; pass < MAX_GAP_FILL_PASSES; pass += 1) {
      var tris = delaunay(points);
      var gaps = newPointSites(points, tris, field, bounds, 0, GAP_FILL_RATIO, NEIGHBOR_AREA_LIMIT);
      if (!gaps.length) return { points: points, tris: tris };
      gaps.forEach(function (gap) {
        points.push({ id: points.length, x: gap.x, y: gap.y, z: 0, frame: false });
      });
    }
    console.warn("ua.metallic_mesh: gaps remain after " + MAX_GAP_FILL_PASSES + " fill passes", {
      seed: settings.seed, width: width, height: height,
    });
    return { points: points, tris: delaunay(points) };
  }

  function delaunay(points) {
    var n = points.length;
    var minX = Infinity;
    var minY = Infinity;
    var maxX = -Infinity;
    var maxY = -Infinity;
    points.forEach(function (p) {
      minX = Math.min(minX, p.x);
      minY = Math.min(minY, p.y);
      maxX = Math.max(maxX, p.x);
      maxY = Math.max(maxY, p.y);
    });
    var span = Math.max(maxX - minX, maxY - minY) * 30;
    var midX = (minX + maxX) / 2;
    var midY = (minY + maxY) / 2;
    var verts = points.map(function (p) { return { x: p.x, y: p.y }; });
    verts.push({ x: midX - span, y: midY - span });
    verts.push({ x: midX + span, y: midY - span });
    verts.push({ x: midX, y: midY + span });

    function makeTriangle(a, b, c) {
      var A = verts[a];
      var B = verts[b];
      var C = verts[c];
      var d = 2 * (A.x * (B.y - C.y) + B.x * (C.y - A.y) + C.x * (A.y - B.y));
      var a2 = A.x * A.x + A.y * A.y;
      var b2 = B.x * B.x + B.y * B.y;
      var c2 = C.x * C.x + C.y * C.y;
      var ux = (a2 * (B.y - C.y) + b2 * (C.y - A.y) + c2 * (A.y - B.y)) / d;
      var uy = (a2 * (C.x - B.x) + b2 * (A.x - C.x) + c2 * (B.x - A.x)) / d;
      return { a: a, b: b, c: c, x: ux, y: uy, r2: (A.x - ux) * (A.x - ux) + (A.y - uy) * (A.y - uy) };
    }

    var triangles = [makeTriangle(n, n + 1, n + 2)];
    for (var i = 0; i < n; i += 1) {
      var p = verts[i];
      var edges = new Map();
      var kept = [];
      triangles.forEach(function (t) {
        var dx = p.x - t.x;
        var dy = p.y - t.y;
        if (dx * dx + dy * dy < t.r2) {
          [[t.a, t.b], [t.b, t.c], [t.c, t.a]].forEach(function (e) {
            var key = e[0] < e[1] ? e[0] + "," + e[1] : e[1] + "," + e[0];
            if (edges.has(key)) edges.set(key, null);
            else edges.set(key, e);
          });
        } else {
          kept.push(t);
        }
      });
      edges.forEach(function (e) {
        if (e) kept.push(makeTriangle(e[0], e[1], i));
      });
      triangles = kept;
    }
    return triangles.filter(function (t) {
      return t.a < n && t.b < n && t.c < n;
    }).map(function (t) { return [t.a, t.b, t.c]; });
  }

  function cornerAngles(a, b, c) {
    function angle(p, q, r) {
      var ux = q.x - p.x;
      var uy = q.y - p.y;
      var vx = r.x - p.x;
      var vy = r.y - p.y;
      var cos = (ux * vx + uy * vy) / Math.max(1e-9, Math.hypot(ux, uy) * Math.hypot(vx, vy));
      return Math.acos(Math.max(-1, Math.min(1, cos)));
    }
    return [angle(a, b, c), angle(b, c, a), angle(c, a, b)];
  }

  function inViewport(point, pad) {
    return point.x > -pad && point.x < width + pad && point.y > -pad && point.y < height + pad;
  }

  function faceKey(t, points) {
    return t.map(function (i) { return points[i].id; }).sort(function (a, b) { return a - b; });
  }

  function signedArea(a, b, c) {
    return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
  }

  // Triangles are stored counter-clockwise in screen space, so a negative
  // area later means a point has crossed an edge.
  function orient(t, points) {
    return signedArea(points[t[0]], points[t[1]], points[t[2]]) < 0 ? [t[0], t[2], t[1]] : t;
  }

  function angleAt(p, q, r) {
    var ux = q.x - p.x;
    var uy = q.y - p.y;
    var vx = r.x - p.x;
    var vy = r.y - p.y;
    var cos = (ux * vx + uy * vy) / Math.max(1e-9, Math.hypot(ux, uy) * Math.hypot(vx, vy));
    return Math.acos(Math.max(-1, Math.min(1, cos)));
  }

  function edgeOwnersOf(faces) {
    var owners = new Map();
    faces.forEach(function (t, index) {
      for (var corner = 0; corner < 3; corner += 1) {
        var a = t[(corner + 1) % 3];
        var b = t[(corner + 2) % 3];
        var key = a < b ? a + "," + b : b + "," + a;
        var list = owners.get(key);
        if (!list) owners.set(key, list = []);
        list.push({ tri: index, corner: corner });
      }
    });
    return owners;
  }

  // ---------- living mesh ----------
  //
  // The mesh is a fixed set of points. Interior points drift on a slow
  // swirling current (the curl of a moving noise field, so they neither
  // bunch up nor thin out on their own), keep their spacing with gentle
  // repulsion, and rise and fall at their own rhythms, so faces stretch,
  // travel and tilt into and out of the light.
  //
  // Connections change by edge flips: when a quad's shared edge fails the
  // Delaunay test by more than FLIP_HYSTERESIS, it swaps diagonals. The
  // hysteresis stops an edge from flipping back and forth. Mirror-like metal
  // turns even a small sudden change into a flash, so the two replaced faces
  // stay on top as "ghosts" and fade out; they cover exactly the same quad,
  // so the change reads as one face smoothly turning into two others.
  //
  // Chrome moments roam the mesh: a face that nearly reflects the softbox is
  // eased into a full reflection, held, and released. The first frame (and
  // every static surface) shows the same chrome faces fully on.

  var FLIP_HYSTERESIS = 0.06;
  var GHOST_FADE_SECONDS = 0.9;
  var MAX_STEP_SECONDS = 0.1;
  // Drift speed in facet sizes per second at mesh speed 100.
  var FLOW_SPEED = 0.05;
  // Spacing: edges shorter than SPACING_REST push their points apart, edges
  // longer than SPACING_GAP pull them together, and edges in between are
  // left alone so the layout stays irregular. Both are in units of the
  // local point radius.
  var SPACING_REST = 1.15;
  var SPACING_GAP = 1.9;
  var SPACING_STIFFNESS = 1.6;
  var LIVE_GAP_RATIO = 1.2;
  var LIVE_NEIGHBOR_AREA_LIMIT = 2.6;
  var LIVE_CROWD_RATIO = 0.55;
  var GAP_STIFFNESS = 0.5;
  var WALL_STIFFNESS = 2.0;
  var CHROME_EASE_SECONDS = 3;
  // Seconds of spacing-only relaxation before the first frame. Fresh random
  // points are not yet balanced against the spacing forces; without this the
  // mesh visibly jolts for its first second.
  var SETTLE_SECONDS = 3;

  var world = null;

  function restRadius(w, p) {
    return w.field.radius(p.x, p.y, w.time);
  }

  // Heights follow the reflection clock: a tilting face reads as moving
  // light, so it belongs with the light speed, not the mesh speed.
  function updateHeights(w) {
    var size = w.size;
    var t = w.shimmer;
    var amplitude = settings.facetDepth / 100 * size * 0.6;
    w.points.forEach(function (p) {
      if (p.dead) return;
      var broad = w.noise(p.x / (size * 4.5) + 31.7 + t * 0.004, p.y / (size * 4.5) - 12.3 - t * 0.003) * 2 - 1;
      var drama = 0.15 + Math.pow(w.noise(p.x / (size * 5.5) - 7.1, p.y / (size * 5.5) + 3.3), 1.5) * 1.3;
      var jitter = 0.62 * Math.sin(p.phaseA + p.rateA * t) + 0.38 * Math.sin(p.phaseB + p.rateB * t);
      p.baseZ = amplitude * (broad * 0.55 + jitter * 0.6 * drama);
    });
  }

  // The normal that reflects the camera ray from `origin` onto a point of a
  // softbox, or null when the face would have to tilt too steeply.
  function aimAtSoftbox(origin, light, along, across, camera) {
    var target = [
      light.center[0] + light.axisU[0] * light.size[0] * along + light.axisV[0] * light.size[1] * across,
      light.center[1] + light.axisU[1] * light.size[0] * along + light.axisV[1] * light.size[1] * across,
      light.center[2] + light.axisU[2] * light.size[0] * along + light.axisV[2] * light.size[1] * across,
    ];
    var toLight = normalize3([target[0] - origin.x, target[1] - origin.y, target[2] - origin.z]);
    var toCamera = normalize3([camera[0] - origin.x, camera[1] - origin.y, camera[2] - origin.z]);
    var normal = normalize3([toLight[0] + toCamera[0], toLight[1] + toCamera[1], toLight[2] + toCamera[2]]);
    return Math.acos(normal[2]) <= MAX_CHROME_TILT ? normal : null;
  }

  // Where on the softbox a chrome face points, keyed by face identity.
  // Aiming at the middle of the light gives a bright chrome plate; aiming
  // near its soft edge gives a chrome-to-grey ramp across the face.
  function chromeAim(key, center, light, camera) {
    var along = (hashUnit(key[0], key[1], key[2], 9) - 0.5) * 1.0;
    var edgeRoll = hashUnit(key[0], key[1], key[2], 10);
    var across = edgeRoll < 0.35
      ? (edgeRoll / 0.35 - 0.5) * 0.6
      : (edgeRoll < 0.675 ? -1 : 1) * (0.8 + hashUnit(key[0], key[1], key[2], 11) * 0.5);
    return aimAtSoftbox(center, light, along, across, camera);
  }

  function baseCenter(w, t) {
    var P = w.points;
    return {
      x: (P[t[0]].x + P[t[1]].x + P[t[2]].x) / 3,
      y: (P[t[0]].y + P[t[1]].y + P[t[2]].y) / 3,
      z: (P[t[0]].baseZ + P[t[1]].baseZ + P[t[2]].baseZ) / 3,
    };
  }

  // Broad, well-separated visible faces that already point nearly at a
  // softbox; the nudge stays small so their neighbors barely change.
  function pickChromeFaces(w, count, light, camera) {
    var P = w.points;
    var busy = new Set();
    var centers = [];
    w.moments.forEach(function (m) {
      m.tri.forEach(function (i) { busy.add(i); });
      centers.push(baseCenter(w, m.tri));
    });
    var candidates = w.tris.filter(function (t) {
      var corners = t.map(function (i) { return P[i]; });
      if (corners.some(function (p) { return p.frame || busy.has(p.index); })) return false;
      var center = baseCenter(w, t);
      if (!inViewport(center, -w.size * 0.25)) return false;
      var angles = cornerAngles(corners[0], corners[1], corners[2]);
      return Math.min(angles[0], angles[1], angles[2]) > 35 * Math.PI / 180
        && Math.abs(signedArea(corners[0], corners[1], corners[2])) > w.size * w.size * 0.35;
    }).map(function (t) {
      var key = faceKey(t, P);
      var center = baseCenter(w, t);
      var aim = chromeAim(key, center, light, camera);
      if (!aim) return null;
      var natural = normalOf(
        { x: P[t[0]].x, y: P[t[0]].y, z: P[t[0]].baseZ },
        { x: P[t[1]].x, y: P[t[1]].y, z: P[t[1]].baseZ },
        { x: P[t[2]].x, y: P[t[2]].y, z: P[t[2]].baseZ }
      );
      var cos = natural[0] * aim[0] + natural[1] * aim[1] + natural[2] * aim[2];
      var nudge = Math.acos(Math.max(-1, Math.min(1, cos)));
      if (nudge > MAX_CHROME_NUDGE) return null;
      // Prefer small nudges, with seeded variety; moving meshes add time so
      // the same face is not always picked next.
      var variety = hashUnit(key[0], key[1], key[2], 7, Math.floor(w.shimmer)) * 0.12;
      return { tri: t.slice(), key: key, center: center, order: nudge + variety };
    }).filter(Boolean).sort(function (left, right) { return left.order - right.order; });

    var chosen = [];
    candidates.forEach(function (face) {
      if (chosen.length >= count) return;
      if (face.tri.some(function (i) { return busy.has(i); })) return;
      var crowded = centers.some(function (other) {
        return Math.hypot(other.x - face.center.x, other.y - face.center.y) < w.size * 2;
      });
      if (crowded) return;
      face.tri.forEach(function (i) { busy.add(i); });
      centers.push(face.center);
      chosen.push(face);
    });
    return chosen;
  }

  function startMoment(w, face, alreadyOn) {
    var key = face.key;
    var hold = 7 + hashUnit(key[0], key[1], key[2], 12, Math.floor(w.shimmer)) * 8;
    w.moments.push({
      tri: face.tri,
      key: key,
      duration: hold + CHROME_EASE_SECONDS * 2,
      age: alreadyOn ? CHROME_EASE_SECONDS : 0,
      normal: null,
    });
  }

  function smoothstep(edge0, edge1, value) {
    var t = Math.max(0, Math.min(1, (value - edge0) / (edge1 - edge0)));
    return t * t * (3 - 2 * t);
  }

  function momentStrength(m) {
    return smoothstep(0, CHROME_EASE_SECONDS, m.age)
      * (1 - smoothstep(m.duration - CHROME_EASE_SECONDS, m.duration, m.age));
  }

  // Heights are the breathing base plus each active chrome moment's offset,
  // which tilts its three corners onto the aimed plane.
  function applyHeights(w, light, camera) {
    var P = w.points;
    P.forEach(function (p) { p.z = p.baseZ; });
    w.moments.forEach(function (m) {
      var center = baseCenter(w, m.tri);
      var aim = chromeAim(m.key, center, light, camera);
      if (aim) m.normal = aim;
      if (!m.normal) return;
      var strength = momentStrength(m);
      m.tri.forEach(function (i) {
        var p = P[i];
        var planeZ = center.z - (m.normal[0] * (p.x - center.x) + m.normal[1] * (p.y - center.y)) / m.normal[2];
        p.z = p.baseZ + (planeZ - p.baseZ) * strength;
      });
    });
  }

  function initPointMotion(p) {
    p.phaseA = hashUnit(p.id, 21) * Math.PI * 2;
    p.phaseB = hashUnit(p.id, 22) * Math.PI * 2;
    p.rateA = Math.PI * 2 / (18 + hashUnit(p.id, 23) * 27);
    p.rateB = Math.PI * 2 / (11 + hashUnit(p.id, 24) * 19);
  }

  function createWorld() {
    var size = facetSizeForViewport();
    var random = mulberry32(settings.seed);
    var noise = valueNoise(settings.seed ^ 0x5bd1e995);
    var bounds = layoutBounds(size);
    var field = sizeField(noise, size, bounds);
    var mesh = buildLayout(random, field, size, bounds);
    var w = {
      size: size,
      noise: noise,
      field: field,
      points: mesh.points,
      tris: [],
      ghosts: [],
      moments: [],
      // Two clocks: `time` drives shape motion (mesh speed); `shimmer`
      // drives the light, face tilt and chrome moments (reflection speed).
      time: 0,
      shimmer: 0,
      nextMomentAt: 3,
      chromeTarget: 0,
      nextId: mesh.points.length,
      flips: 0,
      births: 0,
      deaths: 0,
      bounds: bounds,
    };
    w.points.forEach(function (p, index) {
      p.index = index;
      initPointMotion(p);
    });
    w.tris = mesh.tris.map(function (t) { return orient(t, w.points); });
    for (var settled = 0; settled < SETTLE_SECONDS; settled += 1 / 24) {
      movePoints(w, 1 / 24, false);
      flipEdges(w);
    }
    w.ghosts = [];
    updateHeights(w);
    var visible = w.tris.filter(function (t) { return inViewport(baseCenter(w, t), 0); }).length;
    w.chromeTarget = settings.chromeFaces > 0 ? Math.max(1, Math.round(visible * settings.chromeFaces / 100)) : 0;
    var light = studioLight(0);
    var camera = cameraPosition();
    pickChromeFaces(w, w.chromeTarget, light, camera).forEach(function (face) {
      startMoment(w, face, true);
    });
    applyHeights(w, light, camera);
    return w;
  }

  // Velocity of the swirling current: the curl of a slowly scrolling noise
  // potential, so the flow has no sources or sinks.
  function flowAt(w, x, y) {
    var scale = w.size * 3.5;
    var t = w.time * 0.02;
    function potential(px, py) {
      return w.noise(px / scale + t, py / scale - t * 0.7)
        + 0.5 * w.noise(px / (scale * 0.5) - t * 1.3 + 17.1, py / (scale * 0.5) + t + 4.2);
    }
    var h = 1;
    var dx = (potential(x + h, y) - potential(x - h, y)) / (2 * h);
    var dy = (potential(x, y + h) - potential(x, y - h)) / (2 * h);
    var speed = FLOW_SPEED * w.size * scale;
    return [dy * speed, -dx * speed];
  }

  function movePoints(w, dt, withFlow) {
    var P = w.points;
    var push = P.map(function () { return [0, 0]; });
    edgeOwnersOf(w.tris).forEach(function (list, key) {
      var ends = key.split(",");
      var i = Number(ends[0]);
      var j = Number(ends[1]);
      var dx = P[j].x - P[i].x;
      var dy = P[j].y - P[i].y;
      var distance = Math.max(1e-6, Math.hypot(dx, dy));
      var radius = (restRadius(w, P[i]) + restRadius(w, P[j])) * 0.5;
      var amount = 0;
      if (distance < radius * SPACING_REST) {
        amount = SPACING_STIFFNESS * (radius * SPACING_REST - distance) / distance * 0.5;
      } else if (distance > radius * SPACING_GAP) {
        amount = -GAP_STIFFNESS * (distance - radius * SPACING_GAP) / distance * 0.5;
      } else {
        return;
      }
      push[i][0] -= dx * amount;
      push[i][1] -= dy * amount;
      push[j][0] += dx * amount;
      push[j][1] += dy * amount;
    });
    var b = w.bounds;
    var inset = w.size * 0.35;
    P.forEach(function (p, i) {
      if (p.frame || p.dead) return;
      // The current fades out near the fixed frame so the border never thins.
      var edgeDistance = Math.min(p.x - b.minX, b.maxX - p.x, p.y - b.minY, b.maxY - p.y);
      var calm = smoothstep(w.size * 0.4, w.size * 1.4, edgeDistance);
      var flow = withFlow ? flowAt(w, p.x, p.y) : [0, 0];
      flow[0] *= calm;
      flow[1] *= calm;
      var wallX = Math.max(0, b.minX + inset - p.x) - Math.max(0, p.x - (b.maxX - inset));
      var wallY = Math.max(0, b.minY + inset - p.y) - Math.max(0, p.y - (b.maxY - inset));
      p.x += (flow[0] + push[i][0] + wallX * WALL_STIFFNESS) * dt;
      p.y += (flow[1] + push[i][1] + wallY * WALL_STIFFNESS) * dt;
      // Never let an interior point reach the fixed frame.
      p.x = Math.max(b.minX + 2, Math.min(b.maxX - 2, p.x));
      p.y = Math.max(b.minY + 2, Math.min(b.maxY - 2, p.y));
    });
  }

  function segmentsCross(a, b, c, d) {
    return signedArea(a, b, c) * signedArea(a, b, d) < 0
      && signedArea(c, d, a) * signedArea(c, d, b) < 0;
  }

  function flipEdges(w) {
    var P = w.points;
    var total = 0;
    for (var pass = 0; pass < 4; pass += 1) {
      var touched = new Set();
      var flipped = 0;
      edgeOwnersOf(w.tris).forEach(function (list) {
        if (list.length !== 2) return;
        var first = list[0];
        var second = list[1];
        if (touched.has(first.tri) || touched.has(second.tri)) return;
        var t1 = w.tris[first.tri];
        var t2 = w.tris[second.tri];
        var c = t1[first.corner];
        var d = t2[second.corner];
        var a = t1[(first.corner + 1) % 3];
        var b = t1[(first.corner + 2) % 3];
        if (angleAt(P[c], P[a], P[b]) + angleAt(P[d], P[a], P[b]) <= Math.PI + FLIP_HYSTERESIS) return;
        if (!segmentsCross(P[a], P[b], P[c], P[d])) return;
        w.ghosts.push({ tri: t1, opacity: 1 }, { tri: t2, opacity: 1 });
        w.tris[first.tri] = orient([c, d, a], P);
        w.tris[second.tri] = orient([d, c, b], P);
        touched.add(first.tri);
        touched.add(second.tri);
        flipped += 1;
      });
      total += flipped;
      if (!flipped) break;
    }
    return total;
  }

  // Reconnects the live points from scratch. Faces that no longer exist
  // stay on top as ghosts and fade out; unchanged faces are not touched.
  function retriangulate(w) {
    var P = w.points;
    var live = [];
    P.forEach(function (p, i) { if (!p.dead) live.push(i); });
    var next = delaunay(live.map(function (i) { return P[i]; })).map(function (t) {
      return orient(t.map(function (k) { return live[k]; }), P);
    });
    function key(t) { return t.slice().sort(function (a, b) { return a - b; }).join(","); }
    var kept = new Set(next.map(key));
    w.tris.forEach(function (t) {
      if (!kept.has(key(t))) w.ghosts.push({ tri: t, opacity: 1 });
    });
    w.tris = next;
  }

  // A crossed edge means a point outran the flips. Rebuild the connections
  // and fade the old ones out; this should stay rare.
  function repairInvertedFaces(w) {
    var P = w.points;
    var inverted = w.tris.some(function (t) { return signedArea(P[t[0]], P[t[1]], P[t[2]]) <= 0; });
    if (!inverted) return;
    console.warn("ua.metallic_mesh: a point crossed an edge; rebuilding mesh connections", {
      seed: settings.seed, time: w.time,
    });
    retriangulate(w);
  }

  function addPoint(w, x, y) {
    var p = { id: w.nextId, x: x, y: y, z: 0, baseZ: 0, frame: false, index: w.points.length };
    w.nextId += 1;
    initPointMotion(p);
    w.points.push(p);
  }

  // Keeps face sizes bounded while the mesh moves: a gap wider than
  // LIVE_GAP_RATIO local radii or a face larger than LIVE_NEIGHBOR_AREA_LIMIT
  // times its neighbors gets a new point, and of two points closer than
  // LIVE_CROWD_RATIO radii the newer one is removed.
  // The ratios sit on either side of the layout limits so the mesh does not
  // keep adding and removing the same point. Removed points stay in the
  // array, frozen, until their fading ghosts are gone.
  function maintainSpacing(w) {
    var P = w.points;
    var inMoment = new Set();
    w.moments.forEach(function (m) { m.tri.forEach(function (i) { inMoment.add(i); }); });
    var removed = new Set();
    edgeOwnersOf(w.tris).forEach(function (list, key) {
      var ends = key.split(",");
      var i = Number(ends[0]);
      var j = Number(ends[1]);
      if (removed.has(i) || removed.has(j)) return;
      var radius = (restRadius(w, P[i]) + restRadius(w, P[j])) * 0.5;
      if (Math.hypot(P[j].x - P[i].x, P[j].y - P[i].y) >= radius * LIVE_CROWD_RATIO) return;
      var victim = P[i].frame ? j : P[j].frame ? i : (P[i].id > P[j].id ? i : j);
      if (P[victim].frame || inMoment.has(victim)) return;
      removed.add(victim);
    });
    var gaps = newPointSites(P, w.tris, w.field, w.bounds, w.time, LIVE_GAP_RATIO, LIVE_NEIGHBOR_AREA_LIMIT);
    if (!removed.size && !gaps.length) return;
    removed.forEach(function (i) { P[i].dead = true; });
    gaps.forEach(function (gap) { addPoint(w, gap.x, gap.y); });
    w.births += gaps.length;
    w.deaths += removed.size;
    retriangulate(w);
  }

  // Drops removed points once no fading ghost refers to them.
  function compactPoints(w) {
    if (w.ghosts.length) return;
    var deadCount = w.points.filter(function (p) { return p.dead; }).length;
    if (deadCount < 32) return;
    var remap = new Map();
    var kept = [];
    w.points.forEach(function (p, i) {
      if (p.dead) return;
      remap.set(i, kept.length);
      p.index = kept.length;
      kept.push(p);
    });
    w.points = kept;
    w.tris = w.tris.map(function (t) { return t.map(function (i) { return remap.get(i); }); });
    w.moments = w.moments.filter(function (m) {
      return m.tri.every(function (i) { return remap.has(i); });
    });
    w.moments.forEach(function (m) { m.tri = m.tri.map(function (i) { return remap.get(i); }); });
  }

  // realDt fades ghosts at a fixed pace; meshDt moves shapes; shimmerDt
  // tilts faces and runs chrome moments.
  function stepWorld(w, realDt, meshDt, shimmerDt, light, camera) {
    w.time += meshDt;
    w.shimmer += shimmerDt;
    if (meshDt > 0) {
      movePoints(w, meshDt, true);
      maintainSpacing(w);
    }
    updateHeights(w);
    if (meshDt > 0) {
      w.flips += flipEdges(w);
      repairInvertedFaces(w);
    }
    w.ghosts.forEach(function (g) { g.opacity -= realDt / GHOST_FADE_SECONDS; });
    w.ghosts = w.ghosts.filter(function (g) { return g.opacity > 0; });
    compactPoints(w);
    w.moments.forEach(function (m) {
      m.age += shimmerDt;
      // A moment whose corner was removed eases out.
      if (m.tri.some(function (i) { return w.points[i].dead; })) {
        m.age = Math.max(m.age, m.duration - CHROME_EASE_SECONDS);
      }
    });
    w.moments = w.moments.filter(function (m) { return m.age < m.duration; });
    if (shimmerDt > 0 && w.moments.length < w.chromeTarget && w.shimmer >= w.nextMomentAt) {
      pickChromeFaces(w, 1, light, camera).forEach(function (face) { startMoment(w, face, false); });
      w.nextMomentAt = w.shimmer + 2 + hashUnit(Math.floor(w.shimmer * 10), 31) * 4;
    }
    applyHeights(w, light, camera);
  }

  function normalOf(a, b, c) {
    var ux = b.x - a.x;
    var uy = b.y - a.y;
    var uz = b.z - a.z;
    var vx = c.x - a.x;
    var vy = c.y - a.y;
    var vz = c.z - a.z;
    var nx = uy * vz - uz * vy;
    var ny = uz * vx - ux * vz;
    var nz = ux * vy - uy * vx;
    if (nz < 0) {
      nx = -nx;
      ny = -ny;
      nz = -nz;
    }
    var len = Math.hypot(nx, ny, nz) || 1;
    return [nx / len, ny / len, nz / len];
  }

  // Outward direction of an edge, lying in the face plane, perpendicular to
  // the edge and pointing away from the opposite corner.
  function edgeOutward(normal, start, end, opposite) {
    var ex = end.x - start.x;
    var ey = end.y - start.y;
    var ez = end.z - start.z;
    var ox = normal[1] * ez - normal[2] * ey;
    var oy = normal[2] * ex - normal[0] * ez;
    var oz = normal[0] * ey - normal[1] * ex;
    var toOpposite = (opposite.x - start.x) * ox + (opposite.y - start.y) * oy + (opposite.z - start.z) * oz;
    if (toOpposite > 0) {
      ox = -ox;
      oy = -oy;
      oz = -oz;
    }
    var len = Math.hypot(ox, oy, oz) || 1;
    return [ox / len, oy / len, oz / len];
  }

  function facePalette() {
    return {
      gold: hexToLinear(settings.accents.gold.color),
      bronze: hexToLinear(settings.accents.bronze.color),
      steel: hexToLinear(settings.metalColor),
      accentNoise: valueNoise(settings.seed ^ 0x27d4eb2f),
    };
  }

  // A face's metal, keyed by its corner identities: tint (gold and bronze
  // gather in loose clusters), reflectance, roughness and brush direction.
  function faceMaterial(w, palette, corners, key) {
    var size = w.size;
    var cx = (corners[0].x + corners[1].x + corners[2].x) / 3;
    var cy = (corners[0].y + corners[1].y + corners[2].y) / 3;
    var accentField = palette.accentNoise(cx / (size * 2.6), cy / (size * 2.6));
    var roll = hashUnit(key[0], key[1], key[2], 1);
    var tint = palette.steel;
    var goldShare = settings.accents.gold.share / 100 * (0.3 + accentField * 1.4);
    var bronzeShare = settings.accents.bronze.share / 100 * (0.3 + (1 - accentField) * 1.4);
    if (roll < goldShare) tint = palette.gold;
    else if (roll < goldShare + bronzeShare) tint = palette.bronze;
    var reflectance = 0.8 + hashUnit(key[0], key[1], key[2], 2) * 0.3;

    // Brush along the face's longest edge, like rolled sheet stock.
    var lengths = [0, 1, 2].map(function (i) {
      var p = corners[i];
      var q = corners[(i + 1) % 3];
      return Math.hypot(q.x - p.x, q.y - p.y);
    });
    var longestIndex = lengths.indexOf(Math.max(lengths[0], lengths[1], lengths[2]));
    var p0 = corners[longestIndex];
    var p1 = corners[(longestIndex + 1) % 3];
    return {
      f0: [tint[0] * reflectance, tint[1] * reflectance, tint[2] * reflectance],
      roughness: 0.07 + hashUnit(key[0], key[1], key[2], 3) * 0.16,
      brushAngle: Math.atan2(p1.y - p0.y, p1.x - p0.x) + (hashUnit(key[0], key[1], key[2], 4) - 0.5) * 0.25,
      brushSeed: hashUnit(key[0], key[1], key[2], 5) * 1000,
    };
  }

  // Packs faces into the vertex buffer: current faces first (opaque), then
  // fading ghosts from newest to oldest, so older ghosts stay on top and the
  // picture never jumps. Face looks come from corner identities, never from
  // the order of triangles.
  // ---------- reveal ----------
  //
  // Onboarding's intro fills the background in: faces fade in from near the
  // middle outward, with a little seeded scatter so the edge of the filled
  // area is ragged rather than a circle. While it runs the faces are packed
  // again every frame. `null` when no reveal runs.
  var reveal = null;
  var REVEAL_FADE = 0.25;

  // A page that cannot yet tell whether its background fills in (Web Chat
  // first asks whether someone is still signed in) marks <html> with
  // data-ua-background-hold. Until the mark goes the faces draw empty, which
  // is the fill's first frame, so the fill starts from nothing. Hiding the
  // canvas instead let the browser show frames drawn while hidden for a
  // moment once it showed. reveal() ends the hold; removing the mark without
  // a reveal shows the faces at once.
  var HOLD_ATTRIBUTE = "data-ua-background-hold";
  var held = document.documentElement.hasAttribute(HOLD_ATTRIBUTE);

  function watchHold() {
    if (!held || !window.MutationObserver) return;
    var watch = new window.MutationObserver(function () {
      if (document.documentElement.hasAttribute(HOLD_ATTRIBUTE)) return;
      watch.disconnect();
      if (!held) return;
      held = false;
      if (world) uploadWorld();
      restart();
    });
    watch.observe(document.documentElement, { attributes: true, attributeFilter: [HOLD_ATTRIBUTE] });
  }

  function revealOpacity(w, t, time) {
    if (held) return 0;
    if (!reveal) return 1;
    var P = w.points;
    var cx = (P[t[0]].x + P[t[1]].x + P[t[2]].x) / 3;
    var cy = (P[t[0]].y + P[t[1]].y + P[t[2]].y) / 3;
    var distance = Math.min(1, Math.hypot(cx / width - 0.5, cy / height - 0.5) / 0.71);
    var delay = distance * 0.6 + hashUnit(P[t[0]].id, P[t[1]].id, P[t[2]].id, 41) * 0.15;
    return smoothstep(0, 1, ((time - reveal.start) / reveal.duration - delay) / REVEAL_FADE);
  }

  function revealing(time) {
    if (reveal && time - reveal.start >= reveal.duration) reveal = null;
    return reveal !== null;
  }

  function packFaces(w) {
    var P = w.points;
    var time = window.performance.now();
    var faces = w.tris.map(function (t) { return { tri: t, opacity: revealOpacity(w, t, time) }; });
    for (var g = w.ghosts.length - 1; g >= 0; g -= 1) faces.push(w.ghosts[g]);
    var triangles = faces.map(function (f) { return f.tri; });
    var normals = triangles.map(function (t) { return normalOf(P[t[0]], P[t[1]], P[t[2]]); });
    var owners = edgeOwnersOf(triangles);

    var palette = facePalette();
    var data = new Float32Array(faces.length * 3 * FLOATS_PER_VERTEX);
    var offset = 0;
    faces.forEach(function (face, index) {
      var t = face.tri;
      var corners = [P[t[0]], P[t[1]], P[t[2]]];
      var n = normals[index];
      var key = faceKey(t, P);

      // Per-edge chamfer: outward in-plane direction (xyz) and the chamfer
      // slope (w). Positive w marks a ridge, negative a valley, 0 the hull.
      // The neighbor is the face across the edge on the other side; current
      // faces are preferred over ghosts.
      var edgeData = [];
      for (var corner = 0; corner < 3; corner += 1) {
        var start = corners[(corner + 1) % 3];
        var end = corners[(corner + 2) % 3];
        var a = t[(corner + 1) % 3];
        var b = t[(corner + 2) % 3];
        var mySide = signedArea(start, end, corners[corner]);
        var far = null;
        (owners.get(a < b ? a + "," + b : b + "," + a) || []).some(function (owner) {
          if (owner.tri === index) return false;
          var candidate = P[triangles[owner.tri][owner.corner]];
          if (signedArea(start, end, candidate) * mySide >= 0) return false;
          far = candidate;
          return true;
        });
        var outward = edgeOutward(n, start, end, corners[corner]);
        if (!far) {
          edgeData.push([outward[0], outward[1], outward[2], 0]);
          continue;
        }
        var signedDistance = n[0] * (far.x - start.x) + n[1] * (far.y - start.y) + n[2] * (far.z - start.z);
        var lo = Math.min(start.id, end.id);
        var hi = Math.max(start.id, end.id);
        var slope = Math.tan((20 + hashUnit(lo, hi, corners[corner].id, 6) * 22) * Math.PI / 180);
        edgeData.push([outward[0], outward[1], outward[2], signedDistance < 0 ? slope : -slope]);
      }

      var material = faceMaterial(w, palette, corners, key);

      corners.forEach(function (p) {
        data[offset++] = p.x;
        data[offset++] = p.y;
        data[offset++] = p.z;
        data[offset++] = n[0];
        data[offset++] = n[1];
        data[offset++] = n[2];
        for (var e = 0; e < 3; e += 1) {
          data[offset++] = edgeData[e][0];
          data[offset++] = edgeData[e][1];
          data[offset++] = edgeData[e][2];
          data[offset++] = edgeData[e][3];
        }
        data[offset++] = material.f0[0];
        data[offset++] = material.f0[1];
        data[offset++] = material.f0[2];
        data[offset++] = material.roughness;
        data[offset++] = material.brushAngle;
        data[offset++] = material.brushSeed;
        data[offset++] = face.opacity;
      });
    });
    return { data: data, count: faces.length * 3 };
  }

  function narrowestVisibleCornerDeg(w) {
    var narrowest = Math.PI;
    w.tris.forEach(function (t) {
      if (!inViewport(baseCenter(w, t), 0)) return;
      var angles = cornerAngles(w.points[t[0]], w.points[t[1]], w.points[t[2]]);
      narrowest = Math.min(narrowest, angles[0], angles[1], angles[2]);
    });
    return Math.round(narrowest * 180 / Math.PI);
  }

  // ---------- shaders ----------

  // Light shared by both shaders. A flat face is a mirror, so the softbox's
  // straight outline can cross the middle of a face and light part of it
  // together with part of its neighbor. The face's light is therefore taken
  // at its three corners and blended across it: each face shows one even
  // gradient, and neighbors differ because they face different ways.
  var SHEEN_STRENGTH = 0.12;
  var LIGHT_GLSL = [
    "uniform vec3 uLightCenter;",
    "uniform vec3 uLightAxisU;",
    "uniform vec3 uLightAxisV;",
    "uniform vec3 uLightNormal;",
    "uniform vec2 uLightSize;",
    "uniform vec4 uLightColor;",
    "uniform float uLightBlur;",
    "const float SHEEN_STRENGTH = " + SHEEN_STRENGTH.toFixed(3) + ";",
    // Reflection of the rectangular softbox. Roughness widens its edges in
    // proportion to the distance travelled, light softness blurs them
    // further, and a dim halo surrounds it.
    "vec3 softbox(vec3 P, vec3 R, float rough) {",
    "  float denom = dot(R, uLightNormal);",
    "  if (abs(denom) < 1e-4) return vec3(0.0);",
    "  float t = dot(uLightCenter - P, uLightNormal) / denom;",
    "  if (t <= 0.0) return vec3(0.0);",
    "  vec3 H = P + R * t - uLightCenter;",
    "  vec2 size = uLightSize;",
    "  vec2 ab = abs(vec2(dot(H, uLightAxisU), dot(H, uLightAxisV))) / size;",
    "  vec2 soft = 0.04 + uLightBlur + rough * t / size;",
    "  vec2 inside = 1.0 - smoothstep(1.0 - soft, 1.0 + soft, ab);",
    // The strip softbox falls off toward its ends; this puts a ramp on faces
    // that reflect the whole light instead of a flat white fill.
    "  vec2 ab2 = min(ab * ab, 1.0);",
    "  float core = inside.x * inside.y * (1.0 - 0.7 * ab2.x) * (1.0 - 0.25 * ab2.y);",
    "  vec2 outside = max(ab - 1.0, 0.0) * size;",
    "  float halo = exp(-dot(outside, outside) / max(1.0, t * t * 0.0256));",
    "  return uLightColor.rgb * (core + halo * uLightColor.a);",
    "}",
    // Brushed metal's broad sheen: a face is brighter the more directly it
    // mirrors the light's middle, rougher faces with a wider, dimmer lobe.
    // It gives every face a brightness of its own, like real 3D lighting,
    // even where it does not reflect the softbox itself.
    "vec3 sheen(vec3 P, vec3 R, float rough) {",
    "  vec3 L = normalize(uLightCenter - P);",
    "  float spread = clamp(1.5 / max(rough, 0.02), 4.0, 40.0);",
    "  return uLightColor.rgb * SHEEN_STRENGTH * pow(max(dot(R, L), 0.0), spread);",
    "}",
    "vec3 faceLight(vec3 P, vec3 R, float rough) {",
    "  return softbox(P, R, rough) + sheen(P, R, rough);",
    "}",
  ];

  var VERTEX_SHADER = [
    "#version 300 es",
    "layout(location=0) in vec3 aPos;",
    "layout(location=1) in vec3 aNormal;",
    "layout(location=2) in vec4 aEdge0;",
    "layout(location=3) in vec4 aEdge1;",
    "layout(location=4) in vec4 aEdge2;",
    "layout(location=5) in vec4 aMaterial;",
    "layout(location=6) in vec2 aBrush;",
    "layout(location=7) in float aOpacity;",
    "uniform vec2 uResolution;",
    "uniform vec3 uCamera;",
  ].concat(LIGHT_GLSL, [
    "out vec3 vPos;",
    "out vec3 vFaceLight;",
    "out vec3 vBary;",
    "flat out vec3 vNormal;",
    "flat out vec4 vEdge0;",
    "flat out vec4 vEdge1;",
    "flat out vec4 vEdge2;",
    "flat out vec4 vMaterial;",
    "flat out vec2 vBrush;",
    "flat out float vOpacity;",
    "void main() {",
    "  int corner = gl_VertexID % 3;",
    "  vBary = vec3(corner == 0, corner == 1, corner == 2);",
    "  vPos = aPos;",
    "  vNormal = aNormal;",
    "  vEdge0 = aEdge0;",
    "  vEdge1 = aEdge1;",
    "  vEdge2 = aEdge2;",
    "  vMaterial = aMaterial;",
    "  vBrush = aBrush;",
    "  vOpacity = aOpacity;",
    "  vec3 V = normalize(uCamera - aPos);",
    "  vFaceLight = faceLight(aPos, reflect(-V, normalize(aNormal)), aMaterial.w);",
    "  vec2 clip = vec2(aPos.x / uResolution.x * 2.0 - 1.0, 1.0 - aPos.y / uResolution.y * 2.0);",
    "  gl_Position = vec4(clip, 0.0, 1.0);",
    "}",
  ]).join("\n");

  var FRAGMENT_SHADER = [
    "#version 300 es",
    "precision highp float;",
    "in vec3 vPos;",
    "in vec3 vBary;",
    "flat in vec3 vNormal;",
    "flat in vec4 vEdge0;",
    "flat in vec4 vEdge1;",
    "flat in vec4 vEdge2;",
    "flat in vec4 vMaterial;",
    "flat in vec2 vBrush;",
    "flat in float vOpacity;",
    "uniform vec2 uResolution;",
    "uniform float uDpr;",
    "uniform vec3 uCamera;",
    "uniform float uExposure;",
    "uniform float uBevelWidth;",
    "uniform float uGrain;",
    "uniform vec3 uRoom;",
    "uniform vec3 uKeyDir;",
  ].concat(LIGHT_GLSL, [
    "in vec3 vFaceLight;",
    "const float EDGE_CONTRAST = 2.5;",
    "const vec3 EDGE_FLOOR = vec3(0.03);",
    "out vec4 outColor;",
    "",
    "float hash12(vec2 p) {",
    "  vec3 p3 = fract(vec3(p.xyx) * 0.1031);",
    "  p3 += dot(p3, p3.yzx + 33.33);",
    "  return fract((p3.x + p3.y) * p3.z);",
    "}",
    "float vnoise(vec2 p) {",
    "  vec2 i = floor(p);",
    "  vec2 f = fract(p);",
    "  vec2 u = f * f * (3.0 - 2.0 * f);",
    "  float a = hash12(i);",
    "  float b = hash12(i + vec2(1.0, 0.0));",
    "  float c = hash12(i + vec2(0.0, 1.0));",
    "  float d = hash12(i + vec2(1.0, 1.0));",
    "  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);",
    "}",
    "",
    // Dark room: dim overhead (behind the camera), brighter toward the
    // horizon, and brightest on the key side, so small tilts between
    // neighboring dark planes still separate them.
    "vec3 room(vec3 R) {",
    "  float horizon = 1.0 - clamp(R.z, 0.0, 1.0);",
    "  vec2 flat2 = normalize(R.xy + vec2(1e-5));",
    "  float toward = 0.5 + 0.5 * dot(flat2, uKeyDir.xy);",
    "  float side = 0.5 + 0.5 * dot(flat2, vec2(-uKeyDir.y, uKeyDir.x));",
    "  float level = 0.45 + pow(horizon, 1.6) * (0.4 + 3.2 * toward * toward) + 0.5 * side * horizon;",
    "  vec3 c = uRoom * level;",
    "  if (R.z < 0.0) c *= 0.3;",
    "  return c;",
    "}",
    "",
    // Accent tints show where the metal is lit. The dim room reflection uses
    // a mostly neutral reflectance so shadowed gold or bronze planes stay
    // gunmetal instead of turning muddy brown. `lit` is the light the surface
    // reflects toward the camera (see faceLight).
    "vec3 shade(vec3 V, vec3 n, vec3 F0, vec3 lit) {",
    "  vec3 R = reflect(-V, n);",
    "  float NdotV = clamp(dot(n, V), 0.0, 1.0);",
    "  float fresnel = pow(1.0 - NdotV, 5.0);",
    "  vec3 F = F0 + (1.0 - F0) * fresnel;",
    "  float grey = dot(F0, vec3(0.2126, 0.7152, 0.0722));",
    "  vec3 Froom = mix(vec3(grey), F0, 0.25) + (1.0 - grey) * fresnel;",
    "  return Froom * room(R) + F * lit;",
    "}",
    "",
    // Extended Reinhard: close to linear in the shadows so dark planes keep
    // their gradients, with a soft shoulder that rolls chrome into white.
    "vec3 tonemap(vec3 x) {",
    "  const float white = 3.2;",
    "  return clamp(x * (1.0 + x / (white * white)) / (1.0 + x), 0.0, 1.0);",
    "}",
    "vec3 toSrgb(vec3 c) {",
    "  return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));",
    "}",
    "",
    "void main() {",
    "  vec3 baseN = normalize(vNormal);",
    // Distance to each edge in CSS pixels, from the barycentric gradients.
    "  vec3 grad = vec3(length(vec2(dFdx(vBary.x), dFdy(vBary.x))),",
    "                   length(vec2(dFdx(vBary.y), dFdy(vBary.y))),",
    "                   length(vec2(dFdx(vBary.z), dFdy(vBary.z))));",
    "  vec3 dist = vBary / max(grad, vec3(1e-6)) / uDpr;",
    "  vec4 edges[3] = vec4[3](vEdge0, vEdge1, vEdge2);",
    // The nearest edge owns the chamfer, which mitres the corners.
    "  int nearest = dist.x < dist.y ? (dist.x < dist.z ? 0 : 2) : (dist.y < dist.z ? 1 : 2);",
    "  vec4 edge = edges[nearest];",
    "  float edgeDistance = dist[nearest];",
    "  float aa = 0.6 / uDpr;",
    "  float chamfer = edge.w == 0.0 ? 0.0 : 1.0 - smoothstep(uBevelWidth - aa, uBevelWidth + aa, edgeDistance);",
    "  vec3 chamferN = normalize(baseN + edge.xyz * abs(edge.w));",
    "  float occlusion = 1.0;",
    "  for (int i = 0; i < 3; i++) {",
    "    if (edges[i].w < 0.0) {",
    "      occlusion *= mix(0.7, 1.0, smoothstep(0.0, uBevelWidth * 2.5, dist[i]));",
    "    }",
    "  }",
    "",
    "  vec3 P = vPos;",
    "  vec3 V = normalize(uCamera - P);",
    "",
    // Brushed grain: long streaks along the brush direction.
    "  float ang = vBrush.x;",
    "  vec2 dir = vec2(cos(ang), sin(ang));",
    "  vec2 q = vec2(dot(P.xy, dir), dot(P.xy, vec2(-dir.y, dir.x)));",
    "  float streak = vnoise(vec2(q.x * 0.004, q.y * 0.9 + vBrush.y)) * 0.55",
    "    + vnoise(vec2(q.x * 0.02, q.y * 2.3 + vBrush.y * 3.1)) * 0.25",
    "    + vnoise(vec2(q.x * 0.0015, q.y * 0.08 + vBrush.y * 0.7)) * 0.2 - 0.5;",
    "  vec3 F0 = vMaterial.rgb * (1.0 + streak * uGrain * 0.3);",
    "",
    // The face and its chamfer are shaded separately and blended by chamfer
    // coverage. Blending normals instead would sweep the reflection through
    // the light inside a sub-pixel band and break hairlines into dashes. The
    // face takes its light from its corners (vFaceLight); the narrow chamfer
    // is lit per pixel so hairlines follow the light closely.
    "  vec3 color = shade(V, baseN, F0, vFaceLight * (1.0 + streak * uGrain * 0.4));",
    "  if (chamfer > 0.0) {",
    // An edge is at most a few times as bright as its plate: dark plates get
    // faint hairlines, lit plates bright ones. Unlimited, a mirror-smooth
    // chamfer catching the light draws a near-white line between two dark
    // plates, which reads as overexposed.
    "    vec3 chamferLit = faceLight(P, reflect(-V, chamferN), 0.05);",
    "    vec3 edgeColor = min(shade(V, chamferN, vMaterial.rgb, chamferLit), color * EDGE_CONTRAST + EDGE_FLOOR);",
    "    color = mix(color, edgeColor, chamfer);",
    "  }",
    "  color *= occlusion;",
    "",
    "  vec2 uv = gl_FragCoord.xy / (uResolution * uDpr);",
    "  float vignette = 1.0 - 0.3 * smoothstep(0.4, 1.1, length((uv - vec2(0.5, 0.5)) * vec2(1.25, 1.0)));",
    "  color *= vignette * uExposure;",
    "  vec3 mapped = toSrgb(tonemap(color));",
    "  float dither = (hash12(gl_FragCoord.xy) + hash12(gl_FragCoord.yx + 17.0) - 1.0) / 255.0;",
    "  outColor = vec4(mapped + dither, vOpacity);",
    "}",
  ]).join("\n");

  function compile(type, source) {
    var shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      var log = gl.getShaderInfoLog(shader);
      gl.deleteShader(shader);
      throw new Error("ua.metallic_mesh shader compile failed: " + log);
    }
    return shader;
  }

  function initProgram() {
    var vs = compile(gl.VERTEX_SHADER, VERTEX_SHADER);
    var fs = compile(gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
    program = gl.createProgram();
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error("ua.metallic_mesh program link failed: " + gl.getProgramInfoLog(program));
    }
    [
      "uResolution", "uDpr", "uCamera", "uExposure", "uBevelWidth",
      "uGrain", "uRoom", "uKeyDir", "uLightCenter", "uLightAxisU", "uLightAxisV",
      "uLightNormal", "uLightSize", "uLightColor", "uLightBlur",
    ].forEach(function (name) {
      uniforms[name] = gl.getUniformLocation(program, name);
    });
    vao = gl.createVertexArray();
    buffer = gl.createBuffer();
    gl.bindVertexArray(vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    var stride = FLOATS_PER_VERTEX * 4;
    var layout = [[0, 3, 0], [1, 3, 3], [2, 4, 6], [3, 4, 10], [4, 4, 14], [5, 4, 18], [6, 2, 22], [7, 1, 24]];
    layout.forEach(function (entry) {
      gl.enableVertexAttribArray(entry[0]);
      gl.vertexAttribPointer(entry[0], entry[1], gl.FLOAT, false, stride, entry[2] * 4);
    });
    gl.bindVertexArray(null);
    // Fading ghost faces blend over the current faces; current faces are opaque.
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  }

  function uploadWorld() {
    if (mode === MODE_FULL) {
      var packed = packFaces(world);
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, packed.data, gl.DYNAMIC_DRAW);
      vertexCount = packed.count;
    }
  }

  function meshStats() {
    return {
      mode: mode,
      triangles: world.tris.length,
      ghosts: world.ghosts.length,
      flips: world.flips,
      births: world.births,
      deaths: world.deaths,
      points: world.points.filter(function (p) { return !p.dead; }).length,
      chromeMoments: world.moments.length,
      narrowestCornerDeg: narrowestVisibleCornerDeg(world),
    };
  }

  function uploadMesh() {
    world = createWorld();
    lastStepTime = 0;
    uploadWorld();
  }

  // ---------- modes and contexts ----------

  // A canvas keeps the first kind of context it hands out, so switching
  // between the WebGL and 2D drawings needs a fresh layer.
  function replaceCanvas() {
    var replacement = createLayer();
    canvas.parentNode.removeChild(canvas);
    canvas = replacement;
    if (api) api.canvas = canvas;
  }

  function startFull() {
    gl = canvas.getContext("webgl2", {
      antialias: true,
      alpha: false,
      depth: false,
      stencil: false,
      premultipliedAlpha: false,
      powerPreference: "low-power",
    });
    if (!gl) return false;
    try {
      initProgram();
    } catch (error) {
      console.warn("ua.metallic_mesh: the WebGL shader could not be prepared", error);
      return false;
    }
    canvas.addEventListener("webglcontextlost", onContextLost);
    canvas.addEventListener("webglcontextrestored", onContextRestored);
    return true;
  }

  // Picks the drawing for the current settings. The full drawing falls back
  // to the basic one, with a warning, when WebGL2 cannot be used.
  function activateMode() {
    var wanted = settings.detail === MODE_BASIC || webglUnavailable ? MODE_BASIC : MODE_FULL;
    if (wanted === mode) return;
    if (mode) replaceCanvas();
    gl = null;
    context2d = null;
    program = null;
    vertexCount = 0;
    width = 0;
    contextLost = false;
    if (wanted === MODE_FULL) {
      if (startFull()) {
        mode = MODE_FULL;
        return;
      }
      webglUnavailable = true;
      console.warn("ua.metallic_mesh: WebGL2 is unavailable; drawing the basic background instead.");
      replaceCanvas();
      gl = null;
    }
    context2d = canvas.getContext("2d");
    mode = MODE_BASIC;
  }

  function onContextLost(event) {
    event.preventDefault();
    contextLost = true;
    if (frameHandle) window.cancelAnimationFrame(frameHandle);
    frameHandle = 0;
    console.warn("ua.metallic_mesh: the graphics context was lost; waiting for the browser to restore it.");
  }

  function onContextRestored() {
    contextLost = false;
    initProgram();
    uploadWorld();
    restart();
  }


  // ---------- light ----------

  function normalize3(v) {
    var len = Math.hypot(v[0], v[1], v[2]) || 1;
    return [v[0] / len, v[1] / len, v[2] / len];
  }

  function cross3(a, b) {
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  }

  function makeSoftbox(center, longAxis, halfLength, halfWidth, color, intensity, halo, blur) {
    var target = [width * 0.5, height * 0.5, 0];
    var facing = normalize3([target[0] - center[0], target[1] - center[1], target[2] - center[2]]);
    var u = normalize3(longAxis);
    var v = normalize3(cross3(facing, u));
    return {
      center: center,
      axisU: u,
      axisV: v,
      normal: normalize3(cross3(u, v)),
      size: [halfLength, halfWidth],
      color: [color[0] * intensity, color[1] * intensity, color[2] * intensity, halo],
      blur: blur,
    };
  }

  function cameraPosition() {
    return [width * 0.5, height * 0.45, Math.hypot(width, height) * 1.15];
  }

  // Where the light rests relative to the screen center, in pixels. The
  // left/right and up/down settings are percentages of the distance from the
  // center to the screen edge: 100 reaches the edge and larger values place
  // the light off screen, where only plates tilted steeply toward it (and
  // the plate edges) catch it. Near the center it lights the plates broadly.
  function restingOffset() {
    return [settings.lightX / 100 * width * 0.5, settings.lightY / 100 * height * 0.5];
  }

  // With Wandering light the light also roams around its resting place on a
  // smooth path that never repeats exactly, by up to the wander distance (in
  // the same percentages), so it lights different parts of the screen over
  // time. The path runs on the reflection clock and starts at the resting
  // place, so still pages show the light where it was set.
  function wanderOffset(seconds) {
    if (!settings.lightWander) return [0, 0];
    var wander = wanderPath(seconds);
    var reach = settings.lightWanderDistance / 100;
    return [wander[0] * reach * width * 0.5, wander[1] * reach * height * 0.5];
  }

  function lightOffset(seconds) {
    var resting = restingOffset();
    var wander = wanderOffset(seconds);
    return [resting[0] + wander[0], resting[1] + wander[1]];
  }

  // A point within about -1..1 on each axis. Each axis sums three slow waves
  // whose speeds do not divide evenly into each other and whose starting
  // points come from the seed; subtracting each wave's start value puts the
  // path at the origin when the clock reads zero. Crossing the range takes
  // about a minute at reflection speed 1.00x.
  var WANDER_WAVES = [[0.051, 0.5], [0.083, 0.3], [0.131, 0.2]];

  function wanderPath(seconds) {
    var phases = mulberry32(settings.seed ^ 0x2545f491);
    return [0, 1].map(function () {
      return WANDER_WAVES.reduce(function (sum, wave) {
        var phase = phases() * Math.PI * 2;
        var rate = wave[0] * (0.85 + phases() * 0.3);
        return sum + wave[1] * (Math.sin(seconds * rate + phase) - Math.sin(phase));
      }, 0);
    });
  }

  // Direction from the screen center toward the light for the dark room's
  // brighter side. It shrinks to nothing as the light nears the center,
  // because a light overhead favors no side of the room.
  function keyDirection(seconds) {
    var offset = lightOffset(seconds);
    var distance = Math.hypot(offset[0], offset[1]);
    if (distance < 1) return [0, 0];
    var reach = Math.min(1, distance / (Math.hypot(width, height) * 0.5));
    return [offset[0] / distance * reach, offset[1] / distance * reach];
  }

  // At this softness the light has its reference size (a long strip about
  // 0.84 by 0.2 screen diagonals). Every 25 above or below doubles or halves
  // it. A larger light is dimmer in proportion to its area, so a big soft
  // light gives even, flatter metal instead of washing the screen out, and
  // above the reference its edges also blur.
  var REFERENCE_SOFTNESS = 25;

  function softnessScale() {
    return Math.pow(2, (settings.lightSoftness - REFERENCE_SOFTNESS) / 25);
  }

  function softnessBlur() {
    return Math.max(0, settings.lightSoftness - REFERENCE_SOFTNESS) / (100 - REFERENCE_SOFTNESS) * 0.5;
  }

  // The studio's single light: a strip softbox at a fixed height, laid across
  // the direction from the screen center (level when it sits over the
  // center). It takes the reflection clock in seconds; accumulating that
  // clock (rather than scaling absolute time) keeps the light from jumping
  // when the speed changes. With Moving light it swings a little around the
  // center and drifts a little sideways and in height, so a light placed
  // over the screen moves as well. The strip turns only with the swing, never
  // with the drift or the wandering, so a light passing the center does not
  // spin.
  function studioLight(seconds) {
    var diag = Math.hypot(width, height);
    var resting = restingOffset();
    var wander = wanderOffset(seconds);
    var swing = Math.sin(seconds * 0.05) * 0.14;
    var cos = Math.cos(swing);
    var sin = Math.sin(swing);
    var restX = resting[0] * cos - resting[1] * sin;
    var restY = resting[0] * sin + resting[1] * cos;
    var distance = Math.hypot(restX, restY);
    var across = distance >= 1 ? [-restY / distance, restX / distance] : [cos, sin];
    var x = restX + wander[0];
    var y = restY + wander[1];
    var scale = softnessScale();
    return makeSoftbox(
      [
        width * 0.5 + x + Math.sin(seconds * 0.037) * width * 0.03,
        height * 0.5 + y + Math.sin(seconds * 0.029) * height * 0.03,
        diag * (0.5 + Math.sin(seconds * 0.031 + 1.3) * 0.06),
      ],
      [across[0], across[1], 0.15],
      diag * 0.42 * scale,
      diag * 0.1 * scale,
      [1.0, 0.98, 0.95],
      4.6 / Math.pow(scale, 2),
      0.035,
      softnessBlur()
    );
  }

  // ---------- basic mode ----------
  //
  // Without WebGL2, or when a person chooses the basic background, the same
  // mesh is drawn once with the 2D canvas. Each face is filled with a
  // gradient sampled from the same studio light at a few points along
  // the direction its brightness changes most, and thin lines mark the
  // edges. There is no motion, grain or bevel.
  //
  // The functions below are a JavaScript copy of `room`, `softbox`, `sheen`,
  // `shade`, `tonemap` and the vignette in the shaders; keep them in step.

  var BASIC_GRADIENT_SAMPLES = 6;
  var BASIC_EDGE_COLOR = "rgba(200, 208, 218, 0.2)";

  function dot3(a, b) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  }

  function roomLight(R, params) {
    var horizon = 1 - Math.max(0, Math.min(1, R[2]));
    var flatLength = Math.hypot(R[0], R[1]) || 1e-5;
    var fx = R[0] / flatLength;
    var fy = R[1] / flatLength;
    var toward = 0.5 + 0.5 * (fx * params.keyDir[0] + fy * params.keyDir[1]);
    var side = 0.5 + 0.5 * (-fx * params.keyDir[1] + fy * params.keyDir[0]);
    var level = 0.45 + Math.pow(horizon, 1.6) * (0.4 + 3.2 * toward * toward) + 0.5 * side * horizon;
    if (R[2] < 0) level *= 0.3;
    return [params.room[0] * level, params.room[1] * level, params.room[2] * level];
  }

  function softboxLight(light, P, R, rough) {
    var denom = dot3(R, light.normal);
    if (Math.abs(denom) < 1e-4) return 0;
    var toCenter = [light.center[0] - P[0], light.center[1] - P[1], light.center[2] - P[2]];
    var t = dot3(toCenter, light.normal) / denom;
    if (t <= 0) return 0;
    var hit = [P[0] + R[0] * t - light.center[0], P[1] + R[1] * t - light.center[1], P[2] + R[2] * t - light.center[2]];
    var a = Math.abs(dot3(hit, light.axisU)) / light.size[0];
    var b = Math.abs(dot3(hit, light.axisV)) / light.size[1];
    var softA = 0.04 + light.blur + rough * t / light.size[0];
    var softB = 0.04 + light.blur + rough * t / light.size[1];
    var inside = (1 - smoothstep(1 - softA, 1 + softA, a)) * (1 - smoothstep(1 - softB, 1 + softB, b));
    var core = inside * (1 - 0.7 * Math.min(a * a, 1)) * (1 - 0.25 * Math.min(b * b, 1));
    var outA = Math.max(a - 1, 0) * light.size[0];
    var outB = Math.max(b - 1, 0) * light.size[1];
    var halo = Math.exp(-(outA * outA + outB * outB) / Math.max(1, t * t * 0.0256));
    return core + halo * light.color[3];
  }

  // Linear radiance reflected toward the camera from point P on a face.
  // Copy of `sheen` in LIGHT_GLSL.
  function sheenLight(light, P, R, rough) {
    var L = normalize3([light.center[0] - P[0], light.center[1] - P[1], light.center[2] - P[2]]);
    var spread = Math.max(4, Math.min(40, 1.5 / Math.max(rough, 0.02)));
    return SHEEN_STRENGTH * Math.pow(Math.max(dot3(R, L), 0), spread);
  }

  function shadePoint(P, n, f0, rough, light, params) {
    var V = normalize3([params.camera[0] - P[0], params.camera[1] - P[1], params.camera[2] - P[2]]);
    var d = dot3(n, V);
    var R = [2 * d * n[0] - V[0], 2 * d * n[1] - V[1], 2 * d * n[2] - V[2]];
    var fresnel = Math.pow(1 - Math.max(0, Math.min(1, d)), 5);
    var grey = 0.2126 * f0[0] + 0.7152 * f0[1] + 0.0722 * f0[2];
    var room = roomLight(R, params);
    var amount = softboxLight(light, P, R, rough) + sheenLight(light, P, R, rough);
    return [0, 1, 2].map(function (c) {
      var F = f0[c] + (1 - f0[c]) * fresnel;
      var Froom = grey + (f0[c] - grey) * 0.25 + (1 - grey) * fresnel;
      return Froom * room[c] + F * light.color[c] * amount;
    });
  }

  function displayColor(color, x, y, params) {
    var ux = (x / width - 0.5) * 1.25;
    var uy = y / height - 0.5;
    var vignette = 1 - 0.3 * smoothstep(0.4, 1.1, Math.hypot(ux, uy));
    var white = 3.2;
    var rgb = color.map(function (value) {
      var v = value * vignette * params.exposure;
      var mapped = Math.max(0, Math.min(1, v * (1 + v / (white * white)) / (1 + v)));
      var srgb = mapped <= 0.0031308 ? mapped * 12.92 : 1.055 * Math.pow(mapped, 1 / 2.4) - 0.055;
      return Math.round(Math.max(0, Math.min(1, srgb)) * 255);
    });
    return "rgb(" + rgb[0] + ", " + rgb[1] + ", " + rgb[2] + ")";
  }

  function luminance(color) {
    return 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2];
  }

  function drawBasic(light, params) {
    var ctx = context2d;
    var P = world.points;
    var palette = facePalette();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, width, height);
    world.tris.forEach(function (t) {
      var corners = [P[t[0]], P[t[1]], P[t[2]]];
      var n = normalOf(corners[0], corners[1], corners[2]);
      var material = faceMaterial(world, palette, corners, faceKey(t, P));
      function planeZ(x, y) {
        return corners[0].z - (n[0] * (x - corners[0].x) + n[1] * (y - corners[0].y)) / n[2];
      }
      // As in the WebGL drawing, the face is shaded at its corners and the
      // colors are blended linearly across it (each channel is a plane
      // through the three corner values).
      var cornerColors = corners.map(function (c) {
        return shadePoint([c.x, c.y, planeZ(c.x, c.y)], n, material.f0, material.roughness, light, params);
      });
      var dx1 = corners[1].x - corners[0].x;
      var dy1 = corners[1].y - corners[0].y;
      var dx2 = corners[2].x - corners[0].x;
      var dy2 = corners[2].y - corners[0].y;
      var det = dx1 * dy2 - dx2 * dy1;
      function shadeAt(x, y) {
        if (!det) return cornerColors[0];
        var u = ((x - corners[0].x) * dy2 - (y - corners[0].y) * dx2) / det;
        var v = (dx1 * (y - corners[0].y) - dy1 * (x - corners[0].x)) / det;
        return [0, 1, 2].map(function (c) {
          return cornerColors[0][c] + (cornerColors[1][c] - cornerColors[0][c]) * u
            + (cornerColors[2][c] - cornerColors[0][c]) * v;
        });
      }
      // Direction in which the face's brightness changes most.
      var lum = cornerColors.map(luminance);
      var gx = det ? ((lum[1] - lum[0]) * dy2 - (lum[2] - lum[0]) * dy1) / det : 1;
      var gy = det ? (dx1 * (lum[2] - lum[0]) - dx2 * (lum[1] - lum[0])) / det : 0;
      var length = Math.hypot(gx, gy);
      var dir = length > 1e-12 ? [gx / length, gy / length] : [1, 0];
      var spans = corners.map(function (c) { return c.x * dir[0] + c.y * dir[1]; });
      var cx = (corners[0].x + corners[1].x + corners[2].x) / 3;
      var cy = (corners[0].y + corners[1].y + corners[2].y) / 3;
      var mid = cx * dir[0] + cy * dir[1];
      var lo = Math.min(spans[0], spans[1], spans[2]) - mid;
      var hi = Math.max(spans[0], spans[1], spans[2]) - mid;
      var x0 = cx + dir[0] * lo;
      var y0 = cy + dir[1] * lo;
      var x1 = cx + dir[0] * hi;
      var y1 = cy + dir[1] * hi;
      var gradient = ctx.createLinearGradient(x0, y0, x1, y1);
      for (var k = 0; k < BASIC_GRADIENT_SAMPLES; k += 1) {
        var f = k / (BASIC_GRADIENT_SAMPLES - 1);
        var x = x0 + (x1 - x0) * f;
        var y = y0 + (y1 - y0) * f;
        gradient.addColorStop(f, displayColor(shadeAt(x, y), x, y, params));
      }
      ctx.beginPath();
      ctx.moveTo(corners[0].x, corners[0].y);
      ctx.lineTo(corners[1].x, corners[1].y);
      ctx.lineTo(corners[2].x, corners[2].y);
      ctx.closePath();
      ctx.fillStyle = gradient;
      ctx.globalAlpha = revealOpacity(world, t, window.performance.now());
      ctx.fill();
      ctx.globalAlpha = 1;
    });
    if (reveal) return;
    ctx.beginPath();
    edgeOwnersOf(world.tris).forEach(function (owners, key) {
      var ends = key.split(",");
      var a = P[Number(ends[0])];
      var b = P[Number(ends[1])];
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
    });
    ctx.strokeStyle = BASIC_EDGE_COLOR;
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  // ---------- drawing ----------

  function resize() {
    var nextWidth = window.innerWidth;
    var nextHeight = window.innerHeight;
    // Cap the drawing buffer so very large high-density screens stay cheap;
    // the hairline width is computed from this scale, so edges stay correct.
    var nextDpr = Math.min(
      2,
      window.devicePixelRatio || 1,
      Math.sqrt(MAX_RENDER_PIXELS / Math.max(1, nextWidth * nextHeight))
    );
    if (nextWidth === width && nextHeight === height && nextDpr === dpr && world) return;
    width = nextWidth;
    height = nextHeight;
    dpr = nextDpr;
    canvas.width = Math.max(1, Math.round(width * dpr));
    canvas.height = Math.max(1, Math.round(height * dpr));
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    uploadMesh();
  }

  function meshMotionEnabled() {
    return settings.meshMotion && !staticSurface && !reducedMotion.matches;
  }

  function lightMotionEnabled() {
    return settings.lightMotion && !staticSurface && !reducedMotion.matches;
  }

  function shadingParams(seconds) {
    return {
      exposure: 0.3 + settings.shine / 100 * 1.4,
      room: hexToLinear(settings.surfaceColor),
      keyDir: keyDirection(seconds),
      camera: cameraPosition(),
    };
  }

  function draw(time) {
    // Only hide inline; the page's own rules (such as print styles) decide
    // the rest.
    canvas.style.display = enabled ? "" : "none";
    if (!enabled || contextLost) return;
    // WebGL is prepared the first time the dark background is shown.
    if (!mode) activateMode();
    resize();
    var params = shadingParams(world ? world.shimmer : 0);
    if (mode === MODE_BASIC) {
      drawBasic(studioLight(world.shimmer), params);
      return;
    }
    if (frozenTime === null) {
      var realDt = lastStepTime ? Math.min(MAX_STEP_SECONDS, (time - lastStepTime) / 1000) : 0;
      lastStepTime = time;
      var meshDt = meshMotionEnabled() ? realDt * settings.meshSpeed / 100 : 0;
      var shimmerDt = lightMotionEnabled() ? realDt * settings.lightSpeed / 100 : 0;
      if (meshDt > 0 || shimmerDt > 0 || (realDt > 0 && world.ghosts.length)) {
        stepWorld(world, realDt, meshDt, shimmerDt, studioLight(world.shimmer + shimmerDt), cameraPosition());
        uploadWorld();
      }
    }
    if (reveal) {
      // Faces fade in through their packed opacity; the last pack after the
      // reveal ends leaves every face fully opaque.
      revealing(window.performance.now());
      uploadWorld();
    }
    var light = studioLight(world.shimmer);
    // The step above may have moved the light; the room follows it.
    params.keyDir = keyDirection(world.shimmer);
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.useProgram(program);
    gl.uniform2f(uniforms.uResolution, width, height);
    gl.uniform1f(uniforms.uDpr, dpr);
    gl.uniform3f(uniforms.uCamera, params.camera[0], params.camera[1], params.camera[2]);
    gl.uniform1f(uniforms.uExposure, params.exposure);
    gl.uniform1f(uniforms.uBevelWidth, settings.edgeHighlights > 0 ? 0.5 + settings.edgeHighlights / 100 * 1.6 : 0);
    gl.uniform1f(uniforms.uGrain, settings.surfaceGrain / 100);
    gl.uniform3f(uniforms.uRoom, params.room[0], params.room[1], params.room[2]);
    gl.uniform3f(uniforms.uKeyDir, params.keyDir[0], params.keyDir[1], 0);
    gl.uniform3fv(uniforms.uLightCenter, light.center);
    gl.uniform3fv(uniforms.uLightAxisU, light.axisU);
    gl.uniform3fv(uniforms.uLightAxisV, light.axisV);
    gl.uniform3fv(uniforms.uLightNormal, light.normal);
    gl.uniform2fv(uniforms.uLightSize, light.size);
    gl.uniform4fv(uniforms.uLightColor, light.color);
    gl.uniform1f(uniforms.uLightBlur, light.blur);
    gl.bindVertexArray(vao);
    gl.drawArrays(gl.TRIANGLES, 0, vertexCount);
    gl.bindVertexArray(null);
  }

  function animating() {
    if (!enabled || contextLost) return false;
    if (revealing(window.performance.now())) return true;
    return mode === MODE_FULL && frozenTime === null
      && (meshMotionEnabled() || lightMotionEnabled());
  }

  function loop(now) {
    frameHandle = 0;
    if (now - lastFrameTime >= FRAME_INTERVAL_MS) {
      lastFrameTime = now;
      draw(now);
    }
    if (animating()) frameHandle = window.requestAnimationFrame(loop);
  }

  function restart() {
    if (frameHandle) window.cancelAnimationFrame(frameHandle);
    frameHandle = 0;
    draw(frozenTime === null ? window.performance.now() : frozenTime);
    if (animating()) frameHandle = window.requestAnimationFrame(loop);
  }

  // Settings that change the layout itself rebuild the mesh; the rest only
  // change how it is drawn or how it moves.
  var LAYOUT_KEYS = ["seed", "facetSize", "facetDepth", "chromeFaces", "metalColor", "accents"];

  function applySettings(candidate, nextEnabled) {
    var next = normalizeSettings(candidate);
    var rebuild = LAYOUT_KEYS.some(function (key) {
      return JSON.stringify(next[key]) !== JSON.stringify(settings[key]);
    });
    if (!rebuild && nextEnabled === enabled && JSON.stringify(next) === JSON.stringify(settings)) return;
    settings = next;
    enabled = nextEnabled;
    if (mode) activateMode();
    if (rebuild) world = null;
    restart();
  }

  window.addEventListener("resize", restart);
  reducedMotion.addEventListener("change", restart);
  // appearance.js announces every real appearance change, including the
  // switch between light and dark mode.
  document.addEventListener("ua:appearance-change", function (event) {
    var appearance = event.detail && event.detail.appearance;
    applySettings(appearance && appearance.dark, Boolean(appearance && appearance.mode === "dark"));
  });

  api = {
    host: host,
    canvas: canvas,
    defaults: cloneDefaults,
    normalize: normalizeSettings,
    update: function (candidate, nextEnabled) {
      applySettings(candidate, nextEnabled !== false);
    },
    // Fades the faces in over `duration` ms (onboarding's intro, Web Chat's
    // sign-in greeting); it ends a hold.
    reveal: function (duration) {
      held = false;
      reveal = { start: window.performance.now(), duration: Math.max(200, Number(duration) || 1800) };
      restart();
    },
    // Inspection and stepping hooks for the background lab
    // (ui/background-playground) and visual checks. Pages do not use them.
    lab: {
      mode: function () { return mode; },
      settings: function () { return JSON.parse(JSON.stringify(settings)); },
      world: function () { return world; },
      stats: function () { return world ? meshStats() : null; },
      light: function () {
        return { camera: cameraPosition(), light: studioLight(world ? world.shimmer : 0) };
      },
      // Stops the clock; `null` resumes it. Drawing still follows settings.
      freeze: function (frozen) {
        frozenTime = frozen ? window.performance.now() : null;
        restart();
      },
      // Advances the mesh by some real seconds in fixed steps, at the
      // current mesh and reflection speeds.
      advance: function (seconds) {
        var step = 1 / 24;
        for (var elapsed = 0; elapsed < seconds; elapsed += step) {
          var meshDt = meshMotionEnabled() ? step * settings.meshSpeed / 100 : 0;
          var shimmerDt = lightMotionEnabled() ? step * settings.lightSpeed / 100 : 0;
          stepWorld(world, step, meshDt, shimmerDt, studioLight(world.shimmer + shimmerDt), cameraPosition());
        }
        uploadWorld();
        draw(window.performance.now());
      },
    },
  };
  window.UAMetallicMesh = api;
  watchHold();
  restart();
})();
