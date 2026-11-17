/* Trade history. Prefers GET /review/trades (cursor). Falls back to /status fills. Read only. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  };
  var num = function (v, d) {
    var x = Number(v);
    return isFinite(x) ? x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }) : "缺值";
  };
  var pnl = function (v) {
    if (window.TraderAuth && TraderAuth.pnlHtml) return TraderAuth.pnlHtml(v);
    return v == null || v === "" ? "缺值" : String(v);
  };
  var reason = function (c) {
    if (c == null || c === "") return "缺值";
    return typeof window.reasonZh === "function" ? window.reasonZh(c) : String(c);
  };
  var filter = "all";
  var cursor = "";
  var pages = [];
  var source = "缺值";
  var rows = [];
  var nextCursor = null;

  function kindOf(r) {
    if (r.liquidation || String(r.kind || "").toLowerCase() === "liquidation") return "liquidation";
    var k = String(r.kind || "").toUpperCase();
    if (k === "CLOSE" || k === "CLOSED") return "closed";
    if (k === "OPEN" || k === "FILL") return "fill";
    return String(r.kind || "fill").toLowerCase();
  }

  function visible(list) {
    if (filter === "real") return [];
    return list.filter(function (r) {
      var k = kindOf(r);
      if (filter === "closed") return k === "closed";
      if (filter === "fill") return k === "fill";
      if (filter === "liquidation") return k === "liquidation";
      return true;
    });
  }

  function render() {
    var shown = visible(rows);
    var body = filter === "real"
      ? '<tr><td colspan="10">尚未開通。沒有實盤成交，這裡不重用 Demo。</td></tr>'
      : (shown.length ? shown.map(function (r) {
          var k = kindOf(r);
          var label = k === "liquidation" ? "強平" : k === "closed" ? "已平倉" : k === "fill" ? "成交" : (r.kind || "缺值");
          var pnlCell = k === "fill" ? "缺值" : pnl(r.realized_pnl_usdt != null ? r.realized_pnl_usdt : r.pnl_usdt);
          return "<tr><td>" + esc(r.ts || r.time || "缺值") + "</td><td>" + esc(label) + "</td><td>"
            + esc((r.code ? r.code + " " : "") + (r.symbol || "缺值")) + "</td><td>" + esc(r.side || "缺值")
            + '</td><td class="num">' + num(r.qty, 4) + '</td><td class="num">' + num(r.price, 4)
            + '</td><td class="num">' + (r.fee == null ? "缺值" : num(r.fee, 4)) + "</td><td>" + pnlCell
            + "</td><td>" + esc(reason(r.reason || r.cause || r.kind)) + "</td><td>" + esc(r.source || source) + "</td></tr>";
        }).join("") : '<tr><td colspan="10" class="empty-row">尚無成交</td></tr>');
    var chips = [
      ["all", "全部"], ["demo", "Demo"], ["real", "Real"],
      ["closed", "已平倉"], ["fill", "成交"], ["liquidation", "強平"]
    ].map(function (c) {
      return '<button type="button" data-hist="' + c[0] + '"' + (filter === c[0] ? ' class="on"' : "") + ">" + c[1] + "</button>";
    }).join("");
    $("main").className = "";
    $("main").innerHTML = '<section class="section"><div class="section-head"><h2>成交歷史 <span class="info-mark" title="來源、公式、資料時間">ⓘ</span></h2>'
      + '<span class="hint">' + (filter === "real" ? "尚未開通" : shown.length + " 筆") + " · " + esc(source) + "</span></div>"
      + '<div class="h-note">公式：每一列是一筆 fill、已平倉或強平，三者的損益不要加在一起。來源：' + esc(source)
      + "。環境 demo。篩選在已載入的列上進行；伺服器沒有依幣種或方向過濾的參數。</div>"
      + '<div class="hist-filters">' + chips + "</div>"
      + '<div class="card"><div class="table-scroll"><table class="data"><thead><tr>'
      + "<th>時間</th><th>類型</th><th>標的</th><th>方向</th><th>數量</th><th>價格</th><th>手續費</th><th>已實現損益</th><th>原因</th><th>來源</th>"
      + "</tr></thead><tbody>" + body + "</tbody></table></div></div>"
      + '<div class="pager"><button type="button" id="histPrev"' + (pages.length ? "" : " disabled") + ">上一頁</button>"
      + '<button type="button" id="histNext"' + (nextCursor ? "" : " disabled") + ">下一頁</button>"
      + "<span>分頁游標 " + esc(nextCursor || "缺值") + "</span></div></section>";
  }

  async function apiBase() {
    var b = window.TraderAuth && TraderAuth.getApiBase && TraderAuth.getApiBase();
    if (b) return b;
    try {
      var cfg = await (await fetch("./data/cloud_api.json?t=" + Date.now(), { cache: "no-store" })).json();
      return String(cfg.base || "").replace(/\/$/, "");
    } catch (e) {
      return "";
    }
  }

  function fromReview(data) {
    return (data.trades || []).map(function (r) {
      return {
        ts: r.time, kind: r.kind, liquidation: r.liquidation, symbol: r.symbol, code: r.code || r.strategy_id || "",
        side: r.side, qty: r.qty, price: r.price, fee: r.fee != null ? r.fee : r.commission,
        realized_pnl_usdt: r.realized_pnl_usdt != null ? r.realized_pnl_usdt : r.pnl_usdt,
        reason: r.reason || r.cause, source: "GET /review/trades"
      };
    });
  }

  async function load(next) {
    if (window.TraderAuth && !TraderAuth.getToken()) {
      $("main").textContent = "請先登入";
      return;
    }
    try {
      var base = await apiBase();
      var usedReview = false;
      if (base) {
        var q = "/review/trades?limit=50" + (next ? "&cursor=" + encodeURIComponent(next) : "");
        var res = await fetch(base + q, { cache: "no-store" });
        if (res.ok) {
          var data = await res.json();
          if (data && data.trades) {
            rows = fromReview(data);
            nextCursor = data.next_cursor || null;
            source = "GET /review/trades · " + (data.updated_at || "缺值");
            usedReview = true;
          }
        }
      }
      if (!usedReview) {
        nextCursor = null;
        var st = null;
        if (base) {
          try { st = await (await fetch(base + "/status?t=" + Date.now(), { cache: "no-store" })).json(); } catch (e0) { st = null; }
        }
        if (st && window.buildAccountChanges) {
          rows = window.buildAccountChanges(st);
          source = "GET /status recent_fills · " + (st.updated_at || "缺值");
          var banner = $("cloudBanner");
          if (banner && st.ok === false) banner.classList.remove("hidden");
        } else {
          rows = [];
          source = "缺值";
        }
        if (!rows.length) {
          try {
            var arr = await (await fetch("./data/strategy-crypto-s2/settlement.json?t=" + Date.now(), { cache: "no-store" })).json();
            if (Array.isArray(arr) && arr.length) { rows = arr; source = "靜態快取 settlement.json"; }
          } catch (e1) {
            try {
              var text = await (await fetch("./state/settlement.jsonl?t=" + Date.now(), { cache: "no-store" })).text();
              rows = text.trim().split("\n").filter(Boolean).map(function (l) { return JSON.parse(l); });
              if (rows.length) source = "靜態快取 settlement.jsonl";
            } catch (e2) { rows = []; }
          }
        }
        if (!rows.length) {
          var fail = $("cloudBanner");
          if (fail) fail.classList.remove("hidden");
        }
      }
      render();
    } catch (e) {
      $("errBanner").textContent = "載入失敗：" + (e && e.message ? e.message : e);
      $("errBanner").classList.remove("hidden");
      $("main").textContent = "載入失敗";
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("main").addEventListener("click", function (ev) {
      var chip = ev.target.closest("[data-hist]");
      if (chip) {
        filter = chip.getAttribute("data-hist") || "all";
        render();
        return;
      }
      if (ev.target.id === "histNext" && nextCursor) {
        pages.push(cursor);
        cursor = nextCursor;
        load(cursor);
      }
      if (ev.target.id === "histPrev" && pages.length) {
        cursor = pages.pop();
        load(cursor);
      }
    });
    load("");
  });
})();
