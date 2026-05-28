/**
 * Extracted from chat.html: chat_terminal.js
 */

  // --- Terminal ---
  function addTermLine(type, text, cls = "", { persist = true, sessionId = getSessionId(), noScroll = false } = {}) {
    const displayText = normalizeTermDisplayText(text);
    let line = null;
    if (type === "cmd") {
      termRequestGroup = null;
      const group = ensureTermRequestGroup();
      const row = document.createElement("div");
      row.className = "term-call-row cmd";
      row.innerHTML = `<span class="term-prompt">tinda@agent ~ % </span><span class="term-cmd">${escapeHtml(displayText)}</span>`;
      appendTermNode(row, { parent: group, scroll: !noScroll });
    } else {
      const normalizedCls = inferTerminalClass(displayText, cls);
      if (termRequestGroup instanceof Node) {
        line = termRequestGroup;
      } else {
        const currentOutGroup = getCurrentTerminalOutGroup();
        if (currentOutGroup) {
          line = currentOutGroup;
        } else {
          line = document.createElement("div");
          line.className = "term-line term-out-group";
          line.dataset.termKind = "out-group";
          appendTermNode(line, { scroll: false, topLevel: true });
        }
      }
      const row = document.createElement("div");
      row.className = line === termRequestGroup ? "term-call-row out" : "term-out-row";
      const span = document.createElement("span");
      span.className = "term-out" + (normalizedCls ? " " + normalizedCls : "");
      span.textContent = displayText;
      row.appendChild(span);
      appendTermNode(row, { parent: line, scroll: !noScroll });
    }
    if (persist) {
      enqueueTermPersist(
        {
          role: "assistant",
          type: "terminal",
          display_target: "terminal",
          context_policy: "include",
          entry_type: "terminal",
          terminal_kind: type === "cmd" ? "cmd" : "out",
          terminal_class: type === "cmd" ? "" : inferTerminalClass(displayText, cls),
          content: displayText,
          ts: toIsoWithOffset(new Date()),
        },
        sessionId,
      );
    }
  }

  function addTermSep({ persist = true, sessionId = getSessionId(), noScroll = false } = {}) {
    termRequestGroup = null;
    if (!noScroll) queueTerminalDomFlush({ scroll: true });
  }

  function appendTerminalWidget(contentEl, { noScroll = false } = {}) {
    if (!(contentEl instanceof Node)) return null;
    flushTerminalRenderQueueNow();
    termRequestGroup = null;
    const line = document.createElement("div");
    line.className = "term-line term-widget";
    line.dataset.termKind = "widget";
    line.appendChild(contentEl);
    termBodyEl.appendChild(line);
    trimTerminalLines();
    if (!noScroll) termBodyEl.scrollTop = termBodyEl.scrollHeight;
    return line;
  }

  function initTermContent() {
    addTermLine("out", `Tinda Terminal ${getVersionBadgeText()}`, "info", { persist: false });
    addTermLine("out", "使用 /tool echo <文本> 在此输出内容", "dim", { persist: false });
    addTermSep({ persist: false });
  }

  function ensureTermInitialized() {
    if (termInitialized) return;
    initTermContent();
    termInitialized = true;
  }

  function openTerm() {
    ensureTermInitialized();
    termOpen = true;
    const tb = document.getElementById("termToggleBtn");
    if (tb) {
      tb.classList.add("on");
      tb.title = "关闭终端";
      tb.setAttribute("data-tip", "关闭终端");
    }
    // double-RAF：确保浏览器已记录 width:0/opacity:0 的初始帧，再启动过渡
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        termPanelEl.style.transition = "";
        termPanelEl.style.width   = termWidth + "px";
        termPanelEl.style.opacity = "1";
        termPanelEl.classList.add("open");
        resizerEl.classList.add("visible");
        scheduleRecalcPinnedSpacerHeight();
      });
    });
  }

  function closeTerm() {
    termOpen = false;
    termPanelEl.style.transition = "";
    termPanelEl.style.width   = "0px";
    termPanelEl.style.opacity = "0";
    termPanelEl.classList.remove("open");
    resizerEl.classList.remove("visible");
    scheduleRecalcPinnedSpacerHeight();
    const tb = document.getElementById("termToggleBtn");
    if (tb) {
      tb.classList.remove("on");
      tb.title = "打开终端";
      tb.setAttribute("data-tip", "打开终端");
    }
  }

  function smoothNavigate(url) {
    if (!url) return;
    if (navigationPending) return;
    navigationPending = true;
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      if (termOpen) closeTerm();
      location.href = url;
      return;
    }
    if (isResizing) {
      isResizing = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      resizerEl.classList.remove("dragging");
    }
    closeTimePanel();
    closeModelPanel();
    closeRecordsPanel();
    if (termOpen) {
      termWidth = Math.max(termPanelEl.offsetWidth, 180);
      localStorage.setItem(TERM_WIDTH_KEY, String(Math.round(termWidth)));
      closeTerm();
    }
    document.body.classList.add("chat-leaving");
    setTimeout(() => { location.href = url; }, 520);
  }

  function showTerm() {
    if (termOpen) return;
    openTerm();
  }

  function clearTerm() {
    flushTermPersistBuffer();
    clearTerminalRenderQueue();
    closeTermFullCard();
    termFullOutputCache.clear();
    termRequestGroup = null;
    termBodyEl.innerHTML = "";
    termInitialized = false;
    initTermContent();
    termInitialized = true;
  }

  function toggleTerm() {
    if (!termOpen) {
      openTerm();
      return;
    }
    termWidth = Math.max(termPanelEl.offsetWidth, 180);
    localStorage.setItem(TERM_WIDTH_KEY, String(termWidth));
    closeTerm();
  }

  function isEchoCmd(text) {
    const parts = text.trim().split(/\s+/);
    return parts[0] === "/tool" && parts[1] === "echo";
  }

  function compactValueForTerminal(v, {
    maxChars = 1600,
    maxKeys = 24,
    maxItems = 12,
    depth = 0,
  } = {}) {
    if (v === null || v === undefined) return "";
    if (typeof v === "string") return v;
    if (typeof v === "number" || typeof v === "boolean") return String(v);
    if (depth >= 3) return "[已压缩]";
    if (Array.isArray(v)) {
      const shown = v.slice(0, maxItems).map((item) => compactValueForTerminal(item, {
        maxChars,
        maxKeys,
        maxItems,
        depth: depth + 1,
      }));
      if (v.length > shown.length) shown.push(`... 已省略 ${v.length - shown.length} 项`);
      return shown.join("\n");
    }
    if (typeof v === "object") {
      const entries = Object.entries(v);
      const lines = [];
      entries.slice(0, maxKeys).forEach(([key, value]) => {
        const rendered = compactValueForTerminal(value, {
          maxChars: Math.max(160, Math.floor(maxChars / 2)),
          maxKeys,
          maxItems,
          depth: depth + 1,
        });
        const oneLine = String(rendered || "").replace(/\s+/g, " ").trim();
        lines.push(`${key}: ${normalizeTermDisplayText(oneLine || "[空]")}`);
      });
      if (entries.length > lines.length) lines.push(`... 已省略 ${entries.length - lines.length} 个字段`);
      return lines.join("\n");
    }
    return String(v);
  }

  function stringifyValue(v) {
    return compactValueForTerminal(v, { maxChars: 1200, maxKeys: 16, maxItems: 8 });
  }

  function normalizeTerminalSummaryLines(summary) {
    if (Array.isArray(summary)) {
      return summary
        .map((line) => normalizeTermDisplayText(line))
        .filter((line) => String(line || "").trim());
    }
    if (typeof summary === "string" && summary.trim()) {
      return summary.split(/\r?\n/).map((line) => normalizeTermDisplayText(line));
    }
    return [];
  }

  function collectToolResultTerminalLines(step, result) {
    const lines = [];
    if (!result || typeof result !== "object") {
      const raw = compactValueForTerminal(step?.raw_result, { maxChars: 1200, maxKeys: 12, maxItems: 8 });
      return raw ? raw.split(/\r?\n/) : [];
    }
    const summaryLines = normalizeTerminalSummaryLines(result.terminal_summary || result.display_summary);
    if (summaryLines.length > 0) return summaryLines;
    if (result.stdout) lines.push(...splitTermOutputLines(result.stdout));
    if (result.stderr) lines.push(...splitTermOutputLines(result.stderr));
    if (result.output) lines.push(...splitTermOutputLines(result.output));
    if (result.result !== undefined) {
      const rendered = compactValueForTerminal(result.result, { maxChars: 1600, maxKeys: 24, maxItems: 12 });
      if (rendered) lines.push(...splitTermOutputLines(rendered));
    }
    if (result.tools && typeof result.tools === "object") {
      lines.push("可用工具列表：");
      Object.entries(result.tools).slice(0, TERM_OUTPUT_LINE_LIMIT).forEach(([k, v]) => {
        lines.push(`- ${k}: ${normalizeTermDisplayText(v)}`);
      });
      const omitted = Object.keys(result.tools).length - Math.min(Object.keys(result.tools).length, TERM_OUTPUT_LINE_LIMIT);
      if (omitted > 0) lines.push(termOmittedLine(omitted));
    }
    if (lines.length === 0 && result.frontend_truncated) {
      lines.push("结果已压缩显示。");
    }
    return lines;
  }

  function renderToolTraceToTerminal(trace, { sessionId = getSessionId(), persist = false } = {}) {
    if (!Array.isArray(trace) || trace.length === 0) return false;
    const planShown = showPlanPanelsFromTrace(trace);
    const terminalTrace = trace.filter((step) => !isPlanToolTraceStep(step));
    if (terminalTrace.length === 0) return planShown;
    showTerm();
    terminalTrace.forEach((step) => {
      const name = step?.agent_tool || "unknown_tool";
      const args = step?.arguments || {};
      addTermLine("cmd", `[tool] ${name} ${stringifyValue(args)}`, "", { sessionId, persist });

      const result = step?.result;
      if (result && typeof result === "object") {
        const inner = result.result;
        if (inner && typeof inner === "object" && inner.pending_confirmation === true) {
          const cmd = String(inner.cmd || "").trim();
          const callId = String(inner.call_id || result.call_id || step?.call_id || step?.tool_call_id || "").trim();
          if (cmd) {
            syncPendingConfirmationsFromServer(getSessionId(), { silent: true });
            addTermLine("out", `[warn] 需要确认: ${cmd.slice(0, 80)}`, "info", { sessionId, persist });
          }
        } else if (result.pending_confirmation === true) {
          const cmd = String(result.cmd || "").trim();
          const callId = String(result.call_id || step?.call_id || step?.tool_call_id || "").trim();
          if (cmd) {
            syncPendingConfirmationsFromServer(getSessionId(), { silent: true });
            addTermLine("out", `[warn] 需要确认: ${cmd.slice(0, 80)}`, "info", { sessionId, persist });
          }
        } else if (result.ok === false) {
          if (result.error_code === "permission_denied" && result.expose_to_user === false) {
            addTermLine("out", `[error] ${result.user_message || "该工具当前不可用，请尝试其它方式。"}`, "err", { sessionId, persist });
          } else {
            addTermLine("out", `[error] ${result.error || "工具执行失败"}`, "err", { sessionId, persist });
          }
        } else {
          if (result.tool_name) addTermLine("out", `tool: ${result.tool_name}`, "info", { sessionId, persist });
          const lines = collectToolResultTerminalLines(step, result);
          if (lines.length > 0) addLimitedTermLines(lines, "", { sessionId, persist, title: `${name} 输出` });
        }
      } else {
        const lines = collectToolResultTerminalLines(step, result);
        if (lines.length > 0) addLimitedTermLines(lines, "", { sessionId, persist, title: `${name} 输出` });
      }
      addTermSep({ sessionId, persist });
    });
    showToast("已同步工具调用到终端");
    return true;
  }

  function buildToolTraceMarkerMarkdown(trace) {
    if (!Array.isArray(trace) || trace.length === 0) return "";
    const renderer = window.ChatBubbleRenderer?.renderToolMarkerMarkdown;
    if (typeof renderer !== "function") return "";
    const blocks = [];
    trace.forEach((step) => {
      if (!step || typeof step !== "object") return;
      const name = String(step.agent_tool || step.name || step.tool_name || "unknown").trim() || "unknown";
      const callId = String(step.call_id || step.id || "").trim();
      const toolCallId = String(step.tool_call_id || step.toolCallId || "").trim();
      const marker = renderer(
        {
          name,
          id: callId,
          call_id: callId,
          tool_call_id: toolCallId,
          arguments: step.arguments && typeof step.arguments === "object" ? step.arguments : {},
          result: step.result && typeof step.result === "object" ? step.result : {},
          status: "done",
        },
        { done: true, tool_call_id: toolCallId, trailingNewline: false },
      );
      if (String(marker || "").trim()) blocks.push(String(marker).trim());
    });
    return blocks.join("\n\n");
  }
