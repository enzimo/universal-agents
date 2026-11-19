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
