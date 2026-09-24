# 統一三年回測評分 — strategy-unified-3y（更新）

> 產生：**2026-09-24 16:01:10 CST**
> 3 年窗：2023-09-24 → 2026-09-24｜init=10000.0｜單邊 20bps
> 新增：`full_period` / `gate_pass_both`｜FET 子報告 `fet_lowdd/FET_LOWDD.md`｜ICP→衛星候補

## 資金控管後續摘要

### FET 降 MaxDD
- 測試 **14** 組；3y 過關 4；雙過 4
- 最佳：`donchian55_s2.0_t3.0__FET__4h`｜3y ret=77.9805 maxdd=-36.0232 oos=4/6｜全期 ret=78.2755 maxdd=-40.4041 oos=4/6｜both=True

### ICP 全期
- full ret=36.5319 bh=-81.7054 maxdd=-50.1641 oos=4/6 gate_full=False (2022-03-11→2026-09-24)

### 核心槽雙標準（s3 已存結果，未擴大搜尋）
- No strategy among 16 persisted s3 named results passes BOTH unified-3y gate and full-period gate under 20bps.
- 詳見 `core_dual_gate_scan.json`

## OOS 定義

Fixed-parameter segmented test: split the 3y evaluation window into 6 contiguous ~6-month folds; count folds where strategy return > buy&hold return. NOT a re-optimizing walk-forward.

全期 OOS 使用同一「固定參數六等分」定義。

## 總表

| status | id | ret_3y | maxdd | oos | gate3 | score | full_ret | full_maxdd | full_oos | both |
|--------|----|--------|-------|-----|-------|-------|----------|------------|----------|------|
| 現役 | `donchian20_s1.5_t1.5__OP__4h` | 105.6755 | -28.5979 | 5/6 | Y | 54.3147 | 130.2371 | -37.4121 | 5/6 | Y |
| 候選 | `ema12_26_atr_btcRegime__SOL__1d` | 546.9384 | -37.8425 | 4/6 | Y | 53.4996 | 21332.4411 | -45.4713 | 3/6 | N |
| 衛星候補 | `donchian55_s2.5_t3.5__ICP__4h` | 52.7535 | -39.7865 | 4/6 | Y | 46.6784 | 36.5319 | -50.1641 | 4/6 | N |
| 現役 | `donchian20_s1.5_t1.5__DOT__4h` | 42.4987 | -28.1604 | 5/6 | Y | 45.701 | -37.5518 | -68.1595 | 5/6 | N |
| 候選 | `donchian55_s2.0_t3.0__FET__4h` | 77.9805 | -36.0232 | 4/6 | Y | 32.5451 | 78.2755 | -40.4041 | 4/6 | Y |
| 候選 | `donchian55_s2.0_t3.0_btcRegimeD__FET__4h` | 53.291 | -31.6736 | 4/6 | Y | 27.6881 | 62.1394 | -31.6736 | 4/6 | Y |
| 候選 | `donchian55_s2.0_t3.0__LSK__4h` | -6.0959 | -33.4947 | 4/6 | Y | 23.704 | -49.1794 | -68.0114 | 3/6 | N |
| 候選 | `donchian55_s3.0_t4.0_btcRegimeD__FET__4h` | 27.2522 | -41.5263 | 4/6 | Y | 14.2303 | 20.1536 | -43.8765 | 4/6 | Y |
| 候選 | `donchian55_s2.5_t3.5_btcRegimeD__FET__4h` | 25.055 | -39.6434 | 4/6 | Y | 12.6596 | 27.7736 | -39.6434 | 4/6 | Y |
| 對照 | `donchian_breakout_atr__NEAR__4h` | 1658.4537 | -34.0037 | 3/6 | N | 0.0 | 509.8923 | -66.7161 | 4/6 | N |
| 對照 | `donchian_breakout_wide__NEAR__4h` | 786.4484 | -60.1375 | 3/6 | N | 0.0 | 369.8462 | -60.1375 | 4/6 | N |
| 候選 | `donchian55_atr_btcRegime_lev1.5__SOL__1d` | 523.2773 | -58.3528 | 4/6 | N | 0.0 | 39316.7084 | -58.3528 | 3/6 | N |
| 現役 | `donchian20_atr_btcRegime__SOL__1d` | 435.3457 | -40.8998 | 4/6 | N | 0.0 | 36580.6438 | -45.0127 | 4/6 | N |
| 候選 | `supertrend3__SOL__4h` | 280.0389 | -65.7891 | 3/6 | N | 0.0 | 612.7348 | -80.83 | 2/6 | N |
| 候選 | `donchian_breakout_atr__SUI__4h` | 269.2993 | -56.2064 | 3/6 | N | 0.0 | 192.2715 | -56.2064 | 4/6 | N |
| 候選 | `rotation_lb120_top3_absMom__MULTI__1d` | 257.8846 | -56.931 | 3/6 | N | 0.0 | 21080.6104 | -79.1567 | 5/6 | N |
| 候選 | `ema_trend_hold__DOGE__4h` | 180.0504 | -72.357 | 3/6 | N | 0.0 | 262.8653 | -75.1688 | 4/6 | N |
| 候選 | `donchian20_s2.5_t3.5__CAKE__4h` | 157.7786 | -50.8004 | 2/6 | N | 0.0 | 81.5071 | -50.8004 | 4/6 | N |
| 候選 | `donchian20_s2.0_t3.0__ADA__4h` | 157.5839 | -44.5717 | 3/6 | N | 0.0 | -34.6768 | -78.019 | 3/6 | N |
| 現役 | `donchian55_s2.0_t3.0__FET__1h` | 108.0462 | -53.6757 | 4/6 | N | 0.0 | 66.2906 | -53.6757 | 4/6 | N |

## 檔案

- `scores.json` / `scores.csv` / `REPORT.md`
- `fet_lowdd/FET_LOWDD.md` / `fet_lowdd/fet_variants.json`
- `core_dual_gate_scan.json`
