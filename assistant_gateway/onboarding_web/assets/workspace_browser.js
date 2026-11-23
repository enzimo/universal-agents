/*
 * Workspace Files (/webchat?view=files; ui/DESIGN.md "Workspace Files").
 *
 * A lazy, read-only file tree and one preview pane. The gateway lists only
 * what the signed-in person may read: members get their own folder (under
 * users/) and team/; administrators also get agents/. The page title follows
 * what the top level lists ("Your files" or "Workspace"); it is display only.
 *
 * Clicking a row selects it: a file opens in the preview, a folder shows its
 * name there. `options.fileActions` (workspace_file_actions.mjs) adds Download
 * for every readable file and Share… / Send… for items inside the person's own
 * folder, at the top right of the preview pane (`options.itemActions`), plus
 * the "Shared" chips in the tree. The tree itself carries no buttons (owner's
 * decision 2026-10-04). This script reports each folder listing to it so it
 * can learn which folder is the person's own, supports file deep-links
 * (?view=files&path=...&download=1), and shows each action's result in the
 * page's status line.
 */
(function () {
  "use strict";

  var DEFAULT_TITLE = "Workspace Files";

  function element(tagName, className, text) {
    var node = document.createElement(tagName);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function formatBytes(value) {
    var bytes = Number(value || 0);
    if (bytes < 1024) {
      return bytes + " B";
    }
    if (bytes < 1024 * 1024) {
      return (bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0) + " KB";
    }
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function formatTimestamp(value) {
    var date = new Date(String(value || ""));
    if (Number.isNaN(date.getTime())) {
      return "";
    }
    return date.toLocaleString([], {
      dateStyle: "medium",
      timeStyle: "short",
    });
  }

  function contentUrl(path) {
    return "/api/web-chat/workspace/content?path=" + encodeURIComponent(path);
  }

  function downloadUrl(path) {
    return "/api/web-chat/workspace/download?path=" + encodeURIComponent(path);
  }

  function parseDeepLinkSegments(rawPath) {
    var trimmed = String(rawPath || "").trim();
    if (
      !trimmed
      || trimmed.indexOf("/") === 0
      || trimmed.slice(-1) === "/"
      || trimmed.indexOf("\\") !== -1
    ) {
      return null;
    }
    var segments = trimmed.split("/");
    for (var i = 0; i < segments.length; i += 1) {
      if (!segments[i] || segments[i] === "." || segments[i] === "..") {
        return null;
      }
    }
    return segments;
  }

  function splitFileName(value) {
    var name = String(value || "");
    var extensionIndex = name.lastIndexOf(".");
    if (extensionIndex <= 0 || extensionIndex === name.length - 1) {
      return { stem: name, extension: "" };
    }
    return {
      stem: name.slice(0, extensionIndex),
      extension: name.slice(extensionIndex),
    };
  }

  function create(options) {
    var tree = options.tree;
    var viewer = options.viewer;
    var viewerTitle = options.viewerTitle;
    var viewerMeta = options.viewerMeta;
    var viewerTools = options.viewerTools;
    var itemActions = options.itemActions;
    var status = options.status;
    var refreshButton = options.refreshButton;
    var markdownRenderer = options.markdownRenderer;
    var onUnauthorized = options.onUnauthorized;
    // The page title and the tree heading: "Your files" or "Workspace".
    var titleNodes = options.titleNodes || [];
    var fileActions = options.fileActions || null;
    var onDownload = typeof options.onDownload === "function" ? options.onDownload : null;
    var selectedPath = "";
    var selectedRow = null;
    var loadGeneration = 0;
    var selectionSequence = 0;
    var loadingRoot = false;
    var wrapTextFiles = true;
    var nameTooltip = null;
    var nameTooltipOwner = null;
    var nameTooltipTimer = 0;
    var viewTitle = DEFAULT_TITLE;
    // Rows by workspace path, so "Shared" chips and deep-links can find them.
    var rowsByPath = new Map();
    var sharedPaths = new Set();

    function startBrowserDownload(url, entry) {
      if (onDownload) {
        onDownload(url, entry);
        return;
      }
      // Omit link.download: the server sends Content-Disposition: attachment
      // with the sanitized UTF-8 filename, and without the attribute even a
      // race-window 302 redirect navigates to sign-in instead of saving HTML.
      var link = document.createElement("a");
      link.href = url;
      link.rel = "noopener";
      link.hidden = true;
      document.body.appendChild(link);
      link.click();
      link.remove();
    }

    function downloadFailureMessage(statusCode, entry) {
      var label = entry && entry.name ? "\"" + entry.name + "\"" : "This file";
      if (statusCode === 404) {
        return label + " could not be found. It may have been moved or deleted.";
      }
      if (statusCode === 403) {
        return "You do not have permission to download " + label + ".";
      }
      if (statusCode === 400) {
        return label + " cannot be downloaded.";
      }
      return "Could not download " + label + ".";
    }

    function triggerDownload(entry, button) {
      if (button && button.disabled) {
        return Promise.resolve({ ok: false, unauthorized: false });
      }
      if (button) {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
      }
      var url = downloadUrl(entry.path);
      var generation = loadGeneration;
      function isCurrentTarget() {
        return generation === loadGeneration && selectedPath === entry.path;
      }
      return window.fetch(url, {
        method: "HEAD",
        cache: "no-store",
        credentials: "same-origin",
      }).then(function (response) {
        if (generation !== loadGeneration) {
          return { ok: false, unauthorized: false };
        }
        if (response.status === 401) {
          var handled = typeof onUnauthorized === "function" && onUnauthorized(response);
          if (!handled && isCurrentTarget()) {
            setStatus("Your Web Chat session ended.", true);
          }
          return { ok: false, unauthorized: true };
        }
        if (!response.ok) {
          if (isCurrentTarget()) {
            setStatus(downloadFailureMessage(response.status, entry), true);
          }
          return { ok: false, unauthorized: false };
        }
        if (isCurrentTarget()) {
          setStatus("", false);
        }
        startBrowserDownload(url, entry);
        return { ok: true, unauthorized: false };
      }).catch(function () {
        if (isCurrentTarget()) {
          setStatus(downloadFailureMessage(0, entry), true);
        }
        return { ok: false, unauthorized: false };
      }).finally(function () {
        if (button) {
          button.disabled = false;
          button.removeAttribute("aria-busy");
        }
      });
    }

    function setStatus(message, isError) {
      status.textContent = message || "";
      status.hidden = !message;
      status.classList.toggle("is-error", Boolean(isError));
    }

    /**
     * Names the page. The browser tab title changes only when a listing
     * arrives: clear() also runs at sign-out on the chat page, whose tab
     * title belongs to the chat (unread counts).
     */
    function setViewTitle(title, updateDocumentTitle) {
      viewTitle = title || DEFAULT_TITLE;
      titleNodes.forEach(function (node) {
        node.textContent = viewTitle;
      });
      tree.setAttribute("aria-label", viewTitle);
      if (updateDocumentTitle) {
        document.title = viewTitle + " · Universal Agents";
      }
    }

    function markShared(path) {
      sharedPaths.add(path);
      var parts = rowsByPath.get(path);
      if (!parts || parts.chip || !fileActions) {
        return;
      }
      parts.chip = element("span", "workspace-tree-chip", fileActions.labels.shared);
      parts.row.appendChild(parts.chip);
    }

    function loadSharedPaths(generation) {
      if (!fileActions) {
        return Promise.resolve();
      }
      return fileActions.sharedPaths().then(function (paths) {
        if (generation !== loadGeneration) {
          return;
        }
        paths.forEach(markShared);
      });
    }

    function parseResponse(response, fallback) {
      if (response.status === 401 && typeof onUnauthorized === "function" && onUnauthorized(response)) {
        var sessionError = new Error("Your Web Chat session ended.");
        sessionError.status = 401;
        sessionError.unauthorized = true;
        throw sessionError;
      }
      return response.text().then(function (rawText) {
        var payload = {};
        if (rawText) {
          try {
            payload = JSON.parse(rawText);
          } catch (_error) {
            payload = {};
          }
        }
        if (!response.ok) {
          var requestError = new Error(String(payload.message || rawText || fallback));
          requestError.status = response.status;
          requestError.unauthorized = response.status === 401;
          throw requestError;
        }
        return payload;
      });
    }

    function hideNameTooltip() {
      if (nameTooltipTimer) {
        window.clearTimeout(nameTooltipTimer);
        nameTooltipTimer = 0;
      }
      nameTooltipOwner = null;
      if (nameTooltip) {
        nameTooltip.hidden = true;
        nameTooltip.removeAttribute("style");
      }
    }

    function ensureNameTooltip() {
      if (nameTooltip) {
        return nameTooltip;
      }
      nameTooltip = element("div", "workspace-name-tooltip");
      nameTooltip.id = "workspace-name-tooltip";
      nameTooltip.setAttribute("role", "tooltip");
      nameTooltip.hidden = true;
      document.body.appendChild(nameTooltip);
      return nameTooltip;
    }

    function nameIsClipped(targets) {
      return targets.some(function (target) {
        return target.scrollWidth > target.clientWidth + 1;
      });
    }

    function showNameTooltip(row, targets, fullName) {
      if (nameTooltipOwner !== row || !nameIsClipped(targets)) {
        return;
      }
      var tooltip = ensureNameTooltip();
      tooltip.textContent = fullName;
      tooltip.hidden = false;
      var rowRect = row.getBoundingClientRect();
      var tooltipRect = tooltip.getBoundingClientRect();
      var left = Math.min(
        Math.max(rowRect.left, 12),
        window.innerWidth - tooltipRect.width - 12
      );
      var top = rowRect.bottom + 8;
      if (top + tooltipRect.height > window.innerHeight - 12) {
        top = Math.max(12, rowRect.top - tooltipRect.height - 8);
      }
      tooltip.style.left = left + "px";
      tooltip.style.top = top + "px";
    }

    function scheduleNameTooltip(row, targets, fullName) {
      hideNameTooltip();
      nameTooltipOwner = row;
      nameTooltipTimer = window.setTimeout(function () {
        nameTooltipTimer = 0;
        showNameTooltip(row, targets, fullName);
      }, 1000);
    }

    function bindNameTooltip(row, targets, fullName) {
      row.addEventListener("mouseenter", function () {
        scheduleNameTooltip(row, targets, fullName);
      });
      row.addEventListener("mouseleave", hideNameTooltip);
      row.addEventListener("focus", function () {
        scheduleNameTooltip(row, targets, fullName);
      });
      row.addEventListener("blur", hideNameTooltip);
    }

    function treeName(entry) {
      var name = element("span", "workspace-tree-name");
      if (entry.kind !== "file") {
        name.textContent = entry.name;
        return { node: name, clipTargets: [name] };
      }
      var parts = splitFileName(entry.name);
      name.classList.add("is-file");
      var stem = element("span", "workspace-tree-name-stem", parts.stem);
      name.appendChild(stem);
      var clipTargets = [stem];
      if (parts.extension) {
        var extension = element(
          "span",
          "workspace-tree-name-extension",
          parts.extension
        );
        name.appendChild(extension);
        clipTargets.push(extension);
      }
      return { node: name, clipTargets: clipTargets };
    }

    function fetchDirectory(path) {
      return window.fetch(
        "/api/web-chat/workspace?path=" + encodeURIComponent(path || ""),
        { cache: "no-store", credentials: "same-origin" }
      ).then(function (response) {
        return parseResponse(response, "Workspace directory could not be loaded.");
      });
    }

    function fetchFile(path) {
      return window.fetch(
        "/api/web-chat/workspace/file?path=" + encodeURIComponent(path),
        { cache: "no-store", credentials: "same-origin" }
      ).then(function (response) {
        return parseResponse(response, "Workspace file could not be loaded.");
      });
    }

    function clearViewer(message) {
      selectedPath = "";
      hideItemActions();
      viewerTitle.textContent = "Select a file";
      viewerMeta.textContent = "";
      viewerTools.replaceChildren();
      viewerTools.hidden = true;
      viewer.replaceChildren(
        element(
          "div",
          "workspace-empty-state",
          message || "Choose a file from the workspace browser to preview it."
        )
      );
    }

    function renderUnavailable(file) {
      var state = element("div", "workspace-empty-state");
      state.appendChild(element("strong", "", "Preview unavailable"));
      state.appendChild(
        element(
          "p",
          "",
          file.message || "This file format does not have a browser preview."
        )
      );
      viewer.replaceChildren(state);
    }

    function renderTextFile(file) {
      var source = element("pre", "workspace-text-preview");
      var code = element("code", "workspace-syntax");
      var showRaw = false;
      var hasFormattedContent = typeof file.formatted_content === "string";
      var language = String(file.syntax_language || "Plain text");
      var languageBadge = element("span", "workspace-preview-language");
      var wrapButton = element(
        "button",
        "button ghost workspace-preview-control",
        "Wrap lines"
      );
      wrapButton.type = "button";
      viewerTools.replaceChildren(languageBadge, wrapButton);
      viewerTools.hidden = false;

      var rawButton = null;
      if (hasFormattedContent) {
        rawButton = element(
          "button",
          "button ghost workspace-preview-control",
          "Show raw"
        );
        rawButton.type = "button";
        viewerTools.appendChild(rawButton);
      }

      function paintText() {
        var formatted = hasFormattedContent && !showRaw;
        var highlighted = formatted || !hasFormattedContent;
        var canUseHighlight = highlighted
          && typeof file.highlighted_html === "string";
        var displayContent = formatted
          ? file.formatted_content
          : String(file.content || "");
        source.classList.toggle("is-nowrap", !wrapTextFiles);
        wrapButton.setAttribute("aria-pressed", String(wrapTextFiles));
        wrapButton.classList.toggle("is-active", wrapTextFiles);
        languageBadge.textContent = language
          + (formatted ? " · formatted" : "")
          + (!canUseHighlight && file.syntax_language ? " · plain display" : "");
        if (canUseHighlight) {
          // This field contains only Pygments span markup generated by the server.
          // The formatter escapes the untrusted file text before returning it.
          code.innerHTML = file.highlighted_html;
        } else {
          code.textContent = displayContent;
        }
        if (rawButton) {
          rawButton.textContent = showRaw ? "Show formatted" : "Show raw";
          rawButton.classList.toggle("is-active", showRaw);
        }
      }

      wrapButton.addEventListener("click", function () {
        wrapTextFiles = !wrapTextFiles;
        paintText();
      });
      if (rawButton) {
        rawButton.addEventListener("click", function () {
          showRaw = !showRaw;
          paintText();
        });
      }
      source.appendChild(code);
      paintText();
      viewer.replaceChildren(source);
    }

    function renderFile(file) {
      viewerTools.replaceChildren();
      viewerTools.hidden = true;
      viewerTitle.textContent = file.name;
      var details = [formatBytes(file.size_bytes), formatTimestamp(file.modified_at_utc)];
      viewerMeta.textContent = details.filter(Boolean).join(" · ");
      if (file.preview_kind === "markdown") {
        var markdown = element("article", "workspace-markdown markdown-body");
        markdown.innerHTML = markdownRenderer.render(String(file.content || ""));
        viewer.replaceChildren(markdown);
        return;
      }
      if (file.preview_kind === "text") {
        renderTextFile(file);
        return;
      }
      if (file.preview_kind === "html") {
        var htmlFrame = element("iframe", "workspace-frame-preview");
        htmlFrame.title = "Rendered HTML preview of " + file.name;
        htmlFrame.setAttribute("sandbox", "");
        htmlFrame.setAttribute("referrerpolicy", "no-referrer");
        htmlFrame.src = contentUrl(file.path);
        viewer.replaceChildren(htmlFrame);
        return;
      }
      if (file.preview_kind === "pdf") {
        var pdfFrame = element("iframe", "workspace-frame-preview");
        pdfFrame.title = "PDF preview of " + file.name;
        pdfFrame.setAttribute("sandbox", "");
        pdfFrame.setAttribute("referrerpolicy", "no-referrer");
        pdfFrame.src = contentUrl(file.path);
        viewer.replaceChildren(pdfFrame);
        return;
      }
      if (file.preview_kind === "image") {
        var imageWrap = element("div", "workspace-image-preview");
        var image = element("img", "");
        image.alt = file.name;
        image.src = contentUrl(file.path);
        imageWrap.appendChild(image);
        viewer.replaceChildren(imageWrap);
        return;
      }
      renderUnavailable(file);
    }

    /** Marks the row as the selected item (one at a time). */
    function selectRow(row) {
      hideNameTooltip();
      if (selectedRow) {
        selectedRow.classList.remove("is-selected");
      }
      selectedRow = row || null;
      if (selectedRow) {
        selectedRow.classList.add("is-selected");
      }
    }

    function selectFile(entry, row, item) {
      selectRow(row);
      selectedPath = entry.path;
      viewerTitle.textContent = entry.name;
      viewerMeta.textContent = "Loading preview...";
      viewer.replaceChildren(element("div", "workspace-loading", "Loading preview..."));
      hideItemActions();
      var sequence = ++selectionSequence;
      return fetchFile(entry.path).then(function (file) {
        if (sequence !== selectionSequence || selectedPath !== entry.path) {
          return { ok: false, aborted: true, unauthorized: false };
        }
        var resolvedEntry = row
          ? entry
          : {
              path: file.path || entry.path,
              name: file.name || entry.name,
              kind: "file",
              size_bytes: file.size_bytes,
              modified_at_utc: file.modified_at_utc,
              preview_kind: file.preview_kind,
              content_type: file.content_type || null,
            };
        renderFile(file);
        showItemActions(resolvedEntry, row, item);
        return { ok: true, file: file, entry: resolvedEntry };
      }).catch(function (error) {
        if (sequence !== selectionSequence || selectedPath !== entry.path) {
          return {
            ok: false,
            aborted: true,
            unauthorized: Boolean(error && error.unauthorized),
          };
        }
        hideItemActions();
        viewerMeta.textContent = "";
        viewer.replaceChildren(
          element("div", "workspace-empty-state is-error", error.message)
        );
        return {
          ok: false,
          aborted: false,
          unauthorized: Boolean(error && error.unauthorized),
          status: error && error.status,
          message: error && error.message,
        };
      });
    }

    /** A folder has no preview; selecting it names it in the preview pane and offers its actions. */
    function selectFolder(entry, row, item) {
      selectRow(row);
      selectedPath = entry.path;
      selectionSequence += 1;
      viewerTitle.textContent = entry.name;
      viewerMeta.textContent = "Folder";
      viewerTools.replaceChildren();
      viewerTools.hidden = true;
      viewer.replaceChildren(
        element("div", "workspace-empty-state", "Folders have no preview. Open a file inside it to see it here.")
      );
      showItemActions(entry, row, item);
    }

    // 24x24 outline icons drawn with the thread buttons' stroke style:
    // a downward arrow into a tray (download), a person with a plus (share),
    // and a paper plane (send).
    var DOWNLOAD_ICON_PATHS = ["M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4", "M7 10l5 5 5-5", "M12 15V3"];
    var SHARE_ICON_PATHS = ["M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2", "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
      "M19 8v6", "M22 11h-6"];
    var SEND_ICON_PATHS = ["M22 2 11 13", "M22 2 15 22l-4-9-9-4 20-7z"];

    function actionIcon(paths) {
      var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("aria-hidden", "true");
      paths.forEach(function (d) {
        var path = document.createElementNS(svg.namespaceURI, "path");
        path.setAttribute("d", d);
        svg.appendChild(path);
      });
      return svg;
    }

    /** A labeled button with its icon; the item's name is in its accessible name. */
    function actionButton(label, accessibleName, iconPaths, onClick) {
      var control = element("button", "button ghost workspace-item-action");
      control.type = "button";
      control.setAttribute("aria-label", accessibleName);
      control.appendChild(actionIcon(iconPaths));
      control.appendChild(element("span", "", label));
      control.addEventListener("click", onClick);
      return control;
    }

    function hideItemActions() {
      if (!itemActions) {
        return;
      }
      itemActions.replaceChildren();
      itemActions.hidden = true;
    }

    /**
     * Download, Share…, and Send… for the selected item, at the top right of
     * the preview pane. Every readable file gets Download; items inside the
     * person's own folder also get Share… (and Send… for files). Results go to
     * the page status line; a file moved to the team folder leaves this folder
     * at once, so its row is removed and the preview cleared.
     */
    function showItemActions(entry, row, item) {
      hideItemActions();
      if (!itemActions) {
        return;
      }
      var allowed = fileActions
        ? fileActions.actionsFor(entry)
        : { download: entry.kind === "file", share: false, send: false };
      if (!allowed || (!allowed.download && !allowed.share && !allowed.send)) {
        return;
      }
      itemActions.setAttribute("aria-label", "Actions for " + entry.name);
      if (allowed.download) {
        var downloadLabel = fileActions && fileActions.labels && fileActions.labels.download
          ? fileActions.labels.download
          : "Download";
        var download = actionButton(
          downloadLabel,
          "Download " + entry.name,
          DOWNLOAD_ICON_PATHS,
          function () {
            triggerDownload(entry, download);
          }
        );
        itemActions.appendChild(download);
      }
      if (allowed.share) {
        var share = actionButton(fileActions.labels.share, "Share " + entry.name, SHARE_ICON_PATHS, function () {
          setStatus("", false);
          fileActions.openShare({
            entry: entry,
            returnFocus: share,
            onDone: function (result) {
              markShared(entry.path);
              setStatus(result.message, false);
            },
          });
        });
        itemActions.appendChild(share);
      }
      if (allowed.send) {
        var send = actionButton(fileActions.labels.send, "Send " + entry.name, SEND_ICON_PATHS, function () {
          setStatus("", false);
          fileActions.openSend({
            entry: entry,
            returnFocus: send,
            onDone: function (result) {
              if (result.movedAway) {
                if ((row && selectedRow === row) || (!row && selectedPath === entry.path)) {
                  selectedRow = null;
                  clearViewer();
                }
                rowsByPath.delete(entry.path);
                if (item) {
                  item.remove();
                }
              }
              setStatus(result.message, false);
            },
          });
        });
        itemActions.appendChild(send);
      }
      itemActions.hidden = false;
    }

    function renderEntries(container, entries, depth, parentPath, generation) {
      if (fileActions) {
        fileActions.noteListing(parentPath, entries);
      }
      entries.forEach(function (entry) {
        var item = element("div", "workspace-tree-item");
        // The row, its action strip and its message line share the indent.
        item.style.setProperty("--tree-indent", String((depth - 1) * 14) + "px");
        item.setAttribute("role", "treeitem");
        item.setAttribute("aria-level", String(depth));
        var line = element("div", "workspace-tree-line");
        var row = element("button", "workspace-tree-row");
        var group = null;
        var pendingExpand = null;
        row.type = "button";
        row.style.setProperty("--tree-indent", String((depth - 1) * 14) + "px");
        var disclosure = element(
          "span",
          "workspace-tree-disclosure",
          entry.kind === "directory" ? "›" : ""
        );
        disclosure.setAttribute("aria-hidden", "true");
        row.appendChild(disclosure);
        row.appendChild(
          element(
            "span",
            "workspace-tree-kind",
            entry.kind === "directory" ? "Folder" : "File"
          )
        );
        var renderedName = treeName(entry);
        row.appendChild(renderedName.node);
        bindNameTooltip(row, renderedName.clipTargets, entry.name);
        var expandFolder = null;
        if (entry.kind === "file") {
          row.appendChild(
            element("span", "workspace-tree-size", formatBytes(entry.size_bytes))
          );
          row.addEventListener("click", function () {
            selectFile(entry, row, item);
          });
        } else {
          expandFolder = function () {
            row.setAttribute("aria-expanded", "true");
            disclosure.textContent = "⌄";
            if (group) {
              group.hidden = false;
              return Promise.resolve({ ok: true, unauthorized: false });
            }
            if (pendingExpand) {
              return pendingExpand;
            }
            row.classList.add("is-loading");
            pendingExpand = fetchDirectory(entry.path).then(function (payload) {
              if (generation !== loadGeneration) {
                return { ok: false, unauthorized: false };
              }
              group = element("div", "workspace-tree-group");
              group.setAttribute("role", "group");
              if ((payload.entries || []).length) {
                renderEntries(group, payload.entries || [], depth + 1, entry.path, generation);
              } else {
                group.appendChild(
                  element("div", "workspace-tree-empty", "This folder is empty.")
                );
              }
              group.hidden = row.getAttribute("aria-expanded") !== "true";
              item.appendChild(group);
              if (payload.truncated) {
                setStatus(
                  "A folder exceeded the " + payload.entry_limit + " entry display limit.",
                  false
                );
              }
              return { ok: true, unauthorized: false };
            }).catch(function (error) {
              if (generation === loadGeneration) {
                row.setAttribute("aria-expanded", "false");
                disclosure.textContent = "›";
                setStatus(error.message, true);
              }
              return { ok: false, unauthorized: Boolean(error && error.unauthorized) };
            }).finally(function () {
              pendingExpand = null;
              row.classList.remove("is-loading");
            });
            return pendingExpand;
          };
          row.setAttribute("aria-expanded", "false");
          row.addEventListener("click", function () {
            selectFolder(entry, row, item);
            var expanded = row.getAttribute("aria-expanded") === "true";
            if (expanded) {
              row.setAttribute("aria-expanded", "false");
              disclosure.textContent = "›";
              if (group) {
                group.hidden = true;
              }
              return;
            }
            expandFolder();
          });
        }
        line.appendChild(row);
        item.appendChild(line);
        rowsByPath.set(entry.path, {
          entry: entry,
          row: row,
          item: item,
          chip: null,
          expand: expandFolder,
        });
        if (sharedPaths.has(entry.path)) {
          markShared(entry.path);
        }
        container.appendChild(item);
      });
    }

    function openDeepLink(
      initialPath,
      autoDownload,
      onDownloadConsumed,
      onDeepLinkConsumed,
      generation,
      initialSelection
    ) {
      function consumeDeepLink() {
        if (generation === loadGeneration && typeof onDeepLinkConsumed === "function") {
          onDeepLinkConsumed();
        }
      }
      function consumeDownload() {
        if (generation === loadGeneration && autoDownload && typeof onDownloadConsumed === "function") {
          onDownloadConsumed();
        }
      }
      var segments = parseDeepLinkSegments(initialPath);
      if (!segments) {
        setStatus("The requested workspace file path is not valid.", true);
        consumeDeepLink();
        consumeDownload();
        return Promise.resolve();
      }
      var chain = Promise.resolve({ continueWalk: true, failed: false });
      for (var i = 0; i < segments.length - 1; i += 1) {
        (function (prefix) {
          chain = chain.then(function (state) {
            if (!state.continueWalk || state.failed || generation !== loadGeneration) {
              return state;
            }
            if (initialSelection !== selectionSequence) {
              consumeDeepLink();
              consumeDownload();
              return { continueWalk: false, failed: true };
            }
            var folderNode = rowsByPath.get(prefix);
            if (!folderNode) {
              // Ancestor may sit past the 2,000-entry display cap; fall back to direct fetchFile.
              return { continueWalk: false, failed: false };
            }
            if (folderNode.entry.kind !== "directory" || typeof folderNode.expand !== "function") {
              setStatus("The requested file \"" + initialPath + "\" could not be found.", true);
              consumeDeepLink();
              consumeDownload();
              return { continueWalk: false, failed: true };
            }
            return folderNode.expand().then(function (expandResult) {
              if (generation !== loadGeneration || (expandResult && expandResult.unauthorized)) {
                return { continueWalk: false, failed: true };
              }
              if (!expandResult || !expandResult.ok) {
                consumeDeepLink();
                consumeDownload();
                return { continueWalk: false, failed: true };
              }
              return { continueWalk: true, failed: false };
            });
          });
        })(segments.slice(0, i + 1).join("/"));
      }
      return chain.then(function (state) {
        if (state.failed || generation !== loadGeneration) {
          return;
        }
        if (initialSelection !== selectionSequence) {
          consumeDeepLink();
          consumeDownload();
          return;
        }
        var target = rowsByPath.get(initialPath);
        if (target && target.entry.kind !== "file") {
          selectFolder(target.entry, target.row, target.item);
          consumeDeepLink();
          if (autoDownload) {
            setStatus("Folders cannot be downloaded. Select a file inside it to download.", false);
            consumeDownload();
          }
          return;
        }
        var entryToSelect = target
          ? target.entry
          : {
              path: initialPath,
              name: segments[segments.length - 1],
              kind: "file",
              size_bytes: null,
            };
        return selectFile(
          entryToSelect,
          target ? target.row : null,
          target ? target.item : null
        ).then(function (result) {
          if (generation !== loadGeneration || (result && result.unauthorized)) {
            return;
          }
          if (selectedPath !== entryToSelect.path) {
            consumeDeepLink();
            consumeDownload();
            return;
          }
          if (!result || !result.ok) {
            if (!target) {
              setStatus(
                result && result.status === 404
                  ? "The requested file \"" + initialPath + "\" could not be found."
                  : (result && result.message)
                    || ("The requested file \"" + initialPath + "\" could not be found."),
                true
              );
            }
            consumeDeepLink();
            consumeDownload();
            return;
          }
          if (!autoDownload) {
            consumeDeepLink();
            return;
          }
          var downloadTarget = result.entry || entryToSelect;
          return triggerDownload(downloadTarget).then(function (downloadResult) {
            if (generation !== loadGeneration || (downloadResult && downloadResult.unauthorized)) {
              return;
            }
            consumeDeepLink();
            consumeDownload();
          });
        });
      });
    }

    function load(loadOptions) {
      if (loadingRoot) {
        return Promise.resolve();
      }
      var opts = loadOptions && typeof loadOptions === "object" ? loadOptions : {};
      var initialPath = typeof opts.initialPath === "string" ? opts.initialPath.trim() : "";
      var autoDownload = Boolean(opts.autoDownload && initialPath);
      var onDownloadConsumed = typeof opts.onDownloadConsumed === "function"
        ? opts.onDownloadConsumed
        : null;
      var onDeepLinkConsumed = typeof opts.onDeepLinkConsumed === "function"
        ? opts.onDeepLinkConsumed
        : null;
      loadingRoot = true;
      hideNameTooltip();
      var finishRefresh = window.UAActionFeedback.beginRefresh(refreshButton);
      var generation = ++loadGeneration;
      var initialSelection = ++selectionSequence;
      clearViewer();
      rowsByPath = new Map();
      sharedPaths = new Set();
      selectedRow = null;
      var shares = loadSharedPaths(generation);
      return fetchDirectory("").then(function (payload) {
        if (generation !== loadGeneration) {
          return;
        }
        var entries = payload.entries || [];
        tree.replaceChildren();
        if (fileActions) {
          setViewTitle(fileActions.viewTitle(entries), true);
        }
        renderEntries(tree, entries, 1, "", generation);
        if (!entries.length) {
          var memberView = fileActions && viewTitle === fileActions.labels.memberTitle;
          tree.appendChild(
            element(
              "div",
              "workspace-tree-empty",
              memberView ? "You have no files yet." : "The workspace is empty."
            )
          );
        }
        if (payload.truncated) {
          setStatus(
            "The workspace root exceeded the " + payload.entry_limit + " entry display limit.",
            false
          );
        } else {
          setStatus("", false);
        }
        if (initialPath) {
          return openDeepLink(
            initialPath,
            autoDownload,
            onDownloadConsumed,
            onDeepLinkConsumed,
            generation,
            initialSelection
          );
        }
      }).catch(function (error) {
        if (generation !== loadGeneration) {
          return;
        }
        tree.replaceChildren(
          element("div", "workspace-tree-empty is-error", error.message)
        );
        setStatus(error.message, true);
      }).then(function () {
        return shares;
      }).finally(function () {
        if (generation === loadGeneration) {
          loadingRoot = false;
        }
        finishRefresh();
      });
    }

    function clear() {
      hideNameTooltip();
      loadGeneration += 1;
      selectionSequence += 1;
      loadingRoot = false;
      selectedPath = "";
      selectedRow = null;
      rowsByPath = new Map();
      sharedPaths = new Set();
      if (fileActions) {
        fileActions.reset();
      }
      tree.replaceChildren();
      setStatus("", false);
      setViewTitle(DEFAULT_TITLE, false);
      clearViewer();
    }

    refreshButton.addEventListener("click", function () {
      load();
    });
    tree.addEventListener("scroll", hideNameTooltip, { passive: true });
    window.addEventListener("resize", hideNameTooltip);
    clearViewer();

    return {
      clear: clear,
      load: load,
    };
  }

  window.UAWorkspaceBrowser = {
    create: create,
  };
})();
