# Crypto 策略績效儀表板（GitHub Pages 靜態站）

純靜態前端：相對路徑讀取 `./data/...`，可部署到 GitHub Pages（含專案子路徑）。

## 結構

```
crypto-dashboard-pages/
  index.html
  app.js
  styles.css
  data/
    strategies.json          # 策略清單 + default
    <strategy-id>/
      results.json
      equity_curve.csv
      settlement.json
  README.md
```

## 本機預覽

```bash
cd /workspace/crypto-dashboard-pages
python3 -m http.server 8877
# 開啟 http://127.0.0.1:8877/
```

## 行為摘要

- 讀取 `data/strategies.json`；預設選 crypto（`is_crypto` 或 id 含 `crypto`）中建置時寫入的 `default`
- KPI、近 7／30 交易日報酬（由 equity CSV 推算）、標的 chips、Chart.js 資金曲線、變體表、成交、結算空殼
- 「重整」重新 `fetch`（`cache: no-store`）
- 標籤繁中；深色加密風；標的來自資料欄位（不寫死）

## 更新資料

1. 將新策略資料放入 `data/<id>/`
2. 重建或手動更新 `data/strategies.json`（含 `default`）
3. 重新部署 Pages

## 注意

- 必須以 HTTP 伺服器開啟（`file://` 無法 fetch 本地 JSON）
- 不要寫死以 `/` 開頭的絕對路徑，以免 Pages 專案子路徑失效
