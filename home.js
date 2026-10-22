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

  function tpe(iso, withDate) {
    if (!iso) return "—";
    var d = new Date(iso); if (isNaN(d)) return "—";
    var p = new Intl.DateTimeFormat("zh-TW", {timeZone: "Asia/Taipei", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}).formatToParts(d);
    var g = function (t) { return (p.find(function (x) { return x.type === t; }) || {}).value; };
    return (withDate === false ? "" : g("month") + "/" + g("day") + " ") + g("hour") + ":" + g("minute");
  }
  function nextHourly(iso) {   // hourly job runs at :01 every hour
    var d = iso ? new Date(iso) : new Date(); var n = new Date(Math.max(d.getTime(), Date.now()));
    n.setUTCMinutes(1, 0, 0); if (n.getTime() <= Date.now()) n = new Date(n.getTime() + 3600e3);
    return tpe(n.toISOString());
  }
  function blockRunning(st) {
    var ul = st.u1_live || {}, u = ul.sleeve || {}, lr = ul.last_run || u.last_run || {};
    var fut = ((st.books || {}).usdc || {}).futures || {};
    var held = (fut.positions || []).filter(function (p) { return Number(p.qty || p.positionAmt || 0) !== 0; });
    var line;
    if (held.length) {
      line = held.map(function (p) {
        var coin = String(p.symbol || "").replace(/USDC$|USDT$/, "");
        return coin + " " + n(p.qty != null ? p.qty : p.positionAmt, 4) + " 顆，進場價 " + n(p.entry_price != null ? p.entry_price : p.entryPrice) +
          "，未實現損益 " + sn(p.unrealized_pnl != null ? p.unrealized_pnl : p.unRealizedProfit) + " USDC";
      }).join("；");
    } else line = "空手中，等待進場訊號";
    var paused = !!(st.paused_slots || {})[u.slot] || !!st.paused;
    if (paused) line = "已暫停開新倉；" + line;
    var mon = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Taipei", year: "numeric", month: "2-digit"}).format(new Date());
    var closed = (st.closed_trades || []).filter(function (t) {
      return t.code === "U1" && t.closed_at && new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Taipei", year: "numeric", month: "2-digit"}).format(new Date(t.closed_at)) === mon;
    });
    var mreal = closed.reduce(function (s, t) { return s + Number(t.pnl_usdt != null ? t.pnl_usdt : (t.pnl || 0)); }, 0);
    var gateOk = lr.gate === "ok";
    var rows = ((st.live_vs_backtest || {}).rows || []).filter(function (r) { return r.code === "U1"; });
    var parOk = rows.every(function (r) { return r.match !== false; });
    return '<section class="h-sec"><h2>運行中策略<a href="./account.html">帳戶明細 ›</a></h2>' +
      '<div class="h-item open"><div style="padding:12px 14px">' +
      '<div style="display:flex;justify-content:space-between;gap:8px;align-items:center"><b style="font-size:16px">U1｜BTC/ETH 日線趨勢</b>' +
      '<span class="tag ' + (paused ? "warn" : "ok") + '">' + (paused ? "已暫停" : "運行中") + "</span></div>" +
      '<div style="margin:8px 0;font-size:15px">' + line + "</div>" +
      '<div class="h-kpis">' +
      kpi("本月已實現損益", sn(mreal) + ' <span style="font-size:13px">USDC</span>', "本月交易 " + closed.length + " 筆") +
      kpi("檢查", (gateOk ? "對帳正常 ✓" : "對帳異常 ✗"), "上次檢查 " + tpe(lr.at) + " · 下次執行 " + nextHourly(lr.at)) +
      kpi("與回測比對", parOk ? "一致 ✓" : "不一致 ✗", "每日比對 " + tpe((st.parity || {}).last_run_at)) +
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

  function shadowHtml(v) {
    if (!v || v.error) return '<div class="h-note">影子帳本尚無資料' + (v && v.error ? "（讀取失敗）" : "") + "</div>";
    var u = v.units || {}, m = v.mark || {};
    var pos = ["BTC", "ETH"].filter(function (k) { return Number(u[k]) !== 0; }).map(function (k) { return k + " " + n(u[k], 4) + " 顆"; }).join("、") || "空手中，等待進場訊號";
    return '<div class="h-kpis" style="margin:6px 0 8px">' +
      kpi("影子帳本權益", n(v.equity) + ' <span style="font-size:13px">USDC</span>', "起始 " + n(v.start_equity) + " · " + tpe(v.started_at_utc) + " 開始") +
      kpi("持倉", '<span style="font-size:16px">' + pos + "</span>", "上次更新 " + tpe(v.updated_at_utc)) +
      kpi("開始至今損益", sn(v.pnl), sn(v.return_pct) + "%") + "</div>" +
      '<div class="h-note">只在雲端模擬記帳，不下任何單；與 U1 同一引擎、同一排程，目標波動 0.40、槓桿上限 3。</div>';
  }
  function parseCsv(t) {
    var L = String(t || "").trim().split(/\r?\n/); if (L.length < 2) return [];
    var h = L[0].split(","); return L.slice(1).map(function (l) { var v = l.split(","), o = {}; h.forEach(function (k, i) { o[k.trim()] = v[i]; }); return o; });
  }
  function lastBy(rows) { var o = {}; rows.forEach(function (r) { var b = r.book || r.name; if (b) o[b] = r; }); return o; }
  function shadowCompare(st, sd) {
    var sh = ((st || {}).paper || {}).U1_TV40 || {}, an = lastBy(sd.analyst || []), ab = lastBy(sd.ab || []);
    var sv = (((st.books || {}).usdc || {}).futures || {}).u1_sleeve || {};
    var a40 = an.shadow_tv040, a60 = an.orig_tv060;
    var diff = (a40 && sh.equity != null) ? sh.equity - Number(a40.equity) : null;
    var findAB = function (re) { var k = Object.keys(ab).find(function (x) { return re.test(x); }); return k ? ab[k] : null; };
    var A = findAB(/^A$|(^|_)A($|_)/i), B = findAB(/^B$|(^|_)B($|_)/i);
    var r = function (name, eq, note) { return "<tr><td>" + name + "</td><td>" + (eq == null ? "—" : n(eq)) + "</td><td>" + (eq == null ? "—" : sn(Number(eq) - 1000)) + '</td><td class="dim">' + note + "</td></tr>"; };
    return '<div class="h-sec" style="margin:0 0 10px;padding:12px 14px"><b>影子帳本比較</b>（僅模擬記帳，不下單，無核准按鈕；起始皆 1,000 USDC，10/21 起）' +
      '<table class="h-tbl" style="margin-top:6px"><tr><th>帳本</th><th>權益</th><th>損益</th><th>說明</th></tr>' +
      r("原版 U1（實盤）", sv.equity, "幣安回讀") +
      r("原版 U1 回測對照", a60 && a60.equity, a60 ? "分析師帳本 " + a60.date : "尚無資料") +
      r("目標波動 0.40", sh.equity, sh.equity == null ? "雲端影子帳本建立中（每小時與 U1 同排程）" :
        "雲端計算" + (diff == null ? "" : "；分析師帳本 " + n(a40.equity) + "（" + a40.date + "），差 " + sn(diff) + (Math.abs(diff) <= 2 ? " ✓ 一致" : " ✗ 不一致"))) +
      r("A：70% 波動 0.40＋30% 全市場多空趨勢", A && A.equity, A ? "分析師帳本 " + (A.date || "") : "10/23 08:20 起每日更新") +
      r("B：A × 1.5 槓桿", B && B.equity, B ? "分析師帳本 " + (B.date || "") : "10/23 08:20 起每日更新") +
      '</table><div class="h-note">A／B 需要全市場約 900 檔永續資料與分群計算，暫不在雲端計算；數字由分析師在研究主機每日 08:20 產生後同步到網站（' + esc(sd.synced_at ? tpe(sd.synced_at) : "尚未同步") + '）。回測參考：A（1 倍、70%）樣本外夏普 1.57、最大回撤 25%；B（1.5 倍、70%）夏普 1.23、最大回撤 31%（皆事後選參，非真正樣本外）。</div></div>';
  }
  function blockEval(ev, st, sd) {
    var sh = ((st || {}).paper || {}).U1_TV40;
    return '<section class="h-sec"><h2>評估中策略<a href="./backtest.html">策略評分 ›</a></h2>' + shadowCompare(st, sd || {}) + '<div class="h-list">' +
      (ev.evaluating || []).map(function (e) {
        var h = item(e, '<span class="tag">評估中</span>');
        if (e.code === "U1-tv0.40") h = h.replace('<div class="body">', '<div class="body">' + shadowHtml(sh)) + "";
        return h;
      }).join("") + "</div></section>";
  }

  function blockRetired(ev, appr) {
    return '<section class="h-sec"><h2>淘汰策略</h2><div class="h-list">' +
      (ev.retired || []).map(function (e) { return item(e, appr[e.code] ? '<span class="tag warn">建議淘汰，待 Robin 決定</span>' : '<span class="tag">未核准</span>'); }).join("") +
      '</div><div class="h-note">' + esc(ev.note) + "</div></section>";
  }

  function series(st) {
    var pts = (((st.asset_daily || {}).points) || []).slice();
    var ta = (st.total_assets || {}).total_usdt;
    var today = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Taipei"}).format(new Date());
    if (ta != null) { pts = pts.filter(function (p) { return p[0] !== today; }); pts.push([today, Number(ta)]); }
    return pts.filter(function (p) { return p[0] >= "2026-09-30"; });
  }
  function months(pts) {
    var by = {}, order = [];
    pts.forEach(function (p, i) {
      var m = p[0].slice(0, 7);
      if (m < "2026-10") return;
      if (!by[m]) { by[m] = {start: i ? pts[i - 1][1] : p[1], startDay: i ? pts[i - 1][0] : p[0]}; order.push(m); }
      by[m].end = p[1]; by[m].endDay = p[0];
    });
    return order.map(function (m) { var e = by[m]; e.m = m; e.chg = e.end - e.start; e.pct = e.start ? e.chg / e.start * 100 : null; return e; });
  }
  function blockChart(st) {
    var pts = series(st), ms = months(pts);
    var ad = st.asset_daily || {};
    var tbl = ms.map(function (r) { return "<tr><td>" + r.m.replace("-", " 年 ") + " 月</td><td>" + n(r.start) + "</td><td>" + n(r.end) + "</td><td>" + sn(r.chg) + "</td><td>" + sn(r.pct) + "%</td></tr>"; }).join("");
    return '<section class="h-sec"><h2>總資產走勢</h2>' + (pts.length ? '<div style="height:260px"><canvas id="monChart"></canvas></div>' +
      '<table class="h-tbl" style="margin-top:8px"><tr><th>月份</th><th>月初</th><th>月底</th><th>增減金額</th><th>增減%</th></tr>' + tbl + "</table>"
      : '<div class="empty-state">尚無資料</div>') +
      '<div class="h-note">每日點為台北時間日終總資產（USDT 計）；最後一點為即時值。10/1 起的歷史由幣安帳戶成交、資金費紀錄逆推，並以幣安收盤價估值' +
      (ad.built_at ? "（重建於 " + tpe(ad.built_at) + "）" : "（雲端重建中）") + "。月初＝上月最後一日日終。</div></section>";
  }

  function drawChart(st) {
    var c = $("monChart"); if (!c || !window.Chart) return;
    var pts = series(st), labels = pts.map(function (p) { return p[0].slice(5).replace("-", "/"); }), vals = pts.map(function (p) { return p[1]; });
    var isEnd = pts.map(function (p, i) { return i === pts.length - 1 || (pts[i + 1] && pts[i + 1][0].slice(0, 7) !== p[0].slice(0, 7)); });
    new Chart(c, {type: "line", data: {labels: labels, datasets: [{label: "總資產（USDT）", data: vals, borderColor: "#3b82f6", backgroundColor: "#3b82f633",
      tension: 0.2, pointRadius: isEnd.map(function (e) { return e ? 6 : 2; }), pointStyle: isEnd.map(function (e) { return e ? "rectRot" : "circle"; }),
      pointBackgroundColor: isEnd.map(function (e) { return e ? "#f59e0b" : "#3b82f6"; })}]},
      options: {maintainAspectRatio: false, plugins: {legend: {display: false}, tooltip: {callbacks: {label: function (x) { return n(x.parsed.y) + " USDT" + (isEnd[x.dataIndex] ? "（月底）" : ""); }}}},
        scales: {x: {ticks: {color: "#8b9bb0", maxTicksLimit: 12}}, y: {beginAtZero: false, grace: "5%", ticks: {color: "#8b9bb0", callback: function (v) { return Number(v).toLocaleString("zh-TW"); }}}}}});
  }

  async function load() {
    var main = $("main");
    if (window.TraderAuth && !window.TraderAuth.getToken()) { main.textContent = "請先登入"; return; }
    try {
      var base = await apiBase();
      var res = await Promise.all([j(base + "/status"), j("./config/strategy_eval.json"), j("./config/strategy_codes.json").catch(function () { return {}; })]);
      var st = res[0], ev = res[1], codes = res[2];
      var sd = {};
      try {
        var idx = await j("./data/shadow/index.json"); sd.synced_at = idx.synced_at;
        sd.analyst = parseCsv(await (await fetch("./data/shadow/tv040_analyst.csv", {cache: "no-store"})).text());
        var abf = (idx.files || []).filter(function (f) { return /^AB\/.*\.csv$/.test(f); });
        sd.ab = [];
        for (var i = 0; i < abf.length; i++) sd.ab = sd.ab.concat(parseCsv(await (await fetch("./data/shadow/" + abf[i], {cache: "no-store"})).text()));
      } catch (e) {}
      main.className = "";
      main.innerHTML = blockFunds(st) + blockRunning(st) + blockEval(ev, st, sd) + blockRetired(ev, approvedCodes(st, codes)) + blockChart(st) +
        '<div class="h-links">其他資訊：<a href="./account.html">帳戶明細（持倉、對帳、實盤 vs 回測、幣安掛單、幣安帳本、控制紀錄）</a><a href="./history.html">成交歷史</a><a href="./backtest.html">策略評分</a></div>';
      main.querySelectorAll("[data-tg]").forEach(function (b) { b.onclick = function () { b.parentNode.classList.toggle("open"); }; });
      drawChart(st);
      $("lastUpdated").textContent = "最後更新：" + new Date().toLocaleTimeString("zh-TW", {timeZone: "Asia/Taipei", hour12: false});
      $("errBanner").textContent = "";
    } catch (e) { $("errBanner").textContent = "載入失敗：" + (e && e.message || e); main.textContent = ""; }
  }
  document.addEventListener("DOMContentLoaded", function () { $("btnRefresh").onclick = load; setTimeout(load, 50); });
})();
