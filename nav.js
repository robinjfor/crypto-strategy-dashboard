/* Shared top nav — same tabs/order/style on every page. Drop obsolete 追蹤名單. */
(function () {
  "use strict";

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

  function render() {
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
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
