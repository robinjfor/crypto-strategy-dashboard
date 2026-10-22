/* Homepage: 5 blocks only. Numbers from cloud GET /status (Binance readback); evaluations from config/strategy_eval.json. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]; }); }
  function n(v, d) { var x = Number(v); return isFinite(x) ? x.toLocaleString("zh-TW", {minimumFractionDigits: d == null ? 2 : d, maximumFractionDigits: d == null ? 2 : d}) : "—"; }
  function sn(v, d) { var x = Number(v); if (!isFinite(x)) return "—"; return '<span class="' + (x > 0 ? "pos" : x < 0 ? "neg" : "") + '">' + (x > 0 ? "+" : "") + n(x, d) + "</span>"; }
  function kpi(l, v, s) { return '<div class="h-kpi"><div class="l">' + l + '</div><div class="v">' + v + '</div>' + (s ? '<div class="s">' + s + '</div>' : "") + "</div>"; }
  async function j(url) { var r = await fetch(url, {cache: "no-store"}); if (!r.ok) throw new Error(url + " HTTP " + r.status); return r.json(); }
  async function apiBase() {
    var b = window.TraderAuth && window.TraderAuth.getApiBase && window.TraderAuth.getApiBase();
    if (b) return b;
    var c = await j("./data/cloud_api.json"); return String(c.base || "").replace(/\/$/, "");
  }

  function blockFunds(st) {
    var ta = st.total_assets || {}, books = st.books || {};
    var real = 0, unr = 0;
    ["usdt", "usdc"].forEach(function (q) {
      var b = books[q] || {};
      real += Number(b.realized_pnl || 0);
      unr += Number(b.unrealized_pnl || 0) + Number(((b.futures || {}).unrealized_pnl) || 0);
    });
    var f = ta.futures || {};
    return '<section class="h-sec"><h2>資金總覽<a href="./account.html">帳戶明細 ›</a></h2><div class="h-kpis">' +
      kpi("總資產（USDT 計）", n(ta.total_usdt), "現貨 " + n(ta.spot_usdt) + " · 永續 " + n(ta.futures_usdt) + (ta.complete ? "" : " · 不完整")) +
      kpi("已實現損益", sn(real), "USDT " + sn((books.usdt || {}).realized_pnl) + " · USDC " + sn((books.usdc || {}).realized_pnl)) +
      kpi("未實現損益", sn(unr), "永續錢包 USDT " + n(f.USDT) + " · USDC " + n(f.USDC)) +
      '</div><div class="h-note">來源：幣安 Demo 回讀（' + esc(ta.source || "binance") + '）。已實現損益為有幣安成交的已結算交易，未扣部分手續費。</div></section>';
  }

  function blockRunning(st) {
    var u = (st.u1_live || {}).sleeve || {}, lr = u.last_run || (st.u1_live || {}).last_run || {};
    var fut = ((st.books || {}).usdc || {}).futures || {}, sv = fut.u1_sleeve || {};
    var eq = Number(sv.equity != null ? sv.equity : u.cash), cap = Number(u.capital_usdt || 0);
    var units = u.units || {};
    var posTxt = (fut.positions || []).map(function (p) { return esc(p.symbol) + " " + n(p.qty, 4); }).join(" · ") ||
      ("BTC " + n(units.BTC, 4) + " · ETH " + n(units.ETH, 4) + "（空手）");
    var paused = !!(st.paused_slots || {})[u.slot] || !!st.paused;
    var p = st.parity || {};
    var rows = ((st.live_vs_backtest || {}).rows || []).filter(function (r) { return r.code === "U1"; });
    var u1ok = rows.length ? rows.every(function (r) { return r.match !== false; }) : null;
    var ptag = u1ok === null ? '<span class="tag">無 U1 比對</span>' : u1ok ? '<span class="tag ok">U1 一致（' + rows.length + ' 筆）</span>' : '<span class="tag bad">U1 不一致</span>';
    return '<section class="h-sec"><h2>運行中策略<a href="./account.html">對帳／實盤 vs 回測 ›</a></h2>' +
      '<div class="h-item open"><button type="button"><span class="code">U1</span><span class="nm">BTC/ETH USDC 1d 趨勢（' + esc(u.slot || "") + '）</span>' +
      '<span class="tag ' + (paused ? "warn" : "ok") + '">' + (paused ? "已暫停" : "運行中") + '</span></button><div class="body">' +
      '<div class="h-kpis">' + kpi("持倉", posTxt, "上次執行 " + esc(lr.at || "—") + " · gate " + esc(lr.gate || "—")) +
      kpi("損益（USDC）", sn(eq - cap), "權益 " + n(eq) + " / 本金 " + n(cap) + " · 手續費 " + n(u.fees) + " · 資金費 " + sn(u.funding, 4)) +
      kpi("Parity", ptag, "每日 " + esc(p.last_run_at || "—") + " · 全帳戶 " + esc(p.status || "—") + "（一致 " + (p.n_match == null ? "—" : p.n_match) + "／不一致 " + (p.n_mismatch == null ? "—" : p.n_mismatch) + "）") +
      "</div></div></div></section>";
  }

  function approvedCodes(st, codes) {
    var ids = (st.approved || []).map(function (a) { return typeof a === "string" ? a : (a.strategy_id || a.id); });
    var out = {}, fams = (codes && codes.families) || {};
    Object.keys(fams).forEach(function (f) {
      var rows = fams[f].rows || {};
      Object.keys(rows).forEach(function (sid) { if (ids.indexOf(sid) >= 0 && rows[sid].code) out[rows[sid].code] = true; });
    });
    return out;
  }

  function item(e, tag) {
    return '<div class="h-item"><button type="button" data-tg="1"><span class="car">▸</span><span class="code">' + esc(e.code) + '</span><span class="nm">' + esc(e.name) + "</span>" + (tag || "") + '</button><div class="body">' +
      "<ul>" + (e.metrics || []).map(function (m) { return "<li>" + esc(m) + "</li>"; }).join("") + "</ul>" +
      (e.detail ? "<div>" + esc(e.detail) + "</div>" : "") + '<div><b>結論：</b>' + esc(e.conclusion) + '</div><div class="h-note">出處：' + esc(e.src) + "</div></div></div>";
  }

  function blockEval(ev) {
    return '<section class="h-sec"><h2>評估中策略<a href="./backtest.html">策略評分 ›</a></h2><div class="h-list">' +
      (ev.evaluating || []).map(function (e) { return item(e, '<span class="tag">評估中</span>'); }).join("") + "</div></section>";
  }

  function blockRetired(ev, appr) {
    return '<section class="h-sec"><h2>淘汰策略</h2><div class="h-list">' +
      (ev.retired || []).map(function (e) { return item(e, appr[e.code] ? '<span class="tag warn">建議淘汰，待 Robin 決定</span>' : '<span class="tag">未核准</span>'); }).join("") +
      '</div><div class="h-note">' + esc(ev.note) + "</div></section>";
  }

  function blockChart(st) {
    var mon = st.asset_monthly || {}, keys = Object.keys(mon).filter(function (k) { return k >= "2026-10"; }).sort();
    var rows = keys.map(function (k, i) {
      var e = mon[k], prev = i ? mon[keys[i - 1]].last_total : e.first_total;
      return {m: k, end: e.last_total, chg: e.last_total - prev, base: i ? "上月底" : "本月首筆快照 " + e.first_at};
    });
    var tbl = rows.map(function (r) { return "<tr><td>" + r.m + "</td><td>" + n(r.end) + "</td><td>" + sn(r.chg) + "</td><td class=\"dim\">" + esc(r.base) + "</td></tr>"; }).join("");
    return '<section class="h-sec"><h2>每月總資產變化</h2>' + (rows.length ? '<div style="height:220px"><canvas id="monChart"></canvas></div>' +
      '<table class="h-tbl"><tr><th>月份</th><th>月底／最新總資產</th><th>變化</th><th>比較基準</th></tr>' + tbl + "</table>"
      : '<div class="empty-state">尚無快照（對帳每 15 分鐘記錄一次幣安總資產）</div>') +
      '<div class="h-note">當月為最新快照；2026-10 起算。</div></section>';
  }

  function drawChart(st) {
    var c = $("monChart"); if (!c || !window.Chart) return;
    var mon = st.asset_monthly || {}, keys = Object.keys(mon).filter(function (k) { return k >= "2026-10"; }).sort();
    var vals = keys.map(function (k, i) { return mon[k].last_total - (i ? mon[keys[i - 1]].last_total : mon[k].first_total); });
    new Chart(c, {type: "bar", data: {labels: keys, datasets: [{label: "月變化（USDT）", data: vals,
      backgroundColor: vals.map(function (v) { return v >= 0 ? "#22c55e99" : "#ef444499"; })}]},
      options: {maintainAspectRatio: false, plugins: {legend: {display: false}}, scales: {x: {ticks: {color: "#8b9bb0"}}, y: {ticks: {color: "#8b9bb0"}}}}});
  }

  async function load() {
    var main = $("main");
    if (window.TraderAuth && !window.TraderAuth.getToken()) { main.textContent = "請先登入"; return; }
    try {
      var base = await apiBase();
      var res = await Promise.all([j(base + "/status"), j("./config/strategy_eval.json"), j("./config/strategy_codes.json").catch(function () { return {}; })]);
      var st = res[0], ev = res[1], codes = res[2];
      main.className = "";
      main.innerHTML = blockFunds(st) + blockRunning(st) + blockEval(ev) + blockRetired(ev, approvedCodes(st, codes)) + blockChart(st) +
        '<div class="h-links">其他資訊：<a href="./account.html">帳戶明細（持倉、對帳、實盤 vs 回測、幣安掛單、幣安帳本、控制紀錄）</a><a href="./history.html">成交歷史</a><a href="./backtest.html">策略評分</a></div>';
      main.querySelectorAll("[data-tg]").forEach(function (b) { b.onclick = function () { b.parentNode.classList.toggle("open"); }; });
      drawChart(st);
      $("lastUpdated").textContent = "最後更新：" + new Date().toLocaleTimeString("zh-TW", {timeZone: "Asia/Taipei", hour12: false});
      $("errBanner").textContent = "";
    } catch (e) { $("errBanner").textContent = "載入失敗：" + (e && e.message || e); main.textContent = ""; }
  }
  document.addEventListener("DOMContentLoaded", function () { $("btnRefresh").onclick = load; setTimeout(load, 50); });
})();
