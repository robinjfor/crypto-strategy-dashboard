# 配置檔 `allocation.json` 說明（分析師）

分析師用這個檔案決定**實盤要跑哪些幣／週期／名義金額**。Emily 只批准**策略家族**；家族批准後，仍須在本檔把對應 slot 設為 `enabled: true` 才會下單。

## 檔案位置

- Repo：`config/allocation.json`
- 線上（GCS）：`gs://$GCS_BUCKET/trader/allocation.json`
- 推送後由 workflow `sync-allocation` 驗證並上傳；不合法則**整份拒絕**，保留上一份有效檔，並在 `/status.allocation_alert` 顯示錯誤。

## 頂層欄位

| 欄位 | 型別 | 說明 |
|------|------|------|
| `version` | number | schema 版本，目前為 `1` |
| `book_usdt` | number | **USDT 帳本**基準（預設／上限 **5000**）；與 USDC 完全分開。新資金用帳上 **USDC**，不要把 USDT 帳本灌到 10000 |
| `book_usdc` | number | **USDC 帳本**基準（預設 5000）；尚未交易時可省略（runner 仍當 5000） |
| `max_notional_per_order_usdt` | number | USDT 單筆上限（預設 1500） |
| `max_notional_per_order_usdc` | number | USDC 單筆上限（預設同 USDT 上限） |
| `updated_by` | string | 誰改的 |
| `updated_at` | string | ISO8601（建議 +08:00） |
| `slots` | array | 槽位列表 |

## `slots[]` 欄位

| 欄位 | 型別 | 必填 | 說明 |
|------|------|------|------|
| `slot` | string | ✓ | 唯一槽位 id，例如 `sat_op_4h` |
| `family` | string | ✓ | 策略家族 id（catalog），例如 `donchian_atr`、`donchian_btc_regime` |
| `strategy_id` | string | ✓ | 完整策略 id（含幣與週期） |
| `symbol` | string | ✓ | Binance 現貨對：`*USDT` 或 `*USDC`（兩帳本不可混用） |
| `timeframe` | string | ✓ | `1h` / `4h` / `1d` 等 |
| `quote_currency` | string | | `USDT` 或 `USDC`；省略則依 symbol 後綴推斷 |
| `notional_usdt` | number | ✓* | USDT 名義；USDT 槽必填（或用 `notional`） |
| `notional_usdc` | number | ✓* | USDC 名義；USDC 槽用這個（不要填進 `book_usdt` 加總） |
| `enabled` | boolean | ✓ | `true` 才可能實盤；`false` 僅候選／只算訊號 |
| `params` | object | ✓ | 執行參數（`donch_n`、`stop_atr_mult`、`trail_atr_mult`、`atr_mode`、`require_reset_below_hi`…） |
| `note` | string | | 備註 |

## 驗證規則（workflow + 交易 job）

1. `family` 須在雲端支援清單內（目前：`donchian_atr`、`donchian_btc_regime`）。
2. `params` 須可被 runner 執行（必要鍵齊全、數值合理）。
3. 每個 notional ≤ 對應 quote 的單筆上限（USDT→`max_notional_per_order_usdt`，USDC→`max_notional_per_order_usdc`）。
4. **僅 `enabled: true`** 的 notional **依 quote 分開加總**：USDT 槽合計 ≤ `book_usdt`（預設／硬頂 **5000**）；USDC 槽合計 ≤ `book_usdc`（預設／硬頂 **5000**）。兩本帳互不佔額度。
5. `symbol` 必須是 Binance 現貨 `*USDT` 或 `*USDC` 且存在；`quote_currency` 須與後綴一致。
6. 不得有重複 `symbol`（同一檔幣只能有一個 slot，避免衝突）。USDT 與 USDC 視為不同 symbol（例如 BTCUSDT ≠ BTCUSDC）。
7. `slot`、`strategy_id` 不可重複。

任一條失敗 → **整份檔案拒絕**。


## `params`（donchian_atr）

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| `donch_n` | number | — | Donchian 視窗（必填） |
| `stop_atr_mult` | number | — | 初始停損 ATR 倍數（必填） |
| `trail_atr_mult` | number | — | 移動停利 ATR 倍數（必填） |
| `max_hold_bars` | number | | 最長持有 bar 數 |
| `require_reset_below_hi` | boolean | `false` | `true`＝出場後須先收盤跌破上軌才重新允許進場；`false`＝不需重置（與 ARB 回測同款） |
| `atr_mode` | `"wilder"` \| `"sma"` | `"wilder"` | ATR 計算：`wilder`＝Wilder ATR14（既有預設）；`sma`＝TR 的 SMA14（與 SOL core / 部分 3y 回測相同）。影響初始停損、移動停利、距離（ATR）顯示 |
| `btc_regime` | boolean | `false` | 是否套用 BTC 趨勢過濾（donchian_atr 通常為 false） |

其他 `atr_mode` 值會讓整份 allocation **驗證失敗**。

## 與家族批准的關係

| 條件 | 行為 |
|------|------|
| 家族**未**批准 | 即使 `enabled: true` 也只算訊號，不開新倉 |
| 家族已批准且 `enabled: true` | 可實盤開倉／管理 |
| 家族已批准但 `enabled: false` | 不上線（候選） |
| 家族被撤銷 | 停止開新倉；已持有倉位仍依止損／出場規則管理 |

預設：`donchian_atr` 已批准；`donchian_btc_regime` 未批准。

## 分析師操作步驟

1. 編輯 `config/allocation.json`（改 `enabled` / `notional_usdt` / 新增 slot）。
2. `git commit` + `git push` 到 `main`。
3. 到 GitHub Actions 看 `sync-allocation` 是否成功。
4. 打 `/status`，確認：
   - `allocation.updated_at` 已更新
   - `allocation_alert` 為 `null`
   - `live_slots` 只含預期幣種
5. 首頁「目前配置」應自動反映 notional／book_usdt（USDT 上限 5000）。

## 範例：只讓 OP、DOT 上線（種子狀態）

OP、DOT：`enabled: true`、各 1000 USDT；FET／SOL：`enabled: false`。  
不要把 FET 4h 設成 `enabled: true`，除非分析師明確決定上線（因其家族 `donchian_atr` 已批准，一啟用就會進實盤）。

## USDC 帳本（目前閒置）

USDT / USDC 各上限 **5000**。Emily 決策（2026-10-01）：

- 多出來的資金是 Demo 帳上既有的 **5000 USDC 現金**，**不要**把 `book_usdt` 拉到 10000。
- **USDC 帳本維持閒置**（不下單、不換匯成 USDT、不接 USDC-M、不把 `port_ew_univ10` 掛上來）。
- USDT 帳本 `book_usdt=5000`，僅既有 6 個 live 槽：`sat_apt_1h`、`sat_dot_4h`、`sat_fet_4h`、`sat_fil_1h`、`sat_sol_ema_1d`、`ls_near_4h`。
- 合約路徑仍是 Demo **USD-M（USDT 保證金）** only；驗證會拒絕 `quote_currency=USDC` 的 futures 家族（避免假扣 USDC）。

若日後要測現貨 `*USDC`，欄位為：`quote_currency: "USDC"`、`symbol: "XXXUSDC"`、`notional_usdc`（計入 `book_usdc`）。現階段請不要加。
