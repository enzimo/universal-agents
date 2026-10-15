"""Shared HTML shell for secure tool broker authentication pages."""

from __future__ import annotations

import base64
import html
import json
import os
from datetime import datetime, timezone
from typing import Mapping
from pathlib import Path

from runtime_hosting.page_icon import favicon_link_tag
from runtime_settings.appearance import dark_appearance_css_declarations

BROKER_APPEARANCE_ENV = "SECURE_TOOL_BROKER_APPEARANCE_JSON"

BROKER_AUTH_THEME = """
<style>
  :root {
    color-scheme: light;
    --accent-low: rgba(22, 163, 74, 0.12);
    --accent: #0f766e;
    --accent-high: #075985;
    --bg: #f8faf7;
    --nav-bg: rgba(247, 255, 252, 0.62);
    --surface: rgba(255, 255, 255, 0.70);
    --surface-soft: rgba(255, 255, 255, 0.56);
    --text: #334942;
    --text-strong: #10201c;
    --text-muted: #466159;
    --line: rgba(28, 69, 60, 0.16);
    --line-strong: rgba(15, 118, 110, 0.38);
    --broker-chip-bg: rgba(220, 252, 231, 0.76);
    --broker-chip-border: rgba(22, 163, 74, 0.32);
    --broker-chip-text: #14532d;
    --code-bg: rgba(255, 255, 255, 0.58);
    --code-panel: rgba(255, 255, 255, 0.46);
    --shadow: 0 34px 120px rgba(3, 28, 42, 0.22);
    --danger: #b91c1c;
    --danger-text: #7f1d1d;
    --danger-low: rgba(185, 28, 28, 0.10);
    --content-width: 50rem;
  }
  :root[data-color-mode="dark"] {
    color-scheme: dark;
    --accent-low: rgba(89, 185, 209, 0.14);
    --accent: #8bc8d7;
    --accent-high: #bce8f0;
    --bg: #030202;
    --nav-bg: rgba(8, 10, 11, 0.82);
    --surface: rgba(12, 15, 16, 0.84);
    --surface-soft: rgba(15, 18, 19, 0.72);
    --text: #c9d4d0;
    --text-strong: #edf3f0;
    --text-muted: #a9b8b3;
    --line: rgba(222, 235, 229, 0.16);
    --line-strong: rgba(139, 200, 215, 0.36);
    --broker-chip-bg: rgba(21, 69, 78, 0.72);
    --broker-chip-border: rgba(139, 200, 215, 0.30);
    --broker-chip-text: #d4f1f5;
    --code-bg: rgba(3, 5, 6, 0.72);
    --code-panel: rgba(7, 9, 10, 0.66);
    --shadow: 0 34px 120px rgba(0, 0, 0, 0.64);
    --danger: #b94850;
    --danger-text: #f3c4c7;
    --danger-low: rgba(185, 72, 80, 0.18);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: Figtree, Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.75;
  }
  a { color: var(--accent-high); text-underline-offset: 3px; overflow-wrap: anywhere; }
  a:hover { color: var(--text-strong); }
  .site-header {
    border-bottom: 1px solid var(--line);
    background: var(--nav-bg);
    padding: 0.85rem 1.5rem;
  }
  .header-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 1rem;
    max-width: var(--content-width);
    margin: 0 auto;
  }
  .brand {
    display: inline-flex;
    align-items: center;
    gap: 0.55rem;
    color: var(--text-strong);
    font-weight: 700;
    text-decoration: none;
    white-space: nowrap;
  }
  .brand-mark {
    width: 1.55rem;
    height: 1.55rem;
    object-fit: contain;
    flex: 0 0 auto;
  }
  .search-chip {
    padding: 0.45rem 0.75rem;
    border: 1px solid var(--broker-chip-border, var(--line));
    border-radius: 8px;
    color: var(--broker-chip-text, var(--text-muted));
    background: var(--broker-chip-bg, var(--surface));
    font-size: 0.875rem;
  }
  main { max-width: var(--content-width); margin: 0 auto; padding: 2.5rem 1.5rem 4rem; }
  .content-panel { padding: 1.5rem 0; }
  .content-panel + .content-panel { border-top: 1px solid var(--line); }
  .eyebrow {
    margin: 0 0 0.75rem;
    color: var(--accent-high);
    font-size: 0.8125rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    line-height: 1.4;
    text-transform: uppercase;
  }
  h1, h2, h3 {
    color: var(--text-strong);
    font-weight: 650;
    letter-spacing: 0;
    line-height: 1.2;
  }
  h1 { margin: 0; max-width: 16ch; font-size: 2.625rem; }
  h2 { margin: 0 0 1rem; font-size: 2rem; }
  h3 { margin: 1.2rem 0 0.5rem; font-size: 1.25rem; }
  p { margin: 0; }
  p + p { margin-top: 1rem; }
  .lede { max-width: 46rem; color: var(--text); font-size: 1.125rem; }
  .note, .status {
    border-left: 0.25rem solid var(--accent);
    background: var(--accent-low);
    color: var(--text-strong);
    padding: 1rem;
  }
  .steps {
    counter-reset: steps;
    display: grid;
    gap: 0;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .step {
    counter-increment: steps;
    position: relative;
    min-height: 2.25rem;
    padding: 0 0 1.25rem 2.75rem;
  }
  .step + .step { margin-top: 0.25rem; }
  .step::before {
    content: counter(steps);
    position: absolute;
    top: 0;
    left: 0;
    width: 1.75rem;
    height: 1.75rem;
    border-radius: 999rem;
    box-shadow: inset 0 0 0 1px var(--line);
    background: var(--surface-soft);
    color: var(--text-strong);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 0.8125rem;
    font-weight: 700;
    line-height: 1;
  }
  .step::after {
    content: "";
    position: absolute;
    top: 2.1rem;
    bottom: 0.35rem;
    left: 0.875rem;
    width: 1px;
    background: var(--line);
  }
  .step:last-child::after { display: none; }
  form {
    display: grid;
    gap: 1rem;
    margin-top: 1rem;
  }
  fieldset {
    margin: 0;
    padding: 1rem;
    border: 1px solid var(--line);
    border-radius: 8px;
  }
  legend { color: var(--text-strong); font-weight: 700; }
  label {
    display: block;
    margin-bottom: 0.35rem;
    color: var(--text-strong);
    font-weight: 700;
  }
  input, textarea, select {
    width: 100%;
    max-width: 42rem;
    padding: 0.65rem 0.75rem;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--surface);
    color: var(--text-strong);
    font: inherit;
  }
  .broker-confirmation {
    display: grid;
    grid-template-columns: 1.125rem minmax(0, 1fr);
    align-items: start;
    gap: 0.65rem;
    max-width: 42rem;
    color: var(--text-strong);
    cursor: pointer;
  }
  .broker-confirmation input[type="checkbox"] {
    width: 1.125rem;
    height: 1.125rem;
    margin: 0.3rem 0 0;
    padding: 0;
    accent-color: #0284c7;
  }
  .broker-confirmation span {
    min-width: 0;
    color: inherit;
  }
  /* Same inline eye button as the onboarding form's credential fields
     (assistant_gateway/onboarding_web/assets/styles.css .credential-reveal-button). */
  .secret-input-row {
    position: relative;
    display: grid;
    align-items: center;
    max-width: 42rem;
  }
  .secret-input-row input {
    min-width: 0;
    max-width: none;
    padding-right: 52px;
  }
  .secret-toggle {
    position: absolute;
    top: 50%;
    right: 5px;
    width: 34px;
    height: 32px;
    min-height: 0;
    display: grid;
    place-items: center;
    padding: 0;
    border: 1px solid rgba(7, 89, 133, 0.34);
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.52);
    color: #075985;
    box-shadow: none;
    transform: translateY(-50%);
  }
  .secret-toggle:hover,
  .secret-toggle:focus-visible {
    border-color: rgba(7, 89, 133, 0.68);
    outline: none;
  }
  .secret-toggle svg {
    width: 17px;
    height: 17px;
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 2;
  }
  :root[data-color-mode="dark"] .secret-toggle {
    border-color: #367287;
    background: #10232d;
    color: #f2f7f8;
  }
  :root[data-color-mode="dark"] .secret-toggle:hover,
  :root[data-color-mode="dark"] .secret-toggle:focus-visible {
    border-color: #f0a35a;
    background: #173440;
  }
  textarea { min-height: 12rem; resize: vertical; }
  input:focus, textarea:focus, select:focus {
    outline: 2px solid color-mix(in srgb, var(--accent-high) 45%, transparent);
    outline-offset: 2px;
    border-color: var(--accent-high);
  }
  button, .button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: fit-content;
    min-height: 2.5rem;
    padding: 0.55rem 1.1rem;
    border: 1px solid var(--accent-high);
    border-radius: 999rem;
    background: var(--accent-high);
    color: var(--bg);
    font: inherit;
    font-weight: 700;
    line-height: 1.2;
    cursor: pointer;
  }
  .page-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    margin-top: 1.25rem;
  }
  .auth-open-button { width: 7rem; }
  .auth-open-button:disabled {
    cursor: wait;
    background: var(--surface-soft);
    border-color: var(--line-strong);
    color: var(--text-muted);
  }
  .secondary-action {
    border-color: var(--line-strong);
    background: var(--surface-soft);
    color: var(--text-muted);
    box-shadow: none;
  }
  .secondary-action:hover {
    color: var(--text-strong);
    border-color: var(--line-strong);
  }
  /* A failed action, shown just above the form it came from (ui/DESIGN.md). */
  .status.status-error,
  .status-error {
    border-left: 0.25rem solid var(--danger);
    background: var(--danger-low);
    color: var(--danger-text);
    padding: 1rem;
    margin-bottom: 1rem;
  }
  .status-success { margin-bottom: 1rem; }
  .close-page-help {
    margin-top: 0.75rem;
    color: var(--text-muted);
    font-size: 0.9375rem;
  }
  .code-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
    margin: 1.25rem 0;
  }
  .code-value {
    color: var(--text-strong);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.35rem 0.6rem;
    font-size: 1.125rem;
    font-weight: 700;
    letter-spacing: 0.04em;
  }
  .copy-button {
    width: 2.5rem;
    padding: 0;
    border-radius: 8px;
  }
  code, pre {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  }
  code {
    margin-block: -0.125rem;
    padding: 0.125rem 0.375rem;
    background: var(--code-bg);
    font-size: 0.875rem;
  }
  pre {
    margin: 0.75rem 0 0;
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.75rem 1rem;
    background: var(--code-panel);
    color: var(--text-strong);
    font-size: 0.875rem;
    line-height: 1.55;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  pre code { all: unset; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }
  details {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 1rem;
    background: var(--surface);
  }
  summary {
    cursor: pointer;
    color: var(--text-strong);
    font-weight: 700;
  }
  details ol { margin-bottom: 0; }
  dl { display: grid; gap: 0.5rem; }
  dt { color: var(--text-muted); font-size: 0.75rem; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; }
  dd { margin: 0 0 0.75rem; }
  .ua-markdown > :first-child { margin-top: 0; }
  .ua-markdown > :last-child { margin-bottom: 0; }
  .ua-markdown p { margin: 0 0 1rem; }
  .ua-markdown ul,
  .ua-markdown ol { margin: 0.5rem 0 1rem; padding-left: 1.4rem; }
  .ua-markdown blockquote {
    margin: 0.75rem 0;
    padding-left: 0.9rem;
    border-left: 0.25rem solid var(--line-strong);
    color: var(--text-muted);
  }
  .message-table-wrap { overflow-x: auto; margin: 0.75rem 0; }
  .message-table-wrap table { width: 100%; border-collapse: collapse; font-size: 0.92rem; }
  .message-table-wrap th,
  .message-table-wrap td { border: 1px solid var(--line); padding: 0.45rem 0.55rem; text-align: left; vertical-align: top; }
  .message-table-wrap th { background: var(--surface-soft); color: var(--text-strong); }
  html {
    min-height: 100%;
    background:
      radial-gradient(circle at 16% 14%, color-mix(in srgb, var(--ua-doc-light-blue) 46%, transparent) 0, transparent 36%),
      radial-gradient(circle at 84% 14%, color-mix(in srgb, var(--ua-doc-light-yellow) 48%, transparent) 0, transparent 38%),
      radial-gradient(circle at 18% 86%, color-mix(in srgb, var(--ua-doc-light-green) 38%, transparent) 0, transparent 40%),
      radial-gradient(circle at 86% 86%, color-mix(in srgb, var(--ua-doc-light-red) 26%, transparent) 0, transparent 42%),
      var(--bg);
  }
  body {
    min-height: 100vh;
    background: transparent;
    color: var(--text);
  }
  .ua-doc-static-mesh {
    position: fixed;
    inset: 0;
    z-index: 0;
    width: 100vw;
    height: 100vh;
    pointer-events: none;
  }
  .site-header,
  main {
    position: relative;
    z-index: 1;
  }
  .site-header {
    border-bottom: 1px solid var(--line);
    background: var(--nav-bg);
    backdrop-filter: blur(24px) saturate(140%);
    -webkit-backdrop-filter: blur(24px) saturate(140%);
  }
  main {
    margin-top: 2rem;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--surface);
    box-shadow: var(--shadow);
    backdrop-filter: blur(24px) saturate(140%);
    -webkit-backdrop-filter: blur(24px) saturate(140%);
  }
  .content-panel + .content-panel {
    border-top-color: var(--line);
  }
  .note,
  .status,
  fieldset,
  input,
  textarea,
  select,
  .step::before,
  .code-value,
  pre,
  details {
    background: var(--surface-soft);
    border-color: var(--line);
    backdrop-filter: blur(18px) saturate(135%);
    -webkit-backdrop-filter: blur(18px) saturate(135%);
  }
  button,
  .button {
    border-color: #075985;
    background: linear-gradient(135deg, #075985, #0284c7);
    color: #ffffff;
  }
  .secondary-action {
    border-color: var(--line-strong);
    background: rgba(255, 255, 255, 0.58);
    color: var(--text-strong);
  }
  h1 {
    max-width: none;
  }
  @media (max-width: 50rem) {
    .site-header { padding: 0.65rem 1rem; }
    .search-chip { display: none; }
    main { padding: 1.5rem 1rem 3rem; }
    h1 { max-width: none; font-size: 2rem; }
    h2 { font-size: 1.625rem; }
    .secret-input-row { align-items: stretch; }
  }
</style>
"""


BROKER_AUTH_MESH_SCRIPT = """
<script>
(function () {
  "use strict";
  var canvas = document.createElement("canvas");
  var ctx = canvas.getContext("2d");
  if (!ctx) return;
  var triangles = [];
  var width = 0;
  var height = 0;
  canvas.className = "ua-doc-static-mesh";
  canvas.setAttribute("aria-hidden", "true");
  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }
  function cssNumber(name, fallback) {
    var value = parseFloat(cssVar(name, String(fallback)));
    return Number.isFinite(value) ? value : fallback;
  }
  function alpha(value, fallback) {
    var next = Number.isFinite(value) ? value : fallback;
    return next > 1 ? Math.max(0, Math.min(1, next / 100)) : Math.max(0, Math.min(1, next));
  }
  function hexToRgb(hex, fallback) {
    var value = String(hex || fallback || "#202426").trim();
    if (!/^#[0-9a-f]{6}$/i.test(value)) value = fallback || "#202426";
    return [parseInt(value.slice(1, 3), 16), parseInt(value.slice(3, 5), 16), parseInt(value.slice(5, 7), 16)];
  }
  function seeded(seed) {
    var value = seed >>> 0;
    return function () {
      value = (value * 1664525 + 1013904223) >>> 0;
      return value / 4294967296;
    };
  }
  function makeTriangles(nextWidth, nextHeight) {
    var random = seeded(9187);
    var cols = nextWidth < 700 ? 7 : 13;
    var rows = nextHeight < 700 ? 7 : 10;
    var cellWidth = nextWidth / cols;
    var cellHeight = nextHeight / rows;
    var grid = [];
    var y;
    var x;
    for (y = 0; y <= rows; y += 1) {
      var line = [];
      for (x = 0; x <= cols; x += 1) {
        line.push({
          x: x * cellWidth + (x === 0 || x === cols ? 0 : (random() - 0.5) * cellWidth * 0.72),
          y: y * cellHeight + (y === 0 || y === rows ? 0 : (random() - 0.5) * cellHeight * 0.72)
        });
      }
      grid.push(line);
    }
    var next = [];
    for (y = 0; y < rows; y += 1) {
      for (x = 0; x < cols; x += 1) {
        var a = grid[y][x];
        var b = grid[y][x + 1];
        var c = grid[y + 1][x];
        var d = grid[y + 1][x + 1];
        if (random() > 0.5) {
          next.push({ points: [a, b, d], tone: random() });
          next.push({ points: [a, d, c], tone: random() });
        } else {
          next.push({ points: [a, b, c], tone: random() });
          next.push({ points: [b, d, c], tone: random() });
        }
      }
    }
    return next;
  }
  function drawGradient(nextWidth, nextHeight) {
    var colors = [
      cssVar("--ua-doc-light-blue", "#082f73"),
      cssVar("--ua-doc-light-yellow", "#ffd60a"),
      cssVar("--ua-doc-light-green", "#20f05a"),
      cssVar("--ua-doc-light-red", "#ff3030")
    ];
    var points = [[0, 0], [nextWidth, 0], [0, nextHeight], [nextWidth, nextHeight]];
    points.forEach(function (point, index) {
      var gradient = ctx.createRadialGradient(point[0], point[1], 0, point[0], point[1], Math.max(nextWidth, nextHeight) * 0.78);
      gradient.addColorStop(0, colors[index]);
      gradient.addColorStop(1, "rgba(248, 250, 247, 0)");
      ctx.globalAlpha = index === 3 ? 0.28 : 0.46;
      ctx.fillStyle = gradient;
      ctx.fillRect(0, 0, nextWidth, nextHeight);
    });
    ctx.globalAlpha = 1;
  }
  function draw() {
    if (document.body && canvas.parentElement !== document.body) {
      document.body.insertBefore(canvas, document.body.firstChild);
    }
    var nextWidth = window.innerWidth || document.documentElement.clientWidth || 1024;
    var nextHeight = window.innerHeight || document.documentElement.clientHeight || 768;
    var scale = window.devicePixelRatio || 1;
    if (nextWidth !== width || nextHeight !== height) {
      width = nextWidth;
      height = nextHeight;
      triangles = makeTriangles(width, height);
      canvas.width = Math.floor(width * scale);
      canvas.height = Math.floor(height * scale);
      canvas.style.width = width + "px";
      canvas.style.height = height + "px";
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
    }
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#f8faf7";
    ctx.fillRect(0, 0, width, height);
    drawGradient(width, height);
    ctx.save();
    ctx.globalCompositeOperation = "multiply";
    var low = alpha(cssNumber("--ua-doc-polygon-opacity-low", 0.2), 0.2);
    var high = alpha(cssNumber("--ua-doc-polygon-opacity-high", 0.7), 0.7);
    triangles.forEach(function (triangle) {
      var shade = Math.floor(86 + triangle.tone * 116);
      var opacity = low + (1 - triangle.tone) * (high - low);
      ctx.beginPath();
      triangle.points.forEach(function (point, index) {
        if (index === 0) ctx.moveTo(point.x, point.y);
        else ctx.lineTo(point.x, point.y);
      });
      ctx.closePath();
      ctx.fillStyle = "rgba(" + shade + ", " + shade + ", " + shade + ", " + opacity.toFixed(3) + ")";
      ctx.fill();
    });
    ctx.restore();
    var line = hexToRgb(cssVar("--ua-doc-polygon-line-color", "#202426"), "#202426");
    var lineOpacity = alpha(cssNumber("--ua-doc-polygon-line-opacity", 0.08), 0.08);
    if (lineOpacity > 0) {
      ctx.strokeStyle = "rgba(" + line[0] + ", " + line[1] + ", " + line[2] + ", " + lineOpacity.toFixed(3) + ")";
      triangles.forEach(function (triangle) {
        ctx.beginPath();
        triangle.points.forEach(function (point, index) {
          if (index === 0) ctx.moveTo(point.x, point.y);
          else ctx.lineTo(point.x, point.y);
        });
        ctx.closePath();
        ctx.stroke();
      });
    }
  }
  window.addEventListener("resize", draw);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", draw);
  else draw();
})();
</script>
"""


def _ua_logo_data_uri() -> str:
    logo_path = (
        Path(__file__).resolve().parent.parent
        / "assistant_gateway"
        / "onboarding_web"
        / "assets"
        / "agent-mark.png"
    )
    try:
        encoded = base64.b64encode(logo_path.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f"data:image/png;base64,{encoded}"


def _ua_brand_markup() -> str:
    logo_uri = _ua_logo_data_uri()
    logo = (
        f'<img class="brand-mark" src="{html.escape(logo_uri, quote=True)}" alt="" aria-hidden="true">'
        if logo_uri
        else ""
    )
    return f'<a class="brand" href="/health">{logo}<span>Universal Agents</span></a>'


def _css_hex(value: object, fallback: str) -> str:
    candidate = str(value or "").strip()
    if len(candidate) == 7 and candidate.startswith("#"):
        try:
            int(candidate[1:], 16)
        except ValueError:
            return fallback
        return candidate.lower()
    return fallback


def _css_int(value: object, fallback: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, parsed))


def runtime_theme_css_from_appearance(appearance: Mapping[str, object] | None) -> str:
    source = appearance if isinstance(appearance, Mapping) else {}
    color_mode = (
        "light"
        if str(source.get("color_mode") or "").strip().lower() == "light"
        else "dark"
    )
    raw_colors = source.get("light_colors")
    colors = list(raw_colors) if isinstance(raw_colors, list) else []
    defaults = ["#082f73", "#ffd60a", "#20f05a", "#ff3030"]
    normalized_colors = [
        _css_hex(colors[index] if index < len(colors) else "", fallback)
        for index, fallback in enumerate(defaults)
    ]
    diffusion = _css_int(source.get("light_diffusion"), 58, 0, 100)
    opacity_low = _css_int(source.get("polygon_opacity_low"), 20, 0, 100)
    opacity_high = _css_int(source.get("polygon_opacity_high"), 70, 0, 100)
    if opacity_low > opacity_high:
        opacity_low = opacity_high
    line_color = _css_hex(source.get("polygon_line_color"), "#202426")
    line_opacity = _css_int(source.get("polygon_line_opacity"), 8, 0, 100)
    return "\n".join(
        [
            "<style>",
            ":root {",
            f"  --ua-runtime-color-mode: {color_mode};",
            f"  --ua-doc-light-blue: {normalized_colors[0]};",
            f"  --ua-doc-light-yellow: {normalized_colors[1]};",
            f"  --ua-doc-light-green: {normalized_colors[2]};",
            f"  --ua-doc-light-red: {normalized_colors[3]};",
            f"  --ua-doc-light-diffusion: {diffusion};",
            f"  --ua-doc-polygon-opacity-low: {opacity_low / 100:.3f};",
            f"  --ua-doc-polygon-opacity-high: {opacity_high / 100:.3f};",
            f"  --ua-doc-polygon-line-color: {line_color};",
            f"  --ua-doc-polygon-line-opacity: {line_opacity / 100:.3f};",
            f"  --ua-light-dynamic: {'true' if bool(source.get('dynamic_background', True)) else 'false'};",
            *dark_appearance_css_declarations(source.get("dark")),
            "}",
            "</style>",
            "",
        ]
    )


def _broker_appearance_from_env() -> dict[str, object]:
    raw_value = os.environ.get(BROKER_APPEARANCE_ENV)
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _broker_auth_mesh_script() -> str:
    repository_root = Path(__file__).resolve().parent.parent
    docs_assets = repository_root / "user_docs_source" / "assets"
    # The dark background renderer has one source file, served by the gateway.
    asset_paths = (
        docs_assets / "appearance.js",
        docs_assets / "static-mesh.js",
        repository_root / "assistant_gateway" / "onboarding_web" / "assets" / "metallic_mesh.js",
    )
    scripts: list[str] = []
    for asset_path in asset_paths:
        try:
            scripts.append(asset_path.read_text(encoding="utf-8"))
        except OSError:
            return BROKER_AUTH_MESH_SCRIPT
    return "".join(f"<script>\n{script}\n</script>" for script in scripts)


def markdown_data_attr(value: object) -> str:
    return html.escape(str(value or "").strip(), quote=True)


def render_markdown_text(value: object, *, class_name: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    classes = "ua-markdown" + (f" {html.escape(class_name, quote=True)}" if class_name else "")
    return f"<div class=\"{classes}\" data-markdown=\"{markdown_data_attr(text)}\"></div>"


def _broker_markdown_script() -> str:
    renderer_path = (
        Path(__file__).resolve().parent.parent
        / "assistant_gateway"
        / "onboarding_web"
        / "assets"
        / "chat_markdown.js"
    )
    try:
        renderer = renderer_path.read_text(encoding="utf-8")
    except OSError:
        renderer = ""
    return f"""<script>
{renderer}
(function () {{
  "use strict";
  function renderer() {{
    return window.UAWebChatMarkdown && typeof window.UAWebChatMarkdown.render === "function"
      ? window.UAWebChatMarkdown
      : null;
  }}
  function renderInto(node, text) {{
    var active = renderer();
    if (active) {{
      node.innerHTML = active.render(text || "");
    }} else {{
      node.textContent = text || "";
    }}
  }}
  window.UABrokerRenderMarkdown = renderInto;
  function renderAll() {{
    document.querySelectorAll("[data-markdown]").forEach(function (node) {{
      renderInto(node, node.getAttribute("data-markdown") || "");
    }});
  }}
  if (document.readyState === "loading") {{
    document.addEventListener("DOMContentLoaded", renderAll);
  }} else {{
    renderAll();
  }}
}})();
</script>"""


def render_close_page_button(label: str = "Close Page") -> str:
    escaped_label = html.escape(label)
    return (
        "<div class=\"page-actions\">"
        f"<button type=\"button\" onclick=\"window.close()\">{escaped_label}</button>"
        "</div>"
        "<p class=\"close-page-help\">If your browser blocks the close action, close this tab manually.</p>"
    )


def render_session_countdown(expires_at: datetime, *, label: str = "Session expires in") -> str:
    normalized_expires_at = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=timezone.utc)
    expires_at_attr = html.escape(normalized_expires_at.isoformat(), quote=True)
    remaining_html = html.escape(session_remaining_text(normalized_expires_at))
    escaped_label = html.escape(label)
    return (
        f"<p>{escaped_label} "
        f"<strong data-session-countdown data-expires-at=\"{expires_at_attr}\">{remaining_html}</strong>."
        "</p>"
    )


def session_remaining_text(expires_at: datetime) -> str:
    remaining = expires_at - datetime.now(timezone.utc)
    total_seconds = max(0, int(remaining.total_seconds() + 0.999))
    if total_seconds <= 0:
        return "expired"
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


SESSION_COUNTDOWN_SCRIPT = """
<script>
(() => {
  function formatRemaining(ms) {
    if (!Number.isFinite(ms) || ms <= 0) {
      return "expired";
    }
    const seconds = Math.ceil(ms / 1000);
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = seconds % 60;
    return minutes + ":" + String(remainingSeconds).padStart(2, "0");
  }
  function updateCountdowns() {
    const now = Date.now();
    for (const node of document.querySelectorAll("[data-session-countdown]")) {
      const expiresAt = Date.parse(node.getAttribute("data-expires-at") || "");
      node.textContent = formatRemaining(expiresAt - now);
    }
  }
  if (document.querySelector("[data-session-countdown]")) {
    updateCountdowns();
    window.setInterval(updateCountdowns, 1000);
  }
})();
</script>
"""


# Reveal/hide for masked inputs (ui/DESIGN.md, Controls) and the browser's time zone for forms that ask.
SECRET_INPUT_TOGGLE_SCRIPT = """
<script>
(() => {
  for (const button of document.querySelectorAll("[data-secret-toggle]")) {
    const targetId = button.getAttribute("aria-controls") || "";
    const input = document.getElementById(targetId);
    if (!input) {
      continue;
    }
    button.addEventListener("click", () => {
      const reveal = input.type === "password";
      input.type = reveal ? "text" : "password";
      button.setAttribute("aria-pressed", reveal ? "true" : "false");
    });
  }
  const browserTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  if (browserTimeZone) {
    for (const input of document.querySelectorAll("[data-user-timezone]")) {
      input.value = browserTimeZone;
    }
  }
})();
</script>
"""


# The onboarding form's reveal button label and eye icon (app.js renderRevealableControl).
SECRET_TOGGLE_LABEL = "Show or hide value"
SECRET_TOGGLE_EYE_ICON = (
    '<svg viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"></path>'
    '<circle cx="12" cy="12" r="3"></circle></svg>'
)


def render_credential_input(
    *,
    input_id: str,
    input_type: str,
    autocomplete: str,
    value: str = "",
    placeholder: str = "",
    required: bool = False,
    name: str | None = None,
) -> str:
    """Render an escaped credential field with the shared local reveal control.

    ``name`` defaults to the id; pages with several forms posting the same
    field give each field its own id and the shared name.
    """
    escaped_id = html.escape(input_id)
    escaped_name = html.escape(name or input_id)
    placeholder_attr = (
        f' placeholder="{html.escape(placeholder)}"' if placeholder else ""
    )
    required_attr = " required" if required else ""
    field = (
        f'<input id="{escaped_id}" name="{escaped_name}" '
        f'type="{html.escape(input_type)}" autocomplete="{html.escape(autocomplete)}" '
        f'value="{html.escape(value)}"{placeholder_attr}{required_attr}>'
    )
    if input_type != "password":
        return field
    return (
        '<div class="secret-input-row">'
        f'{field}<button class="secret-toggle" type="button" data-secret-toggle '
        f'aria-controls="{escaped_id}" aria-pressed="false" '
        f'title="{SECRET_TOGGLE_LABEL}" aria-label="{SECRET_TOGGLE_LABEL}">'
        f"{SECRET_TOGGLE_EYE_ICON}</button></div>"
    )


def render_broker_auth_page(title: str, body: str, *, script: str = "") -> str:
    countdown_script = SESSION_COUNTDOWN_SCRIPT if "data-session-countdown" in body else ""
    runtime_theme = runtime_theme_css_from_appearance(_broker_appearance_from_env())
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title>{favicon_link_tag()}{runtime_theme}{BROKER_AUTH_THEME}</head>"
        "<body><canvas class=\"ua-doc-static-mesh\" data-ua-metallic-mesh aria-hidden=\"true\"></canvas>"
        "<header class=\"site-header\"><div class=\"header-row\">"
        f"{_ua_brand_markup()}"
        "<div class=\"search-chip\">Secure tool broker authentication</div>"
        f"</div></header><main>{body}</main>{_broker_auth_mesh_script()}{_broker_markdown_script()}{countdown_script}{script}</body></html>"
    )
