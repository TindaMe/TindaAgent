/**
 * Extracted from chat.html: chat_init.js
 */

  // --- Init ---
  if (!getAuthToken()) {
    location.href = "/";
  }
  startInitialChatLoading();
  renderHeaderStatus();
  startGatewayHeartbeat();
  const savedWidth = Number(localStorage.getItem(TERM_WIDTH_KEY));
  if (Number.isFinite(savedWidth) && savedWidth >= 180) {
    termWidth = Math.min(savedWidth, Math.max(180, Math.floor(window.innerWidth * 0.65)));
  }
  const savedTimeMode = localStorage.getItem(TIME_MODE_KEY);
  if (savedTimeMode) timeMode = savedTimeMode;
  const savedCustomTime = localStorage.getItem(TIME_CUSTOM_KEY);
  if (savedCustomTime) customTimeValue = savedCustomTime;
  setTimeMode(timeMode, { silent: true });
  webSearchEnabled = localStorage.getItem(WEB_SEARCH_ENABLED_KEY) === "1";
  deepEnabled = localStorage.getItem(DEEP_ENABLED_KEY) === "1";
  setDeepToggleUI();
  renderComposerSelections();
  (async () => {
    let shouldReleaseInitialRender = false;
    try {
      const authRes = await apiFetch("/auth/status");
      const authData = authRes.ok ? await authRes.json() : {};
      if (!authRes.ok || !authData?.logged_in) {
        location.href = "/";
        return;
      }
      shouldReleaseInitialRender = true;
      await ensureUserMetaLoaded();
      const settings = await loadWebSettings({ force: true });
      streamEnabled = settings.stream_enabled !== false;
      setStreamToggleUI();
      if (settings.terminal_open === true) openTerm();
      if (isValidContextTokenLimit(settings.token_limit)) {
        const sid = getSessionId();
        if (sid && !isDraftSessionId(sid)) {
          syncContextTokenLimitForSession(sid).catch(() => {});
        }
      }
      await loadAccountList();
      renderQuickButtons();
      await Promise.allSettled([loadModelInfo(), syncAppVersion()]);
      const sid = await restoreExistingSessionForBoot();
      if (sid) {
        await loadCurrentSessionRecord({ sessionId: sid });
        startToolPolling(sid);
      } else {
        enterDraftSession({ title: "新会话", clearTerminal: true });
      }
    } catch (e) {
      if (!shouldReleaseInitialRender) {
        location.href = "/";
        return;
      }
      reportErrorToTerminal(`初始化聊天页失败：${String(e?.message || e)}`, { source: "chat_boot" });
      if (messagesEl && messagesEl.childElementCount === 0) showEmptyState();
    } finally {
      if (shouldReleaseInitialRender) await releaseInitialChatRender();
    }
  })();
  updateComposerBottomSpace();
  closeTerm();
  refreshComposerDisabledState();
  inputEl.focus();
