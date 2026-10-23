# shadow_AB: paper shadow ledgers (no orders, public Binance data only)
Every ledger starts at the 2026-10-23 00:00 UTC open with 1000 USDC and runs to 2026-11-20. Each run refetches closed daily bars and real funding, then recomputes the whole ledger from the start.

| ledger | portfolio | script | output |
|---|---|---|---|
| A | 70% U1 tv0.40 / 30% S5 long/short, ×1.0 | `ledger_a.py` | `ledger_A.csv` |
| B | 70/30, ×1.5 | `ledger_b.py` | `ledger_B.csv` |
| C | 80/20, ×2.5 (leverage-scan pick, in-sample) | `ledger_c.py` | `ledger_C.csv` |
| D | pure U1 tv0.40 ×1.5 (tv 0.60, L cap 3); hard stop at −40% from HWM → flat until the end | `ledger_d.py` | `ledger_D.csv` |
| E | 80/20, ×1.75 | `ledger_e.py` | `ledger_E.csv` |

A, B, C and E share the same engine (`ledger_c.py`, with env LEDGER_NAME / LEDGER_W / LEDGER_LV):
- **U1 sleeve:** BTCUSDC/ETHUSDC perps, cross margin. tv = 0.40 × Lv × m, L cap 3, 25% band.
- **S5 sleeve:** USDT-M perps, isolated margin per leg. Settings X=5M, tv0.2, b0.1, frozen at the WF pick for 2026-Q4; positions × Lv × m.
- **Re-split:** the U1/S5 split is reset at every open, at no cost.
- **Breaker:** DD ≤ −10% → m = 0.5; back to m = 1 once DD > −5%. DD ≤ −40% → flat for 30 days, then m = 0.5 with the HWM reset.
- **Liquidation:** checked on daily Low/High.

Note: A/B here replace the earlier A/B in `../../u1_shorts/shadow_tv040/` (U1 tv0.40 and tv0.60, starting 2026-10-21). Those ledgers still exist and are unchanged.

## Commands (daily, after 00:05 UTC = 08:05 Asia/Taipei)
```
cd /workspace/strategy-unified-3y/spec_2026-11/research_1022/shadow_AB
python3 run_all.py            # = ledger_c.py (fetch) ; ledger_a/b/e.py --no-fetch ; ledger_d.py
```
Single ledger: `python3 ledger_e.py` (fetches data), or `python3 ledger_e.py --no-fetch` (uses the cached data from today's fetch).
- The first full fetch took about 58 minutes (rate limits). Later fetches are incremental.
- Every run appends one line to `run.log`.
- Dry-run test: `LEDGER_TEST_START=2026-09-01 python3 ledger_e.py --no-fetch` writes `test_ledger_E.csv` and does not log.

Nothing in the catalog, allocation.json, or live/cloud config is touched.
