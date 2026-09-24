# SPEC_SOL_CORE — SOL 核心槽接單規格（Binance Spot Demo／先 dry-run）

> 產生時區：Asia/Taipei  
> 策略 ID：`donchian20_atr_btcRegime__SOL__1d`  
> 名義：**1500 USDT**（組合約 30%）  
> 對照實作（唯一真相來源）：
> - `/workspace/strategy-crypto-s3-majors/code/lib.py`：`signal_donchian`、`apply_btc_regime`、`run_long_only`、`atr_series`、`add_indicators`
> - `/workspace/strategy-crypto-s3-majors/sol_core/reference/sol_core_signal.py`
> - 參數鎖：`results.json` → full_pass / top_display  
>
> 目標帳戶：**Binance Spot Demo**；本階段 **dry-run 為預設**（寫狀態、不下真實單；Demo API 可另開開關）。  
> **禁止**修改 `/workspace/strategy-crypto-s2/` 狀態檔。

---

## 0. 鎖定參數

| 鍵 | 值 | 來源 |
|----|-----|------|
| symbol | `SOLUSDT` | — |
| interval | `1d`（UTC 日線） | — |
| donch_n | `20` | params.n |
| donch_hi / lo | `High.rolling(20).max().shift(1)` / `Low.rolling(20).min().shift(1)` | lib.add_indicators |
| atr_n | `14` | — |
| atr 算法 | **SMA(TrueRange, 14)**＝`tr.rolling(14).mean()` | lib.atr_series；**不是 Wilder** |
| atr_stop_mult | `2.0` | — |
| atr_trail_mult | `3.0` | — |
| use_trail | `true` | — |
| max_hold_bars | `10000` | 日線約 27 年，**實務可視為關閉** |
| btc_regime | BTC **收盤 > SMA(200)** | apply_btc_regime；均線為 **SMA 非 EMA** |
| 成本模型 | 單邊 **20 bps** | COST=0.002 |
| 槓桿 | `1.0`（現貨） | — |
| 下單名義 | `quoteOrderQty=1500` | 資金控管裁定 |

Binance `SOLUSDT` 過濾器（預檢當日快照，上線前再拉 `exchangeInfo`）：

- `LOT_SIZE.stepSize = 0.001`，`minQty = 0.001`
- `PRICE_FILTER.tickSize = 0.01`
- `NOTIONAL.minNotional = 5`

---

## 1. 時間框與檢查時點

1. **K 線**：Binance Spot `GET /api/v3/klines?symbol=SOLUSDT&interval=1d`（及 `BTCUSDT`）。  
2. **已收盤定義**：日 K 的 `open_time` 為某日 **UTC 00:00:00**；該根在 **open_time + 24h**（下一 UTC 午夜）才算收盤。  
   - 實作：若 `now_utc < last.open_time + 1d`，**丟棄最後一根**（與 `sol_core_signal.split_closed_1d` 一致）。  
3. **Runner 排程**：建議每日 **UTC 00:05–00:15** 執行一次（收盤後 5–15 分鐘，避開剛好午夜的延遲）。  
4. **盤中**：不開新倉、不因 intraday 突破進場；持倉可選做「觸價停損監看」（見 §5.3 模糊點），但 **訊號進／出以日收盤為準**（對齊回測）。

---

## 2. 指標計算（逐根、只用已收盤）

對 SOL／BTC 各自維護 OHLCV（UTC 日線）：

```
TR[i] = max(H[i]-L[i], |H[i]-C[i-1]|, |L[i]-C[i-1]|)
ATR[i] = mean(TR[i-13..i])          # 含 i，長度 14；不足則 NaN
donch_hi[i] = max(H[i-20..i-1])     # 不含當根；即 shift(1)
donch_lo[i] = min(L[i-20..i-1])
BTC_SMA200[i] = mean(BTC_C[i-199..i])
```

Regime：

```
regime_on[i] = (BTC_C[i] > BTC_SMA200[i])
```

---

## 3. Donchian 狀態＋Regime 過濾（進場／持有訊號）

### 3.1 原始 Donchian 狀態 `raw`（0/1）

與 `lib.signal_donchian` 完全一致：

```
state ← 0
for each bar i (chronological):
  if donch_hi/lo is NaN: hold[i] ← state; continue
  if state==0 and Close[i] > donch_hi[i]: state ← 1
  else if state==1 and Close[i] < donch_lo[i]: state ← 0
  hold[i] ← state
raw[i] = hold[i]
```

### 3.2 Regime 過濾後訊號 `sig`

```
sig[i] = raw[i] AND regime_on[i]    # 0/1 整數與
```

### 3.3 進場邊沿（唯一允許開倉條件）

回測：`shares==0 and sig[i]==1 and sig[i-1]==0`（且 ATR 有效）。

```
rising_edge[i] = (sig[i]==1 and sig[i-1]==0)
```

**禁止**：`sig` 已是 1 且非邊沿時開倉（即使收盤又高於上軌）。  
這對應 PREOPEN 的 **needs_reset_before_entry**：必須先讓 `sig→0`（破下軌或 regime 關），再等下一次 `0→1`。

### 3.4 進場執行（dry-run / Demo）

在確認 bar `i` 已收盤且 `rising_edge[i]`：

1. 狀態：`ARMED → SIGNAL → PENDING`  
2. 下單（Demo 或 dry-run 模擬）：**市價買**，優先 `quoteOrderQty=1500`（或等值）；數量按下式並 **LOT_SIZE 下取整**：
   ```
   qty = floor((1500 / fill_px_est) / 0.001) * 0.001
   ```
3. 回測模型成交價：`entry_px = Close[i] * (1 + 0.002)`  
   實盤：用實際 `avgFillPrice`；冪等鍵見 §7。  
4. 初始止損（在 fill 確認後）：
   ```
   stop = entry_ref - 2.0 * ATR[i]
   ```
   其中回測 `entry_ref = entry_px`（含 20bps）；實盤建議 `entry_ref = avgFillPrice`（見模糊點 A）。  
5. 狀態 → `FILLED`；記錄 `entry_bar_open_time`、`entry_i`、`stop`、`qty`。

---

## 4. 持倉管理與出場（對齊 `run_long_only`）

設持倉中，逐根已收盤 bar `i`（`i > entry_i`）：

### 4.1 停損（優先）

```
if Low[i] <= stop:
  exit_px_model = min(Open[i], stop) * (1 - 0.002)
  reason = "stop_loss"
  → 市價賣出全部 qty；狀態 EXITED
```

### 4.2 移動止損（若未觸發停損）

```
if use_trail and ATR[i] > 0:
  trail = Close[i] - 3.0 * ATR[i]
  if trail > stop:
    stop = trail          # 只上移，不下修
```

### 4.3 訊號出場

```
exit_sig = (sig[i] == 0)   # 含：破下軌 或 BTC regime 關閉
if exit_sig or (i - entry_i) >= 10000:
  exit_px_model = Close[i] * (1 - 0.002)
  reason = "signal_exit" or "max_hold"
  → 市價賣出；EXITED
```

**重要**：`regime_on` 由真變假時，`sig` 變 0 → **視為出場訊號**（回測如此）。不要「只禁止新開、卻繼續抱」。

### 4.4 出場後重新進場

`EXITED`／flat 後回到 `ARMED`。  
下一筆必須再次滿足 **新的** `rising_edge`（先 `sig=0` 至少一根，再 `0→1`）。  
不要在同一根既出場又進場（回測同根先處理出場再檢查進場，但出場後 `shares==0` 時若同根 `sig` 仍為 1 且前根為 0 才進——出場當根 `sig` 通常已是 0，故同根不會立刻再進）。

---

## 5. 狀態機

```
ARMED ──(rising_edge on closed bar)──► SIGNAL
SIGNAL ──(place order / dry-run sim)──► PENDING
PENDING ──(fill ok)──► FILLED
PENDING ──(reject/timeout)──► ARMED（記錄錯誤；該 bar 標記 already_acted）
FILLED ──(stop / signal_exit / max_hold)──► EXITED
EXITED ──(下一排程)──► ARMED

可附加：PAUSED（人工／風控）凍結 ARMED/SIGNAL/PENDING
```

冪等：每個 `entry_bar_open_time` 或 `exit_bar_open_time` 最多成功動作一次（見 §7）。

---

## 6. Dry-run 必須記錄

每次 runner tick 寫入（建議目錄 `sol_core/dry_run/`，**不要**寫入 s2）：

| 檔 | 內容 |
|----|------|
| `signal_last.json` | `sol_core_signal.compute_signal()` 完整輸出 |
| `state.json` | `{status, qty, entry, stop, entry_bar_ts, last_acted_bar_ts, ...}` |
| `orders.jsonl` | 每次擬下單／擬成交一行 |
| `settlement.jsonl` | 出場結算 |
| `expectation.jsonl` | 對齊 EXPECTATION_LOG 欄位（含 HEARTBEAT） |
| `alerts.json` | 異常（缺 K 線、regime 突變、重複鍵等） |

Dry-run **不得**呼叫會改變真實／Demo 倉位的 endpoint，除非顯式 `DRY_RUN=false` 且目標為 Demo。

---

## 7. 冪等與安全

1. `last_acted_entry_bar_ts`：同一 `open_time` 不得第二次買入。  
2. `last_acted_exit_bar_ts`：同一根不得第二次賣出。  
3. `FILLED` 時忽略新的 rising_edge。  
4. `news_light`／全域 pause（若接 dashboard）：非 GREEN 則不進 `SIGNAL`。  
5. 數量：一律 `stepSize=0.001` 下取整；名義不足 `minNotional` 則拒單並告警。

---

## 8. 虛擬碼（Python 風）

```python
def on_schedule_utc_0010():
    sol = closed_daily("SOLUSDT")
    btc = closed_daily("BTCUSDT")
    ind = add_indicators(sol, btc)          # donch20, atr_sma14, btc sma200
    raw = donchian_state(ind)               # §3.1
    sig = raw & (btc.close > btc.sma200)
    i = len(sig) - 1
    bar_ts = ind.index[i]

    snap = build_signal_json(...)           # == sol_core_signal output
    save("signal_last.json", snap)

    if state.status == "FILLED":
        manage_position(ind, sig, i, state) # §4
        return

    if state.status in ("ARMED", "EXITED"):
        state.status = "ARMED"
        if sig[i] == 1 and sig[i-1] == 0 and bar_ts != state.last_acted_entry_bar_ts:
            state.status = "SIGNAL"
            if not DRY_RUN:
                place_market_buy_quote(1500)
            else:
                simulate_fill(close=ind.close[i], cost_bps=20)
            state.last_acted_entry_bar_ts = bar_ts
            state.stop = state.entry - 2.0 * ind.atr[i]
            state.status = "FILLED"
```

---

## 9. 範例 JSON

### 9.1 訊號快照（結構同 `preopen.json`）

```json
{
  "strategy_id": "donchian20_atr_btcRegime__SOL__1d",
  "conclusion": "wait_signal_reset_then_breakout",
  "sol": {
    "close": 114.99,
    "donch20_hi": 119.99,
    "donch20_lo": 95.82,
    "atr14_sma": 5.485
  },
  "btc_regime": {
    "ma_type": "SMA",
    "ma_n": 200,
    "regime_on": true,
    "gap_pct": 19.2144
  },
  "signal": {
    "donchian_state": 1,
    "regime_filtered": 1,
    "rising_edge_0_to_1": false,
    "needs_reset_before_entry": true
  },
  "order_plan_if_enter_at_last_close": {
    "quote_usdt": 1500,
    "qty_floored_lot_step": 13.018,
    "initial_stop": 104.24998
  }
}
```

### 9.2 狀態

```json
{
  "slot": "core_sol",
  "status": "ARMED",
  "symbol": "SOLUSDT",
  "quote_usdt": 1500,
  "qty": null,
  "entry": null,
  "stop": null,
  "entry_bar_ts": null,
  "last_acted_entry_bar_ts": null,
  "last_acted_exit_bar_ts": null,
  "dry_run": true
}
```

### 9.3 成交事件（orders.jsonl）

```json
{
  "ts_taipei": "2026-09-25T08:10:00+08:00",
  "event": "ENTRY_FILL",
  "bar_open_time_utc": "2026-09-24T00:00:00+00:00",
  "side": "BUY",
  "quote_usdt": 1500,
  "qty": 12.5,
  "avg_fill_px": 120.01,
  "model_px_close_plus_20bps": 120.24,
  "slippage_bps_vs_close": 8.3,
  "atr": 5.5,
  "initial_stop": 109.01,
  "dry_run": true
}
```

---

## 10. 規則模糊點（需產品／Emily 拍板）

| ID | 議題 | 回測事實 | 建議預設（dry-run 採用） |
|----|------|----------|-------------------------|
| **A** | 初始止損用「含 20bps 的模型價」還是「成交均價」？ | `stop = entry_px - 2*ATR`，`entry_px=close*(1+COST)` | **實盤用 avgFillPrice − 2×ATR**；dry-run 可同時記兩套 |
| **B** | 收盤確認後何時下單？ | 同根收盤價成交（理想化） | **UTC 00:05–00:15 市價**；滑點記入 expectation |
| **C** | 盤中是否硬停損？ | 僅在 bar 內用 `Low<=stop` 判斷，成交價 `min(Open,stop)` | **Demo 可掛 STOP_LOSS_LIMIT**；須在 log 註明與回測差異 |
| **D** | ATR：用戶口頭「Wilder」vs 程式 SMA | 程式為 **SMA TR** | **必須 SMA**，Wilder 只作對照欄 |
| **E** | `max_hold=10000` 是否改成人性化上限（如 60 日）？ | 回測等同關閉 | **先保持 10000** 以對齊回測 |
| **F** | 上線時已 `sig=1` 無倉 | 不會邊沿進場 | **ARMED + wait reset**（當前 PREOPEN） |
| **G** | quote 1500 固定 vs 權益 30% 浮動 | 回測為權益×0.98 | **先固定 1500** |

---

## 11. 驗收 checklist（給網頁／GCP）

- [ ] 只用已收盤日線；未收盤根不參與計算  
- [ ] Donchian / ATR / SMA200 數值與 `sol_core_signal.py` 誤差 < 1e-6 相對量級  
- [ ] 僅 `rising_edge` 開倉；`needs_reset` 時拒絕  
- [ ] regime 關閉會平倉  
- [ ] 止損棘輪只上不下  
- [ ] 同 bar 冪等  
- [ ] dry-run 預設開啟；不碰 s2 狀態檔  

---

## 12. 檔案索引

| 路徑 | 用途 |
|------|------|
| `sol_core/SPEC_SOL_CORE.md` | 本規格 |
| `sol_core/PREOPEN.md` / `preopen.json` | 上線前預檢 |
| `sol_core/EXPECTATION_LOG.md` / `expectation_baseline.json` | 14 天對照 |
| `sol_core/reference/sol_core_signal.py` | 本地訊號監看 |
| `../code/lib.py` | 回測引擎真相來源 |
