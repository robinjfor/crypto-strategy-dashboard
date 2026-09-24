# service/ — Binance Demo trader (Cloud Run Job)

Region: **asia-east1**. Job: `crypto-trader`.  
Schedulers: `crypto-trader-hourly` (`1 * * * *` UTC) and `crypto-trader-daily-sol` (`7 0 * * *` UTC).

## Modes

| Mode | Behavior |
|------|----------|
| `probe` | ping / time / signed account (nonzero balances only; never print keys) |
| `dry-run` | signals only; no orders |
| `run` | places only when `TRADER_MODE=live` **and** `TRADER_ENABLED=true` |

## Slots

**Satellites** (Wilder ATR14 trail — separate code path):
- FETUSDT 1h donchian55_s2.0_t3.0 — 1250 USDT
- OPUSDT 4h donchian20_s1.5_t1.5 + reset_below_hi — 1000 USDT
- DOTUSDT 4h donchian20_s1.5_t1.5 — 1000 USDT (stop ref 1.073)
- NEAR — not armed

**Core** (own module `sol_core/`; ATR = **SMA TR14**, not Wilder):
- SOLUSDT 1d `donchian20_atr_btcRegime__SOL__1d` — **1500 USDT**
- Entry only on regime-filtered rising edge 0→1 after a reset
- Daily window UTC 00:05–00:15; hard exchange STOP_LOSS_LIMIT/OCO after fill; trail replaces stop daily
- Expectation log → GCS `trader/sol_expectation_log.json` + dashboard feed

## Safety

- Spot only, no leverage
- `MAX_NOTIONAL_USDT` hard cap
- Kill switch: `TRADER_ENABLED=false`
- Idempotent clientOrderId per slot+bar
- State: `gs://$GCS_BUCKET/trader/state.json`
- Feed: `gs://$GCS_BUCKET/trader/dashboard_feed.json`

## Local dry-run

```bash
cd service
pip install -r requirements.txt
python main.py dry-run
```

## Deploy

Manual only: `.github/workflows/deploy-trader.yml` (`workflow_dispatch`).  
See `GCP_SETUP_zh.md`. Default scheduler args = `dry-run` until `TRADER_MODE=live`.
