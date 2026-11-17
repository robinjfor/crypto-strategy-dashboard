# 儀表板改版工程串接說明

## 目標
以 `strategy-dashboard-redesign.html` 的資訊架構改版現有 Binance Demo 儀表板。保留目前的總覽、帳戶明細、成交歷史、策略評分、外部審閱、策略明細、風險、回測可信度與實盤門檻內容；本稿的互動分頁是一頁式原型，正式產品可沿用既有 URL/路由。

## 共通資料規則
- 每筆回應包含 `environment`（demo/paper/real）、`as_of`（ISO 8601 +08:00）、`source`、`currency`、`formula_version`。
- 未知值回傳 `null`，前端顯示「缺資料」，不可以 0 代替。
- Demo、紙上與真實資金分帳；NAV 只加總符合所選環境的錢包。
- 策略帳本 + 未分配資金 + bridge residual = wallet NAV。殘差保留並告警，不自動攤平。
- 數字統一十進位/精度規則；百分比和金額同時顯示。時間全部顯示台北時間。
- 建議總覽 60 秒輪詢；其他分頁按需載入。API 失敗時保留最後成功值並標示資料過期。

## 建議沿用/補足的端點
| 分頁/元件 | 現有或建議端點 | 必要欄位 / 算法 |
|---|---|---|
| 總覽 KPI / NAV 曲線 | `/review/performance` | `capital_basis.nav,pnl,peak,max_drawdown,max_drawdown_pct,nav_history,nav_scope,updated_at`；目前回撤 = 1 - NAV/current peak，最大回撤使用歷史最大值，兩欄不可混用 |
| 資金橋接 | 新增 `/review/bridge` | wallet balances、deposit basis、每策略 equity/capital/realized/unrealized/fees/funding、unallocated cash、residual；校驗 `sum(strategy_equity)+unallocated+residual=wallet_nav` |
| 策略卡/狀態 | `/review/strategies` + `/review/orchestrator` | slot、strategy_id、family、params、paused、weight、target、environment；「已核准」「運行中」「可開新倉」分欄 |
| 成交歷史 | `/review/trades?cursor=&limit=` | `kind` fill/closed/liquidation、strategy_id、slot、side、qty、price、fee、funding、realized_pnl、time、order_id、parity_ref；分頁用 next_cursor |
| 策略明細 | `/review/strategy/{id}`（建議） | 每日 PnL、權益、曝險、成交、成本、參數版本及歸因；虛擬帳本須標記 paper/virtual |
| 持倉風險 | `/review/positions` | symbol、slot、side、qty、entry、mark、notional、leverage、isolated_margin、maint_margin、liq_price、liq_distance、as_of；保留原始欄位和缺值 |
| 風險與控制紀錄 | `/review/risk`（建議）+ orchestrator plan | gross/net exposure、集中度、相關性、壓力情境損失、日損、current/max drawdown、breaker state、risk events；壓測算法版本必須返回 |
| 回測可信度 / parity | `/review/backtest` | run_id、strategy_version、train/OOS dates、fee/slippage/funding assumptions、n_match/n_mismatch、TE、per-day/per-symbol rows、root_cause、repair_cutoff；修復前後樣本分開 |
| 實盤門檻 | `/review/readiness`（建議） | 規則版本、threshold、pass/fail/unknown、證據連結、as_of、approver；前端只讀狀態，不由此端點解鎖交易 |
| 外部審閱 | `/review/comments` + `/review/comments/{id}/reply` | comment_id、reviewer、severity、status、topic、strategy、body、suggested_action、replies、timestamps；支援 status filter 和 cursor |

## 前端實作注意
1. 導覽使用目前網站原有網址：總覽 `/`、帳戶 `/account.html`、成交 `/history.html`、策略評分 `/backtest.html`、外部審閱 `/review.html`；若新頁已存在則沿用 `/strategy.html`、`/risk.html`、`/credibility.html`、`/gate.html`。
2. 將每張卡片的數字旁加「ⓘ」展開來源、公式、資料時間；讀不到來源時標「來源缺失」。
3. 正負損益用文字和色彩雙重編碼，不能只用紅綠色。差額、強平、資料過期使用明確文字告警。
4. 成交表採 cursor pagination；可篩環境、策略、幣種、方向、已平倉/成交/強平。fill 不可與 closed-trade 的 PnL 重複加總。
5. charts 以 API 歷史序列繪製，禁止寫死本原型數字。NAV 圖需可切換期間並標記入金、版本切換、停機事件。
6. Real tab 若尚未有真實帳本 API，顯示「尚未連線」及空狀態，不得複用 Demo 資料。
7. 不新增任何下單、槓桿、配置、暫停/解除按鈕；此設計是績效與審核 UI，不是交易控制面板。

## 驗收案例
- 選 Demo/Real 後，所有卡片和圖表都使用同環境資料；無資料則顯示空狀態。
- wallet NAV 應等於策略帳本 + 未分配 + residual；若 residual 絕對值 > 0.5 USDT 顯示紅色告警並呈現差額。
- `current_drawdown` 和 `max_drawdown` 同時存在且來源公式不同；切換期間不將最大回撤誤標目前回撤。
- 缺少費用或資金費時，淨損益標示「未完整扣成本」，不暗示為淨利。
- parity 未滿規則要求的交易日數/樣本數時，Readiness 永遠不是 pass。
- 7 個無價格/部位訊號必須逐幣列出 unknown reason，不可在匯總中靜默忽略。
- API timeout/過期時顯示最後更新時間和 stale badge；不把 last good data 標成即時。
- 審閱 API 無 `/control` 權限，畫面亦無下單或配置 mutation。

## 原型限制
`strategy-dashboard-redesign.html` 用於視覺與導航討論，金額是根據目前頁面觀察到的樣本或清楚註明的示意值，未連 live API。接入前請以最新 API schema 和真實回讀資料替換，不要複製原型常數。
# 原型導覽調整補充

本版嚴格採用現有網站截圖中的五個頂層分頁：總覽、帳戶明細、成交歷史、策略評分、外部審閱。策略明細、風險、回測可信度、實盤門檻改放到「策略評分」分頁的二級導覽，避免新增頂層分頁改變原有 IA。帳戶明細保留 Demo/模擬/Real 選擇、淨值/回撤卡、策略權益表、未分配、差額與警示。成交歷史保留 fill/closed trade/強平、篩選、cursor 分頁。外部審閱保留評論 thread/replies/severity/status。

正式接入時以既有 route 為優先：`/`、`/account.html`、`/history.html`、`/backtest.html`、`/review.html`。策略分頁內部 tab 可以 query/hash 或前端狀態；不要求建立新頂級 route。所有欄位/API 契約依 `dashboard-integration-notes.md`，切勿複製本 HTML 中的展示常數。
