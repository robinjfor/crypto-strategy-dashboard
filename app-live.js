/* Homepage live view — prefers Cloud Run GET /status; static fallback. Mirror to live.js */
(function () {
  "use strict";

  const BOOK_URL = "./data/live_book.json";
  const HEALTH_URL = "./state/health.json";
  const POSITIONS_URL = "./state/positions.json";
  const PAPER_URL = "./state/paper_trading.json";
  const SETTLEMENT_URL = "./data/strategy-crypto-s2/settlement.json";
  const SETTLEMENT_JSONL = "./state/settlement.jsonl";
  const CLOUD_CFG = "./data/cloud_api.json";

  let book = null;
  let health = null;
  let positions = null;
  let paper = null;
  let settlements = [];
  let cloud = null;
  let cloudOk = false;
  let cloudErr = "";
  let apiBase = "";
  let loading = false;
  let autoOn = true;
  let refreshSec = 30;
  let countdown = 30;
  let timerId = null;
  let pieParts = [];

  function $(id) { return document.getElementById(id); }


  var TARGET_PCT = { FET: 25, OP: 20, DOT: 20, SOL: 30 };
  var STATUS_ZH = {
    waiting_breakout: "等訊號",
    wait_breakout: "等訊號",
    WAIT_BREAKOUT: "等訊號",
    armed: "已武裝",
    ARMED: "已武裝",
    WAIT_RESET: "等回落重置（需先收回上軌下方）",
    wait_reset: "等回落重置（需先收回上軌下方）",
    wait_signal_reset_then_breakout: "等回落重置（需先收回上軌下方）",
    WAIT_SIGNAL_RESET_THEN_BREAKOUT: "等回落重置（需先收回上軌下方）",
    PENDING_FILL: "已掛單，等成交",
    pending_fill: "已掛單，等成交"
  };

  function statusZh(code) {
    if (code == null || code === "") return "—";
    var s = String(code).trim();
    if (STATUS_ZH[s]) return STATUS_ZH[s];
    var low = s.toLowerCase();
    for (var k in STATUS_ZH) {
      if (k.toLowerCase() === low) return STATUS_ZH[k];
    }
    return s + "（未對照）";
  }

  function targetPctFor(pl) {
    if (pl && pl.target_pct != null && pl.target_pct !== "") return Number(pl.target_pct);
    var asset = String((pl && (pl.asset || pl.symbol)) || "").replace("USDT", "").toUpperCase();
    if (TARGET_PCT[asset] != null) return TARGET_PCT[asset];
    return null;
  }

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

  function showCloudBanner(on) {
    var el = $("cloudBanner");
    if (!el) return;
    if (on) el.classList.remove("hidden");
    else el.classList.add("hidden");
  }

  async function getJSON(url) {
    var res = await fetch(url + (url.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now(), { cache: "no-store" });
    if (!res.ok) throw new Error(url + " HTTP " + res.status);
    return res.json();
  }

  async function getJSONOpt(url) {
    try { return await getJSON(url); } catch (e) { return null; }
  }

  async function resolveApiBase() {
    try {
      var c = await getJSON(CLOUD_CFG);
      if (c && c.base) return String(c.base).replace(/\/$/, "");
    } catch (e) {}
    if (window.TRADER_API_BASE) return String(window.TRADER_API_BASE).replace(/\/$/, "");
    return "";
  }

  async function loadSettlements() {
    if (cloud && Array.isArray(cloud.closed_trades) && cloud.closed_trades.length) {
      return cloud.closed_trades.map(function (t) {
        return {
          ts: t.closed_at || t.time,
          kind: "CLOSE",
          symbol: t.symbol,
          side: "SELL",
          qty: t.qty,
          price: t.exit,
          realized_pnl_usdt: t.pnl_usdt,
          reason: t.reason || "manual"
        };
      });
    }
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
      var ta = parseTs(a.ts || a.time || a.closed_at);
      var tb = parseTs(b.ts || b.time || b.closed_at);
      return (tb ? tb.getTime() : 0) - (ta ? ta.getTime() : 0);
    });
  }

  function isTrade(r) {
    var sym = String(r.symbol || "").toUpperCase();
    if (sym.indexOf("OPUSDT") >= 0 || sym.indexOf("FILUSDT") >= 0) return false;
    if (sym === "OP" || sym === "FIL") return false;
    return true;
  }

  function getPin() {
    try { return sessionStorage.getItem("trader_pin") || ""; } catch (e) { return ""; }
  }
  function setPin(v, remember) {
    try {
      if (remember) sessionStorage.setItem("trader_pin", v);
      else sessionStorage.removeItem("trader_pin");
    } catch (e) {}
  }

  function bals() {
    if (cloud && cloud.balances_map) {
      var m = cloud.balances_map;
      return { USDT: m.USDT != null ? Number(m.USDT) : null, USDC: m.USDC != null ? Number(m.USDC) : null, NEAR: m.NEAR != null ? Number(m.NEAR) : 0 };
    }
    if (cloud && Array.isArray(cloud.balances)) {
      var o = { USDT: null, USDC: null, NEAR: 0 };
      cloud.balances.forEach(function (b) {
        var free = Number(b.free || 0) + Number(b.locked || 0);
        if (b.asset === "USDT") o.USDT = free;
        if (b.asset === "USDC") o.USDC = free;
        if (b.asset === "NEAR") o.NEAR = free;
      });
      return o;
    }
    var b = (positions && positions.balances) || (paper && paper.balances) || (book && book.balances) || {};
    return {
      USDT: b.USDT != null ? Number(b.USDT) : null,
      USDC: b.USDC != null ? Number(b.USDC) : null,
      NEAR: b.NEAR != null ? Number(b.NEAR) : 0
    };
  }

  function openPositions() {
    if (cloud && Array.isArray(cloud.positions)) return cloud.positions;
    if (cloud && Array.isArray(cloud.open_positions)) return cloud.open_positions;
    return (positions && Array.isArray(positions.positions) && positions.positions) ||
      (book && book.open_positions) || [];
  }

  function renderHealth() {
    var h = (cloud && cloud.health) || health || {};
    var color = h.color || "idle";
    var label = h.label || h.automation_label || "自動執行：尚未啟用（等待雲端主機）";
    var paused = cloud ? !!cloud.paused : !!h.paused;
    var cls = color === "green" ? "ok" : color === "yellow" ? "warn" : color === "red" ? "bad" : "idle";
    var toggleLabel = paused ? "恢復自動交易" : "暫停自動交易";
    var stateLabel = paused ? "已暫停（不開新倉）" : "自動交易中";
    return '<div class="health-bar ' + cls + '" id="systemHealth">' +
      '<span class="health-light ' + cls + '">● ' + esc(label) + '</span>' +
      '<span class="health-meta">狀態：' + esc(stateLabel) + '</span>' +
      '<span class="health-meta">模式：' + esc((cloud && cloud.mode) || (book && book.mode) || "—") + '</span>' +
      '<span class="health-meta">上次任務：' + esc((cloud && cloud.last_job_run_at) || "—") + '</span>' +
      '<button type="button" class="btn-toggle ' + (paused ? "resume" : "pause") + '" id="btnPauseToggle">' +
      esc(toggleLabel) + '</button>' +
      '</div>';
  }

  function renderAccount() {
    var b = bals();
    var usdt = b.USDT, usdc = b.USDC;
    var total = (usdt != null && usdc != null) ? usdt + usdc
      : (book && book.equity_total_stable != null ? Number(book.equity_total_stable) : null);
    var equity = cloud && cloud.equity_usdt != null ? Number(cloud.equity_usdt)
      : (positions && positions.virtual_equity != null) ? Number(positions.virtual_equity)
      : (book && book.equity_usdt != null ? Number(book.equity_usdt) : usdt);
    var openN = openPositions().filter(function (p) {
      return !p.status || p.status === "FILLED" || p.status === "OPEN";
    }).length;
    pieParts = [];
    if (usdt != null && usdt > 0) pieParts.push({ label: "USDT", value: usdt });
    if (usdc != null && usdc > 0) pieParts.push({ label: "USDC", value: usdc });
    return '<section class="section" id="sec-account">' +
      '<div class="section-head"><h2>帳戶總覽</h2><span class="hint">' +
      esc((cloud && cloud.source_label) || (book && book.source_label) || "Binance Demo") + '</span></div>' +
      '<div class="overview-row"><div class="kpi-grid kpi-grid-demo">' +
      '<div class="kpi"><div class="label">USDT</div><div class="value">' + num(usdt, 2) + '</div><div class="sublabel">可用餘額</div></div>' +
      '<div class="kpi"><div class="label">USDC</div><div class="value">' + num(usdc, 2) + '</div><div class="sublabel">可用餘額</div></div>' +
      '<div class="kpi kpi-emphasis"><div class="label">穩定幣合計</div><div class="value">' + num(total, 2) + '</div><div class="sublabel">USDT + USDC</div></div>' +
      '<div class="kpi"><div class="label">USDT 側權益</div><div class="value">' + num(equity, 2) + '</div><div class="sublabel">雲端即時</div></div>' +
      '<div class="kpi"><div class="label">實際持倉數</div><div class="value">' + num(openN, 0) + '</div><div class="sublabel">' +
      (openN === 0 ? "目前空倉（全現金）" : "幣種倉") + '</div></div>' +
      '</div><div class="kpi kpi-pie"><div class="label">現金結構</div><div class="alloc-pie-wrap"><canvas id="allocPie" width="120" height="120"></canvas></div></div></div></section>';
  }

  function tradeRow(r) {
    var ts = r.ts || r.time || r.closed_at || "—";
    var kind = r.kind || (r.side === "BUY" ? "OPEN" : "CLOSE");
    var sym = r.symbol || "—";
    var side = r.side || "—";
    var qty = r.qty;
    var px = r.price != null ? r.price : r.avg_price != null ? r.avg_price : r.exit;
    var fee = r.fee != null ? r.fee : r.commission;
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
    var open = openPositions().filter(function (p) {
      return !p.status || p.status === "FILLED" || p.status === "OPEN";
    });
    var body;
    if (!open.length) {
      body = '<tr><td colspan="10" class="empty-row">目前空倉（全現金）</td></tr>';
    } else {
      body = open.map(function (p) {
        var upnl = p.unrealized_pnl != null ? p.unrealized_pnl : p.unrealized_usdt;
        var slot = p.slot || "";
        return "<tr>" +
          "<td><strong>" + esc(p.symbol || p.asset || "—") + '</strong><div class="kpi-sub">' + esc(slot) + "</div></td>" +
          "<td>" + esc(p.tf || "—") + "</td>" +
          '<td class="num">' + num(p.qty, 4) + "</td>" +
          '<td class="num">' + num(p.entry != null ? p.entry : p.entry_price, 4) + "</td>" +
          '<td class="num">' + (p.mark_price != null || p.mark != null ? num(p.mark_price != null ? p.mark_price : p.mark, 4) : "—") + "</td>" +
          '<td class="num">' + (p.stop != null ? num(p.stop, 4) : "—") + '<div class="kpi-sub">移動止損</div></td>' +
          '<td class="num">' + (p.donch_lo != null ? num(p.donch_lo, 4) : "—") + '<div class="kpi-sub">出場下軌</div></td>' +
          '<td class="num">' + num((p.entry || p.entry_price || 0) * (p.qty || 0), 2) + "</td>" +
          '<td class="num ' + signedCls(upnl) + '">' + (upnl == null ? "—" : num(upnl, 2)) + "</td>" +
          '<td><button type="button" class="btn-close-pos" data-slot="' + esc(slot) + '" data-sym="' + esc(p.symbol || "") + '">手動平倉</button></td></tr>';
      }).join("");
    }
    return '<section class="section" id="sec-actual">' +
      '<div class="section-head"><h2>實際持倉</h2><span class="hint">雲端即時 · Demo 真實部位</span></div>' +
      '<div class="card"><div class="table-scroll"><table class="data">' +
      "<thead><tr><th>標的／策略</th><th>週期</th><th class=\"num\">數量</th><th class=\"num\">進場價</th><th class=\"num\">現價</th><th class=\"num\">移動止損</th><th class=\"num\">出場下軌</th><th class=\"num\">市值</th><th class=\"num\">未實現損益</th><th>操作</th></tr></thead>" +
      "<tbody>" + body + "</tbody></table></div></div></section>";
  }

  function plannedList() {
    if (cloud && Array.isArray(cloud.planned_positions)) {
      return cloud.planned_positions.map(function (pl) {
        var copy = Object.assign({}, pl);
        copy.status_code = pl.status_code || pl.ui_status || pl.status_label;
        // If API already sent Chinese status_label, keep it; else map
        var raw = pl.status_code || pl.ui_status || "";
        if (pl.status_label && /[\u4e00-\u9fff]/.test(String(pl.status_label))) {
          copy.status_label = pl.status_label;
        } else {
          copy.status_label = statusZh(pl.status_label || pl.ui_status || pl.status_code);
        }
        if (copy.target_pct == null) copy.target_pct = targetPctFor(copy);
        return copy;
      });
    }
    if (cloud && Array.isArray(cloud.armed_slots)) {
      return cloud.armed_slots.map(function (a) {
        return {
          asset: String(a.symbol || "").replace("USDT", ""),
          symbol: a.symbol,
          slot: a.slot,
          strategy_name: a.variant,
          tf: a.tf,
          target_notional_usdt: a.quote_usdt,
          ui_status: a.status || a.action || "ARMED",
          status_label: a.status_zh || statusZh(a.status || a.reason || a.action),
          status_code: a.status || a.reason || a.action,
          entry_rule: a.entry_condition,
          mark: a.mark,
          trigger: a.trigger,
          target_pct: a.target_pct,
          target_notional_usdt: a.quote_usdt || a.target_notional_usdt,
          stop_note: a.suggested_stop != null ? ("建議止損 " + a.suggested_stop) : "—",
          stop_mode: "pending",
          donch_n: a.slot === "sat_fet_1h" ? 55 : 20
        };
      });
    }
    var planned = (book && Array.isArray(book.planned_positions) ? book.planned_positions : []).slice();
    return planned.filter(function (p) {
      var st = String(p.ui_status || p.status || "").toUpperCase();
      return st.indexOf("WAIT_BREAKOUT") >= 0 || st.indexOf("WAIT_RESET") >= 0 ||
        st.indexOf("REARM") >= 0 || st.indexOf("PENDING") >= 0 || st.indexOf("ARMED") >= 0;
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
          : (pl.stop_note || "—");
        var safe = String(stopNote).replace(/目標價/g, "止損參考");
        return "<tr>" +
          "<td><strong>" + esc(pl.asset) + '</strong><div class="kpi-sub">' + esc(pl.strategy_name || "") + "</div></td>" +
          '<td class="num">' + (targetPctFor(pl) != null ? num(targetPctFor(pl), 0) + "%" : "—") + "</td>" +
          '<td class="num">' + num(pl.target_notional_usdt, 0) + "</td>" +
          "<td>" + esc(pl.status_label || statusZh(pl.status_code || pl.ui_status) || "—") + "</td>" +
          "<td>" + esc(pl.tf || "—") + " / Donch " + esc(String(pl.donch_n || "—")) + "</td>" +
          "<td>" + esc(pl.entry_rule || "—") + "</td>" +
          "<td>" + esc(safe) + "</td>" +
          '<td class="num">' + (pl.mark != null ? num(pl.mark, 4) : "—") +
          '<div class="kpi-sub">觸發 ' + (pl.trigger != null ? num(pl.trigger, 4) : "—") +
          (pl.trigger != null && pl.mark != null ? " · 距 " + num(Number(pl.trigger) - Number(pl.mark), 4) : "") +
          "</div></td></tr>";
      }).join("");
    }
    return '<section class="section" id="sec-planned">' +
      '<div class="section-head"><h2>預計持倉</h2><span class="hint">雲端訊號槽 · FET / OP / DOT / SOL</span></div>' +
      '<div class="card"><div class="table-scroll"><table class="data">' +
      "<thead><tr><th>標的</th><th class=\"num\">目標%</th><th class=\"num\">名義 USDT</th><th>狀態</th><th>週期</th><th>進場條件</th><th>止損／出場</th><th class=\"num\">現價／觸發</th></tr></thead>" +
      "<tbody>" + body + "</tbody></table></div></div></section>";
  }

  function renderStrategies() {
    var armed = (cloud && cloud.armed_slots) || [];
    var sats = (book && book.satellite_strategies) || [];
    var core = (book && book.strategies) || [];
    var cards;
    if (armed.length) {
      cards = armed.map(function (s) {
        var active = true;
        return '<article class="strategy-card ' + (active ? "active" : "") + '">' +
          '<div class="sc-top"><strong>' + esc(s.symbol || s.slot) + '</strong>' +
          '<span class="badge ok">' + esc(s.status_zh || statusZh(s.status || s.reason || s.action || "armed")) + "</span></div>" +
          '<p class="sc-sum">' + esc(s.entry_condition || s.variant || "") + "</p>" +
          '<div class="sc-meta">' + esc(s.tf || "") + " · " + esc(s.symbol || "") + " · 1x · 名義 " +
          esc(String(s.quote_usdt || "")) + "</div>" +
          '<div class="sc-rules">' +
          '<div><span class="lbl">進場</span> ' + esc(s.entry_condition || "—") + "</div>" +
          '<div><span class="lbl">觸發</span> ' + (s.trigger != null ? num(s.trigger, 4) : "—") +
          " · 現價 " + (s.mark != null ? num(s.mark, 4) : "—") + "</div>" +
          '<div><span class="lbl">狀態</span> ' + esc(s.status_zh || statusZh(s.status || s.reason) || "—") + "</div>" +
          "</div></article>";
      }).join("");
    } else {
      cards = core.concat(sats).map(function (s) {
        var active = s.status === "active" || s.status === "armed_wait_breakout" || s.status === "wait_reset";
        return '<article class="strategy-card ' + (active ? "active" : "") + '">' +
          '<div class="sc-top"><strong>' + esc(s.name || s.id || "—") + '</strong>' +
          '<span class="badge ' + (active ? "ok" : "muted") + '">' + esc(statusZh(s.status) || "—") + "</span></div>" +
          '<p class="sc-sum">' + esc(s.summary || "") + "</p></article>";
      }).join("");
    }
    return '<section class="section" id="sec-strategies">' +
      '<div class="section-head"><h2>策略列表</h2><span class="hint">使用中訊號槽 ' + plannedList().length + "</span></div>" +
      '<div class="strategy-grid">' + (cards || '<div class="empty-state">無策略</div>') + "</div></section>";
  }

  function renderTrades() {
    var fills = (cloud && cloud.recent_fills) || [];
    var rows;
    if (fills.length) {
      rows = fills.map(function (t) {
        return {
          ts: t.time, kind: t.side === "BUY" ? "OPEN" : "CLOSE", symbol: t.symbol,
          side: t.side, qty: t.qty, price: t.price, fee: t.commission, reason: "myTrades"
        };
      });
    } else {
      rows = sortDesc(settlements.filter(isTrade)).slice(0, 10);
    }
    var body = rows.length ? rows.slice(0, 10).map(tradeRow).join("") :
      '<tr><td colspan="9" class="empty-row">尚無成交</td></tr>';
    return '<section class="section" id="sec-trades">' +
      '<div class="section-head"><h2>最近成交</h2><span class="hint">Binance Demo · 雲端 myTrades</span></div>' +
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

  function openPinModal(opts) {
    opts = opts || {};
    var modal = $("strategyModal");
    var body = $("modalBody");
    if (!modal || !body) return;
    var title = opts.title || "操作密碼";
    var confirmText = opts.confirmText || "";
    var saved = getPin();
    body.innerHTML =
      '<h3>' + esc(title) + '</h3>' +
      (confirmText ? '<p class="modal-confirm">' + esc(confirmText) + '</p>' : '') +
      '<label class="pin-label">操作密碼<input type="password" id="pinInput" class="pin-input" autocomplete="off" value="' + esc(saved) + '" /></label>' +
      '<label class="pin-remember"><input type="checkbox" id="pinRemember" ' + (saved ? "checked" : "") + ' /> 記住於本次瀏覽（sessionStorage）</label>' +
      '<div class="modal-actions">' +
      '<button type="button" class="primary" id="pinSubmit">確認</button>' +
      '<button type="button" id="pinCancel">取消</button></div>' +
      '<p class="modal-result" id="pinResult"></p>';
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    var submit = function () {
      var pin = ($("pinInput") && $("pinInput").value) || "";
      var remember = $("pinRemember") && $("pinRemember").checked;
      setPin(pin, remember);
      if (typeof opts.onSubmit === "function") opts.onSubmit(pin, $("pinResult"));
    };
    if ($("pinSubmit")) $("pinSubmit").onclick = submit;
    if ($("pinCancel")) $("pinCancel").onclick = closeModal;
    if ($("pinInput")) $("pinInput").onkeydown = function (e) {
      if (e.key === "Enter") submit();
    };
  }

  function closeModal() {
    var modal = $("strategyModal");
    if (!modal) return;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  async function postControl(path, pin, body) {
    if (!apiBase) throw new Error("尚未設定雲端 API");
    var res = await fetch(apiBase + path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Trader-Pin": pin
      },
      body: body ? JSON.stringify(body) : "{}"
    });
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    return data;
  }

  function bindControls() {
    var btn = $("btnPauseToggle");
    if (btn) {
      btn.onclick = function () {
        var paused = cloud && cloud.paused;
        openPinModal({
          title: paused ? "恢復自動交易" : "暫停自動交易",
          confirmText: paused ? "確定恢復自動開倉？" : "確定暫停？既有持倉仍會管理止損／出場。",
          onSubmit: function (pin, resultEl) {
            resultEl.textContent = "處理中…";
            postControl(paused ? "/control/resume" : "/control/pause", pin)
              .then(function (r) {
                resultEl.textContent = r.message || "完成";
                setTimeout(function () { closeModal(); loadAll(); }, 600);
              })
              .catch(function (e) {
                resultEl.textContent = "失敗：" + (e.message || e);
              });
          }
        });
      };
    }
    document.querySelectorAll(".btn-close-pos").forEach(function (b) {
      b.onclick = function () {
        var slot = b.getAttribute("data-slot") || "";
        var sym = b.getAttribute("data-sym") || slot;
        openPinModal({
          title: "手動平倉",
          confirmText: "確定以市價平掉 " + sym + "？",
          onSubmit: function (pin, resultEl) {
            resultEl.textContent = "處理中…";
            postControl("/control/close", pin, { slot: slot })
              .then(function (r) {
                resultEl.textContent = r.message || "已平倉";
                setTimeout(function () { closeModal(); loadAll(); }, 700);
              })
              .catch(function (e) {
                resultEl.textContent = "失敗：" + (e.message || e);
              });
          }
        });
      };
    });
    var mc = $("modalClose");
    if (mc) mc.onclick = closeModal;
    var backdrop = document.querySelector("#strategyModal .modal-backdrop");
    if (backdrop) backdrop.onclick = closeModal;
  }

  function renderAll() {
    var main = $("main");
    if (!main) return;
    if (!book && !cloud) {
      main.className = "";
      main.innerHTML = '<div class="empty-state">無法載入雲端狀態或 live_book.json</div>';
      return;
    }
    main.className = "";
    main.innerHTML = renderHealth() + renderAccount() + renderChanges() +
      renderActual() + renderPlanned() + renderStrategies() + renderTrades();
    mountPie(pieParts);
    bindControls();
  }

  async function loadCloud() {
    apiBase = await resolveApiBase();
    if (!apiBase) {
      cloudOk = false;
      cloudErr = "未設定 API";
      return null;
    }
    try {
      var res = await fetch(apiBase + "/status?t=" + Date.now(), {
        cache: "no-store",
        mode: "cors",
        headers: { "Accept": "application/json" }
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      cloud = await res.json();
      cloudOk = !!(cloud && cloud.ok !== false);
      cloudErr = "";
      return cloud;
    } catch (e) {
      cloudOk = false;
      cloudErr = e.message || String(e);
      cloud = null;
      return null;
    }
  }

  async function loadAll() {
    if (loading) return;
    loading = true;
    showErr("");
    try {
      await loadCloud();
      showCloudBanner(!cloudOk);
      if (!cloudOk) {
        book = await getJSONOpt(BOOK_URL);
        health = await getJSONOpt(HEALTH_URL);
        positions = await getJSONOpt(POSITIONS_URL);
        paper = await getJSONOpt(PAPER_URL);
      } else {
        book = {
          title: cloud.title,
          source_label: cloud.source_label,
          mode: cloud.mode,
          planned_positions: cloud.planned_positions,
          open_positions: cloud.open_positions,
          auto_refresh_sec: 30
        };
        health = cloud.health;
        positions = { positions: cloud.positions, balances: cloud.balances_map };
      }
      settlements = await loadSettlements();
      refreshSec = Number((book && book.auto_refresh_sec) || 30);
      if ($("pageTitle")) $("pageTitle").textContent = (cloud && cloud.title) || (book && book.title) || "Binance Demo 帳戶（真實成交）";
      if ($("pageSub")) {
        $("pageSub").textContent = cloudOk
          ? "Binance Demo · 雲端即時 · Cloud Run"
          : ((book && book.source_label) || "Binance Demo") + " · 快取／靜態";
      }
      renderAll();
      if ($("lastUpdated")) {
        $("lastUpdated").innerHTML = "最後更新：<strong>" + esc(nowLabel()) + "</strong>" +
          (cloudOk ? " · 雲端" : " · 快取");
      }
    } catch (e) {
      showErr("載入失敗：" + (e.message || e));
    } finally {
      loading = false;
    }
  }

  function tick() {
    if (!autoOn) return;
    countdown -= 1;
    if ($("countdownPill")) $("countdownPill").textContent = "倒數：" + countdown + "s";
    if (countdown <= 0) {
      countdown = refreshSec;
      loadAll();
    }
  }

  function boot() {
    if ($("btnRefresh")) $("btnRefresh").onclick = function () { countdown = refreshSec; loadAll(); };
    if ($("autoRefresh")) {
      $("autoRefresh").onchange = function () {
        autoOn = !!$("autoRefresh").checked;
      };
    }
    loadAll();
    timerId = setInterval(tick, 1000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
