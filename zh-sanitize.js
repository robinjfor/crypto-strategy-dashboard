/* Site-wide display filter (Robin rule): no program codes / slot ids, no UTC times, no English status words.
   Rewrites visible text nodes only (never inputs, scripts or data). Runs on every DOM change. */
(function () {
  "use strict";
  var TF = {"1h": "1 小時", "2h": "2 小時", "4h": "4 小時", "6h": "6 小時", "12h": "12 小時", "1d": "日線"};
  var KIND = {sat: "多", short: "空", ls: "多空"};
  function slotName(id) {
    if (/^u1(_|$)/.test(id)) return "U1 BTC/ETH 日線趨勢";
    if (id === "core_sol") return "SOL 核心";
    var m = /^(sat|short|ls)_([a-z0-9]+)(?:_[a-z]+)*_(\d+[hd])$/.exec(id);
    if (m) return m[2].toUpperCase() + " " + (TF[m[3]] || m[3]) + KIND[m[1]];
    return null;
  }
  var CODES = {};
  try {
    fetch("./config/strategy_codes.json").then(function (r) { return r.json(); }).then(function (d) {
      var f = d.families || {};
      Object.keys(f).forEach(function (k) { var rows = f[k].rows || {}; Object.keys(rows).forEach(function (sid) { if (rows[sid].code) CODES[sid] = rows[sid].code; }); });
      run(document.body);
    }).catch(function () {});
  } catch (e) {}
  var WORDS = [
    [/\bSIGNAL_ONLY\b/g, "僅訊號"], [/\bLIVE\b/g, "實盤"], [/\bDRY[-_ ]?RUN\b/gi, "演練"], [/\bdry-run\b/g, "演練"],
    [/\bmismatch(es)?\b/gi, "不一致"], [/\bnever_run\b/g, "尚未執行"], [/\bpaper_only\b/g, "僅模擬"],
    [/\bBUY\b/g, "買進"], [/\bSELL\b/g, "賣出"], [/\bLONG\b/g, "多"], [/\bSHORT\b/g, "空"],
    [/\bOPEN\b/g, "開倉"], [/\bCLOSE[D]?\b/g, "平倉"], [/\bFILLED\b/g, "已成交"], [/\bNEW\b/g, "未成交"], [/\bCANCELED\b/g, "已取消"],
    [/\bPASS\b/g, "通過"], [/\bFAIL\b/g, "未通過"], [/\bpending\b/gi, "待處理"], [/\bactive\b/g, "啟用中"], [/\bpaused\b/g, "暫停"],
    [/\bgate ok\b/gi, "檢查正常"], [/\bok\b/g, "正常"], [/\bOK\b/g, "正常"], [/\berror\b/gi, "錯誤"], [/\bunknown\b/gi, "未知"],
    [/\bentry\b/g, "進場"], [/\bexit\b/g, "出場"], [/\bstop(_loss)?\b/g, "停損"], [/\btrail(ing)?\b/g, "移動停利"],
    [/\bspot\b/g, "現貨"], [/\bfutures\b/g, "永續"], [/\bmanual\b/g, "人工單"], [/\bapi\b/g, "系統"], [/\bnone\b/g, "無"],
    [/\bTPT\b/g, "台北時間"], [/\bCST\b/g, "台北時間"],
    [/\bno_backtest_trade_at_entry_bar\b/g, "回測此時無交易"], [/\bno_paper_fill_at_bar\b/g, "模擬此時無成交"], [/\brebalance_band\b/g, "再平衡"],
    [/\bexit_to_cash\b/g, "出場轉現金"], [/\bexternal_close(_adopted)?\b/g, "外部平倉"], [/\bclosed_trades\b/g, "已結算交易"],
    [/\bentry_price\b/g, "進場價"], [/\bexit_price\b/g, "出場價"], [/\bexit_bar\b/g, "出場 K 棒"], [/\bentry_bar\b/g, "進場 K 棒"],
    [/\blive_slots\.order_mode\b/g, "下單模式"], [/\bscores\.json\b/g, "評分檔"], [/\bcatalog(\.json)?\b/g, "策略目錄"],
    [/\bmyTrades\b/g, "成交紀錄"], [/\brunner\b/gi, "自動交易程式"], [/\bbackfill\b/gi, "回補"], [/\bcloud\b/gi, "雲端"],
    [/\bflip\b/g, "反手"], [/\blive\b/g, "實盤"], [/\bPAPER\b/gi, "模擬"], [/\bCROSS\b/gi, "全倉"], [/\bISOLATED\b/gi, "逐倉"],
    [/\bfee\b/g, "手續費"], [/\bslip\b/g, "滑價"], [/\bbug\b/g, "程式錯誤"], [/\bfapi\b/g, "永續介面"],
    [/\bMaxDD\b/g, "最大回撤"], [/\bOOS\b/g, "樣本外"], [/\bCAGR\b/g, "年化報酬"], [/\bCloud Run\b/g, "雲端主機"],
    [/\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{6,40}\b/g, "（版本）"],
    [/\b[a-z]+(?:_[a-z0-9]+)+\b/g, function (m) { return /donch/.test(m) ? "唐奇安通道族群" : /bb_squeeze/.test(m) ? "布林擠壓族群" : /ema/.test(m) ? "均線交叉族群" : /short/.test(m) ? "空頭趨勢族群" : /u1|btc_eth/.test(m) ? "BTC/ETH 趨勢族群" : "系統欄位"; }]
  ];
  var ISO = /\b(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})\b/g;
  var HMUTC = /\b(\d{1,2}):(\d{2})\s*UTC\b/g;
  function fmt(d) {
    var p = new Intl.DateTimeFormat("zh-TW", {timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}).formatToParts(d);
    var g = function (t) { return (p.find(function (x) { return x.type === t; }) || {}).value; };
    return g("year") + "/" + g("month") + "/" + g("day") + " " + g("hour") + ":" + g("minute");
  }
  function fix(t) {
    var o = t;
    t = t.replace(ISO, function (m) { var d = new Date(m.replace(" ", "T")); return isNaN(d) ? m : fmt(d); });
    t = t.replace(HMUTC, function (m, h, mi) { return "台北 " + String((+h + 8) % 24).padStart(2, "0") + ":" + mi; });
    t = t.replace(/\bUTC\s*\+?0?8\b/g, "台北時間").replace(/\(UTC\)|（UTC）/g, "（台北時間）").replace(/\bUTC\b/g, "台北時間");
    t = t.replace(/\b[A-Za-z0-9.]+(?:_[A-Za-z0-9.]+)*__[A-Z0-9_]+__\w+\b/g, function (m) { return CODES[m] || "策略"; });
    t = t.replace(/\b(?:sat|short|ls|core|u1)_[a-z0-9_]+\b/g, function (m) { return slotName(m) || m; });
    t = t.replace(/\b(?:manual|u1-manualclose|u1)-[A-Za-z0-9-]+\b/g, "訂單");
    WORDS.forEach(function (w) { t = t.replace(w[0], w[1]); });
    return t === o ? null : t;
  }
  var SKIP = {SCRIPT: 1, STYLE: 1, TEXTAREA: 1, INPUT: 1, CODE: 0};
  function run(root) {
    if (!root) return;
    var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {acceptNode: function (n) {
      var p = n.parentNode; return p && SKIP[p.nodeName] ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT; }});
    var n, list = [];
    while ((n = w.nextNode())) list.push(n);
    list.forEach(function (n) { var f = fix(n.nodeValue); if (f !== null) n.nodeValue = f; });
  }
  var pending = false;
  var mo = new MutationObserver(function () {
    if (pending) return; pending = true;
    requestAnimationFrame(function () { pending = false; mo.disconnect(); run(document.body); mo.observe(document.body, {childList: true, subtree: true, characterData: true}); });
  });
  function start() { run(document.body); mo.observe(document.body, {childList: true, subtree: true, characterData: true}); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
  window.ZhSanitize = {fix: fix};
})();
