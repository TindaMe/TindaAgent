/**
 * Extracted from chat.html: chat_ui_panels.js
 */

  // ── 快捷按钮 ──
  const QUICK_BUTTON_DEFS = {
    model: {
      label: "切换模型", icon: '<svg class="icon" viewBox="0 0 24 24"><path d="M9 3h6"/><path d="M12 3v4"/><rect x="5" y="7" width="14" height="14" rx="3"/><path d="M9 12h.01M15 12h.01M9 16h6"/></svg>',
      render: () => {
        const wrap = document.createElement("div"); wrap.className = "model-wrap";
        const btn = document.createElement("button"); btn.className = "icon-btn model-btn"; btn.title = "切换模型"; btn.setAttribute("data-tip","切换模型"); btn.setAttribute("aria-label","切换模型");
        btn.id = "modelSwitchBtn"; btn.innerHTML = QUICK_BUTTON_DEFS.model.icon;
        const panel = document.createElement("div"); panel.id = "modelPanel"; panel.className = "model-panel";
        wrap.appendChild(btn); wrap.appendChild(panel);
        btn.addEventListener("click", toggleModelPanel);
        return wrap;
      }
    },
    stream: {
      label: "流式输出", icon: '<svg class="icon stream-icon stream-off" viewBox="0 0 24 24"><path d="M4 5h16"/><path d="M4 12h12"/><path d="M4 19h8"/></svg><svg class="icon stream-icon stream-on" viewBox="0 0 24 24" style="display:none;"><path d="M3 12h4l2 3 3-6 3 6 2-3h4"/></svg>',
      render: () => {
        const btn = document.createElement("button"); btn.className = "icon-btn stream-btn"; btn.title = "流式输出"; btn.setAttribute("data-tip","切换流式输出"); btn.setAttribute("aria-label","流式输出");
        btn.id = "streamToggleBtn"; btn.innerHTML = QUICK_BUTTON_DEFS.stream.icon;
        btn.addEventListener("click", toggleStream);
        return btn;
      }
    },
    terminal: {
      label: "终端", icon: '<svg class="icon" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="14" rx="2"/><path d="M8 20h8"/></svg>',
      render: () => {
        const btn = document.createElement("button"); btn.className = "icon-btn term-toggle"; btn.title = "打开终端"; btn.setAttribute("data-tip","打开终端"); btn.setAttribute("aria-label","打开终端");
        btn.id = "termToggleBtn"; btn.innerHTML = QUICK_BUTTON_DEFS.terminal.icon;
        btn.addEventListener("click", toggleTerm);
        return btn;
      }
    },
    compress: {
      label: "压缩上下文", icon: '<svg class="icon" viewBox="0 0 24 24"><path d="M4 6h16"/><path d="M4 12h10"/><path d="M4 18h7"/><path d="M18 10v8"/><path d="M14 14h8"/></svg>',
      render: () => {
        const btn = document.createElement("button"); btn.className = "icon-btn"; btn.title = "压缩上下文"; btn.setAttribute("data-tip","压缩上下文"); btn.setAttribute("aria-label","压缩上下文");
        btn.id = "compressCtxBtn"; btn.innerHTML = QUICK_BUTTON_DEFS.compress.icon;
        btn.addEventListener("click", compressCurrentContext);
        return btn;
      }
    },
    sessions: {
      label: "会话管理", icon: '<svg class="icon" viewBox="0 0 24 24"><path d="M4 5h12a2 2 0 0 1 2 2v12H6a2 2 0 0 1-2-2z"/><path d="M8 9h6M8 13h6"/><path d="M18 7h2v12h-2"/></svg>',
      render: () => {
        const btn = document.createElement("button"); btn.className = "icon-btn"; btn.title = "会话管理"; btn.setAttribute("data-tip","会话管理"); btn.setAttribute("aria-label","会话管理");
        btn.id = "recordsToggleBtn"; btn.innerHTML = QUICK_BUTTON_DEFS.sessions.icon;
        btn.addEventListener("click", toggleRecordsPanel);
        return btn;
      }
    },
    reset: {
      label: "清空上下文", icon: '<svg class="icon" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v5h-5"/></svg>',
      render: () => {
        const btn = document.createElement("button"); btn.className = "icon-btn"; btn.title = "清空上下文"; btn.setAttribute("data-tip","清空当前会话上下文"); btn.setAttribute("aria-label","清空上下文");
        btn.innerHTML = QUICK_BUTTON_DEFS.reset.icon;
        btn.addEventListener("click", resetChat);
        return btn;
      }
    },
    diagnostics: {
      label: "模型检测", icon: '<svg class="icon" viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M7 15l3-3 2 2 4-4"/><path d="M7 10h3"/></svg>',
      render: () => {
        const a = document.createElement("a"); a.className = "icon-btn"; a.href = "/model-diagnostics"; a.title = "模型检测"; a.setAttribute("data-tip","模型检测"); a.setAttribute("aria-label","模型检测");
        a.onclick = function(e){ e.preventDefault(); smoothNavigate("/model-diagnostics"); };
        a.id = "modelDiagnosticsBtn"; a.innerHTML = QUICK_BUTTON_DEFS.diagnostics.icon;
        return a;
      }
    },
    logs: {
      label: "日志查看", icon: '<svg class="icon" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 9h8M8 13h8M8 17h6"/></svg>',
      render: () => {
        const a = document.createElement("a"); a.className = "icon-btn"; a.href = "/logs"; a.title = "日志查看"; a.setAttribute("data-tip","日志查看"); a.setAttribute("aria-label","日志查看");
        a.onclick = function(e){ e.preventDefault(); smoothNavigate("/logs"); };
        a.innerHTML = QUICK_BUTTON_DEFS.logs.icon;
        return a;
      }
    },
    llm_request: {
      label: "模型数据", icon: '<svg class="icon" viewBox="0 0 24 24"><path d="M8 4h8l4 4v12H4V4z"/><path d="M16 4v4h4"/><path d="M8 12h8M8 16h6"/></svg>',
      render: () => {
        const a = document.createElement("a"); a.className = "icon-btn"; a.href = "/model-data"; a.title = "模型数据面板"; a.setAttribute("data-tip","模型数据面板"); a.setAttribute("aria-label","模型数据面板");
        a.onclick = function(e){ e.preventDefault(); smoothNavigate("/model-data"); };
        a.innerHTML = QUICK_BUTTON_DEFS.llm_request.icon;
        return a;
      }
    },
    admin: {
      label: "用户管理", icon: '<svg class="icon" viewBox="0 0 24 24"><path d="M12 3l7 3v6c0 4.2-2.9 7.8-7 8.8-4.1-1-7-4.6-7-8.8V6z"/><path d="M9.5 12.5l1.8 1.8 3.4-3.4"/></svg>',
      render: () => {
        const a = document.createElement("a"); a.className = "icon-btn"; a.href = "/user-admin"; a.title = "用户管理"; a.setAttribute("data-tip","用户管理"); a.setAttribute("aria-label","用户管理");
        a.onclick = function(e){ e.preventDefault(); smoothNavigate("/user-admin"); };
        a.id = "adminLinkBtn"; a.innerHTML = QUICK_BUTTON_DEFS.admin.icon;
        return a;
      }
    }
  };

  function getQuickSettings() {
    const source = webSettingsCache || readLocalWebSettings();
    return normalizeQuickButtonKeys(source?.quick_buttons);
  }

  function renderQuickButtons() {
    if (!quickBtnsEl) return;
    quickBtnsEl.innerHTML = "";
    const keys = getQuickSettings();
    keys.forEach(key => {
      const def = QUICK_BUTTON_DEFS[key];
      if (!def) return;
      // admin button only for admin users
      if (key === "admin") {
        const p = userMeta.perm;
        if (!Number.isFinite(p) || ((p & 511) !== 511)) return;
      }
      // diagnostics only for LLM perm users
      if (key === "diagnostics") {
        const p = userMeta.perm;
        if (!Number.isFinite(p) || ((p & 4) !== 4)) return;
      }
      const el = def.render();
      if (el) {
        quickBtnsEl.appendChild(el);
      }
    });
    Array.from(quickBtnsEl.children).forEach((el, index, all) => {
      el.style.setProperty("--quick-enter-index", String(3 + all.length - 1 - index));
    });
    // show/hide separator
    if (quickSepEl) quickSepEl.style.display = quickBtnsEl.children.length > 0 ? "" : "none";
    // rebind global refs to newly created elements
    const newModel = document.getElementById("modelSwitchBtn");
    const newPanel = document.getElementById("modelPanel");
    const newStream = document.getElementById("streamToggleBtn");
    const newTerm = document.getElementById("termToggleBtn");
    const newRecords = document.getElementById("recordsToggleBtn");
    const newDiagnostics = document.getElementById("modelDiagnosticsBtn");
    const newAdmin = document.getElementById("adminLinkBtn");
    const newCompress = document.getElementById("compressCtxBtn");
    if (newModel) modelSwitchBtnEl = newModel;
    if (newPanel) modelPanelEl = newPanel;
    if (newStream) window.streamToggleBtnEl = newStream;
    if (newTerm) window.termToggleBtnEl = newTerm;
    if (newRecords) window.recordsToggleBtnEl = newRecords;
    if (newDiagnostics) modelDiagnosticsBtnEl = newDiagnostics;
    if (newAdmin) adminLinkBtnEl = newAdmin;
    // update stream button state
    if (newStream) {
      const offIcon = newStream.querySelector(".stream-off");
      const onIcon = newStream.querySelector(".stream-on");
      if (streamEnabled) { newStream.classList.add("on"); if (offIcon) offIcon.style.display = "none"; if (onIcon) onIcon.style.display = ""; }
      else { newStream.classList.remove("on"); if (offIcon) offIcon.style.display = ""; if (onIcon) onIcon.style.display = "none"; }
    }
  }

  // ── 账户切换 Popup ──
  async function loadAccountList() {
    if (!accountListEl) return;
    try {
      const res = await apiFetch("/auth/local-users");
      const data = await res.json();
      window.__headerUsersCache = Array.isArray(data?.users) ? data.users : [];
      renderAccountList(window.__headerUsersCache, data?.current_uid || userMeta.uid);
    } catch {
      window.__headerUsersCache = [];
      renderAccountList([], "");
    }
  }

  function renderAccountList(users, currentUid) {
    if (!accountListEl) return;
    accountListEl.innerHTML = "";
    if (!Array.isArray(users) || users.length === 0) {
      accountListEl.innerHTML = '<div style="font-size:12px;color:var(--text-dim);padding:8px;">无可用账户</div>';
      return;
    }
    users.forEach(u => {
      const uid = String(u?.uid || "");
      const name = String(u?.name || "-");
      const active = uid === String(currentUid || "");
      const div = document.createElement("div");
      div.className = "account-item" + (active ? " active" : "");
      div.innerHTML = `<span class="dot ${active ? "on" : "off"}"></span><span>${name} (${uid})</span>`;
      div.addEventListener("click", () => switchToAccount(uid));
      accountListEl.appendChild(div);
    });
  }

  async function switchToAccount(uid) {
    if (!uid || uid === String(userMeta.uid || "")) { hideAccountPopup(); return; }
    try {
      stopToolPolling();
      clearPendingConfirm();
      const res = await apiFetch("/auth/local-login", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({uid}),
      });
      const data = await res.json();
      if (!res.ok || data?.ok === false) throw new Error(String(data?.error || `HTTP ${res.status}`));
      const token = String(data?.token || "").trim();
      if (!token) throw new Error("本机登录未返回 token");
      setAuthToken(token);
      applyUserMeta(data?.user || {});
      const sid = await restoreExistingSessionForBoot();
      if (sid) {
        await loadCurrentSessionRecord({ sessionId: sid });
        startToolPolling(sid);
      } else {
        enterDraftSession({ title: "新会话", clearTerminal: true });
      }
      showToast("账户已切换");
      hideAccountPopup();
      await loadAccountList();
      renderQuickButtons();
      if (!adminLinkBtnEl || adminLinkBtnEl.style.display === "none") { if (location.pathname === "/user-admin") location.href = "/"; }
    } catch(e) {
      showToast(`切换失败：${String(e?.message||e)}`, "error");
    }
  }

  function showAccountPopup() {
    if (!accountPopupEl) return;
    loadAccountList();
    openAnimatedPanel(accountPopupEl, "account");
  }
  async function hideAccountPopup() {
    if (!accountPopupEl) return;
    await closeAnimatedPanel(accountPopupEl, "account", "show", { timeout: 460 });
  }


  async function pollToolEventsOnce(sessionId) {
    const sid = String(sessionId || getSessionId()).trim();
    if (!sid) return;
    if (toolPollInFlight) return;
    toolPollInFlight = true;
    try {
      const params = new URLSearchParams({ after_seq: String(toolLastSeq), limit: "200" });
      const res = await apiFetch(`/sessions/${encodeURIComponent(sid)}/tool-events?${params.toString()}`);
      if (!res.ok) {
        if (res.status === 401 || res.status === 403 || res.status === 404) {
          stopToolPolling();
          if (sid === getStoredSessionId()) {
            clearStoredSessionId();
            activeSessionTitle = "无会话";
            statusContextUsageLength = 0;
            statusContextPreviousUsageLength = 0;
            renderHeaderStatus();
          }
          if (res.status === 403 || res.status === 404) {
            showToast("当前会话不可访问，已停止工具事件轮询", "warn");
          }
          return;
        }
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
      const events = Array.isArray(data.events) ? data.events : [];
      if (typeof data.next_seq === "number") toolLastSeq = data.next_seq;
      // 拉取恢复后清空错误抖动计数
      toolEventsFetchErrorStreak = 0;
      toolEventsLastErrorAt = 0;
      if (!events.length) return;
      showTerm();
      appendTerminalEvents(events);
    } catch (e) {
      // 网络抖动/切页瞬断时不每秒刷红错误；仅在持续失败时节流提示
      toolEventsFetchErrorStreak += 1;
      const now = Date.now();
      const message = String(e?.message || e);
      const isTransient = /Failed to fetch|NetworkError|Load failed|AbortError/i.test(message);
      const isFirstPersistentTransient = isTransient && toolEventsFetchErrorStreak === 3;
      const reachedInterval = toolEventsLastErrorAt === 0 || now - toolEventsLastErrorAt >= TOOL_EVENTS_ERROR_REPORT_INTERVAL_MS;
      const shouldReport =
        !isTransient
        || isFirstPersistentTransient
        || reachedInterval;
      if (shouldReport) {
        toolEventsLastErrorAt = now;
        reportErrorToTerminal(`工具事件读取失败：${message}`, { source: "tool_events", sessionId: sid, persist: false });
      }
      if (isTransient && toolEventsFetchErrorStreak >= TOOL_EVENTS_ERROR_AUTO_PAUSE_STREAK && !toolPollPausedByError) {
        toolPollPausedByError = true;
        stopToolPolling();
        reportErrorToTerminal("工具事件通道连续失败，已自动暂停轮询。发送新消息后会自动恢复。", {
          source: "tool_events",
          sessionId: sid,
          persist: false,
        });
        showToast("工具事件通道已自动暂停（连续失败）", "warn");
      }
    } finally {
      toolPollInFlight = false;
    }
  }

  function stopToolPolling() {
    if (toolPollTimer) {
      clearInterval(toolPollTimer);
      toolPollTimer = 0;
    }
    flushTermPersistBuffer();
    toolPollInFlight = false;
    toolEventsFetchErrorStreak = 0;
    toolEventsLastErrorAt = 0;
  }

  function startToolPolling(sessionId) {
    stopToolPolling();
    toolPollPausedByError = false;
    const sid = String(sessionId || getSessionId()).trim();
    if (!sid || isDraftSessionId(sid)) return;
    pollToolEventsOnce(sid);
    toolPollTimer = setInterval(() => {
      pollToolEventsOnce(sid);
    }, 1400);
  }

  async function compressCurrentContext() {
    const sid = String(getSessionId() || "").trim();
    if (!sid || isDraftSessionId(sid)) {
      showToast("当前是草稿会话，发送消息后再压缩上下文", "warn");
      return;
    }
    try {
      const res = await apiFetch(`/sessions/${encodeURIComponent(sid)}/compress`, { method: "POST" });
      const data = await res.json();
      if (!res.ok || data?.ok === false) {
        throw new Error(String(data?.error || `http ${res.status}`));
      }
      const usageBefore = normalizeStatusCount(data?.usage_before);
      const usageAfter = normalizeStatusCount(data?.usage_after);
      const serverLimit = normalizeStatusCount(data?.max_context_tokens);
      if (serverLimit >= 100) statusContextTokenLimit = serverLimit;
      if (usageBefore > 0) statusContextPreviousUsageLength = usageBefore;
      if (usageAfter >= 0) statusContextUsageLength = usageAfter;
      renderHeaderStatus();
      addSystemNotice(`上下文已压缩：压缩 ${Number(data?.compressed_count || 0)} 条历史消息`);
      await refreshRecords();
      refreshContextUsageLength(sid).catch(() => {});
    } catch (e) {
      reportErrorToTerminal(`压缩失败：${String(e?.message || e)}`, { source: "compress", sessionId: sid });
      addSystemNotice(`压缩失败：${String(e?.message || e)}`);
    }
  }

  function showConfigDialog() {
    const el = document.getElementById("configOverlay");
    if (!el) return;
    document.getElementById("configTokenInput").value = String(getContextTokenLimit());
    openAnimatedPanel(el, "config");
  }

  async function hideConfigDialog() {
    const el = document.getElementById("configOverlay");
    if (el) await closeAnimatedPanel(el, "config", "show", { timeout: 460 });
  }

  async function saveConfig() {
    const input = document.getElementById("configTokenInput");
    const val = parseContextTokenLimitInput(input?.value);
    if (!val) {
      showToast(`上下文阈值范围为 ${CONTEXT_TOKEN_MIN} ~ ${CONTEXT_TOKEN_MAX} tokens`, "warn");
      if (input) input.value = String(getContextTokenLimit());
      return;
    }
    if (input) input.value = String(val);
    sessionStorage.setItem("tinda_max_context_tokens", String(val));
    await saveWebSettingsPatch({ token_limit: val });
    const sid = getSessionId();
    if (sid && !isDraftSessionId(sid)) {
      try {
        const synced = await syncContextTokenLimitForSession(sid);
        if (!synced) throw new Error("session config sync failed");
        showToast(`上下文阈值已设为 ${val} tokens`, "ok");
      } catch (e) {
        showToast(`保存失败：${String(e?.message || e)}`, "error");
      }
    } else {
      showToast(`阈值已设为 ${val} tokens（下次会话生效）`, "info");
    }
    hideConfigDialog();
  }
