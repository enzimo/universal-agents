(function () {
  "use strict";

  // Controls and measurements for the dark background lab. The renderer is
  // the product file; this script only edits its settings and reads its
  // `lab` inspection hooks.

  var mesh = window.UAMetallicMesh;
  if (!mesh) return;

  var RANGES = [
    ["facetSize", "Facet size", 140, 520, " px"],
    ["facetDepth", "Facet depth", 0, 100, "%"],
    ["chromeFaces", "Chrome faces", 0, 20, "%"],
    ["shine", "Light strength", 0, 100, "%"],
    ["edgeHighlights", "Edge highlights", 0, 100, "%"],
    ["surfaceGrain", "Brushed grain", 0, 100, "%"],
    ["lightX", "Light left/right", -300, 300, "%"],
    ["lightY", "Light up/down", -300, 300, "%"],
    ["lightSoftness", "Light softness", 0, 100, "%"],
    ["lightWanderDistance", "Wander distance", 0, 150, "%"],
    ["lightSpeed", "Reflection speed", 10, 400, "%"],
    ["meshSpeed", "Mesh speed", 10, 400, "%"],
  ];
  var COLORS = [
    ["surfaceColor", "Surface color"],
    ["metalColor", "Metal color"],
  ];
  var ACCENTS = [["gold", "Gold"], ["bronze", "Bronze"]];
  var TOGGLES = [
    ["lightMotion", "Moving light"],
    ["lightWander", "Wandering light"],
    ["meshMotion", "Moving mesh"],
  ];

  function current() {
    return mesh.lab.settings();
  }

  function apply(change) {
    var next = current();
    Object.keys(change).forEach(function (key) { next[key] = change[key]; });
    mesh.update(next, true);
    showSettingsText();
  }

  // Share settings uses the product's one text form (UAAppearance.settingsText):
  // the lab's dark settings as a whole dark-mode appearance.
  function settingsText() {
    return window.UAAppearance.settingsText({ mode: "dark", dark: current() });
  }

  function showSettingsText() {
    document.getElementById("settings-text").value = settingsText();
  }

  function setStatus(message) {
    document.getElementById("status").textContent = message;
  }

  function heading(text) {
    var element = document.createElement("h2");
    element.textContent = text;
    return element;
  }

  function rangeRow(label, value, min, max, unit, onInput) {
    var row = document.createElement("label");
    row.className = "row";
    var name = document.createElement("span");
    name.textContent = label;
    var output = document.createElement("output");
    var input = document.createElement("input");
    input.type = "range";
    input.min = String(min);
    input.max = String(max);
    input.value = String(value);
    output.textContent = value + unit;
    input.addEventListener("input", function () {
      output.textContent = input.value + unit;
      onInput(Number(input.value));
    });
    row.append(name, output, input);
    return row;
  }

  function colorRow(label, value, onInput) {
    var row = document.createElement("label");
    row.className = "row";
    var name = document.createElement("span");
    name.textContent = label;
    var input = document.createElement("input");
    input.type = "color";
    input.value = value;
    var code = document.createElement("code");
    code.textContent = value;
    input.addEventListener("input", function () {
      code.textContent = input.value;
      onInput(input.value);
    });
    row.append(name, input, code);
    return row;
  }

  function checkRow(label, checked, onChange) {
    var row = document.createElement("label");
    row.className = "check";
    var input = document.createElement("input");
    input.type = "checkbox";
    input.checked = checked;
    input.addEventListener("change", function () { onChange(input.checked); });
    var name = document.createElement("span");
    name.textContent = label;
    row.append(input, name);
    return row;
  }

  function render() {
    var settings = current();
    var root = document.getElementById("controls");
    root.textContent = "";
    root.append(heading("Background Detail"));
    root.append(checkRow("Basic background (still, no WebGL)", settings.detail === "basic", function (on) {
      apply({ detail: on ? "basic" : "full" });
      setStatus("Drawing: " + mesh.lab.mode());
    }));
    root.append(heading("Shape and Light"));
    RANGES.forEach(function (entry) {
      root.append(rangeRow(entry[1], settings[entry[0]], entry[2], entry[3], entry[4], function (value) {
        var change = {};
        change[entry[0]] = value;
        apply(change);
      }));
    });
    root.append(heading("Motion"));
    TOGGLES.forEach(function (entry) {
      root.append(checkRow(entry[1], settings[entry[0]], function (on) {
        var change = {};
        change[entry[0]] = on;
        apply(change);
      }));
    });
    root.append(heading("Colors"));
    COLORS.forEach(function (entry) {
      root.append(colorRow(entry[1], settings[entry[0]], function (value) {
        var change = {};
        change[entry[0]] = value;
        apply(change);
      }));
    });
    ACCENTS.forEach(function (entry) {
      var name = entry[0];
      root.append(colorRow(entry[1] + " tint", settings.accents[name].color, function (value) {
        var accents = current().accents;
        accents[name].color = value;
        apply({ accents: accents });
      }));
      root.append(rangeRow(entry[1] + " faces", settings.accents[name].share, 0, 50, "%", function (value) {
        var accents = current().accents;
        accents[name].share = value;
        apply({ accents: accents });
      }));
    });
  }

  document.getElementById("regenerate").addEventListener("click", function () {
    apply({ seed: 1 + Math.floor(Math.random() * 4294967294) });
    setStatus("New mesh layout.");
  });
  document.getElementById("mock-toggle").addEventListener("click", function () {
    var mock = document.getElementById("mock");
    mock.hidden = !mock.hidden;
  });
  var frozen = false;
  document.getElementById("freeze").addEventListener("click", function (event) {
    frozen = !frozen;
    mesh.lab.freeze(frozen);
    event.currentTarget.textContent = frozen ? "Resume" : "Freeze";
  });
  // Browsers refuse clipboard access on plain-HTTP pages other than
  // localhost; there the text is selected for the person to copy.
  function selectSettingsText(message) {
    var output = document.getElementById("settings-text");
    output.focus();
    output.select();
    setStatus(message);
  }

  document.getElementById("copy").addEventListener("click", function () {
    var text = document.getElementById("settings-text").value;
    if (!navigator.clipboard || !window.isSecureContext) {
      selectSettingsText("The browser blocks the clipboard here. The text is selected: copy it with Ctrl+C (Cmd+C on a Mac).");
      return;
    }
    navigator.clipboard.writeText(text).then(function () {
      setStatus("Settings copied.");
    }, function () {
      selectSettingsText("The browser refused clipboard access. The text is selected: copy it with Ctrl+C (Cmd+C on a Mac).");
    });
  });
  document.getElementById("apply").addEventListener("click", function () {
    var parsed;
    try {
      parsed = window.UAAppearance.readSettingsText(document.getElementById("settings-text").value);
    } catch (error) {
      setStatus(error.message);
      return;
    }
    var start = window.UAAppearance.normalize({ mode: "dark", dark: current() });
    var next = window.UAAppearance.normalize(parsed, start);
    mesh.update(next.dark, true);
    render();
    showSettingsText();
    setStatus("Settings applied.");
  });
  document.getElementById("reset").addEventListener("click", function () {
    mesh.update(mesh.defaults(), true);
    render();
    showSettingsText();
    setStatus("Defaults restored.");
  });
  document.getElementById("hide").addEventListener("click", function () {
    document.getElementById("panel").hidden = true;
    document.getElementById("show").hidden = false;
  });
  document.getElementById("show").addEventListener("click", function () {
    document.getElementById("panel").hidden = false;
    document.getElementById("show").hidden = true;
  });

  // Face size balance and shape for faces whose centers are on screen:
  // each face's area against the median of its neighbors and of all visible
  // faces, and the narrowest corner. Used by visual checks.
  window.labSizeStats = function () {
    var w = mesh.lab.world();
    var P = w.points;
    function area(t) {
      return Math.abs((P[t[1]].x - P[t[0]].x) * (P[t[2]].y - P[t[0]].y)
        - (P[t[1]].y - P[t[0]].y) * (P[t[2]].x - P[t[0]].x)) / 2;
    }
    function angle(p, q, r) {
      var ux = q.x - p.x;
      var uy = q.y - p.y;
      var vx = r.x - p.x;
      var vy = r.y - p.y;
      var cos = (ux * vx + uy * vy) / Math.hypot(ux, uy) / Math.hypot(vx, vy);
      return Math.acos(Math.max(-1, Math.min(1, cos))) * 180 / Math.PI;
    }
    var owners = new Map();
    w.tris.forEach(function (t, i) {
      for (var c = 0; c < 3; c += 1) {
        var a = t[(c + 1) % 3];
        var b = t[(c + 2) % 3];
        var key = a < b ? a + "," + b : b + "," + a;
        if (!owners.has(key)) owners.set(key, []);
        owners.get(key).push(i);
      }
    });
    var areas = w.tris.map(area);
    var visible = [];
    w.tris.forEach(function (t, i) {
      var cx = (P[t[0]].x + P[t[1]].x + P[t[2]].x) / 3;
      var cy = (P[t[0]].y + P[t[1]].y + P[t[2]].y) / 3;
      if (cx > 0 && cy > 0 && cx < window.innerWidth && cy < window.innerHeight) visible.push(i);
    });
    var neighborRatios = visible.map(function (i) {
      var t = w.tris[i];
      var near = [];
      for (var c = 0; c < 3; c += 1) {
        var a = t[(c + 1) % 3];
        var b = t[(c + 2) % 3];
        owners.get(a < b ? a + "," + b : b + "," + a).forEach(function (j) {
          if (j !== i) near.push(areas[j]);
        });
      }
      near.sort(function (x, y) { return x - y; });
      return areas[i] / near[Math.floor(near.length / 2)];
    }).sort(function (x, y) { return x - y; });
    var sorted = visible.map(function (i) { return areas[i]; }).sort(function (x, y) { return x - y; });
    var median = sorted[Math.floor(sorted.length / 2)];
    var narrowest = Math.min.apply(null, visible.map(function (i) {
      var c = w.tris[i].map(function (k) { return P[k]; });
      return Math.min(angle(c[0], c[1], c[2]), angle(c[1], c[2], c[0]), angle(c[2], c[0], c[1]));
    }));
    return {
      visible: visible.length,
      maxNeighborRatio: +neighborRatios[neighborRatios.length - 1].toFixed(2),
      maxOverMedian: +(sorted[sorted.length - 1] / median).toFixed(2),
      minOverMedian: +(sorted[0] / median).toFixed(2),
      narrowestDeg: Math.round(narrowest),
    };
  };

  // Renders one still frame and posts the canvas pixels to `/upload` on the
  // serving host. Only a capture-aware lab server accepts it; a plain static
  // server answers with an error and nothing is saved.
  window.labCapture = function (name, format) {
    mesh.lab.freeze(true);
    var type = format === "png" ? "image/png" : "image/jpeg";
    return new Promise(function (resolve) {
      mesh.canvas.toBlob(function (blob) {
        fetch("/upload?name=" + encodeURIComponent(name), {
          method: "POST", body: blob, headers: { "Content-Type": type },
        }).then(function (response) { return response.text(); }).then(resolve, resolve);
      }, type, 0.93);
    });
  };

  render();
  showSettingsText();
})();
