# Unified 3-year scores schema (real)

**Source of truth:** `/workspace/strategy-unified-3y/scores.json`  
**Pages copy:** `data/unified-3y/scores.json` (via `scripts/sync_unified_scores.sh`)  
**Companion files:** `scores.csv`, `REPORT.md`, `equity_3y.png`, `equity/<strategy_id>.csv`

## Top-level

```json
{
  "meta": { ... },
  "strategies": [ /* ScoreRow[], already sorted by score desc */ ]
}
```

## meta (observed)

```json
{
  "period": {
    "start_utc": "2023-09-24 00:00:00+00:00",
    "end_utc": "2026-09-24 00:00:00+00:00",
    "note": "Warmup indicators use pre-period bars; trading/equity normalized from 2023-09-24. Last closed kline used."
  },
  "initial": 10000.0,
  "costs": {
    "fee_bps": 10,
    "slippage_bps": 10,
    "one_way_bps": 20
  },
  "gate": {
    "ret_3y_gt_bh": true,
    "oos_pass_min": "4/6",
    "maxdd_max_abs": 45,
    "oos_definition": "Fixed-parameter segmented test: split the 3y evaluation window into 6 contiguous ~6-month folds; count folds where strategy return > buy&hold return. NOT a re-optimizing walk-forward."
  },
  "scoring_method": {
    "only_gate_passers_nonzero": true,
    "weights": {
      "cagr_3y": 35,
      "ret_1y": 30,
      "maxdd": 15,
      "oos_pass": 10,
      "excess_vs_bh": 10
    },
    "minmax": "For cagr_3y, ret_1y, excess=(ret_3y-bh_ret_3y), and maxdd_quality=(-abs(maxdd)): norm=(x-min)/(max-min) among gate passers; if only one passer or max==min, norm=1.0",
    "oos_points": "oos_wins/6*10 (absolute, not minmax)",
    "sort": "score desc, then ret_3y desc",
    "notes": []
  },
  "generated_at": "2026-09-24 15:58:10 CST",
  "n_strategies": 16,
  "n_gate_pass": 5
}
```

## ScoreRow fields (observed keys)

- `strategy_id`: example `"donchian20_s1.5_t1.5__OP__4h"`
- `symbol`: example `"OP"`
- `timeframe`: example `"4h"`
- `status`: example `"現役"`
- `params`: example `{"symbol": "OP", "tf": "4h", "donch_n": 20, "stop_m": 1.5, "trail_m": 1.5, "max_`
- `kind`: example `"donchian"`
- `initial`: example `10000.0`
- `final`: example `20567.55`
- `ret_3y`: example `105.6755`
- `cagr_3y`: example `27.1658`
- `maxdd`: example `-28.5979`
- `n_trades`: example `68`
- `bh_ret_3y`: example `-90.3773`
- `period_start`: example `"2023-09-24"`
- `period_end`: example `"2026-09-24"`
- `years`: example `3.0007`
- `ret_1y`: example `31.9873`
- `bh_final`: example `962.27`
- `oos_pass`: example `"5/6"`
- `oos_wins`: example `5`
- `oos_total`: example `6`
- `oos_folds`: example `[{"fold": 1, "start": "2023-09-24", "end": "2024-03-24", "strat_ret": 24.2662, "`
- `n_liquidations`: example `0`
- `gate_pass`: example `true`
- `gate_fail_reasons`: example `[]`
- `score`: example `48.7519`
- `score_breakdown`: example `{"cagr_norm": 0.3304, "ret_1y_norm": 0.1472, "maxdd_norm": 0.9624, "oos_points":`

### Semantics used by `backtest.html`

| Field | UI |
|-------|-----|
| `strategy_id` | 策略 id |
| `symbol` + `timeframe` | 幣別／週期 |
| `period_start` / `period_end` | 期間 |
| `initial` | 投入（10000） |
| `final` | 最終金額 |
| `ret_3y` | 3 年報酬（**percent points**, e.g. 435.3 = +435.3%） |
| `ret_1y` | 近 1 年報酬（percent points） |
| `bh_ret_3y` | B&H 3 年（percent points） |
| `maxdd` | MaxDD（percent points, negative） |
| `oos_pass` / `oos_wins`/`oos_total` | OOS（顯示為 wins/total） |
| `n_trades` | 交易次數 |
| `gate_pass` | 是否過門檻 |
| `gate_fail_reasons` | 未過原因（字串陣列；UI 盡量中文化） |
| `score` | 分數（gate 未過者為 0） |
| `status` | `現役` / `候選` / `對照` |
| equity | `equity/{strategy_id}.csv`（欄位 t,equity 或 date,equity） |

### Returns convention

Analyst file stores returns as **percent points** (42.5 = +42.5%), **not** fractions. The UI divides by 100 only for formatting as percent.

### Approve lock

`gate_pass === false` → 審核通過 button locked.  
Family not in runner support set → locked with 「雲端尚未支援此策略類型」.


## Additional fields (2026-09-24 update)

### Per-row
- `full_period`: `{start, end, ret, cagr, bh_ret, final, maxdd, oos_pass, oos_wins, oos_total, n_trades, gate_pass_full, gate_fail_reasons}`
- `gate_pass_both`: true only when 3y `gate_pass` AND `full_period.gate_pass_full`
- `slot`: e.g. `satellite_A` (optional)
- `review`: e.g. `PENDING_EMILY` (optional)
- `status`: verbatim badge text (現役 / 候選 / 對照 / 衛星A候選（首選） / 待替換（未過門檻） / …)

### meta extras
- `full_period_note`, `n_gate_pass_both`, `fet_lowdd`, `core_dual_gate_scan`

### Approve lock (revised)
`gate_pass_both === false` → 審核通過 locked (Emily 資金控管).  
When meta is unclear, UI still shows both 3y and full-period gates and locks on `gate_pass_both` if present else `gate_pass`.

## catalog.json (preferred when present)

Path: `/workspace/strategy-unified-3y/catalog.json` → copied to `data/unified-3y/catalog.json`.

Schema is analyst-owned and may evolve. Loader (`backtest.js`) is tolerant and maps:

| catalog shape | mapping |
|---------------|---------|
| `{strategies:[{id,key,params,rules_zh,rows|variants|symbols:[...]}]}` | use as cards |
| `{catalog: [...]}` / `{groups: [...]}` | same |
| missing / unreadable | fall back to grouping `scores.json` by strategy key (prefix before `__SYMBOL__TF`) |

Chinese rule text is generated from `params` + known specs when catalog omits `rules_zh` / `description_zh`.
