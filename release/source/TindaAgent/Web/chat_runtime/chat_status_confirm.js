/**
 * Extracted from chat.html: chat_status_confirm.js
 */

  // --- Toast ---
  let toastTimer = 0;
  const chatBootStartedAt = performance.now();
  let initialChatRenderReleasing = false;
  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, Math.max(0, Number(ms) || 0)));
  }
  function startInitialChatLoading() {
    if (!document.body.classList.contains("chat-booting")) return;
    requestAnimationFrame(() => {
      document.body.classList.add("chat-loading-visible");
    });
  }
  async function releaseInitialChatRender() {
    if (!document.body.classList.contains("chat-booting")) return;
    if (initialChatRenderReleasing) return;
    initialChatRenderReleasing = true;
    const elapsed = performance.now() - chatBootStartedAt;
    await sleep(CHAT_BOOT_MIN_MS - elapsed);
    document.body.classList.remove("chat-loading-visible");
    document.body.classList.add("chat-loading-leaving");
    await sleep(CHAT_LOADING_FADE_MS);
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        document.body.classList.remove("chat-booting", "chat-loading-leaving");
        document.body.classList.add("chat-ready");
      });
    });
  }

  function startChatSessionLoading() {
    document.body.classList.remove("chat-ready", "chat-loading-leaving");
    document.body.classList.add("chat-booting");
    requestAnimationFrame(() => {
      document.body.classList.add("chat-loading-visible");
    });
  }

  async function releaseChatSessionLoading(startedAt = performance.now()) {
    if (!document.body.classList.contains("chat-booting")) return;
    const elapsed = performance.now() - startedAt;
    await sleep(CHAT_SESSION_LOADING_MIN_MS - elapsed);
    document.body.classList.remove("chat-loading-visible");
    document.body.classList.add("chat-loading-leaving");
    await sleep(CHAT_LOADING_FADE_MS);
    await new Promise((resolve) => {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          document.body.classList.remove("chat-booting", "chat-loading-leaving");
          document.body.classList.add("chat-ready");
          resolve();
        });
      });
    });
  }

  function showToast(text, variant = "info") {
    if (!toastEl) return;
    toastEl.textContent = text;
    toastEl.classList.remove("warn", "err");
    if (variant === "warn" || variant === "err") toastEl.classList.add(variant);
    // 重新触发动画
    toastEl.classList.remove("show");
    void toastEl.offsetWidth;
    toastEl.classList.add("show");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toastEl.classList.remove("show");
      toastTimer = 0;
    }, 2400);
  }

  function waitForTransition(node, { property = "transform", timeout = 360 } = {}) {
    return new Promise((resolve) => {
      if (!node) {
        resolve();
        return;
      }
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        node.removeEventListener("transitionend", onEnd);
        clearTimeout(timer);
        resolve();
      };
      const onEnd = (event) => {
        if (event.target !== node) return;
        if (property && event.propertyName !== property) return;
        finish();
      };
      const timer = setTimeout(finish, timeout);
      node.addEventListener("transitionend", onEnd);
    });
  }

  function openAnimatedPanel(node, closingKey, visibleClass = "show") {
    if (!node) return;
    if (closingKey && Object.prototype.hasOwnProperty.call(panelClosing, closingKey)) {
      panelClosing[closingKey] = false;
    }
    node.classList.remove("closing");
    node.classList.add(visibleClass);
    node.setAttribute("aria-hidden", "false");
  }

  async function closeAnimatedPanel(node, closingKey, visibleClass = "show", { timeout = 420 } = {}) {
    if (!node) return;
    if (!node.classList.contains(visibleClass) && !node.classList.contains("closing")) return;
    if (closingKey && panelClosing[closingKey]) return;
    if (closingKey && Object.prototype.hasOwnProperty.call(panelClosing, closingKey)) {
      panelClosing[closingKey] = true;
    }
    node.classList.add("closing");
    node.classList.remove(visibleClass);
    node.setAttribute("aria-hidden", "true");
    await waitForTransition(node, { property: "opacity", timeout });
    node.classList.remove("closing");
    if (closingKey && Object.prototype.hasOwnProperty.call(panelClosing, closingKey)) {
      panelClosing[closingKey] = false;
    }
  }

  function normalizeStatusCount(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n < 0) return 0;
    return Math.floor(n);
  }

  function renderHeaderStatus() {
    const onlineText = gatewayOnline ? "在线" : "离线";
    const sessionText = String(activeSessionTitle || "新会话").trim() || "新会话";
    const usageNow = normalizeStatusCount(statusContextUsageLength);
    const limit = normalizeStatusCount(statusContextTokenLimit) || getContextTokenLimit();
    const previous = normalizeStatusCount(statusContextPreviousUsageLength);
    const usageText = previous > usageNow ? `${previous}→${usageNow}/${limit}` : `${usageNow}/${limit}`;
    if (statusTextEl) {
      statusTextEl.innerHTML = `${escapeHtml(onlineText)} <span class="status-sep">·</span> ${escapeHtml(sessionText)} <span class="status-sep">·</span> ${escapeHtml(usageText)}`;
    }
    if (statusDotEl) {
      // 用 class 切换以支持深色模式覆盖
      statusDotEl.classList.toggle("status-online", !!gatewayOnline);
      statusDotEl.classList.toggle("status-offline", !gatewayOnline);
      // 兜底清除上一版 inline style(从老缓存返回时)
      statusDotEl.style.background = "";
      statusDotEl.style.boxShadow = "";
    }
  }

  function setGatewayOnlineState(isOnline) {
    gatewayOnline = !!isOnline;
    renderHeaderStatus();
  }

  async function probeGatewayOnline() {
    try {
      const res = await apiFetch("/system/version", {
        method: "GET",
        headers: authHeaders({ "Cache-Control": "no-cache" }),
        cache: "no-store",
      });
      if (!res.ok) throw new Error(`http ${res.status}`);
      setGatewayOnlineState(true);
    } catch {
      setGatewayOnlineState(false);
    }
  }

  function startGatewayHeartbeat() {
    if (gatewayHeartbeatTimer) {
      clearInterval(gatewayHeartbeatTimer);
      gatewayHeartbeatTimer = 0;
    }
    probeGatewayOnline();
    gatewayHeartbeatTimer = setInterval(() => {
      probeGatewayOnline();
    }, GATEWAY_HEARTBEAT_INTERVAL_MS);
  }

  async function refreshContextUsageLength(sessionId = getSessionId()) {
    const sid = String(sessionId || "").trim();
    if (!sid || isDraftSessionId(sid)) {
      statusContextUsageLength = 0;
      statusContextPreviousUsageLength = 0;
      renderHeaderStatus();
      return;
    }
    try {
      const res = await apiFetch(`/sessions/${encodeURIComponent(sid)}/context-usage`, { method: "GET" });
      if (!res.ok) throw new Error(`http ${res.status}`);
      const data = await res.json();
      const nextTitle = String(data?.title || "").trim();
      if (nextTitle && nextTitle !== activeSessionTitle) {
        activeSessionTitle = nextTitle;
      }
      statusContextUsageLength = normalizeStatusCount(data?.usage_length);
      const serverLimit = normalizeStatusCount(data?.max_context_tokens);
      if (serverLimit >= 100) statusContextTokenLimit = serverLimit;
      renderHeaderStatus();
      setGatewayOnlineState(true);
    } catch {
      // 网关探测由独立心跳负责；这里保持上一次可用值，避免 UI 抖动。
    }
  }

  function refreshComposerDisabledState() {
    const hardLocked = terminalConfirmLockActive;
    if (inputEl) {
      inputEl.disabled = hardLocked;
      const pendingKind = String(pendingConfirmCurrent?.kind || "").trim().toLowerCase();
      const lockPlaceholder = pendingKind === "question" ? INPUT_PLACEHOLDER_QUESTION_LOCK : INPUT_PLACEHOLDER_CONFIRM_LOCK;
      inputEl.placeholder = hardLocked ? lockPlaceholder : INPUT_PLACEHOLDER_DEFAULT;
    }
    if (sendBtn) {
      sendBtn.disabled = hardLocked || messageSendInFlight;
    }
  }

  function setMessageSendInFlight(active) {
    messageSendInFlight = !!active;
    refreshComposerDisabledState();
  }

  function setTerminalConfirmLock(active, pendingCount = 0) {
    terminalConfirmLockActive = !!active;
    terminalConfirmPendingCount = Math.max(0, Number(pendingCount) || 0);
    refreshComposerDisabledState();
  }

  function normalizePendingConfirmItem(raw) {
    if (!raw || typeof raw !== "object") return null;
    const kind = String(raw.kind || "").trim().toLowerCase() === "question" ? "question" : "terminal";
    const cmd = String(raw.cmd || "").trim();
    const question = String(raw.question || "").trim();
    if (kind === "question" && !question) return null;
    if (kind !== "question" && !cmd) return null;
    const callId = String(raw.call_id || raw.confirm_id || "").trim();
    const confirmId = String(raw.confirm_id || callId || "").trim() || ("tcf_" + Date.now().toString(36));
    return {
      flow: String(raw.flow || "").trim(),
      kind,
      confirm_id: confirmId,
      call_id: callId || confirmId,
      cmd,
      question,
      options: Array.isArray(raw.options) ? raw.options.map((x) => String(x || "").trim()).filter(Boolean).slice(0, 8) : [],
      none_of_them_value: String(raw.none_of_them_value || "__none_of_them__"),
      none_of_them_label: String(raw.none_of_them_label || "以上都不是，我自己补充"),
      allow_custom_answer: raw.allow_custom_answer !== false,
      placeholder: String(raw.placeholder || "补充你的答案或限制条件..."),
      selected_choice: "",
      status: "pending",
    };
  }

  function setPendingConfirmFromList(items) {
    const list = Array.isArray(items) ? items : [];
    const visibleList = list.filter((item) => String(item?.flow || "") !== "deep_alignment");
    const first = visibleList.length > 0 ? normalizePendingConfirmItem(visibleList[0]) : null;
    pendingConfirmCurrent = first;
    renderPendingConfirmOverlay();
    setTerminalConfirmLock(!!first, visibleList.length);
  }

  function clearPendingConfirm() {
    pendingConfirmCurrent = null;
    void hidePendingConfirmOverlay();
    setPendingConfirmButtonsDisabled(false);
    setTerminalConfirmLock(false, 0);
  }

  function showPendingConfirmOverlay() {
    if (!pendingConfirmOverlayEl) return;
    openAnimatedPanel(pendingConfirmOverlayEl, "pendingConfirm");
  }

  async function hidePendingConfirmOverlay() {
    if (!pendingConfirmOverlayEl) return;
    await closeAnimatedPanel(pendingConfirmOverlayEl, "pendingConfirm", "show", { timeout: 420 });
  }

  function setPendingConfirmButtonsDisabled(disabled) {
    const locked = !!disabled;
    if (pendingConfirmAllowBtnEl) pendingConfirmAllowBtnEl.disabled = locked;
    if (pendingConfirmDenyBtnEl) pendingConfirmDenyBtnEl.disabled = locked;
  }

  function renderPendingConfirmOverlay() {
    if (!pendingConfirmOverlayEl || !pendingConfirmMetaEl || !pendingConfirmCmdEl) return;
    const dialog = pendingConfirmOverlayEl.querySelector(".pending-confirm-dialog");
    if (!pendingConfirmCurrent) {
      dialog?.classList.remove("question-mode");
      if (pendingConfirmTitleEl) pendingConfirmTitleEl.textContent = "终端命令请求确认";
      if (pendingConfirmSubtitleEl) pendingConfirmSubtitleEl.textContent = "请确认这条命令是否允许在当前环境执行。";
      if (pendingConfirmLabelEl) pendingConfirmLabelEl.textContent = "Command";
      pendingConfirmMetaEl.textContent = "等待确认";
      pendingConfirmCmdEl.textContent = "";
      if (pendingQuestionOptionsEl) pendingQuestionOptionsEl.innerHTML = "";
      if (pendingQuestionAnswerEl) pendingQuestionAnswerEl.value = "";
      void hidePendingConfirmOverlay();
      setPendingConfirmButtonsDisabled(false);
      return;
    }
    const isQuestion = pendingConfirmCurrent.kind === "question";
    dialog?.classList.toggle("question-mode", isQuestion);
    if (pendingConfirmTitleEl) pendingConfirmTitleEl.textContent = isQuestion ? "需要补充信息" : "终端命令请求确认";
    if (pendingConfirmSubtitleEl) {
      pendingConfirmSubtitleEl.textContent = isQuestion
        ? "继续执行前需要你补充条件或选择一个答案。"
        : "请确认这条命令是否允许在当前环境执行。";
    }
    if (pendingConfirmLabelEl) pendingConfirmLabelEl.textContent = isQuestion ? "Question" : "Command";
    pendingConfirmMetaEl.textContent = isQuestion ? "待回答" : "待确认";
    pendingConfirmCmdEl.textContent = isQuestion ? String(pendingConfirmCurrent.question || "") : String(pendingConfirmCurrent.cmd || "");
    if (pendingQuestionOptionsEl) {
      pendingQuestionOptionsEl.innerHTML = "";
      if (isQuestion) {
        (pendingConfirmCurrent.options || []).forEach((option) => {
          const isNoneOption = option === pendingConfirmCurrent.none_of_them_value;
          const label = isNoneOption ? pendingConfirmCurrent.none_of_them_label : option;
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "pending-question-option";
          btn.textContent = label;
          if (pendingConfirmCurrent.selected_choice === option) btn.classList.add("selected");
          btn.addEventListener("click", () => {
            pendingConfirmCurrent.selected_choice = option;
            if (pendingQuestionAnswerEl && !pendingQuestionAnswerEl.value.trim() && !isNoneOption) {
              pendingQuestionAnswerEl.value = option;
            }
            if (pendingQuestionAnswerEl && isNoneOption) pendingQuestionAnswerEl.focus();
            renderPendingConfirmOverlay();
          });
          pendingQuestionOptionsEl.appendChild(btn);
        });
      }
    }
    if (pendingQuestionAnswerEl) {
      pendingQuestionAnswerEl.placeholder = String(pendingConfirmCurrent.placeholder || "补充你的答案或限制条件...");
      pendingQuestionAnswerEl.style.display = isQuestion && pendingConfirmCurrent.allow_custom_answer ? "" : "none";
    }
    if (pendingConfirmAllowBtnEl) pendingConfirmAllowBtnEl.textContent = isQuestion ? "提交回答" : "允许执行";
    if (pendingConfirmDenyBtnEl) pendingConfirmDenyBtnEl.textContent = isQuestion ? "取消" : "拒绝";
    showPendingConfirmOverlay();
    setPendingConfirmButtonsDisabled(pendingConfirmSubmitting);
  }

  async function syncPendingConfirmationsFromServer(sessionId = getSessionId(), { silent = true } = {}) {
    const sid = String(sessionId || getSessionId()).trim();
    if (!sid) {
      clearPendingConfirm();
      return [];
    }
    try {
      const params = new URLSearchParams({ session_id: sid });
      const res = await apiFetch(`/terminal/pending?${params.toString()}`, { method: "GET" });
      const data = await res.json();
      if (!res.ok || data?.ok === false) throw new Error(String(data?.error || `http ${res.status}`));
      const pending = Array.isArray(data?.pending) ? data.pending : [];
      setPendingConfirmFromList(pending);
      return pending;
    } catch (e) {
      if (!silent) {
        showToast(`同步确认状态失败：${String(e?.message || e)}`, "err");
      }
      return [];
    }
  }

  async function submitPendingConfirmation(approval) {
    if (pendingConfirmSubmitting) return;
    const currentEl = pendingConfirmCurrent;
    if (!currentEl) return;
    const sid = String(getSessionId() || "").trim();
    if (!sid) return;

    pendingConfirmSubmitting = true;
    setPendingConfirmButtonsDisabled(true);
    void hidePendingConfirmOverlay();
    pendingConfirmCurrent = null;

    try {
      const isQuestion = currentEl.kind === "question";
      const selectedChoice = String(currentEl.selected_choice || "").trim();
      const isNoneChoice = selectedChoice && selectedChoice === String(currentEl.none_of_them_value || "__none_of_them__");
      const answerText = isQuestion ? String(pendingQuestionAnswerEl?.value || selectedChoice || "").trim() : "";
      if (isQuestion && approval && !answerText && !selectedChoice) {
        pendingConfirmSubmitting = false;
        pendingConfirmCurrent = currentEl;
        renderPendingConfirmOverlay();
        showToast("请先选择或补充回答", "warn");
        return;
      }
      if (isQuestion && approval && isNoneChoice && !String(pendingQuestionAnswerEl?.value || "").trim()) {
        pendingConfirmSubmitting = false;
        pendingConfirmCurrent = currentEl;
        renderPendingConfirmOverlay();
        pendingQuestionAnswerEl?.focus();
        showToast("选择“以上都不是”后请补充你的答案", "warn");
        return;
      }
      const res = await apiFetch("/terminal/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sid,
          approval: !!approval,
          kind: currentEl.kind || "terminal",
          call_id: currentEl.call_id || "",
          cmd: currentEl.cmd || "",
          choice: isNoneChoice ? "" : selectedChoice,
          answer: answerText,
        }),
      });
      const data = await res.json();
      if (!res.ok || data?.ok === false) {
        throw new Error(String(data?.error || `http ${res.status}`));
      }
      if (data?.flow === "deep_alignment") {
        const confirmPending = Array.isArray(data?.pending) ? data.pending : [];
        if (data?.active && String(data?.alignment_text || "").trim()) {
          removeDeepCard(deepActiveCard);
          deepPendingPayload = normalizeDeepPayload({
            sid,
            message: String(data.original_message || deepPendingPayload?.message || ""),
            file_names: Array.isArray(data.file_names) ? data.file_names : [],
            file_contents: Array.isArray(data.file_contents) ? data.file_contents : [],
            alignment_text: String(data.alignment_text || ""),
            rounds: Array.isArray(data.rounds) ? data.rounds : [],
            active_index: Number(data.active_index || 0),
            can_back: !!data.can_back,
            pending_deep_ask: data.pending_deep_ask || null,
            force_latest: true,
          });
          renderDeepAlignmentCard(deepPendingPayload);
        } else if (data?.state === "cancelled") {
          deepPendingPayload = null;
          removeDeepCard(deepActiveCard);
        }
        setPendingConfirmFromList(confirmPending);
        showToast(data?.state === "cancelled" ? "已取消 Deep 对齐" : "已提交回答");
        return;
      }
      const replyText = sanitizeAssistantDisplayText(String(data?.reply || ""));
      const confirmPending = Array.isArray(data?.pending) ? data.pending : null;
      const toolSteps = Math.max(0, Number(data?.tool_steps || 0));
      const marker = buildToolTraceMarkerMarkdown(data?.tool_trace);
      const compressionText = absorbContextCompressionPayload(data.context_compression);
      let finalReply = marker
        ? (replyText ? `${replyText}\n\n${marker}` : marker)
        : replyText;
      if (compressionText) {
        finalReply = String(finalReply || "").trim()
          ? `${finalReply}\n\n${compressionText}`
          : compressionText;
      }
      if (finalReply.trim()) {
        upsertAssistantTurnBubble(finalReply, { turnId: data?.turn_id, append: true });
      }
      if (Array.isArray(data?.tool_trace) && data.tool_trace.length > 0) {
        renderToolTraceToTerminal(data.tool_trace, { sessionId: sid, persist: false });
      }
      if (confirmPending) {
        setPendingConfirmFromList(confirmPending);
      } else {
        await syncPendingConfirmationsFromServer(sid, { silent: true });
      }
      showToast(currentEl.kind === "question" ? "已提交回答" : (approval ? "已允许执行" : "已拒绝执行"));
    } catch (e) {
      showToast(`操作失败：${String(e?.message || e)}`, "err");
      await syncPendingConfirmationsFromServer(sid, { silent: false });
    } finally {
      pendingConfirmSubmitting = false;
      renderPendingConfirmOverlay();
      syncTerminalConfirmLockFromDom();
    }
  }

  function syncTerminalConfirmLockFromDom() {
    const pending = pendingConfirmCurrent ? 1 : 0;
    setTerminalConfirmLock(pending > 0, pending);
    return pending;
  }

  function pendingConfirmLockMessage() {
    const kind = String(pendingConfirmCurrent?.kind || "").trim().toLowerCase();
    if (kind === "question") return "存在待回答问题，请先在弹窗中提交回答或取消。";
    return "存在待确认终端命令，请先在弹窗中允许/拒绝。";
  }

  function reportErrorToTerminal(message, { source = "", sessionId = getSessionId(), persist = false } = {}) {
    const text = String(message ?? "").trim() || "未知错误";
    const prefix = source ? `[error/${source}]` : "[error]";
    showTerm();
    addTermLine("out", `${prefix} ${text}`, "err", { sessionId, persist });
  }

  function normalizeTerminalClass(raw) {
    const v = String(raw || "").trim().toLowerCase();
    return ["err", "info", "dim"].includes(v) ? v : "";
  }

  function inferTerminalClass(text, rawClass = "") {
    const normalized = normalizeTerminalClass(rawClass);
    if (normalized) return normalized;
    const line = String(text || "").trim();
    if (!line) return "";
    if (/^\[error(?:\/[^\]]+)?\]/i.test(line)) return "err";
    if (/(请求失败|执行失败|命令解析失败|未知命令|权限不足|exception|traceback|permission denied)/i.test(line)) {
      return "err";
    }
    if (/^(tool:\s*|可用工具)/i.test(line)) return "info";
    if (/^\(已跳过重复工具轨迹渲染\)/.test(line)) return "dim";
    return "";
  }

  function getVersionBadgeText() {
    return String(appVersionDisplay || VERSION_FALLBACK).trim() || VERSION_FALLBACK;
  }

  function normalizeVersionTag(v) {
    const raw = String(v || "").trim();
    if (!raw) return "";
    return raw.replace(/^v/i, "");
  }

  function compareVersionTag(a, b) {
    const sa = normalizeVersionTag(a);
    const sb = normalizeVersionTag(b);
    if (!sa && !sb) return 0;
    if (!sa) return -1;
    if (!sb) return 1;
    const pa = sa.split(/[.+-]/);
    const pb = sb.split(/[.+-]/);
    const len = Math.max(pa.length, pb.length);
    for (let i = 0; i < len; i++) {
      const xa = String(pa[i] ?? "0");
      const xb = String(pb[i] ?? "0");
      const na = /^\d+$/.test(xa);
      const nb = /^\d+$/.test(xb);
      if (na && nb) {
        const ia = Number(xa);
        const ib = Number(xb);
        if (ia > ib) return 1;
        if (ia < ib) return -1;
        continue;
      }
      const cmp = xa.localeCompare(xb);
      if (cmp !== 0) return cmp > 0 ? 1 : -1;
    }
    return 0;
  }

  async function syncAppVersion() {
    try {
      const res = await apiFetch("/system/version", { method: "GET" });
      const data = res.ok ? await res.json() : {};
      const display = String(data?.display || "").trim();
      if (display) appVersionDisplay = display;
      appVersionSignatureId = String(data?.signature_id || "").trim();
      appVersionVerified = Boolean(data?.verified);
    } catch {}
    const badgeEl = document.getElementById("versionBadgeBtn");
    if (badgeEl) badgeEl.textContent = getVersionBadgeText();
  }

  function trimTerminalLines() {
    const children = termBodyEl?.children;
    if (!children) return;
    const overflow = children.length - TERM_MAX_LINES;
    if (overflow <= 0) return;
    for (let i = 0; i < overflow; i++) {
      if (!termBodyEl.firstElementChild) break;
      termBodyEl.removeChild(termBodyEl.firstElementChild);
    }
  }

  function enqueueTermPersist(entry, sessionId) {
    if (!entry) return;
    termPersistBuffer.push({ entry, sessionId: String(sessionId || getSessionId()).trim() });
    if (termPersistBuffer.length >= TERM_FLUSH_BATCH) {
      flushTermPersistBuffer();
      return;
    }
    if (termPersistTimer) return;
    termPersistTimer = setTimeout(() => {
      termPersistTimer = 0;
      flushTermPersistBuffer();
    }, 160);
  }

  function flushTermPersistBuffer() {
    if (termPersistTimer) {
      clearTimeout(termPersistTimer);
      termPersistTimer = 0;
    }
    if (!Array.isArray(termPersistBuffer) || termPersistBuffer.length === 0) return;
    const batch = termPersistBuffer.splice(0, termPersistBuffer.length);
    const groups = new Map();
    batch.forEach((item) => {
      const sid = String(item?.sessionId || getSessionId()).trim();
      if (!sid || !item?.entry) return;
      if (!groups.has(sid)) groups.set(sid, []);
      groups.get(sid).push(item.entry);
    });
    groups.forEach((entries, sid) => {
      enqueueSessionEntries(entries, { sessionId: sid });
    });
  }

  function splitTermOutputLines(text) {
    return String(text ?? "").split(/\r?\n/);
  }

  function termOmittedLine(count) {
    const n = Math.max(1, Number(count) || 1);
    return `... 已限制输出，省略 ${n} 行`;
  }

  function normalizeTermDisplayText(text) {
    const raw = String(text ?? "");
    if (raw.length <= TERM_LINE_CHAR_LIMIT) return raw;
    return raw.slice(0, TERM_LINE_CHAR_LIMIT) + ` ... 已截断 ${raw.length - TERM_LINE_CHAR_LIMIT} 字符`;
  }

  function normalizeTermFullLines(lines) {
    const arr = Array.isArray(lines) ? lines : splitTermOutputLines(lines);
    return arr.map((line) => String(line ?? ""));
  }

  function cacheTermFullOutput(lines, {
    title = "完整信息",
    cls = "",
  } = {}) {
    const fullLines = normalizeTermFullLines(lines);
    const cacheId = "term_full_" + (++termFullOutputSeq).toString(36);
    termFullOutputCache.set(cacheId, {
      id: cacheId,
      title: String(title || "完整信息"),
      cls: String(cls || ""),
      content: fullLines.join("\n"),
      lines: fullLines.length,
      ts: Date.now(),
    });
    while (termFullOutputCache.size > TERM_FULL_CACHE_LIMIT) {
      const oldest = termFullOutputCache.keys().next().value;
      if (!oldest) break;
      termFullOutputCache.delete(oldest);
    }
    return cacheId;
  }

  function touchTermFullCache(cacheId) {
    if (!termFullOutputCache.has(cacheId)) return null;
    const item = termFullOutputCache.get(cacheId);
    termFullOutputCache.delete(cacheId);
    termFullOutputCache.set(cacheId, item);
    return item;
  }

  function openTermFullCard(cacheId, title = "") {
    const item = touchTermFullCache(String(cacheId || ""));
    if (!item || !termFullOverlayEl || !termFullContentEl) {
      showToast("完整信息缓存已过期", "warn");
      return;
    }
    if (termFullTitleEl) termFullTitleEl.textContent = title || item.title || "完整信息";
    termFullContentEl.textContent = "正在加载缓存...";
    termFullOverlayEl.classList.add("show");
    termFullOverlayEl.setAttribute("aria-hidden", "false");
    if (termFullRenderRaf) cancelAnimationFrame(termFullRenderRaf);
    termFullRenderRaf = requestAnimationFrame(() => {
      termFullRenderRaf = 0;
      if (termFullOverlayEl?.classList.contains("show")) {
        termFullContentEl.textContent = item.content || "";
      }
    });
  }

  function closeTermFullCard() {
    if (!termFullOverlayEl) return;
    if (termFullRenderRaf) {
      cancelAnimationFrame(termFullRenderRaf);
      termFullRenderRaf = 0;
    }
    termFullOverlayEl.classList.remove("show");
    termFullOverlayEl.setAttribute("aria-hidden", "true");
    if (termFullContentEl) termFullContentEl.textContent = "";
  }

  function createTermFullButton(cacheId, omitted, title = "完整信息") {
    const row = document.createElement("div");
    row.className = "term-full-row";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "term-full-btn";
    btn.dataset.termFullId = cacheId;
    btn.dataset.termFullTitle = title;
    btn.textContent = omitted > 0
      ? `查看完整信息（另有 ${omitted} 行）`
      : "查看完整信息";
    row.appendChild(btn);
    return row;
  }

  function appendTermOutputPreviewRows(parent, lines, rawClass = "", {
    title = "完整信息",
    previewLimit = TERM_ENTRY_PREVIEW_LINES,
  } = {}) {
    if (!(parent instanceof Node)) return { shown: 0, omitted: 0 };
    const fullLines = normalizeTermFullLines(lines);
    const safePreviewLimit = Math.max(1, Number(previewLimit) || TERM_ENTRY_PREVIEW_LINES);
    const shown = fullLines.slice(0, safePreviewLimit);
    shown.forEach((line) => {
      const displayText = normalizeTermDisplayText(line);
      const row = document.createElement("div");
      row.className = parent.classList?.contains("term-call-group") ? "term-call-row out" : "term-out-row";
      const span = document.createElement("span");
      const cls = inferTerminalClass(displayText, rawClass);
      span.className = "term-out" + (cls ? " " + cls : "");
      span.textContent = displayText;
      row.appendChild(span);
      parent.appendChild(row);
    });
    const omitted = Math.max(0, fullLines.length - shown.length);
    if (omitted > 0) {
      const cacheId = cacheTermFullOutput(fullLines, { title, cls: rawClass });
      parent.appendChild(createTermFullButton(cacheId, omitted, title));
    }
    return { shown: shown.length, omitted };
  }

  function addTermOmittedLine(count, { sessionId = getSessionId(), persist = true, noScroll = false } = {}) {
    addTermLine("out", termOmittedLine(count), "dim", { sessionId, persist, noScroll });
  }

  function getCurrentTerminalOutGroup() {
    if (!termBodyEl) return null;
    const last = termBodyEl.lastElementChild;
    if (
      last
      && last.classList
      && last.classList.contains("term-line")
      && String(last.dataset?.termKind || "") === "out-group"
    ) {
      return last;
    }
    const queued = Array.isArray(termRenderQueue) ? termRenderQueue : [];
    for (let i = queued.length - 1; i >= 0; i -= 1) {
      const node = queued[i]?.node;
      if (
        node instanceof Element
        && node.classList.contains("term-line")
        && String(node.dataset?.termKind || "") === "out-group"
      ) {
        return node;
      }
    }
    return null;
  }

  function queueTerminalDomFlush({ scroll = true } = {}) {
    if (scroll) termPendingScroll = true;
    if (termRenderRaf) return;
    termRenderRaf = requestAnimationFrame(flushTerminalDomQueue);
  }

  function flushTerminalDomQueue() {
    termRenderRaf = 0;
    if (!termBodyEl || termRenderQueue.length === 0) {
      termPendingScroll = false;
      return;
    }
    const batch = termRenderQueue.splice(0, TERM_RENDER_BATCH_SIZE);
    const frag = document.createDocumentFragment();
    batch.forEach((item) => {
      if (!item || !(item.node instanceof Node)) return;
      if (item.parent instanceof Node) {
        item.parent.appendChild(item.node);
      } else {
        frag.appendChild(item.node);
      }
    });
    if (frag.childNodes.length > 0) termBodyEl.appendChild(frag);
    trimTerminalLines();
    if (termRenderQueue.length > 0) {
      termRenderRaf = requestAnimationFrame(flushTerminalDomQueue);
      return;
    }
    if (termPendingScroll) {
      termBodyEl.scrollTop = termBodyEl.scrollHeight;
      termPendingScroll = false;
    }
  }

  function appendTermNode(node, { parent = null, scroll = true, topLevel = false } = {}) {
    if (!(node instanceof Node)) return;
    termRenderQueue.push({ parent: topLevel ? null : parent, node });
    queueTerminalDomFlush({ scroll });
  }

  function flushTerminalRenderQueueNow() {
    if (termRenderRaf) {
      cancelAnimationFrame(termRenderRaf);
      termRenderRaf = 0;
    }
    if (!termBodyEl || termRenderQueue.length === 0) {
      termPendingScroll = false;
      return;
    }
    const topLevelFrag = document.createDocumentFragment();
    while (termRenderQueue.length > 0) {
      const item = termRenderQueue.shift();
      if (!item || !(item.node instanceof Node)) continue;
      if (item.parent instanceof Node) {
        item.parent.appendChild(item.node);
      } else {
        topLevelFrag.appendChild(item.node);
      }
    }
    if (topLevelFrag.childNodes.length > 0) termBodyEl.appendChild(topLevelFrag);
    trimTerminalLines();
    if (termPendingScroll) {
      termBodyEl.scrollTop = termBodyEl.scrollHeight;
      termPendingScroll = false;
    }
  }

  function clearTerminalRenderQueue() {
    if (termRenderRaf) {
      cancelAnimationFrame(termRenderRaf);
      termRenderRaf = 0;
    }
    termRenderQueue = [];
    termPendingScroll = false;
  }

  function addLimitedTermLines(lines, cls = "", {
    sessionId = getSessionId(),
    persist = true,
    noScroll = false,
    limit = TERM_ENTRY_PREVIEW_LINES,
    title = "完整信息",
  } = {}) {
    const arr = normalizeTermFullLines(lines);
    if (arr.length === 0) return { shown: 0, omitted: 0 };
    const safeLimit = Math.max(1, Number(limit) || TERM_ENTRY_PREVIEW_LINES);
    let group = null;
    if (termRequestGroup instanceof Node) {
      group = termRequestGroup;
    } else {
      const currentOutGroup = getCurrentTerminalOutGroup();
      if (currentOutGroup) {
        group = currentOutGroup;
      } else {
        group = document.createElement("div");
        group.className = "term-line term-out-group";
        group.dataset.termKind = "out-group";
        appendTermNode(group, { scroll: false, topLevel: true });
      }
    }
    appendTermOutputPreviewRows(group, arr, cls, { title, previewLimit: safeLimit });
    if (persist) {
      arr.forEach((line) => {
        const displayText = normalizeTermDisplayText(line);
        enqueueTermPersist(
          {
            role: "assistant",
            type: "terminal",
            display_target: "terminal",
            context_policy: "include",
            entry_type: "terminal",
            terminal_kind: "out",
            terminal_class: inferTerminalClass(displayText, cls),
            content: displayText,
            ts: toIsoWithOffset(new Date()),
          },
          sessionId,
        );
      });
    }
    const omitted = Math.max(0, arr.length - Math.min(arr.length, safeLimit));
    if (!noScroll) queueTerminalDomFlush({ scroll: true });
    return { shown: Math.min(arr.length, safeLimit), omitted };
  }

  function createTermRequestGroup() {
    const group = document.createElement("div");
    group.className = "term-line term-call-group";
    group.dataset.termKind = "call-group";
    appendTermNode(group, { scroll: false, topLevel: true });
    termRequestGroup = group;
    return group;
  }

  function ensureTermRequestGroup() {
    if (termRequestGroup instanceof Node) return termRequestGroup;
    return createTermRequestGroup();
  }
