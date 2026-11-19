/**
 * Broker pages inside Web Chat cards.
 *
 * A credential form, sign-in, account link, model key page or team export
 * page the chat sends a person to is the secure broker's own page. Web Chat
 * shows it inside the conversation in an iframe (the page's card view,
 * ``frame_url``), so the person never leaves the chat, while the chat page
 * still cannot see what they type:
 *
 * - the iframe is sandboxed without ``allow-same-origin`` (CARD_FRAME_SANDBOX,
 *   the same tokens the broker sends in the card view's own
 *   Content-Security-Policy, secure_tools/broker_pages.py). Behind a reverse
 *   proxy the broker shares the chat's address; only an opaque origin keeps the
 *   chat page out of the frame's document;
 * - the frame tells this page only its height and, at a confirmed end, its
 *   outcome (secure_tool_broker/page_assets/card_bridge.js). A message counts
 *   only when ``event.source`` is a card's own frame and its shape is exactly
 *   one of those two; the page sends the frame nothing;
 * - a frame that never reports (the browser refused it, a proxy added its own
 *   X-Frame-Options, the chat was reached on another address) is replaced by
 *   "Open in its own tab" (``onUnavailable``).
 *
 * The chat page keeps a live card's element across re-renders (``live``), because
 * an iframe that leaves the document reloads and loses what was typed. A card
 * is closed, and its frame removed, when its item leaves the conversation, the
 * thread changes or the person signs out (``retainOnly``, ``closeAll``).
 */

export const CARD_MESSAGE_TYPE = "ua-broker-card";
export const CARD_FRAME_SANDBOX = "allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox";
export const CARD_OUTCOMES = Object.freeze(["finished", "declined"]);
// The person's light or dark mode, sent so the card matches the chat (secure_tools/broker_pages.py).
export const CARD_COLOR_MODE_PARAM = "mode";

/** The card view address with the chat's color mode, when it is light or dark. */
export function withColorMode(frameUrl, mode) {
  if (mode !== "light" && mode !== "dark") return frameUrl;
  const url = new URL(frameUrl);
  url.searchParams.set(CARD_COLOR_MODE_PARAM, mode);
  return url.href;
}
// Heights the chat gives a frame: never so small a page cannot show its first
// line, never taller than most of the window (a long page scrolls inside).
export const MIN_FRAME_HEIGHT = 64;
const MAX_FRAME_SHARE_OF_WINDOW = 0.7;
// After the frame loaded, how long the page has to say it is there.
export const FIRST_MESSAGE_AFTER_LOAD_MS = 3000;
// How long a frame that never finishes loading may take before the card gives up.
export const LOAD_LIMIT_MS = 20000;

/** Whether a message is exactly one the bridge sends: {type, height} or {type, outcome}. */
export function isCardMessage(data) {
  if (!data || typeof data !== "object" || Array.isArray(data) || data.type !== CARD_MESSAGE_TYPE) {
    return false;
  }
  const keys = Object.keys(data).sort().join(",");
  if (keys === "height,type") {
    return typeof data.height === "number" && Number.isFinite(data.height) && data.height > 0;
  }
  return keys === "outcome,type" && CARD_OUTCOMES.includes(data.outcome);
}

/**
 * The chat page's broker cards.
 *
 * ``open`` builds and registers the frame for one item; the chat places the
 * returned iframe inside the card element it passes, and gets
 * ``onOutcome(outcome)`` once at a confirmed end or ``onUnavailable()`` when
 * the frame never reported.
 */
export function createBrokerCards({ browser, document, lifecycle, logger = browser.console }) {
  const cards = new Map();

  function maxHeight() {
    return Math.max(MIN_FRAME_HEIGHT, Math.round((browser.innerHeight || 800) * MAX_FRAME_SHARE_OF_WINDOW));
  }

  function cardForSource(source) {
    if (!source) return null;
    for (const card of cards.values()) {
      if (card.frame.contentWindow === source) return card;
    }
    return null;
  }

  function clearTimers(card) {
    if (card.loadTimer !== null) lifecycle.clearTimeout(card.loadTimer);
    if (card.firstMessageTimer !== null) lifecycle.clearTimeout(card.firstMessageTimer);
    card.loadTimer = null;
    card.firstMessageTimer = null;
  }

  function onMessage(event) {
    const card = cardForSource(event.source);
    if (!card || !isCardMessage(event.data)) return;
    card.heard = true;
    clearTimers(card);
    if ("height" in event.data) {
      const height = Math.min(maxHeight(), Math.max(MIN_FRAME_HEIGHT, Math.ceil(event.data.height)));
      card.frame.style.height = `${height}px`;
      return;
    }
    if (card.outcome) return;
    card.outcome = event.data.outcome;
    const { onOutcome } = card;
    close(card.itemId);
    onOutcome(card.outcome);
  }
  lifecycle.listen(browser, "message", onMessage);

  function giveUp(card, reason) {
    if (cards.get(card.itemId) !== card || card.heard) return;
    logger.warn(`A secure page could not be shown in its chat card (${reason}); offering it in its own tab.`, card.itemId);
    const { onUnavailable } = card;
    close(card.itemId);
    onUnavailable();
  }

  function open({ itemId, frameUrl, title, element, onOutcome, onUnavailable }) {
    close(itemId);
    const frame = document.createElement("iframe");
    // The sandbox must be in place before the frame navigates.
    frame.setAttribute("sandbox", CARD_FRAME_SANDBOX);
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.setAttribute("allow", "clipboard-write");
    frame.setAttribute("title", title || "Secure page");
    frame.className = "broker-card-frame";
    frame.style.height = "160px";
    const card = {
      itemId, frame, element, onOutcome, onUnavailable,
      heard: false, outcome: "", loadTimer: null, firstMessageTimer: null,
    };
    cards.set(itemId, card);
    frame.addEventListener("load", () => {
      if (cards.get(itemId) !== card || card.heard || card.firstMessageTimer !== null) return;
      card.firstMessageTimer = lifecycle.timeout(() => giveUp(card, "no word after loading"), FIRST_MESSAGE_AFTER_LOAD_MS);
    });
    card.loadTimer = lifecycle.timeout(() => giveUp(card, "did not load"), LOAD_LIMIT_MS);
    frame.setAttribute("src", frameUrl);
    return frame;
  }

  /** The element of a card that is still open, to reuse instead of building a new frame. */
  function live(itemId) {
    const card = cards.get(itemId);
    return card ? card.element : null;
  }

  function close(itemId) {
    const card = cards.get(itemId);
    if (!card) return;
    cards.delete(itemId);
    clearTimers(card);
    card.frame.remove();
  }

  /** Close every card whose item is not in ``itemIds`` (it left the conversation, or the thread changed). */
  function retainOnly(itemIds) {
    const keep = new Set(itemIds);
    for (const itemId of [...cards.keys()]) {
      if (!keep.has(itemId)) close(itemId);
    }
  }

  function closeAll() {
    retainOnly([]);
  }

  return { open, live, close, retainOnly, closeAll, openItemIds: () => [...cards.keys()] };
}

/**
 * Make ``container``'s children exactly ``nodes`` without detaching a node in ``keep``.
 *
 * Every other child is removed and the new ones are inserted around the kept
 * ones, so a live frame never leaves the document. When the kept nodes would
 * have to change order (not expected: items keep their place in the
 * conversation) it rebuilds instead and returns false.
 */
export function replaceChildrenKeeping(container, nodes, keep) {
  const kept = nodes.filter((node) => keep.has(node));
  for (const child of [...container.children]) {
    if (!kept.includes(child)) child.remove();
  }
  const present = [...container.children];
  const sameOrder = present.length === kept.length && present.every((node, index) => node === kept[index]);
  if (!sameOrder) {
    container.replaceChildren(...nodes);
    return false;
  }
  let next = 0;
  for (const node of nodes) {
    if (node === kept[next]) {
      next += 1;
      continue;
    }
    container.insertBefore(node, kept[next] || null);
  }
  return true;
}

// How a broker card stands in the chat after its frame closed. The card keeps
// this state while its item stays in the conversation, so a re-render shows
// the same line instead of a new frame.
export const BROKER_CARD_FINISHED = "finished";
export const BROKER_CARD_DECLINED = "declined";
export const BROKER_CARD_UNAVAILABLE = "unavailable";
export const BROKER_CARD_EXPIRED = "expired";
// The one line a closed card keeps. True whether a task waits on the page or a
// person opened it with a command.
export const BROKER_CARD_NOTES = Object.freeze({
  [BROKER_CARD_FINISHED]: "Done. This secure page is closed.",
  [BROKER_CARD_DECLINED]: "Declined. Nothing was saved.",
  [BROKER_CARD_UNAVAILABLE]: "This secure page could not be shown here. Open it in its own tab.",
});
export const OPEN_IN_OWN_TAB_LABEL = "Open in its own tab";

function element(document, tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function notice(document, item, extraClass) {
  const node = element(document, "div", `message-transient-notice broker-card ${extraClass}`.trim());
  node.setAttribute("data-item-id", item.itemId);
  node.setAttribute("data-broker-card", "");
  if (item.createdAt) node.setAttribute("data-created-at", item.createdAt);
  return node;
}

// The card's one line about the page: the first line of the item's text, which
// is the task's own reason (tool_setup) or what the page is for (sign-ins). The
// rest of that text is the "goes to the secure broker" note, which the page's
// own footer already says once.
function reasonLine(item) {
  return String(item.content || "").split("\n")[0].trim();
}

function reason(document, item) {
  const line = reasonLine(item);
  return line ? [element(document, "p", "broker-card-reason", line)] : [];
}

function openInOwnTab(document, item) {
  // A quiet text link beside the countdown: the page's own buttons stay the loudest controls.
  const link = element(document, "a", "message-transient-open broker-card-open", OPEN_IN_OWN_TAB_LABEL);
  link.setAttribute("href", item.url);
  link.setAttribute("target", "_blank");
  link.setAttribute("rel", "noopener noreferrer");
  return link;
}

function countdown(document, item) {
  if (!item.expiresAtUtc) return [];
  // The chat's countdown timer fills it ("9:59 remaining", credential_controls.js).
  const value = element(document, "span", "message-transient-countdown");
  value.setAttribute("data-expires-at", item.expiresAtUtc);
  value.setAttribute("role", "status");
  return [value];
}

/**
 * The chat element of one broker page item.
 *
 * - ``state`` finished or declined: the one-line note; expired: ``expiredMessage``;
 * - unavailable (the frame never reported): the reason, the line saying so,
 *   "Open in its own tab" and the countdown;
 * - otherwise the live card: the reason, the sandboxed frame and a footer with
 *   the countdown and "Open in its own tab". An open card is reused as is.
 *
 * Decline is inside the frame (the broker's own button), so the card has no
 * Dismiss. ``onState`` receives finished, declined or unavailable.
 */
export function brokerCardElement({ document, cards, item, state, expiredMessage, onState }) {
  if (state === BROKER_CARD_FINISHED || state === BROKER_CARD_DECLINED || state === BROKER_CARD_EXPIRED) {
    const closed = notice(document, item, "is-closed");
    closed.appendChild(element(document, "p", "broker-card-note", state === BROKER_CARD_EXPIRED ? expiredMessage : BROKER_CARD_NOTES[state]));
    return closed;
  }
  if (state === BROKER_CARD_UNAVAILABLE) {
    const linked = notice(document, item, "is-unavailable");
    linked.append(...reason(document, item));
    linked.appendChild(element(document, "p", "broker-card-note", BROKER_CARD_NOTES[BROKER_CARD_UNAVAILABLE]));
    const actions = element(document, "div", "message-transient-actions broker-card-footer");
    actions.append(...countdown(document, item), openInOwnTab(document, item));
    linked.appendChild(actions);
    return linked;
  }
  const live = cards.live(item.itemId);
  if (live) return live;
  const card = notice(document, item, "is-framed");
  card.setAttribute("data-broker-frame", "");
  card.append(...reason(document, item));
  const host = element(document, "div", "broker-card-frame-host");
  card.appendChild(host);
  const footer = element(document, "div", "message-transient-actions broker-card-footer");
  footer.append(...countdown(document, item), openInOwnTab(document, item));
  card.appendChild(footer);
  const root = document.documentElement;
  const frame = cards.open({
    itemId: item.itemId,
    frameUrl: withColorMode(item.frameUrl, root ? root.getAttribute("data-color-mode") : ""),
    title: item.label,
    element: card,
    onOutcome: (outcome) => onState(outcome),
    onUnavailable: () => onState(BROKER_CARD_UNAVAILABLE),
  });
  host.appendChild(frame);
  return card;
}
