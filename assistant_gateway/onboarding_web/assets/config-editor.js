(function () {
  "use strict";

  var authPanel = document.getElementById("config-editor-auth");
  var editorApp = document.getElementById("config-editor-app");
  var authForm = document.getElementById("config-editor-auth-form");
  var tokenInput = document.getElementById("config-editor-token");
  var tokenReveal = document.getElementById("config-editor-token-reveal");
  var authError = document.getElementById("config-editor-auth-error");
  var pathLabel = document.getElementById("config-editor-path");
  var yamlEditor = document.getElementById("config-editor-yaml");
  var highlightLayer = document.getElementById("config-editor-highlight");
  var saveButton = document.getElementById("config-editor-save");
  var reloadButton = document.getElementById("config-editor-reload");
  var downloadButton = document.getElementById("config-editor-download");
  var backupsList = document.getElementById("config-editor-backups");
  var restartConfirm = document.getElementById("config-editor-restart-confirm");
  var restartButton = document.getElementById("config-editor-restart");
  var statusLine = document.getElementById("config-editor-status");
  var preferredArrowColumn = null;
  var restartRequested = false;

  function showStatus(message, isError) {
    statusLine.textContent = message || "";
    statusLine.hidden = !message;
    statusLine.classList.toggle("is-error", Boolean(isError));
  }

  function showAuthError(message) {
    authError.textContent = message || "";
    authError.hidden = !message;
  }

  function revealPassword(input, button) {
    var isVisible = input.type === "text";
    input.type = isVisible ? "password" : "text";
    button.setAttribute("aria-pressed", isVisible ? "false" : "true");
  }

  function parseResponse(response) {
    return response.text().then(function (body) {
      var payload = {};
      try {
        payload = body ? JSON.parse(body) : {};
      } catch (error) {
        payload = {};
      }
      if (!response.ok) {
        throw new Error(payload.message || payload.error || body || response.statusText || "Request failed.");
      }
      return payload;
    });
  }

  function normalizeHexColor(value, fallback) {
    if (/^#[0-9a-f]{6}$/i.test(value || "")) {
      return value;
    }
    return fallback;
  }

  function normalizePercent(value, fallback) {
    var parsed = Number(value);
    if (!Number.isFinite(parsed)) {
      return fallback;
    }
    return Math.max(0, Math.min(100, parsed));
  }

  function setRootVariable(name, value) {
    document.documentElement.style.setProperty(name, String(value));
  }

  function redrawStaticMesh() {
    try {
      window.dispatchEvent(new Event("resize"));
    } catch (error) {
      var resizeEvent = document.createEvent("Event");
      resizeEvent.initEvent("resize", true, true);
      window.dispatchEvent(resizeEvent);
    }
  }

  function applyRuntimeTheme(appearance) {
    if (!appearance || typeof appearance !== "object") {
      return;
    }
    if (window.UAAppearance && typeof window.UAAppearance.setBaseline === "function") {
      window.UAAppearance.setBaseline(appearance);
    }
    var defaults = ["#082f73", "#ffd60a", "#20f05a", "#ff3030"];
    var colors = Array.isArray(appearance.light_colors) ? appearance.light_colors : [];
    var opacityLow = normalizePercent(appearance.polygon_opacity_low, 20);
    var opacityHigh = normalizePercent(appearance.polygon_opacity_high, 70);
    if (opacityLow > opacityHigh) {
      opacityLow = opacityHigh;
    }
    setRootVariable("--ua-doc-light-blue", normalizeHexColor(colors[0], defaults[0]));
    setRootVariable("--ua-doc-light-yellow", normalizeHexColor(colors[1], defaults[1]));
    setRootVariable("--ua-doc-light-green", normalizeHexColor(colors[2], defaults[2]));
    setRootVariable("--ua-doc-light-red", normalizeHexColor(colors[3], defaults[3]));
    setRootVariable("--ua-doc-light-diffusion", normalizePercent(appearance.light_diffusion, 58));
    setRootVariable("--ua-doc-polygon-opacity-low", (opacityLow / 100).toFixed(3));
    setRootVariable("--ua-doc-polygon-opacity-high", (opacityHigh / 100).toFixed(3));
    setRootVariable("--ua-doc-polygon-line-color", normalizeHexColor(appearance.polygon_line_color, "#202426"));
    setRootVariable("--ua-doc-polygon-line-opacity", (normalizePercent(appearance.polygon_line_opacity, 8) / 100).toFixed(3));
    var polygonSeed = Number.parseInt(appearance.polygon_seed, 10);
    setRootVariable("--ua-light-mesh-seed", Number.isFinite(polygonSeed) ? String(polygonSeed) : "4147");
    redrawStaticMesh();
  }

  function loadRuntimeTheme() {
    window.fetch("/api/web-chat/settings", {
      cache: "no-store",
      credentials: "same-origin",
    }).then(parseResponse).then(function (payload) {
      applyRuntimeTheme(payload.appearance);
    }).catch(function () {
      redrawStaticMesh();
    });
  }

  function loadUserAppearance() {
    if (
      window.UAAppearance
      && typeof window.UAAppearance.loadServerPreference === "function"
    ) {
      return window.UAAppearance.loadServerPreference().catch(function () {
        return null;
      });
    }
    return Promise.resolve(null);
  }

  function formatTimestamp(value) {
    var date = new Date(value || "");
    if (Number.isNaN(date.getTime())) {
      return "";
    }
    return date.toLocaleString();
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function wrapYamlToken(className, value) {
    return '<span class="' + className + '">' + escapeHtml(value) + "</span>";
  }

  function findYamlCommentIndex(line) {
    var quote = "";
    var index;
    for (index = 0; index < line.length; index += 1) {
      var character = line.charAt(index);
      if (quote) {
        if (character === "\\" && quote === '"') {
          index += 1;
        } else if (character === quote) {
          quote = "";
        }
      } else if (character === '"' || character === "'") {
        quote = character;
      } else if (character === "#") {
        return index;
      }
    }
    return -1;
  }

  function highlightYamlScalar(value) {
    var match = String(value || "").match(/^(\s*)(.*?)(\s*)$/);
    var leading = match ? match[1] : "";
    var scalar = match ? match[2] : value;
    var trailing = match ? match[3] : "";
    var className = "";
    if (!scalar) {
      return escapeHtml(value);
    }
    if (/^["']/.test(scalar)) {
      className = "config-editor-yaml-token-string";
    } else if (/^(true|false)$/i.test(scalar)) {
      className = "config-editor-yaml-token-bool";
    } else if (/^(null|~)$/i.test(scalar)) {
      className = "config-editor-yaml-token-null";
    } else if (/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:e[+-]?\d+)?$/i.test(scalar)) {
      className = "config-editor-yaml-token-number";
    } else if (/^[&*][A-Za-z0-9_.-]+$/.test(scalar)) {
      className = "config-editor-yaml-token-anchor";
    }
    if (!className) {
      return escapeHtml(value);
    }
    return escapeHtml(leading) + wrapYamlToken(className, scalar) + escapeHtml(trailing);
  }

  function highlightYamlCode(code) {
    var keyMatch = String(code || "").match(/^(\s*(?:-\s*)?)([^:\s][^:]*?)(\s*:(?:\s+|$))(.*)$/);
    if (keyMatch) {
      return escapeHtml(keyMatch[1])
        + wrapYamlToken("config-editor-yaml-token-key", keyMatch[2])
        + wrapYamlToken("config-editor-yaml-token-punctuation", keyMatch[3])
        + highlightYamlScalar(keyMatch[4]);
    }
    var listMatch = String(code || "").match(/^(\s*)(-\s+)(.*)$/);
    if (listMatch) {
      return escapeHtml(listMatch[1])
        + wrapYamlToken("config-editor-yaml-token-punctuation", listMatch[2])
        + highlightYamlScalar(listMatch[3]);
    }
    return highlightYamlScalar(code);
  }

  function highlightYamlLine(line) {
    var commentIndex = findYamlCommentIndex(line);
    if (commentIndex === -1) {
      return highlightYamlCode(line);
    }
    return highlightYamlCode(line.slice(0, commentIndex))
      + wrapYamlToken("config-editor-yaml-token-comment", line.slice(commentIndex));
  }

  function syncHighlightScroll() {
    highlightLayer.scrollTop = yamlEditor.scrollTop;
    highlightLayer.scrollLeft = yamlEditor.scrollLeft;
  }

  function syncEditorHighlight() {
    preferredArrowColumn = null;
    var lines = String(yamlEditor.value || "").split("\n");
    highlightLayer.innerHTML = lines.map(highlightYamlLine).join("\n") + "\n";
    syncHighlightScroll();
  }

  function setYamlEditorValue(value, selectionStart, selectionEnd) {
    yamlEditor.value = value;
    yamlEditor.setSelectionRange(selectionStart, selectionEnd);
    syncEditorHighlight();
  }

  function selectedLineBounds() {
    var value = yamlEditor.value;
    var start = yamlEditor.selectionStart;
    var end = yamlEditor.selectionEnd;
    var effectiveEnd = end;
    if (effectiveEnd > start && value.charAt(effectiveEnd - 1) === "\n") {
      effectiveEnd -= 1;
    }
    var lineStart = value.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
    var lineEnd = value.indexOf("\n", effectiveEnd);
    if (lineEnd === -1) {
      lineEnd = value.length;
    }
    return {
      start: start,
      end: end,
      lineStart: lineStart,
      lineEnd: lineEnd,
    };
  }

  function lineStartAt(value, index) {
    return value.lastIndexOf("\n", Math.max(0, index - 1)) + 1;
  }

  function lineEndAt(value, index) {
    var lineEnd = value.indexOf("\n", index);
    return lineEnd === -1 ? value.length : lineEnd;
  }

  function lineNumberAt(value, index) {
    return value.slice(0, index).split("\n").length - 1;
  }

  function scrollEditorCaretIntoView(index) {
    var styles = window.getComputedStyle(yamlEditor);
    var lineHeight = parseFloat(styles.lineHeight) || 21;
    var paddingTop = parseFloat(styles.paddingTop) || 0;
    var lineNumber = lineNumberAt(yamlEditor.value, index);
    var caretTop = paddingTop + lineNumber * lineHeight;
    var caretBottom = caretTop + lineHeight;
    if (caretTop < yamlEditor.scrollTop) {
      yamlEditor.scrollTop = Math.max(0, caretTop - paddingTop);
    } else if (caretBottom > yamlEditor.scrollTop + yamlEditor.clientHeight) {
      yamlEditor.scrollTop = caretBottom - yamlEditor.clientHeight + paddingTop;
    }
    syncHighlightScroll();
  }

  function moveSelectionVertically(direction, extendSelection) {
    var value = yamlEditor.value;
    var selectionDirection = yamlEditor.selectionDirection || "none";
    var anchor;
    var focus;
    var currentLineStart;
    var currentLineEnd;
    var targetLineStart;
    var targetLineEnd;
    var target;

    if (extendSelection && yamlEditor.selectionStart !== yamlEditor.selectionEnd) {
      anchor = selectionDirection === "backward" ? yamlEditor.selectionEnd : yamlEditor.selectionStart;
      focus = selectionDirection === "backward" ? yamlEditor.selectionStart : yamlEditor.selectionEnd;
    } else {
      anchor = yamlEditor.selectionStart;
      focus = direction < 0 ? yamlEditor.selectionStart : yamlEditor.selectionEnd;
    }

    currentLineStart = lineStartAt(value, focus);
    currentLineEnd = lineEndAt(value, focus);
    if (preferredArrowColumn === null) {
      preferredArrowColumn = focus - currentLineStart;
    }

    if (direction < 0) {
      if (currentLineStart === 0) {
        target = Math.min(preferredArrowColumn, currentLineEnd);
      } else {
        targetLineEnd = currentLineStart - 1;
        targetLineStart = lineStartAt(value, targetLineEnd);
        target = Math.min(targetLineStart + preferredArrowColumn, targetLineEnd);
      }
    } else if (currentLineEnd >= value.length) {
      target = Math.min(currentLineStart + preferredArrowColumn, value.length);
    } else {
      targetLineStart = currentLineEnd + 1;
      targetLineEnd = lineEndAt(value, targetLineStart);
      target = Math.min(targetLineStart + preferredArrowColumn, targetLineEnd);
    }

    if (extendSelection) {
      yamlEditor.setSelectionRange(
        Math.min(anchor, target),
        Math.max(anchor, target),
        target < anchor ? "backward" : "forward"
      );
    } else {
      yamlEditor.setSelectionRange(target, target, "none");
    }
    scrollEditorCaretIntoView(target);
  }

  function transformSelectedLines(transformLine) {
    var value = yamlEditor.value;
    var bounds = selectedLineBounds();
    var block = value.slice(bounds.lineStart, bounds.lineEnd);
    var lines = block.split("\n");
    var transformed = [];
    var lineOffset = bounds.lineStart;
    var startAdjustment = 0;
    var endAdjustment = 0;
    lines.forEach(function (line) {
      var nextLine = transformLine(line);
      var diff = nextLine.length - line.length;
      if (lineOffset < bounds.start) {
        startAdjustment += diff;
      }
      if (lineOffset < bounds.end) {
        endAdjustment += diff;
      }
      transformed.push(nextLine);
      lineOffset += line.length + 1;
    });
    var nextValue = value.slice(0, bounds.lineStart) + transformed.join("\n") + value.slice(bounds.lineEnd);
    var nextStart = Math.max(bounds.lineStart, bounds.start + startAdjustment);
    var nextEnd = Math.max(nextStart, bounds.end + endAdjustment);
    setYamlEditorValue(nextValue, nextStart, nextEnd);
  }

  function insertAtSelection(text) {
    var value = yamlEditor.value;
    var start = yamlEditor.selectionStart;
    var end = yamlEditor.selectionEnd;
    setYamlEditorValue(value.slice(0, start) + text + value.slice(end), start + text.length, start + text.length);
  }

  function insertNewlineAtSelection() {
    var value = yamlEditor.value;
    var start = yamlEditor.selectionStart;
    var lineStart = lineStartAt(value, start);
    var currentLine = value.slice(lineStart, start);
    var indentMatch = currentLine.match(/^\s*/);
    insertAtSelection("\n" + (indentMatch ? indentMatch[0] : ""));
  }

  function indentSelection() {
    if (yamlEditor.selectionStart === yamlEditor.selectionEnd) {
      insertAtSelection("  ");
      return;
    }
    transformSelectedLines(function (line) {
      return "  " + line;
    });
  }

  function outdentSelection() {
    transformSelectedLines(function (line) {
      if (line.indexOf("  ") === 0) {
        return line.slice(2);
      }
      if (line.indexOf(" ") === 0) {
        return line.slice(1);
      }
      return line;
    });
  }

  function isCommentedYamlLine(line) {
    return !line.trim() || /^\s*#/.test(line);
  }

  function toggleCommentSelection() {
    var value = yamlEditor.value;
    var bounds = selectedLineBounds();
    var block = value.slice(bounds.lineStart, bounds.lineEnd);
    var lines = block.split("\n");
    var hasText = lines.some(function (line) {
      return Boolean(line.trim());
    });
    var shouldUncomment = hasText && lines.every(isCommentedYamlLine);
    transformSelectedLines(function (line) {
      if (!line.trim()) {
        return line;
      }
      if (shouldUncomment) {
        return line.replace(/^(\s*)# ?/, "$1");
      }
      return line.replace(/^(\s*)/, "$1# ");
    });
  }

  function handleYamlEditorKeydown(event) {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") {
      preferredArrowColumn = null;
    }
    if ((event.key === "ArrowUp" || event.key === "ArrowDown") && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      moveSelectionVertically(event.key === "ArrowUp" ? -1 : 1, event.shiftKey);
      return;
    }
    if (event.key === "Tab") {
      event.preventDefault();
      if (event.shiftKey) {
        outdentSelection();
      } else {
        indentSelection();
      }
      return;
    }
    if (event.key === "Enter" && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      insertNewlineAtSelection();
      return;
    }
    if ((event.metaKey || event.ctrlKey) && (event.key === "/" || event.code === "Slash")) {
      event.preventDefault();
      toggleCommentSelection();
    }
  }

  function setEditorVisible(isVisible) {
    authPanel.hidden = Boolean(isVisible);
    editorApp.hidden = !isVisible;
  }

  function renderBackups(backups) {
    backupsList.innerHTML = "";
    if (!Array.isArray(backups) || backups.length === 0) {
      var empty = document.createElement("p");
      empty.className = "config-editor-empty";
      empty.textContent = "No backups yet.";
      backupsList.appendChild(empty);
      return;
    }
    backups.forEach(function (backup) {
      var row = document.createElement("div");
      row.className = "config-editor-backup";
      var meta = document.createElement("div");
      var name = document.createElement("strong");
      name.textContent = String(backup.name || "backup");
      var timestamp = document.createElement("span");
      timestamp.textContent = formatTimestamp(backup.created_at);
      meta.appendChild(name);
      meta.appendChild(timestamp);
      var button = document.createElement("button");
      button.className = "button secondary";
      button.type = "button";
      button.textContent = "Restore";
      button.addEventListener("click", function () {
        restoreBackup(String(backup.name || ""), button);
      });
      row.appendChild(meta);
      row.appendChild(button);
      backupsList.appendChild(row);
    });
  }

  function applyConfigPayload(payload) {
    setEditorVisible(true);
    pathLabel.textContent = payload.path || "";
    yamlEditor.value = payload.content || "";
    syncEditorHighlight();
    renderBackups(payload.backups || []);
  }

  function loadConfig() {
    showStatus("Loading...", false);
    return window.fetch("/api/config-editor/config", {
      cache: "no-store",
      credentials: "same-origin",
    }).then(parseResponse).then(function (payload) {
      applyConfigPayload(payload);
      showStatus("Loaded.", false);
    }).catch(function (error) {
      if (/unauthorized|invalid|expired/i.test(error.message || "")) {
        setEditorVisible(false);
        showStatus("", false);
        return;
      }
      showStatus(error.message || "Could not load config.", true);
    });
  }

  function saveConfig() {
    var finishActionFeedback = window.UAActionFeedback.begin(saveButton, {
      busyLabel: "Saving…",
      group: saveButton.parentElement,
    });
    showStatus("Saving...", false);
    window.fetch("/api/config-editor/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ content: yamlEditor.value }),
    }).then(parseResponse).then(function (payload) {
      renderBackups(payload.backups || []);
      showStatus("Saved. Backup created: " + (payload.backup && payload.backup.name ? payload.backup.name : "yes"), false);
    }).catch(function (error) {
      showStatus(error.message || "Save failed.", true);
    }).finally(function () {
      finishActionFeedback();
    });
  }

  function restoreBackup(backupName, button) {
    if (!backupName) {
      return;
    }
    var finishActionFeedback = window.UAActionFeedback.begin(button, {
      busyLabel: "Restoring…",
      group: button.parentElement,
    });
    showStatus("Restoring backup...", false);
    window.fetch("/api/config-editor/backups/" + encodeURIComponent(backupName) + "/restore", {
      method: "POST",
      credentials: "same-origin",
    }).then(parseResponse).then(function () {
      return loadConfig();
    }).then(function () {
      showStatus("Restored backup: " + backupName, false);
    }).catch(function (error) {
      showStatus(error.message || "Restore failed.", true);
    }).finally(function () {
      finishActionFeedback();
    });
  }

  function requestRestart() {
    if (!restartConfirm.checked) {
      showStatus("Confirm the restart first.", true);
      return;
    }
    var finishActionFeedback = window.UAActionFeedback.begin(restartButton, {
      busyLabel: "Restarting…",
      group: restartButton.parentElement,
    });
    showStatus("Requesting supervised runtime restart...", false);
    window.fetch("/api/config-editor/restart", {
      method: "POST",
      credentials: "same-origin",
    }).then(parseResponse).then(function (payload) {
      finishActionFeedback();
      restartRequested = true;
      restartButton.disabled = true;
      restartButton.textContent = "Restart requested";
      showStatus(payload.message || "Supervised runtime restart requested.", false);
    }).catch(function (error) {
      showStatus(error.message || "Restart request failed.", true);
      finishActionFeedback();
    });
  }

  tokenReveal.addEventListener("click", function () {
    revealPassword(tokenInput, tokenReveal);
  });

  authForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var token = tokenInput.value.trim();
    if (!token) {
      showAuthError("Enter your Web Chat authentication token.");
      return;
    }
    window.fetch("/api/web-chat/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ token: token }),
    }).then(parseResponse).then(function () {
      tokenInput.value = "";
      showAuthError("");
      return loadUserAppearance().then(loadConfig);
    }).catch(function (error) {
      showAuthError(error.message || "Authentication failed.");
    });
  });

  saveButton.addEventListener("click", saveConfig);
  reloadButton.addEventListener("click", loadConfig);
  downloadButton.addEventListener("click", function () {
    window.location.href = "/api/config-editor/download";
  });
  yamlEditor.addEventListener("input", syncEditorHighlight);
  yamlEditor.addEventListener("click", function () {
    preferredArrowColumn = null;
  });
  yamlEditor.addEventListener("scroll", syncHighlightScroll);
  yamlEditor.addEventListener("keydown", handleYamlEditorKeydown);
  restartConfirm.addEventListener("change", function () {
    restartButton.disabled = restartRequested
      || window.UAActionFeedback.isPending(restartButton)
      || !restartConfirm.checked;
  });
  restartButton.addEventListener("click", requestRestart);

  loadRuntimeTheme();
  loadConfig();
}());
