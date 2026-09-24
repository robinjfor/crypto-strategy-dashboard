# EXPECTATION_LOG — 上線前 14 天對照基準

> 產生：**2026-09-24 15:03:17 CST**  
> 策略：`donchian20_atr_btcRegime__SOL__1d`｜名義 1500 USDT  
> 機器基準：`expectation_baseline.json`

## 選擇偏誤（必讀）

本策略是 s3 研究 **946** 組參數×標的中挑出的 **1** 組（另有嚴格門檻過濾）。  
全樣本報酬極高，但近 3 年／5 年遠低於全期；OOS 4/6 中部分「勝 B&H」來自空倉避熊。  
**14 天觀測的目的是抓執行／規則偏差，不是驗證 800% 級期望。**

## 回測基準（全樣本已收盤日線，至 2026-09-23）

| 指標 | 數值 | 上線解讀 |
|------|------|----------|
| 樣本 | 2020-08-11 → 2026-09-23（~6.12y） | — |
| 交易數 | **24** | 約 **3.9 筆／年** |
| 勝率 | **50.0%** | 近半；勿期待連勝 |
| 平均持有 | **24.2 天**（中位 13.5；P25–P75 3.8–34.2） | 14 天窗內多半 **0 次完整圓滿交易** |
| 平均賺（勝筆） | **+206.6%**（中位 +26.4%） | 均值被早期暴漲拉高，**看中位** |
| 平均賠（負筆） | **-9.1%**（中位 -7.3%） | 停損／破軌為主 |
| 典型 14 天進場訊號數 | **~0.15** | 期望 **0 次**，偶爾 1 次 |
| 出場原因（回測） | stop_loss 14／signal_exit 9／eod_flat 1 | 實盤應對齊 |
| 成本假設 | 單邊 **20 bps** | 記錄實際滑點 vs 此值 |

## 14 天每日應記什麼

即使無訊號，每日 UTC 收盤後仍記一行「心跳」：

1. `bar_open_time_utc`、決策時間（台北）
2. SOL 收盤、上下軌、ATR14_SMA
3. BTC 收盤、SMA200、`regime_on`
4. `donchian_state`、`regime_filtered`、是否 `rising_edge`
5. 若有 SIGNAL／FILLED／EXIT：預期價、成交價、滑點 bps、數量、止損更新軌跡、出場原因
6. **同日回測重播動作**（用同一套閉環規則）與差異說明

欄位清單見 `expectation_baseline.json` → `fields_to_log_per_event`。

## 建議記錄模板（JSONL 一行一事件）

```json
{"event_type":"HEARTBEAT","bar_open_time_utc":"2026-09-23T00:00:00+00:00","decision_ts_taipei":"2026-09-24 15:03:17 CST","sol_close":114.99,"donch20_hi":119.99,"donch20_lo":95.82,"atr14_sma":5.485,"btc_close":84397.6,"btc_sma200":70794.82,"regime_on":true,"donchian_state":1,"regime_filtered_signal":1,"rising_edge":false,"expected_entry_px_model":null,"actual_fill_px":null,"slippage_bps_vs_close":null,"qty":null,"stop_before":null,"stop_after":null,"exit_reason":null,"backtest_replay_same_bar_action":"HOLD_FLAT_NO_EDGE","diff_vs_backtest_note":"live flat; backtest also no new entry"}
```

## 通過 14 天觀測的最低標準（執行面）

- 無「同根重複下單」
- 無「regime 關閉仍新開多」
- 無「非 0→1 邊沿進場」
- 止損棘輪只上不下；破停損／破下軌／regime→0 的出場原因與回測重播一致
- 滑點中位不要穩定 >> 20bps（否則成本假設失效）

---
相關：`PREOPEN.md`｜`SPEC_SOL_CORE.md`
