/**
 * The team's live status for the whole Web Chat page.
 *
 * The gateway adds the team's status (`team_activity.members`, the same
 * fields as /api/web-chat/team) to every live page it sends Web Chat, about
 * every two seconds (chat_live_events.mjs). Three places show it: the
 * sidebar activity mesh (activity_mesh.mjs), Settings → Team
 * (team_controls.mjs) and the Tasks view's agent list (task_controls.js).
 * Each used to ask the gateway on its own timer (4 and 10 seconds); now one
 * read reaches all of them at once (owner's request 2026-10-04).
 *
 * `subscribe(listener)` calls `listener(members, { changed })` with every
 * update, and at once with the current members when there are any.
 * `changed` is false when only "last seen" times moved, so views that do not
 * show those times can skip the redraw. `current()` is null until the first
 * update arrives.
 */

function list(value) {
  return Array.isArray(value) ? value : [];
}

/** What a view compares between updates: everything except when each agent was last seen. */
export function activitySignature(members) {
  return JSON.stringify(list(members).map(member => ({
    agent_id: member.agent_id,
    name: member.name,
    named_agent_type: member.named_agent_type,
    is_team_lead: member.is_team_lead,
    status: member.status,
    available_skills: member.available_skills,
  })));
}

export function createTeamActivity() {
  const listeners = new Set();
  let members = null;
  let signature = "";

  function notify(listener, changed) {
    try {
      listener(members, { changed });
    } catch (error) {
      // One broken view must not stop the others from updating.
      globalThis.console.warn("ua.team_activity: a view failed to show the team's status.", error);
    }
  }

  return {
    /** A live page arrived. `activity` is its `team_activity` (null when the person may not see it). */
    publish(activity) {
      if (!activity || typeof activity !== "object") return;
      members = list(activity.members);
      const next = activitySignature(members);
      const changed = next !== signature;
      signature = next;
      [...listeners].forEach(listener => notify(listener, changed));
    },
    subscribe(listener) {
      listeners.add(listener);
      if (members) notify(listener, true);
      return () => { listeners.delete(listener); };
    },
    current: () => members,
    /** Sign-out: forget the team; views keep their subscriptions and get the next sign-in's updates. */
    clear() {
      members = null;
      signature = "";
      [...listeners].forEach(listener => notify(listener, true));
    },
  };
}
