# research_1023：7 個方向回測（只回測，未開倉、未動 catalog/allocation/live）
產生：2026-10-23（Asia/Taipei）。程式 `research.py`，結果 `results.json`、`ranking.csv`、`returns_*.csv`。
規則：決策於 t 收盤、t+1 開盤成交；每邊 20bps；Binance 真實資金費率；滾動 WF 12m 訓練/3m 測試；OOS 2023-10-01~2026-09-30；DSR（Bailey & López de Prado，試驗數=該方向網格大小）；
門檻 OOS Sharpe≥0.8、MaxDD≤35%、鄰近參數穩健度≥50%。每月 USD 以本金 20,500U 按 CAGR 換算（190U/月 ≈ 年化 11.7%）。

## 排名表
```
              direction    cagr   maxdd  sharpe   dsr  trials  neighbor_rob passed  cagr_ge_120  usd_per_month_20500
  1_funding_basis_carry  0.1203  0.0221    4.54 1.000      27          1.00   True        False                195.0
           BTC_buy_hold  0.4578  0.5298    1.03   NaN       0           NaN    NaN        False                654.0
     3_vol_breakout_lev  0.4240  0.3417    1.02 0.923      18          0.00  False        False                613.0
  7_risk_parity_U1_S5_E  0.2100  0.4101    0.75 0.886       4          0.00  False        False                328.0
 4_liq_cascade_reversal  0.1605  0.3577    0.63 0.241      36          0.67  False        False                256.0
6_momentum_rotation_lev  0.0505  0.8555    0.51 0.648      18          0.00  False        False                 84.0
                   cash  0.0000 -0.0000    0.00   NaN       0           NaN    NaN        False                  0.0
  5_event_listing_short -0.6826  0.9807   -0.56 0.084      18          0.00  False        False              -1870.0
2_cross_exchange_spread     NaN     NaN     NaN   NaN       0           NaN  False        False                  NaN
```

## 各方向重點
1. **資金費率/基差（現貨多+永續空）**：唯一過關。但 WF 前 10 季挑 lev3（需現貨保證金/借貸，Demo 永續帳戶無法照做），最後 2 季改挑 lev1；後半段（2025-04 起）Sharpe −0.44，收益明顯衰退。lev1 原版 CAGR 約 3.8%（見 ../funding_arb/results.json）。年化 120%：否；190U/月：僅 lev3 理論值剛好達到（195U），實際不可行。
2. **跨所價差**：不可行。本機只有 Binance 公開資料，且日線無法模擬跨所同步成交、提幣延遲與費用。0 次試驗。
3. **波動率突破+槓桿**（Donchian、Top10、波動壓縮濾網）：Sharpe 1.02、DD 34%，但鄰近穩健度 0%（L2 時 DD 63%；鄰近組 DD>35%），後半段 Sharpe 0.47 → 未過關。
4. **清算瀑布反轉**（日跌≥X%且 OI 日降≥Y% → 下一開盤做多 H 天；OI 取自 data.binance.vision metrics，6 檔大幣）：鄰近穩健度 67%，但 WF OOS Sharpe 0.63、DD 36%、DSR 0.24 → 未過關。
5. **事件驅動**：上幣（做空新上線永續）測了，大虧（−68%/年，被軋空）；**解鎖無公開歷史資料，不可測**。
6. **動量輪動高槓桿**：Sharpe 0.51、DD 86% → 未過關。
7. **風險平價 U1/S5/E**：本機與記憶中都找不到「E」的定義，**暫以第 5 方向的事件 sleeve 當 E（假設）**。Sharpe 0.75、DD 41% → 未過關。訓練窗只能用 2023-10 之後的 sleeve OOS 資料（已截短）。

## 結論
- 沒有任何方向能做到年化 120%。能穩定到 190U/月的只有 BTC 持有（DD 53%）和波動率突破（未過關）。
- 這輪的試驗次數：27+18+36+18+18+4 = 121。

## Demo 小倉建議規格
建議只放**資金費率套利 lev1**，作為**觀察倉**，不是主力：
- 規則照 `../funding_arb/SPEC.md` 一字不改（Top20 流動性、F7>15% 進場、F1≥0、exit_thr/K 由 WF 每季重選；目前 WF 選 exit 0、K10、lev 1）、t+1 開盤雙腿同時成交、永續 1/3 保證金、淨 1x。
- 大小：≤ 本金 5%（約 1,000U）。日報跟回測同一份程式碼重算並對帳；偏差 >0.5%/週就停。
- 注意：後半段 Sharpe 是負的，預期報酬接近現金。其他方向都不建議上 Demo。
- 需要有 Binance 現貨＋永續的 Demo 帳戶；本次沒有下任何單。

## 資料限制
- 本機現在連 fapi.binance.com / api.binance.com 都回 HTTP 451（地區封鎖）。這輪研究用的是已快取的 panel（到 2026-09-30）加上 data.binance.vision 的 OI 資料。
