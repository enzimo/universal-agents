/** Keep a draft until its destination exists. Never resolve a send from the current selection later. */
export function createChatSubmission({ readDraft, context, createThread, setParallel, takeDraft, send, preparing, failed }) {
  let pending = null;

  async function submit() {
    const origin = context();
    if (pending || !origin.authenticated) return;
    const draft = readDraft();
    if (!draft.content && !draft.hasAttachments) return;
    const attempt = {};
    const current = () => {
      const now = context();
      return pending === attempt && now.authenticated && now.generation === origin.generation;
    };
    // Slash commands keep their existing routing, including /new, and never
    // create a second empty chat.
    const command = draft.content.startsWith("/") && !draft.hasAttachments;
    pending = attempt;
    preparing(true);
    try {
      // Keep the selected conversation, including its model history. Run
      // concurrently is the thread's saved setting, so the message carries
      // nothing. With no chat yet, the new chat takes the switch's position
      // before the message is sent; if that fails, the draft stays.
      let thread = origin.thread;
      if (!command && !thread) {
        thread = await createThread();
        if (thread && draft.parallel && current()) await setParallel(thread, true);
      }
      if (!current() || (!thread && !command)) return;
      const attachments = takeDraft();
      send(draft.content, { thread, attachments });
    } catch (error) {
      if (current()) failed(error, submit);
    } finally {
      if (pending === attempt) {
        pending = null;
        preparing(false);
      }
    }
  }

  return {
    submit,
    isPreparing: () => Boolean(pending),
    reset() { pending = null; preparing(false); },
  };
}
