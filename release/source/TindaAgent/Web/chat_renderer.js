/**
 * chat_renderer.js — new-format session message renderer.
 * Load after chat.html (which defines addBubble, renderMarkdown, messagesEl, etc.)
 */
(function () {
  "use strict";

  function renderSession(entries) {
    if (!Array.isArray(entries) || entries.length === 0) {
      if (typeof showEmptyState === "function") showEmptyState();
      return;
    }
    if (typeof clearEmptyState === "function") clearEmptyState();
    if (typeof messagesEl !== "undefined" && messagesEl) {
      messagesEl.innerHTML = "";
      messagesEl.dataset.hydrating = "1";
    }
    try {
      entries.forEach(function (entry) {
        var role = (entry.role || "").trim();
        if (role === "user") renderUserBubble(entry);
        else if (role === "assistant") renderAssistantBubble(entry);
        else if (role === "system") renderSystemNotice(entry);
      });
    } finally {
      if (typeof messagesEl !== "undefined" && messagesEl) {
        delete messagesEl.dataset.hydrating;
      }
    }
    if (typeof scrollToBottom === "function") scrollToBottom();
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
          if (fn && typeof addFileChipBubble === "function") addFileChipBubble(fn);
        } else {
          var t = String((step.data && step.data.text) || step.data || step || "");
          if (t.trim() && typeof addBubble === "function") addBubble(t.trim(), "user");
        }
      });
    } else {
      var text = _extractText(content);
      if (text.trim() && typeof addBubble === "function") addBubble(text.trim(), "user");
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
            parts.push("> " + String(step.data || "").split("\n").join("\n> "));
          } else if (kind === "text") {
            parts.push(String(step.data || ""));
          } else {
            parts.push(String(step.data || step || ""));
          }
        }
      });
    } else {
      parts.push(_extractText(content));
    }
    var text = parts.filter(function(p) { return p.trim(); }).join("\n\n");
    if (text.trim() && typeof addBubble === "function") addBubble(text, "bot");
  }

  function _renderToolMarker(step) {
    if (!step) return "";
    var lines = ["> >_<", "> --调用工具中--"];
    var d = step.data || {};
    if (typeof d !== "object") d = {};
    var name = d.name || d.tool_name || "unknown";
    var cid = d.id || d.call_id || "";
    var status = String(d.status || "").trim().toLowerCase();
    if (status === "running") {
      lines.push("> **准备调用工具**: " + name);
    } else {
      lines.push("> **已调用工具**: " + name + (cid ? " #" + cid : ""));
    }
    if (typeof renderToolOutputToTerminal === "function") {
      var output = d.stdout || d.stderr || "";
      if (output) renderToolOutputToTerminal(name, cid, output, d.ok);
    }
    return lines.join("\n");
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
  window.renderSession = renderSession;
  window.renderUserBubble = renderUserBubble;
  window.renderAssistantBubble = renderAssistantBubble;
  window.renderSystemNotice = renderSystemNotice;
})();
