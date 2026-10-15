/**
 * Links that open the web chat at a panel.
 *
 * `/webchat?view=objectives`, `/webchat?view=files` and `/webchat?view=knowledge` are separate pages, so
 * their address stays as it is. `/webchat?view=inbox#access_request=<id>` and
 * `/webchat?view=access#agent=<type>` are one-time requests to open a panel
 * over the chat after sign-in. Once read, the address goes back to plain chat
 * so a reload stays on the chat instead of reopening the panel, and the page
 * counts as the chat page for browser chat alerts.
 */
import { parseAccessRequestLink } from "./access_inbox.mjs";
import { parseAccessMapLink } from "./access_map.mjs";

export const VIEW_PARAM = "view";
export const INBOX_VIEW = "inbox";
export const ACCESS_VIEW = "access";
// Separate pages, like objectives and files: their address stays as it is.
export const KNOWLEDGE_VIEW = "knowledge";
const PANEL_VIEWS = Object.freeze([INBOX_VIEW, ACCESS_VIEW]);

/**
 * Reads the requested view from a location (`{ pathname, search, hash }`).
 * `cleanUrl` is the address to put back without reloading, or "" when the
 * address should stay as it is.
 */
export function readChatPageLink(location) {
  const params = new URLSearchParams(location.search || "");
  const view = params.get(VIEW_PARAM) || "";
  if (!PANEL_VIEWS.includes(view)) {
    return { view, accessRequestId: "", accessAgentType: "", cleanUrl: "" };
  }
  params.delete(VIEW_PARAM);
  const query = params.toString();
  return {
    view,
    accessRequestId: view === INBOX_VIEW ? parseAccessRequestLink(location.hash) : "",
    accessAgentType: view === ACCESS_VIEW ? parseAccessMapLink(location.hash) : "",
    cleanUrl: location.pathname + (query ? "?" + query : ""),
  };
}
