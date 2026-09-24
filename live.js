/* Homepage live view — Binance Demo real fills. Mirror to live.js */
(function () {
  "use strict";

  const BOOK_URL = "./data/live_book.json";
  const HEALTH_URL = "./state/health.json";
  const POSITIONS_URL = "./state/positions.json";
  const PAPER_URL = "./state/paper_trading.json";
  const SETTLEMENT_URL = "./data/strategy-crypto-s2/settlement.json";
  const SETTLEMENT_JSONL = "./state/settlement.jsonl";

  let book = null;
  let health = null;
  let positions = null;
  let paper = null;
  let settlements = [];
  let loading = false;
  let autoOn = true;
  let refreshSec = 30;
  let countdown = 30;
  let timerId = null;
  let pieParts = [];

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function num(v, d) {
    if (v == null || v === "" || Number.isNaN(Number(v))) return "—";
    return Number(v).toLocaleString("en-US", {
      minimumFractionDigits: d, maximumFractionDigits: d
    });
  }

  function signedCls(v) {
    if (v == null || Number.isNaN(Number(v))) return "";
    if (Number(v) > 0) return "pos";
    if (Number(v) < 0) return "neg";
    return "";
  }

  function reason(code) {
    if (typeof window.reasonZh === "function") return window.reasonZh(code);
    return (code == null || code === "") ? "—" : String(code);
  }

  function showErr(msg) {
    var el = $("errBanner");
    if (!el) return;
    if (!msg) { el.textContent = ""; el.classList.add("hidden"); return; }
    el.textContent = msg; el.classList.remove("hidden");
  }

  async function getJSON(url) {
    var res = await fetch(url + (url.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now(), { cache: "no-store" });
    if (!res.ok) throw new Error(url + " HTTP " + res.status);
    return res.json();
  }

  async function getJSONOpt(url) {
    try { return await getJSON(url); } catch (e) { return null; }
  }

  async function loadSettlements() {
    try {
      var arr = await getJSON(SETTLEMENT_URL);
      if (Array.isArray(arr)) return arr;
    } catch (e) {}
    try {
      var res = await fetch(SETTLEMENT_JSONL + "?t=" + Date.now(), { cache: "no-store" });
      if (!res.ok) return [];
      return (await res.text()).trim().split("\n").filter(Boolean).map(function (l) { return JSON.parse(l); });
    } catch (e) { return []; }
  }

  function parseTs(s) {
    if (!s) return null;
    var d = new Date(String(s).replace(" ", "T"));
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function nowLabel() {
    try { return new Date().toLocaleString("zh-TW", { timeZone: "Asia/Taipei" }) + " CST"; }
    catch (e) { return new Date().toISOString(); }
  }

  function sortDesc(rows) {
    return (rows || []).slice().sort(function (a, b) {
      var ta = parseTs(a.ts || a.time);
      var tb = parseTs(b.ts || b.time);
      return (tb ? tb.getTime() : 0) - (ta ? ta.getTime() : 0);
    });
  }

  function isTrade(r) {
    var sym = String(r.symbol || "").toUpperCase();
    if (sym.indexOf("OPUSDT") >= 0 || sym.indexOf("FILUSDT") >= 0) return false;
    if (sym === "OP" || sym === "FIL") return false;
    return true;
  }

  function bals() {
    var b = (positions && positions.balances) || (paper && paper.balances) || (book && book.balances) || {};
    return {
      USDT: b.USDT != null ? Number(b.USDT) : null,
      USDC: b.USDC != null ? Number(b.USDC) : null,
      NEAR: b.NEAR != null ? Number(b.NEAR) : 0
    };
  }

  function renderHealth() {
    var h = health || {};
    var label = h.automation_label || "自動執行：尚未啟用（等待雲端主機）";
    return '<div class="health-bar idle" id="systemHealth">' +
      '<span class="health-light idle">● ' + esc(label) + '</span>' +
      '<span class="health-meta">訊號 runner 待命 · Actions 已停用</span>' +
      '<span class="health-meta">來源：' + esc((book && book.source_label) || "Binance Demo") + '</span>' +
      '</div>';
  }

  function renderAccount() {
    var b = bals();
    var usdt = b.USDT, usdc = b.USDC;
    var total = (usdt != null && usdc != null) ? usdt + usdc
      : (book && book.equity_total_stable != null ? Number(book.equity_total_stable) : null);
    var equity = (positions && positions.virtual_equity != null) ? Number(positions.virtual_equity)
      : (book && book.equity_usdt != null ? Number(book.equity_usdt) : usdt);
    var realized = (book && book.realized_pnl_usdt != null) ? Number(book.realized_pnl_usdt) : null;
    var maxDd = (book && book.max_drawdown != null) ? book.max_drawdown : null;
    var openN = ((positions && positions.positions) || (book && book.open_positions) || []).length;

    pieParts = [];
    if (usdt != null && usdt > 0) pieParts.push({ label: "USDT", value: usdt });
    if (usdc != null && usdc > 0) pieParts.push({ label: "USDC", value: usdc });

    return '<section class="section" id="sec-account">' +
      '<div class="section-head"><h2>帳戶總覽</h2><span class="hint">' +
      esc((book && book.source_label) || "Binance Demo 帳戶（真實成交）") + '</span></div>' +
      '<div class="overview-row"><div class="kpi-grid kpi-grid-demo">' +
      '<div class="kpi"><div class="label">USDT</div><div class="value">' + num(usdt, 2) + '</div><div class="sublabel">可用餘額</div></div>' +
      '<div class="kpi"><div class="label">USDC</div><div class="value">' + num(usdc, 2) + '</div><div class="sublabel">可用餘額</div></div>' +
      '<div class="kpi kpi-emphasis"><div class="label">穩定幣合計</div><div class="value">' + num(total, 2) + '</div><div class="sublabel">USDT + USDC（標明合計）</div></div>' +
      '<div class="kpi"><div class="label">USDT 側權益</div><div class="value">' + num(equity, 2) + '</div><div class="sublabel">含粉塵標的市值（若有）</div></div>' +
      '<div class="kpi"><div class="label">已實現損益（NEAR）</div><div class="value ' + signedCls(realized) + '">' +
      (realized == null ? "—" : ((realized > 0 ? "+" : "") + num(realized, 2))) +
      '</div><div class="sublabel">僅 Demo 真實成交</div></div>' +
      '<div class="kpi"><div class="label">最大回撤</div><div class="value">' + (maxDd == null ? "—" : num(maxDd, 2) + "%") + '</div><div class="sublabel">資料未提供則顯示 —</div></div>' +
      '<div class="kpi"><div class="label">實際持倉數</div><div class="value">' + num(openN, 0) + '</div><div class="sublabel">' +
      (openN === 0 ? "目前空倉（全現金）" : "幣種倉") + '</div></div>' +
      '</div><div class="kpi kpi-pie"><div class="label">現金結構</div><div class="alloc-pie-wrap"><canvas id="allocPie" width="120" height="120"></canvas></div></div></div></section>';
  }

  function tradeRow(r) {
    var ts = r.ts || r.time || "—";
    var kind = r.kind || "—";
    var sym = r.symbol || "—";
    var side = r.side || (String(kind).toUpperCase() === "OPEN" ? "BUY" : String(kind).toUpperCase() === "CLOSE" ? "SELL" : "—");
    var qty = r.qty;
    var px = r.price != null ? r.price : r.avg_price;
    var fee = r.fee;
    var pnl = r.realized_pnl_usdt != null ? r.realized_pnl_usdt : r.pnl_usdt;
    return "<tr>" +
      "<td>" + esc(String(ts)) + "</td>" +
      "<td>" + esc(String(kind)) + "</td>" +
      "<td><strong>" + esc(String(sym)) + "</strong></td>" +
      "<td>" + esc(String(side)) + "</td>" +
      '<td class="num">' + num(qty, 4) + "</td>" +
      '<td class="num">' + num(px, 4) + "</td>" +
      '<td class="num">' + (fee == null ? "—" : num(fee, 4)) + "</td>" +
      '<td class="num ' + signedCls(pnl) + '">' + (pnl == null ? "—" : num(pnl, 2)) + "</td>" +
      "<td>" + esc(reason(r.reason || kind)) + "</td></tr>";
  }

  function renderChanges() {
    var rows = sortDesc(settlements.filter(isTrade)).slice(0, 5);
    var body = rows.length ? rows.map(tradeRow).join("") :
      '<tr><td colspan="9" class="empty-row">尚無成交紀錄</td></tr>';
    return '<section class="section" id="sec-changes">' +
      '<div class="section-head"><h2>帳戶變動明細</h2><span class="hint">最新 5 筆 · <a href="./history.html">完整歷史</a></span></div>' +
      '<div class="card"><div class="table-scroll"><table class="data">' +
      "<thead><tr><th>時間</th><th>類型</th><th>標的</th><th>方向</th><th class=\"num\">數量</th><th class=\"num\">價格</th><th class=\"num\">手續費</th><th class=\"num\">已實現損益</th><th>原因</th></tr></thead>" +
      "<tbody>" + body + "</tbody></table></div></div></section>";
  }

  function renderActual() {
    var list = (positions && Array.isArray(positions.positions) && positions.positions) ||
      (book && book.open_positions) || [];
    var open = list.filter(function (p) { return !p.status || p.status === "FILLED" || p.status === "OPEN"; });
    var body;
    if (!open.length) {
      body = '<tr><td colspan="9" class="empty-row">目前空倉（全現金）</td></tr>';
    } else {
      body = open.map(function (p) {
        var upnl = p.unrealized_pnl != null ? p.unrealized_pnl : p.unrealized_usdt;
        return "<tr>" +
          "<td><strong>" + esc(p.symbol || p.asset || "—") + '</strong><div class="kpi-sub">' + esc(p.variant || "") + "</div></td>" +
          "<td>" + esc(p.tf || "—") + "</td>" +
          '<td class="num">' + num(p.qty, 4) + "</td>" +
          '<td class="num">' + num(p.entry != null ? p.entry : p.entry_price, 4) + "</td>" +
          '<td class="num">' + (p.mark_price != null || p.mark != null ? num(p.mark_price != null ? p.mark_price : p.mark, 4) : "—") + "</td>" +
          '<td class="num">' + (p.stop != null ? num(p.stop, 4) : "—") + '<div class="kpi-sub">移動止損</div></td>' +
          '<td class="num">' + (p.donch_lo != null ? num(p.donch_lo, 4) : "—") + '<div class="kpi-sub">出場下軌</div></td>' +
          '<td class="num">' + num((p.entry || p.entry_price || 0) * (p.qty || 0), 2) + "</td>" +
          '<td class="num ' + signedCls(upnl) + '">' + (upnl == null ? "—" : num(upnl, 2)) + "</td></tr>";
      }).join("");
    }
    return '<section class="section" id="sec-actual">' +
      '<div class="section-head"><h2>實際持倉</h2><span class="hint">僅 Demo 真實部位</span></div>' +
      '<div class="card"><div class="table-scroll"><table class="data">' +
      "<thead><tr><th>標的／策略</th><th>週期</th><th class=\"num\">數量</th><th class=\"num\">進場價</th><th class=\"num\">現價</th><th class=\"num\">移動止損</th><th class=\"num\">出場下軌</th><th class=\"num\">市值</th><th class=\"num\">未實現損益</th></tr></thead>" +
      "<tbody>" + body + "</tbody></table></div></div></section>";
  }

  function plannedList() {
    var planned = (book && Array.isArray(book.planned_positions) ? book.planned_positions : []).slice();
    planned = planned.filter(function (p) {
      var st = String(p.ui_status || p.status || "").toUpperCase();
      return st.indexOf("WAIT_BREAKOUT") >= 0 || st.indexOf("WAIT_RESET") >= 0 ||
        st.indexOf("REARM") >= 0 || st.indexOf("PENDING") >= 0 || st.indexOf("ARMED") >= 0;
    });
    return planned.filter(function (p) {
      var a = String(p.asset || p.symbol || "").toUpperCase();
      return a !== "NEAR" && a !== "NEARUSDT" && String(p.ui_status || "").indexOf("DISARM") < 0;
    });
  }

  function renderPlanned() {
    var planned = plannedList();
    var body;
    if (!planned.length) {
      body = '<tr><td colspan="8" class="empty-row">目前沒有預計持倉</td></tr>';
    } else {
      body = planned.map(function (pl) {
        var stopNote = pl.stop_mode === "pending"
          ? (pl.stop_note || "未開倉 → 無有效移動止損／出場下軌")
          : (pl.stop_note || (pl.stop_ref != null ? "止損參考 " + pl.stop_ref : "—"));
        var safe = String(stopNote).replace(/目標價/g, "止損參考");
        return "<tr>" +
          "<td><strong>" + esc(pl.asset) + '</strong><div class="kpi-sub">' + esc(pl.strategy_name || "") + "</div></td>" +
          '<td class="num">' + num(pl.target_pct, 0) + "%</td>" +
          '<td class="num">' + num(pl.target_notional_usdt, 0) + "</td>" +
          "<td>" + esc(pl.status_label || pl.ui_status || "—") + "</td>" +
          "<td>" + esc(pl.tf || "—") + " / Donch " + esc(String(pl.donch_n || "—")) + "</td>" +
          "<td>" + esc(pl.entry_rule || "—") + "</td>" +
          "<td>" + esc(safe) + "</td>" +
          '<td class="num">' + (pl.mark != null ? num(pl.mark, 4) : "—") +
          '<div class="kpi-sub">觸發 ' + (pl.trigger != null ? num(pl.trigger, 4) : "—") + "</div></td></tr>";
      }).join("");
    }
    return '<section class="section" id="sec-planned">' +
      '<div class="section-head"><h2>預計持倉</h2><span class="hint">訊號槽 · 不含 NEAR</span></div>' +
      '<div class="card"><div class="table-scroll"><table class="data">' +
      "<thead><tr><th>標的</th><th class=\"num\">目標%</th><th class=\"num\">名義 USDT</th><th>狀態</th><th>週期</th><th>進場條件</th><th>止損／出場</th><th class=\"num\">現價／觸發</th></tr></thead>" +
      "<tbody>" + body + "</tbody></table></div></div></section>";
  }

  function renderStrategies() {
    var sats = (book && book.satellite_strategies) || [];
    var core = (book && book.strategies) || [];
    var n = plannedList().length;
    var cards = core.concat(sats).map(function (s) {
      var active = s.status === "active" || s.status === "armed_wait_breakout" || s.status === "wait_reset";
      return '<article class="strategy-card ' + (active ? "active" : "") + '">' +
        '<div class="sc-top"><strong>' + esc(s.name || s.id || "—") + '</strong>' +
        '<span class="badge ' + (active ? "ok" : "muted") + '">' + esc(s.status || "—") + "</span></div>" +
        '<p class="sc-sum">' + esc(s.summary || "") + "</p>" +
        '<div class="sc-meta">' + esc(s.timeframe || "") + " · " + esc(s.symbol || s.asset || "") + " · " + esc(s.leverage || "1x") + "</div>" +
        '<div class="sc-rules">' +
        '<div><span class="lbl">進場</span> ' + esc(s.entry_rule || "—") + "</div>" +
        '<div><span class="lbl">出場</span> ' + esc(s.exit_rule || "—") + "</div>" +
        '<div><span class="lbl">止損</span> ' + esc(String(s.stop_rule || "—").replace(/目標價/g, "止損參考")) + "</div>" +
        "</div></article>";
    }).join("");
    return '<section class="section" id="sec-strategies">' +
      '<div class="section-head"><h2>策略列表</h2><span class="hint">使用中訊號槽 ' + n + "</span></div>" +
      '<div class="strategy-grid">' + (cards || '<div class="empty-state">無策略</div>') + "</div></section>";
  }

  function renderTrades() {
    var rows = sortDesc(settlements.filter(isTrade)).slice(0, 10);
    var body = rows.length ? rows.map(tradeRow).join("") :
      '<tr><td colspan="9" class="empty-row">尚無成交</td></tr>';
    return '<section class="section" id="sec-trades">' +
      '<div class="section-head"><h2>最近成交</h2><span class="hint">Binance Demo 真實成交 · 原因已中文化</span></div>' +
      '<div class="card"><div class="table-scroll"><table class="data">' +
      "<thead><tr><th>時間</th><th>類型</th><th>標的</th><th>方向</th><th class=\"num\">數量</th><th class=\"num\">價格</th><th class=\"num\">手續費</th><th class=\"num\">已實現損益</th><th>原因</th></tr></thead>" +
      "<tbody>" + body + "</tbody></table></div></div></section>";
  }

  function mountPie(parts) {
    var canvas = $("allocPie");
    if (!canvas || typeof Chart === "undefined") return;
    var data = (parts || []).filter(function (p) { return p.value > 0; });
    if (!data.length) return;
    if (canvas._chart) canvas._chart.destroy();
    canvas._chart = new Chart(canvas, {
      type: "doughnut",
      data: {
        labels: data.map(function (d) { return d.label; }),
        datasets: [{ data: data.map(function (d) { return d.value; }),
          backgroundColor: ["#3b82f6", "#22d3ee", "#22c55e", "#f59e0b"], borderWidth: 0 }]
      },
      options: {
        plugins: { legend: { display: true, position: "bottom",
          labels: { color: "#8b9bb0", boxWidth: 10, font: { size: 10 } } } },
        cutout: "62%"
      }
    });
  }

  function renderAll() {
    var main = $("main");
    if (!main) return;
    if (!book) {
      main.className = "";
      main.innerHTML = '<div class="empty-state">無法載入 live_book.json</div>';
      return;
    }
    main.className = "";
    main.innerHTML = renderHealth() + renderAccount() + renderChanges() +
      renderActual() + renderPlanned() + renderStrategies() + renderTrades();
    mountPie(pieParts);
  }

  async function loadAll() {
    if (loading) return;
    loading = true;
    showErr("");
    try {
      book = await getJSON(BOOK_URL);
      health = await getJSONOpt(HEALTH_URL);
      positions = await getJSONOpt(POSITIONS_URL);
      paper = await getJSONOpt(PAPER_URL);
      settlements = await loadSettlements();
      refreshSec = Number(book.auto_refresh_sec) || 30;
      if ($("pageTitle")) $("pageTitle").textContent = book.title || "Binance Demo 帳戶（真實成交）";
      if ($("pageSub")) {
        $("pageSub").textContent = (book.source_label || "Binance Demo") + " · 訊號監控 · 靜態／GitHub Pages";
      }
      renderAll();
      if ($("lastUpdated")) {
        $("lastUpdated").innerHTML = "最後更新：<strong>" + esc(nowLabel()) + "</strong>";
      }
    } catch (e) {
      showErr("載入失敗：" + (e.message || e));
      if ($("main") && $("main").classList.contains("loading")) $("main").textContent = "載入失敗";
    } finally {
      loading = false;
      countdown = refreshSec;
      updateCountdown();
    }
  }

  function updateCountdown() {
    var el = $("countdownPill");
    if (!el) return;
    el.textContent = autoOn ? ("倒數：" + countdown + "s") : "自動重整：關";
  }

  function startTimer() {
    if (timerId) clearInterval(timerId);
    timerId = setInterval(function () {
      if (!autoOn) { updateCountdown(); return; }
      countdown -= 1;
      if (countdown <= 0) loadAll();
      else updateCountdown();
    }, 1000);
  }

  function init() {
    var btn = $("btnRefresh");
    if (btn) btn.addEventListener("click", function () { countdown = refreshSec; loadAll(); });
    var chk = $("autoRefresh");
    if (chk) {
      chk.addEventListener("change", function () {
        autoOn = chk.checked; countdown = refreshSec; updateCountdown();
      });
      autoOn = chk.checked;
    }
    loadAll();
    startTimer();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
