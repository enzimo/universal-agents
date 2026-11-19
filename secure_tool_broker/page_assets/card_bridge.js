// The card view's bridge (secure_tool_broker.page_view, inlined by
// auth_pages.render_broker_auth_page). Web Chat shows this broker page in a
// sandboxed iframe; this script tells that page two things only:
//   {type: "ua-broker-card", height: <pixels>}   when the page's height changes;
//   {type: "ua-broker-card", outcome: "finished" | "declined"}
//                                                once, when the broker marked
//                                                the page as ended that way.
// It never reads a form field or page text, sends nothing else, and listens
// to no message. It posts only to the Web Chat's own address
// (data-card-parent-origin), so no other page receives it.
(function () {
  "use strict";
  var MESSAGE_TYPE = "ua-broker-card";
  var OUTCOMES = ["finished", "declined"];
  var root = document.documentElement;
  var parentOrigin = root.getAttribute("data-card-parent-origin") || "";
  if (!parentOrigin || window.parent === window) {
    return;
  }
  function send(message) {
    window.parent.postMessage(message, parentOrigin);
  }
  var lastHeight = -1;
  function reportHeight() {
    var height = Math.ceil(document.body.getBoundingClientRect().height);
    if (!(height > 0) || height === lastHeight) {
      return;
    }
    lastHeight = height;
    send({ type: MESSAGE_TYPE, height: height });
  }
  function start() {
    reportHeight();
    if (typeof window.ResizeObserver === "function") {
      new window.ResizeObserver(reportHeight).observe(document.body);
    }
    var outcome = root.getAttribute("data-card-outcome") || "";
    if (OUTCOMES.indexOf(outcome) !== -1) {
      send({ type: MESSAGE_TYPE, outcome: outcome });
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
