/**
 * chat_renderer.js — chat message renderer.
 * Normal chat bubble DOM, markdown updates and assistant turn binding live here.
 */
(function () {
  "use strict";

  var STREAM_DRAFT_PLACEHOLDER = "（正在生成，若页面刷新可稍后继续查看）";

  var activeRenderToken = 0;
  var assistantTurnBubbleById = new Map();

  function nextFrame() {
    return new Promise(function (resolve) {
      requestAnimationFrame(resolve);
    });
  }

  function getMessagesEl() {
    return (typeof messagesEl !== "undefined" && messagesEl) ? messagesEl : null;
  }

  function isHydrating() {
    return typeof isHydratingMessages === "function" && isHydratingMessages();
  }

  function getSpacer(container) {
    return container ? container.querySelector(".sys-pin-spacer") : null;
  }

  function previousRenderable(container, spacer) {
    return spacer ? spacer.previousElementSibling : container.lastElementChild;
  }

  function appendBeforeSpacer(container, node) {
    var spacer = getSpacer(container);
    if (spacer) container.insertBefore(node, spacer);
    else container.appendChild(node);
  }

  function botAvatarSvg() {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3.2"></circle><path d="M12 2.8v4.2M12 17v4.2M2.8 12h4.2M17 12h4.2M5.3 5.3l3 3M15.7 15.7l3 3M18.7 5.3l-3 3M8.3 15.7l-3 3"></path></svg>';
  }

  function userAvatarSvg() {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 21a8 8 0 0 0-16 0"></path><circle cx="12" cy="8" r="3.2"></circle></svg>';
  }

  function normalizeRole(role) {
    return String(role || "") === "user" ? "user" : "bot";
  }

  function normalizeTurn(raw) {
    if (typeof normalizeTurnId === "function") return normalizeTurnId(raw);
    return String(raw || "").trim().replace(/[^A-Za-z0-9._:-]+/g, "_").slice(0, 80);
  }

  function rememberTurn(turnId, bubble) {
    var tid = normalizeTurn(turnId);
    if (!tid || !bubble) return;
    assistantTurnBubbleById.set(tid, bubble);
  }

  function resetTurns() {
    assistantTurnBubbleById.clear();
  }

  function findTurnBubbleInDom(turnId) {
    var tid = normalizeTurn(turnId);
    var container = getMessagesEl();
    if (!tid || !container) return null;
    var bubbles = container.querySelectorAll(".bubble[data-turn-id]");
    for (var i = 0; i < bubbles.length; i++) {
      if (String(bubbles[i].dataset.turnId || "") === tid) return bubbles[i];
    }
    return null;
  }

  function resolveTurnBubble(turnId) {
    var tid = normalizeTurn(turnId);
    if (!tid) return null;
    var bubble = assistantTurnBubbleById.get(tid);
    if (bubble && bubble.isConnected) return bubble;
    assistantTurnBubbleById.delete(tid);
    bubble = findTurnBubbleInDom(tid);
    if (bubble) rememberTurn(tid, bubble);
    return bubble;
  }

  function renderMd(text) {
    return typeof renderMarkdown === "function" ? renderMarkdown(text) : String(text || "");
  }

  function markdownQuoteBlock(text) {
    var raw = String(text ?? "").trim();
    if (!raw) return "";
    return raw
      .split(/\r?\n/)
      .map(function(line) { return line ? "> " + line : ">"; })
      .join("\n");
  }

  function updateBubbleMarkdown(bubbleEl, text, options) {
    options = options || {};
    if (!bubbleEl) return false;
    var raw = String(text ?? "");
    bubbleEl.classList.add("md");
    bubbleEl.dataset.rawMd = raw;
    bubbleEl.innerHTML = renderMd(raw);
    if (options.turnId) {
      var tid = normalizeTurn(options.turnId);
      if (tid) {
        bubbleEl.dataset.turnId = tid;
        rememberTurn(tid, bubbleEl);
      }
    }
    if (options.scroll !== false && !isHydrating() && typeof scrollToBottom === "function") {
      scrollToBottom();
    }
    return true;
  }

  function appendMarkdownToBubble(bubbleEl, extraText, options) {
    if (!bubbleEl) return false;
    var prev = String(bubbleEl.dataset.rawMd ?? "");
    var next = prev + String(extraText ?? "");
    return updateBubbleMarkdown(bubbleEl, next, options);
  }

  function escape(value) {
    if (typeof escapeHtml === "function") return escapeHtml(value);
    return String(value ?? "").replace(/[&<>"']/g, function (ch) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch];
    });
  }

  function inferPlanStepStatus(text) {
    var raw = String(text ?? "").trim();
    if (!raw) return "";
    if (/✅|(?:→|—|-)\s*(?:已完成|已实施|完成|done)(?:\s*[（(][^）)]*[）)])?\s*$/i.test(raw)) return "done";
    if (/(?:^|[→—\-\s])(?:进行中|处理中|in[_ -]?progress)(?:$|[，。,.\s])/i.test(raw)) return "in_progress";
    if (/⏳|(?:^|[→—\-\s])(?:未实施|搁置|待定|阻塞|blocked)(?:$|[：:，。,.\s])/i.test(raw)) return "blocked";
    return "";
  }

  function cleanPlanStepText(text) {
    var clean = String(text ?? "").trim();
    clean = clean.replace(/^\s*[-*•]\s*/, "");
    clean = clean.replace(/^\s*\d{1,3}[.、)]\s*/, "");
    clean = clean.replace(/[✅⏳🏁]/g, "");
    clean = clean.trim();
    clean = clean.replace(/\s*(?:→|—|-)\s*(?:已完成|已实施|完成|done)(?:\s*[（(][^）)]*[）)])?\s*$/i, "");
    clean = clean.replace(/\s*(?:→|—|-)\s*(?:进行中|处理中|in[_ -]?progress)\s*$/i, "");
    clean = clean.replace(/\s*(?:→|—|-)\s*(?:待定|未实施|搁置|阻塞|blocked)(?:\s*[：:].*)?\s*$/i, "");
    clean = clean.replace(/^(?:未实施|待定|搁置)(?:[（(][^）)]*[）)])?\s*[：:]\s*/, "");
    return clean.replace(/\s{2,}/g, " ").trim();
  }

  function displayFileName(fullName) {
    if (typeof getDisplayFileName === "function") return getDisplayFileName(fullName);
    var name = String(fullName || "").trim();
    var lastSlash = Math.max(name.lastIndexOf("/"), name.lastIndexOf("\\"));
    return lastSlash >= 0 ? name.slice(lastSlash + 1) : name;
  }

  function createMessageRow(role, options) {
    options = options || {};
    var container = getMessagesEl();
    if (!container) return null;
    var normalizedRole = normalizeRole(role);
    var row = document.createElement("div");
    row.className = "msg " + normalizedRole;
    if (container.dataset.hydrating === "1" || options.history) row.classList.add("history-msg");

    var spacer = getSpacer(container);
    var prev = previousRenderable(container, spacer);
    if (prev && prev.classList && prev.classList.contains("msg") && prev.classList.contains(normalizedRole)) {
      row.classList.add("same-as-prev");
    }

    var avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.innerHTML = normalizedRole === "bot" ? botAvatarSvg() : userAvatarSvg();
    row.appendChild(avatar);
    return row;
  }

  function mountRow(row) {
    var container = getMessagesEl();
    if (!container || !row) return;
    appendBeforeSpacer(container, row);
    if (!isHydrating() && typeof scrollToBottom === "function") scrollToBottom();
  }

  function renderBubble(text, role, options) {
    options = options || {};
    var row = createMessageRow(role, options);
    if (!row) return { row: null, bubble: null };
    var normalizedRole = normalizeRole(role);
    var bubble = document.createElement("div");
    bubble.className = "bubble" + (options.isCommand ? " cmd" : "");
    if (normalizedRole === "bot") {
      var tid = normalizeTurn(options.turnId || "");
      updateBubbleMarkdown(bubble, text, { turnId: tid, scroll: false });
    } else {
      bubble.textContent = String(text ?? "");
    }
    row.appendChild(bubble);
    mountRow(row);
    return { row: row, bubble: bubble };
  }

  function renderFileChip(fileName) {
    var row = createMessageRow("user", {});
    if (!row) return { row: null, bubble: null };
    var shortName = displayFileName(fileName);
    var bubble = document.createElement("div");
    bubble.className = "bubble file-chip";
    bubble.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg><span>' + escape(shortName) + '</span>';
    row.appendChild(bubble);
    mountRow(row);
    return { row: row, bubble: bubble };
  }

  function renderTyping() {
    var row = createMessageRow("bot", {});
    if (!row) return null;
    var bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
    row.appendChild(bubble);
    mountRow(row);
    return row;
  }

  function createPlainRow(role, contentEl) {
    var row = createMessageRow(role, {});
    if (!row) return null;
    if (contentEl instanceof Node) row.appendChild(contentEl);
    return row;
  }

  function upsertAssistantBubble(rawText, options) {
    options = options || {};
    var text = String(rawText ?? "");
    if (typeof sanitizeAssistantDisplayText === "function") {
      text = sanitizeAssistantDisplayText(text);
    }
    if (!text) return null;
    var tid = normalizeTurn(options.turnId || "");
    var existing = resolveTurnBubble(tid);
    if (existing) {
      if (options.append) {
        var prev = String(existing.dataset.rawMd ?? "");
        var next = prev ? prev + "\n\n" + text : text;
        updateBubbleMarkdown(existing, next, { turnId: tid });
      } else {
        updateBubbleMarkdown(existing, text, { turnId: tid });
      }
      return { row: existing.closest(".msg"), bubble: existing };
    }
    return renderBubble(text, "bot", {
      isCommand: !!options.isCommand,
      turnId: tid,
    });
  }

  function renderToolMarkerMarkdown(marker, options) {
    options = options || {};
    var d = marker || {};
    if (d.data && typeof d.data === "object") d = d.data;
    if (typeof d !== "object") d = {};
    var name = d.name || d.tool_name || d.agent_tool || options.name || "unknown";
    var result = d.result && typeof d.result === "object" ? d.result : {};
    var innerResult = result.result && typeof result.result === "object" ? result.result : result;
    if (name === "plan" && innerResult.kind === "plan") {
      return renderPlanMarkerMarkdown(innerResult, options);
    }
    var cid = d.id || d.call_id || options.id || options.callId || "";
    var displayCid = String(cid || "").replace(/^tc_/, "");
    var toolCallId = d.tool_call_id || d.toolCallId || options.tool_call_id || options.toolCallId || "";
    var status = String(d.status || options.status || "").trim().toLowerCase();
    var progressText = String(d.progress || options.progress || "").trim();
    var elapsedMs = Number(d.elapsed_ms || d.elapsedMs || options.elapsed_ms || options.elapsedMs || 0);
    if (!progressText && elapsedMs > 0) {
      var seconds = Math.max(1, Math.floor(elapsedMs / 1000));
      progressText = "连接中 / 执行中 · 已等待 " + seconds + "s";
    }
    var done = Object.prototype.hasOwnProperty.call(options, "done")
      ? !!options.done
      : status && status !== "running" && status !== "pending";
    var lines = [
      "> >_<",
      "> --调用工具中--",
      "> **准备调用工具**: " + name,
    ];
    if (!done && progressText) {
      lines.push("> **状态**: " + progressText);
    }
    if (!done && (toolCallId || cid)) {
      var params = new URLSearchParams();
      if (toolCallId) params.set("tool_call_id", String(toolCallId));
      if (cid) params.set("call_id", String(cid));
      lines.push("> [" + "跳过" + "](toolskip:" + params.toString() + ")");
    }
    if (done) {
      lines.push("> **已调用工具**: " + name + (displayCid ? " #" + displayCid : ""));
    }
    return lines.join("\n") + (options.trailingNewline === false ? "" : "\n");
  }

  function renderPlanMarkerMarkdown(plan, options) {
    options = options || {};
    var d = plan && typeof plan === "object" ? plan : {};
    var action = String(d.action || "").trim().toLowerCase();
    var lines = ["> >_<", action === "clear" ? "> --计划已清除--" : action === "set_step_status" ? "> --计划状态已更新--" : "> --计划已记录--"];
    var status = String(d.status || "").trim();
    if (action === "clear") return lines.join("\n") + (options.trailingNewline === false ? "" : "\n");
    if (action === "set_step_status") {
      var updates = Array.isArray(d.step_updates) ? d.step_updates : [];
      if (updates.length > 0) {
        lines.push("> **更新步骤**:");
        updates.forEach(function(update) {
          if (!update || typeof update !== "object") return;
          var idx = Number(update.index || 0);
          var text = String(update.text || "").trim();
          var stepStatus = String(update.status || "pending").trim();
          var label = stepStatus === "done" ? "完成" : stepStatus === "in_progress" ? "进行中" : stepStatus === "blocked" ? "阻塞" : "待办";
          var target = idx > 0 ? "第 " + idx + " 步" : text || "匹配步骤";
          lines.push("> " + target + " → " + label);
        });
      }
      var updateNote = String(d.update_note || d.updateNote || "").trim();
      if (updateNote) lines.push("> **说明**: " + updateNote);
      return lines.join("\n") + (options.trailingNewline === false ? "" : "\n");
    }
    var completed = d.completed === true || status === "complete";
    if (completed) lines.push("> **状态**: 已完成");
    else if (status === "awaiting_completion_confirmation") lines.push("> **状态**: 已修订");
    else if (status) lines.push("> **状态**: " + status);
    var goal = String(d.goal || "").trim();
    if (goal) lines.push("> **目标**: " + goal);
    var schemaVersion = Number(d.schema_version || d.schemaVersion || 0);
    var allowLegacyTextStatus = !schemaVersion || schemaVersion < 2;
    var steps = Array.isArray(d.steps) ? d.steps : [];
    if (steps.length > 0) {
      lines.push("> **步骤**:");
      steps.forEach(function(step, idx) {
        if (!step) return;
        var text = typeof step === "object" ? String(step.text || "") : String(step || "");
        var stepStatus = String((step && typeof step === "object" ? step.status : "") || "").trim();
        var inferredStatus = allowLegacyTextStatus ? inferPlanStepStatus(text) : "";
        if (allowLegacyTextStatus && (!stepStatus || stepStatus === "pending") && inferredStatus) stepStatus = inferredStatus;
        text = allowLegacyTextStatus ? cleanPlanStepText(text) : text.replace(/^\s*[-*•]\s*/, "").replace(/^\s*\d{1,3}[.、)]\s*/, "").trim();
        var prefix = "";
        if (stepStatus === "done") prefix = "[完成] ";
        else if (stepStatus === "in_progress") prefix = "[进行中] ";
        else if (stepStatus === "blocked") prefix = "[阻塞] ";
        if (text) lines.push("> " + String(idx + 1) + ". " + prefix + text);
      });
    }
    var notes = String(d.notes || "").trim();
    if (notes) lines.push("> **备注**: " + notes);
    var completionNote = String(d.completion_note || d.completionNote || "").trim();
    if (completionNote) lines.push("> **完成说明**: " + completionNote);
    return lines.join("\n") + (options.trailingNewline === false ? "" : "\n");
  }

  async function renderSession(entries, options) {
    options = options || {};
    var token = ++activeRenderToken;
    var chunkSize = Math.max(8, Math.min(Number(options.chunkSize || 18) || 18, 40));
    var preserveScroll = !!options.preserveScroll;
    var prevHeight = 0;
    var prevTop = 0;

    if (!Array.isArray(entries) || entries.length === 0) {
      if (typeof showEmptyState === "function") showEmptyState();
      return;
    }
    if (typeof clearEmptyState === "function") clearEmptyState();
    if (typeof messagesEl !== "undefined" && messagesEl) {
      var wrap = (typeof messagesWrap !== "undefined" && messagesWrap) ? messagesWrap : null;
      if (preserveScroll && wrap) {
        prevHeight = wrap.scrollHeight;
        prevTop = wrap.scrollTop;
      }
      resetTurns();
      messagesEl.innerHTML = "";
      messagesEl.dataset.hydrating = "1";
    }
    try {
      for (var i = 0; i < entries.length; i++) {
        if (token !== activeRenderToken) return false;
        var entry = entries[i] || {};
        var target = String(entry.display_target || "chat").trim();
        if (target && target !== "chat") continue;
        var type = String(entry.type || "").trim();
        var role = String(entry.role || "").trim();
        if (type === "summary" || type === "system_notice") renderSystemNotice(entry);
        else if (type === "user_message" || role === "user") renderUserBubble(entry);
        else if (type === "assistant_message" || type === "tool_marker" || role === "assistant") renderAssistantBubble(entry);
        else if (role === "system") renderSystemNotice(entry);
        if ((i + 1) % chunkSize === 0) {
          await nextFrame();
        }
      }
    } finally {
      if (token === activeRenderToken && typeof messagesEl !== "undefined" && messagesEl) {
        delete messagesEl.dataset.hydrating;
      }
    }
    if (token !== activeRenderToken) return false;
    if (preserveScroll && typeof messagesWrap !== "undefined" && messagesWrap) {
      var delta = messagesWrap.scrollHeight - prevHeight;
      messagesWrap.scrollTop = Math.max(0, prevTop + delta);
    } else if (typeof scrollToBottom === "function") {
      scrollToBottom();
    }
    return true;
  }

  function _extractText(content) {
    if (!content) return "";
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      // Flatten array sub-steps to text
      return content.map(function(s) {
        if (!s) return "";
        if (typeof s === "string") return s;
        if (s.kind === "text" || s.kind === "thinking") return String(s.data || "");
        if (s.kind === "tool_marker") return "";
        if (s.kind === "system") return _renderSystemSubstep(s);
        return String(s.data || s || "");
      }).filter(Boolean).join("\n\n");
    }
    if (typeof content === "object") {
      return content.user || content.text || content.content || "";
    }
    return String(content);
  }

  function renderUserBubble(entry) {
    var content = entry.content;
    if (Array.isArray(content)) {
      content.forEach(function (step) {
        if (!step) return;
        var kind = step.kind || "";
        if (kind === "file") {
          var d = step.data || {};
          if (typeof d !== "object") d = {};
          var fn = d.file_name || d.name || "";
          if (fn) renderFileChip(fn);
        } else {
          var t = String((step.data && step.data.text) || step.data || step || "");
          if (t.trim()) renderBubble(t.trim(), "user");
        }
      });
    } else {
      var text = _extractText(content);
      if (text.trim()) renderBubble(text.trim(), "user");
    }
  }

  function renderAssistantBubble(entry) {
    var content = entry.content;
    var parts = [];
    if (Array.isArray(content)) {
      content.forEach(function (step) {
        if (!step) return;
        var kind = step.kind || "";
        if (kind === "tool_marker") {
          var toolText = _renderToolMarker(step);
          if (toolText) parts.push(toolText);
        } else {
          if (kind === "thinking") {
            parts.push(markdownQuoteBlock(step.data));
          } else if (kind === "text") {
            parts.push(String(step.data || ""));
          } else if (kind === "system") {
            parts.push(_renderSystemSubstep(step));
          } else {
            parts.push(String(step.data || step || ""));
          }
        }
      });
    } else {
      parts.push(_extractText(content));
    }
    var text = parts.filter(function(p) { return p.trim(); }).join("\n\n");
    if (String(text || "").trim() === STREAM_DRAFT_PLACEHOLDER) return;
    if (text.trim()) renderBubble(text, "bot", { turnId: entry.turn_id || entry.turnId || "" });
  }

  function _renderToolMarker(step) {
    if (!step) return "";
    var d = step.data || {};
    if (typeof d !== "object") d = {};
    var name = d.name || d.tool_name || "unknown";
    var cid = d.id || d.call_id || "";
    var status = String(d.status || "").trim().toLowerCase();
    var text = renderToolMarkerMarkdown(d, {
      done: status !== "running",
      trailingNewline: false,
    });
    if (typeof renderToolOutputToTerminal === "function") {
      var output = d.stdout || d.stderr || "";
      if (output) renderToolOutputToTerminal(name, cid, output, d.ok);
    }
    return text;
  }

  function _renderSystemSubstep(step) {
    if (!step) return "";
    var d = step.data || {};
    if (typeof d !== "object") d = { text: String(d || "") };
    var text = String(d.text || d.content || d.summary || "").trim();
    if (!text) return "";
    return text;
  }

  function renderSystemNotice(entry) {
    var text = _extractText(entry.content);
    if (!text.trim()) return;
    var el = document.createElement("div");
    el.className = "sys-notice";
    el.textContent = text;
    if (typeof messagesEl !== "undefined" && messagesEl) {
      messagesEl.appendChild(el);
      var hydrating = typeof isHydratingMessages === "function" && isHydratingMessages();
      if (!hydrating && typeof scrollToBottom === "function") scrollToBottom();
    }
  }

  // Expose
  window.ChatBubbleRenderer = {
    renderBubble: renderBubble,
    renderFileChip: renderFileChip,
    renderTyping: renderTyping,
    createRow: createPlainRow,
    updateBubbleMarkdown: updateBubbleMarkdown,
    appendMarkdownToBubble: appendMarkdownToBubble,
    upsertAssistantBubble: upsertAssistantBubble,
    renderToolMarkerMarkdown: renderToolMarkerMarkdown,
    renderPlanMarkerMarkdown: renderPlanMarkerMarkdown,
    rememberTurnBubble: rememberTurn,
    resolveTurnBubble: resolveTurnBubble,
    resetTurnBubbles: resetTurns,
  };
  window.renderSession = renderSession;
  window.renderUserBubble = renderUserBubble;
  window.renderAssistantBubble = renderAssistantBubble;
  window.renderSystemNotice = renderSystemNotice;
})();
