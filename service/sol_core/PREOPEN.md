# PREOPEN — SOL 核心槽預檢

> 產生：**2026-09-24 15:02:52 CST**（Asia/Taipei）  
> 策略：`donchian20_atr_btcRegime__SOL__1d`｜名義 **1500 USDT**（約組合 30%）  
> 資料：Binance 公開日線，**僅已收盤**（最後一根 open=`2026-09-23 00:00:00+00:00`，UTC 日期 `2026-09-23`）  
> 機器輸出：`preopen.json`（與 `reference/sol_core_signal.py` 同跑）

## 結論

**Donchian 狀態已為持有（或價仍高於下軌的延續態）且無 0→1 邊沿：不可立即進；需等訊號歸零（收盤跌破下軌或 regime 關）後，再等突破上軌**

代碼結論：`wait_signal_reset_then_breakout`

| 項目 | 狀態 |
|------|------|
| 可立即進？ | **否** |
| 等突破？ | 單等突破不夠：目前 Donchian 狀態已是 1，無 0→1 邊沿 |
| regime？ | **開啟**（BTC 收盤 > SMA200） |
| reset？ | **需要**：等同回測「無 rising edge 不進場」；須等訊號歸零後再突破 |

## SOL 水準（已收盤）

| 欄位 | 數值 |
|------|------|
| 收盤價 | **114.99** |
| Donchian20 上軌（前 20 根 High.max，已 shift） | **119.99** |
| Donchian20 下軌（前 20 根 Low.min，已 shift） | **95.82** |
| ATR14（**回測用法 = SMA of TR**，非 Wilder） | **5.485** |
| ATR14 Wilder（僅對照，**不用於下單**） | 5.362793 |
| 距上軌 | **4.348%**（**0.912×ATR**） |
| 是否已在上軌上方 | False |

## BTC Regime

| 欄位 | 數值 |
|------|------|
| 均線 | **SMA 200**（與 `lib.add_indicators` / `apply_btc_regime` 一致） |
| BTC 收盤 | **84397.6** |
| SMA200 | **70794.8201** |
| 差距 | **+19.21%** |
| regime_on | **True** |

## 訊號狀態機

| 欄位 | 值 |
|------|-----|
| donchian_state（破上進、破下出） | 1 |
| regime_filtered | 1 |
| 前一根 regime_filtered | 1 |
| rising edge 0→1 | False |
| needs_reset_before_entry | **True** |

### reset_below_hi／重新進場（對照回測）

回測 `run_long_only` **只在** `sig[i]==1 and sig[i-1]==0` 進場。  
目前 `sig=1` 且非邊沿 → **即使收盤再次上穿上軌也不會觸發新進場**（狀態已在 1）。  
實務等同「必須先 reset」：等到 `close < donch20_lo` 或 BTC regime 關閉使 filtered signal→0，之後再等突破上軌的 0→1。

（現價 **低於** 上軌，但 Donchian 持有態仍延續至破下軌——這是通道策略正常行為。）

## 若（假設）以本根收盤模型進場的預定單

> 僅供數量／止損示意；**本次結論是不進場**。

| 項目 | 數值 |
|------|------|
| quoteOrderQty | 1500.0 USDT |
| 模型進場價（收盤×(1+20bps)） | 115.21998 |
| 數量（raw） | 13.018575 SOL |
| 數量（LOT_SIZE step=0.001 下取整） | **13.018** SOL |
| 初始止損 `entry - 2×ATR` | **104.24998** |
| 移動止損 | 每日收盤後 `stop = max(stop, close - 3×ATR)` |

下一根若出現合法 0→1：止損改以 **實際成交均價** 重算 `fill*(1+0) - 2*ATR_at_signal_bar`（見 SPEC；回測用含成本 entry_px）。

## 檢查時點建議

每日 **UTC 00:05–00:15**（日K收盤後 5–15 分鐘）拉最新已收盤 klines 跑 `sol_core_signal.py`，再決策。

---
對照檔：`SPEC_SOL_CORE.md`｜`reference/sol_core_signal.py`｜`/workspace/strategy-crypto-s3-majors/code/lib.py`
