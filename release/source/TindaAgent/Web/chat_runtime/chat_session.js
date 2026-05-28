/**
 * Extracted from chat.html: chat_session.js
 */

  // --- Resize drag ---
  resizerEl.addEventListener("mousedown", (e) => {
    if (!termOpen) return;
    isResizing   = true;
    resizeStartX = e.clientX;
    resizeStartW = termPanelEl.offsetWidth;
    document.body.style.cursor     = "col-resize";
    document.body.style.userSelect = "none";
    resizerEl.classList.add("dragging");
    e.preventDefault();
  });

  document.addEventListener("mousemove", (e) => {
    if (!isResizing || !termOpen) return;
    const dx   = resizeStartX - e.clientX;
    const newW = Math.min(Math.max(resizeStartW + dx, 180), window.innerWidth * 0.65);
    termWidth = newW;
    termPanelEl.style.transition = "none";
    termPanelEl.style.width      = newW + "px";
    scheduleRecalcPinnedSpacerHeight();
  });

  document.addEventListener("mouseup", () => {
    if (!isResizing) return;
    isResizing = false;
    document.body.style.cursor     = "";
    document.body.style.userSelect = "";
    resizerEl.classList.remove("dragging");
    termPanelEl.style.transition = "";
    localStorage.setItem(TERM_WIDTH_KEY, String(Math.round(termWidth)));
    scheduleRecalcPinnedSpacerHeight();
  });

  // --- Session handling ---
  function genSessionId() {
    return "s_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 10);
  }
  function genClientTurnId() {
    return "turn_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 10);
  }
  function genDraftSessionId() {
    return DRAFT_SESSION_PREFIX + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 8);
  }
  function getSessionId() {
    return getStoredSessionId();
  }
  function rotateSessionId() {
    const sid = genSessionId();
    setStoredSessionId(sid);
    return sid;
  }

  function enterDraftSession({ title = "新会话", clearTerminal = true } = {}) {
    clearActiveSessionState({ title });
    clearChatSessionView({ showEmpty: true, clearTerminal });
    activeSessionTitle = title;
    renderHeaderStatus();
  }

  function createDraftSessionId() {
    const sid = genDraftSessionId();
    setStoredSessionId(sid);
    activeSessionTitle = "新会话";
    statusContextUsageLength = 0;
    statusContextPreviousUsageLength = 0;
    renderHeaderStatus();
    return sid;
  }

  function clearChatSessionView({ showEmpty = true, clearTerminal = true } = {}) {
    stopToolPolling();
    toolLastSeq = 0;
    processedTerminalSeq.clear();
    toolPollPausedByError = false;
    toolPollInFlight = false;
    resetStageActive = false;
    clearPinnedSpacer();
    resetAssistantTurnBubbleMap();
    clearPendingConfirm();
    hydratedSessionEntries = [];
    historyPagingState = { sid: "", oldestSeq: 0, hasMore: false, loading: false };
    if (messagesEl) {
      delete messagesEl.dataset.hydrating;
      messagesEl.innerHTML = "";
    }
    if (clearTerminal) clearTerm();
    if (showEmpty) showEmptyState();
    if (messagesWrap) messagesWrap.scrollTop = 0;
  }

  function clearActiveSessionState({ title = "无会话" } = {}) {
    clearStoredSessionId();
    apiFetch("/web-settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ last_session_id: "" }),
    }).catch(() => {});
    activeSessionTitle = title;
    statusContextUsageLength = 0;
    statusContextPreviousUsageLength = 0;
    renderHeaderStatus();
  }

  async function withChatSessionLoading(action, { keepRecordsPanel = false } = {}) {
    const shouldReopenRecords = !!keepRecordsPanel && !!recordsPanelOpen;
    const startedAt = performance.now();
    startChatSessionLoading();
    await sleep(60);
    try {
      if (typeof action === "function") {
        await action();
      }
    } finally {
      await releaseChatSessionLoading(startedAt);
      if (shouldReopenRecords) openRecordsPanel();
    }
  }

  async function ensureActiveSession() {
    let sid = getStoredSessionId();
    if (!sid || isDraftSessionId(sid)) {
      sid = createDraftSessionId();
    }
    return sid;
  }

  async function restoreExistingSessionForBoot() {
    let sid = getStoredSessionId();
    if (isDraftSessionId(sid)) {
      clearStoredSessionId();
      sid = "";
    }

    let restoreEnabled = false;
    let serverLastSid = "";
    try {
      const wr = await apiFetch("/web-settings");
      if (wr.ok) {
        const ws = await wr.json();
        restoreEnabled = !!ws?.restore_last_session;
        serverLastSid = String(ws?.last_session_id || "").trim();
      }
    } catch {}

    const candidate = restoreEnabled && serverLastSid ? serverLastSid : sid;
    if (!candidate) return "";

    try {
      const res = await apiFetch("/sessions?limit=500&offset=0");
      if (!res.ok) return "";
      const data = await res.json();
      const list = Array.isArray(data?.sessions) ? data.sessions : [];
      const found = list.find((x) => String(x?.id || "") === candidate);
      if (!found) {
        if (candidate === sid) clearStoredSessionId();
        return "";
      }
      setActiveSession(candidate, { title: String(found.title || "新对话"), silent: true });
      return candidate;
    } catch {
      return "";
    }
  }

  async function ensureSessionForUserMessage() {
    const existing = String(getStoredSessionId() || "").trim();
    if (existing && !isDraftSessionId(existing)) {
      try {
        const res = await apiFetch(`/sessions/${encodeURIComponent(existing)}/messages?limit=1`, {
          method: "GET",
          retryOnNetworkError: false,
        });
        if (res.ok) return existing;
        if (res.status !== 404 && res.status !== 403) return existing;
      } catch {
        return existing;
      }
      clearStoredSessionId();
    }

    const sid = genSessionId();
    setActiveSession(sid, { title: "新对话", silent: true, skipContextUsage: true });
    return sid;
  }

  function setActiveSession(sessionId, { title = "新对话", silent = false, skipContextUsage = false } = {}) {
    const sid = String(sessionId || "").trim();
    if (!sid) return;
    setStoredSessionId(sid);
    // Notify server of last session for restore-later feature
    apiFetch("/web-settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ last_session_id: sid }),
    }).catch(() => {});
    activeSessionTitle = String(title || "新对话");
    renderHeaderStatus();
    if (!skipContextUsage && !isDraftSessionId(sid)) refreshContextUsageLength(sid).catch(() => {});
    if (!silent) showToast(`已切换会话：${activeSessionTitle}`);
    toolLastSeq = 0;
    processedTerminalSeq.clear();
    clearPendingConfirm();
  }

  async function createSessionAndSwitch(title = "新对话", { silent = false } = {}) {
    const res = await apiFetch("/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    const data = await res.json();
    if (!res.ok || !data?.session?.id) {
      throw new Error(String(data?.error || `http ${res.status}`));
    }
    setActiveSession(data.session.id, { title: data.session.title || title, silent });
    return data.session;
  }

  function normalizeEntryForPersist(raw) {
    if (!raw || typeof raw !== "object") return null;
    const role = String(raw.role || "").trim();
    if (role !== "user" && role !== "assistant" && role !== "system") return null;
    const content = String(raw.content ?? "");
    if (!content.trim()) return null;
    const entry = { role, content, ts: String(raw.ts || toIsoWithOffset(new Date())) };
    if (raw.type) entry.type = String(raw.type);
    if (raw.display_target) entry.display_target = String(raw.display_target);
    if (raw.context_policy) entry.context_policy = String(raw.context_policy);
    if (raw.entry_type) entry.entry_type = String(raw.entry_type);
    if (raw.terminal_kind) entry.terminal_kind = String(raw.terminal_kind);
    if (raw.terminal_class) entry.terminal_class = String(raw.terminal_class);
    return entry;
  }

  function enqueueSessionEntries(entries, { sessionId = getSessionId() } = {}) {
    const sid = String(sessionId || getSessionId()).trim();
    if (!sid || !Array.isArray(entries) || entries.length === 0) return;
    const payloadEntries = entries
      .map(normalizeEntryForPersist)
      .filter((x) => !!x);
    if (payloadEntries.length === 0) return;

    sessionPersistQueue = sessionPersistQueue
      .then(async () => {
        try {
          await apiFetch("/session/events", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              session_id: sid,
              entries: payloadEntries,
            }),
          });
        } catch (e) {
          reportErrorToTerminal(`会话事件写入失败：${String(e?.message || e)}`, {
            source: "session_events",
            sessionId: sid,
            persist: false,
          });
        }
      })
      .catch(() => {
        // keep queue alive after an upstream failure
      });
  }

  function normalizeModelChoice(raw) {
    const key = String(raw || "").trim().toLowerCase();
    return MODEL_ALIAS_MAP[key] || String(raw || "").trim();
  }

  function getModelLabel(modelId) {
    const id = normalizeModelChoice(modelId) || String(modelId || "").trim();
    const item = modelChoices.find((x) => String(x?.id || "") === id);
    return item?.label || id || "unknown";
  }

  function setModelSwitchBtnUI() {
    const mb = document.getElementById("modelSwitchBtn");
    if (!mb) return;
    const label = getModelLabel(currentModel);
    mb.classList.toggle("on", !!currentModel);
    mb.title = `当前模型：${label}`;
    mb.setAttribute("data-tip", `当前模型：${label}`);
  }

  function renderModelPanel() {
    if (!modelPanelEl) return;
    const rows = modelChoices.length > 0 ? modelChoices : MODEL_CHOICES_FALLBACK;
    const html = rows.map((item) => {
      const modelId = String(item?.id || "");
      const label = String(item?.label || modelId);
      const active = normalizeModelChoice(currentModel) === normalizeModelChoice(modelId);
      return `<button type="button" class="model-item${active ? " active" : ""}" data-model="${escapeHtml(modelId)}">${escapeHtml(label)}</button>`;
    }).join("");
    modelPanelEl.innerHTML = html || `<div class="records-empty">无可用模型</div>`;
    modelPanelEl.querySelectorAll(".model-item").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const modelId = btn.getAttribute("data-model") || "";
        if (!modelId) return;
        await switchModel(modelId);
      });
    });
  }

  async function closeModelPanel() {
    if (!modelPanelEl) return;
    await closeAnimatedPanel(modelPanelEl, "model", "open", { timeout: 360 });
  }

  function toggleModelPanel() {
    if (!modelPanelEl) return;
    const willOpen = !modelPanelEl.classList.contains("open");
    closeTimePanel();
    if (willOpen) {
      renderModelPanel();
      openAnimatedPanel(modelPanelEl, "model", "open");
    } else {
      closeModelPanel();
    }
  }

  async function loadModelInfo() {
    try {
      const res = await apiFetch("/model");
      if (!res.ok) throw new Error(`http ${res.status}`);
      const data = await res.json();
      currentModel = normalizeModelChoice(data?.current_model) || currentModel;
      if (Array.isArray(data?.available_models) && data.available_models.length > 0) {
        modelChoices = data.available_models
          .map((x) => ({
            id: normalizeModelChoice(x?.id) || normalizeModelChoice(x?.label) || String(x?.id || x?.label || "").trim(),
            label: String(x?.label || x?.id || "").trim(),
          }))
          .filter((x) => x.id && x.label);
      }
    } catch (e) {
      reportErrorToTerminal(`读取模型信息失败：${String(e?.message || e)}`, { source: "model_info" });
    }
    if (!currentModel) currentModel = "deepseek-v4-flash";
    if (!Array.isArray(modelChoices) || modelChoices.length === 0) {
      modelChoices = MODEL_CHOICES_FALLBACK.slice();
    }
    setModelSwitchBtnUI();
    renderModelPanel();
  }

  async function switchModel(modelId) {
    const target = normalizeModelChoice(modelId);
    if (!target) return;
    if (normalizeModelChoice(currentModel) === target) {
      closeModelPanel();
      return;
    }
    try {
      const res = await apiFetch("/model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: target }),
      });
      const data = await res.json();
      if (!res.ok || data?.ok === false) {
        throw new Error(String(data?.error || `http ${res.status}`));
      }
      currentModel = normalizeModelChoice(data?.current_model) || target;
      if (Array.isArray(data?.available_models) && data.available_models.length > 0) {
        modelChoices = data.available_models
          .map((x) => ({
            id: normalizeModelChoice(x?.id) || normalizeModelChoice(x?.label) || String(x?.id || x?.label || "").trim(),
            label: String(x?.label || x?.id || "").trim(),
          }))
          .filter((x) => x.id && x.label);
      }
      setModelSwitchBtnUI();
      renderModelPanel();
      closeModelPanel();
      showToast(`模型已切换：${getModelLabel(currentModel)}`);
    } catch (e) {
      reportErrorToTerminal(`模型切换失败：${String(e?.message || e)}`, { source: "model_switch" });
      showToast(`模型切换失败：${String(e?.message || e)}`, "err");
    }
  }

  function formatRecordTime(value) {
    const text = String(value || "").trim();
    if (!text) return "N/A";
    const dt = new Date(text);
    if (Number.isNaN(dt.getTime())) return text;
    return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")} ${String(dt.getHours()).padStart(2, "0")}:${String(dt.getMinutes()).padStart(2, "0")}`;
  }

  function renderRecordsList(rows) {
    if (!recordsListEl) return;
    if (!Array.isArray(rows) || rows.length === 0) {
      recordsListEl.innerHTML = `<div class="records-empty">暂无聊天记录</div>`;
      return;
    }
    recordsListEl.innerHTML = "";
    rows.forEach((item) => {
      const recordId = String(item?.id || item?.session_id || "");
      const messageCount = Number(item?.message_count || 0);
      const node = document.createElement("div");
      node.className = "record-item";
      if (recordId === getStoredSessionId()) node.classList.add("active");
      node.innerHTML = `
        <div class="record-item-title">${escapeHtml(String(item?.title || "新对话"))}</div>
        <div class="record-item-meta">
          session: ${escapeHtml(String(item?.id || item?.session_id || "N/A"))}<br/>
          updated: ${escapeHtml(formatRecordTime(item?.updated_at))}<br/>
          messages: ${messageCount}
        </div>
      `;
      const btn = document.createElement("button");
      btn.className = "record-item-btn";
      btn.type = "button";
      btn.textContent = "切换";
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await switchSessionFromPanel(recordId, String(item?.title || "新对话"));
        } finally {
          btn.disabled = false;
        }
      });
      node.appendChild(btn);
      recordsListEl.appendChild(node);
    });
  }

  async function switchSessionFromPanel(sessionId, title = "新对话") {
    const sid = String(sessionId || "").trim();
    if (!sid) return;
    try {
      await withChatSessionLoading(async () => {
        setActiveSession(sid, { title, silent: true });
        clearChatSessionView({ showEmpty: false, clearTerminal: true });
        await loadCurrentSessionRecord({ sessionId: sid });
        startToolPolling(sid);
        await refreshRecords();
      }, { keepRecordsPanel: true });
    } catch (e) {
      reportErrorToTerminal(`切换会话失败：${String(e?.message || e)}`, { source: "session_switch", sessionId: sid });
      addSystemNotice(`切换会话失败：${String(e?.message || e)}`, { persist: false });
    }
  }

  async function createSessionFromPanel() {
    if (creatingSessionFromPanel) return;
    creatingSessionFromPanel = true;
    const createBtn = recordsPanelEl?.querySelector(".records-actions button");
    if (createBtn) createBtn.disabled = true;
    try {
      await withChatSessionLoading(async () => {
        createDraftSessionId();
        clearChatSessionView({ showEmpty: true, clearTerminal: true });
        await refreshRecords();
      }, { keepRecordsPanel: true });
    } catch (e) {
      reportErrorToTerminal(`创建会话失败：${String(e?.message || e)}`, { source: "session_create" });
      addSystemNotice(`创建会话失败：${String(e?.message || e)}`, { persist: false });
    } finally {
      creatingSessionFromPanel = false;
      if (createBtn) createBtn.disabled = false;
    }
  }

  async function deleteCurrentSessionFromPanel() {
    const sid = String(getStoredSessionId() || "").trim();
    if (!sid) {
      await withChatSessionLoading(async () => {
        clearActiveSessionState();
        clearChatSessionView({ showEmpty: true, clearTerminal: true });
        await refreshRecords();
      }, { keepRecordsPanel: true });
      return;
    }
    try {
      await withChatSessionLoading(async () => {
        stopToolPolling();
        const res = await apiFetch(`/sessions/${encodeURIComponent(sid)}`, { method: "DELETE" });
        let data = {};
        const bodyText = await res.text();
        if (bodyText) {
          try {
            data = JSON.parse(bodyText);
          } catch {
            data = { ok: false, error: `HTTP ${res.status} ${bodyText.slice(0, 120)}` };
          }
        }
        if (!res.ok || data?.ok === false) {
          throw new Error(String(data?.error || `http ${res.status}`));
        }
        clearActiveSessionState();
        clearChatSessionView({ showEmpty: true, clearTerminal: true });
        await refreshRecords();
      }, { keepRecordsPanel: true });
    } catch (e) {
      reportErrorToTerminal(`删除会话失败：${String(e?.message || e)}`, { source: "session_delete" });
      addSystemNotice(`删除会话失败：${String(e?.message || e)}`, { persist: false });
    }
  }

  async function deleteAllSessionsFromPanel() {
    if (!confirm("确定要删除全部会话吗？此操作不可撤销。")) return;

    try {
      await withChatSessionLoading(async () => {
        stopToolPolling();
        const res = await apiFetch("/sessions", { method: "DELETE" });
        let data = {};
        const bodyText = await res.text();
        if (bodyText) {
          try {
            data = JSON.parse(bodyText);
          } catch {
            data = { ok: false, error: `HTTP ${res.status} ${bodyText.slice(0, 120)}` };
          }
        }
        if (!res.ok || data?.ok === false) {
          throw new Error(String(data?.error || `http ${res.status}`));
        }
        clearActiveSessionState();
        clearChatSessionView({ showEmpty: true, clearTerminal: true });
        await refreshRecords();
      }, { keepRecordsPanel: true });
    } catch (e) {
      reportErrorToTerminal(`删除全部会话失败：${String(e?.message || e)}`, { source: "session_delete_all" });
      addSystemNotice(`删除全部会话失败：${String(e?.message || e)}`, { persist: false });
    }
  }

  async function refreshRecords() {
    if (!recordsListEl) return;
    const keyword = String(recordsSearchInputEl?.value || "").trim();
    recordsListEl.innerHTML = `<div class="records-empty">加载中...</div>`;
    try {
      const params = new URLSearchParams({
        limit: String(RECORDS_PAGE_LIMIT),
        offset: "0",
      });
      const res = await apiFetch(`/sessions?${params.toString()}`);
      if (!res.ok) throw new Error(`http ${res.status}`);
      const data = await res.json();
      let rows = Array.isArray(data.sessions) ? data.sessions : [];
      if (keyword) {
        const kw = keyword.toLowerCase();
        rows = rows.filter((x) => String(x?.id || "").toLowerCase().includes(kw) || String(x?.title || "").toLowerCase().includes(kw));
      }
      sessionsCache = rows;
      renderRecordsList(rows);
    } catch (e) {
      reportErrorToTerminal(`读取记录失败：${String(e?.message || e)}`, { source: "records" });
      recordsListEl.innerHTML = `<div class="records-empty">读取记录失败</div>`;
    }
  }

	  function openRecordsPanel() {
	    recordsPanelClosing = false;
	    recordsPanelOpen = true;
	    recordsMaskEl?.classList.add("show");
	    recordsPanelEl?.classList.remove("closing");
	    recordsPanelEl?.classList.add("show");
	    if (recordsPanelEl) recordsPanelEl.setAttribute("aria-hidden", "false");
	    refreshRecords();
	  }

	  function waitForRecordsPanelExit() {
	    return waitForTransition(recordsPanelEl, { property: "transform", timeout: 620 });
	  }

	  async function closeRecordsPanel() {
	    if (recordsPanelClosing) return;
	    recordsPanelOpen = false;
	    recordsPanelClosing = true;
	    recordsPanelEl?.classList.add("closing");
	    recordsPanelEl?.classList.remove("show");
	    recordsMaskEl?.classList.remove("show");
	    await waitForRecordsPanelExit();
	    recordsPanelEl?.classList.remove("closing");
	    if (recordsPanelEl) recordsPanelEl.setAttribute("aria-hidden", "true");
	    recordsPanelClosing = false;
	  }

  function toggleRecordsPanel() {
    if (recordsPanelOpen) {
      closeRecordsPanel();
    } else {
      openRecordsPanel();
    }
  }

  function resetTerminalForReplay() {
    clearTerminalRenderQueue();
    closeTermFullCard();
    termFullOutputCache.clear();
    termRequestGroup = null;
    termBodyEl.innerHTML = "";
    termInitialized = false;
    initTermContent();
    termInitialized = true;
  }

  // renderSession is now defined in chat_renderer.js
  // Called by chat_renderer.js to display tool output in terminal
  function renderToolOutputToTerminal(toolName, callId, output, ok) {
    if (!output) return;
    var cls = ok ? "" : "err";
    addTermLine("cmd", "[" + toolName + " #" + callId + "]", cls, { persist: false, noScroll: true });
    addLimitedTermLines(output, cls, { persist: false, noScroll: true, title: `${toolName || "tool"} #${callId || ""}` });
    addTermSep({ persist: false, noScroll: true });
  }

  function appendTerminalCallOutRow(group, text, rawClass = "") {
    if (!group) return;
    appendTermOutputPreviewRows(group, [text], rawClass, { previewLimit: TERM_ENTRY_PREVIEW_LINES });
  }

  function ensureTerminalLooseGroup(frag) {
    let group = frag.lastElementChild;
    if (
      !group
      || !group.classList
      || !group.classList.contains("term-line")
      || String(group.dataset?.termKind || "") !== "out-group"
    ) {
      group = document.createElement("div");
      group.className = "term-line term-out-group";
      group.dataset.termKind = "out-group";
      frag.appendChild(group);
    }
    return group;
  }

  function appendTerminalLooseOutRow(frag, text, rawClass = "") {
    const group = ensureTerminalLooseGroup(frag);
    appendTermOutputPreviewRows(group, [text], rawClass, { previewLimit: TERM_ENTRY_PREVIEW_LINES });
  }

  function flushTerminalReplayOmissions(state) {
    if (!state) return;
    if (state.activeGroup && Array.isArray(state.activeGroupLines) && state.activeGroupLines.length > 0) {
      appendTermOutputPreviewRows(
        state.activeGroup,
        state.activeGroupLines,
        state.activeGroupClass || "",
        { title: "命令输出", previewLimit: TERM_ENTRY_PREVIEW_LINES },
      );
    } else if (!state.activeGroup && Array.isArray(state.looseLines) && state.looseLines.length > 0) {
      const group = ensureTerminalLooseGroup(state.frag);
      appendTermOutputPreviewRows(
        group,
        state.looseLines,
        state.looseClass || "",
        { title: "终端输出", previewLimit: TERM_ENTRY_PREVIEW_LINES },
      );
    }
    state.activeGroupLines = [];
    state.activeGroupClass = "";
    state.looseLines = [];
    state.looseClass = "";
  }

  function appendTerminalEventToFragment(ev, state) {
    if (!ev || typeof ev !== "object") return;
    const kind = String(ev.kind || ev.terminal_kind || "out");
    const text = String(ev.text ?? ev.content ?? "");
    const rawClass = String(ev.class || ev.terminal_class || "");
    const frag = state?.frag;
    if (!frag) return;
    if (kind === "cmd") {
      flushTerminalReplayOmissions(state);
      const group = document.createElement("div");
      group.className = "term-line term-call-group";
      group.dataset.termKind = "call-group";
      const row = document.createElement("div");
      row.className = "term-call-row cmd";
      row.innerHTML = `<span class="term-prompt">tinda@agent ~ % </span><span class="term-cmd">${escapeHtml(text)}</span>`;
      group.appendChild(row);
      frag.appendChild(group);
      state.activeGroup = group;
      state.activeGroupLines = [];
      state.activeGroupClass = "";
      state.looseLines = [];
      state.looseClass = "";
    } else if (kind === "sep") {
      flushTerminalReplayOmissions(state);
      state.activeGroup = null;
    } else if (state.activeGroup) {
      if (!Array.isArray(state.activeGroupLines)) state.activeGroupLines = [];
      if (!state.activeGroupClass && rawClass) state.activeGroupClass = rawClass;
      state.activeGroupLines.push(text);
    } else {
      if (!Array.isArray(state.looseLines)) state.looseLines = [];
      if (!state.looseClass && rawClass) state.looseClass = rawClass;
      state.looseLines.push(text);
    }
  }

  function appendTerminalEvents(events, { replay = false } = {}) {
    if (!Array.isArray(events) || !events.length) return 0;
    const rows = [];
    const state = {
      frag: document.createDocumentFragment(),
      activeGroup: null,
      activeGroupLines: [],
      activeGroupClass: "",
      looseLines: [],
      looseClass: "",
    };
    let appended = 0;
    events.forEach((ev) => {
      if (!ev || typeof ev !== "object") return;
      if (String(ev.display_target || "terminal") !== "terminal") return;
      if (String(ev.type || "terminal") !== "terminal") return;
      const seq = replay
        ? (String(ev.source || "") === "tool_runtime" ? Number(ev.source_seq || 0) : 0)
        : Number(ev.seq || ev.source_seq || 0);
      if (seq > 0) {
        if (processedTerminalSeq.has(seq)) return;
        processedTerminalSeq.add(seq);
        if (replay) toolLastSeq = Math.max(toolLastSeq, seq);
      }
      appendTerminalEventToFragment(ev, state);
      appended += 1;
    });
    flushTerminalReplayOmissions(state);
    if (appended > 0 && state.frag.childNodes.length > 0) {
      Array.from(state.frag.childNodes).forEach((node) => rows.push(node));
      rows.forEach((node) => appendTermNode(node, { topLevel: true, scroll: false }));
      queueTerminalDomFlush({ scroll: true });
    }
    return appended;
  }

  async function fetchSessionMessagesPage(sid, { beforeSeq = 0, limit = CHAT_INITIAL_MESSAGE_LIMIT } = {}) {
    const params = new URLSearchParams();
    const safeLimit = Math.max(1, Math.min(Number(limit) || CHAT_INITIAL_MESSAGE_LIMIT, 500));
    params.set("limit", String(safeLimit));
    if (beforeSeq > 0) params.set("before_seq", String(beforeSeq));
    const res = await apiFetch(`/sessions/${encodeURIComponent(sid)}/messages?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return {
      entries: Array.isArray(data.entries) ? data.entries : [],
      oldestSeq: Number(data.oldest_seq || 0),
      newestSeq: Number(data.newest_seq || 0),
      hasMore: !!data.has_more,
      total: Number(data.total || 0),
      plan: data && typeof data.plan === "object" ? data.plan : {},
    };
  }

  async function fetchSessionTerminalEvents(sid) {
    const params = new URLSearchParams({ limit: String(TERM_HISTORY_REPLAY_LIMIT) });
    const res = await apiFetch(`/sessions/${encodeURIComponent(sid)}/terminal?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const entries = Array.isArray(data.entries) ? data.entries : [];
    const omitted = Math.max(0, Number(data.omitted || 0));
    if (omitted > 0) {
      entries.unshift({
        type: "terminal",
        display_target: "terminal",
        kind: "out",
        class: "dim",
        content: `... 终端历史较长，仅加载最近 ${entries.length} 行，省略 ${omitted} 行`,
      });
    }
    return entries;
  }

  async function skipRunningToolCall(rawUrl) {
    const sid = String(getSessionId() || "").trim();
    if (!sid || isDraftSessionId(sid)) {
      showToast("当前没有可跳过的工具调用", "warn");
      return;
    }
    const raw = String(rawUrl || "");
    const query = raw.startsWith("toolskip:") ? raw.slice("toolskip:".length) : raw;
    const params = new URLSearchParams(query);
    const toolCallId = String(params.get("tool_call_id") || "").trim();
    const callId = String(params.get("call_id") || "").trim();
    const targetId = toolCallId || callId;
    if (!targetId) {
      showToast("工具调用 ID 缺失，无法跳过", "warn");
      return;
    }
    try {
      const res = await apiFetch(`/sessions/${encodeURIComponent(sid)}/tool-calls/${encodeURIComponent(targetId)}/skip`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ call_id: callId, tool_call_id: toolCallId }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data?.ok === false) throw new Error(String(data?.error || `HTTP ${res.status}`));
      showToast("已请求跳过当前工具", "ok");
    } catch (e) {
      showToast(`跳过失败：${String(e?.message || e)}`, "error");
    }
  }

  function removeHistoryLoadMoreControl() {
    messagesEl?.querySelectorAll(".history-load-more").forEach((node) => node.remove());
  }

  function renderHistoryLoadMoreControl() {
    removeHistoryLoadMoreControl();
    if (!messagesEl || !historyPagingState.hasMore || !historyPagingState.sid) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "history-load-more";
    btn.textContent = historyPagingState.loading ? "加载中..." : "加载更早消息";
    btn.disabled = !!historyPagingState.loading;
    btn.addEventListener("click", loadOlderHistoryPage);
    messagesEl.insertBefore(btn, messagesEl.firstElementChild || null);
  }

  async function renderHydratedSessionEntries(entries, { preserveScroll = false } = {}) {
    if (typeof renderSession === "function") {
      await renderSession(entries, {
        chunkSize: 16,
        preserveScroll,
      });
    }
    if (!preserveScroll) {
      const sid = String(historyPagingState.sid || getSessionId() || "").trim();
      const currentPlan = sid ? planCurrentBySession.get(sid) : null;
      if (!sid || planDeletedBySession.get(sid)) {
        clearPlanFloatContent();
        hidePlanFloat();
      } else if (currentPlan && typeof currentPlan === "object") {
        renderPlanFloat(currentPlan);
      } else {
        const hasPlan = showLatestPlanFromSessionEntries(entries);
        if (!hasPlan) {
          clearPlanFloatContent();
          hidePlanFloat();
        }
      }
    }
    renderHistoryLoadMoreControl();
  }

  async function loadOlderHistoryPage() {
    if (historyPagingState.loading || !historyPagingState.hasMore || !historyPagingState.sid) return;
    const sid = historyPagingState.sid;
    historyPagingState.loading = true;
    renderHistoryLoadMoreControl();
    try {
      const page = await fetchSessionMessagesPage(sid, {
        beforeSeq: historyPagingState.oldestSeq,
        limit: CHAT_HISTORY_PAGE_LIMIT,
      });
      if (sid !== historyPagingState.sid) return;
      const seen = new Set(hydratedSessionEntries.map((entry) => String(entry?.seq || entry?.id || "")));
      const older = page.entries.filter((entry) => !seen.has(String(entry?.seq || entry?.id || "")));
      hydratedSessionEntries = older.concat(hydratedSessionEntries);
      historyPagingState.oldestSeq = page.oldestSeq || historyPagingState.oldestSeq;
      historyPagingState.hasMore = page.hasMore;
      await renderHydratedSessionEntries(hydratedSessionEntries, { preserveScroll: true });
    } catch (e) {
      reportErrorToTerminal(`读取更早消息失败：${String(e?.message || e)}`, { source: "session_record", sessionId: sid });
      showToast("读取更早消息失败", "warn");
    } finally {
      if (sid === historyPagingState.sid) {
        historyPagingState.loading = false;
        renderHistoryLoadMoreControl();
      }
    }
  }

  async function loadCurrentSessionRecord({ sessionId = "" } = {}) {
    try {
      let sid = String(sessionId || "").trim();
      if (!sid || isDraftSessionId(sid)) {
        showEmptyState();
        clearPlanFloatContent();
        hidePlanFloat();
        return false;
      }
      const data = await fetchSessionMessagesPage(sid, { limit: CHAT_INITIAL_MESSAGE_LIMIT });
      if (sid !== String(getSessionId() || "").trim()) {
        return false;
      }
      const planMeta = data.plan && typeof data.plan === "object" ? data.plan : {};
      planDeletedBySession.set(sid, !!planMeta.deleted);
      if (planMeta.current && typeof planMeta.current === "object" && !planMeta.deleted) {
        planCurrentBySession.set(sid, planMeta.current);
      } else {
        planCurrentBySession.delete(sid);
      }
      hydratedSessionEntries = data.entries;
      historyPagingState = {
        sid,
        oldestSeq: data.oldestSeq,
        hasMore: data.hasMore,
        loading: false,
      };
      await renderHydratedSessionEntries(hydratedSessionEntries);
      resetTerminalForReplay();
      try {
        const terminalEntries = await fetchSessionTerminalEvents(sid);
        appendTerminalEvents(terminalEntries, { replay: true });
      } catch (terminalError) {
        reportErrorToTerminal(`读取终端历史失败：${String(terminalError?.message || terminalError)}`, {
          source: "session_terminal",
          sessionId: sid,
          persist: false,
        });
      }
      await syncPendingConfirmationsFromServer(sid, { silent: true });
      await restoreDeepAlignmentCard(sid);
      await refreshContextUsageLength(sid);
      return true;
    } catch (e) {
      reportErrorToTerminal(`读取当前会话记录失败：${String(e?.message || e)}`, { source: "session_record" });
      if (messagesEl && messagesEl.childElementCount === 0) showEmptyState();
      return false;
    }
  }
