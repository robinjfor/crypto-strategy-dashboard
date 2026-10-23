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

  function blockMarket(st) {
    var m = st.market_indicators || {}, g = st.guards || {};
    var c = m.crowding || {}, fg = m.fear_greed || {}, oi = m.open_interest || {}, etf = m.etf_flows || {};
    var dot = {red: "🔴", yellow: "🟡", green: "🟢"}[c.light] || "⚪";
    var etfDays = (etf.days || []).filter(function (d) { return d[1] !== 0; }).slice(-5);
    var etfTxt = etfDays.map(function (d) { return d[0].slice(5).replace("-", "/") + " " + sn(d[1], 1); }).join(" · ");
    var cal = (g.calendar || {}).next || [];
    var gp = Object.keys(g.guard_paused_slots || {}).length;
    var dep = g.depeg || {};
    var guardLine = g.entry_blocked ? '<span class="tag bad">暫停新開倉</span> ' + esc(g.reason_zh) : '<span class="tag ok">正常</span> 目前沒有風控限制';
    var logs = (st.control_log || []).filter(function (e) { return e.actor === "guard"; }).slice(-3).reverse()
      .map(function (e) { return "<li>" + tpe(e.at) + "　" + esc(e.reason_zh || e.action) + "</li>"; }).join("");
    return '<section class="h-sec"><h2>市場指標<span class="dim" style="font-size:12px;font-weight:400">每小時更新 · ' + tpe(m.at) + "</span></h2>" +
      '<div class="h-kpis">' +
      kpi("擁擠度燈號", dot + " " + esc(c.light_zh || "—"), "BTC 7 日平均資金費 " + n(c.funding_7d_avg_pct, 4) + "%（過去一年第 " + n(c.funding_percentile, 0) + " 百分位）· 基差 " + sn(c.basis_pct, 3) + "%") +
      kpi("BTC 現貨 ETF 資金流", sn(etf.sum_5d, 1) + ' <span style="font-size:13px">百萬美元／近 5 日</span>', etfTxt || "—") +
      kpi("恐懼貪婪指數", (fg.value == null ? "—" : fg.value) + " " + esc(fg.label_zh || ""), "一週前 " + (fg.week_ago == null ? "—" : fg.week_ago)) +
      kpi("BTC 永續未平倉量", n((oi.btc_usd || 0) / 1e9, 2) + ' <span style="font-size:13px">十億美元</span>', "7 日 " + sn(oi.chg_7d_pct, 1) + "% · 30 日 " + sn(oi.chg_30d_pct, 1) + "%") +
      "</div>" +
      '<div style="margin-top:10px"><b>風控</b>　' + guardLine + "</div>" +
      '<ul class="h-note" style="margin:6px 0 0 18px">' +
      "<li>經濟數據：公布前 1 小時至後 4 小時暫停新開倉。下一個：" + (cal.length ? cal.slice(0, 2).map(function (e) { return esc(e.kind_zh) + " " + tpe(e.at); }).join("、") : "—") +
      ((g.calendar || {}).calendar_stale ? "（⚠ 日曆需補充）" : "") + "</li>" +
      "<li>幣安下市／合約調整公告：涉及的幣種暫停開新倉並提醒，不會自動平倉。" + (gp ? "目前因公告暫停 " + gp + " 個策略。" : "目前無。") + "</li>" +
      "<li>穩定幣脫鉤（USDC/USDT 偏離超過 0.5%）：永續槓桿改為 1 倍並提醒。目前 " + n(((dep.usdc_usdt) || ((g.depeg || {}).usdc_usdt) || 1), 4) + (dep.active ? "，⚠ 已觸發" : "，正常") + "</li>" +
      "<li>極端波動（15 分鐘振幅超過 4 倍標準差）：暫停新開倉 2 小時。</li>" +
      "</ul>" + (logs ? '<div class="h-note">最近風控紀錄：<ul style="margin:2px 0 0 18px">' + logs + "</ul></div>" : "") +
      '<div class="h-note">燈號、ETF、恐懼貪婪、未平倉量只供參考，不會自動交易。</div></section>';
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
    var A = ab.A, B = ab.B, C = ab.C, D = ab.D, E = ab.E;
    var r = function (name, eq, note) { return "<tr><td>" + name + "</td><td>" + (eq == null ? "—" : n(eq)) + "</td><td>" + (eq == null ? "—" : sn(Number(eq) - 1000)) + '</td><td class="dim">' + note + "</td></tr>"; };
    return '<div class="h-sec" style="margin:0 0 10px;padding:12px 14px"><b>影子帳本比較</b>（僅模擬記帳，不下單，無核准按鈕；起始皆 1,000 USDC，10/21 起）' +
      '<table class="h-tbl" style="margin-top:6px"><tr><th>帳本</th><th>權益</th><th>損益</th><th>說明</th></tr>' +
      r("原版 U1（實盤）", sv.equity, "幣安回讀；10/21 已人工平倉，目前空手") +
      r("原版 U1 回測對照", a60 && a60.equity, a60 ? "雲端依分析師程式計算 " + a60.date : "尚無資料") +
      r("目標波動 0.40", sh.equity, sh.equity == null ? "雲端影子帳本建立中（每小時與 U1 同排程）" : (function () {
        if (!a40) return "雲端計算";
        var u = sh.units || {}, dB = (u.BTC / Number(a40.btc_units) - 1) * 100, dE = (u.ETH / Number(a40.eth_units) - 1) * 100;
        var ok = Math.abs(dB) <= 3 && Math.abs(dE) <= 3;
        return "雲端計算；對照分析師帳本（" + a40.date + " 權益 " + n(a40.equity) + "）：持倉數量差 BTC " + sn(dB, 1) + "%、ETH " + sn(dE, 1) + "% " + (ok ? "✓ 一致" : "✗ 不一致");
      })()) +
      r("A：70% 波動 0.40＋30% 全市場多空趨勢", A && A.equity, A ? "雲端計算 " + (A.date || "") : "10/23 起算，10/24 08:20 起每日更新") +
      r("B：A × 1.5 槓桿", B && B.equity, B ? "雲端計算 " + (B.date || "") : "10/23 起算，10/24 08:20 起每日更新") +
      r("C：80%／20% × 2.5 槓桿", C && C.equity, C ? "雲端計算 " + (C.date || "") : "10/23 起算，10/24 08:20 起每日更新") +
      r("D：波動 0.40 × 1.5，回撤 40% 全平", D && D.equity, D ? "雲端計算 " + (D.date || "") : "10/23 起算，10/24 08:20 起每日更新") +
      r("E：80%／20% × 1.75 槓桿", E && E.equity, E ? "雲端計算 " + (E.date || "") : "10/23 起算，10/24 08:20 起每日更新") +
      '</table><div class="h-note">A～E 與分析師帳本由雲端每日 08:20 依分析師原程式重算（最後更新 ' + esc(sd.synced_at ? tpe(sd.synced_at) : "尚未同步") + '）。回測參考：A（1 倍、70%）樣本外夏普 1.57、最大回撤 25%；B（1.5 倍、70%）夏普 1.23、最大回撤 31%（皆事後選參，非真正樣本外）。</div></div>';
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

  function blockPending(st) {
    var c = ((st || {}).demo_candidates || []).filter(function (x) { return x.approvable; }); if (!c.length) return "";
    return '<div class="h-sec"><h2>待審核（測試網）</h2><table class="h-tbl"><tr><th>策略</th><th>資金</th><th>雲端下單測試</th><th>逐筆對帳</th><th>狀態</th></tr>' +
      c.map(function (x) {
        return "<tr><td>" + esc(x.name) + '<div class="dim">' + esc(x.spec) + "；" + esc(x.decision) + (x.warning ? "；⚠ " + esc(x.warning) : "") + "</div></td><td>" + n(x.capital_usdt) + " USDT</td><td>" +
          (x.cloud_ordering_ok ? "✓ 通過" : "未完成") + "</td><td>" + (x.parity_pass ? "✓ 通過" : "未完成") + "</td><td>" +
          (x.approvable ? '<a class="btn" href="./backtest.html#pending">前往批准</a>' : "未啟用，" + esc(x.status)) + "</td></tr>";
      }).join("") + '</table><div class="h-note">兩項檢查都通過後才會出現批准按鈕；啟用後每日對帳，每週與回測差距超過 0.5% 會自動暫停該策略。</div></div>';
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
      var sab = ((st || {}).paper || {}).SHADOW_AB || {};
      sd.synced_at = sab.updated_at; sd.analyst = sab.tv040 || []; sd.ab = [];
      Object.keys(sab.ledgers || {}).forEach(function (k) { var L = sab.ledgers[k] || {}; if (L.last) { var o = Object.assign({}, L.last); o.book = k; sd.ab.push(o); } });
      main.className = "";
      main.innerHTML = blockFunds(st) + blockRunning(st) + blockMarket(st) + blockEval(ev, st, sd) + blockPending(st) + blockRetired(ev, approvedCodes(st, codes)) + blockChart(st) +
        '<div class="h-links">其他資訊：<a href="./account.html">帳戶明細（持倉、對帳、實盤 vs 回測、幣安掛單、幣安帳本、控制紀錄）</a><a href="./history.html">成交歷史</a><a href="./backtest.html">策略評分</a></div>';
      main.querySelectorAll("[data-tg]").forEach(function (b) { b.onclick = function () { b.parentNode.classList.toggle("open"); }; });
      drawChart(st);
      $("lastUpdated").textContent = "最後更新：" + new Date().toLocaleTimeString("zh-TW", {timeZone: "Asia/Taipei", hour12: false});
      $("errBanner").textContent = "";
    } catch (e) { $("errBanner").textContent = "載入失敗：" + (e && e.message || e); main.textContent = ""; }
  }
  document.addEventListener("DOMContentLoaded", function () { $("btnRefresh").onclick = load; setTimeout(load, 50); });
})();
