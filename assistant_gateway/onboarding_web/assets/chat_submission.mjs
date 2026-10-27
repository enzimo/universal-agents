/** Keep a draft until its destination exists. Never resolve a send from the current selection later. */
export function createChatSubmission({ readDraft, context, createThread, takeDraft, send, preparing, failed }) {
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
    // Slash commands keep their existing routing, including /new. The switch
    // describes ordinary messages and must not create a second empty chat.
    const command = draft.content.startsWith("/") && !draft.hasAttachments;
    pending = attempt;
    preparing(true);
    try {
      let thread = origin.thread;
      // The switch launches a parallel thread: the server runs its requests
      // beside the agent's other work. Without a thread, a plain one starts.
      if (!command && draft.separate) thread = await createThread({ parallelThread: true });
      else if (!command && !thread) thread = await createThread();
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
