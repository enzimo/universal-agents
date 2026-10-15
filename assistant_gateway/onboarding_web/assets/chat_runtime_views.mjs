/** Existing chat runtime_views view behavior with explicit state and callback dependencies. */
export function createRuntimeViews({ runtime_viewsState, sharedState, operations, lifecycle, requestClient }) {
function objectiveValue(value, fallback) {
    if (value === 0 || value === false) {
      return String(value);
    }
    var normalized = String(value || "").trim();
    return normalized || String(fallback || "");
  }

function objectiveMetric(value, label) {
    var metric = operations.uiElement("div", "objective-metric", objectiveValue(value, "-"));
    metric.appendChild(operations.uiElement("span", "", label));
    return metric;
  }

function objectiveList(title, items, itemRenderer) {
    var wrapper = operations.uiElement("div", "objective-card-section");
    wrapper.appendChild(operations.uiElement("div", "objective-card-section-title", title));
    var list = operations.uiElement("ul", "objective-card-list");
    items.forEach(function (item) {
      list.appendChild(itemRenderer(item));
    });
    wrapper.appendChild(list);
    return wrapper;
  }

function runtimeActionButton(label, action, callback, isDangerous) {
    var button = operations.uiElement(
      "button",
      "runtime-control-button" + (isDangerous ? " is-danger" : ""),
      label
    );
    button.type = "button";
    button.dataset.action = action;
    lifecycle.listen(button, "click", function () {
      if (isDangerous && button.dataset.confirmed !== "true") {
        button.dataset.confirmed = "true";
        button.textContent = "Confirm " + label.toLowerCase();
        lifecycle.timeout(function () {
          if (button.dataset.confirmed === "true") {
            button.dataset.confirmed = "";
            button.textContent = label;
          }
        }, 5000);
        return;
      }
      button.dataset.confirmed = "";
      button.textContent = label;
      var pendingLabels = {
        pause: "Pausing…",
        resume: "Resuming…",
        stop: "Stopping…",
        abandon: "Abandoning…",
        delete: "Deleting…",
        disable: "Disabling…",
        remove: "Removing…",
        attach: "Adding access…",
        detach: "Removing access…",
        install: "Installing…",
        start: "Starting…",
        publish: "Publishing…",
      };
      var finishActionFeedback = window.UAActionFeedback.begin(button, {
        busyLabel: pendingLabels[action] || "Applying…",
        group: button.parentElement,
      });
      Promise.resolve().then(callback).finally(finishActionFeedback);
    });
    return button;
  }

function runtimeControlFetch(url, payload, fallbackMessage) {
    return requestClient.fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-UA-Permission-CSRF": sharedState.runtimeControlsCsrfToken,
      },
      body: JSON.stringify(payload),
    }).then(function (response) {
      return operations.parseApiResponse(response, fallbackMessage);
    });
  }

function controlObjective(objective, action) {
    setObjectivesStatus("Applying Objective action...", false);
    return runtimeControlFetch(
      "/api/web-chat/objectives/" + encodeURIComponent(objective.objective_id) + "/control",
      {
        session_key: sharedState.runtimeControlsSessionKey,
        action: action,
        expected_record_digest: objective.record_digest,
      },
      "The Objective action could not be applied. Refresh and try again."
    ).then(function (result) {
      setObjectivesStatus(result.message || "Objective action applied.", !result.success);
      return loadObjectives();
    }).catch(function (error) {
      setObjectivesStatus(error.message || "The Objective action failed.", true);
    });
  }

function objectiveCard(objective) {
    var card = operations.uiElement("article", "objective-card");
    var header = operations.uiElement("div", "objective-card-header");
    header.appendChild(operations.uiElement(
      "h2",
      "objective-card-title",
      objectiveValue(objective.objective_title || objective.title, objective.objective_id || "Objective")
    ));
    header.appendChild(operations.uiElement(
      "span",
      "objective-status-badge",
      objectiveValue(objective.status, "Unknown")
    ));
    card.appendChild(header);
    card.appendChild(operations.uiElement(
      "div",
      "objective-card-id",
      objectiveValue(objective.objective_id, "No objective ID")
    ));
    card.appendChild(operations.uiElement(
      "p",
      "objective-card-outcome",
      objectiveValue(
        objective.desired_outcome || objective.latest_summary,
        "No desired outcome has been published yet."
      )
    ));

    var progress = operations.uiElement("p", "objective-card-progress");
    progress.appendChild(operations.uiElement("strong", "", "Progress: "));
    progress.appendChild(document.createTextNode(objectiveValue(
      objective.progress_summary,
      "No evidence update has been recorded."
    )));
    card.appendChild(progress);

    var focus = operations.uiElement("div", "objective-card-focus");
    focus.appendChild(operations.uiElement("strong", "", "Current focus: "));
    focus.appendChild(document.createTextNode(objectiveValue(objective.current_focus, "Not set")));
    card.appendChild(focus);

    var successCriteria = Array.isArray(objective.success_criteria)
      ? objective.success_criteria
      : [];
    if (successCriteria.length) {
      card.appendChild(objectiveList("Success criteria", successCriteria, function (criterion) {
        var item = operations.uiElement("li", "");
        if (criterion && typeof criterion === "object") {
          item.appendChild(document.createTextNode(objectiveValue(
            criterion.description || criterion.title,
            "Criterion"
          )));
          if (criterion.status) {
            item.appendChild(operations.uiElement(
              "span",
              "objective-criterion-status",
              " · " + objectiveValue(criterion.status, "open")
            ));
          }
          return item;
        }
        item.textContent = objectiveValue(criterion, "Criterion");
        return item;
      }));
    }

    var subgoals = Array.isArray(objective.subgoals) ? objective.subgoals : [];
    if (subgoals.length) {
      card.appendChild(objectiveList("Plan subgoals", subgoals, function (subgoal) {
        var status = objectiveValue(subgoal.status, "planned");
        var statusClass = status.toLowerCase().replace(/_/g, "-").replace(/[^a-z-]/g, "");
        var item = operations.uiElement("li", "objective-subgoal is-" + statusClass);
        item.appendChild(operations.uiElement(
          "strong",
          "",
          objectiveValue(subgoal.title || subgoal.description, "Subgoal")
        ));
        item.appendChild(operations.uiElement(
          "span",
          "objective-subgoal-status",
          status + (subgoal.result_summary ? " · " + subgoal.result_summary : "")
        ));
        return item;
      }));
    }

    var policyStatus = objectiveValue(objective.work_policy_status, "");
    var meta = operations.uiElement("div", "objective-card-meta");
    meta.appendChild(operations.uiElement(
      "span",
      "objective-policy-badge",
      policyStatus
        ? "Time-boxed · " + policyStatus
        : "Continuous execution"
    ));
    var updated = operations.timestampElement(
      objective.updated_at || objective.last_updated_at,
      "objective-card-updated"
    );
    if (updated) {
      updated.textContent = "Updated " + updated.textContent;
      meta.appendChild(updated);
    }
    card.appendChild(meta);

    var actions = Array.isArray(objective.available_actions)
      ? objective.available_actions
      : [];
    if (actions.length) {
      var controls = operations.uiElement("div", "runtime-control-actions");
      actions.forEach(function (action) {
        controls.appendChild(runtimeActionButton(
          action.charAt(0).toUpperCase() + action.slice(1),
          action,
          function () { return controlObjective(objective, action); },
          action === "delete" || action === "abandon"
        ));
      });
      card.appendChild(controls);
    }
    return card;
  }

function setObjectivesStatus(message, isError) {
    runtime_viewsState.objectivesStatus.textContent = String(message || "");
    runtime_viewsState.objectivesStatus.hidden = !message;
    runtime_viewsState.objectivesStatus.classList.toggle("is-error", Boolean(isError));
  }

function renderObjectives(objectives) {
    var signature = JSON.stringify(objectives);
    if (signature === sharedState.objectivesSnapshotSignature && sharedState.objectivesContent.childNodes.length) {
      return;
    }
    sharedState.objectivesSnapshotSignature = signature;
    var fragment = document.createDocumentFragment();
    if (!objectives.length) {
      fragment.appendChild(operations.uiElement(
        "p",
        "permission-empty-state objectives-empty-state",
        "No Objectives are registered for this user."
      ));
    } else {
      objectives.forEach(function (objective) {
        fragment.appendChild(objectiveCard(objective));
      });
    }
    var previousScrollTop = sharedState.objectivesContent.scrollTop;
    sharedState.objectivesContent.replaceChildren(fragment);
    sharedState.objectivesContent.scrollTop = previousScrollTop;
  }

function controlSchedule(schedule, action, pausedUntil) {
    setObjectivesStatus("Applying schedule action...", false);
    return runtimeControlFetch(
      "/api/web-chat/schedules/" + encodeURIComponent(schedule.schedule_id) + "/control",
      {
        session_key: sharedState.runtimeControlsSessionKey,
        action: action,
        expected_record_digest: schedule.record_digest,
        paused_until: pausedUntil || null,
      },
      "The schedule action could not be applied. Refresh and try again."
    ).then(function (result) {
      setObjectivesStatus(
        result.success ? "Schedule action applied." : "Schedule action was rejected.",
        !result.success
      );
      return loadObjectives();
    }).catch(function (error) {
      setObjectivesStatus(error.message || "The schedule action failed.", true);
    });
  }

function scheduleCard(schedule) {
    var card = operations.uiElement("article", "objective-card");
    var header = operations.uiElement("div", "objective-card-header");
    header.appendChild(operations.uiElement(
      "h2",
      "objective-card-title",
      objectiveValue(schedule.schedule_name, schedule.schedule_id)
    ));
    var assignment = schedule.worker_assignment;
    // A stuck schedule says so instead of "Enabled" (owner's request 2026-10-04).
    var lifecycle = schedule.enabled
      ? (schedule.paused_until ? "Paused" : (assignment.not_running_since ? "Not running" : "Enabled"))
      : "Disabled";
    header.appendChild(operations.uiElement(
      "span",
      "objective-status-badge schedule-status is-" + lifecycle.toLowerCase().replace(/ /g, "-"),
      lifecycle
    ));
    card.appendChild(header);
    card.appendChild(operations.uiElement("div", "objective-card-id", schedule.schedule_id));
    if (assignment.not_running_since) {
      // `warning-callout` is a two-column grid: the icon, then one text
      // block. Both lines go in that block; as separate grid items the first
      // took the icon column and squeezed the second to one letter per line.
      var problem = operations.uiElement("div", "warning-callout schedule-not-running");
      var since = operations.timestampElement(assignment.not_running_since, "");
      var problemText = operations.uiElement("div", "schedule-not-running-text");
      problemText.appendChild(operations.uiElement(
        "p", "", "Not running since " + (since ? since.textContent : "its last due time") + "."
      ));
      problemText.appendChild(operations.uiElement("p", "", assignment.not_running_explanation || ""));
      problem.appendChild(operations.uiElement("span", "warning-icon", "!"));
      problem.appendChild(problemText);
      card.appendChild(problem);
    }
    var details = operations.uiElement("div", "runtime-resource-details");
    var systemReview = Boolean(assignment.review_agent_type);
    if (systemReview) {
      details.appendChild(operations.uiElement(
        "span", "", "Review agent: " + objectiveValue(assignment.review_agent_type, "Not specified")
      ));
    }
    details.appendChild(operations.uiElement(
      "span", "", systemReview
        ? (assignment.upgrade_required ? "Upgrade required: Move this timer to the local scheduler" : "Managed by: Local scheduler")
        : "Assigned agent type: " + objectiveValue(assignment.agent_type, "Not specified")
    ));
    if (assignment.agent_id) {
      details.appendChild(operations.uiElement("span", "", "Specific agent: " + assignment.agent_id));
    }
    var automaticCreationLabels = {
      not_configured: "Not configured",
      enabled: "Enabled by TeamLead",
      disabled: "Disabled by TeamLead",
      default_policy: "Allowed by default policy",
    };
    details.appendChild(operations.uiElement(
      "span", "", (systemReview ? "Agent startup: " : "Automatic creation: ") + automaticCreationLabels[assignment.automatic_creation]
    ));
    if (assignment.reassignment_pending) {
      details.appendChild(operations.uiElement(
        "span", "", "Pending agent type: " + assignment.desired_agent_type + " (after the current claim ends)"
      ));
    }
    if (!assignment.upgrade_required && ["enabled", "default_policy"].includes(assignment.automatic_creation)) {
      details.appendChild(operations.uiElement(
        "span", "", "Starts when needed, subject to current access and launch limits."
      ));
    }
    details.appendChild(operations.uiElement(
      "span",
      "",
      "Class: " + objectiveValue(schedule.management_class, "unclassified")
    ));
    if (schedule.objective_id) {
      details.appendChild(operations.uiElement("span", "", "Objective: " + schedule.objective_id));
    }
    var nextFire = operations.timestampElement(schedule.next_fire_at, "");
    if (nextFire) {
      nextFire.textContent = "Next fire: " + nextFire.textContent;
      details.appendChild(nextFire);
    }
    var pausedUntil = operations.timestampElement(schedule.paused_until, "");
    if (pausedUntil) {
      pausedUntil.textContent = "Paused until: " + pausedUntil.textContent;
      details.appendChild(pausedUntil);
    }
    card.appendChild(details);
    var actions = Array.isArray(schedule.available_actions)
      ? schedule.available_actions
      : [];
    if (actions.length) {
      var controls = operations.uiElement(
        "div",
        "runtime-control-actions runtime-schedule-controls"
      );
      var actionButtons = operations.uiElement("div", "runtime-schedule-actions");
      var pauseInput = null;
      if (actions.indexOf("pause") >= 0) {
        pauseInput = document.createElement("input");
        pauseInput.type = "datetime-local";
        pauseInput.className = "runtime-pause-input";
        pauseInput.setAttribute("aria-label", "Pause schedule until");
        pauseInput.value = new Date(Date.now() + 24 * 60 * 60 * 1000)
          .toISOString()
          .slice(0, 16);
        controls.appendChild(pauseInput);
      }
      actions.forEach(function (action) {
        var label = action === "remove"
          ? "Delete"
          : action.charAt(0).toUpperCase() + action.slice(1);
        var button = runtimeActionButton(
          label,
          action,
          function () {
            var until = null;
            if (action === "pause") {
              var parsed = new Date(pauseInput && pauseInput.value || "");
              if (Number.isNaN(parsed.getTime())) {
                setObjectivesStatus("Choose a valid pause-until time.", true);
                return Promise.resolve();
              }
              until = parsed.toISOString();
            }
            return controlSchedule(schedule, action, until);
          },
          action === "remove"
        );
        actionButtons.appendChild(button);
      });
      controls.appendChild(actionButtons);
      card.appendChild(controls);
    }
    return card;
  }

function renderSchedules(schedules) {
    schedules = schedules.slice().sort(function (left, right) {
      var leftTime = Date.parse(left.next_fire_at);
      var rightTime = Date.parse(right.next_fire_at);
      leftTime = Number.isFinite(leftTime) ? leftTime : Infinity;
      rightTime = Number.isFinite(rightTime) ? rightTime : Infinity;
      return leftTime - rightTime || String(left.schedule_id || "").localeCompare(String(right.schedule_id || ""));
    });
    var signature = JSON.stringify(schedules);
    if (signature === sharedState.objectivesSnapshotSignature && sharedState.objectivesContent.childNodes.length) {
      return;
    }
    sharedState.objectivesSnapshotSignature = signature;
    var fragment = document.createDocumentFragment();
    if (!schedules.length) {
      fragment.appendChild(operations.uiElement(
        "p",
        "permission-empty-state objectives-empty-state",
        "No schedules are registered for this user."
      ));
    } else {
      schedules.forEach(function (schedule) {
        fragment.appendChild(scheduleCard(schedule));
      });
    }
    sharedState.objectivesContent.replaceChildren(fragment);
  }

function controlMcp(resourceKind, resource, action, targetAgentType) {
    var resourceId = resourceKind === "connection"
      ? resource.connection_id
      : resource.hosted_server_id;
    var digest = resourceKind === "connection"
      ? resource.record_digest
      : resource.definition_digest;
    setObjectivesStatus("Applying MCP lifecycle action...", false);
    return runtimeControlFetch(
      "/api/web-chat/mcp/" + encodeURIComponent(resourceKind) + "/"
        + encodeURIComponent(resourceId) + "/control",
      {
        session_key: sharedState.runtimeControlsSessionKey,
        action: action,
        expected_digest: digest,
        expected_lifecycle: resource.lifecycle,
        target_agent_type: targetAgentType || null,
      },
      "The MCP lifecycle action could not be applied. Refresh and try again."
    ).then(function (result) {
      setObjectivesStatus(
        "MCP resource is now " + objectiveValue(result.status, "updated") + ".",
        false
      );
      return loadObjectives();
    }).catch(function (error) {
      setObjectivesStatus(error.message || "The MCP lifecycle action failed.", true);
    });
  }

function mcpResourceCard(resourceKind, resource) {
    var connection = resourceKind === "connection";
    var resourceId = connection ? resource.connection_id : resource.hosted_server_id;
    var title = connection ? resource.connection_name : resource.server_name;
    var card = operations.uiElement("article", "objective-card");
    var header = operations.uiElement("div", "objective-card-header");
    header.appendChild(operations.uiElement("h2", "objective-card-title", title));
    header.appendChild(operations.uiElement(
      "span",
      "objective-status-badge",
      objectiveValue(resource.lifecycle, "Unknown")
    ));
    card.appendChild(header);
    card.appendChild(operations.uiElement("div", "objective-card-id", resourceId));
    var details = operations.uiElement("div", "runtime-resource-details");
    if (connection) {
      details.appendChild(operations.uiElement("span", "", "Transport: " + resource.transport));
      details.appendChild(operations.uiElement(
        "span",
        "",
        // Grants on the server's tool decide who may use it (Access page).
        "Proposed for: " + objectiveValue(
          (resource.target_agent_types || []).join(", "),
          "no agent types"
        )
      ));
    } else {
      details.appendChild(operations.uiElement(
        "span",
        "",
        "Tools: " + objectiveValue((resource.tools || []).join(", "), "none")
      ));
      details.appendChild(operations.uiElement(
        "span",
        "",
        "Listener: " + resource.bind_host + ":" + resource.port
      ));
    }
    card.appendChild(details);
    var actions = Array.isArray(resource.available_actions)
      ? resource.available_actions
      : [];
    var controls = operations.uiElement("div", "runtime-control-actions");
    actions.forEach(function (action) {
      if (action === "detach") {
        (resource.detachable_agent_types || []).forEach(function (agentType) {
          controls.appendChild(runtimeActionButton(
            "Detach " + agentType,
            action,
            function () { return controlMcp(resourceKind, resource, action, agentType); },
            true
          ));
        });
        return;
      }
      controls.appendChild(runtimeActionButton(
        action.charAt(0).toUpperCase() + action.slice(1),
        action,
        function () { return controlMcp(resourceKind, resource, action, null); },
        action === "remove"
      ));
    });
    if (controls.childNodes.length) {
      card.appendChild(controls);
    }
    return card;
  }

function renderMcp(payload) {
    var connections = Array.isArray(payload.mcp_connections)
      ? payload.mcp_connections
      : [];
    var hosted = Array.isArray(payload.hosted_mcp_servers)
      ? payload.hosted_mcp_servers
      : [];
    var signature = JSON.stringify([connections, hosted]);
    if (signature === sharedState.objectivesSnapshotSignature && sharedState.objectivesContent.childNodes.length) {
      return;
    }
    sharedState.objectivesSnapshotSignature = signature;
    var fragment = document.createDocumentFragment();
    connections.forEach(function (connection) {
      fragment.appendChild(mcpResourceCard("connection", connection));
    });
    hosted.forEach(function (server) {
      fragment.appendChild(mcpResourceCard("hosted_server", server));
    });
    if (!connections.length && !hosted.length) {
      fragment.appendChild(operations.uiElement(
        "p",
        "permission-empty-state objectives-empty-state",
        "No MCP connections or hosted servers are registered."
      ));
    }
    sharedState.objectivesContent.replaceChildren(fragment);
  }

function loadObjectives() {
    if (sharedState.objectivesRequestInFlight || !sharedState.authSessionActive) {
      return Promise.resolve();
    }
    if (!sharedState.runtimeControlsSessionKey) {
      return operations.ensureRuntimeControlsSession().then(loadObjectives);
    }
    var requestedView = sharedState.runtimeControlsView;
    var requestedSession = sharedState.runtimeControlsSessionKey;
    var requestToken = {};
    sharedState.runtimeControlsRequest = requestToken;
    sharedState.objectivesRequestInFlight = true;
    var finishRefresh = window.UAActionFeedback.beginRefresh(runtime_viewsState.objectivesRefresh);
    var endpoint = requestedView === "schedules"
      ? "/api/web-chat/schedules"
      : requestedView === "mcp"
        ? "/api/web-chat/team"
        : "/api/web-chat/objectives";
    return requestClient.fetch(
      endpoint + "?session_key=" + encodeURIComponent(requestedSession),
      {
      cache: "no-store",
      credentials: "same-origin",
      }
    ).then(function (response) {
      return operations.parseApiResponse(response, "Unable to load Runtime Controls.");
    }).then(function (payload) {
      // A tab switch or sign-out cannot reinterpret another view's response.
      if (!currentRequest() || requestedView !== sharedState.runtimeControlsView) {
        return;
      }
      if (requestedView === "schedules") {
        renderSchedules(Array.isArray(payload.schedules) ? payload.schedules : []);
      } else if (requestedView === "mcp") {
        renderMcp(payload);
      } else {
        renderObjectives(Array.isArray(payload.objectives) ? payload.objectives : []);
      }
      setObjectivesStatus("", false);
      sharedState.objectivesUpdatedAt.textContent = "Updated " + operations.formatShortLocalTimestamp(new Date().toISOString());
    }).catch(function (error) {
      if (currentRequest() && requestedView === sharedState.runtimeControlsView) {
        setObjectivesStatus(error.message || "Unable to load Runtime Controls.", true);
      }
    }).finally(function () {
      finishRefresh();
      if (!currentRequest()) {
        return;
      }
      sharedState.objectivesRequestInFlight = false;
      sharedState.runtimeControlsRequest = null;
      if (requestedView !== sharedState.runtimeControlsView) {
        loadObjectives();
      }
    });

    function currentRequest() {
      return sharedState.authSessionActive
        && sharedState.runtimeControlsSessionKey === requestedSession
        && sharedState.runtimeControlsRequest === requestToken;
    }
  }

function selectRuntimeControlsView(viewName) {
    if (viewName !== sharedState.runtimeControlsView) {
      // Keep DOM nodes, focusable controls and scroll position for visited tabs.
      // Authentication clears these snapshots together with the visible content.
      var snapshots = sharedState.runtimeControlSnapshots;
      if (sharedState.objectivesSnapshotSignature) {
        snapshots.set(sharedState.runtimeControlsView, {
          nodes: Array.from(sharedState.objectivesContent.childNodes),
          signature: sharedState.objectivesSnapshotSignature,
          scrollTop: sharedState.objectivesContent.scrollTop,
          updatedAt: sharedState.objectivesUpdatedAt.textContent,
        });
      }
      var snapshot = snapshots.get(viewName);
      sharedState.objectivesContent.replaceChildren(...(snapshot ? snapshot.nodes : []));
      sharedState.objectivesContent.scrollTop = snapshot ? snapshot.scrollTop : 0;
      sharedState.objectivesSnapshotSignature = snapshot ? snapshot.signature : "";
      sharedState.objectivesUpdatedAt.textContent = snapshot ? snapshot.updatedAt : "";
      setObjectivesStatus("", false);
    }
    sharedState.runtimeControlsView = viewName;
    [
      [runtime_viewsState.runtimeObjectivesTab, "objectives"],
      [runtime_viewsState.runtimeSchedulesTab, "schedules"],
      [runtime_viewsState.runtimeMcpTab, "mcp"],
    ].forEach(function (entry) {
      var selected = entry[1] === viewName;
      entry[0].classList.toggle("is-active", selected);
      entry[0].setAttribute("aria-selected", selected ? "true" : "false");
    });
    loadObjectives();
  }
return { controlMcp, controlObjective, controlSchedule, loadObjectives, mcpResourceCard, objectiveCard, objectiveList, objectiveMetric, objectiveValue, renderMcp, renderObjectives, renderSchedules, runtimeActionButton, runtimeControlFetch, scheduleCard, selectRuntimeControlsView, setObjectivesStatus };
}
