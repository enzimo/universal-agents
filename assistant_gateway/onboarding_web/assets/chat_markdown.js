(function (root) {
  "use strict";

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // `__text__` underlines, as Discord shows it (owner's request, 2026-10-11:
  // the labels in the /accounts list). Only between word edges, so
  // snake_case words and file names like __init__.py are left alone; code
  // spans never reach here.
  function renderEmphasisEscaped(escaped) {
    // Python's Unicode word boundaries also protect non-ASCII names.
    escaped = escaped.replace(/(^|[^\p{L}\p{N}_])__(?=\S)([^_\n]+?)__(?![\p{L}\p{N}_]|\.[\p{L}\p{N}_])/gu, "$1<u>$2</u>");
    escaped = escaped.replace(/\*\*([^*\n][^*\n]*?)\*\*/g, "<strong>$1</strong>");
    escaped = escaped.replace(/\*([^*\n][^*\n]*?)\*/g, "<em>$1</em>");
    return escaped;
  }

  function renderSlashCommandsEscaped(escaped) {
    return escaped.replace(/(^|[\s([])(\/[A-Za-z][A-Za-z0-9_-]*)(?=$|[\s)\],.;:!?])/g, "$1<strong>$2</strong>");
  }

  function hasUnmatchedClosing(value, openChar, closeChar) {
    var openCount = 0;
    var closeCount = 0;
    for (var index = 0; index < value.length; index += 1) {
      if (value[index] === openChar) {
        openCount += 1;
      } else if (value[index] === closeChar) {
        closeCount += 1;
      }
    }
    return closeCount > openCount;
  }

  function splitBareUrl(rawUrl) {
    var url = String(rawUrl || "");
    var suffix = "";

    function trimLast() {
      suffix = url[url.length - 1] + suffix;
      url = url.slice(0, -1);
    }

    while (url) {
      var last = url[url.length - 1];
      if (/[.,;:!?]/.test(last)) {
        trimLast();
      } else if (last === ")" && hasUnmatchedClosing(url, "(", ")")) {
        trimLast();
      } else if (last === "]" && hasUnmatchedClosing(url, "[", "]")) {
        trimLast();
      } else if (url.endsWith("**")) {
        trimLast();
        trimLast();
      } else {
        break;
      }
    }
    return { url: url, suffix: suffix };
  }

  function renderEscapedText(value) {
    return renderSlashCommandsEscaped(renderEmphasisEscaped(escapeHtml(value)));
  }

  function renderAutolinkedText(value) {
    var raw = String(value || "");
    var urlPattern = /https?:\/\/[^\s<>"']+/g;
    var html = [];
    var lastIndex = 0;
    var match;
    while ((match = urlPattern.exec(raw)) !== null) {
      var parts = splitBareUrl(match[0]);
      html.push(renderEscapedText(raw.slice(lastIndex, match.index)));
      if (parts.url) {
        html.push(
          '<a href="' +
            escapeHtml(parts.url) +
            '" target="_blank" rel="noreferrer">' +
            escapeHtml(parts.url) +
            "</a>"
        );
      }
      html.push(renderEscapedText(parts.suffix));
      lastIndex = urlPattern.lastIndex;
    }
    html.push(renderEscapedText(raw.slice(lastIndex)));
    return html.join("");
  }

  function renderInlinePlain(value) {
    var raw = String(value || "");
    // Root-relative links open dashboard views on this deployment's origin.
    // Exclude protocol-relative URLs and backslashes from that path form.
    var linkPattern = /(\*\*|\*)(https?:\/\/[^\s<>"']+?)\1|\[([^\]\n]+)\]\(((?:https?:\/\/|mailto:)[^\s)]+|\/(?![\/\\])[^\s)\\]*)\)/g;
    var html = [];
    var lastIndex = 0;
    var match;
    while ((match = linkPattern.exec(raw)) !== null) {
      html.push(renderAutolinkedText(raw.slice(lastIndex, match.index)));
      if (match[1]) {
        var parts = splitBareUrl(match[2]);
        if (parts.url) {
          html.push(
            '<strong><a href="' +
              escapeHtml(parts.url) +
              '" target="_blank" rel="noreferrer">' +
              escapeHtml(parts.url) +
              "</a></strong>"
          );
        }
        html.push(renderEscapedText(parts.suffix));
      } else {
        html.push(
          '<a href="' +
            escapeHtml(match[4]) +
            '" target="_blank" rel="noreferrer">' +
            renderEscapedText(match[3]) +
            "</a>"
        );
      }
      lastIndex = linkPattern.lastIndex;
    }
    html.push(renderAutolinkedText(raw.slice(lastIndex)));
    return html.join("");
  }

  function renderInline(value) {
    var raw = String(value || "");
    var parts = raw.split(/(`[^`\n]+`)/g);
    return parts.map(function (part) {
      if (part.length >= 2 && part[0] === "`" && part[part.length - 1] === "`") {
        return "<code>" + escapeHtml(part.slice(1, -1)) + "</code>";
      }
      return renderInlinePlain(part);
    }).join("");
  }

  function languageClass(info) {
    var normalized = String(info || "").trim().split(/\s+/)[0] || "";
    if (!/^[a-zA-Z0-9_-]{1,32}$/.test(normalized)) {
      return "";
    }
    return ' class="language-' + escapeHtml(normalized) + '"';
  }

  function parseTableCells(line) {
    var trimmed = String(line || "").trim();
    if (trimmed.charAt(0) === "|") {
      trimmed = trimmed.slice(1);
    }
    if (trimmed.charAt(trimmed.length - 1) === "|") {
      trimmed = trimmed.slice(0, -1);
    }
    return trimmed.split("|").map(function (cell) {
      return cell.trim();
    });
  }

  function isTableSeparatorLine(line) {
    var cells = parseTableCells(line);
    if (cells.length < 2) {
      return false;
    }
    return cells.every(function (cell) {
      return /^:?-{3,}:?$/.test(cell);
    });
  }

  function isTableRowLine(line) {
    if (String(line || "").indexOf("|") === -1) {
      return false;
    }
    return parseTableCells(line).length >= 2;
  }

  function renderTable(headerCells, bodyRows) {
    var columnCount = headerCells.length;

    function normalizeRow(cells) {
      var normalized = cells.slice(0, columnCount);
      while (normalized.length < columnCount) {
        normalized.push("");
      }
      return normalized;
    }

    var header = normalizeRow(headerCells).map(function (cell) {
      return '<th scope="col">' + renderInline(cell) + "</th>";
    }).join("");
    var body = bodyRows.map(function (row) {
      return "<tr>" + normalizeRow(row).map(function (cell) {
        return "<td>" + renderInline(cell) + "</td>";
      }).join("") + "</tr>";
    }).join("");
    var bodyHtml = body ? "<tbody>" + body + "</tbody>" : "";
    return '<div class="message-table-wrap"><table><thead><tr>' + header + "</tr></thead>" + bodyHtml + "</table></div>";
  }

  function renderMarkdown(value) {
    var lines = String(value || "").replace(/\r\n?/g, "\n").split("\n");
    var html = [];
    var paragraph = [];
    var listItems = [];
    var listType = "";
    var quoteLines = [];
    var codeLines = [];
    var codeInfo = "";
    var inCode = false;

    function flushParagraph() {
      if (!paragraph.length) {
        return;
      }
      html.push("<p>" + renderInline(paragraph.join("\n")).replace(/\n/g, "<br>") + "</p>");
      paragraph = [];
    }

    function flushList() {
      if (!listItems.length) {
        return;
      }
      html.push("<" + listType + ">" + listItems.map(function (item) {
        return "<li>" + renderInline(item) + "</li>";
      }).join("") + "</" + listType + ">");
      listItems = [];
      listType = "";
    }

    function flushQuote() {
      if (!quoteLines.length) {
        return;
      }
      html.push("<blockquote><p>" + renderInline(quoteLines.join("\n")).replace(/\n/g, "<br>") + "</p></blockquote>");
      quoteLines = [];
    }

    function flushOpenBlocks() {
      flushParagraph();
      flushList();
      flushQuote();
    }

    for (var index = 0; index < lines.length; index += 1) {
      var line = lines[index];
      var fence = line.match(/^```(.*)$/);
      if (fence) {
        if (inCode) {
          html.push("<pre><code" + languageClass(codeInfo) + ">" + escapeHtml(codeLines.join("\n")) + "</code></pre>");
          codeLines = [];
          codeInfo = "";
          inCode = false;
        } else {
          flushOpenBlocks();
          inCode = true;
          codeInfo = fence[1] || "";
        }
        continue;
      }
      if (inCode) {
        codeLines.push(line);
        continue;
      }

      if (isTableRowLine(line) && index + 1 < lines.length && isTableSeparatorLine(lines[index + 1])) {
        flushOpenBlocks();
        var headerCells = parseTableCells(line);
        var bodyRows = [];
        index += 2;
        while (index < lines.length && lines[index].trim() && isTableRowLine(lines[index])) {
          if (!isTableSeparatorLine(lines[index])) {
            bodyRows.push(parseTableCells(lines[index]));
          }
          index += 1;
        }
        index -= 1;
        html.push(renderTable(headerCells, bodyRows));
        continue;
      }

      if (!line.trim()) {
        flushOpenBlocks();
        continue;
      }

      var heading = line.match(/^(#{1,4})\s+(.+)$/);
      if (heading) {
        flushOpenBlocks();
        html.push("<h" + heading[1].length + ">" + renderInline(heading[2].trim()) + "</h" + heading[1].length + ">");
        continue;
      }

      var unordered = line.match(/^\s*[-*+]\s+(.+)$/);
      if (unordered) {
        flushParagraph();
        flushQuote();
        if (listType && listType !== "ul") {
          flushList();
        }
        listType = "ul";
        listItems.push(unordered[1].trim());
        continue;
      }

      var ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (ordered) {
        flushParagraph();
        flushQuote();
        if (listType && listType !== "ol") {
          flushList();
        }
        listType = "ol";
        listItems.push(ordered[1].trim());
        continue;
      }

      var quote = line.match(/^>\s?(.*)$/);
      if (quote) {
        flushParagraph();
        flushList();
        quoteLines.push(quote[1]);
        continue;
      }

      flushList();
      flushQuote();
      paragraph.push(line.trim());
    }

    if (inCode) {
      html.push("<pre><code" + languageClass(codeInfo) + ">" + escapeHtml(codeLines.join("\n")) + "</code></pre>");
    }
    flushOpenBlocks();
    return html.join("");
  }

  root.UAWebChatMarkdown = {
    render: renderMarkdown,
  };
})(typeof window !== "undefined" ? window : globalThis);
