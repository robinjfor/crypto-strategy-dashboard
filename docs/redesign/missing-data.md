# 改版需要、但目前 API 沒有的欄位

前端只畫現有回應。下面這些是原型與 `dashboard-integration-notes.md` 要的資料，目前沒有對應欄位。缺的數字顯示「缺值」，實盤沒有帳戶顯示「尚未開通」，不會用原型裡的示意金額代替。

| 端點 | 欄位 | 用途 |
|---|---|---|
| 各 `/review/*` 回應 | `formula_version` | ⓘ 要標這筆數字用的公式版本。現在只有各頁自己寫的公式文字。 |
| 各 `/review/*` 與 `GET /status` | 列上的 `environment`（demo / paper / real） | 同一張表要能按環境過濾。現在 `account` 固定是 `binance_demo`，Paper 只有 `status.paper.nav`，沒有實盤帳本。 |
| （沒有這個端點） | Real 錢包、持倉、成交 | Real 分頁必須空白。現在沒有實盤 API，所以顯示「尚未開通」，不重用 Demo。 |
| `GET /review/strategy` | `realized`、`unrealized`、`turnover_30d`、`end` 目前固定是字面「缺值」；沒有每日損益、曝險序列、成本歸因、參數版本歷史 | 策略明細要分開已實現／未實現／費用／資金費，並能看版本。費用或資金費為缺值時，頁面標「未完整扣成本」。 |
| `GET /review/positions` | `maint_margin` | 持倉風險要並排維持保證金與強平價。風險頁有 `isolated_wallet`，沒有維持保證金。 |
| `GET /review/risk` | 各幣日報酬（相關熱圖目前固定缺值）、`stress_model_version`、具名的 breaker 狀態（現在只有 `halt`） | 原型的相關性、壓力情境版本、斷路器。 |
| `GET /review/credibility` | `sample_period`、`walk_forward`、`trial_count`、`dsr` 目前是「缺值」；沒有 `repair_cutoff`，也沒有帶 `run_id` 的費率／滑價／資金費實測 | 回測可信度的樣本與成本假設。修復前後筆數在 `before_after`，切點日期沒有單獨欄位。 |
| `GET /review/backtest` | `run_id`、訓練／留出區間、費後年化、Sharpe、最大回撤、交易數 | 策略評分的候選比較表。靜態研究 JSON 在 `data/`，不是帶 run_id 的即時回測回應。本頁仍讀那些靜態報告，不把原型上的 1.557 或 30.8% 寫進畫面。 |
| `GET /review/trades` | 伺服器端的環境、策略、幣種、方向、kind 篩選；每筆的 `funding`（與 `fee` 分開）；`parity_ref` | 成交歷史的篩選與對帳連結。現在有 `cursor`／`next_cursor` 與 `kind`，篩選在已載入的那一頁上做。 |
| `GET /review/performance` 的 `nav_history` | 入金、版本切換、停機事件標記（現在是日期與淨值） | 淨值圖要標這些事件。圖只用這條序列，不畫原型裡的示意曲線。 |
| `GET /review/gate` | 每一條的 `evidence_url`、`rule_version`、該條的 `approver`（頁面層有 `approver`／`approved_at`） | 門檻旁邊的證據與版本。此頁沒有解鎖按鈕。 |
| `GET /review/strategies` | 分開的「已核准」「運行中」「可開新倉」三欄 | 現在有 `approved`、`slot_paused`、`status`、`order_mode`，沒有單獨的可開新倉旗標。總覽仍用既有的暫停與編排欄位。 |
| `GET /review/bridge`（目前 main） | 依 `/fapi/v1/income` 分類型、分幣的差額歸因 | 差額原因。這份前端只顯示橋接回應裡已經有的差額，不吸收它。收入明細在另一個未合併的分支，這裡不依賴那個欄位。 |
