/*
 * Workspace Files (/webchat?view=files; ui/DESIGN.md "Workspace Files").
 *
 * A lazy, read-only file tree and one preview pane. The gateway lists only
 * what the signed-in person may read: members get their own folder (the top
 * level lists only users/, and users/ only their folder); administrators also
 * get team/ and agents/. The page title follows what the top level lists
 * ("Your files" or "Workspace"); it is display only.
 *
 * Clicking a row selects it: a file opens in the preview, a folder shows its
 * name there. `options.fileActions` (workspace_file_actions.mjs) adds Share…
 * and Send… for the selected item, when it is inside the person's own folder,
 * at the top right of the preview pane (`options.itemActions`), and the
 * "Shared" chips in the tree. The tree itself carries no buttons (owner's
 * decision 2026-10-04). This script reports each folder listing to it so it
 * can learn which folder is the person's own, and shows each action's result
 * in the page's status line.
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
    var selectedPath = "";
    var selectedRow = null;
    var requestSequence = 0;
    var loadingRoot = false;
    var wrapTextFiles = true;
    var nameTooltip = null;
    var nameTooltipOwner = null;
    var nameTooltipTimer = 0;
    var viewTitle = DEFAULT_TITLE;
    // Rows by workspace path, so "Shared" chips can be added when shares load.
    var rowsByPath = new Map();
    var sharedPaths = new Set();

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

    function loadSharedPaths(sequence) {
      if (!fileActions) {
        return Promise.resolve();
      }
      return fileActions.sharedPaths().then(function (paths) {
        if (sequence !== requestSequence) {
          return;
        }
        paths.forEach(markShared);
      });
    }

    function parseResponse(response, fallback) {
      if (response.status === 401 && onUnauthorized(response)) {
        throw new Error("Your Web Chat session ended.");
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
          throw new Error(String(payload.message || rawText || fallback));
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
      selectedRow = row;
      row.classList.add("is-selected");
    }

    function selectFile(entry, row, item) {
      selectRow(row);
      selectedPath = entry.path;
      viewerTitle.textContent = entry.name;
      viewerMeta.textContent = "Loading preview...";
      viewer.replaceChildren(element("div", "workspace-loading", "Loading preview..."));
      showItemActions(entry, row, item);
      var sequence = ++requestSequence;
      fetchFile(entry.path).then(function (file) {
        if (sequence !== requestSequence || selectedPath !== entry.path) {
          return;
        }
        renderFile(file);
      }).catch(function (error) {
        if (sequence !== requestSequence || selectedPath !== entry.path) {
          return;
        }
        viewerMeta.textContent = "";
        viewer.replaceChildren(
          element("div", "workspace-empty-state is-error", error.message)
        );
      });
    }

    /** A folder has no preview; selecting it names it in the preview pane and offers its actions. */
    function selectFolder(entry, row, item) {
      selectRow(row);
      selectedPath = entry.path;
      requestSequence += 1;
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
    // a person with a plus (share with someone) and a paper plane (send).
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
     * Share… and Send… for the selected item, at the top right of the preview
     * pane, when the gateway allows them (items inside the person's own
     * folder; Send for files only). Results go to the page status line; a
     * file moved to the team folder leaves this folder at once, so its row is
     * removed and the preview cleared.
     */
    function showItemActions(entry, row, item) {
      hideItemActions();
      var allowed = fileActions && itemActions ? fileActions.actionsFor(entry) : null;
      if (!allowed || (!allowed.share && !allowed.send)) {
        return;
      }
      itemActions.setAttribute("aria-label", "Actions for " + entry.name);
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
                if (selectedRow === row) {
                  selectedRow = null;
                  clearViewer();
                }
                rowsByPath.delete(entry.path);
                item.remove();
              }
              setStatus(result.message, false);
            },
          });
        });
        itemActions.appendChild(send);
      }
      itemActions.hidden = false;
    }

    function renderEntries(container, entries, depth, parentPath) {
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
        if (entry.kind === "file") {
          row.appendChild(
            element("span", "workspace-tree-size", formatBytes(entry.size_bytes))
          );
          row.addEventListener("click", function () {
            selectFile(entry, row, item);
          });
        } else {
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
            row.setAttribute("aria-expanded", "true");
            disclosure.textContent = "⌄";
            if (group) {
              group.hidden = false;
              return;
            }
            row.classList.add("is-loading");
            fetchDirectory(entry.path).then(function (payload) {
              group = element("div", "workspace-tree-group");
              group.setAttribute("role", "group");
              if ((payload.entries || []).length) {
                renderEntries(group, payload.entries || [], depth + 1, entry.path);
              } else {
                group.appendChild(
                  element("div", "workspace-tree-empty", "This folder is empty.")
                );
              }
              item.appendChild(group);
              if (payload.truncated) {
                setStatus(
                  "A folder exceeded the " + payload.entry_limit + " entry display limit.",
                  false
                );
              }
            }).catch(function (error) {
              row.setAttribute("aria-expanded", "false");
              disclosure.textContent = "›";
              setStatus(error.message, true);
            }).finally(function () {
              row.classList.remove("is-loading");
            });
          });
        }
        line.appendChild(row);
        item.appendChild(line);
        rowsByPath.set(entry.path, { row: row, item: item, chip: null });
        if (sharedPaths.has(entry.path)) {
          markShared(entry.path);
        }
        container.appendChild(item);
      });
    }

    function load() {
      if (loadingRoot) {
        return Promise.resolve();
      }
      loadingRoot = true;
      hideNameTooltip();
      var finishRefresh = window.UAActionFeedback.beginRefresh(refreshButton);
      var sequence = ++requestSequence;
      clearViewer();
      rowsByPath = new Map();
      sharedPaths = new Set();
      selectedRow = null;
      var shares = loadSharedPaths(sequence);
      return fetchDirectory("").then(function (payload) {
        var entries = payload.entries || [];
        tree.replaceChildren();
        if (fileActions) {
          setViewTitle(fileActions.viewTitle(entries), true);
        }
        renderEntries(tree, entries, 1, "");
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
      }).catch(function (error) {
        tree.replaceChildren(
          element("div", "workspace-tree-empty is-error", error.message)
        );
        setStatus(error.message, true);
      }).then(function () {
        return shares;
      }).finally(function () {
        loadingRoot = false;
        finishRefresh();
      });
    }

    function clear() {
      hideNameTooltip();
      requestSequence += 1;
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

    refreshButton.addEventListener("click", load);
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
