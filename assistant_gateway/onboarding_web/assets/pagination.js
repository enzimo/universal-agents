(function () {
  "use strict";

  // Every list uses offsets; keep page selection and pending clicks consistent.
  function create(options) {
    var size = options.pageSize || 20;
    var count = Math.max(1, Math.ceil(options.total / size));
    var current = Math.min(count, Math.floor(options.offset / size) + 1);
    var nav = document.createElement("nav");
    nav.className = "list-pagination";
    nav.setAttribute("aria-label", options.label || "List pages");
    var controls = [], busy = false;

    function button(label, page, disabled, path) {
      var control = document.createElement("button");
      control.type = "button";
      control.className = "button ghost pagination-button";
      control.setAttribute("aria-label", label);
      control.disabled = disabled;
      if (path) {
        var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("aria-hidden", "true");
        var line = document.createElementNS(svg.namespaceURI, "path");
        line.setAttribute("d", path);
        svg.append(line);
        control.append(svg);
      } else {
        control.textContent = String(page);
        if (page === current) control.setAttribute("aria-current", "page");
      }
      control.addEventListener("click", async function () {
        if (busy || page === current) return;
        var host = nav.parentElement, hadFocus = document.activeElement === control;
        busy = true;
        nav.setAttribute("aria-busy", "true");
        controls.forEach(function (entry) { entry.button.disabled = true; });
        try {
          await options.onPage((page - 1) * size);
        } finally {
          busy = false;
          nav.removeAttribute("aria-busy");
          controls.forEach(function (entry) { entry.button.disabled = entry.disabled; });
          // Replacing a page removes its focused button. Keep keyboard users
          // at the pager unless they moved focus elsewhere while it loaded.
          if (hadFocus && !nav.isConnected && host.isConnected && document.activeElement === document.body) {
            var selected = host.querySelector(':scope > .list-pagination [aria-current="page"]');
            if (selected) selected.focus({ preventScroll: true });
          }
        }
      });
      controls.push({ button: control, disabled: disabled });
      nav.append(control);
    }

    function ellipsis() {
      var span = document.createElement("span");
      span.className = "pagination-ellipsis";
      span.textContent = "…";
      span.setAttribute("aria-hidden", "true");
      nav.append(span);
    }

    button("First page", 1, current === 1, "M6 5v14M17 6l-6 6 6 6");
    button("Previous page", current - 1, current === 1, "M15 6l-6 6 6 6");
    var start = Math.max(1, Math.min(current - 2, count - 4));
    var end = Math.min(count, start + 4);
    if (start > 1) ellipsis();
    for (var page = start; page <= end; page++) button("Go to page " + page, page, false);
    if (end < count) ellipsis();
    button("Next page", current + 1, current === count, "M9 6l6 6-6 6");
    button("Last page", count, current === count, "M18 5v14M7 6l6 6-6 6");
    return nav;
  }

  window.UAPagination = { create: create };
})();
