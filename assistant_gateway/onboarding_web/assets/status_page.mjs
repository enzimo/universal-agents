/**
 * The Status page (`/webchat?view=status`): what the framework flags to
 * administrators about itself, now.
 *
 * The gateway gathers the findings (`assistant_gateway/status_page.py`) and
 * returns one page of at most 20, problems first, with counts per severity and
 * the sources it could not ask. This module only renders them: every value is
 * set as text, never HTML. Severity is always a word beside its colored dot
 * (ui/DESIGN.md), and technical details stay collapsed.
 */
import { LIST_PAGE_SIZE, element, failureMessage, isForbidden, localTimeFormatter, setStatusLine, text } from "./settings_support.mjs";

export const STATUS_API = "/api/web-chat/status";
export const SEVERITY_LABELS = Object.freeze({ problem: "Problem", warning: "Warning", note: "Note" });
const SEVERITY_TONES = Object.freeze({ problem: "is-danger", warning: "is-warning", note: "" });
const PLURAL_LABELS = Object.freeze({ problem: "Problems", warning: "Warnings", note: "Notes" });
export const EMPTY_TEXT = "Nothing needs attention. New findings appear here as soon as the framework sees them.";
export const FORBIDDEN_TEXT = "Only team administrators and owners can see the Status page.";

/** "2 Problems", "1 Warning": the summary line's words, in severity order, leaving out zero counts. */
export function severityCounts(counts) {
  return Object.keys(SEVERITY_LABELS)
    .map(severity => [severity, Number(counts && counts[severity]) || 0])
    .filter(([, count]) => count > 0)
    .map(([severity, count]) => ({
      severity,
      label: count + " " + (count === 1 ? SEVERITY_LABELS[severity] : PLURAL_LABELS[severity]),
    }));
}

/** Renders the Status page into `content` and keeps it current on Refresh. */
export function createStatusPage({ document, content, status, refreshButton, updatedAt, client, formatTime }) {
  const localTime = formatTime || localTimeFormatter();
  let offset = 0;

  function severityChip(severity, label) {
    const tone = SEVERITY_TONES[severity] || "";
    return element(document, "span", ("team-presence status-severity " + tone).trim(), label || SEVERITY_LABELS[severity] || severity);
  }

  function findingItem(finding) {
    const item = element(document, "li", "status-finding");
    const heading = element(document, "div", "status-finding-heading");
    heading.append(severityChip(text(finding.severity)), element(document, "strong", "status-finding-title", text(finding.title)));
    item.append(heading);
    if (text(finding.summary)) item.append(element(document, "p", "status-finding-summary", text(finding.summary)));
    if (text(finding.action)) {
      const action = element(document, "p", "status-finding-action");
      action.append(element(document, "span", "status-finding-label", "What to do: "), element(document, "span", "", text(finding.action)));
      item.append(action);
    }
    const meta = ["Reported by " + (text(finding.source) || "the gateway")];
    if (text(finding.since)) meta.push("since " + localTime(finding.since));
    item.append(element(document, "p", "task-metadata", meta.join(" · ")));
    if (text(finding.details)) {
      const details = element(document, "details", "status-finding-details");
      details.append(element(document, "summary", "", "Technical Details"), element(document, "pre", "", text(finding.details)));
      item.append(details);
    }
    return item;
  }

  function unavailableCallout(sources) {
    const callout = element(document, "div", "warning-callout");
    callout.setAttribute("role", "alert");
    callout.append(
      element(document, "span", "warning-icon", "!"),
      element(document, "p", "", "These sources did not answer, so their findings are missing: " + sources.join(", ") + "."),
    );
    return callout;
  }

  function render(payload) {
    content.replaceChildren();
    const sources = Array.isArray(payload.unavailable_sources) ? payload.unavailable_sources.map(text).filter(Boolean) : [];
    if (sources.length) content.append(unavailableCallout(sources));
    const findings = Array.isArray(payload.findings) ? payload.findings : [];
    if (!findings.length && !Number(payload.total)) {
      content.append(element(document, "p", "status-empty", EMPTY_TEXT));
    } else {
      const summary = element(document, "div", "status-summary");
      severityCounts(payload.counts).forEach(({ severity, label }) => summary.append(severityChip(severity, label)));
      const list = element(document, "ul", "status-findings");
      findings.forEach(finding => list.append(findingItem(finding)));
      content.append(summary, list);
      if (Number(payload.total) > LIST_PAGE_SIZE) {
        content.append(client.paginate({ total: Number(payload.total), offset, pageSize: LIST_PAGE_SIZE, label: "Status pages", onPage: load }));
      }
    }
    if (updatedAt) updatedAt.textContent = text(payload.checked_at) ? "Checked " + localTime(payload.checked_at) : "";
  }

  function load(nextOffset) {
    offset = Number.isInteger(nextOffset) ? nextOffset : offset;
    const done = client.beginRefresh(refreshButton);
    return client.get(STATUS_API + "?offset=" + offset, "The Status page could not be loaded.")
      .then(payload => {
        setStatusLine(status, "", false);
        render(payload);
      })
      .catch(error => {
        if (isForbidden(error)) {
          content.replaceChildren(element(document, "p", "status-empty", FORBIDDEN_TEXT));
          setStatusLine(status, "", false);
          return;
        }
        setStatusLine(status, failureMessage(error, "The Status page could not be loaded."), true);
      })
      .finally(done);
  }

  if (refreshButton) refreshButton.addEventListener("click", () => load(offset));
  return { load, render };
}
