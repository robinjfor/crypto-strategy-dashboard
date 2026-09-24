/* Shared exit/entry reason map — Traditional Chinese */
(function (root) {
  "use strict";
  const EXIT_REASON_MAP = {
    trail_stop: "移動止損",
    trail_stop_intrabar: "移動止損",
    EXITED_TRAIL_STOP: "移動止損",
    stop: "初始止損",
    stop_loss: "初始止損",
    initial_stop: "初始止損",
    max_hold: "到期出場",
    max_hold_bars: "到期出場",
    time_exit: "到期出場",
    EXITED_MAX_HOLD: "到期出場",
    donch_lo: "跌破下軌出場",
    lower_band: "跌破下軌出場",
    signal_exit: "跌破下軌出場",
    manual: "手動平倉",
    demo_sell: "Demo 平倉",
    demo_buy: "Demo 進場",
    demo_manual_or_bot: "Demo 進場",
    news_red: "紅燈風控出場",
    risk_exit: "紅燈風控出場",
    breakout_entry: "進場（突破上軌）",
    OPEN: "進場（突破上軌）",
    CLOSE: "平倉",
    entry_breakout: "進場（突破上軌）",
  };
  function reasonZh(code) {
    if (code == null || code === "") return "—";
    const k = String(code);
    if (EXIT_REASON_MAP[k]) return EXIT_REASON_MAP[k];
    const lower = k.toLowerCase();
    for (const [raw, zh] of Object.entries(EXIT_REASON_MAP)) {
      if (raw.toLowerCase() === lower) return zh;
    }
    if (lower.startsWith("time_exit") || lower.startsWith("max_hold")) return "到期出場";
    return k;
  }
  root.EXIT_REASON_MAP = EXIT_REASON_MAP;
  root.reasonZh = reasonZh;
})(typeof window !== "undefined" ? window : globalThis);
