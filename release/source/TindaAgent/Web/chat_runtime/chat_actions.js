/**
 * Extracted from chat.html: chat_actions.js
 */

  // --- Actions ---
  async function recoverInterruptedStream({ sid, turnId, streamText, fallbackMessage = "" } = {}) {
    const cleanSid = String(sid || "").trim();
    const cleanTurn = normalizeTurnId(turnId || "");
    if (!cleanSid || isDraftSessionId(cleanSid)) return false;
    try {
      const loaded = await loadCurrentSessionRecord({ sessionId: cleanSid });
      if (!loaded) {
        if (String(streamText || "").trim()) {
          upsertAssistantTurnBubble(String(streamText || "").trim(), { turnId: cleanTurn });
          return true;
        }
        if (fallbackMessage) addBubble(fallbackMessage, "bot");
        return false;
      }
      const existingBubble = cleanTurn ? resolveAssistantTurnBubble(cleanTurn) : null;
      const escapedTurn = cleanTurn && window.CSS?.escape ? CSS.escape(cleanTurn) : cleanTurn.replace(/"/g, '\\"');
      const recovered = !!existingBubble || (escapedTurn ? !!document.querySelector(`[data-turn-id="${escapedTurn}"]`) : false);
      if (!recovered && String(streamText || "").trim()) {
        upsertAssistantTurnBubble(String(streamText || "").trim(), { turnId: cleanTurn });
      }
      showToast(recovered ? "网络中断，已恢复本轮回复" : "网络中断，已恢复当前会话", "warn");
      return true;
    } catch (error) {
      if (String(streamText || "").trim()) {
        upsertAssistantTurnBubble(String(streamText || "").trim(), { turnId: cleanTurn });
      } else if (fallbackMessage) {
        addBubble(fallbackMessage, "bot");
      }
      return false;
    }
  }

  async function sendMessage(options = {}) {
    options = options || {};
    if (syncTerminalConfirmLockFromDom() > 0) {
      showToast(pendingConfirmLockMessage(), "warn");
      inputEl?.focus();
      return;
    }
    const hasMessageOverride = Object.prototype.hasOwnProperty.call(options, "messageOverride");
    const text = hasMessageOverride ? String(options.messageOverride || "").trim() : inputEl.value.trim();
    const overrideFileNames = Array.isArray(options.fileNamesOverride) ? options.fileNamesOverride : null;
    const overrideFileContents = Array.isArray(options.fileContentsOverride) ? options.fileContentsOverride : null;
    const hasFileOverride = !!overrideFileNames || !!overrideFileContents;
    const hasFile = hasFileOverride ? ((overrideFileNames || []).length > 0) : importedFiles.length > 0;
    if (!text && !hasFile) return;
    let fullMessage = text;
    const fileBackup = hasFile && !hasFileOverride ? importedFiles.slice() : null;
    const file_names = hasFileOverride
      ? (overrideFileNames || []).map((x) => String(x || ""))
      : (hasFile ? importedFiles.map(function(f) { return f.name; }) : []);
    const file_contents = hasFileOverride
      ? (overrideFileContents || []).map((x) => String(x || ""))
      : (hasFile ? importedFiles.map(function(f) { return f.content; }) : []);
    const previousSid = String(getStoredSessionId() || "").trim();
    const sid = await ensureSessionForUserMessage();
    const createdForThisMessage = !previousSid || isDraftSessionId(previousSid);

    await ensureUserMetaLoaded();

    clearEmptyState();
    inputEl.value = "";
    autoResize();
    setMessageSendInFlight(true);
    // 当前 DOM 进入实时对话态，避免“加载更早”重渲染覆盖正在流式生成的内容。
    historyPagingState.hasMore = false;
    removeHistoryLoadMoreControl();
    hydratedSessionEntries = [];

    if (hasFile && fileBackup) {
      fileBackup.forEach(function(f) { addFileChipBubble(f.name); });
      importedFiles = [];
      removeFile();
    } else if (hasFileOverride && !options.skipDeepEcho) {
      file_names.forEach(function(name) { if (name) addFileChipBubble(name); });
    }
    if (text && !options.skipDeepEcho) {
      addBubble(text, "user");
    }
    if (deepEnabled && !options.bypassDeep && !text.startsWith("/")) {
      await startDeepAlignment({
        sid,
        message: fullMessage,
        file_names,
        file_contents,
        createdForThisMessage,
      });
      return;
    }
    const echo = isEchoCmd(text);
    if (echo) { showTerm(); }
    // 所有输入统一走 SSE：命令与普通对话共享同一工具事件工作环。
    const useStream = true;
    const renderDelta = streamEnabled;
    const typingRow = useStream ? (renderDelta ? null : addTyping()) : addTyping();
    const metaPayload = buildRequestMeta();
    const clientTurnId = genClientTurnId();
    let streamBot = null;
    let streamTextForRecovery = "";
    let streamTurnIdForRecovery = clientTurnId;
    const clearTypingPlaceholder = () => {
      if (typingRow && typingRow.isConnected) typingRow.remove();
    };
    const clearDraftBotBubble = () => {
      if (streamBot?.row && streamBot.row.isConnected) streamBot.row.remove();
      streamBot = null;
    };

    try {
      if (useStream) {
        if (renderDelta) {
          streamBot = addBubble("", "bot");
          streamBot.bubble.classList.add("md");
        }
        const params = new URLSearchParams({
          message: fullMessage,
          session_id: sid,
          client_turn_id: clientTurnId,
        });
        params.set("web_search_enabled", webSearchEnabled ? "1" : "0");
        if (file_names.length > 0) {
          file_names.forEach(function(n, i) { params.append("file_names", n); });
          file_contents.forEach(function(c, i) { params.append("file_contents", c); });
        }
        if (options.deepAlignmentContext) {
          params.set("deep_alignment_context", String(options.deepAlignmentContext));
        }
        Object.entries(metaPayload).forEach(([k, v]) => params.set(k, String(v)));
        const url = `/chat/stream?${params.toString()}`;
        const res = await apiFetch(url, {
          method: "GET",
          headers: { "Accept": "text/event-stream" },
        });
        if (!res.ok || !res.body) throw new Error(`stream http ${res.status}`);

        const reader = res.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        let streamText = "";
        let streamSegmentStart = 0;
        let streamReasoning = "";
        let donePayload = null;
        const streamParts = [];
        const toolCallBlockKeyIndex = new Map();
        let streamRenderRaf = 0;
        let streamRenderTimer = 0;
        let streamRenderLastAt = 0;
        let streamRenderPending = "";

        function toolBlockMarkdown(name, cid, done, progress, toolCallId = "") {
          if (window.ChatBubbleRenderer?.renderToolMarkerMarkdown) {
            return window.ChatBubbleRenderer.renderToolMarkerMarkdown(
              { name: name || "unknown", id: cid || "", tool_call_id: toolCallId || "", progress: progress || "" },
              { done: !!done, progress: progress || "", tool_call_id: toolCallId || "" }
            );
          }
          return "";
        }

        function normalizeToolCallKey(value) {
          return String(value || "").trim();
        }

        function toolKeysFromPayload(payload) {
          if (!payload || typeof payload !== "object") return [];
          const keys = [
            payload.tool_call_id,
            payload.call_id,
            payload.id,
          ].map(normalizeToolCallKey).filter(Boolean);
          const name = normalizeToolCallKey(payload.agent_tool || payload.name || payload.tool_name);
          const args = payload.arguments && typeof payload.arguments === "object" ? payload.arguments : {};
          const argHint = normalizeToolCallKey(args.cmd || args.text || args.key || "");
          if (name && argHint) keys.push(`${name}:${argHint}`);
          return Array.from(new Set(keys));
        }

        function findToolBlockIndex(keys, name, hasStrongKey = false) {
          for (const key of keys) {
            if (toolCallBlockKeyIndex.has(key)) return toolCallBlockKeyIndex.get(key);
          }
          if (hasStrongKey) return -1;
          const cleanName = normalizeToolCallKey(name);
          if (cleanName) {
            const runningMatches = [];
            streamParts.forEach((part, index) => {
              if (part && part.type === "tool" && !part.done && part.name === cleanName) runningMatches.push(index);
            });
            if (runningMatches.length === 1) return runningMatches[0];
          }
          return -1;
        }

        function rememberToolBlockKeys(index, keys) {
          keys.forEach((key) => {
            if (key) toolCallBlockKeyIndex.set(key, index);
          });
        }

        function upsertToolBlock(payload, done, progress) {
          if (!payload || typeof payload !== "object") return false;
          const name = normalizeToolCallKey(payload.agent_tool || payload.name || payload.tool_name) || "unknown";
          const cid = normalizeToolCallKey(payload.call_id || payload.id || "");
          const toolCallId = normalizeToolCallKey(payload.tool_call_id || payload.toolCallId || "");
          const progressText = normalizeToolCallKey(progress || payload.progress || "");
          const keys = toolKeysFromPayload(payload);
          const hasStrongKey = !!(toolCallId || cid);
          const idx = findToolBlockIndex(keys, name, hasStrongKey);
          const nextBlock = {
            type: "tool",
            name,
            cid,
            toolCallId,
            done: !!done,
            progress: progressText,
            block: window.ChatBubbleRenderer?.renderToolMarkerMarkdown
              ? window.ChatBubbleRenderer.renderToolMarkerMarkdown(
                  { name, id: cid, tool_call_id: toolCallId, progress: progressText },
                  { done: !!done, progress: progressText, tool_call_id: toolCallId }
                )
              : toolBlockMarkdown(name, cid, !!done, progressText, toolCallId),
          };
          if (idx >= 0) {
            const prev = streamParts[idx] || {};
            const nextDone = prev.done || nextBlock.done;
            const nextName = prev.name || nextBlock.name;
            const nextCid = nextBlock.cid || prev.cid || "";
            const nextToolCallId = nextBlock.toolCallId || prev.toolCallId || "";
            const nextProgress = nextDone ? "" : (nextBlock.progress || prev.progress || "");
            streamParts[idx] = {
              type: "tool",
              name: nextName,
              cid: nextCid,
              toolCallId: nextToolCallId,
              done: nextDone,
              progress: nextProgress,
              block: window.ChatBubbleRenderer?.renderToolMarkerMarkdown
                ? window.ChatBubbleRenderer.renderToolMarkerMarkdown(
                    { name: nextName, id: nextCid, tool_call_id: nextToolCallId, progress: nextProgress },
                    { done: nextDone, progress: nextProgress, tool_call_id: nextToolCallId }
                  )
                : toolBlockMarkdown(nextName, nextCid, nextDone, nextProgress, nextToolCallId),
            };
            rememberToolBlockKeys(idx, keys);
          } else {
            flushCurrentStreamTextPart();
            streamParts.push(nextBlock);
            rememberToolBlockKeys(streamParts.length - 1, keys);
          }
          return true;
        }

        function flushCurrentStreamTextPart() {
          const text = String(streamText || "");
          if (text.trim()) {
            streamParts.push({ type: "text", text });
          }
          streamText = "";
          streamSegmentStart = 0;
        }

        function buildStreamTextWithToolBlocks() {
          const parts = [];
          streamParts.forEach((part) => {
            const text = part?.type === "tool" ? String(part.block || "") : String(part?.text || "");
            if (text.trim()) parts.push(text.trim());
          });
          const current = String(streamText || "").trim();
          if (current) parts.push(current);
          return parts.join("\n\n");
        }

        function rememberStreamRecoveryText(value = buildStreamTextWithToolBlocks()) {
          streamTextForRecovery = String(value || "");
          streamTurnIdForRecovery = normalizeTurnId(donePayload?.turn_id || streamTurnIdForRecovery || clientTurnId);
        }

        function flushStreamReasoningToText() {
          if (!streamReasoning) return false;
          if (streamText && !streamText.endsWith("\n\n")) streamText += "\n";
          streamText += markdownQuoteBlock(streamReasoning) + "\n\n";
          streamReasoning = "";
          streamSegmentStart = streamText.length;
          return true;
        }

        function replaceCurrentStreamSegment(content) {
          flushStreamReasoningToText();
          const replaceStart = Math.max(0, Math.min(streamSegmentStart, streamText.length));
          const cleanSegment = String(content || "");
          streamText = streamText.slice(0, replaceStart) + cleanSegment;
          streamSegmentStart = streamText.length;
        }

        function commitStreamBubbleRender(text) {
          if (!renderDelta || !streamBot) return;
          const displayText = String(text ?? "");
          window.ChatBubbleRenderer?.updateBubbleMarkdown?.(streamBot.bubble, displayText);
          rememberStreamRecoveryText(displayText);
          streamRenderLastAt = performance.now();
        }

        function queueStreamBubbleRender(text, { force = false } = {}) {
          if (!renderDelta || !streamBot) return;
          streamRenderPending = String(text ?? "");
          if (force) {
            if (streamRenderTimer) {
              clearTimeout(streamRenderTimer);
              streamRenderTimer = 0;
            }
            if (streamRenderRaf) {
              cancelAnimationFrame(streamRenderRaf);
              streamRenderRaf = 0;
            }
            commitStreamBubbleRender(streamRenderPending);
            return;
          }
          if (streamRenderRaf || streamRenderTimer) return;
          const elapsed = performance.now() - streamRenderLastAt;
          const wait = Math.max(0, STREAM_RENDER_INTERVAL_MS - elapsed);
          const schedule = () => {
            streamRenderTimer = 0;
            streamRenderRaf = requestAnimationFrame(() => {
              streamRenderRaf = 0;
              commitStreamBubbleRender(streamRenderPending);
            });
          };
          if (wait > 0) {
            streamRenderTimer = setTimeout(schedule, wait);
          } else {
            schedule();
          }
        }

        function refreshToolMarkerNow() {
          if (!renderDelta || !streamBot) return;
          if (streamRenderTimer) {
            clearTimeout(streamRenderTimer);
            streamRenderTimer = 0;
          }
          if (streamRenderRaf) {
            cancelAnimationFrame(streamRenderRaf);
            streamRenderRaf = 0;
          }
          commitStreamBubbleRender(buildStreamTextWithToolBlocks());
        }

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          let idx;
          while ((idx = buffer.indexOf("\n\n")) >= 0) {
            const rawEvent = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            if (!rawEvent.trim()) continue;

            let eventName = "message";
            let dataText = "";
            rawEvent.split("\n").forEach((line) => {
              if (line.startsWith("event:")) eventName = line.slice(6).trim();
              if (line.startsWith("data:")) dataText += line.slice(5).trim();
            });

            let payload = {};
            if (dataText) {
              try { payload = JSON.parse(dataText); } catch { payload = {}; }
            }

            if (eventName === "delta") {
              flushStreamReasoningToText();
              streamText += String(payload.content || "");
              rememberStreamRecoveryText();
              if (renderDelta && streamBot) {
                const cur = _streamDisplay(streamReasoning, buildStreamTextWithToolBlocks());
                queueStreamBubbleRender(cur);
              }
            } else if (eventName === "reasoning_delta") {
              streamReasoning += String(payload.content || "");
              rememberStreamRecoveryText(_streamDisplay(streamReasoning, buildStreamTextWithToolBlocks()));
              if (renderDelta && streamBot) {
                const cur = _streamDisplay(streamReasoning, buildStreamTextWithToolBlocks());
                queueStreamBubbleRender(cur);
              }
            } else if (eventName === "replace_segment") {
              replaceCurrentStreamSegment(payload.content);
              rememberStreamRecoveryText();
              if (renderDelta && streamBot) {
                queueStreamBubbleRender(buildStreamTextWithToolBlocks(), { force: true });
              }
            } else if (eventName === "tool_call_start") {
              flushStreamReasoningToText();
              const calls = Array.isArray(payload.calls) ? payload.calls : [];
              let changed = false;
              calls.forEach(function(call) {
                if (!call || typeof call !== "object") return;
                if (upsertToolBlock(call, false)) changed = true;
              });
              if (!changed) continue;
              streamSegmentStart = streamText.length;
              refreshToolMarkerNow();
            } else if (eventName === "tool_step") {
              const trace = Array.isArray(payload?.trace) ? payload.trace : [];
              if (trace.length > 0) {
                renderToolTraceToTerminal(trace, { sessionId: sid, persist: false });
                trace.forEach(function(step) {
                  if (!step || typeof step !== "object") return;
                  upsertToolBlock(step, true);
                });
                streamSegmentStart = streamText.length;
                rememberStreamRecoveryText();
                refreshToolMarkerNow();
              }
            } else if (eventName === "tool_heartbeat") {
              const hbTool = payload.tool && typeof payload.tool === "object" ? payload.tool : {};
              const hbName = normalizeToolCallKey(hbTool.name || hbTool.agent_tool || "");
              const hasRunningTool = streamParts.some((part) => part && part.type === "tool" && !part.done);
              const elapsed = Math.max(0, Number(payload.elapsed_ms || 0));
              const seconds = elapsed > 0 ? Math.max(1, Math.floor(elapsed / 1000)) : 0;
              const progress = seconds > 0 ? `连接中 / 执行中 · 已等待 ${seconds}s` : "连接中 / 执行中";
              if (hbName || hasRunningTool) {
                const heartbeatPayload = {
                  agent_tool: hbName || "unknown",
                  tool_call_id: hbTool.tool_call_id || "",
                  call_id: hbTool.call_id || "",
                  arguments: hbTool.arguments || {},
                  progress,
                };
                upsertToolBlock(heartbeatPayload, false, progress);
              }
              refreshToolMarkerNow();
            } else if (eventName === "done") {
              donePayload = payload;
              streamTurnIdForRecovery = normalizeTurnId(payload?.turn_id || streamTurnIdForRecovery || clientTurnId);
            } else if (eventName === "error") {
              const pendingCount = Math.max(0, Number(payload?.pending_confirm_count || 0));
              if (pendingCount > 0) {
                setTerminalConfirmLock(true, pendingCount);
                await syncPendingConfirmationsFromServer(sid, { silent: true });
              }
              throw new Error(payload.message || "stream failed");
            }
          }
        }

        const doneTurnId = normalizeTurnId(donePayload?.turn_id || "");
        streamTurnIdForRecovery = normalizeTurnId(doneTurnId || streamTurnIdForRecovery || clientTurnId);
        if (donePayload) {
            const compressionText = absorbContextCompressionPayload(donePayload.context_compression);
            // Flush any remaining accumulated reasoning
            flushStreamReasoningToText();
            // Backfill reasoning from done payload (only if no streaming reasoning happened)
            if (!buildStreamTextWithToolBlocks().trim() && donePayload.reasoning_content) {
              const rc = String(donePayload.reasoning_content || "").trim();
              if (rc) {
                streamText = markdownQuoteBlock(rc) + "\n\n";
                streamSegmentStart = streamText.length;
              }
            }
            // Inject tool details from donePayload into streamText (avoid duplicate)
            const toolTrace = Array.isArray(donePayload.tool_trace) ? donePayload.tool_trace : [];
            if (toolTrace.length > 0) {
              showPlanPanelsFromTrace(toolTrace);
              toolTrace.forEach(function(step) {
                if (!step || typeof step !== "object") return;
                upsertToolBlock(step, true);
              });
            }
            streamText = buildStreamTextWithToolBlocks();
            // delta 已完整构建 streamText，done 仅补 turn_id，不再追加文本
            if (typeof donePayload.reply === "string") {
              if (!streamText) {
                streamText = sanitizeAssistantDisplayText(String(donePayload.reply || ""));
              }
              if (renderDelta && streamBot) {
                queueStreamBubbleRender(streamText, { force: true });
                if (doneTurnId) {
                  streamBot.bubble.dataset.turnId = doneTurnId;
                  rememberAssistantTurnBubble(doneTurnId, streamBot.bubble);
                }
              }
            } else if (!streamText && donePayload.reply) {
              streamText = sanitizeAssistantDisplayText(String(donePayload.reply));
              if (renderDelta && streamBot) {
                queueStreamBubbleRender(streamText, { force: true });
                if (doneTurnId) {
                  streamBot.bubble.dataset.turnId = doneTurnId;
                  rememberAssistantTurnBubble(doneTurnId, streamBot.bubble);
                }
              }
            }
            if (compressionText) {
              if (streamText && !streamText.endsWith("\n\n")) streamText += "\n\n";
              streamText += compressionText;
            }
          rememberStreamRecoveryText(streamText);
          if (renderDelta && streamBot && streamText) {
            queueStreamBubbleRender(streamText, { force: true });
          }
          clearTypingPlaceholder();
          if (!renderDelta) {
            const fallbackText = streamText || String(donePayload.reply || "（无回复）");
            upsertAssistantTurnBubble(fallbackText || "（无回复）", { turnId: doneTurnId });
            rememberStreamRecoveryText(fallbackText);
          }
          if (donePayload.tool_async) {
            startToolPolling(sid);
          } else {
            if (!renderDelta) {
              const traced = renderToolTraceToTerminal(donePayload.tool_trace, { sessionId: sid, persist: false });
              if ((donePayload.tool_steps || 0) > 0 && !traced) {
                showToast("本轮工具调用没有终端输出", "warn");
              }
            }
            stopToolPolling();
          }
          const donePendingCount = Math.max(0, Number(donePayload?.pending_confirm_count || 0));
          if (donePendingCount > 0 || donePayload?.pending_confirmation === true) {
            await syncPendingConfirmationsFromServer(sid, { silent: true });
          } else {
            clearPendingConfirm();
          }
        } else if (typingRow) {
          clearTypingPlaceholder();
          if (!renderDelta) {
            upsertAssistantTurnBubble(streamText || "（无回复）", { turnId: doneTurnId });
            rememberStreamRecoveryText(streamText || "（无回复）");
          }
        }
        return;
      }

      const res = await apiFetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: fullMessage,
          session_id: sid,
          client_turn_id: clientTurnId,
          file_names: file_names,
          file_contents: file_contents,
          deep_alignment_context: String(options.deepAlignmentContext || ""),
          web_search_enabled: webSearchEnabled,
          ...metaPayload,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        const pendingCount = Math.max(0, Number(data?.pending_confirm_count || 0));
        if (pendingCount > 0) {
          setTerminalConfirmLock(true, pendingCount);
          await syncPendingConfirmationsFromServer(sid, { silent: true });
        }
        throw new Error(String(data?.detail || data?.error || `http ${res.status}`));
      }
      clearTypingPlaceholder();
      if (data?.tool_async) {
        const replyText = String(data.reply || "> >_<\n> --调用工具中--");
        upsertAssistantTurnBubble(replyText || "（无回复）", { turnId: data?.turn_id, isCommand: true });
        if (data?.tool_job?.job_id) startToolPolling(sid);
      } else if (echo) {
        const reply = data.reply ?? "";
        addLimitedTermLines(reply, "", { sessionId: sid, title: "echo 输出" });
        if (Array.isArray(data.tool_trace) && data.tool_trace.length > 0) {
          addTermSep({ sessionId: sid });
          addTermLine("out", "(已跳过重复工具轨迹渲染)", "dim", { sessionId: sid });
        }
        showToast("已输出至终端");
      } else {
        const reasoningText = String(data.reasoning_content || "").trim();
        const replyText = String(data.reply ?? "（无回复）");
        var displayText = replyText;
        if (reasoningText) {
          displayText = markdownQuoteBlock(reasoningText) + "\n\n" + replyText;
        }
        const compressionText = absorbContextCompressionPayload(data.context_compression);
        if (compressionText) {
          displayText = String(displayText || "").trim()
            ? `${displayText}\n\n${compressionText}`
            : compressionText;
        }
        upsertAssistantTurnBubble(displayText || "（无回复）", { turnId: data?.turn_id, isCommand: text.startsWith("/") });
        // 工具轨迹由后端同轮次写入会话，前端这里仅做实时展示（不落盘），避免刷新后位置漂移。
        const traced = renderToolTraceToTerminal(data.tool_trace, { sessionId: sid, persist: false });
        if (data.tool_steps > 0 && !traced) {
          showToast("本轮工具调用没有终端输出", "warn");
        }
      }
      const responsePendingCount = Math.max(0, Number(data?.pending_confirm_count || 0));
      if (responsePendingCount > 0 || data?.pending_confirmation === true) {
        await syncPendingConfirmationsFromServer(sid, { silent: true });
      } else {
        clearPendingConfirm();
      }
    } catch (e) {
      const errText = String(e?.message || e || "");
      clearTypingPlaceholder();
      clearDraftBotBubble();
      if (errText.includes("待确认终端命令")) {
        showToast(errText, "warn");
        await syncPendingConfirmationsFromServer(sid, { silent: true });
      } else {
        reportErrorToTerminal(`请求失败：${errText}`, { source: "chat", sessionId: sid });
        if (isTransientFetchError(e)) {
          const recovered = await recoverInterruptedStream({
            sid,
            turnId: streamTurnIdForRecovery,
            streamText: streamTextForRecovery,
            fallbackMessage: "请求中断，已保留当前会话。请稍后刷新记录查看结果。",
          });
          if (!recovered && !echo && !streamTextForRecovery.trim()) {
            addBubble("请求中断，已保留当前会话。请稍后刷新记录查看结果。", "bot");
          }
        } else if (!echo) {
          addBubble("请求失败，请确认服务已启动。", "bot");
        }
        if (!isTransientFetchError(e) && createdForThisMessage && String(getStoredSessionId() || "") === sid) {
          clearActiveSessionState({ title: "新会话" });
        }
      }
    } finally {
      setMessageSendInFlight(false);
      if (String(getStoredSessionId() || "") === sid) {
        refreshContextUsageLength(sid).catch(() => {});
        refreshRecords().catch(() => {});
      }
      inputEl.focus();
      removeFile();
    }
  }

  async function resetChat() {
    const sid = String(getSessionId() || "").trim();
    if (!sid || isDraftSessionId(sid)) {
      enterDraftSession({ title: "新会话", clearTerminal: true });
      return;
    }
    try {
      const res = await apiFetch("/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sid }),
      });
      const data = await res.json();
      if (!res.ok || data?.ok === false) throw new Error(String(data?.error || `reset failed: ${res.status}`));
      closeRecordsPanel();
      clearEmptyState();
      clearTerm();
      clearPendingConfirm();
      addSystemNotice("上下文已清理，当前会话已开始新上下文", { pinTop: true, sessionId: sid });
      await refreshRecords();
      startToolPolling(sid);
    } catch (e) {
      reportErrorToTerminal(`上下文清理失败：${String(e?.message || e)}`, { source: "reset" });
      clearEmptyState();
      addSystemNotice("清理失败，请检查服务是否运行");
    }
  }

  function handleTermBodyClick(event) {
    const target = event?.target;
    const btn = target instanceof Element ? target.closest(".term-full-btn") : null;
    if (!btn || !termBodyEl?.contains(btn)) return;
    event.preventDefault();
    event.stopPropagation();
    openTermFullCard(btn.getAttribute("data-term-full-id") || "", btn.getAttribute("data-term-full-title") || "");
  }
