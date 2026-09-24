# 策略回測統一目錄（catalog）

> 產生：**2026-09-24 16:11:49 CST**
> 3 年窗：2023-09-24 → 2026-09-24｜init=10000.0｜單邊 20bps
> 家族數：**9**｜列數：**42**｜3y 過關：**12**｜雙過：**8**

## 雙過清單（gate_pass_both）

| strategy_id | final | ret_3y | maxdd_3y | maxdd_full | score | status |
|---|---:|---:|---:|---:|---:|---|
| `donchian_breakout_atr__ARB__4h` | 55859.7 | 458.597 | -43.769 | -44.3745 | 80.5695 | 已淘汰 |
| `donchian55_s1.5_t2.5__FIL__1h` | 17180.23 | 71.8023 | -27.1672 | -34.2136 | 62.0374 | 已淘汰 |
| `donchian20_s1.5_t1.5__OP__4h` | 20567.55 | 105.6755 | -28.5979 | -37.4121 | 44.884 | 現役 |
| `donchian55_s2.0_t3.0__FET__4h` | 17798.05 | 77.9805 | -36.0232 | -40.4041 | 30.2312 | 衛星A候選（首選） |
| `donchian55_s2.0_t3.0_btcRegimeD__FET__4h` | 15329.1 | 53.291 | -31.6736 | -31.6736 | 26.5556 | 衛星A候選（備選） |
| `donchian55_s3.0_t4.0_btcRegimeD__FET__4h` | 12725.22 | 27.2522 | -41.5263 | -43.8765 | 16.391 | 候選 |
| `donchian55_s2.5_t3.5_btcRegimeD__FET__4h` | 12505.5 | 25.055 | -39.6434 | -39.6434 | 15.1363 | 候選 |
| `donchian55_breakout_atr__APT__1h` | 9158.4 | -8.416 | -44.6682 | -44.6682 | 10.8186 | 已淘汰 |

## 各家族最佳列（依 score，其次 ret_3y）

### Donchian 突破＋ATR 停損／移動停利（`donchian_atr`）— 21 列

收盤站上前 N 根 Donchian 高點做多；跌破 Donchian 低點、觸及 ATR 停損、移動停利或持倉達上限則出場。衛星族常用 Wilder ATR 與 reset_below_hi（出場後須先跌回通道高點下方才可再進）。對照族（breakout_atr）沿用 SMA ATR、無 reset。

- 最佳：`donchian_breakout_atr__ARB__4h`｜status=已淘汰｜ret_3y=458.597｜maxdd=-43.769｜oos=5/6｜score=80.5695｜gate3=True｜both=True

| status | id | ret_3y | maxdd | oos | gate3 | both | score | params_source | data_short |
|---|---|---:|---:|---|:---:|:---:|---:|---|:---:|
| 已淘汰 | `donchian_breakout_atr__ARB__4h` | 458.597 | -43.769 | 5/6 | Y | Y | 80.5695 | selected |  |
| 已淘汰 | `donchian55_s1.5_t2.5__FIL__1h` | 71.8023 | -27.1672 | 5/6 | Y | Y | 62.0374 | selected |  |
| 現役 | `donchian20_s1.5_t1.5__OP__4h` | 105.6755 | -28.5979 | 5/6 | Y | Y | 44.884 | selected |  |
| 候補 | `donchian55_s2.5_t3.5__ICP__4h` | 52.7535 | -39.7865 | 4/6 | Y | N | 41.7688 | selected |  |
| 現役 | `donchian20_s1.5_t1.5__DOT__4h` | 42.4987 | -28.1604 | 5/6 | Y | N | 39.2078 | selected |  |
| 衛星A候選（首選） | `donchian55_s2.0_t3.0__FET__4h` | 77.9805 | -36.0232 | 4/6 | Y | Y | 30.2312 | selected |  |
| 候選 | `donchian55_s2.0_t3.0__LSK__4h` | -6.0959 | -33.4947 | 4/6 | Y | N | 22.4745 | selected |  |
| 已淘汰 | `donchian55_breakout_atr__APT__1h` | -8.416 | -44.6682 | 5/6 | Y | Y | 10.8186 | selected |  |
| 對照 | `donchian_breakout_atr__NEAR__4h` | 1658.4537 | -34.0037 | 3/6 | N | N | 0.0 | selected |  |
| 對照 | `donchian_breakout_wide__NEAR__4h` | 786.4484 | -60.1375 | 3/6 | N | N | 0.0 | selected |  |
| 已淘汰 | `donchian_breakout_atr__SUI__4h` | 269.2993 | -56.2064 | 3/6 | N | N | 0.0 | selected |  |
| 候選 | `donchian20_s2.5_t3.5__CAKE__4h` | 157.7786 | -50.8004 | 2/6 | N | N | 0.0 | selected |  |
| 候選 | `donchian20_s2.0_t3.0__ADA__4h` | 157.5839 | -44.5717 | 3/6 | N | N | 0.0 | selected |  |
| 只算訊號待決 | `donchian55_s2.0_t3.0__FET__1h` | 108.0462 | -53.6757 | 4/6 | N | N | 0.0 | selected |  |
| 已淘汰 | `donchian_breakout_atr__AAVE__4h` | 48.3185 | -58.3134 | 2/6 | N | N | 0.0 | selected |  |
| 已淘汰 | `donchian55_breakout_atr__UNI__4h` | 33.0828 | -28.6598 | 2/6 | N | N | 0.0 | selected |  |
| 已淘汰 | `donchian55_s2.5_t3.5__INJ__1h` | -11.6764 | -49.1876 | 4/6 | N | N | 0.0 | selected |  |
| 已淘汰 | `donchian55_breakout_atr__WLD__4h` | -18.3815 | -48.4468 | 3/6 | N | N | 0.0 | selected |  |
| 已淘汰 | `donchian55_breakout_atr__ONDO__4h` | -29.5238 | -39.3561 | 3/6 | N | N | 0.0 | selected | Y |
| 已淘汰 | `donchian20_s2.0_t3.0__AVAX__4h` | -61.4918 | -78.2317 | 3/6 | N | N | 0.0 | default |  |
| 已淘汰 | `donchian20_s2.0_t3.0__STRK__4h` | -80.3402 | -85.3225 | 3/6 | N | N | 0.0 | default | Y |

### Donchian＋BTC SMA200 趨勢過濾（`donchian_btc_regime`）— 4 列

在 Donchian＋ATR 架構上，僅當 BTC 收盤高於 SMA200（日線或同週期）時允許做多。用於過濾山寨幣熊市空頭行情；槓桿版另計。

- 最佳：`donchian55_s2.0_t3.0_btcRegimeD__FET__4h`｜status=衛星A候選（備選）｜ret_3y=53.291｜maxdd=-31.6736｜oos=4/6｜score=26.5556｜gate3=True｜both=True

| status | id | ret_3y | maxdd | oos | gate3 | both | score | params_source | data_short |
|---|---|---:|---:|---|:---:|:---:|---:|---|:---:|
| 衛星A候選（備選） | `donchian55_s2.0_t3.0_btcRegimeD__FET__4h` | 53.291 | -31.6736 | 4/6 | Y | Y | 26.5556 | selected |  |
| 候選 | `donchian55_s3.0_t4.0_btcRegimeD__FET__4h` | 27.2522 | -41.5263 | 4/6 | Y | Y | 16.391 | selected |  |
| 候選 | `donchian55_s2.5_t3.5_btcRegimeD__FET__4h` | 25.055 | -39.6434 | 4/6 | Y | Y | 15.1363 | selected |  |
| 只算訊號待決 | `donchian20_atr_btcRegime__SOL__1d` | 435.3457 | -40.8998 | 4/6 | N | N | 0.0 | selected |  |

### Donchian＋BTC regime 槓桿版（`donchian_lev`）— 1 列

與 Donchian＋BTC regime 相同進出場，名義槓桿 >1。含年化 10% 資金費率與維持保證金強平檢查。

- 最佳：`donchian55_atr_btcRegime_lev1.5__SOL__1d`｜status=候選｜ret_3y=523.2773｜maxdd=-58.3528｜oos=4/6｜score=0.0｜gate3=False｜both=False

| status | id | ret_3y | maxdd | oos | gate3 | both | score | params_source | data_short |
|---|---|---:|---:|---|:---:|:---:|---:|---|:---:|
| 候選 | `donchian55_atr_btcRegime_lev1.5__SOL__1d` | 523.2773 | -58.3528 | 4/6 | N | N | 0.0 | selected |  |

### EMA 交叉＋ATR（可加 BTC regime）（`ema_cross_atr`）— 5 列

快線站上慢線做多，跌破出場；搭配 ATR 停損／移動停利。部分變體僅在 BTC＞SMA200 時允許持倉。

- 最佳：`ema12_26_atr_btcRegime__SOL__1d`｜status=候選｜ret_3y=546.9384｜maxdd=-37.8425｜oos=4/6｜score=52.9573｜gate3=True｜both=False

| status | id | ret_3y | maxdd | oos | gate3 | both | score | params_source | data_short |
|---|---|---:|---:|---|:---:|:---:|---:|---|:---:|
| 候選 | `ema12_26_atr_btcRegime__SOL__1d` | 546.9384 | -37.8425 | 4/6 | Y | N | 52.9573 | selected |  |
| 已淘汰 | `ema12_26_s2.5_t3.5__AR__4h` | 163.1262 | -61.9949 | 4/6 | N | N | 0.0 | selected |  |
| 已淘汰 | `ema9_21_s2.5_t3.5__HYPE__4h` | 5.0391 | -44.7363 | 2/6 | N | N | 0.0 | selected | Y |
| 已淘汰 | `ema12_26_s2.5_t3.5__ENA__4h` | -3.7896 | -85.1476 | 4/6 | N | N | 0.0 | selected | Y |
| 已淘汰 | `ema_cross_atr__RAY__1h` | -12.8467 | -90.5014 | 2/6 | N | N | 0.0 | selected |  |

### EMA 趨勢持有（寬停損）（`ema_trend_hold`）— 1 列

EMA 多頭排列時持有；使用很寬的 ATR 停損、幾乎不移動停利，偏向趨勢參與。歷史上用於 DOGE 等對照。

- 最佳：`ema_trend_hold__DOGE__4h`｜status=已淘汰｜ret_3y=180.0504｜maxdd=-72.357｜oos=3/6｜score=0.0｜gate3=False｜both=False

| status | id | ret_3y | maxdd | oos | gate3 | both | score | params_source | data_short |
|---|---|---:|---:|---|:---:|:---:|---:|---|:---:|
| 已淘汰 | `ema_trend_hold__DOGE__4h` | 180.0504 | -72.357 | 3/6 | N | N | 0.0 | selected |  |

### Supertrend（`supertrend`）— 1 列

經典 Supertrend 方向翻多做多、翻空出場；本目錄變體不做額外 ATR 硬停損。

- 最佳：`supertrend3__SOL__4h`｜status=候選｜ret_3y=280.0389｜maxdd=-65.7891｜oos=3/6｜score=0.0｜gate3=False｜both=False

| status | id | ret_3y | maxdd | oos | gate3 | both | score | params_source | data_short |
|---|---|---:|---:|---|:---:|:---:|---:|---|:---:|
| 候選 | `supertrend3__SOL__4h` | 280.0389 | -65.7891 | 3/6 | N | N | 0.0 | selected |  |

### 多幣絕對動能輪動（`momentum_rotation`）— 1 列

每週五對一籃子主流幣計算 lookback 報酬，取前 top_n 且報酬＞0（絕對動能）等權持有。對照為等權買入持有。

- 最佳：`rotation_lb120_top3_absMom__MULTI__1d`｜status=候選｜ret_3y=257.8846｜maxdd=-56.931｜oos=3/6｜score=0.0｜gate3=False｜both=False

| status | id | ret_3y | maxdd | oos | gate3 | both | score | params_source | data_short |
|---|---|---:|---:|---|:---:|:---:|---:|---|:---:|
| 候選 | `rotation_lb120_top3_absMom__MULTI__1d` | 257.8846 | -56.931 | 3/6 | N | N | 0.0 | selected |  |

### SMA200 政權持有（股票／ETF）（`sma_regime_hold`）— 7 列

收盤連續 confirm_days 日站上 SMA(period) 則約 99% 滿倉；連續同日數跌破則出場。來自 strategy-v1 Round3／strategy-v2；統一成本單邊 20bps。

- 最佳：`sma200_confirm5__SMH__1d`｜status=對照｜ret_3y=296.7842｜maxdd=-24.7156｜oos=1/6｜score=0.0｜gate3=False｜both=False

| status | id | ret_3y | maxdd | oos | gate3 | both | score | params_source | data_short |
|---|---|---:|---:|---|:---:|:---:|---:|---|:---:|
| 對照 | `sma200_confirm5__SMH__1d` | 296.7842 | -24.7156 | 1/6 | N | N | 0.0 | selected |  |
| 對照 | `sma200_confirm5__0050_TW__1d` | 270.5953 | -21.1839 | 1/6 | N | N | 0.0 | selected |  |
| 對照 | `sma200_confirm5__TQQQ__1d` | 214.6386 | -37.2644 | 2/6 | N | N | 0.0 | selected |  |
| 對照 | `sma200_confirm5__BTC__1d` | 135.8659 | -35.6543 | 1/6 | N | N | 0.0 | selected |  |
| 對照 | `sma200_confirm3__QQQ__1d` | 79.604 | -14.2294 | 1/6 | N | N | 0.0 | selected |  |
| 對照 | `sma200_confirm5__SPY__1d` | 51.9171 | -10.7839 | 1/6 | N | N | 0.0 | selected |  |
| 對照 | `sma200_confirm5__ETH__1d` | 19.2798 | -57.0174 | 2/6 | N | N | 0.0 | selected |  |

### 雙均線＋RSI／波動過濾（股票）（`dual_ma_rsi`）— 1 列

strategy-v1 Round2 選定：均線交叉進場，RSI 過濾＋ATR 移動停利，無固定停利。牛市常輸 B&H；作歷史對照，不作為現役。

- 最佳：`dual_ma_rsi_atr_trail__SPY__1d`｜status=對照｜ret_3y=5.7427｜maxdd=-2.6461｜oos=1/6｜score=0.0｜gate3=False｜both=False

| status | id | ret_3y | maxdd | oos | gate3 | both | score | params_source | data_short |
|---|---|---:|---:|---|:---:|:---:|---:|---|:---:|
| 對照 | `dual_ma_rsi_atr_trail__SPY__1d` | 5.7427 | -2.6461 | 1/6 | N | N | 0.0 | selected |  |

## 資料不足（歷史不滿 3 年／上市晚於窗起）

- `donchian55_breakout_atr__ONDO__4h` start=2025-04-20 data_short=true（門檻仍照算）
- `donchian20_s2.0_t3.0__STRK__4h` start=2024-02-23 data_short=true（門檻仍照算）
- `ema9_21_s2.5_t3.5__HYPE__4h` start=2025-11-08 data_short=true（門檻仍照算）
- `ema12_26_s2.5_t3.5__ENA__4h` start=2024-04-06 data_short=true（門檻仍照算）

## 與 scores.json 不一致處

重疊列之 **ret/maxdd/final 與 scores.json 一致**；**score** 因全目錄 gate_pass_3y 池擴大（含已淘汰 ARB/FIL/APT 等）重新 min-max 而不同：

- `donchian20_s1.5_t1.5__OP__4h` score: scores.json=54.3147 → catalog=44.884
- `donchian20_s1.5_t1.5__DOT__4h` score: scores.json=45.701 → catalog=39.2078
- `donchian55_s2.0_t3.0__FET__4h` score: scores.json=32.5451 → catalog=30.2312
- `donchian55_s2.0_t3.0_btcRegimeD__FET__4h` score: scores.json=27.6881 → catalog=26.5556
- `donchian55_s2.5_t3.5__ICP__4h` score: scores.json=46.6784 → catalog=41.7688
- `donchian55_s2.0_t3.0__LSK__4h` score: scores.json=23.704 → catalog=22.4745
- `donchian55_s3.0_t4.0_btcRegimeD__FET__4h` score: scores.json=14.2303 → catalog=16.391
- `donchian55_s2.5_t3.5_btcRegimeD__FET__4h` score: scores.json=12.6596 → catalog=15.1363
- `ema12_26_atr_btcRegime__SOL__1d` score: scores.json=53.4996 → catalog=52.9573

## 檔案

- `/workspace/strategy-unified-3y/catalog/catalog.json`
- `/workspace/strategy-unified-3y/catalog.json`（根目錄副本）
- `/workspace/strategy-unified-3y/catalog/CATALOG.md`
- `/workspace/strategy-unified-3y/catalog/code/`（`inventory.py`、`build_catalog.py`；引擎重用 `../code/`）

## 誠實聲明

未為過關而調參；參數僅取歷史選定值或家族預設（`params_source=default`：AVAX、STRK）。
已淘汰標的即使雙過門檻仍標「已淘汰」（衛星流程淘汰），供 Emily 批准時對照。