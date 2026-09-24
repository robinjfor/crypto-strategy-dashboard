/* Homepage live view — prefers Cloud Run GET /status; static fallback. Mirror to live.js */
(function () {
  "use strict";

  const BOOK_URL = "./data/live_book.json";

  const ALLOC_URL = "./data/book_allocation.json";
  const SCORES_URL = "./data/unified-3y/scores.json";
  let allocCfg = null;
  let scoresById = {};

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
    if (pl && pl.target_notional_usdt != null && bookUsdt() > 0) {
      return (Number(pl.target_notional_usdt) / bookUsdt()) * 100;
    }
    var sid = pl && pl.strategy_id;
    var live = approvedLiveSlots();
    for (var i = 0; i < live.length; i++) {
      if (sid && live[i].strategy_id === sid) {
        var n = Number(live[i].notional_usdt || 0);
        return bookUsdt() > 0 ? (n / bookUsdt()) * 100 : null;
      }
      if (pl.slot && live[i].slot === pl.slot) {
        var n2 = Number(live[i].notional_usdt || 0);
        return bookUsdt() > 0 ? (n2 / bookUsdt()) * 100 : null;
      }
    }
    var asset = String((pl && (pl.asset || pl.symbol)) || "").replace("USDT", "").toUpperCase();
    if (TARGET_PCT[asset] != null) return TARGET_PCT[asset];
    return null;
  }


  function bookUsdt() {
    return Number((allocCfg && allocCfg.book_usdt) || 5000);
  }

  /** Approved slots that are actually deployed (order_mode=live). Homepage source of truth. */
  function approvedLiveSlots() {
    if (!cloud) return [];
    // Prefer allocation ∩ approved_families from /status.live_slots
    if (Array.isArray(cloud.live_slots) && cloud.live_slots.length) {
      return cloud.live_slots.map(function (s) {
        return {
          strategy_id: s.strategy_id,
          slot: s.slot,
          symbol: s.symbol,
          tf: s.timeframe || s.tf,
          family: s.family,
          notional_usdt: s.notional_usdt,
          order_mode: "live",
          mode: "live",
          approved: true,
          label_zh: "已核准 · 上線待命"
        };
      });
    }
    var list = cloud.approved || cloud.approved_list || [];
    if (!Array.isArray(list) && list && typeof list === "object") {
      list = Object.keys(list).map(function (k) {
        return Object.assign({ strategy_id: k }, list[k]);
      });
    }
    var fams = cloud.approved_families || [];
    return (list || []).filter(function (a) {
      if (!a) return false;
      if (a.approved === false) return false;
      var mode = a.order_mode || a.mode || "live";
      if (mode !== "live") return false;
      if (fams.length && a.family && fams.indexOf(a.family) < 0) return false;
      return true;
    });
  }

  function armedFor(slotId, strategyId) {
    var armed = (cloud && (cloud.armed_slots || cloud.slots)) || [];
    for (var i = 0; i < armed.length; i++) {
      var a = armed[i];
      if ((slotId && (a.slot === slotId || a.id === slotId)) ||
          (strategyId && a.strategy_id === strategyId)) {
        return a;
      }
    }
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

  function signedNum(v, d) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    var n = Number(v);
    return n > 0 ? "+" + num(n, d) : num(n, d);
  }

  function formatPnl(pnl, pct) {
    if (pnl == null || Number.isNaN(Number(pnl))) return "—";
    var out = signedNum(pnl, 2) + " USDT";
    if (pct != null && !Number.isNaN(Number(pct))) out += " (" + signedNum(pct, 2) + "%)";
    return out;
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

  function fetchWithTimeout(url, opts, ms) {
    opts = opts || {};
    ms = ms || 8000;
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, ms);
    return fetch(url, Object.assign({}, opts, { signal: ctrl.signal }))
      .finally(function () { clearTimeout(timer); });
  }

  async function fetchJSONRetry(url, opts, ms, retries) {
    ms = ms || 8000;
    retries = retries == null ? 1 : retries;
    var lastErr = null;
    for (var i = 0; i <= retries; i++) {
      try {
        var res = await fetchWithTimeout(url, opts, ms);
        if (!res.ok) throw new Error("HTTP " + res.status);
        return await res.json();
      } catch (e) {
        lastErr = e;
        if (i < retries) await new Promise(function (r) { setTimeout(r, 400); });
      }
    }
    throw lastErr || new Error("fetch failed");
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
    if (cloud && (cloud.balances_map || cloud.balances_map)) {
      var m = cloud.balances_map || cloud.balances_map;
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
    var color = (cloud && cloud.health && cloud.health.color) || h.color || (cloudOk && !(cloud && cloud.paused) ? "green" : (cloudOk ? "yellow" : "idle"));
    var label = (cloud && cloud.health && cloud.health.label) || h.label || h.automation_label || (cloudOk ? "自動交易中" : "雲端連線失敗");
    var paused = cloud ? !!cloud.paused : !!h.paused;
    var cls = color === "green" ? "ok" : color === "yellow" ? "warn" : color === "red" ? "bad" : "idle";
    var toggleLabel = paused ? "恢復自動交易" : "暫停自動交易";
    var stateLabel = paused ? "已暫停（不開新倉）" : "自動交易中";
    var openN = openPositions().filter(function (p) {
      return !p.status || p.status === "FILLED" || p.status === "OPEN";
    }).length;
    return '<div class="health-bar ' + cls + '" id="systemHealth">' +
      '<span class="health-light ' + cls + '">● ' + esc(label) + '</span>' +
      '<span class="health-meta">狀態：' + esc(stateLabel) + '</span>' +
      '<span class="health-meta">模式：' + esc((cloud && cloud.mode) || (book && book.mode) || "—") + '</span>' +
      '<span class="health-meta">上次任務：' + esc((cloud && cloud.last_job_run_at) || "—") + '</span>' +
      '</div>' +
      '<div class="ops-panel" id="opsPanel">' +
      '<div class="ops-head"><strong>操作區</strong>' +
      '<span class="ops-pos-hint">' + (openN === 0 ? "目前沒有持倉，將只暫停" : ("目前持倉 " + openN + " 檔")) + '</span></div>' +
      '<div class="ops-actions">' +
      '<button type="button" class="btn-toggle ' + (paused ? "resume" : "pause") + '" id="btnPauseToggle">' +
      esc(toggleLabel) + '</button>' +
      '<button type="button" class="btn-close-all" id="btnCloseAll">全部平倉並暫停</button>' +
      '</div>' +
      '<p class="ops-help">暫停：不再開新倉，已持有的倉照止損管理。全部平倉：立即市價賣出所有持倉並暫停。</p>' +
      '<div class="ops-result hidden" id="closeAllResult"></div>' +
      '</div>';
  }


  var DUST_USDT = 1;

  function actualHoldings() {
    var rows = [];
    var balMap = (cloud && (cloud.balances_map || cloud.balances_map)) || {};
    openPositions().forEach(function (p) {
      if (p.status && p.status !== "FILLED" && p.status !== "OPEN") return;
      var sym = String(p.symbol || p.raw_symbol || "").toUpperCase();
      var asset = String(p.asset || sym.replace(/USDT$/i, "")).toUpperCase();
      var rawQty = Number(p.qty != null ? p.qty : (p.quantity != null ? p.quantity : p.position_amt)) || 0;
      var qty = Math.abs(rawQty);
      var entry = Number(p.entry != null ? p.entry : (p.entry_price != null ? p.entry_price : (p.avg_entry_price != null ? p.avg_entry_price : p.avgPrice))) || 0;
      var px = Number(
        p.mark_price != null ? p.mark_price : (p.mark != null ? p.mark : p.price)
      ) || 0;
      var mv = qty * px;
      if (!(mv >= DUST_USDT)) return;
      var side = String(p.side || p.position_side || p.positionSide || "").toUpperCase();
      var isShort = side === "SHORT" || side === "SELL" || rawQty < 0;
      var rawPnl = p.unrealized_pnl != null ? p.unrealized_pnl : (p.unrealized_usdt != null ? p.unrealized_usdt : p.unrealizedPnl);
      var unrealized = rawPnl != null ? Number(rawPnl) : null;
      if (unrealized == null && entry > 0 && px > 0) {
        unrealized = (isShort ? entry - px : px - entry) * qty;
      }
      var entryCost = entry > 0 ? qty * entry : null;
      var unrealizedPct = unrealized != null && entryCost > 0 ? (unrealized / entryCost) * 100 : null;
      rows.push({
        slot: p.slot || "",
        symbol: sym,
        asset: asset,
        tf: p.tf || p.timeframe || "",
        qty: qty,
        entry: entry || null,
        mark: px,
        stop: p.stop,
        donch_lo: p.donch_lo,
        market_value: mv,
        entry_cost: entryCost,
        unrealized: unrealized,
        unrealized_pct: unrealizedPct,
        side: side,
        bal_qty: balMap[asset] != null ? Number(balMap[asset]) : null
      });
    });
    return rows;
  }

  /** Pie: filled position MVs + USDT cash. Exclude USDC. Dust < 1 USDT ignored. */
  function actualPieParts() {
    var holds = actualHoldings();
    var parts = holds.map(function (h) {
      return { label: h.asset, value: h.market_value, symbol: h.symbol, kind: "pos" };
    });
    var usdt = Number((bals().USDT != null ? bals().USDT : 0)) || 0;
    if (!holds.length) {
      return [{ label: "現金", value: Math.max(usdt, 1), symbol: "USDT", kind: "cash" }];
    }
    if (usdt >= DUST_USDT) {
      parts.push({ label: "現金", value: usdt, symbol: "USDT", kind: "cash" });
    }
    return parts.filter(function (p) { return p.value >= DUST_USDT; });
  }

  function unrealizedSummary() {
    var rows = actualHoldings();
    var pnl = 0, cost = 0, count = 0;
    rows.forEach(function (r) {
      if (r.unrealized == null) return;
      pnl += Number(r.unrealized);
      if (r.entry_cost > 0) cost += Number(r.entry_cost);
      count++;
    });
    if (!count) return { pnl: null, cost: null, pct: null };
    return { pnl: pnl, cost: cost, pct: cost > 0 ? (pnl / cost) * 100 : null };
  }

  function renderAccount() {
    var b = bals();
    var usdt = b.USDT, usdc = b.USDC;
    var total = (usdt != null && usdc != null) ? usdt + usdc
      : (book && book.equity_total_stable != null ? Number(book.equity_total_stable) : null);
    var equity = cloud && cloud.equity_usdt != null ? Number(cloud.equity_usdt)
      : (positions && positions.virtual_equity != null) ? Number(positions.virtual_equity)
      : (book && book.equity_usdt != null ? Number(book.equity_usdt) : usdt);
    var holds = actualHoldings();
    var openN = holds.length;
    var upnl = unrealizedSummary();
    var upnlValue = '<span class="' + signedCls(upnl.pnl) + '">' + formatPnl(upnl.pnl, upnl.pct) + '</span>';
    pieParts = actualPieParts();
    var sum = pieParts.reduce(function (s, p) { return s + p.value; }, 0) || 1;
    var legend = pieParts.map(function (p) {
      var pct = (p.value / sum) * 100;
      return '<div class="pie-legend-row" data-pie-label="' + esc(p.label) + '">' +
        "<strong>" + esc(p.label) + "</strong> " +
        num(pct, 1) + "% · " + num(p.value, 2) + " USDT</div>";
    }).join("");
    return '<section class="section" id="sec-account">' +
      '<div class="section-head"><h2>帳戶總覽</h2><span class="hint">' +
      esc((cloud && cloud.source_label) || (book && book.source_label) || "Binance Demo") + "</span></div>" +
      '<div class="overview-row"><div class="kpi-grid kpi-grid-demo">' +
      '<div class="kpi"><div class="label">USDT</div><div class="value">' + num(usdt, 2) + '</div><div class="sublabel">可用餘額</div></div>' +
      '<div class="kpi"><div class="label">USDC</div><div class="value">' + num(usdc, 2) + '</div><div class="sublabel">不計入圓餅</div></div>' +
      '<div class="kpi kpi-emphasis"><div class="label">穩定幣合計</div><div class="value">' + num(total, 2) + '</div><div class="sublabel">USDT + USDC</div></div>' +
      '<div class="kpi"><div class="label">USDT 側權益</div><div class="value">' + num(equity, 2) + '</div><div class="sublabel">雲端即時</div></div>' +
      '<div class="kpi"><div class="label">未實現損益合計</div><div class="value">' + upnlValue + '</div><div class="sublabel">按進場成本計算</div></div>' +
      '<div class="kpi"><div class="label">實際持倉數</div><div class="value">' + num(openN, 0) + '</div><div class="sublabel">' +
      (openN === 0 ? "目前空倉（全現金）" : "已成交部位") + "</div></div></div></div>" +
      '<div class="holdings-pie-panel">' +
      '<div class="section-head"><h2>實際持倉分布</h2>' +
      '<span class="hint">以 USDT 帳戶計；不含 USDC · 與下方實際持倉同一資料</span></div>' +
      '<div class="pie-panel-body">' +
      '<div class="alloc-pie-wrap"><canvas id="allocPie" width="180" height="180"></canvas></div>' +
      '<div class="pie-legend" id="pieLegend">' + legend + "</div></div></div></section>";
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
    var open = actualHoldings();
    var body;
    if (!open.length) {
      body = '<tr><td colspan="10" class="empty-row">目前空倉（全現金）</td></tr>';
    } else {
      body = open.map(function (p) {
        return '<tr class="pos-row" data-symbol="' + esc(p.symbol) + '" data-asset="' + esc(p.asset) + '">' +
          "<td><strong>" + esc(p.symbol || p.asset || "—") + '</strong><div class="kpi-sub">' + esc(p.slot) + "</div></td>" +
          "<td>" + esc(p.tf || "—") + "</td>" +
          '<td class="num">' + num(p.qty, 4) + "</td>" +
          '<td class="num">' + num(p.entry, 4) + "</td>" +
          '<td class="num">' + num(p.mark, 4) + "</td>" +
          '<td class="num">' + (p.stop != null ? num(p.stop, 4) : "—") + '<div class="kpi-sub">移動止損</div></td>' +
          '<td class="num">' + (p.donch_lo != null ? num(p.donch_lo, 4) : "—") + '<div class="kpi-sub">出場下軌</div></td>' +
          '<td class="num">' + num(p.market_value, 2) + "</td>" +
          '<td class="num ' + signedCls(p.unrealized) + '">' + formatPnl(p.unrealized, p.unrealized_pct) + "</td>" +
          '<td><button type="button" class="btn-close-pos" data-slot="' + esc(p.slot) + '" data-sym="' + esc(p.symbol || "") + '">手動平倉</button></td></tr>';
      }).join("");
    }
    return '<section class="section" id="sec-actual">' +
      '<div class="section-head"><h2>實際持倉</h2><span class="hint">僅已成交部位 · 與圓餅同一資料源</span></div>' +
      '<div class="card"><div class="table-scroll"><table class="data" id="actualTable">' +
      "<thead><tr><th>標的／策略</th><th>週期</th><th class=\"num\">數量</th><th class=\"num\">進場價</th><th class=\"num\">現價</th><th class=\"num\">移動止損</th><th class=\"num\">出場下軌</th><th class=\"num\">市值</th><th class=\"num\">未實現損益</th><th>操作</th></tr></thead>" +
      "<tbody>" + body + "</tbody></table></div></div></section>";
  }

  function plannedList() {
    // ONLY from /status.live_slots. Label from order_mode only (no stale planned_positions labels).
    if (!cloud) return [];
    var fams = cloud.approved_families || [];
    var live = Array.isArray(cloud.live_slots) ? cloud.live_slots : [];
    var armed = Array.isArray(cloud.armed_slots) ? cloud.armed_slots : [];
    var plannedRaw = Array.isArray(cloud.planned_positions) ? cloud.planned_positions : [];
    var cache = (cloud && cloud._market_cache) || {};
    var openSyms = {};
    openPositions().forEach(function (p) {
      if (p.status && p.status !== "FILLED" && p.status !== "OPEN") return;
      var sym = String(p.symbol || p.asset || "").toUpperCase();
      if (sym && sym.indexOf("USDT") < 0) sym += "USDT";
      if (sym) openSyms[sym] = true;
      if (p.slot) openSyms["slot:" + p.slot] = true;
    });

    function enrich(slotId) {
      var i;
      for (i = 0; i < armed.length; i++) {
        if (armed[i].slot === slotId || armed[i].id === slotId) return armed[i];
      }
      for (i = 0; i < plannedRaw.length; i++) {
        if (plannedRaw[i].slot === slotId) return plannedRaw[i];
      }
      if (cache[slotId]) return cache[slotId];
      return {};
    }

    function modeLabel(om) {
      return String(om || "").toLowerCase() === "live" ? "等突破進場" : "只算訊號";
    }

    var out = [];
    live.forEach(function (s) {
      if (!s || s.enabled === false) return;
      var fam = s.family;
      if (fams.length && fam && fams.indexOf(fam) < 0) return;
      var om = String(s.order_mode || s.mode || "live").toLowerCase();
      var sym = String(s.symbol || "").toUpperCase();
      if (openSyms[sym] || openSyms["slot:" + s.slot]) return;

      var src = enrich(s.slot);
      var mark = src.mark != null ? Number(src.mark) : null;
      var trigger = src.trigger != null ? Number(src.trigger)
        : (src.donch_hi != null ? Number(src.donch_hi) : null);
      var atr = src.atr != null ? Number(src.atr) : (src.atr14 != null ? Number(src.atr14) : null);
      var pms = s.params || {};
      var donchN = Number(pms.donch_n != null ? pms.donch_n : (src.donch_n || 20));
      var atrMode = String(pms.atr_mode || "wilder").toLowerCase();
      var notion = Number(s.notional_usdt != null ? s.notional_usdt : src.quote_usdt);
      if (!(notion > 0)) notion = 0;

      var distPct = null, distAtr = null;
      if (trigger != null && mark != null && mark > 0) {
        var distAbs = trigger - mark;
        distPct = (distAbs / mark) * 100;
        if (atr != null && atr > 0) distAtr = distAbs / atr;
      }

      out.push({
        slot: s.slot,
        strategy_id: s.strategy_id,
        symbol: sym,
        asset: sym.replace(/USDT$/i, ""),
        tf: s.timeframe || s.tf || src.tf,
        order_mode: om,
        status_label: modeLabel(om),
        target_notional_usdt: notion,
        mark: mark,
        trigger: trigger,
        atr: atr,
        atr_mode: atrMode,
        dist_pct: distPct,
        dist_atr: distAtr,
        donch_n: donchN,
        entry_rule: src.entry_condition || src.entry_rule || ("Donchian" + donchN + " 突破上軌"),
        _needs_market: (mark == null || trigger == null || atr == null)
      });
    });
    return out;
  }

  function renderPlanned() {
    var planned = plannedList();
    var body;
    if (!planned.length) {
      body = '<tr><td colspan="7" class="empty-row">目前沒有預計持倉</td></tr>';
    } else {
      body = planned.map(function (pl) {
        var distTxt = "—";
        if (pl.dist_pct != null) {
          distTxt = num(pl.dist_pct, 2) + "%";
          if (pl.dist_atr != null) distTxt += " · " + num(pl.dist_atr, 2) + " ATR";
        }
        var badge = pl.order_mode === "live" ? "ok" : "muted";
        return '<tr data-slot="' + esc(pl.slot) + '">' +
          "<td><strong>" + esc(pl.asset) + '</strong><div class="kpi-sub">' +
          esc(pl.slot) + " · " + esc(pl.tf || "—") + " / Donch " + esc(String(pl.donch_n)) + "</div></td>" +
          '<td class="num">' + num(pl.target_notional_usdt, 0) + "</td>" +
          '<td><span class="badge ' + badge + '">' + esc(pl.status_label) + "</span></td>" +
          '<td class="num">' + (pl.trigger != null ? num(pl.trigger, 4) : "—") + "</td>" +
          '<td class="num">' + (pl.mark != null ? num(pl.mark, 4) : "—") + "</td>" +
          '<td class="num">' + distTxt + "</td>" +
          "<td>" + esc(pl.entry_rule || "—") + "</td></tr>";
      }).join("");
    }
    return '<section class="section" id="sec-planned">' +
      '<div class="section-head"><h2>預計持倉</h2><span class="hint">已核准家族 · live 槽尚未成交 · 狀態只依 live_slots.order_mode</span></div>' +
      '<div class="card"><div class="table-scroll"><table class="data" id="plannedTable">' +
      "<thead><tr><th>標的</th><th class=\"num\">計畫名義</th><th>狀態</th>" +
      "<th class=\"num\">上軌價</th><th class=\"num\">現價</th><th class=\"num\">距突破</th><th>進場條件</th></tr></thead>" +
      "<tbody>" + body + "</tbody></table></div></div></section>";
  }


  function scoreFor(sid) {
    if (!sid) return null;
    var r = scoresById[sid];
    if (!r) return null;
    var sc = r.score;
    // Formula sets failed rows to 0 — show dash, not a fake 0.00
    if (sc == null || sc === "") return null;
    if (Number(sc) === 0 && r.gate_pass === false) return null;
    if (Number(sc) === 0 && r.gate_pass_both === false && r.gate_pass !== true) return null;
    return sc;
  }

  function slotSignalFromCloud(slotId, strategyId) {
    var planned = (cloud && (cloud.planned_positions || cloud.planned)) || [];
    for (var i = 0; i < planned.length; i++) {
      var pl = planned[i];
      if (pl.slot === slotId || pl.strategy_id === strategyId || pl.id === slotId) {
        return pl.status_label || pl.label_zh || pl.status_zh || pl.status || pl.reason || "—";
      }
    }
    var armed = (cloud && (cloud.armed_slots || cloud.slots || cloud.slot_status)) || [];
    for (var j = 0; j < armed.length; j++) {
      var a = armed[j];
      if (a.slot === slotId || a.strategy_id === strategyId) {
        return a.status_zh || a.label_zh || a.status || a.reason || "—";
      }
    }
    return "—";
  }

  function approvalLabelFor(row) {
    var sid = row.strategy_id;
    if (row.default_status === "cash") return "現金";
    var approvedList = (cloud && (cloud.approved_list || cloud.approved)) || [];
    if (!Array.isArray(approvedList) && approvedList && typeof approvedList === "object") {
      approvedList = Object.keys(approvedList).map(function (k) {
        return Object.assign({ strategy_id: k }, approvedList[k]);
      });
    }
    var signalOnly = (cloud && (cloud.signal_only_list || cloud.signal_only)) || [];
    if (!Array.isArray(signalOnly) && signalOnly && typeof signalOnly === "object") {
      signalOnly = Object.keys(signalOnly).map(function (k) {
        return Object.assign({ strategy_id: k }, signalOnly[k]);
      });
    }
    var ap = approvedList.find(function (x) { return x.strategy_id === sid; });
    if (ap && ap.mode !== "signal_only" && ap.approved !== false) return "已核准";
    var so = signalOnly.find(function (x) { return x.strategy_id === sid; });
    if (so || (ap && ap.mode === "signal_only")) return "只算訊號（未核准）";
    if (row.default_status === "pending_review") return "待審核";
    if (row.default_status === "approved_live") return "已核准";
    return "—";
  }

  function renderAllocation() {
    // Removed per Emily: do not present planned notional as「目前配置」.
    return "";
  }

  function renderStrategies() {
    var approved = approvedLiveSlots();
    var armed = (cloud && (cloud.armed_slots || cloud.slots)) || [];
    function enrich(a) {
      var slot = null;
      for (var i = 0; i < armed.length; i++) {
        if (armed[i].strategy_id === a.strategy_id || armed[i].slot === a.slot) {
          slot = armed[i];
          break;
        }
      }
      var src = slot || {};
      var title = String(src.symbol || a.symbol || a.slot || "").replace(/USDT$/i, "") || (a.strategy_id || "—");
      var stZh = src.status_zh || a.label_zh || "已核准 · 上線待命";
      var tf = src.tf || "";
      var notion = a.notional_usdt || src.quote_usdt || "";
      return '<article class="strategy-card active">' +
        '<div class="sc-top"><strong class="sc-title" title="' + esc(a.strategy_id || "") + '">' +
        esc(title) + (tf ? " · " + esc(tf) : "") + "</strong>" +
        '<span class="badge ok">' + esc(stZh) + "</span></div>" +
        '<p class="sc-sum">' + esc(a.strategy_id || "") + "</p>" +
        '<div class="sc-meta">進場計畫見「預計持倉」</div>' +
        '<div class="sc-rules">' +
        '<div><span class="lbl">進場</span> ' + esc(src.entry_condition || "—") + "</div>" +
        '<div><span class="lbl">觸發</span> ' + (src.trigger != null ? num(src.trigger, 4) : "—") +
        " · 現價 " + (src.mark != null ? num(src.mark, 4) : "—") +
        (src.trigger != null && src.mark != null ? " · 距 " + num(Number(src.trigger) - Number(src.mark), 4) : "") +
        "</div>" +
        '<div><span class="lbl">狀態</span> ' + esc(stZh) + "</div>" +
        "</div></article>";
    }
    var cards = approved.length
      ? approved.map(enrich).join("")
      : '<div class="empty-state">尚無已核准策略</div>';
    return '<section class="section" id="sec-strategies">' +
      '<div class="section-head"><h2>策略列表</h2><span class="hint">已上線（live） ' + approved.length + " · 其餘見策略評分</span></div>" +
      '<div class="strategy-grid">' + cards + "</div></section>";
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
    if (!data.length) data = [{ label: "現金", value: 1 }];
    var sum = data.reduce(function (s, p) { return s + p.value; }, 0) || 1;
    if (canvas._chart) canvas._chart.destroy();
    var colors = ["#3b82f6", "#22d3ee", "#22c55e", "#f59e0b", "#a78bfa", "#94a3b8"];
    canvas._chart = new Chart(canvas, {
      type: "doughnut",
      data: {
        labels: data.map(function (d) {
          var pct = (d.value / sum) * 100;
          return d.label + "  " + pct.toFixed(1) + "%  ·  " + Number(d.value).toFixed(2) + " USDT";
        }),
        datasets: [{
          data: data.map(function (d) { return d.value; }),
          backgroundColor: data.map(function (_d, i) { return colors[i % colors.length]; }),
          borderWidth: 0
        }]
      },
      options: {
        plugins: {
          legend: {
            display: true,
            position: "right",
            labels: { color: "#8b9bb0", boxWidth: 12, font: { size: 11 } }
          }
        },
        cutout: "58%",
        onHover: function (_evt, els) {
          var rows = document.querySelectorAll("#actualTable tr.pos-row");
          rows.forEach(function (r) { r.classList.remove("pie-hover"); });
          if (!els || !els.length) return;
          var lab = data[els[0].index] && data[els[0].index].label;
          if (!lab || lab === "現金") return;
          rows.forEach(function (r) {
            if (r.getAttribute("data-asset") === lab) r.classList.add("pie-hover");
          });
        }
      }
    });
  }

  function openPinModal(opts) {
    // Session login replaces per-action PIN prompts
    if (window.TraderAuth && window.TraderAuth.getToken && window.TraderAuth.getToken()) {
      var confirmed = true;
      if (opts.confirmText) confirmed = window.confirm(opts.confirmText);
      if (!confirmed) return;
      var fakeResult = { textContent: "" };
      // Prefer a visible result target if modal exists
      var modalEarly = $("pinModal");
      if (modalEarly) {
        modalEarly.classList.remove("hidden");
        modalEarly.setAttribute("aria-hidden", "false");
        var bodyEarly = $("pinModalBody");
        if (bodyEarly) {
          bodyEarly.innerHTML = "<h3>" + esc(opts.title || "確認") + "</h3>" +
            (opts.confirmText ? '<p class="modal-confirm">' + esc(opts.confirmText) + "</p>" : "") +
            (opts.extraHtml || "") +
            '<div class="modal-actions">' +
            '<button type="button" class="primary" id="pinSubmit">確認</button>' +
            '<button type="button" id="pinCancel">取消</button></div>' +
            '<p class="modal-result" id="pinResult"></p>';
          $("pinSubmit").onclick = function () {
            opts.onSubmit("", $("pinResult"));
          };
          $("pinCancel").onclick = closeModal;
          return;
        }
      }
      opts.onSubmit("", fakeResult);
      return;
    }

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
      var pin = (window.TraderAuth && window.TraderAuth.normalizePin)
        ? window.TraderAuth.normalizePin(($("pinInput") && $("pinInput").value) || "")
        : String(($("pinInput") && $("pinInput").value) || "").trim();
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
    var tok = (window.TraderAuth && window.TraderAuth.getToken && window.TraderAuth.getToken()) || "";
    var headers = { "Content-Type": "application/json" };
    if (tok) headers["Authorization"] = "Bearer " + tok;
    else if (pin) headers["X-Trader-Pin"] = (window.TraderAuth && window.TraderAuth.normalizePin)
      ? window.TraderAuth.normalizePin(pin) : String(pin || "").trim();
    var res = await fetch(apiBase + path, {
      method: "POST",
      headers: headers,
      body: body ? JSON.stringify(body) : undefined
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
    var btnCloseAll = $("btnCloseAll");
    if (btnCloseAll) {
      btnCloseAll.onclick = function () {
        var openN = openPositions().filter(function (p) {
          return !p.status || p.status === "FILLED" || p.status === "OPEN";
        }).length;
        var confirmText = openN === 0
          ? "目前沒有持倉，將只暫停。確定繼續？"
          : ("⚠ 將先暫停，再以市價賣出全部 " + openN + " 個持倉（取消止損後市價賣出）。此操作不可撤銷。確定？");
        openPinModal({
          title: "全部平倉並暫停",
          confirmText: confirmText,
          onSubmit: function (pin, resultEl) {
            resultEl.textContent = "處理中…";
            postControl("/control/close_all", pin)
              .then(function (r) {
                var lines = [r.message || "完成"];
                (r.results || []).forEach(function (row) {
                  if (row.ok) {
                    var c = row.closed || {};
                    lines.push("✓ " + (row.symbol || row.slot) +
                      (c.pnl_usdt != null ? ("  PnL " + Number(c.pnl_usdt).toFixed(2)) : ""));
                  } else {
                    lines.push("✗ " + (row.symbol || row.slot) + "：" + (row.error || "失敗"));
                  }
                });
                if (!(r.results || []).length) {
                  lines.push("（無持倉，僅暫停）");
                }
                resultEl.textContent = lines.join("\n");
                var box = $("closeAllResult");
                if (box) {
                  box.classList.remove("hidden");
                  box.textContent = lines.join("\n");
                }
                setTimeout(function () { closeModal(); loadAll(); }, 1200);
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

  async function enrichPlannedMarkets(list) {
    var need = (list || []).filter(function (pl) { return pl._needs_market; });
    if (!need.length) return list;
    await Promise.all(need.map(async function (pl) {
      try {
        var interval = String(pl.tf || "1h").toLowerCase();
        var limit = Math.max(80, Number(pl.donch_n || 20) + 25);
        var url = "https://data-api.binance.vision/api/v3/klines?symbol=" +
          encodeURIComponent(pl.symbol) + "&interval=" + encodeURIComponent(interval) +
          "&limit=" + limit;
        var res = await fetch(url, { cache: "no-store" });
        if (!res.ok) return;
        var kl = await res.json();
        if (!Array.isArray(kl) || kl.length < 20) return;
        var bars = kl.slice(0, -1);
        var n = Number(pl.donch_n || 20);
        var i, hi = -Infinity, trs = [];
        for (i = Math.max(0, bars.length - n); i < bars.length; i++) {
          var h = Number(bars[i][2]);
          if (h > hi) hi = h;
        }
        for (i = 1; i < bars.length; i++) {
          var hh = Number(bars[i][2]), ll = Number(bars[i][3]), cprev = Number(bars[i - 1][4]);
          trs.push(Math.max(hh - ll, Math.abs(hh - cprev), Math.abs(ll - cprev)));
        }
        var atrN = 14, atr = null, j;
        if (trs.length >= atrN) {
          if (pl.atr_mode === "sma") {
            var sum = 0;
            for (j = trs.length - atrN; j < trs.length; j++) sum += trs[j];
            atr = sum / atrN;
          } else {
            var seed = 0;
            for (j = 0; j < atrN; j++) seed += trs[j];
            atr = seed / atrN;
            for (j = atrN; j < trs.length; j++) atr = (atr * (atrN - 1) + trs[j]) / atrN;
          }
        }
        var last = bars[bars.length - 1];
        pl.mark = Number(last[4]);
        pl.trigger = hi;
        pl.atr = atr;
        if (pl.trigger != null && pl.mark > 0) {
          var d = pl.trigger - pl.mark;
          pl.dist_pct = (d / pl.mark) * 100;
          if (pl.atr > 0) pl.dist_atr = d / pl.atr;
        }
        pl._needs_market = false;
      } catch (e) { /* ignore */ }
    }));
    return list;
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
    pieParts = actualPieParts();
    main.innerHTML = renderHealth() + renderAccount() +
      renderActual() + renderPlanned() + renderStrategies() + renderTrades() + renderChanges();
    mountPie(pieParts);
    bindControls();
    var pending = plannedList();
    enrichPlannedMarkets(pending).then(function () {
      if (!cloud) return;
      cloud._market_cache = cloud._market_cache || {};
      pending.forEach(function (pl) {
        cloud._market_cache[pl.slot] = {
          mark: pl.mark, trigger: pl.trigger, atr: pl.atr, donch_hi: pl.trigger
        };
      });
      var el = document.getElementById("sec-planned");
      if (el) {
        var tmp = document.createElement("div");
        tmp.innerHTML = renderPlanned();
        if (tmp.firstChild) el.replaceWith(tmp.firstChild);
      }
    });
  }

  async function loadCloud() {
    apiBase = await resolveApiBase();
    if (!apiBase) {
      cloudOk = false;
      cloudErr = "未設定 API";
      return null;
    }
    try {
      cloud = await fetchJSONRetry(
        apiBase + "/status?t=" + Date.now(),
        { cache: "no-store", mode: "cors", headers: { Accept: "application/json" } },
        8000,
        1
      );
      cloudOk = !!(cloud && cloud.ok !== false);
      cloudErr = "";
      return cloud;
    } catch (e) {
      cloudOk = false;
      cloudErr = (e && e.name === "AbortError") ? "逾時" : ((e && e.message) || String(e));
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
      // ALWAYS_LOAD_SCORES
      allocCfg = await getJSONOpt(ALLOC_URL);
      try {
        var sc = await getJSONOpt(SCORES_URL);
        scoresById = {};
        ((sc && sc.strategies) || []).forEach(function (r) { scoresById[r.strategy_id] = r; });
      } catch (eSc) { scoresById = {}; }
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
