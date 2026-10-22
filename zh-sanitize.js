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
    [/\bTPT\b/g, "台北時間"], [/\bCST\b/g, "台北時間"]
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
    t = t.replace(/\b[a-z0-9]+(?:_[a-z0-9.]+)*__[A-Z0-9_]+__\w+\b/g, function (m) { return CODES[m] || "策略"; });
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
