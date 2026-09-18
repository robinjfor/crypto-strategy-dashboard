# strategy-crypto-s2 — Short-term Crypto Backtest BRIEF

> Generated: **2026-09-18 00:35:35 CST** (Asia/Taipei)
> Selected: **`donchian_breakout_atr__NEAR__4h`**
> Hard gate (ret>0 & ret>B&H after 40bps RT): **PASS**
> Path: `/workspace/strategy-crypto-s2/`

## Snapshot

| Metric | Value |
|--------|-------|
| Total return | **589.1%** |
| Buy & hold (paired) | 26.121% |
| Excess vs B&H | **562.979%** |
| CAGR | 70.3161% |
| MaxDD | -41.4377% |
| Sharpe | 1.3037 |
| Trades | 89 |
| Win rate | 41.573% |
| Time in market | 22.6% |
| Final equity | $689,100.0 |
| Sample | 2023-02-01 → 2026-09-17 (~3.6249y) · **4h** |
| OOS / walk-forward | **None** (in-sample only) |

## Why S1 was rejected

- S1 pick `ema_trend_hold__ZEC__1h` returned **+165.8%** but paired B&H was **+194.0%** — failed beat-B&H hard gate.
- S2 mandate: deepen S1 highlight `ema_cross_atr__NEAR__4h` (+78% vs BH +34% in short sample) and find **all** Tier-A 1h+4h variants that are positive **and** beat B&H after costs.

## Cost assumptions (honest)

- Commission **15.0 bps** + slippage **5.0 bps** = **20.0 bps one-way** / **40.0 bps round-trip**
- Applied on every strategy entry/exit **and** on paired B&H (one RT).

## Strategies tested

1. **ema_cross_atr family** (priority): EMA9/21 baseline, 12/26, 8/21, wide/tight ATR, trend-hold
2. **donchian_breakout family**: Donchian20 / Donchian55 + ATR trail
3. **mean-reversion** (secondary): RSI14 & RSI7 MR + ATR stop

Universe: Tier A — BTC ETH SOL XRP SUI HYPE ZEC DOGE NEAR UNI · TF **1h + 4h**.

## All beat-B&H variants (n=10)

| Variant | TF | Ret% | BH% | Excess | MaxDD | Sharpe | Trades | Years |
|---------|----|------|-----|--------|-------|--------|--------|-------|
| `donchian_breakout_atr__NEAR__4h` | 4h | 589.1 | 26.121 | 562.979 | -41.4377 | 1.3037 | 89 | 3.6249 |
| `donchian_breakout_wide__NEAR__4h` | 4h | 331.2992 | 26.121 | 305.1783 | -56.3491 | 0.9607 | 89 | 3.6249 |
| `donchian_breakout_atr__SUI__4h` | 4h | 138.7571 | -32.7059 | 171.463 | -55.3168 | 0.7772 | 75 | 3.3511 |
| `ema_trend_hold__DOGE__4h` | 4h | 88.2538 | -11.2783 | 99.5321 | -71.5641 | 0.5854 | 178 | 3.6249 |
| `ema_cross_atr_wide__NEAR__4h` | 4h | 121.688 | 26.121 | 95.5671 | -68.2839 | 0.6628 | 169 | 3.6249 |
| `ema_trend_hold__NEAR__4h` | 4h | 116.1215 | 26.121 | 90.0005 | -73.6929 | 0.6495 | 169 | 3.6249 |
| `donchian_breakout_wide__SUI__4h` | 4h | 20.1419 | -32.7059 | 52.8478 | -67.5123 | 0.3873 | 75 | 3.3511 |
| `donchian55_breakout_atr__DOGE__1h` | 1h | 48.7733 | 5.162 | 43.6113 | -39.7263 | 0.5678 | 105 | 2.8455 |
| `ema_cross_atr_wide__DOGE__4h` | 4h | 21.8025 | -11.2783 | 33.0808 | -60.9961 | 0.3578 | 178 | 3.6249 |
| `ema_cross_atr_tight__NEAR__4h` | 4h | 28.0961 | 26.121 | 1.9751 | -54.1398 | 0.3832 | 169 | 3.6249 |

## Top variants by score (incl. non-beaters)

| Variant | TF | Ret% | BH% | Excess | MaxDD | Sharpe | BeatsBH | Score |
|---------|----|------|-----|--------|-------|--------|---------|-------|
| `donchian_breakout_atr__NEAR__4h` | 4h | 589.1 | 26.121 | 562.979 | -41.4377 | 1.3037 | True | 1196.6137 |
| `donchian_breakout_wide__NEAR__4h` | 4h | 331.2992 | 26.121 | 305.1783 | -56.3491 | 0.9607 | True | 672.1623 |
| `donchian_breakout_atr__SUI__4h` | 4h | 138.7571 | -32.7059 | 171.463 | -55.3168 | 0.7772 | True | 345.6299 |
| `ema_cross_atr_wide__NEAR__4h` | 4h | 121.688 | 26.121 | 95.5671 | -68.2839 | 0.6628 | True | 241.6047 |
| `ema_trend_hold__NEAR__4h` | 4h | 116.1215 | 26.121 | 90.0005 | -73.6929 | 0.6495 | True | 226.0647 |
| `ema_trend_hold__DOGE__4h` | 4h | 88.2538 | -11.2783 | 99.5321 | -71.5641 | 0.5854 | True | 209.047 |
| `donchian55_breakout_atr__DOGE__1h` | 1h | 48.7733 | 5.162 | 43.6113 | -39.7263 | 0.5678 | True | 132.8461 |
| `donchian_breakout_wide__SUI__4h` | 4h | 20.1419 | -32.7059 | 52.8478 | -67.5123 | 0.3873 | True | 96.3037 |
| `ema_cross_atr_wide__DOGE__4h` | 4h | 21.8025 | -11.2783 | 33.0808 | -60.9961 | 0.3578 | True | 83.2332 |
| `donchian_breakout_atr__SOL__4h` | 4h | 195.9664 | 315.7018 | -119.7354 | -52.071 | 1.0085 | False | 75.6252 |
| `ema_cross_atr_tight__NEAR__4h` | 4h | 28.0961 | 26.121 | 1.9751 | -54.1398 | 0.3832 | True | 64.0586 |
| `donchian55_breakout_atr__ETH__4h` | 4h | 31.4308 | 52.009 | -20.5782 | -28.3437 | 0.4735 | False | 13.0249 |

## Data coverage (Tier A · 1h)

| Symbol | Source | Bars | Start → End | B&H % |
|--------|--------|------|-------------|-------|
| BTC | Binance data-api.binance.vision | 24987 | 2023-11-11 → 2026-09-17 | 105.1316 |
| ETH | Binance data-api.binance.vision | 24987 | 2023-11-11 → 2026-09-17 | 18.1291 |
| SOL | Binance data-api.binance.vision | 24987 | 2023-11-11 → 2026-09-17 | 62.2744 |
| XRP | Binance data-api.binance.vision | 24987 | 2023-11-11 → 2026-09-17 | 92.1736 |
| SUI | Binance data-api.binance.vision | 24987 | 2023-11-11 → 2026-09-17 | 22.9288 |
| HYPE | OKX | 7598 | 2025-11-05 → 2026-09-17 | 103.9175 |
| ZEC | Binance data-api.binance.vision | 24987 | 2023-11-11 → 2026-09-17 | 4682.7631 |
| DOGE | Binance data-api.binance.vision | 24987 | 2023-11-11 → 2026-09-17 | 0.7575 |
| NEAR | Binance data-api.binance.vision | 24987 | 2023-11-11 → 2026-09-17 | 84.9909 |
| UNI | Binance data-api.binance.vision | 24987 | 2023-11-11 → 2026-09-17 | 33.6374 |

## Data coverage (Tier A · 4h)

| Symbol | Bars | Start → End | B&H % |
|--------|------|-------------|-------|
| BTC | 7987 | 2023-01-25 → 2026-09-17 | 235.7942 |
| ETH | 7987 | 2023-01-25 → 2026-09-17 | 57.4606 |
| SOL | 7987 | 2023-01-25 → 2026-09-17 | 324.636 |
| XRP | 7987 | 2023-01-25 → 2026-09-17 | 215.7413 |
| SUI | 7387 | 2023-05-05 → 2026-09-17 | -44.201 |
| HYPE | 1891 | 2025-11-06 → 2026-09-17 | 110.2841 |
| ZEC | 7987 | 2023-01-25 → 2026-09-17 | 3153.6113 |
| DOGE | 7987 | 2023-01-25 → 2026-09-17 | -4.4336 |
| NEAR | 7987 | 2023-01-25 → 2026-09-17 | 20.2657 |
| UNI | 7987 | 2023-01-25 → 2026-09-17 | 13.1869 |

## Selection rationale (honest)

- **Selected:** `donchian_breakout_atr__NEAR__4h` — ret **589.1%** vs B&H **26.121%** (excess **562.979%**), MaxDD -41.4377%, Sharpe 1.3037, 89 trades.
- Hard gate: **PASS**.
- Beat-B&H count across Tier A × families × 1h/4h: **10**.
- Trend/breakout prioritized over mean-reversion when both beat B&H.
- History length: target 25000 1h bars / 8000 4h bars (API max); HYPE shorter via OKX listing.

## Caveats

- **No OOS / walk-forward** — selection is in-sample over the same window.
- Longer sample than S1 (~4 months) but still not a full multi-cycle crypto regime for every alt.
- High trade-count sleeves are cost-sensitive; 40 bps RT already applied.
- Listing-age differs (esp. HYPE, SUI); per-symbol windows are not identical.
- Reference: `/workspace/strategy-crypto-s1/`, `/workspace/crypto-shortterm-universe/summary.json`

## Deliverables

- `results.json` — selected_variant, metrics, beat_bh_variants, all_variants, trades
- `equity_curve.csv` / `equity_curve.png`
- `BRIEF.md` (this file)
