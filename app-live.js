/* Homepage live view — prefers Cloud Run GET /status; static fallback. Mirror to live.js */
(function () {
  "use strict";

  const BOOK_URL = "./data/live_book.json";

  // book_allocation.json removed 2026-10-08 (stale OP/DOT 1000); books come from cloud /status.
  const SCORES_URL = "./data/unified-3y/scores.json";
  let allocCfg = null;
  let scoresById = {};

  const HEALTH_URL = "./state/health.json";
  const POSITIONS_URL = "./state/positions.json";
  const PAPER_URL = "./state/paper_trading.json";
  const SETTLEMENT_URL = "./data/strategy-crypto-s2/settlement.json";
  const SETTLEMENT_JSONL = "./state/settlement.jsonl";
  const CLOUD_CFG = "./data/cloud_api.json";
  const CODES_URL = "./config/strategy_codes.json";

  let book = null;
  let health = null;
  let positions = null;
  let paper = null;
  let settlements = [];
  let cloud = null;
  let cloudOk = false;
  let cloudErr = "";
  let strategyCodes = null;
  let apiBase = "";
  let loading = false;
  let autoOn = true;
  let refreshSec = 30;
  let countdown = 30;
  let timerId = null;
  let pieParts = [];
  var expandedStrategyKeys = {};

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
    waiting_short_entry: "等做空進場邊緣（死叉／濾網轉真當根）",
    stale_signal_before_exit: "訊號早於上次出場（不追）",
    waiting_bb_squeeze_breakout: "等布林壓縮後突破上軌（狀態 0→1 當根）",
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

  function formatPnl(pnl, pct, quote) {
    if (pnl == null || Number.isNaN(Number(pnl))) return "—";
    var q = quote || "USDT";
    var out = signedNum(pnl, 2) + " " + q;
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

  function codeForTrade(t) {
    if (t && t.code) return t.code;
    var sid = t && (t.strategy_id || t.variant_id);
    var fams = (strategyCodes && strategyCodes.families) || {};
    for (var fid in fams) {
      var f = fams[fid] || {};
      var rows = f.rows || {};
      if (sid && rows[sid] && rows[sid].code) return rows[sid].code;
    }
    // No symbol-token guessing: manual/untracked fills (e.g. NEAR 9/18–9/24 spot)
    // must not inherit a strategy code (A6 is a perp strategy).
    var sym = "";
    if (sym) {
      var token = "__" + sym + "__";
      for (var fid2 in fams) {
        var rows2 = (fams[fid2] || {}).rows || {};
        for (var rid in rows2) {
          if (rid.toUpperCase().indexOf(token) >= 0 && rows2[rid].code) return rows2[rid].code;
        }
      }
    }
    return "";
  }

  // Actual Binance commissions (asset + amount) read back after each fill.
  function commissionsText(t) {
    var parts = [];
    [["進", t.entry_commissions || (t.entry_fill && t.entry_fill.commissions)],
     ["出", t.exit_commissions || (t.exit_fill && t.exit_fill.commissions)]].forEach(function (x) {
      (x[1] || []).forEach(function (c) { parts.push(x[0] + " " + c.amount + " " + c.asset); });
    });
    return parts.join("；");
  }

  async function loadSettlements() {
    if (cloud && Array.isArray(cloud.closed_trades) && cloud.closed_trades.length) {
      return cloud.closed_trades.map(function (t) {
        return {
          ts: t.closed_at || t.time,
          kind: "CLOSE",
          symbol: t.symbol,
          code: codeForTrade(t),
          side: "SELL",
          qty: t.qty,
          price: t.exit,
          realized_pnl_usdt: t.pnl_usdt,
          fee: t.fee_usdt,
          commissions_text: commissionsText(t),
          fill_source: (t.exit_fill && t.exit_fill.complete) ? "binance" : "",
          reason: t.reason || "manual"
        };
      });
    }
    try {
      var arr = await getJSON(SETTLEMENT_URL);
      if (Array.isArray(arr)) return arr.map(function (t) {
        return Object.assign({}, t, { code: codeForTrade(t) });
      });
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

  // Data freshness: never present cached/static data as live (資金控管 2026-10-08).
  var STALE_MAX_MIN = 120; // runner is hourly; >2h old cloud data counts as stale
  function dataTimestamp() {
    var cands = [];
    if (cloudOk && cloud) cands = [cloud.last_job_run_at || cloud.updated_at]; // runner data time, not API response time
    else cands = [
      book && (book.updated_at || book.last_updated),
      positions && positions.updated_at,
      paper && (paper.updated_at_taipei || paper.updated_at),
      health && (health.updated_at || health.checked_at)
    ];
    var best = null;
    cands.forEach(function (v) {
      if (!v) return;
      var s = String(v);
      if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(s)) s += "+08:00"; // naive = TPT
      var t = Date.parse(s);
      if (!isNaN(t) && (best == null || t > best)) best = t;
    });
    return best;
  }
  function fmtTpt(ms) {
    try { return new Date(ms).toLocaleString("zh-TW", { timeZone: "Asia/Taipei" }) + " TPT"; }
    catch (e) { return new Date(ms).toISOString(); }
  }
  function freshnessHtml() {
    var ts = dataTimestamp();
    var ageMin = ts != null ? (Date.now() - ts) / 60000 : null;
    var stale = !cloudOk || ageMin == null || ageMin > STALE_MAX_MIN;
    var age = ageMin == null ? "時間未知"
      : ageMin < 90 ? Math.round(ageMin) + " 分鐘前"
      : ageMin < 48 * 60 ? (ageMin / 60).toFixed(1) + " 小時前"
      : Math.floor(ageMin / 1440) + " 天前";
    var src = cloudOk ? "雲端" : "快取／靜態（非即時）";
    var el = $("staleBanner");
    if (el) {
      if (stale) {
        el.classList.remove("hidden");
        el.innerHTML = "⚠ 非即時資料：" + esc(src) + " · 資料時間 " + esc(ts != null ? fmtTpt(ts) : "未知") +
          "（" + esc(age) + "）。帳戶、模式、成交可能與實際不同，請登入／重整取得雲端即時狀態。";
      } else el.classList.add("hidden");
    }
    document.body.classList.toggle("data-stale", stale);
    return "資料時間：<strong>" + esc(ts != null ? fmtTpt(ts) : "未知") + "</strong>（" + esc(age) + "）" +
      " · " + esc(src) + (stale ? ' <span class="badge bad">過期</span>' : "") +
      ' <span class="dim">· 頁面重整 ' + esc(nowLabel()) + "</span>";
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
      (function () {
        var ps = Object.keys((cloud && cloud.paused_slots) || {});
        return ps.length ? '<span class="health-meta">單槽暫停（不開新倉）：' + esc(ps.join("、")) + '</span>' : "";
      })() +
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
      var asset = String(p.asset || sym.replace(/USDT$/i, "").replace(/USDC$/i, "")).toUpperCase();
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
        code: p.code || "",
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

  function quoteOfSymbol(sym) {
    var s = String(sym || "").toUpperCase();
    if (s.indexOf("USDC") >= 0 && s.indexOf("USDT") < 0) return "USDC";
    if (/USDC$/.test(s)) return "USDC";
    return "USDT";
  }

  function bookStarting(quote) {
    var books = (cloud && cloud.books) || {};
    var b = books[String(quote).toLowerCase()] || {};
    if (b.starting != null) return Number(b.starting);
    var a = (cloud && cloud.allocation) || allocCfg || {};
    if (quote === "USDC") return Number(a.book_usdc != null ? a.book_usdc : 5000);
    return Number(a.book_usdt != null ? a.book_usdt : bookUsdt() || 5000);
  }

  /** Holdings for one quote book (USDT or USDC). */
  function holdingsForQuote(quote) {
    return actualHoldings().filter(function (h) {
      return quoteOfSymbol(h.symbol || h.asset) === quote;
    });
  }

  /** Pie parts for one quote: position MVs + that quote's cash. Never mixes books. */
  function piePartsForQuote(quote) {
    var dust = DUST_USDT;
    var holds = holdingsForQuote(quote);
    var parts = holds.map(function (h) {
      return { label: h.asset, value: Number(h.market_value) || 0, symbol: h.symbol, kind: "pos" };
    });
    var cash = Number((bals()[quote] != null ? bals()[quote] : 0)) || 0;
    if (!holds.length) {
      parts = [{ label: "現金", value: Math.max(cash, 0.01), symbol: quote, kind: "cash" }];
    } else if (cash >= dust) {
      parts.push({ label: "現金", value: cash, symbol: quote, kind: "cash" });
    }
    return parts.filter(function (p) { return p.value >= dust || p.kind === "cash"; });
  }

  function actualPieParts() {
    return piePartsForQuote("USDT");
  }

  function bookStats(quote) {
    var books = (cloud && cloud.books) || {};
    var remote = books[String(quote).toLowerCase()] || null;
    var holds = holdingsForQuote(quote);
    var cash = Number((bals()[quote] != null ? bals()[quote] : 0)) || 0;
    var posMv = 0, unreal = 0, cost = 0;
    holds.forEach(function (h) {
      posMv += Number(h.market_value) || 0;
      if (h.unrealized != null) unreal += Number(h.unrealized);
      if (h.entry_cost > 0) cost += Number(h.entry_cost);
    });
    var realized = 0;
    var closed = (cloud && cloud.closed_trades) || [];
    closed.forEach(function (t) {
      if (quoteOfSymbol(t.symbol) !== quote) return;
      if (t.pnl_usdt != null) realized += Number(t.pnl_usdt);
    });
    var starting = bookStarting(quote);
    var equity = remote && remote.equity != null ? Number(remote.equity) : (cash + posMv);
    if (remote) {
      if (remote.realized_pnl != null) realized = Number(remote.realized_pnl);
      if (remote.unrealized_pnl != null) unreal = Number(remote.unrealized_pnl);
      if (remote.cash != null) cash = Number(remote.cash);
      if (remote.positions_mv != null) posMv = Number(remote.positions_mv);
      if (remote.starting != null) starting = Number(remote.starting);
    }
    var feesUsdt = remote && remote.fees_usdt != null ? Number(remote.fees_usdt) : null;
    var unattributed = remote && remote.unattributed_usdt != null ? Number(remote.unattributed_usdt) : null;
    var feeMissing = remote && remote.trades_fee_missing != null ? Number(remote.trades_fee_missing) : 0;
    return {
      quote: quote,
      starting: starting,
      cash: cash,
      positions_mv: posMv,
      equity: equity,
      realized: realized,
      unrealized: unreal,
      unrealized_pct: cost > 0 ? (unreal / cost) * 100 : null,
      open_n: holds.length,
      used: posMv,
      remaining: Math.max(0, starting - posMv),
      fees: feesUsdt,
      unattributed: unattributed,
      fee_missing: feeMissing
    };
  }

  function unrealizedSummary() {
    var u = bookStats("USDT");
    return { pnl: u.unrealized, cost: null, pct: u.unrealized_pct };
  }

  /** USD-M futures wallet for this quote, from the cloud API (Binance-reconciled). Empty when absent. */
  function futuresKpis(q) {
    var b = ((cloud && cloud.books) || {})[String(q).toLowerCase()] || {};
    var f = b.futures;
    if (!f || f.error) return f && f.error ? '<div class="kpi"><div class="label">合約錢包</div><div class="value">—</div><div class="sublabel">讀取失敗</div></div>' : "";
    var rec = f.reconcile || {};
    var recTxt = rec.ok ? "與 Binance 一致" : ("不一致 " + ((rec.mismatches || []).map(function (m) { return m.symbol; }).join(", ")));
    var sl = f.u1_sleeve;
    var pos = (f.positions || []).map(function (p) { return esc(p.symbol) + " " + num(p.qty, 4); }).join(" · ") || "無持倉";
    return '<div class="kpi"><div class="label">合約錢包（USD-M ' + q + '）</div><div class="value">' + num(f.margin_balance, 2) +
      '</div><div class="sublabel">錢包 ' + num(f.wallet, 2) + ' · 未實現 ' + signedNum(f.unrealized_pnl || 0, 2) + ' · 可用 ' + num(f.available, 2) + '</div></div>' +
      '<div class="kpi"><div class="label">合約配置 / 持倉</div><div class="value">' + num(f.allocated || 0, 0) +
      '</div><div class="sublabel">' + pos + (sl && sl.equity != null ? ' · U1 權益 ' + num(sl.equity, 2) : "") + ' · ' + esc(recTxt) + '</div></div>';
  }

  function renderBookCard(st) {
    var q = st.quote;
    var upnl = '<span class="' + signedCls(st.unrealized) + '">' +
      (st.unrealized == null ? "—" : formatPnl(st.unrealized, st.unrealized_pct, q)) + "</span>";
    var rpnl = '<span class="' + signedCls(st.realized) + '">' +
      (st.realized == null ? "—" : signedNum(st.realized, 2) + " " + q) + "</span>";
    var pieId = "allocPie" + q;
    var legendId = "pieLegend" + q;
    var parts = piePartsForQuote(q);
    var sum = parts.reduce(function (s, p) { return s + p.value; }, 0) || 1;
    var legend = parts.map(function (p) {
      var pct = (p.value / sum) * 100;
      return '<div class="pie-legend-row" data-pie-label="' + esc(p.label) + '">' +
        "<strong>" + esc(p.label) + "</strong> " +
        num(pct, 1) + "% · " + num(p.value, 2) + " " + q + "</div>";
    }).join("");
    return '<div class="book-card" data-quote="' + q + '">' +
      '<div class="section-head"><h3>' + q + ' 帳本</h3>' +
      '<span class="hint">與另一帳本完全分開 · 上限 ' + num(st.starting, 0) + '</span></div>' +
      '<div class="kpi-grid kpi-grid-book">' +
      '<div class="kpi"><div class="label">原本餘額</div><div class="value">' + num(st.starting, 2) + '</div><div class="sublabel">起始 ' + q + '</div></div>' +
      '<div class="kpi kpi-emphasis"><div class="label">現在餘額</div><div class="value">' + num(st.equity, 2) + '</div><div class="sublabel">現金 + 持倉市值</div></div>' +
      '<div class="kpi"><div class="label">已實現損益</div><div class="value">' + rpnl + '</div><div class="sublabel">closed_trades 合計' + feeLabel(st) + unattributedBreakdown(st, q) +
        '</div></div>' +
      '<div class="kpi"><div class="label">未實現損益</div><div class="value">' + upnl + '</div><div class="sublabel">持倉市價</div></div>' +
      '<div class="kpi"><div class="label">現金</div><div class="value">' + num(st.cash, 2) + '</div><div class="sublabel">可用 + 鎖定</div></div>' +
      '<div class="kpi"><div class="label">已用 / 剩餘</div><div class="value">' + num(st.used, 2) + ' / ' + num(st.remaining, 2) + '</div><div class="sublabel">持倉市值 vs 原本</div></div>' +
      futuresKpis(q) +
      '</div>' +
      '<div class="holdings-pie-panel book-pie">' +
      '<div class="section-head"><h4>' + q + ' 分布</h4>' +
      '<span class="hint">僅此帳本 · 現金 + 持倉</span></div>' +
      '<div class="pie-panel-body">' +
      '<div class="alloc-pie-wrap"><canvas id="' + pieId + '" width="180" height="180"></canvas></div>' +
      '<div class="pie-legend" id="' + legendId + '">' + legend + "</div></div></div></div>";
  }

  /** Whole account from Binance: spot balances (all assets, ticker-priced) + USD-M USDT/USDC futures margin balance. */
  function renderTotalAssets() {
    var t = cloud && cloud.total_assets;
    if (!t || t.error || t.total_usdt == null) return "";
    var f = t.futures || {};
    return '<div class="kpi-grid"><div class="kpi kpi-emphasis"><div class="label">帳戶總資產（幣安）</div><div class="value">' +
      num(t.total_usdt, 2) + ' USDT</div><div class="sublabel">現貨 ' + num(t.spot_usdt, 2) +
      ' + 合約 USDT ' + (f.USDT == null ? "—" : num(f.USDT, 2)) + ' + 合約 USDC ' + (f.USDC == null ? "—" : num(f.USDC, 2)) +
      (t.complete ? "" : ' · <span class="neg">部分讀取失敗' + (t.unpriced && t.unpriced.length ? "（未計價 " + esc(t.unpriced.join(",")) + "）" : "") + "</span>") +
      "</div></div></div>";
  }

  /** Resting orders from Binance (spot + USD-M futures, incl. algo/stop orders). */
  function renderOpenOrders() {
    var oo = cloud && cloud.open_orders;
    if (!oo) return "";
    var rows = [].concat(oo.spot || [], oo.futures || [], oo.futures_algo || []).map(function (o) {
      var venue = o.venue === "spot" ? "現貨" : (o.venue === "futures_algo" ? "合約條件單" : "合約") + (o.quote ? " " + o.quote : "");
      var px = o.stopPrice || o.triggerPrice || o.price;
      return "<tr><td>" + esc(venue) + "</td><td><strong>" + esc(o.symbol || "") + "</strong></td><td>" + esc(o.side || "") +
        "</td><td>" + esc(o.type || o.orderType || "") + (o.reduceOnly ? " · 只減倉" : "") + '</td><td class="num">' + num(o.origQty || o.quantity, 4) +
        '</td><td class="num">' + (px == null ? "—" : num(px, 4)) + "</td><td>" + esc(o.status || o.algoStatus || "") +
        "</td><td>" + esc(String(o.orderId || o.algoId || "")) + "</td></tr>";
    }).join("");
    var err = (oo.errors || []).length ? '<p class="neg">讀取失敗：' + esc(oo.errors.join("；")) + "</p>" : "";
    return '<div class="ops-panel" id="openOrdersPanel"><div class="ops-head"><strong>幣安掛單（現貨 + USDT/USDC 合約）</strong>' +
      '<span class="hint">' + (oo.count || 0) + " 筆 · 直接讀幣安</span></div>" + err +
      (rows ? '<div class="table-scroll"><table class="data"><thead><tr><th>市場</th><th>標的</th><th>方向</th><th>類型</th><th class="num">數量</th><th class="num">價格／觸發價</th><th>狀態</th><th>訂單號</th></tr></thead><tbody>' + rows + "</tbody></table></div>"
        : '<p class="ops-help">目前沒有掛單</p>') + "</div>";
  }

  function renderReconcile() {
    var r = cloud && cloud.reconcile;
    if (!r || !r.at) return '<div class="ops-panel"><strong>對帳狀態</strong> <span class="dim">尚未執行</span></div>';
    var mism = (r.mismatches || []).map(function (m) {
      return "<li>" + esc((m.venue || "") + " " + (m.symbol || "") + "：本地 " + m.trader + " / 幣安 " + m.binance +
        " · " + (m.kind || "") + ((m.slots || []).length ? " · 槽 " + m.slots.join(",") + (m.pause ? "（已暫停開新倉）" : "") : "")) + "</li>";
    }).join("");
    var errs = (r.errors || []).map(function (e) { return "<li>" + esc(e) + "</li>"; }).join("");
    var alerts = ((cloud && cloud.reconcile_alerts) || []).slice(-5).reverse().map(function (a) {
      return "<li>" + esc((a.at || "") + " " + (a.venue || "") + " " + (a.symbol || "") + " 本地 " + a.trader + " / 幣安 " + a.binance + " " + (a.kind || "")) + "</li>";
    }).join("");
    return '<div class="ops-panel" id="reconcilePanel"><div class="ops-head"><strong>對帳狀態（每 15 分鐘與幣安比對）</strong>' +
      '<span class="' + (r.ok ? "pos" : "neg") + '">' + (r.ok ? "一致" : "不一致") + "</span></div>" +
      '<p class="ops-help">最後對帳：' + esc(r.at) + " · 連續通過 " + esc(String(r.consecutive_ok || 0)) + " 次</p>" +
      (mism ? "<ul>" + mism + "</ul>" : "") + (errs ? '<ul class="neg">' + errs + "</ul>" : "") +
      (alerts ? '<details><summary>告警紀錄</summary><ul>' + alerts + "</ul></details>" : "") + "</div>";
  }

  function renderAccount() {
    var usdt = bookStats("USDT");
    var usdc = bookStats("USDC");
    pieParts = piePartsForQuote("USDT");
    return '<section class="section" id="sec-account">' +
      '<div class="section-head"><h2>帳戶總覽</h2><span class="hint">' +
      esc((cloud && cloud.source_label) || (book && book.source_label) || "Binance Demo") +
      " · USDT / USDC 分開記帳</span></div>" +
      renderTotalAssets() +
      '<div class="books-row">' + renderBookCard(usdt) + renderBookCard(usdc) + "</div>" +
      renderReconcile() +
      renderOpenOrders() +
      '<p class="book-math-hint">驗算：現在餘額 ≈ 原本 + 已實現 + 未實現（若有入金／出金或手續費幣種差異，數字可能不完全對上）。</p>' +
      "</section>";
  }

  function tradeRow(r) {
    var ts = r.ts || r.time || r.closed_at || "—";
    var kind = r.kind || (r.side === "BUY" ? "OPEN" : "CLOSE");
    var sym = r.symbol || "—";
    var code = r.code || "";
    var displaySym = code ? String(code) + " " + String(sym) : String(sym);
    var side = r.side || "—";
    var qty = r.qty;
    var px = r.price != null ? r.price : r.avg_price != null ? r.avg_price : r.exit;
    var fee = r.fee != null ? r.fee : r.commission;
    var pnl = r.realized_pnl_usdt != null ? r.realized_pnl_usdt : r.pnl_usdt;
    return "<tr>" +
      "<td>" + esc(String(ts)) + "</td>" +
      "<td>" + esc(String(kind)) + "</td>" +
      "<td><strong>" + esc(displaySym) + "</strong></td>" +
      "<td>" + esc(String(side)) + "</td>" +
      '<td class="num">' + num(qty, 4) + "</td>" +
      '<td class="num">' + num(px, 4) + "</td>" +
      '<td class="num"' + (r.commissions_text ? ' title="' + esc(r.commissions_text) + '"' : "") + '>' +
        (fee == null || fee === "" ? (r.commissions_text ? esc(r.commissions_text) : '<span class="dim">無資料</span>') : num(fee, 4)) +
        (r.commissions_text ? '<div class="dim" style="font-size:11px">' + esc(r.commissions_text) + "</div>" : "") +
        (r.fill_source === "binance" ? '<div class="dim" style="font-size:11px">幣安成交回讀</div>' : "") + "</td>" +
      '<td class="num ' + signedCls(pnl) + '">' + (pnl == null ? "—" : num(pnl, 2)) + "</td>" +
      "<td>" + esc(reason(r.reason || kind)) + "</td></tr>";
  }

  var CHANGES_N = 10;
  function changeRows() {
    // Same source + builder as history.html (cloud /status); static settlements only offline.
    if (cloud && typeof window.buildAccountChanges === "function") {
      var rows = window.buildAccountChanges(cloud);
      if (rows.length) return rows;
    }
    return sortDesc(settlements || []);
  }

  // U1 模擬倉 (paper only): internal simulation from cloud /status.paper.U1 — never orders, not in the books.
  function renderPaperU1() {
    var p = cloud && cloud.paper && cloud.paper.U1;
    if (!p) return "";
    function tpt(iso) {
      if (!iso) return "—";
      try { return new Date(iso).toLocaleString("zh-TW", { timeZone: "Asia/Taipei", hour12: false }) + " TPT"; } catch (e) { return String(iso); }
    }
    function n(v, d) { return (v == null || !isFinite(Number(v))) ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d == null ? 2 : d, minimumFractionDigits: d == null ? 2 : d }); }
    function pct(v) { return (v == null || !isFinite(Number(v))) ? "—" : (Number(v) >= 0 ? "+" : "") + Number(v).toFixed(2) + "%"; }
    var w = p.weights || {}, u = p.units || {}, ld = p.last_decision || {}, pend = p.pending || {};
    var tgt = (pend.targets_lev || ld.targets_lev || {});
    var fills = (p.paper_fills || []).slice(-5).reverse();
    var frows = fills.length ? fills.map(function (f) {
      return "<tr><td>" + esc(tpt(f.fill_time_utc)) + "</td><td>" + esc(f.symbol) + "</td><td>" + esc(f.side) + "</td><td>" + esc(f.reason) +
        '</td><td class="num">' + n(f.qty_delta, 4) + '</td><td class="num">' + n(f.price) + '</td><td class="num">' + n(f.target_weight, 3) + "</td></tr>";
    }).join("") : '<tr><td colspan="7" class="empty-row">模擬期間尚無調倉</td></tr>';
    return '<section class="section" id="sec-paper-u1">' +
      '<div class="section-head"><h2>模擬倉 U1 · <span class="badge muted">模擬（非實盤）</span></h2>' +
      '<span class="hint">BTC/ETH 200 日線趨勢＋波動目標槓桿（上限 3x）· 內部模擬，不下任何單（含 Demo）· 不計入 USDT/USDC 帳本</span></div>' +
      '<div class="card"><div class="table-scroll"><table class="data"><tbody>' +
      '<tr><th>模擬權益</th><td class="num">' + n(p.equity) + ' USDT</td><th>模擬開始以來</th><td class="num">' + pct(p.return_since_paper_start_pct) + "</td></tr>" +
      '<tr><th>BTC 權重／單位</th><td class="num">' + n(w.BTC, 3) + " / " + n(u.BTC, 4) + '</td><th>ETH 權重／單位</th><td class="num">' + n(w.ETH, 3) + " / " + n(u.ETH, 4) + "</td></tr>" +
      '<tr><th>毛曝險</th><td class="num">' + n(p.gross, 2) + 'x</td><th>帳戶槓桿 L（' + esc(ld.indicator_day || "—") + '）</th><td class="num">' + n(ld.L, 3) + "</td></tr>" +
      '<tr><th>目標權重 BTC／ETH</th><td class="num">' + n(tgt.BTC, 3) + " / " + n(tgt.ETH, 3) + '</td><th>下次決策成交</th><td>' + esc(tpt(p.next_fill_utc)) + "</td></tr>" +
      '<tr><th>最後結算 K 線</th><td>' + esc(tpt(p.last_closed_bar_utc)) + '</td><th>更新</th><td>' + esc(tpt(p.updated_at_utc)) + "</td></tr>" +
      "</tbody></table></div>" +
      '<div class="table-scroll"><table class="data"><thead><tr><th>模擬成交</th><th>標的</th><th>方向</th><th>原因</th><th class="num">數量</th><th class="num">價格(1h 開盤)</th><th class="num">目標權重</th></tr></thead><tbody>' +
      frows + "</tbody></table></div></div></section>";
  }

  // Y1 / X1 模擬倉 (paper only): cloud /status.paper.Y1 / .X1 — internal simulation, never orders, not in the books, not approvable.
  function renderPaperY1() {
    var P = cloud && cloud.paper;
    if (!P || (!P.Y1 && !P.X1)) return "";
    function tpt(iso) {
      if (!iso) return "—";
      try { return new Date(iso).toLocaleString("zh-TW", { timeZone: "Asia/Taipei", hour12: false }) + " TPT"; } catch (e) { return String(iso); }
    }
    function n(v, d) { return (v == null || !isFinite(Number(v))) ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d == null ? 2 : d, minimumFractionDigits: d == null ? 2 : d }); }
    function pct(v) { return (v == null || !isFinite(Number(v))) ? "—" : (Number(v) >= 0 ? "+" : "") + Number(v).toFixed(2) + "%"; }
    function fillsTable(list) {
      var f = (list || []).slice(-8).reverse();
      var rows = f.length ? f.map(function (x) {
        return "<tr><td>" + esc(tpt(x.fill_time_utc)) + "</td><td>" + esc(x.symbol) + "</td><td>" + esc(x.side) + "</td><td>" + esc(x.reason) +
          '</td><td class="num">' + n(x.qty_delta, 4) + '</td><td class="num">' + n(x.price, 6) + '</td><td class="num">' + n(x.target_weight, 4) + "</td></tr>";
      }).join("") : '<tr><td colspan="7" class="empty-row">模擬期間尚無調倉</td></tr>';
      return '<div class="table-scroll"><table class="data"><thead><tr><th>模擬成交</th><th>標的</th><th>方向</th><th>原因</th><th class="num">數量</th><th class="num">價格(1h 開盤)</th><th class="num">目標權重</th></tr></thead><tbody>' + rows + "</tbody></table></div>";
    }
    function topPos(p) {
      var ps = (p.positions || []).slice(0, 6);
      return ps.length ? ps.map(function (x) { return esc(String(x.symbol).replace(/USDT$/, "")) + " " + (x.weight >= 0 ? "+" : "") + n(x.weight, 3); }).join("　") : "—";
    }
    function lastRunNote(p) {
      var r = p.last_run || {};
      var t = r.timing || {};
      var s = r.stopped ? "（暫停於：" + esc(r.stopped) + "，下次執行自動補跑）" : "";
      return "最近一次執行 " + n(t.total_s, 1) + " 秒 · fapi 請求 " + esc(String((r.http || {}).requests == null ? "—" : r.http.requests)) +
        " 次 · 最高用量 " + esc(String((r.http || {}).max_used_weight_1m == null ? "—" : r.http.max_used_weight_1m)) + "/2400 每分鐘" + s;
    }
    var out = "";
    var y = P.Y1;
    if (y) {
      var d = y.last_decision || {};
      out += '<section class="section" id="sec-paper-y1">' +
        '<div class="section-head"><h2>模擬倉 Y1 · <span class="badge muted">模擬（非實盤）</span></h2>' +
        '<span class="hint">U1＋V1（X1）等風險組合 · 組合層波動目標 75%、槓桿上限 5x · 全倉 · 無停損 · ' + esc(y.status_zh || "") + ' · 內部模擬，不下任何單（含 Demo）· 不計入 USDT/USDC 帳本 · 不可核准</span></div>' +
        '<div class="card"><p class="hint">⚠ 脆弱性：' + (y.warnings_zh || []).map(esc).join("；") + "</p>" +
        '<div class="table-scroll"><table class="data"><tbody>' +
        '<tr><th>模擬權益</th><td class="num">' + n(y.equity) + ' USDT</td><th>模擬開始以來</th><td class="num">' + pct(y.return_since_paper_start_pct) + "</td></tr>" +
        '<tr><th>持倉數（多／空）</th><td class="num">' + n(y.n_positions, 0) + "（" + n(y.n_long, 0) + "／" + n(y.n_short, 0) + '）</td><th>毛／淨曝險</th><td class="num">' + n(y.gross, 2) + "x / " + n(y.net, 2) + "x</td></tr>" +
        '<tr><th>距強平（全部持倉同步不利）</th><td class="num">' + pct(y.liq_distance_pct == null ? null : -y.liq_distance_pct) + '</td><th>組合槓桿 L_C（' + esc(d.day || "—") + '）</th><td class="num">' + n(d.L_C, 3) + " / 上限 5</td></tr>" +
        '<tr><th>等風險權重 a_U／a_V</th><td class="num">' + n(d.a_U, 3) + " / " + n(d.a_V, 3) + '</td><th>V1 合格幣數（多／空）</th><td class="num">' + n(d.n_eligible, 0) + "（" + n(d.n_long_V, 0) + "／" + n(d.n_short_V, 0) + "）</td></tr>" +
        '<tr><th>σ60 U1／V1／組合</th><td class="num">' + pct(d.sigma_U == null ? null : d.sigma_U * 100) + " / " + pct(d.sigma_V == null ? null : d.sigma_V * 100) + " / " + pct(d.sigma_C == null ? null : d.sigma_C * 100) +
        '</td><th>目標毛／淨曝險</th><td class="num">' + n(d.gross_target_C, 2) + "x / " + n(d.net_target_C, 2) + "x</td></tr>" +
        '<tr><th>最大部位</th><td colspan="3">' + topPos(y) + "</td></tr>" +
        '<tr><th>下次決策成交</th><td>' + esc(tpt(y.next_fill_utc)) + '</td><th>最後結算 K 線</th><td>' + esc(tpt(y.last_closed_bar_utc)) + "</td></tr>" +
        '<tr><th>資金費／手續費（模擬）</th><td class="num">' + n(y.funding_pnl) + " / " + n(y.fees) + '</td><th>更新</th><td>' + esc(tpt(y.updated_at_utc)) + "</td></tr>" +
        "</tbody></table></div>" + fillsTable(y.recent_fills) +
        '<p class="hint">' + lastRunNote(y) + " · 起始點：分析師種子狀態（2026-10-10 23:00 UTC 收盤，回測自 2020-06-24 以 10,000 起算）</p></div></section>";
    }
    var x = P.X1;
    if (x) {
      var dx = x.last_decision || {};
      out += '<section class="section" id="sec-paper-x1">' +
        '<div class="section-head"><h2>模擬倉 X1 · <span class="badge muted">模擬（非實盤）</span></h2>' +
        '<span class="hint">TradingView Super Scalper 日線移植（PIT 流動全市場，多空）· Y1 的 V1 腿單獨運行，帳戶 vt60、上限 3x · 全倉 · 無停損 · 不下任何單 · 不計入帳本 · 不可核准</span></div>' +
        '<div class="card"><div class="table-scroll"><table class="data"><tbody>' +
        '<tr><th>模擬權益</th><td class="num">' + n(x.equity) + ' USDT</td><th>模擬開始以來</th><td class="num">' + pct(x.return_since_paper_start_pct) + "</td></tr>" +
        '<tr><th>持倉數（多／空）</th><td class="num">' + n(x.n_positions, 0) + "（" + n(x.n_long, 0) + "／" + n(x.n_short, 0) + '）</td><th>毛／淨曝險</th><td class="num">' + n(x.gross, 2) + "x / " + n(x.net, 2) + "x</td></tr>" +
        '<tr><th>帳戶槓桿 L_V（' + esc(dx.day || "—") + '）</th><td class="num">' + n(dx.L_V, 3) + ' / 上限 3</td><th>合格幣數</th><td class="num">' + n(dx.n_eligible, 0) + "</td></tr>" +
        '<tr><th>下次決策成交</th><td>' + esc(tpt(x.next_fill_utc)) + '</td><th>距強平</th><td class="num">' + pct(x.liq_distance_pct == null ? null : -x.liq_distance_pct) + "</td></tr>" +
        "</tbody></table></div>" + fillsTable(x.recent_fills) + "</div></section>";
    }
    return out;
  }

  function renderChanges() {
    var rows = changeRows().slice(0, CHANGES_N);
    var body = rows.length ? rows.map(tradeRow).join("") :
      '<tr><td colspan="9" class="empty-row">尚無成交紀錄</td></tr>';
    return '<section class="section" id="sec-changes">' +
      '<div class="section-head"><h2>帳戶變動明細</h2><span class="hint">最新 ' + CHANGES_N + ' 筆（新→舊）· <a href="./history.html">完整歷史</a></span></div>' +
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
          "<td><strong>" + esc((p.code ? p.code + " " : "") + (p.symbol || p.asset || "—")) + '</strong><div class="kpi-sub">' + esc(p.slot) + "</div></td>" +
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

      var kind = String(src.trigger_kind || "");
      if (!kind) {
        kind = fam === "ema_cross_atr" ? "ema_cross"
          : fam === "short_trend_perp" ? "short_trend"
          : fam === "bb_squeeze_breakout_perp" ? "bb_squeeze"
          : ((fam === "ls_donch_btc_regime_perp" || fam === "donchian_long_short_btc_regime") ? "donchian_ls" : "donchian_hi");
      }
      var statusLabel = modeLabel(om);
      var trigHtml = null, distHtml = null, ruleTxt = null, subTxt = null;
      var needsMarket = (mark == null || trigger == null || atr == null);
      if (kind === "ema_cross") {
        var fN = src.ema_fast_n || pms.ema_fast || pms.fast || 12;
        var sN = src.ema_slow_n || pms.ema_slow || pms.slow || 26;
        var ef = src.ema_fast != null ? Number(src.ema_fast) : null;
        var es = src.ema_slow != null ? Number(src.ema_slow) : null;
        var gap = src.ema_gap_pct != null ? Number(src.ema_gap_pct) : null;
        var bull = src.ema_bull === true || (ef != null && es != null && ef > es);
        var btcOn = src.btc_regime_on;
        var btcTxt = pms.btc_regime || src.btc_regime ? ("BTC 濾網 " + (btcOn === true ? "開（可做多）" : btcOn === false ? "關（不進場）" : "未知")) : "無 BTC 濾網";
        subTxt = "EMA " + fN + "/" + sN;
        trigHtml = (ef != null ? "EMA" + fN + " " + num(ef, 4) : "—") + "<br>" + (es != null ? "EMA" + sN + " " + num(es, 4) : "—");
        distHtml = gap != null ? ("差距 " + (gap >= 0 ? "+" : "") + num(gap, 2) + "%<br>" + (bull ? "多頭區" : "空頭區")) : "—";
        if (om === "live") {
          if ((pms.btc_regime || src.btc_regime) && btcOn === false) statusLabel = "BTC 濾網關閉";
          else if (bull) statusLabel = "已在多頭區 · 等下一次黃金交叉";
          else statusLabel = "等黃金交叉";
        }
        ruleTxt = "EMA" + fN + " 上穿 EMA" + sN + "（只在交叉當根收盤進場，與回測一致）· " + btcTxt +
          (src.last_cross_bar_ts ? " · 上次交叉 " + String(src.last_cross_bar_ts).slice(0, 10) : "");
        needsMarket = false;
      } else if (kind === "short_trend") {
        var sfN = src.ema_fast_n || pms.ema_fast || "?";
        var ssN = src.ema_slow_n || pms.ema_slow || "?";
        var sef = src.ema_fast != null ? Number(src.ema_fast) : null;
        var ses = src.ema_slow != null ? Number(src.ema_slow) : null;
        var sgap = src.ema_gap_pct != null ? Number(src.ema_gap_pct) : (sef != null && ses ? (sef / ses - 1) * 100 : null);
        var bear = src.ema_bear === true || (sef != null && ses != null && sef < ses);
        var isDon = String(src.signal_kind || pms.signal || "sema").toLowerCase() === "sdon";
        subTxt = isDon ? ("Donch " + (pms.donch_n || donchN) + " 做空") : ("EMA " + sfN + "/" + ssN + " 做空");
        trigHtml = isDon ? "—" : ((sef != null ? "EMA" + sfN + " " + num(sef, 6) : "—") + "<br>" + (ses != null ? "EMA" + ssN + " " + num(ses, 6) : "—"));
        distHtml = sgap != null ? ("差距 " + (sgap >= 0 ? "+" : "") + num(sgap, 2) + "%<br>" + (bear ? "空頭區" : "多頭區")) : "—";
        if (om === "live") statusLabel = bear ? "已在空頭區 · 等下一次死叉（不追）" : "等死叉做空";
        ruleTxt = src.entry_condition || src.entry_rule ||
          ("EMA" + sfN + "/" + ssN + " 死叉做空 · 停損 " + (pms.stop_atr_mult || "?") + "×ATR · 移動 " + (pms.trail_atr_mult || "?") + "×ATR · 濾網 " + (pms.filt || "none"));
        needsMarket = false;
      } else if (kind === "bb_squeeze") {
        // Z bb_squeeze_breakout_perp (long only, perp, 1x): rule text from the slot's own params
        var bN = src.bb_n || pms.bb_n || "?", bK = src.bb_k || pms.bb_k || "?", bQ = src.sq_q || pms.sq_q || "?";
        var bUp = src.bb_up != null ? Number(src.bb_up) : null;
        var bMid = src.bb_mid != null ? Number(src.bb_mid) : null;
        var bSq = src.squeeze_recent === true;
        var bSt = src.bb_state != null ? Number(src.bb_state) : null;
        subTxt = "BB" + bN + "/" + bK + " 壓縮突破 做多";
        trigHtml = "上軌 " + (bUp != null ? num(bUp, 6) : "—") + "<br>中軌 " + (bMid != null ? num(bMid, 6) : "—");
        distHtml = (bUp != null && mark ? "距上軌 " + num((bUp / mark - 1) * 100, 2) + "%" : "—") + "<br>" + (bSq ? "近 5 根有壓縮" : "無壓縮");
        if (om === "live") statusLabel = bSt === 1 ? "已在突破狀態 · 等回到中軌下方重置（不追）" : (bSq ? "壓縮中 · 等收盤突破上軌" : "等布林壓縮");
        ruleTxt = src.entry_condition || src.entry_rule ||
          ("BB(" + bN + "," + bK + ") 壓縮（帶寬 ≤ 前 120 根 " + bQ + " 分位，近 5 根內）後收盤突破上軌做多 · 收盤跌破中軌出場 · 停損 " +
           (pms.stop_atr_mult || pms.stop_m || "?") + "×ATR · 移動 " + (pms.trail_atr_mult || pms.trail_m || "?") + "×ATR · 1x 永續");
        needsMarket = false;
      } else if (kind === "donchian_ls") {
        var hi = src.donch_hi != null ? Number(src.donch_hi) : null;
        var lo = src.donch_lo != null ? Number(src.donch_lo) : null;
        var dh = src.dist_hi_pct != null ? Number(src.dist_hi_pct) : (hi != null && mark ? (hi / mark - 1) * 100 : null);
        var dl = src.dist_lo_pct != null ? Number(src.dist_lo_pct) : (lo != null && mark ? (lo / mark - 1) * 100 : null);
        var dir = String(src.allowed_direction || "");
        var dirTxt = dir === "long_only" ? "只做多（BTC 多頭）" : dir === "short_only" ? "只做空（BTC 空頭）" : "多空皆可（BTC 狀態未知）";
        subTxt = "Donch " + donchN + " 多空 ×" + (pms.leverage || src.leverage || 1);
        trigHtml = "上 " + (hi != null ? num(hi, 4) : "—") + "<br>下 " + (lo != null ? num(lo, 4) : "—");
        distHtml = "上 " + (dh != null ? (dh >= 0 ? "+" : "") + num(dh, 2) + "%" : "—") + "<br>下 " + (dl != null ? num(dl, 2) + "%" : "—");
        if (om === "live") statusLabel = dir === "long_only" ? "等突破上軌做多" : dir === "short_only" ? "等跌破下軌做空" : "等突破";
        ruleTxt = "Donchian" + donchN + " 收盤突破上軌做多／跌破下軌做空 · 目前允許：" + dirTxt + " · Demo 合約";
        needsMarket = false;
      }

      out.push({
        kind: kind,
        trig_html: trigHtml,
        dist_html: distHtml,
        sub_txt: subTxt,
        slot: s.slot,
        strategy_id: s.strategy_id,
        code: s.code || src.code || "",
        symbol: sym,
        asset: sym.replace(/USDT$/i, ""),
        tf: s.timeframe || s.tf || src.tf,
        order_mode: om,
        status_label: statusLabel,
        target_notional_usdt: notion,
        mark: mark,
        trigger: trigger,
        atr: atr,
        atr_mode: atrMode,
        dist_pct: distPct,
        dist_atr: distAtr,
        donch_n: donchN,
        entry_rule: ruleTxt || src.entry_condition || src.entry_rule || ("Donchian" + donchN + " 突破上軌"),
        _needs_market: needsMarket
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
        if (pl.dist_html) distTxt = pl.dist_html;
        var badge = pl.order_mode === "live" ? "ok" : "muted";
        return '<tr data-slot="' + esc(pl.slot) + '">' +
          "<td><strong>" + esc((pl.code ? pl.code + " " : "") + pl.asset) + '</strong><div class="kpi-sub">' +
          esc(pl.slot) + " · " + esc(pl.tf || "—") + " / " + esc(pl.sub_txt || ("Donch " + String(pl.donch_n))) + "</div></td>" +
          '<td class="num">' + num(pl.target_notional_usdt, 0) + "</td>" +
          '<td><span class="badge ' + badge + '">' + esc(pl.status_label) + "</span></td>" +
          '<td class="num">' + (pl.trig_html ? pl.trig_html : (pl.trigger != null ? num(pl.trigger, 4) : "—")) + "</td>" +
          '<td class="num">' + (pl.mark != null ? num(pl.mark, 4) : "—") + "</td>" +
          '<td class="num">' + distTxt + "</td>" +
          "<td>" + esc(pl.entry_rule || "—") + "</td></tr>";
      }).join("");
    }
    return '<section class="section" id="sec-planned">' +
      '<div class="section-head"><h2>預計持倉</h2><span class="hint">已核准家族 · live 槽尚未成交 · 狀態只依 live_slots.order_mode</span></div>' +
      '<div class="card"><div class="table-scroll"><table class="data" id="plannedTable">' +
      "<thead><tr><th>標的</th><th class=\"num\">計畫名義</th><th>狀態</th>" +
      "<th class=\"num\">觸發價／指標</th><th class=\"num\">現價</th><th class=\"num\">距觸發</th><th>進場條件</th></tr></thead>" +
      "<tbody>" + body + "</tbody></table></div></div></section>";
  }


  // Known manual (non-strategy) cash movements, listed separately from fees (re-audit 2026-10-08)
  var MANUAL_ADJUSTMENTS = [{ label: "NEAR 手動交易（9/18–9/24，非策略）", usdt: 680.86 }];
  function feeLabel(st) {
    if (st.fee_missing > 0) {
      var known = st.fees != null && Math.abs(st.fees) >= 0.005 ? "已記錄 " + num(st.fees, 2) + "；" : "";
      return "（手續費：" + known + st.fee_missing + " 筆舊交易無資料，以上為未扣手續費毛額）";
    }
    return st.fees != null ? "（已扣手續費 " + num(st.fees, 2) + "）" : "（手續費：無資料）";
  }
  function unattributedBreakdown(st, q) {
    if (st.unattributed == null || Math.abs(st.unattributed) < 0.01) return "";
    var manual = MANUAL_ADJUSTMENTS.reduce(function (a, m) { return a + m.usdt; }, 0);
    var rest = st.unattributed - manual;
    var lines = MANUAL_ADJUSTMENTS.map(function (m) {
      return "<br><span class=\"dim\">" + m.label + " " + signedNum(m.usdt, 2) + " " + q + "</span>";
    }).join("");
    lines += "<br><span class=\"dim\">未記錄手續費（推估）" + signedNum(rest, 2) + " " + q + "（舊交易 fee 欄位為空，由餘額差推得）</span>";
    return lines;
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
      var code = a.code || src.code || "";
      var stZh = src.status_zh || a.label_zh || "已核准 · 上線待命";
      var tf = src.tf || "";
      var notion = a.notional_usdt || src.quote_usdt || "";
      var key = String(a.strategy_id || a.slot || title);
      var open = !!expandedStrategyKeys[key];
      return '<article class="strategy-card active' + (open ? " open" : " collapsed") + '" data-key="' + esc(key) + '">' +
        '<div class="sc-head" data-toggle-key="' + esc(key) + '">' +
        '<div class="sc-top"><strong class="sc-title" title="' + esc(a.strategy_id || "") + '">' +
        esc((code ? code + " " : "") + title) + (tf ? " · " + esc(tf) : "") + "</strong>" +
        '<span class="badge ok">' + esc(stZh) + "</span>" +
        '<span class="chevron" aria-hidden="true">' + (open ? "▾" : "▸") + "</span></div>" +
        '<p class="sc-sum">' + esc(a.strategy_id || "") + "</p>" +
        '<div class="sc-meta">' + (open ? "點擊收合" : "點擊展開進場／觸發") + "</div></div>" +
        '<div class="sc-body"><div class="sc-rules">' +
        '<div><span class="lbl">進場</span> ' + esc(src.entry_condition || "—") + "</div>" +
        '<div><span class="lbl">觸發</span> ' + (src.trigger != null ? num(src.trigger, 4) : "—") +
        " · 現價 " + (src.mark != null ? num(src.mark, 4) : "—") +
        (src.trigger != null && src.mark != null ? " · 距 " + num(Number(src.trigger) - Number(src.mark), 4) : "") +
        "</div>" +
        '<div><span class="lbl">狀態</span> ' + esc(stZh) + "</div>" +
        (notion ? '<div><span class="lbl">名義</span> ' + esc(String(notion)) + " USDT</div>" : "") +
        "</div></div></article>";
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
          ts: t.time, kind: t.side === "BUY" ? "OPEN" : "CLOSE", symbol: t.symbol, code: t.code,
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

  function mountPie(parts, canvasId, quote) {
    var canvas = $(canvasId || "allocPie");
    if (!canvas || typeof Chart === "undefined") return;
    var q = quote || "USDT";
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
          return d.label + "  " + pct.toFixed(1) + "%  ·  " + Number(d.value).toFixed(2) + " " + q;
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

  function mountAllPies() {
    mountPie(piePartsForQuote("USDT"), "allocPieUSDT", "USDT");
    mountPie(piePartsForQuote("USDC"), "allocPieUSDC", "USDC");
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
    // Homepage strategy cards looked clickable but had no handler (details always shown).
    document.querySelectorAll("#sec-strategies .sc-head[data-toggle-key]").forEach(function (head) {
      head.onclick = function (ev) {
        if (ev.target.closest("button, a, input, label")) return;
        var key = head.getAttribute("data-toggle-key");
        if (!key) return;
        expandedStrategyKeys[key] = !expandedStrategyKeys[key];
        var card = head.closest(".strategy-card");
        if (!card) return;
        var open = !!expandedStrategyKeys[key];
        card.classList.toggle("collapsed", !open);
        card.classList.toggle("open", open);
        var ch = card.querySelector(".chevron");
        if (ch) ch.textContent = open ? "▾" : "▸";
        var meta = card.querySelector(".sc-meta");
        if (meta) meta.textContent = open ? "點擊收合" : "點擊展開進場／觸發";
      };
    });
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
      renderActual() + renderPlanned() + renderPaperU1() + renderPaperY1() + renderStrategies() + renderTrades() + renderChanges();
    mountAllPies();
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
      allocCfg = null;
      strategyCodes = await getJSONOpt(CODES_URL);
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
        $("lastUpdated").innerHTML = freshnessHtml();
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
