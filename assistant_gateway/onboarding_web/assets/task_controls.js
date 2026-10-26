(function () {
  "use strict";

  var DESCRIPTION_PREVIEW_LIMIT = 280, INFO_PREVIEW_LIMIT = 240;

  function node(tag, className, text) {
    var result = document.createElement(tag);
    result.className = className || "";
    if (text !== undefined) result.textContent = text;
    return result;
  }

  function create(options) {
    var panel = options.panel;
    var content = panel.querySelector(".tasks-content");
    var status = panel.querySelector(".tasks-status");
    var summary = panel.querySelector(".tasks-summary");
    var confirmation = panel.querySelector(".tasks-confirmation");
    var purgeConfirmation = confirmation, confirmationTrigger = null;
    var refresh = panel.querySelector(".tasks-refresh");
    var purge = panel.querySelector(".tasks-purge");
    var finished = options.view === "finished", pageSize = finished ? 10 : 20;
    if (finished) purge.hidden = true;
    var session = "", version = 0, offset = 0, timer = 0;
    var snapshot = null, active = false, loading = false, busy = false;
    var pendingConfirmation = false;

    function message(text, error) {
      status.textContent = text;
      status.hidden = !text;
      status.classList.toggle("is-error", Boolean(error));
    }

    async function responseJson(response) {
      var body = await response.text();
      var payload;
      try { payload = JSON.parse(body); } catch (_) { payload = { message: body }; }
      if (!response.ok) {
        if (options.onUnauthorized) options.onUnauthorized(response);
        throw new Error(payload.message || "The task request failed. Refresh and try again.");
      }
      return payload;
    }

    function cancelConfirmation(restoreFocus) {
      pendingConfirmation = false;
      confirmation.replaceChildren();
      confirmation.hidden = true;
      if (confirmation !== purgeConfirmation) confirmation.remove();
      confirmation = purgeConfirmation;
      if (restoreFocus === true && confirmationTrigger && confirmationTrigger.isConnected) {
        confirmationTrigger.focus({ preventScroll: true });
      }
      confirmationTrigger = null;
    }

    async function mutate(action, task, digest, button) {
      if (busy) return;
      busy = true;
      var requestVersion = version, requestSession = session;
      var finish = window.UAActionFeedback.begin(button, {
        busyLabel: action === "purge" ? "Purging…" : "Removing…", group: panel,
      });
      try {
        var result = await responseJson(await window.fetch("/api/web-chat/tasks/control", {
          method: "POST", credentials: "same-origin", headers: options.headers(),
          body: JSON.stringify({ session_key: requestSession, agent_id: options.agentId, action: action,
            task_id: task ? task.root_task_id : null, inventory_digest: digest }),
        }));
        if (requestVersion !== version || requestSession !== session) return;
        cancelConfirmation();
        finish();
        busy = false;
        var loaded = await load();
        if (requestVersion !== version) return;
        var unconfirmed = (result.cancellation_receipts || []).reduce(function (count, receipt) {
          return count + Number(receipt.unconfirmed_worker_count || 0);
        }, 0);
        message(result.message + (unconfirmed ? " " + unconfirmed + " worker stop confirmation(s) are still pending." : "")
          + (loaded ? "" : " The list could not be refreshed; select Refresh."), !result.success || !loaded);
      } catch (error) {
        if (requestVersion !== version || requestSession !== session) return;
        // A lost response does not tell us whether the mutation ran. Discard
        // the old confirmation and fetch state; never repeat the write here.
        cancelConfirmation();
        snapshot = null;
        finish();
        busy = false;
        purge.disabled = true;
        var refreshed = await load();
        if (requestVersion === version) message(
          (error.message || "The result of task removal is not confirmed.")
          + (refreshed ? " The list now shows the latest task state." : " The displayed list may be outdated; select Refresh."), true);
      } finally {
        finish();
        busy = false;
      }
    }

    function confirm(action, task, item, trigger) {
      if (!snapshot || busy) return;
      cancelConfirmation();
      confirmationTrigger = trigger;
      if (item) {
        confirmation = node("div", "tasks-confirmation");
        confirmation.setAttribute("role", "region");
        confirmation.setAttribute("aria-label", "Confirm task removal");
        item.querySelector("h4").after(confirmation);
      }
      pendingConfirmation = true;
      var digest = snapshot.inventory_digest;
      var copy = action === "purge"
        ? "Remove all " + snapshot.queued_count + " queued and waiting tasks owned by you? Current work is preserved."
        : "Stop and remove task " + task.root_task_id + " and its attached work?";
      var confirmButton = node("button", "button danger", action === "purge" ? "Confirm purge" : "Confirm removal");
      confirmButton.type = "button";
      confirmButton.addEventListener("click", function () { mutate(action, task, digest, confirmButton); });
      var cancel = node("button", "button ghost", "Cancel");
      cancel.type = "button";
      cancel.addEventListener("click", function () { cancelConfirmation(true); });
      var actions = node("div", "tasks-actions");
      actions.append(confirmButton, cancel);
      confirmation.replaceChildren(node("p", "", copy + " Cancellation history is kept. Recurring schedules are unchanged."), actions);
      confirmation.hidden = false;
      confirmButton.focus();
    }

    function render(payload) {
      var expanded = new Set(Array.from(content.querySelectorAll("details[open]")).map(function (entry) { return entry.dataset.taskKey; }));
      function taskText(task, field, text, className, limit, label) {
        var full = String(text), preview = full.replace(/\s+/g, " ").trim();
        var paragraph = node("p", className, preview);
        if (preview.length <= limit) return paragraph;
        paragraph.textContent = preview.slice(0, limit - 1).trimEnd() + "…";
        var details = node("details", "task-text-details");
        details.dataset.taskKey = JSON.stringify([task.owner_agent_id || options.agentId, task.root_task_id, field]);
        details.open = expanded.has(details.dataset.taskKey);
        var fullText = node("p", "task-full-text " + className, full);
        fullText.tabIndex = 0;
        fullText.setAttribute("aria-label", label);
        details.append(node("summary", "", "Full " + label.toLowerCase()), fullText);
        var block = node("div", "task-text");
        block.append(paragraph, details);
        return block;
      }
      summary.textContent = finished ? payload.total + " finished tasks owned by you · newest first"
        : payload.current_count + " current · " + payload.queued_count + " queued or waiting · " + payload.total + " total owned by you";
      purge.disabled = !payload.queued_count;
      var fragment = document.createDocumentFragment();
      if (!payload.tasks.length) fragment.append(node("p", "permission-empty-state", finished ? "No finished tasks owned by you." : "This agent has no unfinished tasks owned by you."));
      payload.tasks.forEach(function (task) {
        var item = node("article", "memory-item task-item");
        var copy = node("div", "memory-item-copy");
        var label = finished ? (task.owner_agent_name || task.owner_agent_id || "Agent")
          : task.is_current ? "Current work" : task.queue_position ? "Queue position " + task.queue_position : "Waiting work";
        if (!finished && task.is_parallel) label += " · Parallel";
        copy.append(node("h4", "", label), taskText(task, "description", task.problem_statement || "No task description.", "memory-fact", DESCRIPTION_PREVIEW_LIMIT, "Description"));
        copy.append(node("p", "task-metadata", task.root_task_id + " · " + task.state.replaceAll("_", " ")));
        if (task.blocker) copy.append(taskText(task, "blocker", "Waiting on: " + task.blocker, "task-metadata", INFO_PREVIEW_LIMIT, "Waiting reason"));
        if (task.latest_activity) copy.append(taskText(task, "activity", task.latest_activity, "task-metadata", INFO_PREVIEW_LIMIT, "Latest activity"));
        if (task.latest_activity_at) copy.append(node("p", "task-metadata", (finished ? "Finished " : "Updated ") + new Date(task.latest_activity_at).toLocaleString()));
        item.append(copy);
        if (finished) {
          if (task.owner_agent_id) copy.append(node("p", "task-metadata", task.owner_agent_id));
          var details = node("details", "task-result");
          details.dataset.taskKey = JSON.stringify([task.owner_agent_id, task.root_task_id]);
          details.open = expanded.has(details.dataset.taskKey);
          var answer = node("p", "memory-fact task-full-text", task.answer || "No final answer was recorded.");
          answer.tabIndex = 0;
          answer.setAttribute("aria-label", "Task result");
          details.append(node("summary", "", "View result"), answer);
          copy.append(details);
        } else {
          var remove = node("button", "button danger", task.is_current ? "Stop and remove" : "Remove");
          remove.type = "button";
          remove.addEventListener("click", function () { confirm("remove", task, item, remove); });
          item.append(remove);
        }
        fragment.append(item);
      });
      var navigation = window.UAPagination.create({ offset: offset, total: payload.total, pageSize: pageSize, label: finished ? "Finished task pages" : "Task pages",
        onPage: function (target) { offset = target; cancelConfirmation(); return load(); } });
      fragment.append(navigation);
      content.replaceChildren(fragment);
    }

    async function load(quiet) {
      if (!session || !active || loading || busy) return false;
      loading = true;
      var requestVersion = version, requestedOffset = offset;
      var finishRefresh = window.UAActionFeedback.beginRefresh(refresh);
      try {
        var payload = await responseJson(await window.fetch("/api/web-chat/tasks?session_key="
          + encodeURIComponent(session) + "&agent_id=" + encodeURIComponent(options.agentId) + "&offset=" + offset + "&limit=" + pageSize + "&view=" + (finished ? "finished" : "active"), { credentials: "same-origin" }));
        // A read started before Remove was clicked must not replace that row
        // and detach the confirmation while the user is reviewing it.
        if (requestVersion !== version || !active || pendingConfirmation) return false;
        if (offset !== requestedOffset) return false;
        if (offset && offset >= payload.total) {
          offset = Math.max(0, Math.floor((payload.total - 1) / pageSize) * pageSize);
          requestedOffset = offset;
          loading = false;
          return await load(quiet);
        }
        var changed = JSON.stringify(snapshot) !== JSON.stringify(payload);
        snapshot = payload;
        if (changed || !content.childElementCount) render(payload);
        if (!quiet) message("", false);
        return true;
      } catch (error) {
        if (requestVersion === version) message(error.message || "Tasks could not be loaded. Refresh to retry.", true);
        return false;
      } finally {
        finishRefresh();
        if (requestVersion === version) {
          loading = false;
          if (offset !== requestedOffset) load(quiet);
        }
      }
    }

    refresh.addEventListener("click", function () { cancelConfirmation(); load(); });
    purge.addEventListener("click", function () { confirm("purge", null, null, purge); });
    return {
      activate: function (key) {
        if (active && session === key) { load(); return; }
        if (session !== key) {
          version++; offset = 0; snapshot = null; content.replaceChildren(); summary.textContent = "";
          purge.disabled = true;
        }
        session = key; active = true; loading = false;
        window.clearInterval(timer);
        if (!session) { message("Open a conversation before managing tasks.", true); return; }
        load();
        timer = window.setInterval(function () {
          if (!pendingConfirmation && !document.hidden) load(true);
        }, 10000);
      },
      deactivate: function () {
        active = false; version++; loading = false;
        window.clearInterval(timer); cancelConfirmation();
      },
      clear: function () {
        this.deactivate(); session = ""; snapshot = null; offset = 0;
        content.replaceChildren(); summary.textContent = ""; message("", false); purge.disabled = true;
      },
    };
  }
  // Keep each agent's inventory, digest, paging and confirmation independent.
  // The agent list follows the team's live status on the page (team_activity.mjs,
  // `options.teamActivity`), the same copy the activity mesh and Settings →
  // Team show; each agent's own task list keeps its own reads.
  function createTeam(options) {
    var panel = options.panel;
    var template = panel.cloneNode(true);
    var content = panel.querySelector(".tasks-content");
    var status = panel.querySelector(".tasks-status");
    var refresh = panel.querySelector(".tasks-refresh");
    panel.querySelector(".tasks-purge").remove();
    var groups = new Map(), session = "", active = false, unsubscribe = null, shownIds = "";
    function sync(reported) {
      if (!active || !session) return;
      if (!reported) {
        status.textContent = "Waiting for the team's status…"; status.hidden = false; status.classList.remove("is-error");
        return;
      }
      var members = reported.slice().sort(function (left, right) {
        return Number(right.named_agent_type === "TeamArchitect") - Number(left.named_agent_type === "TeamArchitect");
      });
      // Updates arrive every few seconds; only a change in who is running redraws.
      var memberIds = members.map(function (member) { return member.agent_id; }).join("\n");
      if (memberIds === shownIds && content.children.length) return;
      shownIds = memberIds;
      var ids = new Set(members.map(function (member) { return member.agent_id; }));
      var history = groups.get("finished");
      if (history && ids.has(history.agentId)) ids.add("finished");
      groups.forEach(function (group, id) {
        if (!ids.has(id)) { group.controls.clear(); group.panel.remove(); groups.delete(id); }
      });
      if (members.length && !groups.has("finished")) {
        var historyPanel = template.cloneNode(true);
        historyPanel.removeAttribute("id"); historyPanel.removeAttribute("role"); historyPanel.removeAttribute("aria-labelledby");
        historyPanel.hidden = false; historyPanel.className = "tasks-history-panel";
        historyPanel.querySelector("h3").remove();
        historyPanel.querySelector(".settings-panel-heading p").textContent = "Recent results from all agents on this team, including agents that have stopped.";
        var disclosure = node("details", "tasks-agent-group tasks-finished");
        disclosure.append(node("summary", "", "Finished tasks"), historyPanel);
        var historyControls = create({ panel: historyPanel, agentId: members[0].agent_id, view: "finished",
          headers: options.headers, onUnauthorized: options.onUnauthorized });
        groups.set("finished", { panel: disclosure, controls: historyControls, agentId: members[0].agent_id });
        disclosure.addEventListener("toggle", function () {
          if (active && disclosure.open) historyControls.activate(session);
          else historyControls.deactivate();
        });
        content.append(disclosure);
      }
      members.forEach(function (member) {
        var group = groups.get(member.agent_id);
        if (!group) {
          var section = template.cloneNode(true);
          section.removeAttribute("id"); section.removeAttribute("role"); section.removeAttribute("aria-labelledby");
          section.hidden = false; section.className = "tasks-agent-group";
          section.dataset.agentId = member.agent_id;
          section.querySelector("h3").textContent = member.name + " · " + member.named_agent_type;
          section.querySelector(".settings-panel-heading p").textContent = member.agent_id;
          section.querySelector(".tasks-purge").textContent = "Purge this queue";
          var controls = create({ panel: section, agentId: member.agent_id,
            headers: options.headers, onUnauthorized: options.onUnauthorized });
          group = { panel: section, controls: controls };
          groups.set(member.agent_id, group);
          content.appendChild(section);
          controls.activate(session);
        }
      });
      // Move only out-of-order sections so updates preserve focused controls.
      var ordered = members.map(function (member) { return groups.get(member.agent_id).panel; });
      if (groups.has("finished")) ordered.push(groups.get("finished").panel);
      ordered.forEach(function (section, index) {
        if (content.children[index] !== section) content.insertBefore(section, content.children[index] || null);
      });
      status.textContent = members.length ? "" : "No running agents have reported their status yet.";
      status.hidden = Boolean(members.length);
      status.classList.remove("is-error");
    }
    function receive(members) { sync(members); }
    refresh.addEventListener("click", function () {
      shownIds = "";
      sync(options.teamActivity.current());
      groups.forEach(function (group, id) {
        if (id !== "finished" || group.panel.open) group.controls.activate(session);
      });
    });
    return {
      activate: function (key) {
        if (session !== key) this.clear();
        session = key; active = true;
        groups.forEach(function (group, id) {
          if (id !== "finished" || group.panel.open) group.controls.activate(key);
        });
        if (!unsubscribe) unsubscribe = options.teamActivity.subscribe(receive);
        sync(options.teamActivity.current());
      },
      deactivate: function () {
        active = false;
        if (unsubscribe) unsubscribe();
        unsubscribe = null;
        groups.forEach(function (group) { group.controls.deactivate(); });
      },
      clear: function () {
        this.deactivate(); session = ""; shownIds = "";
        groups.forEach(function (group) { group.controls.clear(); });
        groups.clear(); content.replaceChildren(); status.textContent = ""; status.hidden = true;
      },
    };
  }
  window.UATaskControls = { create: create, createTeam: createTeam };
})();
