/* TindaAgent theme toggle — v1.8.3
 * 浅色 ⟷ 深色模式切换。光线状态语义:light → 太阳,dark → 月亮。
 *
 * 持久化:localStorage["tinda_theme"]
 * 系统偏好兜底:prefers-color-scheme(仅在用户从未手动选择时跟随)
 * 跨标签同步:storage 事件
 *
 * 用法:
 *   1. <head> 第一个 <script> 内联读 localStorage 设 data-theme(FOUC 预防)
 *   2. <body> 末尾 <script src="/theme_toggle.js"></script> 绑定按钮
 *   3. HTML 中放置 <button id="themeToggle">,内含 .theme-sun + .theme-moon
 */
(function () {
  "use strict";
  var KEY = "tinda_theme";

  function getStored() {
    try {
      var v = localStorage.getItem(KEY);
      if (v === "dark" || v === "light") return v;
    } catch (e) {}
    return null;
  }

  function getSystemPref() {
    return window.matchMedia &&
           window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  function getInitialTheme() {
    var stored = getStored();
    return stored != null ? stored : getSystemPref();
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    // 同步所有匹配 ID 的按钮(home 和 settings 各一个,共用 ID 不冲突)
    var btns = document.querySelectorAll("#themeToggle");
    for (var i = 0; i < btns.length; i++) {
      var btn = btns[i];
      btn.dataset.theme = theme;
      var label = theme === "dark" ? "切换到浅色模式" : "切换到深色模式";
      btn.setAttribute("aria-label", label);
      btn.title = label;
    }
  }

  function setTheme(theme, persist) {
    if (theme !== "dark" && theme !== "light") return;
    if (persist !== false) {
      try { localStorage.setItem(KEY, theme); } catch (e) {}
    }
    applyTheme(theme);
  }

  // 1. 立即应用(FOUC 预防的二次保险 — head inline 已经先跑过一次)
  applyTheme(document.documentElement.dataset.theme || getInitialTheme());

  // 2. 绑定按钮点击
  function bindToggle() {
    var btns = document.querySelectorAll("#themeToggle");
    for (var i = 0; i < btns.length; i++) {
      var btn = btns[i];
      if (btn.dataset.bound === "1") continue;
      btn.dataset.bound = "1";
      btn.addEventListener("click", function () {
        var cur = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
        setTheme(cur === "dark" ? "light" : "dark");
      });
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindToggle);
  } else {
    bindToggle();
  }

  // 3. 跨标签页同步
  window.addEventListener("storage", function (e) {
    if (e.key === KEY && (e.newValue === "dark" || e.newValue === "light")) {
      applyTheme(e.newValue);
    }
  });

  // 4. 系统偏好变化(仅在用户从未手动选择时跟随)
  if (window.matchMedia) {
    try {
      var mq = window.matchMedia("(prefers-color-scheme: dark)");
      var handler = function (e) {
        if (getStored() == null) {
          applyTheme(e.matches ? "dark" : "light");
        }
      };
      if (mq.addEventListener) mq.addEventListener("change", handler);
      else if (mq.addListener) mq.addListener(handler);
    } catch (e) {}
  }

  // 5. 暴露 API(供调试或其他页面调用)
  window.TindaTheme = {
    get: function () { return document.documentElement.dataset.theme; },
    set: setTheme,
    clear: function () {
      try { localStorage.removeItem(KEY); } catch (e) {}
      applyTheme(getSystemPref());
    }
  };
})();
