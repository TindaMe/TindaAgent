/**
 * Extracted from chat.html: chat_events.js
 */

  // --- Events ---
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  inputEl.addEventListener("input", autoResize);
  termBodyEl?.addEventListener("click", handleTermBodyClick);
  termFullCloseBtnEl?.addEventListener("click", closeTermFullCard);
  termFullOverlayEl?.addEventListener("click", (e) => {
    if (e.target === termFullOverlayEl) closeTermFullCard();
  });
  pendingConfirmAllowBtnEl?.addEventListener("click", () => {
    submitPendingConfirmation(true);
  });
  pendingConfirmDenyBtnEl?.addEventListener("click", () => {
    submitPendingConfirmation(false);
  });
  planFloatHeadEl?.addEventListener("mousedown", startPlanFloatDrag);
  document.addEventListener("mousemove", movePlanFloatDrag);
  document.addEventListener("mouseup", stopPlanFloatDrag);
  planFloatCollapseBtnEl?.addEventListener("click", togglePlanFloatCollapsed);
  planFloatCloseBtnEl?.addEventListener("click", deleteCurrentPlan);
  recordsSearchInputEl?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      refreshRecords();
    }
  });
  modelSwitchBtnEl?.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleModelPanel();
  });
  deepBtnEl?.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleDeepMode();
  });
  composerPlusBtnEl?.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleComposerMenu();
  });
  webSearchBtnEl?.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleWebSearchMode();
  });
  composerSelectedRowEl?.addEventListener("click", (e) => {
    const btn = e.target instanceof Element ? e.target.closest("[data-composer-remove]") : null;
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const kind = String(btn.getAttribute("data-composer-remove") || "");
    if (kind === "web-search") setWebSearchEnabled(false);
    if (kind === "deep") {
      deepEnabled = false;
      localStorage.setItem(DEEP_ENABLED_KEY, "0");
      setDeepToggleUI();
      addSystemNotice("Deep 对齐已关闭", { persist: false });
    }
  });
  timeModeBtnEl?.addEventListener("click", (e) => {
    e.stopPropagation();
    closeModelPanel();
    if (!timeModePanelEl) return;
    if (timeModePanelEl.classList.contains("open")) {
      closeTimePanel();
    } else {
      openAnimatedPanel(timeModePanelEl, "time", "open");
    }
  });
  timeModePanelEl?.querySelectorAll(".time-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.getAttribute("data-mode") || "now";
      setTimeMode(mode);
      if (mode !== "custom") closeTimePanel();
    });
  });
  timeCustomApplyBtnEl?.addEventListener("click", () => {
    const picked = String(timeCustomInputEl?.value || "").trim();
    if (!picked || !parseDateTimeLocal(picked)) {
      reportErrorToTerminal("自定义时间无效，请重新选择", { source: "time_custom" });
      addSystemNotice("自定义时间无效，请重新选择");
      return;
    }
    customTimeValue = picked;
    localStorage.setItem(TIME_CUSTOM_KEY, customTimeValue);
    setTimeMode("custom");
    closeTimePanel();
  });
  document.addEventListener("click", (e) => {
    const target = e.target;
    if (!(target instanceof Node)) return;
    const link = target instanceof Element ? target.closest('a[href^="toolskip:"]') : null;
    if (link) {
      e.preventDefault();
      e.stopPropagation();
      skipRunningToolCall(link.getAttribute("href") || "");
      return;
    }
    if (timeModePanelEl && timeModeBtnEl) {
      if (!(timeModePanelEl.contains(target) || timeModeBtnEl.contains(target))) {
        closeTimePanel();
      }
    }
    if (inputBoxEl && composerMenuEl && composerPlusBtnEl) {
      if (!(composerMenuEl.contains(target) || composerPlusBtnEl.contains(target))) {
        closeComposerMenu();
      }
    }
    if (modelPanelEl) {
      const mb = document.getElementById("modelSwitchBtn");
      if (mb && !(modelPanelEl.contains(target) || mb.contains(target))) {
        closeModelPanel();
      }
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeTermFullCard();
      closeTimePanel();
      closeModelPanel();
      closeRecordsPanel();
    }
  });
  window.addEventListener("resize", () => {
    scheduleRecalcPinnedSpacerHeight();
    updateComposerBottomSpace();
  });
  termPanelEl.addEventListener("transitionend", (e) => {
    if (e.propertyName === "width") scheduleRecalcPinnedSpacerHeight();
  });

  // 桌面滚轮：到顶/到底后硬边界停止，不再回弹
  messagesWrap.addEventListener("wheel", (e) => {
    const deltaY = e.deltaY;
    if (!deltaY) return;
    const currentTop = messagesWrap.scrollTop;
    const maxUseful = getMaxUsefulScroll();

    if (deltaY > 0 && currentTop >= maxUseful - 0.5) {
      e.preventDefault();
      messagesWrap.scrollTop = maxUseful;
      return;
    }
    if (deltaY < 0 && currentTop <= 0.5) {
      e.preventDefault();
      messagesWrap.scrollTop = 0;
      return;
    }
  }, { passive: false });

  // 触摸/拖拽滚动兜底：强制夹紧到有效范围
  messagesWrap.addEventListener("scroll", () => {
    clampMessagesScrollWithinBounds();
  }, { passive: true });
