/* Unified 3y scores — grouped by strategy card. Approve/revoke via Cloud API + PIN. */
(function () {
  "use strict";

  var SCORES_URL = "./data/unified-3y/scores.json";
  var CATALOG_URL = "./data/unified-3y/catalog.json";
  var CLOUD_CFG = "./data/cloud_api.json";
  var EQUITY_BASE = "./data/unified-3y/equity/";
  var MAX_NOTIONAL = 1500;
  var RUNNER_FAMILIES = { donchian: true, donchian_btc_regime: true };

  var scores = null;
  var catalog = null;
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

  function runnerSupports(row) {
    if (row.supported_by_runner === true) return true;
    if (row.supported_by_runner === false) return false;
    var fam = familyOf(row);
    return !!RUNNER_FAMILIES[fam] || fam.indexOf("donchian") === 0;
  }

  /** Emily 資金控管: lock unless BOTH 3y + full-period gates pass. */
  function failReasons(row) {
    return row.gate_fail_reasons || row.gate_fail_reasons || [];
  }
  function fpFailReasons(fp) {
    fp = fp || {};
    return fp.gate_fail_reasons || fp.gate_fail_reasons || [];
  }
  function bothGatesOk(row, meta) {
    if (row.gate_pass_both != null) return !!row.gate_pass_both;
    var fp = row.full_period || {};
    if (fp.gate_pass_full != null && row.gate_pass != null) {
      return !!row.gate_pass && !!fp.gate_pass_full;
    }
    // unclear meta → fall back to 3y gate_pass
    return !!row.gate_pass;
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
    } else if (fam === "rotation") {
      parts.push("輪動／配置類策略。");
    } else {
      parts.push("策略參數見下表；規則依 params 與 AUTOMATION_SPEC。");
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

  function approveControls(row) {
    var sid = row.strategy_id;
    var ap = approvedMap[sid];
    var so = signalOnlyMap[sid];
    if (ap && ap.mode !== "signal_only" && ap.approved !== false) {
      return '<span class="badge ok">已核准</span> ' +
        '<button type="button" class="btn-revoke" data-sid="' + esc(sid) + '">撤銷</button>';
    }
    var meta = (scores && scores.meta) || {};
    var ok = bothGatesOk(row, meta);
    var supported = runnerSupports(row);
    var locked = !ok || !supported;
    var title = !ok
      ? "未過雙門檻（3年＋全期），無法核准"
      : (!supported ? "雲端尚未支援此策略類型" : "核准上線待命");
    var notion = row.notional_usdt != null ? row.notional_usdt
      : (row.slot === "satellite_A" || /FET__4h/.test(sid) ? 1250 : 1000);
    var prefix = (so || (ap && ap.mode === "signal_only"))
      ? '<span class="badge warn">只算訊號</span> '
      : "";
    if (locked) {
      return prefix + '<button type="button" class="btn-approve" disabled title="' + esc(title) + '">審核通過</button>';
    }
    return prefix + '<button type="button" class="btn-approve" data-sid="' + esc(sid) +
      '" data-notional="' + esc(notion) + '" title="' + esc(title) + '">審核通過</button>';
  }

  function renderMeta(meta) {
    if (!meta) return "";
    var period = meta.period || {};
    var costs = meta.costs || {};
    var gate = meta.gate || {};
    var scoring = meta.scoring_method || {};
    return '<section class="section" id="sec-meta">' +
      '<div class="section-head"><h2>統一標準</h2><span class="hint">產生於 ' + esc(meta.generated_at || "—") + "</span></div>" +
      '<div class="meta-grid">' +
      "<div><span class=\"lbl\">期間</span> 近 3 年 · " + esc(period.start_utc || "—") + " → " + esc(period.end_utc || "—") + "</div>" +
      "<div><span class=\"lbl\">起始資金</span> " + num(meta.initial, 0) + " USDT</div>" +
      "<div><span class=\"lbl\">成本</span> 單邊 " + esc(costs.one_way_bps || 20) + " bps（fee " + esc(costs.fee_bps) + " + slip " + esc(costs.slippage_bps) + "）</div>" +
      "<div><span class=\"lbl\">門檻</span> 勝 B&amp;H · OOS≥" + esc((gate.oos_pass_min || gate.oos_pass_min)) + " · |MaxDD|≤" + esc((gate.maxdd_max_abs || gate.maxdd_max_abs)) + "% · 雙過＝3年＋全期</div>" +
      "<div><span class=\"lbl\">通過</span> 3年 " + esc(meta.n_gate_pass) + " · 雙過 " + esc(meta.n_gate_pass_both != null ? meta.n_gate_pass_both : "—") + " / " + esc(meta.n_strategies) + "</div>" +
      "<div class=\"meta-formula\"><span class=\"lbl\">計分</span> " + esc(JSON.stringify(scoring.weights || scoring)) + "</div>" +
      "</div></section>";
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
      g.best_score = g.rows.length ? (g.rows[0].score || 0) : 0;
      return g;
    }).sort(function (a, b) { return b.best_score - a.best_score; });
  }

  /** Tolerant catalog loader → same shape as groupsFromScores */
  function groupsFromCatalog(cat, scoreRows) {
    if (!cat || typeof cat !== "object") return null;
    var list = cat.strategies || cat.catalog || cat.groups || cat.items;
    if (!Array.isArray(list) || !list.length) return null;
    var byId = {};
    (scoreRows || []).forEach(function (r) { byId[r.strategy_id] = r; });
    var out = [];
    list.forEach(function (g) {
      var key = g.key || g.id || g.strategy_key || g.name || "";
      var rowsRaw = g.rows || g.variants || g.symbols || g.results || [];
      var rows = rowsRaw.map(function (r) {
        if (typeof r === "string") return byId[r] || { strategy_id: r };
        var sid = r.strategy_id || r.id || (key + "__" + (r.symbol || "") + "__" + (r.timeframe || r.tf || ""));
        return Object.assign({}, byId[sid] || {}, r, { strategy_id: sid });
      });
      if (!rows.length && key) {
        // pull matching scores rows
        rows = (scoreRows || []).filter(function (r) { return strategyKey(r.strategy_id) === key; });
      }
      out.push({
        key: key,
        params: g.params || (rows[0] && rows[0].params) || {},
        kind: g.kind || g.family || (rows[0] && rows[0].kind),
        rules_zh: g.rules_zh || g.description_zh || g.description || null,
        rows: rows
      });
    });
    out.forEach(function (g) {
      g.rows.sort(function (a, b) { return (b.score || 0) - (a.score || 0); });
      g.best_score = g.rows.length ? (g.rows[0].score || 0) : 0;
    });
    out.sort(function (a, b) { return b.best_score - a.best_score; });
    return out.length ? out : null;
  }

  function dualBadge(row) {
    if (row.gate_pass_both) return '<span class="badge ok">雙過</span>';
    var bits = [];
    bits.push(row.gate_pass ? "3年✓" : "3年✗");
    var fp = row.full_period || {};
    if (fp.gate_pass_full != null) bits.push(fp.gate_pass_full ? "全期✓" : "全期✗");
    return '<span class="badge bad">' + esc(bits.join(" · ")) + "</span>";
  }

  function expandHtml(r) {
    var fp = r.full_period || {};
    var reasons = (failReasons(r)).map(gateReasonZh).join("；");
    var fpReasons = (fpFailReasons(fp)).map(gateReasonZh).join("；");
    return '<div class="equity-wrap">' +
      '<div class="fp-grid">' +
      "<div><span class=\"lbl\">3 年</span> 報酬 " + pctPts(r.ret_3y) + " · MaxDD " + pctPts(r.maxdd) +
      " · OOS " + esc(r.oos_pass || ((r.oos_wins != null) ? (r.oos_wins + "/" + r.oos_total) : "—")) +
      " · 門檻 " + (r.gate_pass ? "過" : "未過") + "</div>" +
      "<div><span class=\"lbl\">全期</span> " + esc((fp.start || "").slice(0, 10)) + " → " + esc((fp.end || "").slice(0, 10)) +
      " · 報酬 " + pctPts(fp.ret) + " · B&amp;H " + pctPts(fp.bh_ret) +
      " · MaxDD " + pctPts(fp.maxdd) + " · OOS " + esc(fp.oos_pass || "—") +
      " · 門檻 " + (fp.gate_pass_full ? "過" : "未過") + "</div>" +
      (reasons ? '<div class="fail-reason">3年未過：' + esc(reasons) + '</div>' : '') +
      (fpReasons ? '<div class="fail-reason">全期未過：' + esc(fpReasons) + '</div>' : '') +
      "</div>" +
      '<canvas id="eq-' + esc(r.strategy_id) + '" height="180"></canvas>' +
      '<div class="equity-status" id="eqst-' + esc(r.strategy_id) + '">展開列以載入權益曲線…</div></div>';
  }

  function renderGroupCard(g) {
    var sample = g.rows[0] || { strategy_id: g.key, params: g.params };
    var rules = g.rules_zh
      ? (Array.isArray(g.rules_zh) ? g.rules_zh : [g.rules_zh])
      : describeRules(g.key, sample);
    var rulesHtml = rules.map(function (line) {
      return "<li>" + esc(line) + "</li>";
    }).join("");
    var p = g.params || sample.params || {};
    var paramBits = [];
    if (p.donch_n != null) paramBits.push("Donch " + p.donch_n);
    if (p.stop_m != null) paramBits.push("停損 " + p.stop_m + "×ATR");
    if (p.trail_m != null) paramBits.push("移動 " + p.trail_m + "×ATR");
    if (p.btc_regime) paramBits.push("BTC SMA200");
    if (p.reset_below_hi) paramBits.push("reset_below_hi");
    if (p.max_hold != null) paramBits.push("max_hold " + p.max_hold);

    var body = g.rows.map(function (r) {
      var fail = !bothGatesOk(r) && !r.gate_pass;
      var failBoth = !bothGatesOk(r);
      var reasons = (failReasons(r)).map(gateReasonZh).join("；");
      var oos = r.oos_pass || ((r.oos_wins != null) ? (r.oos_wins + "/" + r.oos_total) : "—");
      var fp = r.full_period || {};
      var sym = (r.symbol || "").replace(/USDT$/, "");
      return '<tr class="score-row' + (failBoth ? " fail" : "") + '" data-sid="' + esc(r.strategy_id) + '">' +
        "<td><strong>" + esc(sym) + "</strong> / " + esc(r.timeframe || "") +
        (failBoth && reasons ? '<div class="fail-reason">' + esc(reasons) + "</div>" : "") + "</td>" +
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
        "<td>" + approveControls(r) + "</td>" +
        "</tr>" +
        '<tr class="expand-row hidden" id="exp-' + esc(r.strategy_id) + '"><td colspan="12">' +
        expandHtml(r) + "</td></tr>";
    }).join("");

    return '<article class="strategy-score-card" data-key="' + esc(g.key) + '">' +
      '<div class="ssc-head">' +
      "<h3>" + esc(g.key) + "</h3>" +
      '<span class="badge muted">' + esc(g.kind || familyOf(sample)) + "</span>" +
      (paramBits.length ? '<span class="ssc-params">' + esc(paramBits.join(" · ")) + "</span>" : "") +
      "</div>" +
      '<div class="ssc-rules"><div class="lbl">規則說明</div><ul>' + rulesHtml + "</ul></div>" +
      '<div class="table-scroll"><table class="data scores">' +
      "<thead><tr>" +
      "<th>幣別／週期</th>" +
      '<th class="num">投入 10,000</th><th class="num">最終金額</th>' +
      '<th class="num">3 年報酬</th><th class="num">近 1 年</th><th class="num">B&amp;H</th>' +
      '<th class="num">MaxDD</th><th>OOS</th><th>雙過門檻</th>' +
      '<th class="num">分數</th><th>狀態</th><th>核准</th>' +
      "</tr></thead><tbody>" + body + "</tbody></table></div></article>";
  }

  function renderGroups(groups) {
    if (!groups || !groups.length) {
      return '<div class="empty-state">尚無策略資料</div>';
    }
    return '<section class="section" id="sec-scores">' +
      '<div class="section-head"><h2>策略評分（依策略分組）</h2>' +
      '<span class="hint">' + groups.length + " 組 · 點列展開權益曲線與全期數字</span></div>" +
      '<div class="strategy-score-list">' + groups.map(renderGroupCard).join("") + "</div></section>";
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

  function bindRows() {
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
    document.querySelectorAll(".btn-approve").forEach(function (btn) {
      if (btn.disabled) return;
      btn.onclick = function (ev) {
        ev.stopPropagation();
        var sid = btn.getAttribute("data-sid");
        var notion = Number(btn.getAttribute("data-notional") || 1000);
        notion = Math.min(Math.max(notion, 10), MAX_NOTIONAL);
        openPinModal({
          title: "審核通過 · 上線待命",
          confirmText: "核准 " + sid + "？名義約 " + notion + " USDT（上限 " + MAX_NOTIONAL + "）。同衛星槽其他候選將改為只算訊號。",
          extraHtml: '<label class="pin-label">名義 USDT<input type="number" id="notionInput" class="pin-input" value="' + notion + '" min="10" max="' + MAX_NOTIONAL + '" /></label>',
          onSubmit: function (pin, resultEl) {
            var n = Number(($("notionInput") && $("notionInput").value) || notion);
            n = Math.min(Math.max(n, 10), MAX_NOTIONAL);
            resultEl.textContent = "處理中…";
            var row = (scores.strategies || []).find(function (r) { return r.strategy_id === sid; }) || {};
            postControl("/control/approve", pin, {
              strategy_id: sid,
              notional: n,
              passed_threshold: bothGatesOk(row),
              gate_pass_both: bothGatesOk(row),
              supported_by_runner: runnerSupports(row),
              family: familyOf(row),
              slot: row.slot || null,
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
          title: "撤銷核准",
          confirmText: "確定撤銷 " + sid + "？若仍有持倉將續管止損／出場，不再新開倉。",
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

  async function refresh() {
    var err = $("errBanner");
    var metaBox = $("metaBox");
    var main = $("main");
    err.classList.add("hidden");
    try {
      apiBase = await resolveApiBase();
      await loadApproved();
      scores = await getJSON(SCORES_URL);
      catalog = null;
      try { catalog = await getJSON(CATALOG_URL); } catch (e) { catalog = null; }
      var groups = groupsFromCatalog(catalog, scores.strategies) || groupsFromScores(scores);
      var srcHint = catalog ? "catalog.json" : "scores.json（依策略分組）";
      metaBox.innerHTML = renderMeta(scores.meta) +
        '<p class="hint" style="margin:8px 0 0">資料來源：' + esc(srcHint) + "</p>";
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
