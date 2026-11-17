# E5 parity capital and unpriced signals

The latest E5 parity day uses `orchestrator.targets.demo_e5_min5_x175_t05top20` as `sizing_capital`. A snapshot `equity` or `K` of 5000 stays on `virtual_capital` and is not the comparison capital. Example: target 1986.13 with virtual 5000. Older days that have no orchestrator target for that bar still show the virtual figure and are labeled `virtual`.

Fewer than 21 parity days is `unverified`（未驗證）. Trial count and DSR are `missing`; this page does not invent them. Before and after the parity-fix dates (2026-10-08 and 2026-10-21) are separate rows.

A coin with a non-zero signal weight, a null price, and no book quantity is listed with one cause:

- `tradfi`: on `tradfi_symbols.json` or `tradfi_blocked`. No mark and no order.
- `not_listed_on_demo`: the exec skip line says `not listed`.
- `no_mark`: the weight is on the plan, and the snapshot, the open positions, and the exec fills have no price.

This checkout does not contain the live `ws` dump, so the seven names from the review are not in git. The classifier above is what names each of them when that plan is on state. A seven-coin fixture in `tests/test_review_credibility.py` covers all three causes (two symbols from the TradFi map, one not listed, four with no mark).
