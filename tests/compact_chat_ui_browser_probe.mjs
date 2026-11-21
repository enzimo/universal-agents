// Real-browser layout, geometry, accessibility, and WCAG contrast probe for
// compact Web Chat prompts (pending/decided access review cards, transient
// credential link notices, inline workflow cards) and Settings Access Inbox
// cards in both light and dark color modes.
export async function verifyCompactChatUi() {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const parseRgba = (raw) => {
    const match = String(raw || "").match(/rgba?\(\s*([\d.]+)\s*[,\s]\s*([\d.]+)\s*[,\s]\s*([\d.]+)(?:\s*[,/]\s*([\d.]+))?\s*\)/i);
    if (!match) return null;
    return {
      r: Number(match[1]),
      g: Number(match[2]),
      b: Number(match[3]),
      a: match[4] === undefined ? 1 : Number(match[4]),
    };
  };
  const blend = (fg, bg) => {
    const a = fg.a;
    return {
      r: fg.r * a + bg.r * (1 - a),
      g: fg.g * a + bg.g * (1 - a),
      b: fg.b * a + bg.b * (1 - a),
      a: 1,
    };
  };
  const parseGradientAverage = (image) => {
    if (!image || image === "none") return null;
    const matches = [...String(image).matchAll(/rgba?\([^)]+\)/gi)].map(m => parseRgba(m[0])).filter(Boolean);
    if (!matches.length) return null;
    const sum = matches.reduce((acc, c) => ({ r: acc.r + c.r, g: acc.g + c.g, b: acc.b + c.b, a: acc.a + c.a }),
      { r: 0, g: 0, b: 0, a: 0 });
    return { r: sum.r / matches.length, g: sum.g / matches.length, b: sum.b / matches.length, a: sum.a / matches.length };
  };
  const effectiveBackground = (element, canvas) => {
    const chain = [];
    for (let node = element; node && node.nodeType === 1; node = node.parentElement) chain.unshift(node);
    let color = { ...canvas, a: 1 };
    for (const node of chain) {
      const style = getComputedStyle(node);
      const bg = parseRgba(style.backgroundColor);
      if (bg && bg.a > 0) color = blend(bg, color);
      const grad = parseGradientAverage(style.backgroundImage);
      if (grad && grad.a > 0) color = blend(grad, color);
    }
    return color;
  };
  const luminance = ({ r, g, b }) => {
    const channel = (v) => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  };
  const contrastRatio = (c1, c2) => {
    const l1 = luminance(c1);
    const l2 = luminance(c2);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  };
  const checkTextContrast = (el, canvas, label, minRatio = 4.5) => {
    const style = getComputedStyle(el);
    const bg = effectiveBackground(el, canvas);
    const fgRaw = parseRgba(style.color);
    check(fgRaw, `${label}: missing text color`);
    const fg = blend(fgRaw, bg);
    const ratio = contrastRatio(fg, bg);
    check(ratio >= minRatio, `${label}: text contrast ${ratio.toFixed(2)} < ${minRatio} (fg=${style.color}, bg=rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)}))`);
  };
  const checkBorderContrast = (el, canvas, label, minRatio = 3.0) => {
    const style = getComputedStyle(el);
    const parentBg = effectiveBackground(el.parentElement, canvas);
    const borderRaw = parseRgba(style.borderTopColor);
    check(borderRaw, `${label}: missing border color`);
    const border = blend(borderRaw, parentBg);
    const ratio = contrastRatio(border, parentBg);
    check(ratio >= minRatio, `${label}: border contrast ${ratio.toFixed(2)} < ${minRatio} (border=${style.borderTopColor}, parentBg=rgb(${Math.round(parentBg.r)},${Math.round(parentBg.g)},${Math.round(parentBg.b)}))`);
  };

  const { renderReviewCard, renderDecidedCard } = await import("/access_inbox.mjs");
  const { mountWorkflowCard } = await import("/workflow_card.mjs");

  const sampleReview = (canDecide) => ({
    request_id: canDecide ? "req-decidable" : "req-waiting",
    status: "waiting",
    can_decide: canDecide,
    title: "Grant TeamLead write access to GitHub for your tasks",
    summary: "Allow TeamLead to write to GitHub",
    agent_type: "TeamLead",
    level_words: "Read and write",
    for_whose_work: "Your work",
    lifetime_words: "This task",
    created_at_utc: "2026-11-21T12:00:00Z",
    reason: "Needed to open a draft pull request for your repository.",
    keep_help: "Applies to your later tasks without asking again.",
    rows: [
      {
        item_id: "item-1",
        section: "access",
        text: "GitHub (write)",
        flags: ["new_host", "can_hand_on"],
        keepable: true,
        can_hand_on: true,
      },
    ],
    choices: canDecide ? [
      { choice: "approve", label: "Approve" },
      { choice: "approve_read", label: "Approve reading only" },
      { choice: "deny", label: "Deny" },
    ] : [],
    choice_effects: {
      approve: {
        kept: "Grants write access and keeps it for later tasks.",
        task: "Grants write access for this task only.",
      },
      approve_read: {
        kept: "Grants read-only access and keeps it.",
        task: "Grants read-only access for this task only.",
      },
      deny: { any: "Refuses this request." },
    },
  });

  const sampleWorkflow = {
    workflow_id: "access_flow_probe1",
    title: "Portainer access plan",
    target_agent_type: "TeamLead",
    goal: "Connect Portainer and read container health",
    steps: [
      {
        step_id: "step-1",
        title: "Add Portainer connection",
        description: "Register the Portainer API tool.",
        state: "waiting",
        depends_on: [],
        requests: [
          { request_id: "req-1", title: "Approve Portainer API", status: "pending" },
        ],
      },
    ],
  };

  const results = [];
  const stage = document.createElement("div");
  stage.id = "compact-chat-ui-probe-stage";
  Object.assign(stage.style, {
    position: "fixed",
    inset: "0",
    overflow: "auto",
    zIndex: "99999",
    padding: "16px",
    display: "grid",
    gap: "16px",
    alignContent: "start",
    width: "900px",
    maxWidth: "100vw",
  });
  document.body.append(stage);

  try {
    for (const mode of ["light", "dark"]) {
      document.documentElement.setAttribute("data-color-mode", mode);
      document.documentElement.setAttribute("data-ui-motion", "off");
      document.documentElement.removeAttribute("data-ua-background-hold");
      stage.replaceChildren();
      const canvas = mode === "dark" ? { r: 7, g: 11, b: 14, a: 1 } : { r: 248, g: 250, b: 247, a: 1 };
      stage.style.background = mode === "dark" ? "#070b0e" : "#f8faf7";

      const chatMain = document.createElement("section");
      chatMain.className = "chat-main";
      Object.assign(chatMain.style, { width: "760px" });
      const messageList = document.createElement("div");
      messageList.className = "message-list";
      Object.assign(messageList.style, { overflow: "visible" });
      chatMain.append(messageList);

      // 1. Compact pending decidable Web Chat access review card
      const pendingBubble = document.createElement("div");
      pendingBubble.className = "message-bubble is-system";
      const pendingSlot = document.createElement("div");
      pendingSlot.className = "chat-access-review";
      const pendingCard = renderReviewCard(document, sampleReview(true), { compact: true });
      pendingSlot.append(pendingCard);
      pendingBubble.append(pendingSlot);
      messageList.append(pendingBubble);

      // 2. Compact pending waiting-for-administrator Web Chat access review card
      const waitingBubble = document.createElement("div");
      waitingBubble.className = "message-bubble is-system";
      const waitingSlot = document.createElement("div");
      waitingSlot.className = "chat-access-review";
      const waitingCard = renderReviewCard(document, sampleReview(false), { compact: true });
      waitingSlot.append(waitingCard);
      waitingBubble.append(waitingSlot);
      messageList.append(waitingBubble);

      // 3. Compact decided Web Chat access review card with follow-up link & outcome
      const decidedBubble = document.createElement("div");
      decidedBubble.className = "message-bubble is-system";
      const decidedSlot = document.createElement("div");
      decidedSlot.className = "chat-access-review";
      const decidedCard = renderDecidedCard(document, {
        id: "req-decided",
        title: "Connect Portainer account",
        outcome: { label: "Approved", tone: "granted", explanation: "Approved by Alice. Open the form below to finish." },
        links: [{ label: "Connect Portainer", url: "https://broker.example/form/1" }],
        outcomes: ["GitHub credentials saved."],
        compact: true,
      });
      decidedSlot.append(decidedCard);
      decidedBubble.append(decidedSlot);
      messageList.append(decidedBubble);

      // 4a. Active standalone transient link notice
      const transientActive = document.createElement("div");
      transientActive.className = "message-transient-notice is-link-card";
      const tHeader = document.createElement("div");
      tHeader.className = "message-transient-header";
      const tBadge = document.createElement("span");
      tBadge.className = "message-transient-badge";
      tBadge.textContent = "Action needed";
      const tCountdown = document.createElement("span");
      tCountdown.className = "message-transient-countdown";
      tCountdown.textContent = "14:59 remaining";
      tHeader.append(tBadge, tCountdown);
      const tPre = document.createElement("pre");
      tPre.textContent = "Open the secure form to finish connecting Portainer.";
      const tActions = document.createElement("div");
      tActions.className = "message-transient-actions";
      const tOpen = document.createElement("a");
      tOpen.className = "button primary small message-transient-open";
      tOpen.href = "https://broker.example/form/2";
      tOpen.textContent = "Open secure form";
      const tDismiss = document.createElement("button");
      tDismiss.type = "button";
      tDismiss.className = "button ghost small message-transient-dismiss";
      tDismiss.textContent = "Dismiss";
      tActions.append(tOpen, tDismiss);
      transientActive.append(tHeader, tPre, tActions);
      messageList.append(transientActive);

      // 4b. Expired standalone transient link notice
      const transientExpired = document.createElement("div");
      transientExpired.className = "message-transient-notice is-link-card is-expired";
      const eHeader = document.createElement("div");
      eHeader.className = "message-transient-header";
      const eBadge = document.createElement("span");
      eBadge.className = "message-transient-badge is-expired";
      eBadge.textContent = "Expired";
      eHeader.append(eBadge);
      const ePre = document.createElement("pre");
      ePre.textContent = "Open the secure form to finish connecting Portainer.";
      const eActions = document.createElement("div");
      eActions.className = "message-transient-actions";
      const eStatus = document.createElement("span");
      eStatus.className = "message-transient-expired";
      eStatus.textContent = "This secure form link has expired.";
      const eDismiss = document.createElement("button");
      eDismiss.type = "button";
      eDismiss.className = "button ghost small message-transient-dismiss";
      eDismiss.textContent = "Dismiss";
      eActions.append(eStatus, eDismiss);
      transientExpired.append(eHeader, ePre, eActions);
      messageList.append(transientExpired);

      // 5. Inline workflow card inside assistant bubble
      const workflowBubble = document.createElement("div");
      workflowBubble.className = "message-bubble is-assistant";
      const wfLink = document.createElement("a");
      wfLink.href = `${location.origin}/workflows/access_flow_probe1`;
      wfLink.textContent = "View plan";
      workflowBubble.append(wfLink);
      messageList.append(workflowBubble);

      // 6. Settings Access Inbox full request card
      const inboxWrap = document.createElement("section");
      inboxWrap.className = "access-inbox-list";
      Object.assign(inboxWrap.style, { width: "760px" });
      const inboxCard = renderReviewCard(document, sampleReview(true), { compact: false });
      inboxWrap.append(inboxCard);

      stage.append(chatMain, inboxWrap);

      mountWorkflowCard(workflowBubble, {
        lifecycle: { listen: (n, e, h, o) => n.addEventListener(e, h, o) },
        requestClient: { fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({ workflow: sampleWorkflow }) }) },
        isCurrent: () => true,
        loaded: new Map([[sampleWorkflow.workflow_id, sampleWorkflow]]),
      });

      // Let layout settle
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));

      // --- Checks for 1: Compact pending decidable card ---
      const pendingStyle = getComputedStyle(pendingCard);
      check(pendingStyle.backgroundColor === "rgba(0, 0, 0, 0)" && pendingStyle.backgroundImage === "none",
        `${mode} compact pending card has nested background: ${pendingStyle.backgroundColor} / ${pendingStyle.backgroundImage}`);
      for (const side of ["borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth"]) {
        check(pendingStyle[side] === "0px", `${mode} compact pending card has ${side}=${pendingStyle[side]}`);
      }
      const statusRow = pendingCard.querySelector(".access-review-status-row");
      const statusBadge = statusRow.querySelector(".access-inbox-status.is-needs-decision");
      const firstFlag = statusRow.querySelector(".access-review-flag");
      check(Math.abs(statusBadge.getBoundingClientRect().top - firstFlag.getBoundingClientRect().top) < 8,
        `${mode} compact status pill and warning chip do not share one row`);

      const summary = pendingCard.querySelector(".access-review-more-toggle");
      const summaryRectBefore = summary.getBoundingClientRect();
      check(summaryRectBefore.height >= 30 && summaryRectBefore.height <= 42,
        `${mode} Details toggle height ${summaryRectBefore.height.toFixed(1)}px not in [30, 42]`);

      const choiceButtons = [...pendingCard.querySelectorAll(".permission-choice-button")];
      check(choiceButtons.length === 3, `${mode} expected 3 choice buttons`);
      for (const btn of choiceButtons) {
        const h = btn.getBoundingClientRect().height;
        check(h >= 30 && h <= 38, `${mode} choice button '${btn.textContent}' height ${h.toFixed(1)}px not in [30, 38]`);
        check(getComputedStyle(btn).boxShadow === "none",
          `${mode} choice button '${btn.textContent}' kept shadow: ${getComputedStyle(btn).boxShadow}`);
        const descId = btn.getAttribute("aria-describedby");
        check(Boolean(descId), `${mode} choice button '${btn.textContent}' missing aria-describedby`);
        const descNode = document.getElementById(descId);
        check(descNode && descNode.textContent.trim().length > 0,
          `${mode} choice button '${btn.textContent}' aria-describedby target empty while Details is closed`);
      }

      // Opening Details keeps toggle in place (does not jump below buttons) and Keep/note stay before Approve.
      const detailsEl = pendingCard.querySelector(".access-review-more");
      detailsEl.open = true;
      await new Promise(resolve => requestAnimationFrame(resolve));
      const summaryRectAfter = summary.getBoundingClientRect();
      check(Math.abs(summaryRectAfter.top - summaryRectBefore.top) <= 2,
        `${mode} Details toggle jumped on open (${summaryRectBefore.top} -> ${summaryRectAfter.top})`);
      const keepBox = pendingCard.querySelector(".access-review-keep-box");
      const noteArea = pendingCard.querySelector(".access-review-note");
      check(Boolean(keepBox.compareDocumentPosition(choiceButtons[0]) & Node.DOCUMENT_POSITION_FOLLOWING),
        `${mode} Keep checkbox must precede Approve in document order`);
      check(Boolean(noteArea.compareDocumentPosition(choiceButtons[0]) & Node.DOCUMENT_POSITION_FOLLOWING),
        `${mode} Note textarea must precede Approve in document order`);
      detailsEl.open = false;

      checkTextContrast(pendingCard.querySelector(".permission-operation"), canvas, `${mode} pending heading`);
      checkTextContrast(statusBadge, canvas, `${mode} pending status badge`);
      checkTextContrast(firstFlag, canvas, `${mode} pending warning chip`);
      checkTextContrast(summary, canvas, `${mode} Details toggle text`);
      checkBorderContrast(summary, canvas, `${mode} Details toggle border`);
      for (const btn of choiceButtons) checkTextContrast(btn, canvas, `${mode} choice button ${btn.textContent}`);

      // --- Checks for 2: Compact pending waiting card ---
      const waitingBadge = waitingCard.querySelector(".access-inbox-status.is-waiting");
      check(waitingBadge && waitingBadge.textContent === "Waiting for an administrator",
        `${mode} waiting badge text mismatch`);
      checkTextContrast(waitingBadge, canvas, `${mode} waiting badge`);
      check(waitingCard.querySelector(".access-review-more-toggle").getBoundingClientRect().height >= 30,
        `${mode} waiting card Details toggle < 30px`);

      // --- Checks for 3: Compact decided card ---
      const decidedStyle = getComputedStyle(decidedCard);
      check(decidedStyle.backgroundColor === "rgba(0, 0, 0, 0)" && decidedStyle.backgroundImage === "none",
        `${mode} compact decided card has nested background: ${decidedStyle.backgroundColor}`);
      for (const side of ["borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth"]) {
        check(decidedStyle[side] === "0px", `${mode} compact decided card has ${side}=${decidedStyle[side]}`);
      }
      const followUpAnchor = decidedCard.querySelector("a.button");
      check(getComputedStyle(followUpAnchor).textDecorationLine === "none",
        `${mode} decided follow-up link button is underlined`);
      const followUpHeight = followUpAnchor.getBoundingClientRect().height;
      check(followUpHeight >= 30 && followUpHeight <= 38,
        `${mode} decided follow-up button height ${followUpHeight.toFixed(1)}px not in [30, 38]`);
      check(getComputedStyle(followUpAnchor).boxShadow === "none",
        `${mode} decided follow-up button kept shadow: ${getComputedStyle(followUpAnchor).boxShadow}`);
      checkTextContrast(decidedCard.querySelector(".permission-operation"), canvas, `${mode} decided heading`);
      checkTextContrast(decidedCard.querySelector(".access-inbox-status"), canvas, `${mode} decided badge`);
      checkTextContrast(decidedCard.querySelector(".permission-effect-summary"), canvas, `${mode} decided summary`);
      checkTextContrast(decidedCard.querySelector(".access-review-follow-up-outcome"), canvas, `${mode} decided follow-up outcome`);
      checkTextContrast(followUpAnchor, canvas, `${mode} decided follow-up anchor`);

      // --- Checks for 4: Standalone transient link notices ---
      const tBadgeRect = tBadge.getBoundingClientRect();
      const tCountRect = tCountdown.getBoundingClientRect();
      check(tCountRect.left - tBadgeRect.right >= 4,
        `${mode} transient badge and countdown touch (gap=${(tCountRect.left - tBadgeRect.right).toFixed(1)}px)`);
      check(getComputedStyle(tOpen).textDecorationLine === "none",
        `${mode} transient Open link is underlined`);
      const tOpenHeight = tOpen.getBoundingClientRect().height;
      check(tOpenHeight >= 30 && tOpenHeight <= 38,
        `${mode} transient Open link height ${tOpenHeight.toFixed(1)}px not in [30, 38]`);
      const tDismissHeight = tDismiss.getBoundingClientRect().height;
      check(tDismissHeight >= 30 && tDismissHeight <= 38,
        `${mode} transient Dismiss height ${tDismissHeight.toFixed(1)}px not in [30, 38]`);
      check(getComputedStyle(tDismiss).backgroundColor === "rgba(0, 0, 0, 0)",
        `${mode} transient Dismiss should be transparent ghost (got ${getComputedStyle(tDismiss).backgroundColor})`);
      checkTextContrast(tBadge, canvas, `${mode} transient active badge`);
      checkTextContrast(tCountdown, canvas, `${mode} transient countdown`);
      checkTextContrast(tPre, canvas, `${mode} transient copy`);
      checkTextContrast(tOpen, canvas, `${mode} transient Open link`);
      checkTextContrast(tDismiss, canvas, `${mode} transient Dismiss`);
      checkBorderContrast(tDismiss, canvas, `${mode} transient Dismiss border`);
      checkTextContrast(eBadge, canvas, `${mode} transient expired badge`);
      checkTextContrast(eStatus, canvas, `${mode} transient expired status`);

      // --- Checks for 5: Inline workflow card ---
      const wfTitle = workflowBubble.querySelector(".workflow-inline-title");
      const wfOpen = workflowBubble.querySelector("a.workflow-inline-open");
      check(Math.abs(wfTitle.getBoundingClientRect().top - wfOpen.getBoundingClientRect().top) < 12,
        `${mode} inline workflow title and Open button do not share one row`);
      check(getComputedStyle(wfOpen).textDecorationLine === "none",
        `${mode} inline workflow Open link is underlined`);
      const wfOpenHeight = wfOpen.getBoundingClientRect().height;
      check(wfOpenHeight >= 30 && wfOpenHeight <= 38,
        `${mode} inline workflow Open height ${wfOpenHeight.toFixed(1)}px not in [30, 38]`);
      checkTextContrast(wfTitle, canvas, `${mode} workflow inline title`);
      checkTextContrast(workflowBubble.querySelector(".workflow-inline-status"), canvas, `${mode} workflow inline status`);
      checkTextContrast(wfOpen, canvas, `${mode} workflow inline Open`);

      // --- Checks for 6: Settings Access Inbox full card ---
      check(!inboxCard.querySelector("details.access-review-more"), `${mode} Inbox card should not fold Details`);
      const inboxDetailRows = [...inboxCard.querySelectorAll(".access-review-details .permission-detail-row")];
      check(inboxDetailRows.length >= 2, `${mode} Inbox expected at least 2 metadata detail rows`);
      check(Math.abs(inboxDetailRows[0].getBoundingClientRect().top - inboxDetailRows[1].getBoundingClientRect().top) < 8,
        `${mode} Inbox metadata detail rows did not lay out inline`);
      const inboxChoices = [...inboxCard.querySelectorAll(".permission-choice-button")];
      for (const btn of inboxChoices) {
        const h = btn.getBoundingClientRect().height;
        check(h >= 30 && h <= 38, `${mode} Inbox choice button height ${h.toFixed(1)}px not in [30, 38]`);
        check(getComputedStyle(btn).boxShadow === "none",
          `${mode} Inbox choice button '${btn.textContent}' kept shadow: ${getComputedStyle(btn).boxShadow}`);
      }
      const inboxKeepBox = inboxCard.querySelector(".access-review-keep-box").getBoundingClientRect();
      const inboxKeepSelect = inboxCard.querySelector(".access-review-keep-for").getBoundingClientRect();
      check(Math.abs(inboxKeepBox.top - inboxKeepSelect.top) < 12,
        `${mode} Inbox Keep checkbox and duration select do not share one row`);
      const inboxNoteHeight = inboxCard.querySelector(".access-review-note").getBoundingClientRect().height;
      check(inboxNoteHeight <= 44, `${mode} Inbox single-line note too tall (${inboxNoteHeight.toFixed(1)}px)`);
      const inboxChoiceHelp = inboxCard.querySelector(".access-review-choice-help-item");
      check(inboxChoiceHelp && inboxChoiceHelp.getBoundingClientRect().height > 0,
        `${mode} Inbox choice help should be always visible`);
      checkTextContrast(inboxChoiceHelp, canvas, `${mode} Inbox choice help text`);
      checkTextContrast(inboxCard.querySelector(".access-review-keep-help"), canvas, `${mode} Inbox keep help text`);

      // --- Checks for 7: Buttons outside .access-review-card keep 40px size ---
      const outsideBtn = document.createElement("button");
      outsideBtn.type = "button";
      outsideBtn.className = "button primary permission-choice-button";
      outsideBtn.textContent = "Save";
      stage.append(outsideBtn);
      await new Promise(resolve => requestAnimationFrame(resolve));
      const outsideHeight = outsideBtn.getBoundingClientRect().height;
      check(outsideHeight >= 40,
        `${mode} outside-scope .permission-choice-button shrunk to ${outsideHeight.toFixed(1)}px (expected >= 40px)`);
      outsideBtn.remove();

      // --- Checks for 8: Narrow viewport (340..520px) Keep / Hand-on checkbox alignment ---
      for (const narrowWidth of [340, 360, 420, 520]) {
        const narrowHost = document.createElement("div");
        Object.assign(narrowHost.style, { width: `${narrowWidth}px`, display: "grid", gap: "12px" });
        const narrowInbox = renderReviewCard(document, sampleReview(true), { compact: false });
        const narrowBubble = document.createElement("div");
        narrowBubble.className = "message-bubble is-system";
        const narrowSlot = document.createElement("div");
        narrowSlot.className = "chat-access-review";
        const narrowCompact = renderReviewCard(document, sampleReview(true), { compact: true });
        narrowCompact.querySelector(".access-review-more").open = true;
        narrowSlot.append(narrowCompact);
        narrowBubble.append(narrowSlot);
        narrowHost.append(narrowInbox, narrowBubble);
        stage.append(narrowHost);
        await new Promise(resolve => requestAnimationFrame(resolve));
        for (const [label, cardEl] of [["Inbox", narrowInbox], ["compact chat", narrowCompact]]) {
          const cardRect = cardEl.getBoundingClientRect();
          const keepRows = [...cardEl.querySelectorAll(".access-review-keep")];
          check(keepRows.length === 2, `${mode} ${narrowWidth}px ${label}: expected 2 .access-review-keep rows`);
          for (const keepRow of keepRows) {
            const boxEl = keepRow.querySelector(".access-review-keep-box");
            const lblEl = keepRow.querySelector(".access-review-keep-label");
            const boxRect = boxEl.getBoundingClientRect();
            const lblRect = lblEl.getBoundingClientRect();
            check(Math.abs(boxRect.top - lblRect.top) < 8,
              `${mode} ${narrowWidth}px ${label}: checkbox separated vertically from label '${lblEl.textContent}' (box.top=${boxRect.top.toFixed(1)}, label.top=${lblRect.top.toFixed(1)})`);
            check(lblRect.left >= boxRect.right,
              `${mode} ${narrowWidth}px ${label}: label '${lblEl.textContent}' dropped below checkbox (box.right=${boxRect.right.toFixed(1)}, label.left=${lblRect.left.toFixed(1)})`);
            check(lblRect.right <= cardRect.right + 1,
              `${mode} ${narrowWidth}px ${label}: label '${lblEl.textContent}' overflowed card (${lblRect.right.toFixed(1)} > ${cardRect.right.toFixed(1)})`);
            const selectEl = keepRow.querySelector(".access-review-keep-for");
            if (selectEl) {
              const selRect = selectEl.getBoundingClientRect();
              check(selRect.right <= cardRect.right + 1,
                `${mode} ${narrowWidth}px ${label}: duration select overflowed card (${selRect.right.toFixed(1)} > ${cardRect.right.toFixed(1)})`);
            }
          }
        }
        narrowHost.remove();
      }

      results.push(`${mode}: compact pending, waiting, decided, transient active/expired, inline workflow, Settings Inbox, scoped buttons, and narrow checkbox alignment verified`);
    }
    return results;
  } finally {
    stage.remove();
  }
}
