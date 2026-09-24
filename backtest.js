/* Unified 3y scores — grouped by strategy card. Approve/revoke via Cloud API + PIN. */
(function () {
  "use strict";

  var SCORES_URL = "./data/unified-3y/scores.json";
  var CATALOG_URL = "./data/unified-3y/catalog.json";
  var CLOUD_CFG = "./data/cloud_api.json";
  var EQUITY_BASE = "./data/unified-3y/equity/";
  var MAX_NOTIONAL = 1500;
  // Cloud runner whitelist (must match service SUPPORTED_FAMILIES).
  var RUNNER_FAMILIES = { donchian_atr: true, donchian_btc_regime: true };
  // catalog family_id → runner family id (null = not runnable on cloud)
  var CATALOG_FAMILY_RUNNER = {
    donchian_atr: "donchian_atr",
    donchian_btc_regime: "donchian_btc_regime",
    donchian_lev: null,
    donchian_lev_vol: null,
    donchian_long_short_btc_regime: null,
    donchian_fear_greed: null,
    ema_cross_atr: null,
    ema_trend_hold: null,
    supertrend: null,
    momentum_rotation: null,
    sma_regime_hold: null,
    dual_ma_rsi: null
  };
  var LOCK_PREP = "準備中";
  var LOCK_NO_PASS = "未過關，不開放批准";

  var scores = null;
  var catalog = null;
  var filterBothOnly = false;
  var expandedKeys = {};
  var approvedMap = {};
  var signalOnlyMap = {};
  var apiBase = "";

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function num(v, d) {
    if (v == null || v === "" || Number.isNaN(Number(v))) return "—";
    return Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function pctPts(v) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    var n = Number(v);
    return (n > 0 ? "+" : "") + n.toFixed(2) + "%";
  }
  function signedCls(v) {
    if (v == null || Number.isNaN(Number(v))) return "";
    if (Number(v) > 0) return "pos";
    if (Number(v) < 0) return "neg";
    return "";
  }

  function gateReasonZh(raw) {
    var s = String(raw || "");
    if (/ret_3y/.test(s) && /bh_ret/.test(s)) return "3年報酬未勝過買進持有（" + s + "）";
    if (/oos_pass/.test(s) || /oos /.test(s)) return "樣本外勝率不足（" + s + "）";
    if (/maxdd/i.test(s)) return "最大回撤過大（" + s + "）";
    return s || "—";
  }

  function strategyKey(sid) {
    var m = String(sid || "").match(/^(.+?)__([A-Z0-9]+)__(.+)$/);
    return m ? m[1] : String(sid || "");
  }

  function familyOf(row) {
    var k = String(row.kind || row.family || "");
    if (k) return k;
    var id = String(row.strategy_id || "");
    if (/btcRegime|btc_regime/i.test(id)) return "donchian_btc_regime";
    if (/^donchian/.test(id)) return "donchian";
    if (/^ema/.test(id)) return "ema";
    if (/^supertrend/.test(id)) return "supertrend";
    return "other";
  }

  /** Futures / leverage / short — lock even if family name looks like donchian_*. */
  function needsDerivatives(row, familyId) {
    row = row || {};
    var p = row.params || {};
    var lev = Number(p.leverage != null ? p.leverage : row.leverage);
    if (lev > 1) return true;
    var side = String(p.side || p.kind || row.side || row.kind || "").toLowerCase();
    if (side === "ls" || side === "short" || side === "long_short" || side === "longshort") return true;
    var mkt = String(p.market || row.market || p.venue || "").toLowerCase();
    if (/futures|perp|perpetual|swap|contract|合約/.test(mkt)) return true;
    if (p.check_liq === true || row.check_liq === true) return true;
    var fund = Number(p.funding_ann != null ? p.funding_ann : (row.funding_ann != null ? row.funding_ann : 0));
    if (fund > 0) return true;
    var sid = String(row.strategy_id || "");
    if (/^ls_|^lev_|__ls$|_long_short|long.?short/i.test(sid)) return true;
    var fid = String(familyId || row._family_id || row.family || "");
    if (/long_short|_ls|donchian_lev/.test(fid)) return true;
    return false;
  }

  function runnerFamilyId(familyId, row) {
    if (familyId && Object.prototype.hasOwnProperty.call(CATALOG_FAMILY_RUNNER, familyId)) {
      return CATALOG_FAMILY_RUNNER[familyId];
    }
    var fam = familyOf(row || {});
    if (fam === "donchian") return "donchian_atr";
    if (fam === "donchian_btc_regime") return "donchian_btc_regime";
    return null;
  }

  /** True only for cloud-supported spot long families (no leverage/short). */
  function runnerSupports(row, familyId) {
    row = row || {};
    if (row.supported_by_runner === false) return false;
    familyId = familyId || row._family_id || row.family || "";
    if (needsDerivatives(row, familyId)) return false;
    var rf = runnerFamilyId(familyId, row);
    return !!(rf && RUNNER_FAMILIES[rf]);
  }

  function approveLockReason(row, familyId, familyPass3y) {
    row = row || {};
    familyId = familyId || row._family_id || "";
    // Emily: no 3y-pass rows in family → never approvable
    if (familyPass3y === false) return LOCK_NO_PASS;
    if (!runnerSupports(row, familyId)) return LOCK_PREP;
    if (needsDerivatives(row, familyId) && !runnerSupports(row, familyId)) return LOCK_PREP;
    return null;
  }

  /** Emily 資金控管: lock unless BOTH 3y + full-period gates pass. */
  function failReasons(row) {
    var raw = row.gate_fail_reasons || row.gate_fail_reasons_3y || [];
    if (!Array.isArray(raw)) raw = [];
    // Drop full-period / 全期 reasons — 全期僅參考，不算門檻失敗
    return raw.filter(function (s) {
      var x = String(s || "");
      if (/full[_ ]?period|gate_pass_full|全期|full-period|full period/i.test(x)) return false;
      return true;
    });
  }

  function fpFailReasons(fp) {
    fp = fp || {};
    return fp.gate_fail_reasons || fp.gate_fail_reasons || [];
  }
  function gatePass3y(row) {
    if (row.gate_pass_3y != null) return !!row.gate_pass_3y;
    if (row.gate_pass != null) return !!row.gate_pass;
    return false;
  }
  // Emily 資金控管：核准門檻＝僅 3 年窗（全期僅參考）
  var bothGatesOk = gatePass3y; // legacy alias

  function familyHasPass3y(gOrRows) {
    var rows = Array.isArray(gOrRows) ? gOrRows : ((gOrRows && gOrRows.rows) || []);
    return rows.some(function (r) { return gatePass3y(r); });
  }



  function describeRules(key, sample) {
    var p = (sample && sample.params) || {};
    var parts = [];
    var fam = familyOf(sample || {});
    if (fam.indexOf("donchian") === 0 || /^donchian/.test(key)) {
      var n = p.donch_n != null ? p.donch_n : (key.match(/donchian(\d+)/) || [])[1] || "?";
      parts.push("唐奇安通道（Donchian " + n + "）突破進場：收盤價站上上軌。");
      var sm = p.stop_m != null ? p.stop_m : (key.match(/_s([\d.]+)/) || [])[1];
      var tm = p.trail_m != null ? p.trail_m : (key.match(/_t([\d.]+)/) || [])[1];
      if (sm != null) parts.push("初始停損：進場價 − " + sm + "×ATR（Wilder ATR14）。");
      if (tm != null) parts.push("移動停損：" + tm + "×ATR，僅隨收盤上移（ratchet）。");
      parts.push("出場：最低價觸及停損時以 min(開盤, 停損) 平倉；或收盤跌破下軌。");
      if (p.reset_below_hi === true) {
        parts.push("再進場規則：出場後需先收盤回到上軌下方（reset_below_hi）才可再次武裝。");
      }
      if (p.btc_regime || /btcRegime/i.test(key)) {
        parts.push("BTC 日線濾網：僅當 BTC 日收盤 > SMA200 時允許進場。");
      }
      if (p.max_hold != null) parts.push("最長持有 " + p.max_hold + " 根 K 棒。");
    } else if (fam === "ema" || /^ema/.test(key)) {
      parts.push("EMA 交叉／趨勢策略（詳見 params）。");
      if (p.btc_regime || /btcRegime/i.test(key)) {
        parts.push("BTC 日線濾網：BTC 收盤 > SMA200。");
      }
    } else if (fam === "supertrend" || /^supertrend/.test(key)) {
      parts.push("SuperTrend 趨勢跟隨。");
    } else if (fam === "rotation" || /rotation|momentum/i.test(key)) {
      parts.push("多幣動能輪動：依回看報酬選前 N 且為正報酬等權持有。");
    } else if (/fear_greed|fg_/i.test(key) || (sample && sample.params && sample.params.fg_mode)) {
      parts.push("Donchian 突破＋恐懼貪婪指數過濾做多時段。");
      if (sample && sample.params && sample.params.leverage > 1) parts.push("含名義槓桿（合約假設）。");
    } else if (/long_short|_ls/i.test(key) || (sample && String((sample.params || {}).side || "").toLowerCase() === "ls")) {
      parts.push("Donchian 多空雙向：依 BTC SMA200 分向過濾。");
      parts.push("需合約／做空能力（雲端現貨 runner 不支援）。");
    } else if (/_lev|lev_/i.test(key) || (sample && Number((sample.params || {}).leverage) > 1)) {
      parts.push("Donchian＋ATR 架構加名義槓桿（永續假設，含資金費／強平檢查）。");
    } else if (/sma_regime|dual_ma/i.test(key)) {
      parts.push("均線政權／雙均線類規則（詳見家族說明）。");
    } else {
      parts.push("策略參數見下表；規則依家族說明與 params。");
    }
    return parts;
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

  async function getJSON(url) {
    var res = await fetch(url + (url.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now(), { cache: "no-store" });
    if (!res.ok) throw new Error(url + " HTTP " + res.status);
    return res.json();
  }

  async function resolveApiBase() {
    try {
      var c = await getJSON(CLOUD_CFG);
      if (c && c.base) return String(c.base).replace(/\/$/, "");
    } catch (e) {}
    return window.TRADER_API_BASE || "";
  }

  async function loadApproved() {
    if (!apiBase) return;
    try {
      var d = await getJSON(apiBase + "/approved");
      approvedMap = {};
      signalOnlyMap = {};
      (d.approved || []).forEach(function (a) {
        approvedMap[a.strategy_id] = a;
      });
      (d.signal_only || []).forEach(function (a) {
        signalOnlyMap[a.strategy_id] = a;
      });
      $("cloudBanner").classList.add("hidden");
    } catch (e) {
      $("cloudBanner").classList.remove("hidden");
    }
  }

  async function postControl(path, pin, body) {
    var res = await fetch(apiBase + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Trader-Pin": pin },
      body: JSON.stringify(body || {})
    });
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    return data;
  }

  function openPinModal(opts) {
    var modal = $("pinModal");
    var body = $("pinModalBody");
    var saved = getPin();
    body.innerHTML =
      "<h3>" + esc(opts.title || "操作密碼") + "</h3>" +
      (opts.confirmText ? '<p class="modal-confirm">' + esc(opts.confirmText) + "</p>" : "") +
      '<label class="pin-label">操作密碼<input type="password" id="pinInput" class="pin-input" autocomplete="off" value="' + esc(saved) + '" /></label>' +
      '<label class="pin-remember"><input type="checkbox" id="pinRemember" ' + (saved ? "checked" : "") + ' /> 記住於本次瀏覽</label>' +
      (opts.extraHtml || "") +
      '<div class="modal-actions">' +
      '<button type="button" class="primary" id="pinSubmit">確認</button>' +
      '<button type="button" id="pinCancel">取消</button></div>' +
      '<p class="modal-result" id="pinResult"></p>';
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    function submit() {
      var pin = ($("pinInput") && $("pinInput").value) || "";
      setPin(pin, $("pinRemember") && $("pinRemember").checked);
      opts.onSubmit(pin, $("pinResult"));
    }
    $("pinSubmit").onclick = submit;
    $("pinCancel").onclick = closeModal;
    $("pinInput").onkeydown = function (e) { if (e.key === "Enter") submit(); };
  }
  function closeModal() {
    var modal = $("pinModal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  function statusBadge(st) {
    var s = String(st || "—");
    var cls = "muted";
    if (s === "現役") cls = "ok";
    else if (/候選|候補/.test(s)) cls = "warn";
    else if (/待替換|未過/.test(s)) cls = "bad";
    return '<span class="badge ' + cls + '">' + esc(s) + "</span>";
  }

  function approveControls(row, familyId) {
    var sid = row.strategy_id;
    var ap = approvedMap[sid];
    var so = signalOnlyMap[sid];
    if (ap && ap.mode !== "signal_only" && ap.approved !== false) {
      return '<span class="badge ok">已核准</span> ' +
        '<button type="button" class="btn-revoke" data-sid="' + esc(sid) + '">退回</button>';
    }
    var ok = gatePass3y(row);
    var lockReason = approveLockReason(row, familyId || row._family_id, row._family_pass_3y);
    var supported = !lockReason;
    var locked = !ok || !supported;
    var title = !ok
      ? "未過 3 年門檻，無法核准"
      : (lockReason || "核准上線待命");
    var notion = row.notional_usdt != null ? row.notional_usdt
      : (/FET/.test(sid) && /4h/.test(sid) ? 1250 : 1000);
    var bits = [];
    if (so || (ap && ap.mode === "signal_only")) {
      bits.push('<span class="badge warn">只算訊號（未核准）</span>');
    }
    if (row.data_short) {
      bits.push('<span class="badge warn" title="樣本期間偏短">data_short</span>');
    }
    if (lockReason) {
      bits.push('<span class="badge muted">' + esc(lockReason) + "</span>");
    }
    var prefix = bits.length ? bits.join(" ") + " " : "";
    if (locked) {
      return prefix + '<button type="button" class="btn-approve" disabled title="' + esc(title) + '">批准</button>';
    }
    return prefix + '<button type="button" class="btn-approve" data-sid="' + esc(sid) +
      '" data-notional="' + esc(notion) + '" data-family="' + esc(familyId || row._family_id || "") +
      '" title="' + esc(title) + '">批准</button>';
  }



  var SCORE_WEIGHT_LABELS = {
    cagr_3y: "CAGR 3y",
    ret_1y: "近 1 年",
    maxdd: "MaxDD",
    oos_pass: "OOS",
    excess_vs_bh: "超越 B&H",
    cagr: "CAGR 3y",
    ret1y: "近 1 年",
    oos: "OOS",
    excess_bh: "超越 B&H"
  };

  function scoreFormulaZh(scoring) {
    scoring = scoring || {};
    var w = scoring.weights || scoring.weight || null;
    if (!w || typeof w !== "object" || Array.isArray(w)) {
      // flat numeric map?
      var keys = Object.keys(scoring).filter(function (k) { return typeof scoring[k] === "number"; });
      if (keys.length) w = scoring;
      else return "—";
    }
    var order = ["cagr_3y", "cagr", "ret_1y", "ret1y", "maxdd", "oos_pass", "oos", "excess_vs_bh", "excess_bh"];
    var seen = {};
    var parts = [];
    function pushKey(k) {
      if (seen[k] || typeof w[k] !== "number") return;
      seen[k] = true;
      var label = SCORE_WEIGHT_LABELS[k] || k;
      parts.push(label + " " + w[k] + "%");
    }
    order.forEach(pushKey);
    Object.keys(w).forEach(pushKey);
    if (!parts.length) return "—";
    return "評分 = " + parts.join(" + ");
  }

  function renderMeta(meta, srcHint) {
    if (!meta) return "";
    var period = meta.period_3y || meta.period || {};
    var costs = meta.costs || {};
    var gate = meta.gate || {};
    var scoring = meta.scoring_method || meta.scoring || {};
    var start = period.start || period.start_utc || "—";
    var end = period.end || period.end_utc || "—";
    var oneWay = costs.one_way_bps != null ? costs.one_way_bps : 20;
    var fee = costs.fee_bps != null ? costs.fee_bps : "—";
    var slip = costs.slippage_bps != null ? costs.slippage_bps : "—";
    var oosMin = gate.oos_pass_min || "4/6";
    var maxdd = gate.maxdd_max_abs != null ? gate.maxdd_max_abs : 45;
    var nPass = meta.n_gate_pass_3y != null ? meta.n_gate_pass_3y : meta.n_gate_pass;
    var nPass3y = meta.n_gate_pass_3y != null ? meta.n_gate_pass_3y : (meta.n_gate_pass != null ? meta.n_gate_pass : "—");
    var nAll = meta.n_rows != null ? meta.n_rows : meta.n_strategies;
    var nFam = meta.n_strategies != null ? meta.n_strategies : "—";
    return '<section class="section" id="sec-meta">' +
      '<div class="section-head"><h2>統一標準</h2><span class="hint">產生於 ' + esc(meta.generated_at || "—") + "</span></div>" +
      '<div class="meta-grid">' +
      "<div><span class=\"lbl\">期間</span> 近 3 年 · " + esc(start) + " → " + esc(end) + "</div>" +
      "<div><span class=\"lbl\">起始資金</span> " + num(meta.initial, 0) + " USDT</div>" +
      "<div><span class=\"lbl\">成本</span> 單邊 " + esc(oneWay) + " bps（fee " + esc(fee) + " + slip " + esc(slip) + "）</div>" +
      "<div><span class=\"lbl\">門檻</span> 勝 B&amp;H · OOS≥" + esc(oosMin) + " · |MaxDD|≤" + esc(maxdd) + "% · 門檻＝近 3 年（全期僅參考）</div>" +
      "<div><span class=\"lbl\">通過</span> 3年 " + esc(nPass) + " · 過關 " + esc(nPass3y) + " / " + esc(nAll) + " 列（" + esc(nFam) + " 族）</div>" +
      "<div class=\"meta-formula\"><span class=\"lbl\">計分</span> " + esc(scoreFormulaZh(scoring)) + "</div>" +
      "</div>" +
      (srcHint ? '<p class="hint" style="margin:8px 0 0">資料來源：' + esc(srcHint) + " · 頁面分數為 catalog 重標（與 scores.json 不同）</p>" : "") +
      "</section>";
  }



  /** Build strategy groups from scores.json */
  function groupsFromScores(data) {
    var map = {};
    (data.strategies || []).forEach(function (r) {
      var key = strategyKey(r.strategy_id);
      if (!map[key]) {
        map[key] = { key: key, params: r.params || {}, kind: r.kind || familyOf(r), rows: [] };
      }
      map[key].rows.push(r);
      if (!map[key].params || !Object.keys(map[key].params).length) map[key].params = r.params || {};
    });
    return Object.keys(map).map(function (k) {
      var g = map[k];
      g.rows.sort(function (a, b) { return (b.score || 0) - (a.score || 0); });
      var famPass = rows.some(function (r) { return gatePass3y(r); });
      rows.forEach(function (r) { r._family_pass_3y = famPass; });
      g.best_score = g.rows.length ? (g.rows[0].score || 0) : 0;
      return g;
    }).sort(function (a, b) { return b.best_score - a.best_score; });
  }

  
  // runnerFamilyId / runnerSupports defined above


  function gatePass3y(row) {
    if (row.gate_pass_3y != null) return !!row.gate_pass_3y;
    if (row.gate_pass != null) return !!row.gate_pass;
    return false;
  }
  var bothGatesOk = gatePass3y;


  function failReasons(row) {
    return row.gate_fail_reasons || row.gate_fail_reasons || [];
  }


  /** Prefer catalog.json (analyst schema). Fallback: group scores.json. */
  function groupsFromCatalog(cat) {
    if (!cat || typeof cat !== "object") return null;
    var list = cat.strategies || cat.families || cat.catalog || cat.groups || cat.items;
    if (!Array.isArray(list) || !list.length) return null;
    var out = [];
    list.forEach(function (g) {
      var familyId = g.strategy_family_id || g.family_id || g.id || g.key || "";
      var rowsRaw = g.rows || g.variants || g.symbols || g.results || [];
      var rows = rowsRaw.map(function (r) {
        var sid = r.strategy_id || r.id || "";
        return Object.assign({}, r, {
          strategy_id: sid,
          symbol: r.symbol,
          timeframe: r.timeframe || (r.params && (r.params.tf || r.params.timeframe)) || "",
          initial: r.initial != null ? r.initial : 10000,
          final: r.final,
          ret_3y: r.ret_3y,
          ret_1y: r.ret_1y,
          bh_ret_3y: r.bh_ret_3y,
          maxdd: r.maxdd_3y != null ? r.maxdd_3y : r.maxdd,
          maxdd_3y: r.maxdd_3y != null ? r.maxdd_3y : r.maxdd,
          oos_pass: r.oos_pass_3y || r.oos_pass,
          oos_wins: r.oos_wins_3y != null ? r.oos_wins_3y : r.oos_wins,
          oos_total: r.oos_total_3y != null ? r.oos_total_3y : (r.oos_total || 6),
          n_trades: r.n_trades_3y != null ? r.n_trades_3y : r.n_trades,
          gate_pass: r.gate_pass_3y != null ? r.gate_pass_3y : r.gate_pass,
          gate_pass_3y: r.gate_pass_3y != null ? r.gate_pass_3y : r.gate_pass,
          gate_pass_both: r.gate_pass_both,
          gate_fail_reasons: r.gate_fail_reasons || [],
          score: r.score,
          status: r.status,
          review: r.review,
          data_short: !!r.data_short,
          full_period: r.full_period || {},
          params: r.params || {},
          _family_id: familyId,
          notional_usdt: (/FET/.test(sid) && /4h/.test(sid)) ? 1250 : 1000
        });
      });
      rows.sort(function (a, b) { return (b.score || 0) - (a.score || 0); });
      var rf = runnerFamilyId(familyId, rows[0] || {});
      out.push({
        key: familyId,
        family_id: familyId,
        name_zh: g.name_zh || g.name || familyId,
        description_zh: g.description_zh || "",
        entry_zh: g.entry_zh || "",
        exit_zh: g.exit_zh || "",
        stop_zh: g.stop_zh || "",
        params_schema: g.params_schema || {},
        params: (rows[0] && rows[0].params) || {},
        kind: familyId,
        rows: rows,
        best_score: rows.length ? (rows[0].score || 0) : 0,
        runner_family: rf,
        supported: !!(rf && RUNNER_FAMILIES[rf])
      });
    });
    out.sort(function (a, b) { return b.best_score - a.best_score; });
    return out.length ? out : null;
  }

  function dualBadge(row) {
    if (gatePass3y(row)) return '<span class="badge ok">過關</span>';
    return '<span class="badge bad">未過</span>';
  }


  function fpFailReasons(fp) {
    fp = fp || {};
    return fp.gate_fail_reasons || fp.gate_fail_reasons || [];
  }

  function expandHtml(r) {
    var fp = r.full_period || {};
    var reasons = (failReasons(r)).map(gateReasonZh).join("；");
    return '<div class="equity-wrap">' +
      '<div class="fp-grid">' +
      "<div><span class=\"lbl\">3 年（門檻）</span> 報酬 " + pctPts(r.ret_3y) + " · MaxDD " + pctPts(r.maxdd) +
      " · OOS " + esc(r.oos_pass || ((r.oos_wins != null) ? (r.oos_wins + "/" + r.oos_total) : "—")) +
      " · 門檻 " + (gatePass3y(r) ? "過關" : "未過") + "</div>" +
      "<div><span class=\"lbl\">全期（參考）</span> " + esc((fp.start || "").slice(0, 10)) + " → " + esc((fp.end || "").slice(0, 10)) +
      " · 報酬 " + pctPts(fp.ret) + " · B&amp;H " + pctPts(fp.bh_ret) +
      " · MaxDD " + pctPts(fp.maxdd) + " · OOS " + esc(fp.oos_pass || "—") +
      (fp.gate_pass_full != null ? (" · 全期門檻 " + (fp.gate_pass_full ? "過" : "未過") + "（參考）") : "") +
      "</div>" +
      (reasons ? '<div class="fail-reason">3年未過：' + esc(reasons) + "</div>" : "") +
      "</div>" +
      '<canvas id="eq-' + esc(r.strategy_id) + '" height="180"></canvas>' +
      '<div class="equity-status" id="eqst-' + esc(r.strategy_id) + '">展開列以載入權益曲線…</div></div>';
  }


  
  var approvedFamilies = {}; // family_id -> true

  function refreshApprovedFamilies(status) {
    approvedFamilies = {};
    var list = (status && status.approved_families) || [];
    (list || []).forEach(function (f) { approvedFamilies[f] = true; });
  }

  function familyApproved(familyId) {
    return !!approvedFamilies[familyId];
  }

  function familyControls(g) {
    var familyId = g.family_id || g.key;
    var sample = (g.rows && g.rows[0]) || {};
    var hasPass = familyHasPass3y(g);
    var rf = runnerFamilyId(familyId, sample);
    var onRunner = !!(rf && RUNNER_FAMILIES[rf]);
    var famLock = null;
    if (!hasPass) famLock = LOCK_NO_PASS;
    else if (!onRunner) famLock = LOCK_PREP;
    else famLock = approveLockReason(sample, familyId, true);
    var supported = !famLock;
    var nPass = (g.rows || []).filter(function (r) { return gatePass3y(r); }).length;
    var nAll = (g.rows || []).length;
    var approved = familyApproved(familyId);
    var statusHtml = approved
      ? '<span class="badge ok">已批准</span>'
      : '<span class="badge muted">未批准</span>';
    var info = '<span class="fam-pass-info">過關 ' + nPass + " / " + nAll + "</span>";
    var btn;
    if (!supported) {
      btn = '<button type="button" class="btn-fam-approve" disabled title="' + esc(famLock) + '">批准家族</button>';
    } else if (approved) {
      btn = '<button type="button" class="btn-fam-revoke" data-family="' + esc(familyId) + '">撤銷家族</button>';
    } else {
      btn = '<button type="button" class="btn-fam-approve" data-family="' + esc(familyId) + '">批准家族</button>';
    }
    return '<div class="fam-approve-bar" onclick="event.stopPropagation()">' +
      statusHtml + " " + info + " " + btn + "</div>";
  }

  function renderGroupCard(g) {
    var familyId = g.family_id || g.key;
    var supported = g.supported != null ? g.supported : runnerSupports(g.rows[0] || {}, familyId);
    var rows = g.rows || [];
    if (filterBothOnly) {
      rows = rows.filter(function (r) { return gatePass3y(r); });
    }
    if (filterBothOnly && !rows.length) return "";

    var nBoth = (g.rows || []).filter(function (r) { return gatePass3y(r); }).length;
    var best = g.best_score != null ? g.best_score : (g.rows[0] && g.rows[0].score) || 0;
    var desc = g.description_zh || "";
    if (desc.length > 90) desc = desc.slice(0, 90) + "…";
    var open = !!expandedKeys[g.key];

    var rulesHtml = "";
    if (g.description_zh || g.entry_zh || g.exit_zh || g.stop_zh) {
      rulesHtml =
        (g.description_zh ? "<li><strong>概述</strong>：" + esc(g.description_zh) + "</li>" : "") +
        (g.entry_zh ? "<li><strong>進場</strong>：" + esc(g.entry_zh) + "</li>" : "") +
        (g.exit_zh ? "<li><strong>出場</strong>：" + esc(g.exit_zh) + "</li>" : "") +
        (g.stop_zh ? "<li><strong>停損／停利</strong>：" + esc(g.stop_zh) + "</li>" : "");
    } else {
      var rules = g.rules_zh
        ? (Array.isArray(g.rules_zh) ? g.rules_zh : [g.rules_zh])
        : describeRules(g.key, g.rows[0] || {});
      rulesHtml = rules.map(function (line) { return "<li>" + esc(line) + "</li>"; }).join("");
    }
    var sample0 = (g.rows && g.rows[0]) || {};
    var hasPass0 = familyHasPass3y(g);
    var rf0 = runnerFamilyId(familyId, sample0);
    var onRunner0 = !!(rf0 && RUNNER_FAMILIES[rf0]);
    var cardLock = null;
    if (!hasPass0) cardLock = LOCK_NO_PASS;
    else if (!onRunner0) cardLock = LOCK_PREP;
    else cardLock = approveLockReason(sample0, familyId, true);
    supported = !cardLock;
    var supportNote = supported
      ? '<span class="badge ok">雲端可執行</span>'
      : '<span class="badge muted">' + esc(cardLock || LOCK_PREP) + "</span>";

    var body = rows.map(function (r) {
      var passBoth = gatePass3y(r);
      var reasons = (failReasons(r)).map(gateReasonZh).join("；");
      var oos = r.oos_pass || ((r.oos_wins != null) ? (r.oos_wins + "/" + (r.oos_total || 6)) : "—");
      var fp = r.full_period || {};
      var sym = (r.symbol || "").replace(/USDT$/, "");
      var rowCls = passBoth ? " pass" : " fail";
      if (r.data_short) rowCls += " data-short";
      return '<tr class="score-row' + rowCls + '" data-sid="' + esc(r.strategy_id) + '">' +
        "<td><strong>" + esc(sym) + "</strong> / " + esc(r.timeframe || "") +
        (r.data_short ? ' <span class="badge warn">data_short</span>' : "") +
        (!passBoth && reasons ? '<div class="fail-reason">' + esc(reasons) + "</div>" : "") + "</td>" +
        '<td class="num">' + num(r.initial, 0) + "</td>" +
        '<td class="num">' + num(r.final, 2) + "</td>" +
        '<td class="num ' + signedCls(r.ret_3y) + '">' + pctPts(r.ret_3y) + "</td>" +
        '<td class="num ' + signedCls(r.ret_1y) + '">' + pctPts(r.ret_1y) + "</td>" +
        '<td class="num ' + signedCls(r.bh_ret_3y) + '">' + pctPts(r.bh_ret_3y) + "</td>" +
        '<td class="num ' + signedCls(r.maxdd) + '">' + pctPts(r.maxdd) +
        (fp.maxdd != null ? '<div class="dim">全期 ' + pctPts(fp.maxdd) + "</div>" : "") + "</td>" +
        "<td>" + esc(oos) + "</td>" +
        "<td>" + dualBadge(r) + "</td>" +
        '<td class="num">' + num(r.score, 2) + "</td>" +
        "<td>" + statusBadge(r.status) + "</td>" +
        "</tr>" +
        '<tr class="expand-row hidden" id="exp-' + esc(r.strategy_id) + '"><td colspan="11">' +
        expandHtml(r) + "</td></tr>";
    }).join("");

    return '<article class="strategy-score-card' + (open ? "" : " collapsed") +
      (supported ? "" : " unsupported") + '" data-key="' + esc(g.key) + '">' +
      '<div class="ssc-head" data-toggle-key="' + esc(g.key) + '">' +
      "<h3>" + esc(g.name_zh || g.key) + "</h3>" +
      '<span class="badge muted">' + esc(familyId) + "</span> " + supportNote +
      familyControls(g) +
      '<span class="ssc-params">' + (g.rows || []).length + " 列 · 過關 " + nBoth +
      " · 最佳 " + num(best, 1) + "</span>" +
      '<span class="chevron">' + (open ? "▾" : "▸") + "</span>" +
      "</div>" +
      (desc ? '<p class="ssc-one-liner dim">' + esc(desc) + "</p>" : "") +
      '<div class="ssc-body">' +
      '<div class="ssc-rules"><div class="lbl">規則說明</div><ul>' + rulesHtml + "</ul></div>" +
      '<div class="table-scroll"><table class="data scores">' +
      "<thead><tr>" +
      "<th>幣別／週期</th>" +
      '<th class="num">投入</th><th class="num">最終</th>' +
      '<th class="num">3y 報酬</th><th class="num">近1年</th><th class="num">B&amp;H</th>' +
      '<th class="num">MaxDD</th><th>OOS</th><th>過關</th>' +
      '<th class="num">分數</th><th>狀態</th>' +
      "</tr></thead><tbody>" + (body || '<tr><td colspan="11" class="empty-row">此篩選下無列</td></tr>') +
      "</tbody></table></div></div></article>";
  }

  function summarizeGroups(groups) {
    var nFam = groups.length;
    var nRows = 0, nBoth = 0;
    groups.forEach(function (g) {
      (g.rows || []).forEach(function (r) {
        nRows += 1;
        if (gatePass3y(r)) nBoth += 1;
      });
    });
    return '<div class="backtest-summary">' +
      '<span class="pill">家族 ' + nFam + "</span>" +
      '<span class="pill">列數 ' + nRows + "</span>" +
      '<span class="pill">過關 ' + nBoth + "</span>" +
      "</div>" +
      '<div class="backtest-filters">' +
      '<label><input type="checkbox" id="filterBothOnly"' + (filterBothOnly ? " checked" : "") +
      '> 只看過關</label>' +
      '<span class="hint">點家族標題展開／收合規則與表格</span>' +
      "</div>";
  }

  function renderGroups(groups) {
    if (!groups || !groups.length) {
      return '<div class="empty-state">尚無策略資料</div>';
    }
    var cards = groups.map(renderGroupCard).filter(Boolean).join("");
    return '<section class="section" id="sec-scores">' +
      '<div class="section-head"><h2>策略評分（依策略家族）</h2>' +
      '<span class="hint">預設收合 · 點標題展開</span></div>' +
      summarizeGroups(groups) +
      '<div class="strategy-score-list">' + cards + "</div></section>";
  }

  async function loadEquity(sid) {
    var st = $("eqst-" + sid);
    var canvas = $("eq-" + sid);
    if (!canvas) return;
    try {
      if (st) st.textContent = "載入 equity/" + sid + ".csv …";
      var res = await fetch(EQUITY_BASE + encodeURIComponent(sid) + ".csv?t=" + Date.now(), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      var text = await res.text();
      var lines = text.trim().split(/\r?\n/);
      var labels = [], data = [];
      for (var i = 1; i < lines.length; i++) {
        var parts = lines[i].split(",");
        if (parts.length < 2) continue;
        labels.push(parts[0].slice(0, 10));
        data.push(Number(parts[1]));
      }
      if (canvas._chart) canvas._chart.destroy();
      canvas._chart = new Chart(canvas, {
        type: "line",
        data: {
          labels: labels,
          datasets: [{
            label: sid, data: data, borderColor: "#3b82f6",
            backgroundColor: "rgba(59,130,246,0.12)", fill: true,
            pointRadius: 0, borderWidth: 1.5, tension: 0.1
          }]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { maxTicksLimit: 8, color: "#8b9bb0" }, grid: { color: "#1e293b" } },
            y: { ticks: { color: "#8b9bb0" }, grid: { color: "#1e293b" } }
          }
        }
      });
      if (st) st.textContent = labels.length + " 點 · 起始 " + num(data[0], 0) + " → 結束 " + num(data[data.length - 1], 2);
    } catch (e) {
      if (st) st.textContent = "無法載入權益曲線：" + (e.message || e);
    }
  }


  // Canonical name aliases (tolerate mixed spellings in call sites)
  var bothGatesOk = gatePass3y;
  var runnerSupports = runnerSupports;
  var describeRules = describeRules;
  var pctPts = pctPts;
  var approveControls = approveControls;
  var runnerFamilyForCatalog = function (familyId, row) {
    if (familyId && Object.prototype.hasOwnProperty.call(CATALOG_FAMILY_RUNNER, familyId)) {
      return CATALOG_FAMILY_RUNNER[familyId];
    }
    return familyOf(row || {});
  };

  function findRowById(sid) {
    if (catalog && catalog.strategies) {
      for (var i = 0; i < catalog.strategies.length; i++) {
        var rows = catalog.strategies[i].rows || [];
        for (var j = 0; j < rows.length; j++) {
          if (rows[j].strategy_id === sid) {
            return Object.assign({ _family_id: catalog.strategies[i].strategy_family_id }, rows[j]);
          }
        }
      }
    }
    return ((scores && scores.strategies) || []).find(function (r) { return r.strategy_id === sid; }) || {};
  }

  function bindRows() {



    document.querySelectorAll(".btn-fam-approve").forEach(function (btn) {
      if (btn.disabled) return;
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        var fam = btn.getAttribute("data-family") || "";
        openPinModal({
          title: "批准策略家族",
          confirmText: "確定批准家族「" + fam + "」？配置檔中該家族且 enabled 的槽位將可實盤開倉。",
          onSubmit: function (pin, resultEl) {
            resultEl.textContent = "處理中…";
            postControl("/control/approve_family", pin, { family: fam })
              .then(function (r) {
                resultEl.textContent = r.message || "已批准";
                refreshApprovedFamilies({ approved_families: r.approved_families });
                setTimeout(function () { closeModal(); refresh(true); }, 700);
              })
              .catch(function (e) {
                resultEl.textContent = "失敗：" + ((e && e.message) || e);
              });
          }
        });
      });
    });
    document.querySelectorAll(".btn-fam-revoke").forEach(function (btn) {
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        var fam = btn.getAttribute("data-family") || "";
        openPinModal({
          title: "撤銷策略家族",
          confirmText: "確定撤銷家族「" + fam + "」？將停止開新倉；既有持倉仍依止損／出場管理。",
          onSubmit: function (pin, resultEl) {
            resultEl.textContent = "處理中…";
            postControl("/control/revoke_family", pin, { family: fam })
              .then(function (r) {
                resultEl.textContent = r.message || "已撤銷";
                refreshApprovedFamilies({ approved_families: r.approved_families });
                setTimeout(function () { closeModal(); refresh(true); }, 700);
              })
              .catch(function (e) {
                resultEl.textContent = "失敗：" + ((e && e.message) || e);
              });
          }
        });
      });
    });



    // Family card expand/collapse — was missing (cards did nothing on click)
    document.querySelectorAll(".ssc-head[data-toggle-key]").forEach(function (head) {
      head.onclick = function (ev) {
        if (ev.target.closest("button, a, input, label")) return;
        var key = head.getAttribute("data-toggle-key");
        if (!key) return;
        expandedKeys[key] = !expandedKeys[key];
        refresh(true);
      };
    });
    var filt = $("filterPassOnly");
    if (filt) {
      filt.onchange = function () {
        filterPassOnly = !!filt.checked;
        refresh(true);
      };
    }
    document.querySelectorAll("tr.score-row").forEach(function (tr) {
      tr.onclick = function (ev) {
        if (ev.target.closest("button")) return;
        var sid = tr.getAttribute("data-sid");
        var exp = $("exp-" + sid);
        if (!exp) return;
        var open = !exp.classList.contains("hidden");
        document.querySelectorAll("tr.expand-row").forEach(function (r) { r.classList.add("hidden"); });
        if (!open) {
          exp.classList.remove("hidden");
          loadEquity(sid);
        }
      };
    });
    document.querySelectorAll(".btn-approve-REMOVED").forEach(function (btn) {
      if (btn.disabled) return;
      btn.onclick = function (ev) {
        ev.stopPropagation();
        var sid = btn.getAttribute("data-sid");
        var notion = Number(btn.getAttribute("data-notional") || 1000);
        notion = Math.min(Math.max(notion, 10), MAX_NOTIONAL);
        openPinModal({
          title: "批准 · 上線待命",
          confirmText: "批准 " + sid + "？名義約 " + notion + " USDT（上限 " + MAX_NOTIONAL + "）。同衛星槽其他候選將改為只算訊號。",
          extraHtml: '<label class="pin-label">名義 USDT<input type="number" id="notionInput" class="pin-input" value="' + notion + '" min="10" max="' + MAX_NOTIONAL + '" /></label>',
          onSubmit: function (pin, resultEl) {
            var n = Number(($("notionInput") && $("notionInput").value) || notion);
            n = Math.min(Math.max(n, 10), MAX_NOTIONAL);
            resultEl.textContent = "處理中…";
            var row = findRowById(sid);
            var famId = btn.getAttribute("data-family") || row._family_id || "";
            postControl("/control/approve", pin, {
              strategy_id: sid,
              notional: n,
              passed_threshold: gatePass3y(row),
              gate_pass_3y: gatePass3y(row),
              supported_by_runner: runnerSupports(row, famId),
              family: runnerFamilyForCatalog(famId, row) || familyOf(row),
              slot: row.slot || null,
              satellite_slot: row.slot || null,
              symbol: row.symbol ? (String(row.symbol).indexOf("USDT") >= 0 ? row.symbol : row.symbol + "USDT") : null,
              timeframe: row.timeframe || null,
              params: row.params || null
            }).then(function (r) {
              resultEl.textContent = r.message || "已核准";
              setTimeout(function () { closeModal(); refresh(); }, 500);
            }).catch(function (e) {
              resultEl.textContent = "失敗：" + (e.message || e);
            });
          }
        });
      };
    });
    document.querySelectorAll(".btn-revoke").forEach(function (btn) {
      btn.onclick = function (ev) {
        ev.stopPropagation();
        var sid = btn.getAttribute("data-sid");
        openPinModal({
          title: "退回核准",
          confirmText: "確定退回 " + sid + "？若仍有持倉將續管止損／出場，不再新開倉。",
          onSubmit: function (pin, resultEl) {
            resultEl.textContent = "處理中…";
            postControl("/control/revoke", pin, { strategy_id: sid }).then(function (r) {
              resultEl.textContent = r.message || "已撤銷";
              setTimeout(function () { closeModal(); refresh(); }, 500);
            }).catch(function (e) {
              resultEl.textContent = "失敗：" + (e.message || e);
            });
          }
        });
      };
    });
  }

  async function refresh(keepUi) {
    var err = $("errBanner");
    var metaBox = $("metaBox");
    var main = $("main");
    err.classList.add("hidden");
    try {
      if (!keepUi || (!catalog && !scores)) {
        apiBase = await resolveApiBase();
      try {
        if (apiBase) {
          var stFam = await getJSON(apiBase + "/status");
          refreshApprovedFamilies(stFam || {});
        }
      } catch (eFam) { /* ignore */ }
        await loadApproved();
        scores = await getJSON(SCORES_URL);
        catalog = null;
        try { catalog = await getJSON(CATALOG_URL); } catch (e) { catalog = null; }
      }
      var groups = groupsFromCatalog(catalog) || groupsFromScores(scores);
      var srcHint = catalog ? "catalog.json（家族×幣別）" : "scores.json（依策略分組）";
      var meta = (catalog && catalog.meta) || (scores && scores.meta) || {};
      if (!keepUi) metaBox.innerHTML = renderMeta(meta, srcHint);
      main.innerHTML = renderGroups(groups);
      main.classList.remove("loading");
      metaBox.classList.remove("loading");
      bindRows();
      $("lastUpdated").textContent = "最後更新：" + new Date().toLocaleString("zh-TW", { timeZone: "Asia/Taipei" });
    } catch (e) {
      err.textContent = "載入失敗：" + (e.message || e) + "（分析師 scores 若尚未就緒會顯示此狀態）";
      err.classList.remove("hidden");
      metaBox.innerHTML = '<div class="empty-state">等待分析師提供 scores.json／catalog.json…</div>';
      main.innerHTML = "";
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("btnRefresh").onclick = refresh;
    var backdrop = document.querySelector("#pinModal .modal-backdrop");
    if (backdrop) backdrop.onclick = closeModal;
    if ($("pinModalClose")) $("pinModalClose").onclick = closeModal;
    refresh();
  });
})();
