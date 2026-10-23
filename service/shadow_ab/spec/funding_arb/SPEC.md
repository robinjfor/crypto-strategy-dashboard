**STATUS: ARCHIVED (2026-10-21). Not for live.**

# SPEC: funding_arb (long spot + short USDT-M perp). Status: **passes the gate on these assumptions** (read the caveats).
Code: `funding_arb.py` (run `python3 funding_arb.py base`). Live must match this file exactly.

## Data / timing
- Binance spot 1d and USDT-M perp 1d klines (UTC). Real funding settlements (8h/4h/1h intervals all handled by summing them).
- Daily funding F_d = sum of settlements in (00:00 d, 00:00 d+1]. F7_t = Σ F_{t-6..t} × 365/7 (annualized). F1_t = F_t.
- Decision at 00:00 UTC (after the 00:00 settlement); **execute both legs at the open of bar t+1**, same minute, equal notional.
## Universe
- Top 20 USDT-M perps by 30d mean quote volume (≥60 bars history), and a same-name Binance spot pair (XXXUSDT) has to exist. 1000-prefixed perps without a matching spot ticker are excluded. Same exclusion list as satellite.
## Rules
- Entry: not held, in universe, F7_t > 15%, F1_t ≥ 0. If there are more candidates than free slots, take the highest F7 first.
- Exit (both legs): F7_t < exit_thr, or F1_t < 0 (flip), or spot/perp data missing (delist). Out-of-universe positions are held until an exit rule fires.
- Slots: K (WF-chosen from {3,5,10}), exit_thr from {0, 5%, 10%} (WF). If K shrinks, drop the lowest F7 positions.
- Size per slot: 1/K of equity. Spot notional = perp notional = slot/(1+1/3); perp margin = 1/3 of notional (isolated, 3x on the perp leg, net 1x delta-neutral). No spot borrow. Idle USDT earns 0 in the backtest.
- Costs: 20bps per side per leg (4 × 20bps on each round trip). Funding received on the perp short notional. Basis P&L comes from the real spot vs perp daily open prices.
- WF: quarterly re-selection by trailing 12m Sharpe. Positions carry across fold boundaries. Resizing existing slots when K changes is not charged (small effect).
## Live risk controls the backtest does not model (must implement)
- Intraday squeeze vs. perp margin: top up margin or reduce when the perp leg's mark loss is >50% of its margin. Daily bars cannot see liquidation risk.
- Rebalance leg drift if |spot−perp notional| > 5% of the position.
