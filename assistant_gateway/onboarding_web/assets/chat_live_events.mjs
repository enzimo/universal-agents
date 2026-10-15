/** Resume durable delivery from the last applied page, never the last received page. */
export function createLiveEvents({ lifecycle, browser = window, getCursor, applyPage,
  resetHistory, checkSession, isAuthenticated, onUnauthorized, onStatus }) {
  let source = null;
  let timer = null;
  let watchdog = null;
  let generation = 0;
  let sessionGeneration = 0;
  let sessionCheck = null;
  let active = false;
  let recovering = false;
  let retryDelay = 1000;
  let lastReceived = 0;

  function disconnect() {
    generation += 1;
    if (source) source.close();
    source = null;
    lifecycle.clearTimeout(timer);
    lifecycle.clearTimeout(watchdog);
    timer = watchdog = null;
  }
  function stop() {
    sessionGeneration += 1;
    sessionCheck = null;
    active = false;
    recovering = false;
    disconnect();
  }
  function retry(reason) {
    if (!active || lifecycle.signal.aborted) return;
    disconnect();
    browser.console.warn("web_chat_live_reconnecting", { reason });
    onStatus("Updates disconnected. Reconnecting…");
    timer = lifecycle.timeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 15000);
  }
  function armWatchdog() {
    lifecycle.clearTimeout(watchdog);
    watchdog = lifecycle.timeout(() => retry("heartbeat_timeout"), 15000);
  }
  function connect() {
    if (!active || lifecycle.signal.aborted) return;
    disconnect();
    const currentGeneration = generation;
    const current = () => active && generation === currentGeneration && !lifecycle.signal.aborted;
    onStatus("Connecting live updates…");
    armWatchdog();
    try {
      source = new browser.EventSource(`/api/web-chat/events/stream?after=${encodeURIComponent(getCursor())}`);
    } catch (_) {
      retry("connection_failed");
      return;
    }
    source.onerror = () => {
      if (!current()) return;
      retry("connection_lost");
      const failedSession = sessionGeneration;
      // EventSource hides HTTP status. A bounded authenticated read distinguishes
      // an expired login from a temporary transport failure.
      if (sessionCheck) return;
      const check = Promise.resolve().then(checkSession).then(valid => {
        if (!valid && active && sessionGeneration === failedSession) {
          stop();
          onUnauthorized();
        }
      }).catch(() => {}).finally(() => {
        if (sessionCheck === check) sessionCheck = null;
      }); // The reconnect warning already reports transport failure.
      sessionCheck = check;
    };
    source.addEventListener("unauthorized", () => {
      if (!current()) return;
      stop();
      onUnauthorized();
    });
    source.onmessage = event => {
      if (!current()) return;
      try {
        const payload = JSON.parse(event.data);
        if (payload.status === "cursor_reset_required") {
          disconnect();
          const resetGeneration = generation;
          recovering = true;
          onStatus("Restoring chat history…");
          // History requests have their own timeouts. Do not reconnect while a
          // snapshot can still replace the cursor and overwrite applied events.
          Promise.resolve().then(resetHistory).then(() => {
            if (active && generation === resetGeneration) {
              recovering = false;
              connect();
            }
          }).catch(() => {
            if (active && generation === resetGeneration) {
              recovering = false;
              retry("history_reset_failed");
            }
          });
          return;
        }
        if (payload.status !== "ok" || !Array.isArray(payload.messages) || !payload.next_cursor) {
          throw new Error("Invalid live event page");
        }
        applyPage(payload);
        lastReceived = Date.now();
        retryDelay = 1000;
        armWatchdog();
        onStatus("Live updates connected");
      } catch (_) {
        retry("event_page_failed");
      }
    };
  }
  function resume() {
    if (active && !recovering && (!source || Date.now() - lastReceived >= 15000)) connect();
  }
  lifecycle.listen(browser, "online", resume);
  lifecycle.listen(browser, "focus", resume);
  lifecycle.listen(browser.document, "visibilitychange", () => {
    if (!browser.document.hidden) resume();
  });
  lifecycle.signal.addEventListener("abort", stop, { once: true });
  return {
    start() {
      if (active || !isAuthenticated()) return;
      active = true;
      retryDelay = 1000;
      connect();
    },
    stop,
  };
}
