/**
 * Pieces of the Knowledge page (knowledge_page.mjs) that only turn data into
 * words or nodes: change and page-type names, review flags, the line diff of
 * a proposed change, and the inline page editor.
 *
 * Everything is set as text (textContent), never as HTML; page bodies are
 * rendered by the caller through Web Chat's escaped Markdown renderer.
 */
import { element, text } from "./settings_support.mjs";

export const CHANGE_WORDS = Object.freeze({
  create: "Created", edit: "Edited", archive: "Archived", restore: "Restored", revert: "Reverted", merge: "Merged",
});
export const MISSING_LINK_HINT = "no page yet";
// `[[target]]` or `[[target|label]]`, as knowledge_base/text.py reads links.
const LINK_PATTERN = /\[\[([^\[\]|\n]{1,200})(?:\|([^\[\]\n]{0,200}))?\]\]/g;
// Link syntax inside these stays as written: code is shown exactly, and
// links and buttons are never nested.
const KEEP_AS_WRITTEN = new Set(["CODE", "PRE", "A", "BUTTON"]);
const TEXT_NODE = 3;
const ELEMENT_NODE = 1;
export const PROPOSAL_WORDS = Object.freeze({
  create: "New page", edit: "Edit", note: "Note", archive: "Archive",
});
export const SPACE_WORDS = Object.freeze({ team: "Team page", personal: "Personal page" });
// Above this many line pairs the diff would be slow; the page shows before and after instead.
export const DIFF_LIMIT = 250000;

/** "How-to", "Reference": a page type as a word. */
export function pageTypeWord(value) {
  const type = text(value);
  return type ? type.charAt(0).toUpperCase() + type.slice(1) : "";
}

/** "an email address in the body, line 3, column 10": where a flag is, never what it matched. */
export function describeFlag(flag) {
  const parts = [text(flag.label) || text(flag.kind)];
  const place = [];
  if (text(flag.field)) place.push("the " + text(flag.field));
  if (Number(flag.line) > 0) place.push("line " + Number(flag.line));
  if (Number(flag.column) > 0) place.push("column " + Number(flag.column));
  if (place.length) parts.push("(" + place.join(", ") + ")");
  return parts.join(" ");
}

/** A list of flags as items under a heading, for review and refusal messages. */
export function flagList(document, flags) {
  const list = element(document, "ul", "knowledge-flags");
  (Array.isArray(flags) ? flags : []).forEach(flag => list.append(element(document, "li", "", describeFlag(flag))));
  return list;
}

/**
 * Lines of `after` compared with `before`: `{ kind: "same" | "added" | "removed", line }`.
 * Longest common subsequence over lines; null when the texts are too long to compare.
 */
export function lineDiff(before, after) {
  const a = String(before || "").split("\n");
  const b = String(after || "").split("\n");
  if (a.length * b.length > DIFF_LIMIT) return null;
  const lengths = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      lengths[i][j] = a[i] === b[j] ? lengths[i + 1][j + 1] + 1 : Math.max(lengths[i + 1][j], lengths[i][j + 1]);
    }
  }
  const lines = [];
  let i = 0, j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { lines.push({ kind: "same", line: a[i] }); i += 1; j += 1; }
    else if (lengths[i + 1][j] >= lengths[i][j + 1]) { lines.push({ kind: "removed", line: a[i] }); i += 1; }
    else { lines.push({ kind: "added", line: b[j] }); j += 1; }
  }
  a.slice(i).forEach(line => lines.push({ kind: "removed", line }));
  b.slice(j).forEach(line => lines.push({ kind: "added", line }));
  return lines;
}

const DIFF_MARKS = Object.freeze({ same: "  ", added: "+ ", removed: "- " });

/** The body change as marked lines ("+ " added, "- " removed), so color is never the only sign. */
export function diffBlock(document, before, after) {
  const lines = lineDiff(before, after);
  if (lines === null) {
    const both = element(document, "div", "knowledge-before-after");
    both.append(
      element(document, "h4", "", "Before"), element(document, "pre", "knowledge-diff", text(before)),
      element(document, "h4", "", "After"), element(document, "pre", "knowledge-diff", text(after)),
    );
    return both;
  }
  const block = element(document, "pre", "knowledge-diff");
  block.setAttribute("aria-label", "Changes to the page text: lines starting with + are added, with - removed");
  lines.forEach(({ kind, line }) => {
    block.append(element(document, "span", "knowledge-diff-line is-" + kind, DIFF_MARKS[kind] + line + "\n"));
  });
  return block;
}

/** Title, summary and tags that a proposal changes, as "Field: before → after" rows. */
export function fieldChanges(document, current, proposed) {
  const rows = [
    ["Title", text(current.title), text(proposed.title)],
    ["Summary", text(current.summary), text(proposed.summary)],
    ["Tags", (current.tags || []).join(", "), (proposed.tags || []).join(", ")],
  ].filter(([, before, after]) => before !== after);
  const list = element(document, "dl", "knowledge-field-changes");
  rows.forEach(([label, before, after]) => {
    list.append(element(document, "dt", "", label), element(document, "dd", "", (before || "(none)") + " → " + (after || "(none)")));
  });
  return rows.length ? list : null;
}

export function parseTags(value) {
  return String(value || "").split(",").map(tag => tag.trim()).filter(Boolean);
}

function editorField(document, label, input, help) {
  const wrap = element(document, "label", "memory-editor-field");
  wrap.append(element(document, "span", "memory-editor-label", label), input);
  if (help) wrap.append(element(document, "small", "memory-editor-help", help));
  return wrap;
}

/**
 * The inline editor for a page (Memories style): title, summary, tags and
 * text, with Save and Cancel. Returns the form and its fields; the caller
 * owns submitting, so what the person typed stays after a refusal.
 */
export function pageEditor(document, page, { saveLabel = "Save", heading = "" } = {}) {
  const form = element(document, "form", "memory-editor knowledge-editor");
  const title = element(document, "input", "memory-editor-input");
  title.type = "text";
  title.value = text(page.title);
  title.maxLength = 1000;
  const summary = element(document, "input", "memory-editor-input");
  summary.type = "text";
  summary.value = text(page.summary);
  summary.maxLength = 4000;
  const tags = element(document, "input", "memory-editor-input");
  tags.type = "text";
  tags.value = (Array.isArray(page.tags) ? page.tags : []).join(", ");
  tags.placeholder = "Comma-separated tags";
  const body = element(document, "textarea", "memory-editor-text knowledge-editor-body");
  body.rows = 18;
  body.value = String(page.body || "");
  const message = element(document, "div", "knowledge-message");
  message.hidden = true;
  const actions = element(document, "div", "memory-editor-actions");
  const save = element(document, "button", "button primary memory-action-button", saveLabel);
  save.type = "submit";
  const cancel = element(document, "button", "button ghost memory-action-button", "Cancel");
  cancel.type = "button";
  actions.append(save, cancel);
  if (heading) form.append(element(document, "h3", "knowledge-editor-heading", heading));
  form.append(
    message,
    editorField(document, "Title", title),
    editorField(document, "Summary", summary, "One line that says what the page is for."),
    editorField(document, "Tags", tags),
    editorField(document, "Text", body, "Markdown. Link to another page with [[page-name]]."),
    actions,
  );
  return {
    form, message, actions, save, cancel,
    values() {
      return {
        title: String(title.value || "").trim(), summary: String(summary.value || "").trim(),
        tags: parseTags(tags.value), body: String(body.value || ""),
      };
    },
    focus() { title.focus(); },
  };
}

function linkNode(document, link, label, open) {
  if (!link.exists) {
    const missing = element(document, "span", "knowledge-missing-link", label);
    missing.append(element(document, "span", "knowledge-missing-hint", " (" + MISSING_LINK_HINT + ")"));
    return missing;
  }
  const node = element(document, "button", "knowledge-page-link", label);
  node.type = "button";
  node.setAttribute("aria-label", "Open the page " + label);
  node.addEventListener("click", () => open(link.space, link.page));
  return node;
}

function linkTextNode(document, textNode, links, open) {
  const value = String(textNode.data !== undefined ? textNode.data : textNode.textContent || "");
  const matches = [...value.matchAll(LINK_PATTERN)].filter(match => links.has(match[1].trim()));
  if (!matches.length) return;
  const wrap = element(document, "span", "knowledge-linked-text");
  let last = 0;
  matches.forEach(match => {
    const written = match[1].trim();
    wrap.append(document.createTextNode(value.slice(last, match.index)),
      linkNode(document, links.get(written), text(match[2]) || written, open));
    last = match.index + match[0].length;
  });
  wrap.append(document.createTextNode(value.slice(last)));
  textNode.replaceWith(wrap);
}

/**
 * Turns `[[links]]` in a rendered page body into buttons that open the page
 * (`open(space, page)`), walking text nodes after the Markdown renderer ran,
 * never rewriting HTML. `links` is the server's list for the shown text: an
 * existing page (or a merged page's name) becomes a button; a link to no page
 * stays text with a "no page yet" hint. Code is left as written.
 */
export function linkify(document, root, links, open) {
  const byWritten = new Map((Array.isArray(links) ? links : []).map(link => [text(link.written), link]));
  if (!byWritten.size) return;
  const visit = node => {
    Array.from(node.childNodes || []).forEach(child => {
      if (child.nodeType === TEXT_NODE) linkTextNode(document, child, byWritten, open);
      else if (child.nodeType === ELEMENT_NODE && !KEEP_AS_WRITTEN.has(child.tagName)) visit(child);
    });
  };
  visit(root);
}
