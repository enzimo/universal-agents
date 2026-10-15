import { renderWorkflowGraph } from "./workflow_graph.mjs";

/**
 * Enhance only an authenticated, owner-readable workflow link. Prose grants nothing.
 *
 * `loaded` maps workflow ids to the last workflow read for this sign-in (the
 * caller replaces it at each sign-in). The transcript is rebuilt on every
 * change, so a card drawn from it keeps its full size at once instead of
 * shrinking to "Loading…" and growing back when the read returns, which moved
 * the rows around it. Every mount still reads the workflow again, so the card
 * shows its current status.
 */
export function mountWorkflowCard(bubble, { lifecycle, requestClient, isCurrent, loaded = new Map() }) {
  const links = [...bubble.querySelectorAll("a[href]")];
  const link = links.find((candidate) => {
    const url = new URL(candidate.href, location.origin);
    return url.origin === location.origin && /^\/workflows\/access_flow_[a-zA-Z0-9_-]+$/.test(url.pathname);
  });
  if (!link) return;
  const id = new URL(link.href).pathname.split("/").at(-1);
  const card = document.createElement("section");
  card.className = "workflow-inline-card";
  const heading = document.createElement("h3");
  heading.textContent = "Your Access Plan";
  const status = document.createElement("p");
  status.textContent = "Loading the current workflow…";
  const map = document.createElement("div");
  map.className = "workflow-map";
  const open = document.createElement("a");
  open.href = link.href;
  open.textContent = "Open Full Workflow";
  open.className = "button primary";
  card.append(heading, status, map, open);
  bubble.append(card);
  const show = (record) => {
    heading.textContent = record.title;
    status.textContent = `For ${record.target_agent_type}. ${record.goal}`;
    renderWorkflowGraph(map, record, (title, description) => {
      const dialog = document.createElement("dialog");
      dialog.className = "workflow-inline-info";
      const label = document.createElement("h3"); label.textContent = title;
      label.id = `workflow-info-${crypto.randomUUID()}`;
      dialog.setAttribute("aria-labelledby", label.id);
      const text = document.createElement("p"); text.textContent = description;
      const close = document.createElement("button"); close.type = "button"; close.className = "button primary"; close.textContent = "Close";
      lifecycle.listen(close, "click", () => dialog.close());
      lifecycle.listen(dialog, "close", () => dialog.remove(), { once: true });
      dialog.append(label, text, close); bubble.append(dialog); dialog.showModal();
    });
  };
  if (loaded.has(id)) show(loaded.get(id));
  requestClient.fetch(`/api/web-chat/workflows/${encodeURIComponent(id)}`, { cache: "no-store", credentials: "same-origin" })
    .then(async (response) => {
      if (!response.ok) throw new Error("Open the workflow to review its current status.");
      return response.json();
    }).then((payload) => {
      if (!isCurrent()) return;
      loaded.set(id, payload.workflow);
      if (bubble.isConnected) show(payload.workflow);
    }).catch((error) => {
      if (!isCurrent()) return;
      // A failed read shows its message; later renders do not show the old copy.
      loaded.delete(id);
      if (bubble.isConnected) status.textContent = error.message;
    });
}
