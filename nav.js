/* Shared top nav + site-wide login gate (session Bearer). */
(function () {
  "use strict";

  var TOKEN_KEY = "trader_session";
  var CLOUD_CFG = "./data/cloud_api.json";
  var _apiBase = "";
  var _origFetch = window.fetch.bind(window);

  function pageId() {
    var p = (location.pathname || "").split("/").pop() || "";
    if (!p || p === "index.html") return "home";
    if (p.indexOf("history") === 0) return "history";
    if (p.indexOf("backtest") === 0) return "backtest";
    if (p.indexOf("watchlist") === 0) return "watchlist";
    return "home";
  }

  var META = {
    home: {
      mark: "⚡",
      title: "Binance Demo 帳戶（真實成交）",
      sub: "Binance Demo · 訊號監控 · 靜態／GitHub Pages",
      titleId: "pageTitle",
      subId: "pageSub"
    },
    history: {
      mark: "⚡",
      title: "成交歷史",
      sub: "Binance Demo 帳戶（真實成交）· 雲端優先"
    },
    backtest: {
      mark: "📊",
      title: "策略評分",
      sub: "統一 3 年回測 · 起始 10,000 USDT · 僅回測報告"
    },
    watchlist: {
      mark: "📡",
      title: "追蹤名單",
      sub: "舊掃描器（已自主選單移除）"
    }
  };

  var TABS = [
    { id: "home", href: "./", label: "帳戶總覽" },
    { id: "history", href: "./history.html", label: "成交歷史" },
    { id: "backtest", href: "./backtest.html", label: "策略評分" }
  ];

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function normalizePin(raw) {
    var s = String(raw == null ? "" : raw).trim();
    var out = "";
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      if (c >= 0xff10 && c <= 0xff19) out += String.fromCharCode(c - 0xff10 + 48);
      else out += s.charAt(i);
    }
    return out;
  }

  function getToken() {
    try {
      return localStorage.getItem(TOKEN_KEY) || "";
    } catch (e) {
      return "";
    }
  }

  function setToken(tok) {
    try {
      if (tok) localStorage.setItem(TOKEN_KEY, tok);
      else localStorage.removeItem(TOKEN_KEY);
    } catch (e) {}
  }

  function clearToken() {
    setToken("");
    try {
      sessionStorage.removeItem("trader_pin");
    } catch (e) {}
  }

  function isApiUrl(url) {
    try {
      var u = typeof url === "string" ? url : url && url.url;
      if (!u) return false;
      u = String(u);
      if (_apiBase && u.indexOf(_apiBase) === 0) return true;
      // Fallback before cloud_api.json resolves — Cloud Run trader API host
      return u.indexOf("crypto-trader-api") !== -1 && u.indexOf(".run.app") !== -1;
    } catch (e) {
      return false;
    }
  }

  function patchFetch() {
    window.fetch = function (input, init) {
      init = init ? Object.assign({}, init) : {};
      var headers = new Headers(init.headers || (input && input.headers) || {});
      var tok = getToken();
      var sentBearer = false;
      if (tok && isApiUrl(input) && !headers.has("Authorization")) {
        headers.set("Authorization", "Bearer " + tok);
        sentBearer = true;
      } else if (headers.has("Authorization")) {
        sentBearer = String(headers.get("Authorization") || "").toLowerCase().indexOf("bearer ") === 0;
      }
      init.headers = headers;
      return _origFetch(input, init).then(function (res) {
        // Only drop the session if a Bearer request was rejected (avoid race
        // where an early unauthenticated /status 401 clears a fresh login).
        if (res.status === 401 && sentBearer && getToken()) {
          clearToken();
          showLogin("登入已過期，請重新登入");
        }
        return res;
      });
    };
  }

  async function resolveApiBase() {
    if (window.TRADER_API_BASE) {
      _apiBase = String(window.TRADER_API_BASE).replace(/\/$/, "");
      return _apiBase;
    }
    try {
      var res = await _origFetch(CLOUD_CFG + "?t=" + Date.now(), { cache: "no-store" });
      var cfg = await res.json();
      _apiBase = String(cfg.base || "").replace(/\/$/, "");
    } catch (e) {
      _apiBase = "";
    }
    return _apiBase;
  }

  function ensureLoginStyles() {
    if (document.getElementById("loginGateStyles")) return;
    var st = document.createElement("style");
    st.id = "loginGateStyles";
    st.textContent =
      "#loginGate{position:fixed;inset:0;z-index:99999;background:#0b1220;display:flex;align-items:center;justify-content:center;padding:24px;}" +
      "#loginGate.hidden{display:none;}" +
      "#loginGate .login-card{width:min(400px,100%);background:#111827;border:1px solid #1f2a37;border-radius:16px;padding:28px 24px;box-shadow:0 20px 50px rgba(0,0,0,.45);}" +
      "#loginGate h2{margin:0 0 6px;font-size:22px;color:#e8eef7;}" +
      "#loginGate .login-sub{margin:0 0 18px;color:#8b9bb0;font-size:13px;}" +
      "#loginGate label{display:block;font-size:13px;color:#c5d0dc;margin-bottom:6px;}" +
      "#loginGate input{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:10px;border:1px solid #2a3544;background:#0b1220;color:#e8eef7;font-size:16px;}" +
      "#loginGate .login-actions{margin-top:16px;display:flex;gap:10px;}" +
      "#loginGate button.primary{flex:1;padding:10px 14px;border-radius:10px;border:0;background:#3b82f6;color:#fff;font-weight:600;cursor:pointer;}" +
      "#loginGate .login-err{min-height:1.3em;margin-top:12px;color:#fca5a5;font-size:13px;}" +
      "body.login-locked .app{visibility:hidden;}" +
      "#btnLogout{margin-left:8px;}";
    document.head.appendChild(st);
  }

  function showLogin(msg) {
    ensureLoginStyles();
    document.body.classList.add("login-locked");
    var gate = document.getElementById("loginGate");
    if (!gate) {
      gate = document.createElement("div");
      gate.id = "loginGate";
      gate.innerHTML =
        '<div class="login-card">' +
        "<h2>登入</h2>" +
        '<p class="login-sub">請輸入操作密碼以存取儀表板</p>' +
        '<label for="loginPin">操作密碼</label>' +
        '<input type="password" id="loginPin" autocomplete="current-password" />' +
        '<div class="login-actions"><button type="button" class="primary" id="loginSubmit">登入</button></div>' +
        '<p class="login-err" id="loginErr"></p>' +
        "</div>";
      document.body.appendChild(gate);
      document.getElementById("loginSubmit").onclick = doLogin;
      document.getElementById("loginPin").onkeydown = function (e) {
        if (e.key === "Enter") doLogin();
      };
    }
    gate.classList.remove("hidden");
    var err = document.getElementById("loginErr");
    if (err) err.textContent = msg || "";
    setTimeout(function () {
      var inp = document.getElementById("loginPin");
      if (inp) inp.focus();
    }, 50);
  }

  function hideLogin() {
    document.body.classList.remove("login-locked");
    var gate = document.getElementById("loginGate");
    if (gate) gate.classList.add("hidden");
  }

  async function doLogin() {
    var err = document.getElementById("loginErr");
    var inp = document.getElementById("loginPin");
    var pin = normalizePin(inp && inp.value);
    if (!pin) {
      if (err) err.textContent = "請輸入密碼";
      return;
    }
    if (err) err.textContent = "登入中…";
    try {
      if (!_apiBase) await resolveApiBase();
      if (!_apiBase) throw new Error("無法讀取雲端 API 位址");
      var res = await _origFetch(_apiBase + "/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin: pin })
      });
      var data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok || !data.token) {
        throw new Error((data && data.error) || "登入失敗");
      }
      setToken(data.token);
      if (inp) inp.value = "";
      hideLogin();
      // Reload so page scripts refetch /status with Bearer
      location.reload();
    } catch (e) {
      if (err) err.textContent = (e && e.message) || String(e);
    }
  }

  function logout() {
    clearToken();
    showLogin("已登出");
  }

  function renderNav() {
    var id = pageId();
    var m = META[id] || META.home;
    var header = document.getElementById("siteHeader") || document.querySelector("header.topbar");
    if (!header) return;
    var controls = header.querySelector(".controls");
    var controlsHtml = controls ? controls.outerHTML : "";
    var nav = TABS.map(function (t) {
      return (
        '<a class="nav-link' +
        (t.id === id ? " active" : "") +
        '" href="' +
        t.href +
        '">' +
        esc(t.label) +
        "</a>"
      );
    }).join("");
    var h1Attrs = m.titleId ? ' id="' + m.titleId + '"' : "";
    var subAttrs = m.subId ? ' id="' + m.subId + '"' : "";
    header.innerHTML =
      '<div class="brand">' +
      '<div class="brand-mark">' +
      m.mark +
      "</div><div>" +
      "<h1" +
      h1Attrs +
      ">" +
      esc(m.title) +
      "</h1>" +
      '<div class="sub"' +
      subAttrs +
      ">" +
      esc(m.sub) +
      "</div></div></div>" +
      '<nav class="nav-links" aria-label="主選單">' +
      nav +
      "</nav>" +
      controlsHtml;

    // Attach logout into controls if authenticated
    if (getToken()) {
      var ctrl = header.querySelector(".controls");
      if (ctrl && !document.getElementById("btnLogout")) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.id = "btnLogout";
        btn.className = "btn-logout";
        btn.textContent = "登出";
        btn.onclick = logout;
        ctrl.appendChild(btn);
      }
    }
  }

  // Expose helpers for page scripts (backtest / live)
  window.TraderAuth = {
    getToken: getToken,
    setToken: setToken,
    clearToken: clearToken,
    normalizePin: normalizePin,
    logout: logout,
    getApiBase: function () {
      return _apiBase;
    }
  };

  patchFetch();

  // Synchronous early lock to avoid flashing protected UI before boot()
  if (!getToken()) {
    try {
      ensureLoginStyles();
      if (document.body) {
        document.body.classList.add("login-locked");
      } else {
        document.addEventListener("DOMContentLoaded", function () {
          if (!getToken()) document.body.classList.add("login-locked");
        });
      }
    } catch (e) {}
  }

  async function boot() {
    ensureLoginStyles();
    await resolveApiBase();
    if (!getToken()) {
      showLogin("");
      renderNav();
      return;
    }
    hideLogin();
    renderNav();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
