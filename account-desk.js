/* Account page: environment switch, NAV, strategy equity, unallocated, residual, alerts. Read only. */
(function () {
  "use strict";
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function n(v) {
    var x = Number(v);
    return isFinite(x) ? x.toLocaleString("zh-TW", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "缺值";
  }
  function age(iso) {
    var t = new Date(iso).getTime();
    if (!iso || !isFinite(t)) return { level: "missing", text: "缺值" };
    var minutes = (Date.now() - t) / 60000;
    if (minutes > 26 * 60) return { level: "stale", text: "資料過期" };
    if (minutes > 2 * 60) return { level: "yellow", text: "資料超過 2 小時" };
    return { level: "ok", text: "資料時間正常" };
  }
  function fig(label, value, formula, source, when) {
    return '<details class="h-kpi"><summary><div class="l">' + esc(label) + ' <span class="info-mark">ⓘ</span></div><div class="v">' + value + "</div></summary>"
      + '<div class="s">公式：' + esc(formula) + "<br>來源：" + esc(source || "來源缺失") + "<br>資料時間：" + esc(when || "缺值") + "</div></details>";
  }

  async function base() {
    var b = window.TraderAuth && TraderAuth.getApiBase && TraderAuth.getApiBase();
    if (b) return b;
    var cfg = await (await fetch("./data/cloud_api.json", { cache: "no-store" })).json();
    return String(cfg.base || "").replace(/\/$/, "");
  }

  function draw(host, st, bridge) {
    var cb = (st && st.capital_basis) || {};
    var when = (st && st.updated_at) || "缺值";
    var clock = age(st && st.updated_at);
    var strategies = (bridge && bridge.strategies) || [];
    var table = strategies.length
      ? "<table class=\"h-tbl\"><tr><th>策略</th><th>權益</th><th>資金基準</th></tr>" + strategies.map(function (s) {
          var label = s.strategy_capital_basis_label === "orchestrator_target" ? "編排目標" : "虛擬本金";
          return "<tr><td>" + esc(s.code) + "</td><td>" + (s.equity == null ? "缺值" : n(s.equity)) + "</td><td>" + label + " " + (s.strategy_capital_basis == null ? "缺值" : n(s.strategy_capital_basis)) + "</td></tr>";
        }).join("") + "<tr><td>未分配</td><td>" + (bridge.unallocated_cash && bridge.unallocated_cash.value != null ? n(bridge.unallocated_cash.value) : "缺值") + "</td><td>錢包餘額減已配置</td></tr></table>"
      : "<p>策略貢獻缺值</p>";
    var diff = bridge && bridge.bridge_diff_value;
    var diffWord = diff == null ? "缺值" : (Number(diff) > 0 ? "正差額 " : Number(diff) < 0 ? "負差額 " : "差額為零 ");
    var diffLine = diff == null ? "缺值" : diffWord + (Number(diff) > 0 ? "+" : "") + n(diff);
    var alerts = [];
    ((st && st.positions) || []).forEach(function (p) {
      if (p.liq_distance != null && Number(p.liq_distance) < 0.30) alerts.push(esc(p.raw_symbol || p.symbol) + " 強平距離低於 30%");
    });
    if (bridge && bridge.bridge_diff_red) alerts.push("資金橋差額超過 0.5 USDT");
    if (clock.level === "stale") alerts.push("資料過期");
    var banner = "";
    if (bridge && bridge.bridge_diff_red) banner += '<div class="ia-banner">資金橋接仍有差額。差額需保留追查，不自動抹平。</div>';
    if (clock.level === "stale") banner += '<div class="ia-banner bad">資料過期</div>';
    var paperNav = st && st.paper && st.paper.nav != null ? n(st.paper.nav) : "缺值";
    host.innerHTML = banner + '<section class="h-sec"><h2>帳戶明細</h2>'
      + '<div class="env-switch"><button type="button" class="on" data-env="demo">Demo</button><button type="button" data-env="paper">Paper</button><button type="button" data-env="real">Real</button></div>'
      + '<div data-panel="demo"><div class="h-kpis">'
      + fig("合約淨值", cb.nav == null ? "缺值" : n(cb.nav), "USDT-M＋USDC 錢包及未實現。不含現貨，不含 Paper。", "GET /status capital_basis.nav", when)
      + fig("目前回撤", cb.current_drawdown == null ? "缺值" : n(Number(cb.current_drawdown) * 100) + "%", "1 − 淨值／高點。與最大回撤分開。", "capital_basis.current_drawdown", when)
      + fig("未分配", bridge && bridge.unallocated_cash ? (bridge.unallocated_cash.value == null ? "缺值" : n(bridge.unallocated_cash.value)) : "缺值", bridge && bridge.unallocated_cash ? bridge.unallocated_cash.formula : "缺值", "GET /review/bridge", when)
      + fig("橋接差額", diff == null ? "缺值" : diffLine, "策略權益合計 + 未分配現金 + 差額 = 錢包淨值。差額不自動抹平。", "GET /review/bridge", when)
      + "</div>" + table + '<p class="' + (bridge && bridge.bridge_diff_red ? "neg" : "") + '">' + diffLine + "。差額不自動抹平。</p></div>"
      + '<div data-panel="paper" hidden><p>Paper 不計入 Demo 淨值。Paper 淨值 ' + paperNav + "。</p></div>"
      + '<div data-panel="real" hidden><p>尚未開通</p><p>沒有實盤帳戶，顯示的不是 0。</p></div>'
      + '<div class="h-note">資料時間 ' + esc(when) + " · " + esc(clock.text) + "</div>"
      + "<h2>警示</h2>" + (alerts.length ? "<ul>" + alerts.map(function (a) { return "<li>" + a + "</li>"; }).join("") + "</ul>" : "<p>目前沒有警示。</p>")
      + "</section>";
    host.querySelectorAll("[data-env]").forEach(function (b) {
      b.onclick = function () {
        var name = b.getAttribute("data-env");
        host.querySelectorAll("[data-env]").forEach(function (x) { x.classList.toggle("on", x === b); });
        host.querySelectorAll("[data-panel]").forEach(function (p) { p.hidden = p.getAttribute("data-panel") !== name; });
      };
    });
  }

  async function load() {
    var host = document.getElementById("accountDesk");
    if (!host) return;
    if (window.TraderAuth && !TraderAuth.getToken()) return;
    try {
      var root = await base();
      var st = await (await fetch(root + "/status", { cache: "no-store" })).json();
      var bridge = null;
      try { bridge = await (await fetch(root + "/review/bridge", { cache: "no-store" })).json(); } catch (e) { bridge = null; }
      draw(host, st, bridge);
    } catch (e) {
      host.innerHTML = '<p class="h-note">帳戶橋接缺值</p>';
    }
  }
  document.addEventListener("DOMContentLoaded", function () { setTimeout(load, 60); });
})();
