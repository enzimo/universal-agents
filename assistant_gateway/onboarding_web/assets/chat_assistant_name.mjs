/**
 * The assistant's name in every Web Chat page header (Chat, Objectives, Files,
 * Status and Knowledge). Each header marks its name with
 * `data-assistant-name`, so the name is set in one place and a page added
 * later shows the same name instead of the "Lisa" placeholder in chat.html.
 */
export const ASSISTANT_NAME_SELECTOR = "[data-assistant-name]";

/** Records the assistant's name and shows it in every header; an empty name keeps the current one. */
export function setAssistantName(sharedState, root, name) {
  sharedState.assistantName = name || sharedState.assistantName;
  root.querySelectorAll(ASSISTANT_NAME_SELECTOR).forEach(function (label) {
    label.textContent = sharedState.assistantName;
  });
}
