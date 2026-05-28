/**
 * Extracted from chat.html: chat_ui_core.js
 */

  // --- UI helpers ---
  function scrollToBottom() {
    messagesWrap.scrollTop = getMaxUsefulScroll();
  }

  function isHydratingMessages() {
    return messagesEl?.dataset.hydrating === "1";
  }

  function getMaxUsefulScroll() {
    const naturalMax = Math.max(0, messagesWrap.scrollHeight - messagesWrap.clientHeight);
    if (!resetStageActive) return naturalMax;

    const resetPill = messagesEl.querySelector(".sys-notice.pinned");
    if (!resetPill) return naturalMax;

    // 扣掉 spacer 占的额外空间：naturalMax 里包含了 spacer 和 #messages 的 padding-bottom，
    // padding-bottom 是输入框留白，要保留；spacer 是人为撑高的，不能让用户滚进去。
    const spacer = messagesEl.querySelector(".sys-pin-spacer");
    const spacerHeight = spacer ? spacer.offsetHeight : 0;
    const pillTop = Math.max(resetPill.offsetTop - 6, 0);
    const contentMax = Math.max(0, naturalMax - spacerHeight);
    return Math.max(pillTop, contentMax);
  }

  function clampMessagesScrollWithinBounds() {
    const maxUseful = getMaxUsefulScroll();
    if (messagesWrap.scrollTop < 0) {
      messagesWrap.scrollTop = 0;
      return;
    }
    if (messagesWrap.scrollTop > maxUseful) {
      messagesWrap.scrollTop = maxUseful;
    }
  }

  function scheduleRecalcPinnedSpacerHeight() {
    if (spacerRecalcRaf) return;
    spacerRecalcRaf = requestAnimationFrame(() => {
      spacerRecalcRaf = 0;
      recalcPinnedSpacerHeight();
    });
  }

  function recalcPinnedSpacerHeight() {
    if (!resetStageActive) return;
    const spacer = messagesEl.querySelector(".sys-pin-spacer");
    if (!spacer) return;
    const viewportHeight = messagesWrap.clientHeight;
    const nextHeight = Math.max(viewportHeight - 80, 120);
    spacer.style.height = `${nextHeight}px`;
  }

  function clearPinnedSpacer() {
    messagesEl.querySelectorAll(".sys-pin-spacer").forEach((n) => n.remove());
  }

  function addBubble(text, role, { isCommand = false, turnId = "" } = {}) {
    // Coerce objects/arrays to readable text
    if (typeof text !== "string") {
      try { text = JSON.stringify(text, null, 2); } catch (e) { text = String(text || ""); }
    }
    if (window.ChatBubbleRenderer?.renderBubble) {
      return window.ChatBubbleRenderer.renderBubble(text, role, { isCommand, turnId });
    }
    return { row: null, bubble: null };
  }

  function addFileChipBubble(fileName) {
    if (window.ChatBubbleRenderer?.renderFileChip) {
      return window.ChatBubbleRenderer.renderFileChip(fileName);
    }
    return { row: null, bubble: null };
  }

  function getDisplayFileName(fullName) {
    const name = String(fullName || "").trim();
    const lastSlash = Math.max(name.lastIndexOf("/"), name.lastIndexOf("\\"));
    return lastSlash >= 0 ? name.slice(lastSlash + 1) : name;
  }

  function stripFilePrefix(raw) {
    const text = String(raw ?? "");
    const m = text.match(/^\[文件: [^\n]+\]\n```[^\n]*\n[\s\S]*?\n```\n?/);
    if (m) return text.slice(m[0].length).trim();
    return text;
  }

  function sanitizeAssistantDisplayText(raw) {
    const text = String(raw ?? "");
    if (!text) return "";
    const cleaned = text
      .replace(/^\s*\[系统摘要\]\s*/gmu, "")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
    return cleaned;
  }

  function absorbContextCompressionPayload(payload) {
    if (!payload || typeof payload !== "object") return "";
    const usageBefore = normalizeStatusCount(payload.usage_before);
    const usageAfter = normalizeStatusCount(payload.usage_after);
    const serverLimit = normalizeStatusCount(payload.max_context_tokens);
    if (serverLimit >= 100) statusContextTokenLimit = serverLimit;
    if (usageBefore > 0) statusContextPreviousUsageLength = usageBefore;
    if (usageAfter >= 0) statusContextUsageLength = usageAfter;
    renderHeaderStatus();
    return String(payload.text || payload.content || payload.summary || "").trim();
  }

  function _streamDisplay(reasoning, content) {
    const r = String(reasoning || "").trim();
    const c = String(content || "").trim();
    if (!r && !c) return "";
    if (r) {
      const rBlock = markdownQuoteBlock(r);
      return c ? c + "\n\n" + rBlock : rBlock;
    }
    return c;
  }

  function markdownQuoteBlock(text) {
    const raw = String(text ?? "").trim();
    if (!raw) return "";
    return raw
      .split(/\r?\n/)
      .map((line) => line ? `> ${line}` : ">")
      .join("\n");
  }

  function buildStreamFullMd(reasoning, content) {
    // Legacy: still used by non-streaming display path
    return _streamDisplay(reasoning, content);
  }

  function normalizeTurnId(raw) {
    const text = String(raw ?? "").trim();
    if (!text) return "";
    return text.replace(/[^A-Za-z0-9._:-]+/g, "_").slice(0, 80);
  }

  function resetAssistantTurnBubbleMap() {
    window.ChatBubbleRenderer?.resetTurnBubbles?.();
  }

  function resolveAssistantTurnBubble(turnId) {
    return window.ChatBubbleRenderer?.resolveTurnBubble?.(turnId) || null;
  }

  function rememberAssistantTurnBubble(turnId, bubbleEl) {
    window.ChatBubbleRenderer?.rememberTurnBubble?.(turnId, bubbleEl);
  }

  function upsertAssistantTurnBubble(rawText, { turnId = "", append = false, isCommand = false } = {}) {
    return window.ChatBubbleRenderer?.upsertAssistantBubble?.(rawText, { turnId, append, isCommand }) || null;
  }

  function createMsgRow(role, contentEl) {
    if (window.ChatBubbleRenderer?.createRow) {
      return window.ChatBubbleRenderer.createRow(role, contentEl);
    }
    return null;
  }

  function renderTermExecBubble(entry) {
    let data;
    try { data = JSON.parse(entry.content || "{}"); } catch { data = {}; }
    const cmd = data.cmd || "";
    const output = data.output || "(no output)";
    const rc = data.returncode !== null && data.returncode !== undefined ? String(data.returncode) : "-";
    const ok = data.status === "ok";

    const bubble = document.createElement("div");
    bubble.className = "term-exec-bubble";

    const head = document.createElement("div");
    head.className = "term-exec-head";
    head.innerHTML = `<span class="term-exec-dots"><span class="term-exec-dot"></span><span class="term-exec-dot"></span><span class="term-exec-dot"></span></span>
      <span class="term-exec-title">终端执行</span>
      <span class="term-exec-status ${ok ? "ok" : "err"}">${ok ? "完成" : "失败"}</span>`;
    bubble.appendChild(head);

    if (cmd) {
      const cmdEl = document.createElement("div");
      cmdEl.className = "term-exec-cmd";
      cmdEl.textContent = `tinda@agent ~ % ${cmd}`;
      bubble.appendChild(cmdEl);
    }

    const outEl = document.createElement("div");
    outEl.className = "term-exec-output";
    outEl.textContent = output;
    bubble.appendChild(outEl);

    const foot = document.createElement("div");
    foot.className = "term-exec-foot";
    foot.innerHTML = `<span>返回码 ${rc}</span>`;
    bubble.appendChild(foot);

    return bubble;
  }

  function appendMarkdownToBotBubble(bubbleEl, extraText) {
    return window.ChatBubbleRenderer?.appendMarkdownToBubble?.(bubbleEl, extraText) || false;
  }

  // markdown 渲染函数（escapeHtml / safeHref / renderInlineMarkdown / parseTableCells /
  // isTableSeparatorLine / parseTableAlign / renderMarkdown）已抽到 /markdown_renderer.js，
  // 通过 window 全局向后兼容暴露，旧调用点 renderMarkdown(text) / escapeHtml(text) 等继续可用。

  function addTyping() {
    if (window.ChatBubbleRenderer?.renderTyping) {
      return window.ChatBubbleRenderer.renderTyping();
    }
    return null;
  }

  function setStreamToggleUI() {
    const sb = document.getElementById("streamToggleBtn");
    if (!sb) return;
    const iconOn = sb.querySelector(".stream-on");
    const iconOff = sb.querySelector(".stream-off");
    if (iconOn && iconOff) {
      iconOn.style.display = streamEnabled ? "" : "none";
      iconOff.style.display = streamEnabled ? "none" : "";
    }
    sb.classList.toggle("on", streamEnabled);
    const tip = "切换流式输出";
    sb.title = tip;
    sb.setAttribute("data-tip", tip);
  }

  function setDeepToggleUI() {
    if (!deepBtnEl) return;
    deepBtnEl.classList.toggle("on", deepEnabled);
    deepBtnEl.innerHTML = "<span>Deep 对齐</span>";
    const tip = deepEnabled ? "Deep 对齐已开启：发送前先确认理解" : "Deep 对齐已关闭";
    deepBtnEl.title = tip;
    deepBtnEl.setAttribute("data-tip", tip);
    deepBtnEl.setAttribute("aria-pressed", deepEnabled ? "true" : "false");
    renderComposerSelections();
  }

  function toggleDeepMode() {
    deepEnabled = !deepEnabled;
    localStorage.setItem(DEEP_ENABLED_KEY, deepEnabled ? "1" : "0");
    setDeepToggleUI();
    addSystemNotice(deepEnabled ? "Deep 对齐已开启" : "Deep 对齐已关闭", { persist: false });
  }

  function toggleStream() {
    streamEnabled = !streamEnabled;
    localStorage.setItem(STREAM_ENABLED_KEY, streamEnabled ? "1" : "0");
    setStreamToggleUI();
    addSystemNotice(streamEnabled ? "已开启流式输出" : "已关闭流式输出");
  }

  function addSystemNotice(text, { pinTop = false, persist = true, sessionId = getSessionId() } = {}) {
    const pill = document.createElement("div");
    pill.className = "sys-notice";
    pill.textContent = text;
    if (!pinTop) {
      const spacer = messagesEl.querySelector(".sys-pin-spacer");
      if (spacer) {
        messagesEl.insertBefore(pill, spacer);
      } else {
        messagesEl.appendChild(pill);
      }
    } else {
      messagesEl.appendChild(pill);
    }

    if (pinTop) {
      resetStageActive = true;
      // 清掉旧的 pinned 标记，只留最新的重置 pill
      messagesEl.querySelectorAll(".sys-notice.pinned").forEach((n) => {
        if (n !== pill) n.classList.remove("pinned");
      });
      pill.classList.add("pinned");
      // 重建 spacer：在 pill 之后追加一个大占位，撑高 scrollHeight 才能把 pill 滚到顶
      clearPinnedSpacer();
      const spacer = document.createElement("div");
      spacer.className = "sys-pin-spacer";
      spacer.setAttribute("aria-hidden", "true");
      messagesEl.appendChild(spacer);
      recalcPinnedSpacerHeight();

      requestAnimationFrame(() => {
        messagesWrap.scrollTo({
          top: Math.max(pill.offsetTop - 6, 0),
          behavior: "smooth",
        });
      });
    } else if (resetStageActive) {
      // 重置阶段：保持重置 pill 在顶，新 pill 自然显示在其下方
      const resetPill = messagesEl.querySelector(".sys-notice.pinned");
      if (resetPill) {
        requestAnimationFrame(() => {
          messagesWrap.scrollTo({
            top: Math.max(resetPill.offsetTop - 6, 0),
            behavior: "smooth",
          });
        });
      } else {
        scrollToBottom();
      }
    } else {
      scrollToBottom();
    }
    if (persist) {
      enqueueSessionEntries(
        [
          {
            role: "system",
            entry_type: "notice",
            content: String(text ?? ""),
            ts: toIsoWithOffset(new Date()),
          },
        ],
        { sessionId },
      );
    }
    return pill;
  }

  function normalizePlanPayload(raw) {
    const stack = [];
    const seen = new Set();
    if (raw && typeof raw === "object") stack.push(raw);
    let result = null;
    while (stack.length > 0) {
      const source = stack.shift();
      if (!source || typeof source !== "object" || seen.has(source)) continue;
      seen.add(source);
      const looksLikePlan = String(source.kind || "") === "plan"
        || (Array.isArray(source.steps) && (
          Object.prototype.hasOwnProperty.call(source, "goal")
          || Object.prototype.hasOwnProperty.call(source, "status")
          || Object.prototype.hasOwnProperty.call(source, "notes")
        ));
      if (looksLikePlan) {
        result = source;
        break;
      }
      ["data", "tool_marker", "result"].forEach((key) => {
        const next = source[key];
        if (next && typeof next === "object") stack.push(next);
      });
    }
    if (!result || typeof result !== "object") return null;
    const inferPlanStepStatus = (text) => {
      const rawText = String(text ?? "").trim();
      if (!rawText) return "";
      if (/✅|(?:→|—|-)\s*(?:已完成|已实施|完成|done)(?:\s*[（(][^）)]*[）)])?\s*$/i.test(rawText)) return "done";
      if (/(?:^|[→—\-\s])(?:进行中|处理中|in[_ -]?progress)(?:$|[，。,.\s])/i.test(rawText)) return "in_progress";
      if (/⏳|(?:^|[→—\-\s])(?:未实施|搁置|待定|阻塞|blocked)(?:$|[：:，。,.\s])/i.test(rawText)) return "blocked";
      return "";
    };
    const cleanPlanStepText = (text) => {
      let clean = String(text ?? "").trim();
      clean = clean.replace(/^\s*[-*•]\s*/, "");
      clean = clean.replace(/^\s*\d{1,3}[.、)]\s*/, "");
      clean = clean.replace(/[✅⏳🏁]/g, "");
      clean = clean.trim();
      clean = clean.replace(/\s*(?:→|—|-)\s*(?:已完成|已实施|完成|done)(?:\s*[（(][^）)]*[）)])?\s*$/i, "");
      clean = clean.replace(/\s*(?:→|—|-)\s*(?:进行中|处理中|in[_ -]?progress)\s*$/i, "");
      clean = clean.replace(/\s*(?:→|—|-)\s*(?:待定|未实施|搁置|阻塞|blocked)(?:\s*[：:].*)?\s*$/i, "");
      clean = clean.replace(/^(?:未实施|待定|搁置)(?:[（(][^）)]*[）)])?\s*[：:]\s*/, "");
      return clean.replace(/\s{2,}/g, " ").trim();
    };
    const schemaVersion = Number(result.schema_version || result.schemaVersion || 0);
    const allowLegacyTextStatus = !schemaVersion || schemaVersion < 2;
    const stepsRaw = Array.isArray(result.steps) ? result.steps : [];
    const steps = stepsRaw.map((step, idx) => {
      const rawText = typeof step === "object" ? String(step.text || "") : String(step || "");
      let stepStatus = String((step && typeof step === "object" ? step.status : "") || "pending").trim().toLowerCase();
      const inferredStatus = allowLegacyTextStatus ? inferPlanStepStatus(rawText) : "";
      if (allowLegacyTextStatus && (!stepStatus || stepStatus === "pending") && inferredStatus) stepStatus = inferredStatus;
      if (!["pending", "in_progress", "done", "blocked"].includes(stepStatus)) stepStatus = "pending";
      return {
        index: Number((step && typeof step === "object" ? step.index : 0) || idx + 1),
        text: allowLegacyTextStatus ? cleanPlanStepText(rawText) : rawText.replace(/^\s*[-*•]\s*/, "").replace(/^\s*\d{1,3}[.、)]\s*/, "").trim(),
        status: stepStatus,
      };
    }).filter((step) => step.text);
    const status = String(result.status || "planned").trim();
    const completed = result.completed === true || status === "complete";
    return {
      action: String(result.action || "create").trim().toLowerCase(),
      goal: String(result.goal || "").trim(),
      status,
      schema_version: schemaVersion || 1,
      completed,
      notes: String(result.notes || "").trim(),
      completion_note: String(result.completion_note || result.completionNote || "").trim(),
      update_note: String(result.update_note || result.updateNote || "").trim(),
      step_updates: normalizePlanStepUpdates(result.step_updates || result.stepUpdates || [], {
        step_index: result.step_index || result.stepIndex || 0,
        step_text: result.step_text || result.stepText || "",
        step_status: result.step_status || result.stepStatus || "",
        update_note: result.update_note || result.updateNote || "",
      }),
      steps,
    };
  }

  function normalizePlanStepStatus(status) {
    const raw = String(status || "pending").trim().toLowerCase();
    if (raw === "complete" || raw === "completed" || raw === "finished") return "done";
    if (raw === "doing" || raw === "working" || raw === "progress") return "in_progress";
    if (raw === "block") return "blocked";
    return ["pending", "in_progress", "done", "blocked"].includes(raw) ? raw : "pending";
  }

  function normalizePlanStepUpdates(rawUpdates, fallback = {}) {
    const values = Array.isArray(rawUpdates)
      ? rawUpdates
      : rawUpdates && typeof rawUpdates === "object"
      ? [rawUpdates]
      : [];
    const fallbackIndex = Number(fallback.step_index || 0);
    const fallbackText = String(fallback.step_text || "").trim();
    const fallbackStatus = String(fallback.step_status || "").trim();
    if (fallbackIndex > 0 || fallbackText || fallbackStatus) {
      values.push({
        index: fallbackIndex,
        text: fallbackText,
        status: fallbackStatus,
        note: fallback.update_note || "",
      });
    }
    return values.map((item) => {
      if (!item || typeof item !== "object") return null;
      const index = Number(item.index || item.step_index || 0);
      const text = String(item.text || item.step_text || "").trim();
      if (index <= 0 && !text) return null;
      return {
        index,
        text,
        status: normalizePlanStepStatus(item.status || item.step_status),
        note: String(item.note || item.update_note || "").trim(),
      };
    }).filter(Boolean);
  }

  function mergePlanPayload(base, eventPayload) {
    const nextEvent = normalizePlanPayload(eventPayload);
    if (!nextEvent) return base || null;
    if (nextEvent.action === "clear") return nextEvent;
    if (nextEvent.action !== "set_step_status") {
      return nextEvent;
    }
    const current = base ? normalizePlanPayload(base) : null;
    if (!current) return nextEvent;
    const merged = {
      ...current,
      action: "update",
      status: nextEvent.status === "planned" ? current.status : nextEvent.status,
      notes: nextEvent.notes || current.notes,
      completion_note: nextEvent.completion_note || current.completion_note,
      update_note: nextEvent.update_note || current.update_note || "",
      steps: current.steps.map((step) => ({ ...step })),
    };
    nextEvent.step_updates.forEach((update) => {
      let idx = Number(update.index || 0) - 1;
      if (idx < 0 && update.text) {
        const needle = update.text.toLowerCase();
        idx = merged.steps.findIndex((step) => String(step.text || "").toLowerCase().includes(needle));
      }
      if (idx >= 0 && idx < merged.steps.length) {
        merged.steps[idx] = { ...merged.steps[idx], status: update.status };
      }
    });
    return merged;
  }

  function planStateView(payload) {
    const status = String(payload?.status || "planned").trim();
    if (payload?.completed || status === "complete") return { text: "已完成", cls: "complete" };
    if (status === "awaiting_completion_confirmation") return { text: "已修订", cls: "" };
    if (status === "revised") return { text: "已修订", cls: "" };
    if (status === "blocked") return { text: "已阻塞", cls: "blocked" };
    return { text: "计划中", cls: "" };
  }

  function planStepStatusText(status) {
    const normalized = String(status || "pending").trim().toLowerCase();
    if (normalized === "done") return "完成";
    if (normalized === "in_progress") return "进行中";
    if (normalized === "blocked") return "阻塞";
    return "待办";
  }

  function renderPlanFloat(plan) {
    if (!planFloatEl || !planFloatBodyEl || !planFloatSubtitleEl) return;
    const currentPayload = planFloatEl?._planPayload || null;
    const incomingPayload = normalizePlanPayload(plan);
    const payload = incomingPayload?.action === "set_step_status"
      ? mergePlanPayload(currentPayload, incomingPayload)
      : incomingPayload;
    if (!payload) return;
    if (payload.action === "clear") {
      planFloatEl._planPayload = null;
      clearPlanFloatContent();
      hidePlanFloat();
      return;
    }
    planFloatEl._planPayload = payload;
    const stateView = planStateView(payload);
    planFloatSubtitleEl.textContent = payload.goal ? `${stateView.text} · ${payload.goal}` : `${stateView.text} · ${payload.steps.length || 0} 个步骤`;
    const stateHtml = `<div class="plan-state ${escapeHtml(stateView.cls)}">${escapeHtml(stateView.text)}</div>`;
    const goalHtml = payload.goal
      ? `<p class="plan-goal">${escapeHtml(payload.goal)}</p>`
      : `<p class="plan-goal">LLM 已记录一份执行计划。</p>`;
    const stepsHtml = payload.steps.length
      ? `<ol class="plan-steps">${payload.steps.map((step, idx) => `
          <li class="plan-step plan-step-${escapeHtml(step.status || "pending")}">
            <span class="plan-step-index">${escapeHtml(String(step.index || idx + 1))}</span>
            <span class="plan-step-text">${escapeHtml(step.text)}</span>
            <span class="plan-step-status">${escapeHtml(planStepStatusText(step.status))}</span>
          </li>`).join("")}</ol>`
      : "";
    const notesHtml = payload.notes ? `<div class="plan-notes">${renderMarkdown(payload.notes)}</div>` : "";
    const completionHtml = payload.completion_note
      ? `<div class="plan-completion-note">${escapeHtml(payload.completion_note)}</div>`
      : "";
    planFloatBodyEl.innerHTML = stateHtml + goalHtml + stepsHtml + notesHtml + completionHtml;
    showPlanFloat();
  }

  function clearPlanFloatContent() {
    if (planFloatBodyEl) planFloatBodyEl.innerHTML = "";
    if (planFloatSubtitleEl) planFloatSubtitleEl.textContent = "等待 LLM 制定计划";
    if (planFloatEl) {
      planFloatEl.classList.remove("collapsed");
      planFloatEl._planPayload = null;
    }
    if (planFloatCollapseBtnEl) {
      planFloatCollapseBtnEl.textContent = "－";
      planFloatCollapseBtnEl.setAttribute("aria-label", "折叠计划面板");
    }
  }

  function showPlanFloat() {
    if (!planFloatEl) return;
    planFloatEl.style.display = "flex";
    requestAnimationFrame(() => planFloatEl.classList.add("show"));
  }

  function hidePlanFloat() {
    if (!planFloatEl) return;
    planFloatEl.classList.remove("show");
    window.setTimeout(() => {
      if (!planFloatEl.classList.contains("show")) planFloatEl.style.display = "none";
    }, 280);
  }

  async function deleteCurrentPlan() {
    const sid = String(getSessionId() || "").trim();
    if (!sid || isDraftSessionId(sid)) {
      clearPlanFloatContent();
      hidePlanFloat();
      return;
    }
    if (planDeleteInFlight) return;
    planDeleteInFlight = true;
    planDeletedBySession.set(sid, true);
    clearPlanFloatContent();
    hidePlanFloat();
    try {
      const res = await apiFetch(`/sessions/${encodeURIComponent(sid)}/plan`, { method: "DELETE" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data?.ok === false) throw new Error(String(data?.error || `HTTP ${res.status}`));
    } catch (e) {
      showToast(`删除计划失败：${String(e?.message || e)}`, "warn");
    } finally {
      planDeleteInFlight = false;
    }
  }

  function togglePlanFloatCollapsed() {
    if (!planFloatEl || !planFloatCollapseBtnEl) return;
    const collapsed = !planFloatEl.classList.contains("collapsed");
    planFloatEl.classList.toggle("collapsed", collapsed);
    planFloatCollapseBtnEl.textContent = collapsed ? "+" : "－";
    planFloatCollapseBtnEl.setAttribute("aria-label", collapsed ? "展开计划面板" : "折叠计划面板");
  }

  function clampPlanFloat(left, top) {
    const rect = planFloatEl?.getBoundingClientRect?.();
    const width = rect?.width || 420;
    const height = rect?.height || 220;
    const maxLeft = Math.max(8, window.innerWidth - width - 8);
    const maxTop = Math.max(8, window.innerHeight - Math.min(height, window.innerHeight - 16) - 8);
    return {
      left: Math.max(8, Math.min(Number(left) || 8, maxLeft)),
      top: Math.max(8, Math.min(Number(top) || 8, maxTop)),
    };
  }

  function startPlanFloatDrag(e) {
    if (!planFloatEl || e.target?.closest?.(".plan-float-actions")) return;
    planFloatDragging = true;
    const rect = planFloatEl.getBoundingClientRect();
    planFloatDragStartX = e.clientX;
    planFloatDragStartY = e.clientY;
    planFloatStartLeft = rect.left;
    planFloatStartTop = rect.top;
    planFloatEl.classList.add("dragging");
    planFloatEl.style.left = `${rect.left}px`;
    planFloatEl.style.top = `${rect.top}px`;
    planFloatEl.style.right = "auto";
    e.preventDefault();
  }

  function movePlanFloatDrag(e) {
    if (!planFloatDragging || !planFloatEl) return;
    const pos = clampPlanFloat(
      planFloatStartLeft + e.clientX - planFloatDragStartX,
      planFloatStartTop + e.clientY - planFloatDragStartY,
    );
    planFloatEl.style.left = `${pos.left}px`;
    planFloatEl.style.top = `${pos.top}px`;
  }

  function stopPlanFloatDrag() {
    if (!planFloatDragging) return;
    planFloatDragging = false;
    planFloatEl?.classList.remove("dragging");
  }

  function showPlanPanelFromToolResult(step) {
    const name = String(step?.agent_tool || step?.name || step?.tool_name || "").trim();
    if (name !== "plan") return false;
    const payload = normalizePlanPayload(step?.result || step);
    if (!payload) return false;
    const sid = String(getSessionId() || "").trim();
    if (payload.action === "clear") {
      if (sid) planDeletedBySession.set(sid, true);
      if (sid) planCurrentBySession.delete(sid);
      clearPlanFloatContent();
      hidePlanFloat();
      return true;
    }
    if (sid) planDeletedBySession.set(sid, false);
    if (sid) planCurrentBySession.set(sid, payload);
    renderPlanFloat(payload);
    return true;
  }

  function showPlanPanelsFromTrace(trace) {
    if (!Array.isArray(trace)) return false;
    let shown = false;
    trace.forEach((step) => {
      if (showPlanPanelFromToolResult(step)) shown = true;
    });
    return shown;
  }

  function showLatestPlanFromSessionEntries(entries) {
    if (!Array.isArray(entries)) return false;
    let latest = null;
    entries.forEach((entry) => {
      const content = entry?.content;
      if (!Array.isArray(content)) return;
      content.forEach((step) => {
        if (!step || String(step.kind || "") !== "tool_marker") return;
        const marker = step.data && typeof step.data === "object" ? step.data : {};
        const name = String(marker.name || marker.tool_name || marker.agent_tool || "").trim();
        if (name !== "plan") return;
        const payload = normalizePlanPayload(marker);
        if (payload) latest = mergePlanPayload(latest, payload);
      });
    });
    if (!latest) return false;
    renderPlanFloat(latest);
    return true;
  }

  function isPlanToolTraceStep(step) {
    return String(step?.agent_tool || step?.name || step?.tool_name || "").trim() === "plan";
  }

  function buildDeepAlignmentContext(payload) {
    return String(payload?.alignment_text || "").trim();
  }

  function getDeepDisplayIndex(payload) {
    const rounds = Array.isArray(payload?.rounds) ? payload.rounds : [];
    const fallback = Math.max(0, Math.min(rounds.length - 1, Number(payload?.active_index ?? (rounds.length - 1)) || 0));
    const raw = Number(payload?.display_index ?? fallback);
    if (!rounds.length) return 0;
    return Math.max(0, Math.min(rounds.length - 1, Number.isFinite(raw) ? raw : fallback));
  }

  function normalizeDeepPayload(payload) {
    const next = payload && typeof payload === "object" ? payload : {};
    const rounds = Array.isArray(next.rounds) ? next.rounds : [];
    const latestIndex = Math.max(0, rounds.length - 1);
    const activeIndex = Math.max(0, Math.min(latestIndex, Number(next.active_index ?? latestIndex) || 0));
    next.active_index = activeIndex;
    if (next.display_index === undefined || next.force_latest === true) {
      next.display_index = activeIndex;
    }
    return next;
  }

  function getDeepPendingAsk(payload) {
    const ask = payload?.pending_deep_ask;
    return ask && typeof ask === "object" ? ask : null;
  }

  function getDeepDisplayText(payload) {
    const rounds = Array.isArray(payload?.rounds) ? payload.rounds : [];
    const idx = getDeepDisplayIndex(payload);
    const row = rounds[idx] || {};
    return String(row.alignment_text || payload?.alignment_text || "正在整理理解...");
  }

  function updateDeepCardView(wrapper) {
    if (!wrapper?.card || !wrapper.payload) return;
    const payload = wrapper.payload;
    const rounds = Array.isArray(payload.rounds) ? payload.rounds : [];
    const idx = getDeepDisplayIndex(payload);
    const body = wrapper.card.querySelector(".deep-align-body");
    if (body) body.innerHTML = renderMarkdown(getDeepDisplayText(payload));
    const countEl = wrapper.card.querySelector("[data-deep-page-count]");
    if (countEl) countEl.textContent = rounds.length > 0 ? `${idx + 1}/${rounds.length}` : "0/0";
    const prevBtn = wrapper.card.querySelector('[data-action="prev-round"]');
    const nextBtn = wrapper.card.querySelector('[data-action="next-round"]');
    if (prevBtn) prevBtn.disabled = idx <= 0;
    if (nextBtn) nextBtn.disabled = !rounds.length || idx >= rounds.length - 1;
    wrapper.card.classList.toggle("collapsed", !!payload.collapsed);
    wrapper.card.classList.toggle("readonly", !!payload.readonly);
    const actions = wrapper.card.querySelector(".deep-align-actions");
    const ask = getDeepPendingAsk(payload);
    if (actions) actions.hidden = !!payload.collapsed || !!payload.readonly || !!ask;
    const askPanel = wrapper.card.querySelector(".deep-ask-panel");
    if (askPanel) {
      renderDeepAskPanel(wrapper, askPanel, ask);
    }
    wrapper.card.classList.toggle("asking", !!ask);
  }

  function collapseDeepCard(card) {
    if (!card?.payload || !card.card) return;
    card.payload.collapsed = true;
    card.payload.readonly = true;
    card.card.classList.remove("busy", "revising");
    updateDeepCardView(card);
  }

  function toggleDeepRecord(card) {
    if (!card?.payload?.readonly) return;
    card.payload.collapsed = !card.payload.collapsed;
    updateDeepCardView(card);
  }

  function switchDeepRound(card, delta) {
    if (!card?.payload) return;
    const rounds = Array.isArray(card.payload.rounds) ? card.payload.rounds : [];
    if (!rounds.length) return;
    const next = Math.max(0, Math.min(rounds.length - 1, getDeepDisplayIndex(card.payload) + Number(delta || 0)));
    card.payload.display_index = next;
    card.payload.collapsed = false;
    updateDeepCardView(card);
    scrollToBottom();
  }

  function renderDeepAskPanel(wrapper, panel, ask) {
    if (!panel) return;
    if (!ask) {
      panel.innerHTML = "";
      return;
    }
    const options = Array.isArray(ask.options) ? ask.options : [];
    const selected = String(ask.selected_choice || "");
    const noneValue = String(ask.none_of_them_value || "__none_of_them__");
    const allowCustom = ask.allow_custom_answer !== false;
    const optionsHtml = options.map((option) => {
      const value = String(option || "");
      const label = value === noneValue ? String(ask.none_of_them_label || "以上都不是，我自己补充") : value;
      const cls = value === selected ? "deep-ask-option selected" : "deep-ask-option";
      return `<button type="button" class="${cls}" data-deep-ask-choice="${escapeHtml(value)}">${escapeHtml(label)}</button>`;
    }).join("");
    panel.innerHTML = `
      <p class="deep-ask-question">${escapeHtml(String(ask.question || "需要你补充一个条件"))}</p>
      <div class="deep-ask-options">${optionsHtml}</div>
      ${allowCustom ? `<textarea class="deep-ask-input" rows="3" placeholder="${escapeHtml(String(ask.placeholder || "补充你的答案或限制条件..."))}">${escapeHtml(String(ask.answer_text || ""))}</textarea>` : ""}
      <div class="deep-ask-actions">
        <button type="button" data-action="cancel-deep-ask">取消</button>
        <button type="button" class="deep-ask-submit" data-action="submit-deep-ask">提交回答</button>
      </div>`;
    panel.querySelectorAll("[data-deep-ask-choice]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const choice = String(btn.getAttribute("data-deep-ask-choice") || "");
        ask.selected_choice = choice;
        const isNone = choice === noneValue;
        const input = panel.querySelector(".deep-ask-input");
        if (input && !input.value.trim() && !isNone) input.value = choice;
        if (input && isNone) input.focus();
        renderDeepAskPanel(wrapper, panel, ask);
      });
    });
    panel.querySelector(".deep-ask-input")?.addEventListener("input", (e) => {
      ask.answer_text = String(e.target?.value || "");
    });
    panel.querySelector('[data-action="submit-deep-ask"]')?.addEventListener("click", () => submitDeepAskAnswer(wrapper));
    panel.querySelector('[data-action="cancel-deep-ask"]')?.addEventListener("click", () => cancelDeepAskAnswer(wrapper));
  }

  async function sendDeepAskDecision(wrapper, approval) {
    const ask = getDeepPendingAsk(wrapper?.payload);
    if (!ask) return;
    const sid = String(wrapper?.payload?.sid || getSessionId() || "").trim();
    if (!sid) return;
    const selectedChoice = String(ask.selected_choice || "").trim();
    const noneValue = String(ask.none_of_them_value || "__none_of_them__");
    const isNoneChoice = selectedChoice && selectedChoice === noneValue;
    const answerText = String(ask.answer_text || "").trim() || (isNoneChoice ? "" : selectedChoice);
    if (approval && !answerText && !selectedChoice) {
      showToast("请先选择或补充回答", "warn");
      return;
    }
    if (approval && isNoneChoice && !String(ask.answer_text || "").trim()) {
      showToast("选择“以上都不是”后请补充你的答案", "warn");
      wrapper.card?.querySelector(".deep-ask-input")?.focus();
      return;
    }
    setDeepCardBusy(wrapper, true);
    try {
      const res = await apiFetch("/terminal/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sid,
          approval: !!approval,
          kind: "question",
          call_id: ask.call_id || ask.confirm_id || "",
          choice: isNoneChoice ? "" : selectedChoice,
          answer: answerText,
        }),
      });
      const data = await res.json();
      if (!res.ok || data?.ok === false) throw new Error(String(data?.error || `http ${res.status}`));
      const confirmPending = Array.isArray(data?.pending) ? data.pending : [];
      if (data?.active && String(data?.alignment_text || "").trim()) {
        removeDeepCard(deepActiveCard);
        deepPendingPayload = normalizeDeepPayload({
          sid,
          message: String(data.original_message || wrapper.payload?.message || ""),
          file_names: Array.isArray(data.file_names) ? data.file_names : [],
          file_contents: Array.isArray(data.file_contents) ? data.file_contents : [],
          alignment_text: String(data.alignment_text || ""),
          rounds: Array.isArray(data.rounds) ? data.rounds : [],
          active_index: Number(data.active_index ?? 0),
          can_back: !!data.can_back,
          force_latest: true,
        });
        renderDeepAlignmentCard(deepPendingPayload);
      } else if (data?.state === "cancelled") {
        deepPendingPayload = null;
        removeDeepCard(deepActiveCard);
      }
      setPendingConfirmFromList(confirmPending);
      showToast(data?.state === "cancelled" ? "已取消 Deep 对齐" : "已提交回答");
    } catch (e) {
      showToast(`Deep 回答提交失败：${String(e?.message || e)}`, "warn");
      setDeepCardBusy(wrapper, false);
    }
  }

  function submitDeepAskAnswer(wrapper) {
    return sendDeepAskDecision(wrapper, true);
  }

  function cancelDeepAskAnswer(wrapper) {
    return sendDeepAskDecision(wrapper, false);
  }

  function removeDeepCard(card) {
    if (card?.row?.isConnected) card.row.remove();
    if (deepActiveCard === card) deepActiveCard = null;
  }

  function renderDeepLoadingBubble(text) {
    const rendered = addBubble(String(text || "正在理解您的问题..."), "bot");
    rendered?.row?.classList?.add("deep-loading-msg");
    return rendered;
  }

  function replaceDeepLoadingWithCard(loading, card) {
    if (!loading?.row?.isConnected || !card?.row) return;
    loading.row.replaceWith(card.row);
  }

  function setDeepCardBusy(card, busy) {
    if (!card?.card) return;
    card.card.classList.toggle("busy", !!busy);
    card.card.querySelectorAll("button, textarea").forEach((el) => {
      el.disabled = !!busy;
    });
  }

  function showDeepRevisionInput(card) {
    if (!card?.card) return;
    card.payload.collapsed = false;
    updateDeepCardView(card);
    card.card.classList.add("revising");
    const box = card.card.querySelector(".deep-align-revision textarea");
    if (box) {
      box.disabled = false;
      box.focus();
    }
  }

  async function submitDeepRevision(card) {
    if (!card?.payload) return;
    const textarea = card.card?.querySelector(".deep-align-revision textarea");
    const revision = String(textarea?.value || "").trim();
    if (!revision) {
      showToast("请先输入需要修正的理解点", "warn");
      textarea?.focus();
      return;
    }
    setDeepCardBusy(card, true);
    try {
      const sid = String(card.payload.sid || getSessionId() || "").trim();
      const res = await apiFetch(`/sessions/${encodeURIComponent(sid)}/deep/revise`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revision }),
      });
      const data = await res.json();
      if (!res.ok || data?.ok === false) throw new Error(String(data?.error || `http ${res.status}`));
      removeDeepCard(card);
      renderDeepAlignmentCard(normalizeDeepPayload({
        ...card.payload,
        alignment_text: String(data.alignment_text || ""),
        rounds: Array.isArray(data.rounds) ? data.rounds : [],
        active_index: Number(data.active_index || 0),
        can_back: !!data.can_back,
        pending_deep_ask: data.pending_deep_ask || null,
        force_latest: true,
      }));
    } catch (e) {
      showToast(`Deep 修正失败：${String(e?.message || e)}`, "warn");
      setDeepCardBusy(card, false);
    }
  }

  async function backDeepAlignment(card) {
    if (!card?.payload) return;
    setDeepCardBusy(card, true);
    try {
      const sid = String(card.payload.sid || getSessionId() || "").trim();
      const res = await apiFetch(`/sessions/${encodeURIComponent(sid)}/deep/back`, { method: "POST" });
      const data = await res.json();
      if (!res.ok || data?.ok === false) throw new Error(String(data?.error || `http ${res.status}`));
      removeDeepCard(card);
      renderDeepAlignmentCard(normalizeDeepPayload({
        ...card.payload,
        alignment_text: String(data.alignment_text || ""),
        rounds: Array.isArray(data.rounds) ? data.rounds : [],
        active_index: Number(data.active_index || 0),
        can_back: !!data.can_back,
        pending_deep_ask: data.pending_deep_ask || null,
        force_latest: true,
      }));
    } catch (e) {
      showToast(`返回上一版失败：${String(e?.message || e)}`, "warn");
      setDeepCardBusy(card, false);
    }
  }

  async function cancelDeepAlignment(card) {
    const sid = String(card?.payload?.sid || getSessionId() || "").trim();
    if (sid) {
      apiFetch(`/sessions/${encodeURIComponent(sid)}/deep/cancel`, { method: "POST" }).catch(() => {});
    }
    removeDeepCard(card);
    deepPendingPayload = null;
  }

  async function confirmDeepAlignment(card) {
    if (!card?.payload) return;
    const payload = card.payload;
    setDeepCardBusy(card, true);
    try {
      const sid = String(payload.sid || getSessionId() || "").trim();
      if (sid) {
        await apiFetch(`/sessions/${encodeURIComponent(sid)}/deep/confirm`, { method: "POST" }).catch(() => {});
      }
      collapseDeepCard(card);
      deepPendingPayload = null;
      inputEl.value = "";
      autoResize();
      await sendMessage({
        bypassDeep: true,
        skipDeepEcho: true,
        messageOverride: String(payload.message || ""),
        fileNamesOverride: Array.isArray(payload.file_names) ? payload.file_names : [],
        fileContentsOverride: Array.isArray(payload.file_contents) ? payload.file_contents : [],
        deepAlignmentContext: buildDeepAlignmentContext(payload),
      });
    } catch (e) {
      showToast(`Deep 确认失败：${String(e?.message || e)}`, "warn");
      setDeepCardBusy(card, false);
    }
  }

  function renderDeepAlignmentCard(payload) {
    payload = normalizeDeepPayload(payload);
    const row = document.createElement("div");
    row.className = "deep-align-row";
    const card = document.createElement("div");
    card.className = "deep-align-card";
    const rounds = Array.isArray(payload.rounds) ? payload.rounds : [];
    payload.display_index = getDeepDisplayIndex(payload);
    card.innerHTML = `
      <div class="deep-align-head">
        <div class="deep-align-title">Deep 理解确认</div>
        <div class="deep-align-round">
          <button type="button" class="deep-align-page-btn" data-action="prev-round" aria-label="上一条 Deep 记录">&lt;</button>
          <span data-deep-page-count>${escapeHtml(rounds.length > 0 ? `${payload.display_index + 1}/${rounds.length}` : "0/0")}</span>
          <button type="button" class="deep-align-page-btn" data-action="next-round" aria-label="下一条 Deep 记录">&gt;</button>
        </div>
      </div>
      <div class="deep-align-body"></div>
      <div class="deep-ask-panel"></div>
      <div class="deep-align-actions">
        <button type="button" class="deep-primary" data-action="confirm">一致，继续执行</button>
        <button type="button" data-action="revise">不一致，补充说明</button>
        <button type="button" data-action="back">返回上一级</button>
        <button type="button" data-action="cancel">取消</button>
        <div class="deep-align-revision">
          <textarea rows="2" placeholder="告诉我哪里理解错了，或补充你的真实要求..."></textarea>
          <button type="button" class="deep-primary" data-action="submit-revision">重新对齐</button>
        </div>
      </div>`;
    const wrapper = { row, card, payload };
    card.querySelector('[data-action="confirm"]')?.addEventListener("click", () => confirmDeepAlignment(wrapper));
    card.querySelector('[data-action="revise"]')?.addEventListener("click", () => showDeepRevisionInput(wrapper));
    card.querySelector('[data-action="back"]')?.addEventListener("click", () => backDeepAlignment(wrapper));
    card.querySelector('[data-action="cancel"]')?.addEventListener("click", () => cancelDeepAlignment(wrapper));
    card.querySelector('[data-action="submit-revision"]')?.addEventListener("click", () => submitDeepRevision(wrapper));
    card.querySelector('[data-action="prev-round"]')?.addEventListener("click", () => switchDeepRound(wrapper, -1));
    card.querySelector('[data-action="next-round"]')?.addEventListener("click", () => switchDeepRound(wrapper, 1));
    card.querySelector(".deep-align-head")?.addEventListener("click", (e) => {
      if (e.target?.closest?.(".deep-align-page-btn")) return;
      toggleDeepRecord(wrapper);
    });
    const backBtn = card.querySelector('[data-action="back"]');
    if (backBtn) backBtn.disabled = !payload.can_back;
    updateDeepCardView(wrapper);
    row.appendChild(card);
    messagesEl.appendChild(row);
    deepActiveCard = wrapper;
    scrollToBottom();
    return wrapper;
  }

  function mountDeepAlignmentCard(payload, replaceTarget = null) {
    const wrapper = renderDeepAlignmentCard(payload);
    if (replaceTarget?.row?.isConnected) {
      replaceDeepLoadingWithCard(replaceTarget, wrapper);
      scrollToBottom();
    }
    return wrapper;
  }

  async function startDeepAlignment(payload) {
    deepAlignmentBusy = true;
    setMessageSendInFlight(true);
    removeDeepCard(deepActiveCard);
    const deepLoadingBubble = renderDeepLoadingBubble("正在理解您的问题...");
    try {
      const res = await apiFetch(`/sessions/${encodeURIComponent(payload.sid)}/deep/align`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: payload.sid,
          message: payload.message,
          file_names: payload.file_names || [],
          file_contents: payload.file_contents || [],
        }),
      });
      const data = await res.json();
      if (!res.ok || data?.ok === false) throw new Error(String(data?.error || `http ${res.status}`));
      const pendingCount = Math.max(0, Number(data?.pending_confirm_count || 0));
      if (pendingCount > 0 || data?.pending_confirmation === true) {
        deepPendingPayload = normalizeDeepPayload({
          ...payload,
          alignment_text: "",
          rounds: Array.isArray(data.rounds) ? data.rounds : [],
          active_index: Number(data.active_index || 0),
          can_back: !!data.can_back,
          pending_deep_ask: data.pending_deep_ask || (Array.isArray(data.pending) ? data.pending.find((x) => String(x?.flow || "") === "deep_alignment") : null),
          force_latest: true,
        });
        removeDeepCard(deepActiveCard);
        mountDeepAlignmentCard(deepPendingPayload, deepLoadingBubble);
        setPendingConfirmFromList(Array.isArray(data.pending) ? data.pending : []);
        return;
      }
      deepPendingPayload = normalizeDeepPayload({
        ...payload,
        alignment_text: String(data.alignment_text || ""),
        rounds: Array.isArray(data.rounds) ? data.rounds : [],
        active_index: Number(data.active_index || 0),
        can_back: !!data.can_back,
        pending_deep_ask: data.pending_deep_ask || null,
        force_latest: true,
      });
      mountDeepAlignmentCard(deepPendingPayload, deepLoadingBubble);
    } catch (e) {
      reportErrorToTerminal(`Deep 对齐失败：${String(e?.message || e)}`, { source: "deep_alignment", sessionId: payload.sid });
      if (deepLoadingBubble?.bubble?.isConnected) {
        window.ChatBubbleRenderer?.updateBubbleMarkdown?.(deepLoadingBubble.bubble, "Deep 对齐失败，请检查模型服务或关闭 Deep 后重试。");
      } else {
        addBubble("Deep 对齐失败，请检查模型服务或关闭 Deep 后重试。", "bot");
      }
      if (payload.createdForThisMessage && String(getStoredSessionId() || "") === payload.sid) {
        clearActiveSessionState({ title: "新会话" });
      }
    } finally {
      deepAlignmentBusy = false;
      setMessageSendInFlight(false);
      inputEl.focus();
      removeFile();
    }
  }

  async function restoreDeepAlignmentCard(sid) {
    const safeSid = String(sid || "").trim();
    if (!safeSid || isDraftSessionId(safeSid)) return false;
    try {
      const res = await apiFetch(`/sessions/${encodeURIComponent(safeSid)}/deep`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data?.ok === false || !data?.active) return false;
      removeDeepCard(deepActiveCard);
      deepPendingPayload = normalizeDeepPayload({
        sid: safeSid,
        message: String(data.original_message || ""),
        file_names: Array.isArray(data.file_names) ? data.file_names : [],
        file_contents: Array.isArray(data.file_contents) ? data.file_contents : [],
        alignment_text: String(data.alignment_text || ""),
        rounds: Array.isArray(data.rounds) ? data.rounds : [],
        active_index: Number(data.active_index || 0),
        can_back: !!data.can_back,
        pending_deep_ask: data.pending_deep_ask || null,
        force_latest: true,
      });
      clearEmptyState();
      if (deepPendingPayload.file_names.length > 0) {
        deepPendingPayload.file_names.forEach(function(name) { if (name) addFileChipBubble(name); });
      }
      if (deepPendingPayload.message) {
        addBubble(deepPendingPayload.message, "user");
      }
      renderDeepAlignmentCard(deepPendingPayload);
      return true;
    } catch (e) {
      reportErrorToTerminal(`恢复 Deep 对齐状态失败：${String(e?.message || e)}`, {
        source: "deep_alignment",
        sessionId: safeSid,
        persist: false,
      });
      return false;
    }
  }

  async function showVersionTip() {
    clearEmptyState();
    const localDisplay = getVersionBadgeText();
    const localNorm = normalizeVersionTag(localDisplay);
    const signText = appVersionSignatureId ? `，签名${appVersionSignatureId}` : "";
    const verifyText = appVersionSignatureId ? (appVersionVerified ? "，签名已验证" : "，签名未验证") : "";
    addSystemNotice(`当前版本${localDisplay}${signText}${verifyText}，正在检查最新版本...`);

    let latestVersion = "";
    let remoteError = "";
    try {
      const res = await apiFetch("/system/versions", { method: "GET" });
      const data = res.ok ? await res.json() : {};
      if (!res.ok) {
        remoteError = String(data?.error || `HTTP ${res.status}`);
      } else {
        const latestVerified = String(data?.latest_verified?.version || "").trim();
        const remoteRows = Array.isArray(data?.remote_versions) ? data.remote_versions : [];
        const firstRemote = String(remoteRows[0]?.version || "").trim();
        latestVersion = latestVerified || firstRemote;
        if (!latestVersion && data?.remote_ok === false) {
          remoteError = String(data?.error || "远端版本不可用");
        }
      }
    } catch (e) {
      remoteError = String(e?.message || e);
    }

    if (remoteError) {
      addSystemNotice(`当前版本${localDisplay}${signText}${verifyText}，最新版本检查失败：${remoteError}`);
      return;
    }

    const latestNorm = normalizeVersionTag(latestVersion);
    if (!latestNorm) {
      addSystemNotice(`当前版本${localDisplay}${signText}${verifyText}，暂未获取到远端版本。`);
      return;
    }

    const cmp = compareVersionTag(localNorm, latestNorm);
    const latestText = `v${latestNorm}`;
    if (cmp >= 0) {
      addSystemNotice(`当前版本${localDisplay}${signText}${verifyText}，已是最新（远端最新 ${latestText}）。`);
    } else {
      addSystemNotice(`当前版本${localDisplay}${signText}${verifyText}，发现新版本 ${latestText}。前往 <a href="/home" target="_blank">版本管理</a> 安装。`);
    }
  }
