# Capital bridge and E5 parity capital

Display only. The bridge does not change a ledger, an order, leverage, or margin.

## (b) Why the books and the wallets do not match

USDT-M is one deposit of 5000. VBO's 1000 was carved out of that deposit, so the bridge subtracts `vbo_carve_usdt` 1000 from E5 equity before adding VBO. E5's deposit claim on the bridge is 4000. Unallocated USDC is the USDC wallet balance minus U1 equity, not U1's configured capital. The difference that remains is `bridge_diff` and is not absorbed. Income rows from `/fapi/v1/income`, when the snapshot has them, are grouped by type and symbol beside that difference.

Before this adjustment the review named a USDT-M overcount of 993.19 and a bridge residual of -776.26. Those figures counted E5 as if it still owned the full 5000 deposit. `GET /review/bridge` now subtracts the 1000 carve from E5 equity first. It does not subtract the remaining residual to force `bridge_diff` to 0. On a fixture with E5 cash 5000, VBO 1000, R2S 6.79, U1 equity 1000, and both wallets at 5000, `bridge_diff` is **-6.79**. This checkout has no live account snapshot, so that fixture is the number reported here.

USDC is a separate 5000 deposit. Unallocated USDC is the wallet balance minus U1 equity (cash plus position marks), not U1's configured capital of 1000. When U1 equity is 1000 the unallocated figure is **4000.00**.

The retired APT 1h slot (`sat_apt_1h`, C8) was **750 USDT**, disabled 2026-10-07. It is not USDC cash and it is not the 4000. The bridge lists it so the two are not mixed.

`bridge_diff = nav − (sum of strategy equity + unallocated_cash)`. `bridge_diff_red` is true when the absolute diff is at least 0.5 USDT. Income rows already on the snapshot (`futures_income`, from `/fapi/v1/income`) are grouped by type and symbol on `income_breakdown`. That grouping does not change `bridge_diff`.

Paper NAV is a separate figure with `env: paper`. It is not added into the Demo NAV. Real has no account here: the figure is 尚未開通, not 0. A missing number is 缺值.

## (a) E5 parity capital, and coins with a signal but no price

`e5_tracking._sizing` currently returns the plan snapshot's `equity` or `K` before the orchestrator target. Those fields are the virtual book (5000), not the sizing target the orchestrator wrote (for example 1986.13 on `orchestrator.targets.demo_e5_min5_x175_t05top20`). The bridge already labels `strategy_capital_basis` as `orchestrator_target` when that target is present, and `virtual` otherwise. The parity comparison itself is switched to the orchestrator target in the credibility change (C4). This file does not change `demo_e5_exec`.

A parity row is built for every S5 symbol in `ws` (`demo_e5.s5_symbols`). The price is taken only from the plan snapshot, from an open position, or from an exec fill. When none of those has a mark, `price` and `target_qty` are null. If the virtual book also has no leg, `actual_qty` is 0. That is a signal with no price and no position.

This checkout has no saved live `ws` dump, so the seven names are not in git. The cause for each such coin, in order, is:

1. The symbol is on the TradFi list (`tradfi_symbols.json` or `tradfi_blocked`). Those contracts are not priced and are not opened (`-4411`).
2. The exec log skipped it as `not listed` on Demo. There is no mark and no order.
3. Otherwise `no_mark`: the weight is on the target file, and neither the snapshot nor the positions carry a price, so the qty cannot be sized and the book stays flat.

C4 lists each unpriced signal from the plan with one of those causes. It does not invent the seven names.
