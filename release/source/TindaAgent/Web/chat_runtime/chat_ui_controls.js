/**
 * Extracted from chat.html: chat_ui_controls.js
 */

  function showEmptyState() {
    resetStageActive = false;
    clearPinnedSpacer();
    resetAssistantTurnBubbleMap();
    messagesEl.innerHTML = `
      <div class="empty-state">
        <div class="logo-big"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3h6"></path><path d="M12 3v4"></path><rect x="5" y="7" width="14" height="14" rx="3"></rect><path d="M9 12h.01M15 12h.01M9 16h6"></path></svg></div>
        <h2>TindaAgent</h2>
        <div class="sparkle-row" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"></path></svg><svg viewBox="0 0 24 24"><path d="M12 4l2.2 4.9L19 10l-4.8 2.1L12 17l-2.2-4.9L5 10l4.8-2.1z"></path></svg><svg viewBox="0 0 24 24"><path d="M12 2.8v4M12 17.2v4M2.8 12h4M17.2 12h4"></path><circle cx="12" cy="12" r="3"></circle></svg><svg viewBox="0 0 24 24"><path d="M12 4l2.2 4.9L19 10l-4.8 2.1L12 17l-2.2-4.9L5 10l4.8-2.1z"></path></svg><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"></path></svg></div>
        <p>由 Tinda 开发的可爱 AI Agent 助手，可对话、可调用工具～</p>
        <div class="suggest">
          <button onclick="insertPrompt('/help')">/help</button>
          <button onclick="insertPrompt('/tools')">/tools</button>
          <button onclick="insertPrompt('你好')">你好</button>
        </div>
      </div>
    `;
  }

  function clearEmptyState() {
    const es = messagesEl.querySelector(".empty-state");
    if (es) es.remove();
  }

  function insertPrompt(text) {
    inputEl.value = text;
    inputEl.focus();
    autoResize();
  }

  function pickFile() {
    fileInputEl.value = "";
    fileInputEl.click();
  }

  function openFileBar() {
    if (!fileBarWrapEl) return;
    fileBarClosing = false;
    fileBarWrapEl.classList.remove("closing", "file-list-closing");
    fileBarWrapEl.style.display = "block";
    fileBtnEl?.classList.add("has-file");
    renderComposerSelections();
    requestAnimationFrame(updateComposerBottomSpace);
    void fileBarWrapEl.offsetWidth;
  }

  async function closeFileList() {
    if (!fileBarWrapEl) return;
    if (!fileBarWrapEl.classList.contains("expanded")) {
      fileBarWrapEl.classList.remove("file-list-closing");
      return;
    }
    if (fileListClosing) return;
    fileListClosing = true;
    fileBarWrapEl.classList.add("file-list-closing");
    fileBarWrapEl.classList.remove("expanded");
    await waitForTransition(fileListPanelEl, { property: "transform", timeout: 380 });
    fileBarWrapEl.classList.remove("file-list-closing");
    fileListClosing = false;
  }

  async function hideFileBar() {
    if (!fileBarWrapEl) return;
    if (fileBarClosing) return;
    fileBarClosing = true;
    await closeFileList();
    fileBarWrapEl.classList.add("closing");
    await waitForTransition(fileBarWrapEl, { property: "opacity", timeout: 360 });
    if (importedFiles.length === 0) {
      fileBarWrapEl.style.display = "none";
    } else {
      fileBarWrapEl.style.display = "block";
    }
    fileBarWrapEl.classList.remove("closing", "expanded", "file-list-closing");
    fileBarClosing = false;
    renderComposerSelections();
    updateComposerBottomSpace();
  }

  function removeFile() {
    importedFiles = [];
    hideFileBar();
    if (fileBtnEl) fileBtnEl.classList.remove("has-file");
    if (fileInputEl) fileInputEl.value = "";
    renderComposerSelections();
    updateComposerBottomSpace();
    window.setTimeout(() => {
      if (importedFiles.length === 0 && fileListPanelEl) fileListPanelEl.innerHTML = "";
    }, 380);
  }

  function removeOneFile(index) {
    importedFiles.splice(index, 1);
    if (importedFiles.length === 0) {
      removeFile();
      return;
    }
    refreshFileList();
  }

  function refreshFileList() {
    const n = importedFiles.length;
    fileNameEl.textContent = n + " 个文件";
    if (n === 0) {
      removeFile();
      return;
    }
    var html = "";
    importedFiles.forEach(function(f, i) {
      var shortName = getDisplayFileName(f.name);
      html += '<div class="file-list-item">' +
        '<span class="file-list-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg></span>' +
        '<span class="file-list-name" title="' + escapeHtml(f.name) + '">' + escapeHtml(shortName) + '</span>' +
        '<button class="file-list-delete" onclick="event.stopPropagation();removeOneFile(' + i + ')">删除</button>' +
        '</div>';
    });
    fileListPanelEl.innerHTML = html;
    fileListPanelEl.style.maxHeight = FILE_LIST_MAX_HEIGHT + "px";
    fileListPanelEl.style.display = "flex";
    var actualH = fileListPanelEl.scrollHeight;
    var cappedH = Math.min(actualH, FILE_LIST_MAX_HEIGHT);
    fileListPanelEl.style.maxHeight = cappedH + "px";
    if (actualH <= FILE_LIST_MAX_HEIGHT) {
      fileListPanelEl.style.overflowY = "hidden";
    } else {
      fileListPanelEl.style.overflowY = "auto";
    }
    fileListPanelEl.style.display = "";
    renderComposerSelections();
    requestAnimationFrame(updateComposerBottomSpace);
  }

  function toggleFileList() {
    if (!fileBarWrapEl) return;
    var expanded = fileBarWrapEl.classList.contains("expanded");
    if (expanded) {
      closeFileList();
    } else {
      fileListClosing = false;
      fileBarWrapEl.classList.remove("file-list-closing");
      refreshFileList();
      fileBarWrapEl.classList.add("expanded");
    }
  }

  function getFileExtension(filename) {
    const m = filename.match(/\.([^.]+)$/);
    return m ? m[1].toLowerCase() : "";
  }

  function isTextFile(filename) {
    const ext = getFileExtension(filename);
    const textExts = new Set([
      "txt", "md", "markdown", "json", "xml", "yaml", "yml", "toml", "ini", "cfg", "conf", "log", "csv",
      "py", "pyw", "js", "mjs", "cjs", "ts", "tsx", "jsx", "html", "htm", "css", "scss", "less",
      "c", "cpp", "cxx", "cc", "h", "hpp", "hxx", "java", "go", "rs", "rb", "php", "swift", "kt",
      "scala", "lua", "r", "sql", "sh", "bash", "bat", "cmd", "ps1", "cmake", "make", "makefile",
      "dockerfile", "gitignore", "env", "vue", "svelte", "astro", "tex", "gradle", "properties",
      "pl", "pm", "dart", "proto", "graphql", "gql", "tf", "tfvars", "nix", "erb", "ejs", "jinja",
      "jinja2", "twig", "mustache", "handlebars", "hbs", "prisma", "sol", "zig", "nim", "ex", "exs",
      "elm", "fs", "fsx", "fsi", "ml", "mli", "clj", "cljs", "edn", "erl", "hrl", "hs", "lhs",
      "rkt", "scm", "ss", "jl", "f", "f90", "f95", "for", "groovy", "gy", "vala", "vapi",
    ]);
    return textExts.has(ext);
  }

  function handleFileSelect(e) {
    const files = Array.from(e.target.files || []);
    if (files.length === 0) return;
    const validFiles = files.filter(function(f) {
      if (!isTextFile(f.name)) { showToast("不支持的文件类型: " + f.name, "warn"); return false; }
      return true;
    });
    if (validFiles.length === 0) { fileInputEl.value = ""; return; }
    var loaded = 0;
    validFiles.forEach(function(file) {
      const reader = new FileReader();
      reader.onload = function(ev) {
        importedFiles.push({ name: file.name, content: ev.target.result });
        loaded++;
        if (loaded === validFiles.length) {
          openFileBar();
          refreshFileList();
        }
      };
      reader.onerror = function() { showToast("读取失败: " + file.name, "error"); };
      reader.readAsText(file);
    });
  }

  fileInputEl?.addEventListener("change", handleFileSelect);
  fileBarEl?.addEventListener("click", function(e) {
    if (!e.target.closest(".file-list-delete")) toggleFileList();
  });

  function updateComposerBottomSpace() {
    const dockH = Math.ceil(document.querySelector(".input-dock")?.getBoundingClientRect?.().height || 0);
    const next = Math.max(148, dockH + 24);
    document.documentElement.style.setProperty("--composer-bottom-space", `${next}px`);
  }

  function autoResize() {
    const maxInputHeight = 168;
    inputEl.style.height = "auto";
    const nextHeight = Math.min(inputEl.scrollHeight, maxInputHeight);
    inputEl.style.height = nextHeight + "px";
    inputEl.style.overflowY = inputEl.scrollHeight > maxInputHeight ? "auto" : "hidden";
    updateComposerBottomSpace();
  }

  function sanitizeMetaText(v) {
    const text = String(v ?? "").trim().replace(/\s+/g, " ");
    return text || "N/A";
  }

  function formatReadableDate(date) {
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, "0");
    const d = String(date.getDate()).padStart(2, "0");
    const hh = String(date.getHours()).padStart(2, "0");
    const mm = String(date.getMinutes()).padStart(2, "0");
    const ss = String(date.getSeconds()).padStart(2, "0");
    return `${y}年${m}月${d}日 ${hh}:${mm}:${ss}`;
  }

  function toIsoWithOffset(date) {
    const pad2 = (n) => String(n).padStart(2, "0");
    const y = date.getFullYear();
    const m = pad2(date.getMonth() + 1);
    const d = pad2(date.getDate());
    const hh = pad2(date.getHours());
    const mm = pad2(date.getMinutes());
    const ss = pad2(date.getSeconds());
    const offsetMin = -date.getTimezoneOffset();
    const sign = offsetMin >= 0 ? "+" : "-";
    const absMin = Math.abs(offsetMin);
    const offH = pad2(Math.floor(absMin / 60));
    const offM = pad2(absMin % 60);
    return `${y}-${m}-${d}T${hh}:${mm}:${ss}${sign}${offH}:${offM}`;
  }

  function parseDateTimeLocal(value) {
    const s = String(value || "").trim();
    if (!s) return null;
    const normalized = s.length === 16 ? `${s}:00` : s;
    const dt = new Date(normalized);
    if (Number.isNaN(dt.getTime())) return null;
    return dt;
  }

  function collectTimeMeta() {
    if (timeMode === "none") {
      return { time_iso: "N/A", time_text: "N/A" };
    }

    if (timeMode === "custom") {
      const dt = parseDateTimeLocal(customTimeValue);
      if (!dt) return { time_iso: "N/A", time_text: "N/A" };
      return {
        time_iso: toIsoWithOffset(dt),
        time_text: formatReadableDate(dt),
      };
    }

    const now = new Date();
    return {
      time_iso: toIsoWithOffset(now),
      time_text: formatReadableDate(now),
    };
  }

  function buildRequestMeta() {
    const timeMeta = collectTimeMeta();
    return {
      meta_user_name: sanitizeMetaText(userMeta.name),
      meta_user_id: sanitizeMetaText(userMeta.uid),
      meta_user_perm: sanitizeMetaText(userMeta.perm),
      meta_time_iso: sanitizeMetaText(timeMeta.time_iso),
      meta_time_text: sanitizeMetaText(timeMeta.time_text),
    };
  }

  function closeComposerMenu() {
    inputBoxEl?.classList.remove("tools-open");
    composerPlusBtnEl?.setAttribute("aria-expanded", "false");
    composerMenuEl?.setAttribute("aria-hidden", "true");
  }

  function toggleComposerMenu() {
    const nextOpen = !inputBoxEl?.classList.contains("tools-open");
    inputBoxEl?.classList.toggle("tools-open", nextOpen);
    composerPlusBtnEl?.setAttribute("aria-expanded", nextOpen ? "true" : "false");
    composerMenuEl?.setAttribute("aria-hidden", nextOpen ? "false" : "true");
  }

  function composerChipHtml(kind, label, removable = false) {
    const remove = removable
      ? `<button type="button" data-composer-remove="${escapeHtml(kind)}" aria-label="移除${escapeHtml(label)}">×</button>`
      : "";
    return `<span class="composer-chip" data-kind="${escapeHtml(kind)}">${escapeHtml(label)}${remove}</span>`;
  }

  function renderComposerSelections() {
    if (!composerSelectedRowEl) return;
    const chips = [];
    if (webSearchEnabled) chips.push(composerChipHtml("web-search", "网络搜索", true));
    if (deepEnabled) chips.push(composerChipHtml("deep", "Deep 对齐", true));
    if (timeMode !== "none") chips.push(composerChipHtml("time", timeMode === "custom" ? "自定义时间" : "当前时间", false));
    if (importedFiles.length > 0) chips.push(composerChipHtml("file", `${importedFiles.length} 个文件`, false));
    composerSelectedRowEl.innerHTML = chips.join("");
    composerSelectedRowEl.classList.toggle("empty", chips.length === 0);
    webSearchBtnEl?.classList.toggle("on", webSearchEnabled);
    webSearchBtnEl?.setAttribute("aria-pressed", webSearchEnabled ? "true" : "false");
    requestAnimationFrame(updateComposerBottomSpace);
  }

  function setWebSearchEnabled(enabled, { silent = false } = {}) {
    webSearchEnabled = !!enabled;
    localStorage.setItem(WEB_SEARCH_ENABLED_KEY, webSearchEnabled ? "1" : "0");
    renderComposerSelections();
    if (!silent) addSystemNotice(webSearchEnabled ? "本轮允许 LLM 使用网络搜索" : "已关闭网络搜索", { persist: false });
  }

  function toggleWebSearchMode() {
    setWebSearchEnabled(!webSearchEnabled);
  }

  async function closeTimePanel() {
    if (!timeModePanelEl) return;
    await closeAnimatedPanel(timeModePanelEl, "time", "open", { timeout: 360 });
  }

  function setTimeMode(nextMode, { silent = false } = {}) {
    const mode = ["now", "none", "custom"].includes(nextMode) ? nextMode : "now";
    timeMode = mode;
    localStorage.setItem(TIME_MODE_KEY, mode);

    if (mode !== "custom") {
      timeCustomBoxEl?.classList.remove("open");
    } else {
      timeCustomBoxEl?.classList.add("open");
      if (timeCustomInputEl && customTimeValue) timeCustomInputEl.value = customTimeValue;
    }

    if (timeModeBtnEl) {
      const iconClock = timeModeBtnEl.querySelector(".icon-clock");
      const iconCalendar = timeModeBtnEl.querySelector(".icon-calendar");
      const iconStop = timeModeBtnEl.querySelector(".icon-stop");
      if (iconClock && iconCalendar && iconStop) {
        iconClock.style.display = mode === "now" ? "" : "none";
        iconCalendar.style.display = mode === "custom" ? "" : "none";
        iconStop.style.display = mode === "none" ? "" : "none";
      }
      timeModeBtnEl.classList.toggle("on", mode !== "none");
      const tip =
        mode === "none"
          ? "时间注入：关闭"
          : mode === "custom"
            ? "时间注入：自定义"
            : "时间注入：当前时间";
      timeModeBtnEl.title = tip;
    }

    timeModePanelEl?.querySelectorAll(".time-item").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-mode") === mode);
    });

    if (!silent) {
      const notice =
        mode === "none"
          ? "已设置：发送消息不附带时间"
          : mode === "custom"
            ? "已设置：发送消息附带自定义时间"
            : "已设置：发送消息附带当前时间";
      addSystemNotice(notice);
    }
    renderComposerSelections();
  }

  async function ensureUserMetaLoaded() {
    if (userMeta.name !== "N/A" && userMeta.uid !== "N/A") return;
    try {
      const res = await apiFetch("/user/profile");
      if (!res.ok) return;
      const data = await res.json();
      applyUserMeta(data);
    } catch (e) {
      reportErrorToTerminal(`读取用户信息失败：${String(e?.message || e)}`, { source: "user_profile" });
    }
  }

  function applyUserMeta(data) {
    userMeta = {
      name: sanitizeMetaText(data?.name),
      uid: sanitizeMetaText(data?.uid),
      perm: sanitizeMetaText(data?.perm_label ?? data?.perm),
    };
    const p = Number(data?.perm ?? 0);
    const hasLlmPerm = Number.isFinite(p) && ((p & 4) === 4);
    if (modelDiagnosticsBtnEl) modelDiagnosticsBtnEl.style.display = hasLlmPerm ? "" : "none";
    if (adminLinkBtnEl) {
      const visible = Number.isFinite(p) && ((p & USER_ADMIN_PERM) === USER_ADMIN_PERM);
      adminLinkBtnEl.style.display = visible ? "" : "none";
    }
  }
