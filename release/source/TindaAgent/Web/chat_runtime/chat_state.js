/**
 * Extracted from chat.html: chat_state.js
 */

  const messagesEl   = document.getElementById("messages");
  const messagesWrap = document.getElementById("messages-wrap");
  const inputEl      = document.getElementById("input");
  const sendBtn      = document.getElementById("sendBtn");
  const inputBoxEl = document.getElementById("inputBox");
  const composerPlusBtnEl = document.getElementById("composerPlusBtn");
  const composerMenuEl = document.getElementById("composerMenu");
  const composerSelectedRowEl = document.getElementById("composerSelectedRow");
  const webSearchBtnEl = document.getElementById("webSearchBtn");
  const termBodyEl = document.getElementById("termBody");
  const termPanelEl = document.getElementById("termPanel");
  const resizerEl   = document.getElementById("resizer");
  let modelSwitchBtnEl = document.getElementById("modelSwitchBtn");
  let modelPanelEl = document.getElementById("modelPanel");
  let modelDiagnosticsBtnEl = document.getElementById("modelDiagnosticsBtn");
  let adminLinkBtnEl = document.getElementById("adminLinkBtn");
  const quickBtnsEl = document.getElementById("quickBtns");
  const quickSepEl = document.getElementById("quickSep");
  const accountBtnEl = document.getElementById("accountBtn");
  const accountPopupEl = document.getElementById("accountPopup");
  const accountListEl = document.getElementById("accountList");
  const recordsMaskEl = document.getElementById("recordsMask");
  const recordsPanelEl = document.getElementById("recordsPanel");
  const recordsListEl = document.getElementById("recordsList");
  const recordsSearchInputEl = document.getElementById("recordsSearchInput");
  const pendingConfirmOverlayEl = document.getElementById("pendingConfirmOverlay");
  const pendingConfirmTitleEl = document.getElementById("pendingConfirmTitle");
  const pendingConfirmSubtitleEl = document.getElementById("pendingConfirmSubtitle");
  const pendingConfirmLabelEl = document.getElementById("pendingConfirmLabel");
  const pendingConfirmMetaEl = document.getElementById("pendingConfirmMeta");
  const pendingConfirmCmdEl = document.getElementById("pendingConfirmCmd");
  const pendingConfirmAllowBtnEl = document.getElementById("pendingConfirmAllowBtn");
  const pendingConfirmDenyBtnEl = document.getElementById("pendingConfirmDenyBtn");
  const pendingQuestionOptionsEl = document.getElementById("pendingQuestionOptions");
  const pendingQuestionAnswerEl = document.getElementById("pendingQuestionAnswer");
  const termFullOverlayEl = document.getElementById("termFullOverlay");
  const termFullTitleEl = document.getElementById("termFullTitle");
  const termFullContentEl = document.getElementById("termFullContent");
  const termFullCloseBtnEl = document.getElementById("termFullCloseBtn");
  const planFloatEl = document.getElementById("planFloat");
  const planFloatHeadEl = document.getElementById("planFloatHead");
  const planFloatBodyEl = document.getElementById("planFloatBody");
  const planFloatSubtitleEl = document.getElementById("planFloatSubtitle");
  const planFloatCollapseBtnEl = document.getElementById("planFloatCollapseBtn");
  const planFloatCloseBtnEl = document.getElementById("planFloatCloseBtn");
  const toastEl = document.getElementById("tindaToast");
  const statusPillEl = document.getElementById("statusPill");
  const statusDotEl = statusPillEl ? statusPillEl.querySelector(".status-dot") : null;
  const statusTextEl = statusPillEl ? statusPillEl.querySelector(".status-text") : null;
  const timeModeBtnEl = document.getElementById("timeModeBtn");
  const timeModePanelEl = document.getElementById("timeModePanel");
  const timeCustomBoxEl = document.getElementById("timeCustomBox");
  const timeCustomInputEl = document.getElementById("timeCustomInput");
  const timeCustomApplyBtnEl = document.getElementById("timeCustomApplyBtn");
  const deepBtnEl = document.getElementById("deepBtn");
  const fileBtnEl = document.getElementById("fileBtn");
  const fileBarWrapEl = document.getElementById("fileBarWrap");
  const fileBarEl = document.getElementById("fileBar");
  const fileNameEl = document.getElementById("fileName");
  const fileListPanelEl = document.getElementById("fileListPanel");
  const fileInputEl = document.getElementById("fileInput");
  let importedFiles = [];
  const FILE_LIST_MAX_HEIGHT = 200;
  const SESSION_KEY = "tinda_active_session_id";
  const DRAFT_SESSION_PREFIX = "draft_";
  const TERM_WIDTH_KEY = "tinda_term_width";
  const STREAM_ENABLED_KEY = "tinda_stream_enabled";
  const DEEP_ENABLED_KEY = "tinda_deep_enabled";
  const TIME_MODE_KEY = "tinda_time_mode";
  const TIME_CUSTOM_KEY = "tinda_time_custom";
  const WEB_SEARCH_ENABLED_KEY = "tinda_web_search_enabled";
  const RECORDS_PAGE_LIMIT = 200;
  const MODEL_CHOICES_FALLBACK = [
    { id: "deepseek-chat", label: "deepseek-chat" },
    { id: "deepseek-v4-pro", label: "deepseek-pro" },
    { id: "deepseek-reasoner", label: "deepseek-reasoner" },
    { id: "deepseek-v4-flash", label: "deepseek-v4-flash" },
  ];
  const MODEL_ALIAS_MAP = {
    "deepseek-chat": "deepseek-chat",
    "chat": "deepseek-chat",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek-pro": "deepseek-v4-pro",
    "pro": "deepseek-v4-pro",
    "deepseek-reasoner": "deepseek-reasoner",
    "reasoner": "deepseek-reasoner",
    "deepseek-v4-flash": "deepseek-v4-flash",
    "v4-flash": "deepseek-v4-flash",
    "flash": "deepseek-v4-flash",
  };
  const USER_ADMIN_PERM = 511;
  const AUTH_TOKEN_KEY = "ta_auth_token";
  const VERSION_FALLBACK = "v1.12.2";
  const CONTEXT_TOKEN_DEFAULT = 16000;
  const CONTEXT_TOKEN_MIN = 16000;
  const CONTEXT_TOKEN_MAX = 200000;
  const CHAT_INITIAL_MESSAGE_LIMIT = 120;
  const CHAT_HISTORY_PAGE_LIMIT = 120;
  const STREAM_RENDER_INTERVAL_MS = 56;
  const API_RETRY_DELAY_MS = 180;
  const API_TRANSIENT_ERROR_RE = /Failed to fetch|NetworkError|Load failed|AbortError/i;

  /**
   * 全局唯一的上下文 token 阈值入口。
   * 优先级：localStorage 全局设置 > sessionStorage 会话缓存 > 默认 16k。
   * 只有用户主动在设置页或配置弹窗保存才会改写前两级。
   */
  function getContextTokenLimit() {
    try {
      var s = JSON.parse(localStorage.getItem("tinda_settings") || "{}");
      if (isValidContextTokenLimit(s.token_limit)) return Math.floor(Number(s.token_limit));
    } catch (_) {}
    var ss = sessionStorage.getItem("tinda_max_context_tokens");
    if (ss) { var v = parseInt(ss, 10); if (isValidContextTokenLimit(v)) return v; }
    return CONTEXT_TOKEN_DEFAULT;
  }

  function isValidContextTokenLimit(value) {
    const n = Number(value);
    return Number.isFinite(n) && n >= CONTEXT_TOKEN_MIN && n <= CONTEXT_TOKEN_MAX;
  }

  function parseContextTokenLimitInput(value) {
    const parsed = parseInt(String(value || ""), 10);
    if (isValidContextTokenLimit(parsed)) return Math.floor(parsed);
    return 0;
  }

  async function syncContextTokenLimitForSession(sessionId = getSessionId()) {
    const sid = String(sessionId || "").trim();
    const limit = Number(getContextTokenLimit());
    if (!sid || isDraftSessionId(sid) || !isValidContextTokenLimit(limit)) return 0;
    try {
      const res = await apiFetch(`/sessions/${encodeURIComponent(sid)}/config`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_context_tokens: Math.floor(limit) }),
      });
      const data = await res.json();
      if (res.ok && data?.ok !== false) {
        statusContextTokenLimit = Math.floor(limit);
        return Math.floor(limit);
      }
    } catch (_) {}
    return 0;
  }

  function getAuthToken() {
    const current = String(sessionStorage.getItem(AUTH_TOKEN_KEY) || "").trim();
    if (current) return current;
    const legacy = String(localStorage.getItem(AUTH_TOKEN_KEY) || "").trim();
    if (legacy) {
      sessionStorage.setItem(AUTH_TOKEN_KEY, legacy);
      localStorage.removeItem(AUTH_TOKEN_KEY);
    }
    return legacy;
  }

  function setAuthToken(token) {
    const t = String(token || "").trim();
    if (!t) {
      localStorage.removeItem(AUTH_TOKEN_KEY);
      sessionStorage.removeItem(AUTH_TOKEN_KEY);
      return;
    }
    localStorage.removeItem(AUTH_TOKEN_KEY);
    sessionStorage.setItem(AUTH_TOKEN_KEY, t);
  }

  function authHeaders(extra) {
    const headers = Object.assign({}, extra || {});
    const token = getAuthToken();
    if (token) headers["X-User-Token"] = token;
    return headers;
  }

  function resolveApiUrl(url) {
    const raw = String(url || "").trim();
    if (!raw) return raw;
    const candidateBases = [
      String(window.__TINDA_API_BASE__ || "").trim(),
      (() => {
        const origin = String(window.location?.origin || "").trim();
        return origin && origin !== "null" ? origin : "";
      })(),
      String(window.location?.href || "").trim(),
    ].filter(Boolean);
    for (const base of candidateBases) {
      try {
        return new URL(raw, base).toString();
      } catch (_) {}
    }
    return raw;
  }

  function isTransientFetchError(error) {
    const message = String(error?.message || error || "");
    return error?.name === "AbortError" || API_TRANSIENT_ERROR_RE.test(message);
  }

  async function apiFetch(url, options = {}) {
    const opts = Object.assign({}, options);
    const retryOnNetworkError = opts.retryOnNetworkError !== false;
    delete opts.retryOnNetworkError;
    const method = String(opts.method || "GET").trim().toUpperCase() || "GET";
    const requestUrl = resolveApiUrl(url);
    opts.headers = authHeaders(opts.headers || {});
    const retryable = retryOnNetworkError
      && (method === "GET" || method === "HEAD")
      && !String(requestUrl).includes("/chat/stream");
    let lastError = null;
    for (let attempt = 0; attempt < (retryable ? 2 : 1); attempt++) {
      try {
        return await fetch(requestUrl, opts);
      } catch (error) {
        lastError = error;
        if (!retryable || attempt >= 1 || !isTransientFetchError(error)) {
          throw error;
        }
        await new Promise((resolve) => setTimeout(resolve, API_RETRY_DELAY_MS));
      }
    }
    throw lastError;
  }

  function getSessionStorageKey() {
    const uid = String(userMeta?.uid || "").trim();
    return uid && uid !== "N/A" ? `${SESSION_KEY}:${uid}` : SESSION_KEY;
  }

  function getStoredSessionId() {
    return String(localStorage.getItem(getSessionStorageKey()) || "").trim();
  }

  function setStoredSessionId(sessionId) {
    const sid = String(sessionId || "").trim();
    if (!sid) return;
    localStorage.setItem(getSessionStorageKey(), sid);
    localStorage.setItem(SESSION_KEY, sid);
  }

  function clearStoredSessionId() {
    localStorage.removeItem(getSessionStorageKey());
    localStorage.removeItem(SESSION_KEY);
  }

  function isDraftSessionId(sessionId) {
    return String(sessionId || "").startsWith(DRAFT_SESSION_PREFIX);
  }

  let termInitialized = false;
  let termOpen = false;
  let termWidth   = 320;
  let isResizing  = false, resizeStartX = 0, resizeStartW = 0;
  let streamEnabled = false;
  let resetStageActive = false;
  let spacerRecalcRaf = 0;
	  let recordsPanelOpen = false;
	  let recordsPanelClosing = false;
  let fileListClosing = false;
  let fileBarClosing = false;
  const panelClosing = {
    model: false,
    time: false,
    account: false,
    config: false,
    pendingConfirm: false,
  };
  let sessionsCache = [];
  let activeSessionTitle = "新对话";
  let statusContextUsageLength = 0;
  let statusContextTokenLimit = CONTEXT_TOKEN_DEFAULT;
  let statusContextPreviousUsageLength = 0;
  let gatewayOnline = true;
  let gatewayHeartbeatTimer = 0;
  let toolPollTimer = 0;
  let toolLastSeq = 0;
  const processedTerminalSeq = new Set();
  let userMeta = {
    name: "N/A",
    uid: "N/A",
    perm: "N/A",
  };
  let timeMode = "now";
  let customTimeValue = "";
  let modelChoices = MODEL_CHOICES_FALLBACK.slice();
  let currentModel = "deepseek-v4-flash";
  let sessionPersistQueue = Promise.resolve();
  let creatingSessionFromPanel = false;
  let appVersionDisplay = VERSION_FALLBACK;
  let appVersionSignatureId = "";
  let appVersionVerified = false;
  let toolEventsFetchErrorStreak = 0;
  let toolEventsLastErrorAt = 0;
  let toolPollInFlight = false;
  let toolPollPausedByError = false;
  let messageSendInFlight = false;
  let navigationPending = false;
  let terminalConfirmLockActive = false;
  let terminalConfirmPendingCount = 0;
  let deepEnabled = false;
  let webSearchEnabled = false;
  let deepAlignmentBusy = false;
  let deepPendingPayload = null;
  let deepActiveCard = null;
  let planFloatDragging = false;
  let planFloatDragStartX = 0;
  let planFloatDragStartY = 0;
  let planFloatStartLeft = 0;
  let planFloatStartTop = 0;
  let planDeletedBySession = new Map();
  let planCurrentBySession = new Map();
  let planDeleteInFlight = false;
  let pendingConfirmCurrent = null;
  let pendingConfirmSubmitting = false;
  const TOOL_EVENTS_ERROR_REPORT_INTERVAL_MS = 15000;
  const TOOL_EVENTS_ERROR_AUTO_PAUSE_STREAK = 12;
  const GATEWAY_HEARTBEAT_INTERVAL_MS = 12000;
  const TERM_MAX_LINES = 500;
  const TERM_FLUSH_BATCH = 120;
  const TERM_ENTRY_PREVIEW_LINES = 6;
  const TERM_OUTPUT_LINE_LIMIT = 80;
  const TERM_FULL_CACHE_LIMIT = 10;
  const TERM_RENDER_BATCH_SIZE = 160;
  const TERM_LINE_CHAR_LIMIT = 1200;
  const TERM_HISTORY_REPLAY_LIMIT = 2000;
  const CHAT_BOOT_MIN_MS = 1000;
  const CHAT_LOADING_FADE_MS = 500;
  const CHAT_SESSION_LOADING_MIN_MS = 520;
  const INPUT_PLACEHOLDER_DEFAULT = "跟 Tinda 说说话吧 ...";
  const INPUT_PLACEHOLDER_CONFIRM_LOCK = "存在待确认终端命令，请先在弹窗中允许/拒绝";
  const INPUT_PLACEHOLDER_QUESTION_LOCK = "存在待回答问题，请先在弹窗中提交回答或取消";
  let termPersistBuffer = [];
  let termPersistTimer = 0;
  let termRequestGroup = null;
  let termRenderQueue = [];
  let termRenderRaf = 0;
  let termPendingScroll = false;
  let termFullOutputCache = new Map();
  let termFullOutputSeq = 0;
  let termFullRenderRaf = 0;
  let hydratedSessionEntries = [];
  let historyPagingState = { sid: "", oldestSeq: 0, hasMore: false, loading: false };
