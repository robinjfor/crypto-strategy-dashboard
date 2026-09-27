/* Shared account-change ledger (homepage 帳戶變動明細 + history.html).
   Single source: cloud /status → recent_fills (Binance myTrades, grouped per
   order) joined with closed_trades (reason / realized PnL by order_id).
   No symbol filters; newest first by real timestamp (mixed tz offsets). */
(function (root) {
  "use strict";
  function t(v) {
    if (v == null || v === "") return 0;
    var d = new Date(String(v));
    return isNaN(d.getTime()) ? 0 : d.getTime();
  }
  function fmtTaipei(ms) {
    if (!ms) return "—";
    var d = new Date(ms + 8 * 3600 * 1000);
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate()) + " " +
      p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" + p(d.getUTCSeconds());
  }
  function build(st) {
    st = st || {};
    var fills = Array.isArray(st.recent_fills) ? st.recent_fills : [];
    var closed = Array.isArray(st.closed_trades) ? st.closed_trades : [];
    var closedByOid = {};
    closed.forEach(function (c) { if (c && c.order_id != null) closedByOid[String(c.order_id)] = c; });
    var groups = {}, order = [];
    fills.forEach(function (f) {
      if (!f || !f.symbol) return;
      // Without orderId (older API), fills of one market order share symbol+side+second.
      var key = f.symbol + "|" + (f.orderId != null ? f.orderId : (f.side + "|" + String(f.time || "").slice(0, 19)));
      var g = groups[key];
      if (!g) {
        g = groups[key] = { symbol: f.symbol, code: f.code, side: f.side, orderId: f.orderId,
          qty: 0, quote: 0, fee: 0, feeAsset: f.commissionAsset, ms: 0 };
        order.push(key);
      }
      var q = Number(f.qty) || 0, px = Number(f.price) || 0;
      g.qty += q;
      g.quote += f.quoteQty != null ? Number(f.quoteQty) : q * px;
      g.fee += Number(f.commission) || 0;
      g.ms = Math.max(g.ms, t(f.time));
      if (!g.code && f.code) g.code = f.code;
    });
    var used = {};
    var rows = order.map(function (k) {
      var g = groups[k];
      var isBuy = String(g.side).toUpperCase() === "BUY";
      var c = g.orderId != null ? closedByOid[String(g.orderId)] : null;
      if (!c && !isBuy) {
        // Fallback join: same symbol, closed within 120 s of the sell fill
        for (var i = 0; i < closed.length; i++) {
          var cc = closed[i];
          if (!cc || cc.symbol !== g.symbol || cc._joined) continue;
          if (Math.abs(t(cc.closed_at) - g.ms) <= 120000) { c = cc; break; }
        }
      }
      if (c) { c._joined = true; if (c.order_id != null) used[String(c.order_id)] = true; }
      return {
        ms: g.ms, ts: fmtTaipei(g.ms), kind: isBuy ? "OPEN" : "CLOSE", symbol: g.symbol,
        code: g.code || (c && c.code) || "", side: isBuy ? "BUY" : "SELL", qty: g.qty,
        price: g.qty ? g.quote / g.qty : null, fee: g.fee || null, fee_asset: g.feeAsset,
        realized_pnl_usdt: c ? c.pnl_usdt : null,
        reason: c ? (c.reason || "CLOSE") : (isBuy ? "OPEN" : "CLOSE"),
        order_id: g.orderId, source: "cloud"
      };
    });
    closed.forEach(function (c) {
      if (!c) return;
      if (c._joined || (c.order_id != null && used[String(c.order_id)])) { delete c._joined; return; }
      var ms = t(c.closed_at || c.time);
      var short = String(c.side || "").toUpperCase() === "SHORT";
      rows.push({
        ms: ms, ts: fmtTaipei(ms), kind: "CLOSE", symbol: c.symbol, code: c.code || "",
        side: short ? "BUY" : "SELL", qty: c.qty, price: c.exit, fee: null,
        realized_pnl_usdt: c.pnl_usdt, reason: c.reason || "CLOSE", order_id: c.order_id, source: "cloud"
      });
    });
    rows.sort(function (a, b) { return (b.ms || 0) - (a.ms || 0); });
    return rows;
  }
  root.buildAccountChanges = build;
  root.fmtTaipeiMs = fmtTaipei;
})(typeof window !== "undefined" ? window : globalThis);
