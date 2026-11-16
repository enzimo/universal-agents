// The chat ("space") an inbound Photon message came from, as the Python app
// reads it (assistant_gateway/interactive_apps/photon.py).
//
// iMessage spaces carry `type` ("dm" | "group") and `phone` directly. A type
// the provider did not report stays null rather than a guess: the Python app
// treats only "dm" as a private chat (commands such as /upgrade, whose reply
// holds a private link, run only there), and keeps its own rule for which
// group allowlist applies.
export function normalizeSpace(space, messageSpace) {
  const fromMessage = messageSpace || {};
  return {
    id: space.id ?? fromMessage.id ?? null,
    type: space.type ?? fromMessage.type ?? null,
    phone: space.phone ?? fromMessage.phone ?? null,
  };
}
