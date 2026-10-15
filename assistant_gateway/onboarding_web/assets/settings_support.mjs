/**
 * Shared helpers for the Web Chat Settings dialog (settings_controls.mjs,
 * team_controls.mjs and memory_controls.mjs).
 *
 * Every Settings read and write goes through `createSettingsClient`:
 * - reads use the signed-in browser session (an HTTP-only cookie);
 * - writes also send the session-memory CSRF token in the
 *   `X-UA-Permission-CSRF` header, and are refused locally when no token is
 *   known (the person must sign in again).
 * Gateway errors arrive as `{error: {code, title, message, recovery,
 * technical?}}` and become one plain sentence for the person. The thrown Error
 * keeps the HTTP status in `error.status` so a view can tell "not allowed"
 * (403) apart from a failure, the stable code in `error.code`, and the whole
 * answer in `error.payload`.
 *
 * Rendering helpers only set textContent, never HTML.
 */

export const CSRF_HEADER = "X-UA-Permission-CSRF";
export const SESSION_CONTEXT_API = "/api/web-chat/session-context";
/** Every Settings list shows at most this many rows per page (ui/DESIGN.md). */
export const LIST_PAGE_SIZE = 20;
export const EXPIRED_SESSION_MESSAGE = "Your sign-in session expired. Sign in again, then retry.";
export const NETWORK_FAILURE_MESSAGE =
  "The request did not reach the gateway. Check your connection and try again.";
export const FORBIDDEN_STATUS = 403;

const TIME_FORMAT = Object.freeze({
  year: "numeric", month: "short", day: "numeric",
  hour: "numeric", minute: "2-digit", timeZoneName: "short",
});

export function text(value) {
  return value === undefined || value === null ? "" : String(value).trim();
}

export function element(document, tagName, className, value) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (value !== undefined && value !== null) node.textContent = String(value);
  return node;
}

/** Local, readable time such as "Sep 25, 2026, 3:04 PM EDT"; "Unknown" when missing or invalid. */
export function localTimeFormatter({ locale, timeZone } = {}) {
  const format = new Intl.DateTimeFormat(locale, timeZone ? { ...TIME_FORMAT, timeZone } : TIME_FORMAT);
  return value => {
    const date = new Date(text(value));
    return Number.isNaN(date.getTime()) ? "Unknown" : format.format(date);
  };
}

/** Shows or hides a status line; errors get the `is-error` style. */
export function setStatusLine(node, message, isError) {
  node.textContent = String(message || "");
  node.hidden = !message;
  node.classList.toggle("is-error", Boolean(message && isError));
}

function errorText(error, response, fallbackMessage) {
  const technical = error.technical;
  let technicalText = "";
  if (technical && typeof technical === "object") {
    // Only technical presentation mode includes these fields.
    technicalText = " Technical: code=" + String(error.code || "unknown")
      + "; status=" + String(technical.status_code || response.status)
      + "; operation=" + String(technical.operation || "unknown")
      + "; failure_type=" + String(technical.failure_type || "unknown") + ".";
  }
  return [error.message, error.recovery].filter(Boolean).join(" ") + technicalText || fallbackMessage;
}

/**
 * Reads a gateway response. Resolves with the JSON body; rejects with an
 * Error carrying the gateway's plain explanation, `status` and `code`.
 * `onUnauthorized(response)` runs for every failed response so the page can
 * sign out on 401.
 */
export function parseResponse(response, fallbackMessage, onUnauthorized) {
  const textResponse = response.clone();
  return response.json().catch(() => textResponse.text()
    .then(message => ({ message: String(message || "").trim() }))
    .catch(() => ({}))).then(payload => {
    if (response.ok) return payload;
    if (typeof onUnauthorized === "function") onUnauthorized(response);
    const body = payload && typeof payload === "object" ? payload : {};
    const error = body.error;
    const failure = new Error(error && typeof error === "object"
      ? errorText(error, response, fallbackMessage)
      : body.message || error || fallbackMessage);
    failure.status = response.status;
    failure.code = error && typeof error === "object" ? String(error.code || "") : "";
    // The whole answer, for views that show more than the message (the Knowledge page's review flags).
    failure.payload = body;
    throw failure;
  });
}

/** One sentence for the person; network failures say the request never arrived. */
export function failureMessage(error, fallbackMessage) {
  const message = text(error && error.message);
  if (error instanceof TypeError || /failed to fetch|networkerror|network request failed/i.test(message)) {
    return NETWORK_FAILURE_MESSAGE;
  }
  return message || fallbackMessage;
}

export function isForbidden(error) {
  return Boolean(error && error.status === FORBIDDEN_STATUS);
}

/**
 * The Settings connection to the gateway. `browser` supplies `fetch` and the
 * page's shared helpers (`UAActionFeedback`, `UAPagination`); tests pass a
 * stand-in.
 */
export function createSettingsClient({ browser = globalThis, onUnauthorized } = {}) {
  let csrfToken = "";

  function writeHeaders() {
    return { "Content-Type": "application/json", [CSRF_HEADER]: csrfToken };
  }

  function get(path, fallbackMessage) {
    return Promise.resolve()
      .then(() => browser.fetch(path, { cache: "no-store", credentials: "same-origin" }))
      .then(response => parseResponse(response, fallbackMessage, onUnauthorized));
  }

  function send(method, path, body, fallbackMessage) {
    if (!csrfToken) return Promise.reject(new Error(EXPIRED_SESSION_MESSAGE));
    return Promise.resolve()
      .then(() => browser.fetch(path, {
        method, credentials: "same-origin", headers: writeHeaders(), body: JSON.stringify(body || {}),
      }))
      .then(response => parseResponse(response, fallbackMessage, onUnauthorized));
  }

  return {
    setCsrfToken(value) { csrfToken = String(value || ""); },
    writeHeaders,
    get,
    send,
    post: (path, body, fallbackMessage) => send("POST", path, body, fallbackMessage),
    /**
     * Binds this signed-in browser session to the conversation before a
     * decision (MCP connections, installed procedures). It grants nothing.
     */
    ensureSessionContext(sessionKey) {
      return send("POST", SESSION_CONTEXT_API, { session_key: String(sessionKey || "") },
        "Your session could not be confirmed because the gateway returned no explanation. Sign in again and retry.");
    },
    beginAction(button, busyLabel, group) {
      return browser.UAActionFeedback.begin(button, { busyLabel, group: group || null });
    },
    beginRefresh(button) {
      return browser.UAActionFeedback.beginRefresh(button);
    },
    paginate(settings) {
      return browser.UAPagination.create(settings);
    },
  };
}

// -- attention counts (ui/DESIGN.md, "Attention counts") ---------------------------

/** How often a badge's count is read again while the page is visible. */
export const ATTENTION_COUNT_POLL_MS = 30000;
/** Coalesces a burst of reasons to read again (chat messages) into one read. */
export const ATTENTION_COUNT_SOON_MS = 1500;

/**
 * Keeps one attention count current for its badges.
 *
 * `read()` resolves the value from the server. It is read once at `start()`
 * (sign-in), every `pollMs` while the page is visible and once when it becomes
 * visible again; `refreshSoon()` reads shortly after something may have
 * changed, and `set(next)` takes a value another read already returned, so the
 * page never reads the same thing twice. A newer `set()` supersedes earlier
 * polls, even when the value is unchanged. `refresh({force: true})` reads
 * after a known change without waiting for a poll taken before it.
 * `onChange(value)` runs only when the value changes (`same` compares; `merge`
 * combines a partial `set`). `stop()` (sign-out) detaches earlier reads, so a
 * new sign-in can read immediately, and goes back to `empty`. A
 * failed read keeps the last value and warns once until a read succeeds; a
 * read that resolves `null` had nothing to ask with yet and keeps it too.
 */
export function createAttentionCount({
  read, onChange, empty = 0, same = Object.is, merge = (_current, next) => next, warning,
  browser = globalThis, pollMs = ATTENTION_COUNT_POLL_MS, soonMs = ATTENTION_COUNT_SOON_MS,
}) {
  const document = browser.document;
  let value = empty;
  let running = false, version = 0, pollTimer = 0, soonTimer = 0, reading = null, warned = false;

  function update(next) {
    const merged = merge(value, next);
    if (same(merged, value)) return;
    value = merged;
    if (typeof onChange === "function") onChange(value);
  }

  function discardRead() {
    version += 1;
    reading = null;
  }

  function set(next) {
    // A list or decision has a newer total than any poll already in flight.
    discardRead();
    update(next);
  }

  function refresh({ force = false } = {}) {
    if (!running) return Promise.resolve();
    if (force) discardRead();
    if (reading) return reading;
    const current = version;
    const pending = Promise.resolve().then(() => current === version ? read() : null).then(next => {
      if (current !== version || next === null) return;
      warned = false;
      update(next);
    }).catch(error => {
      if (current !== version) return;
      if (!warned && browser.console) browser.console.warn(warning || "ua.attention_count: a count could not be read; retrying.", error);
      warned = true;
    }).finally(() => {
      // A detached reply must not clear a newer session's or change's read.
      if (reading === pending) reading = null;
    });
    reading = pending;
    return pending;
  }

  function refreshSoon() {
    if (!running || soonTimer) return;
    soonTimer = browser.setTimeout(() => {
      soonTimer = 0;
      refresh();
    }, soonMs);
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });

  return {
    /** Signed in: read now and keep the count current. */
    start() {
      if (running) return;
      running = true;
      refresh();
      pollTimer = browser.setInterval(() => { if (!document.hidden) refresh(); }, pollMs);
    },
    /** Sign-out: stop reading, ignore replies to earlier reads and clear the count. */
    stop() {
      running = false;
      discardRead();
      warned = false;
      browser.clearInterval(pollTimer);
      browser.clearTimeout(soonTimer);
      pollTimer = soonTimer = 0;
      update(empty);
    },
    refresh,
    refreshSoon,
    set,
    current: () => value,
  };
}

/**
 * Gives a control with a count badge its accessible name: `name`, plus what the
 * count stands for in words (`words`, "" when nothing waits).
 */
export function nameWithCount(control, name, words) {
  control.setAttribute("aria-label", words ? `${name} (${words})` : name);
}

/**
 * Shows `count` on a badge, or hides it at zero. `label` says in words what
 * the number counts; it becomes the badge's tooltip.
 */
export function showCountBadge(badge, count, label) {
  const value = Number.isInteger(count) && count > 0 ? String(count) : "";
  badge.textContent = value;
  badge.hidden = !value;
  if (value && label) badge.setAttribute("title", label);
  else badge.removeAttribute("title");
}
