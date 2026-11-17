# Capital bridge and E5 parity capital

Display only. The bridge does not change a ledger, an order, leverage, or margin.

## (b) Why the books and the wallets do not match

USDT-M is one deposit of 5000. E5's virtual book is still sized off 5000. VBO's 1000 was carved out of that same deposit on 2026-11-04 (it is also the FA idle that was not a second pile of cash). Adding E5 equity + VBO equity + R2S equity therefore counts that 1000 twice whenever E5's cash was not reduced by the carve.

The live overcount named in review is **993.19**: E5+VBO+R2S sits that far above the USDT-M wallet. 1000 minus 993.19 is 6.81, which is cash already moved or pnl already inside one of the books. `GET /review/bridge` reports `usdt_book_over_wallet` as that difference. It does not subtract 993.19, and it does not subtract the 1000, to force `bridge_diff` to 0.

USDC is a separate 5000 deposit. U1's orchestrator cap and sleeve capital are **1000** (`orchestrator.DEFAULT_CAP["U"]`). The other **4000.00** is unallocated USDC cash: wallet balance minus that 1000. It stays on `unallocated_usdc`.

The retired APT 1h slot (`sat_apt_1h`, C8) was **750 USDT**, disabled 2026-10-07. It is not USDC cash and it is not the 4000. The bridge lists it so the two are not mixed.

`bridge_diff = nav − (sum of strategy equity + unallocated_cash)`. When the absolute diff is above 0.5 USDT, `bridge_diff_red` is true.

Paper NAV is a separate figure with `env: paper`. It is not added into the Demo NAV. Real has no account here: the figure is `not yet enabled`, not 0.

## (a) E5 parity capital, and coins with a signal but no price

`e5_tracking._sizing` currently returns the plan snapshot's `equity` or `K` before the orchestrator target. Those fields are the virtual book (5000), not the sizing target the orchestrator wrote (for example 1986.13 on `orchestrator.targets.demo_e5_min5_x175_t05top20`). The bridge already labels `strategy_capital_basis` as `orchestrator_target` when that target is present, and `virtual` otherwise. The parity comparison itself is switched to the orchestrator target in the credibility change (C4). This file does not change `demo_e5_exec`.

A parity row is built for every S5 symbol in `ws` (`demo_e5.s5_symbols`). The price is taken only from the plan snapshot, from an open position, or from an exec fill. When none of those has a mark, `price` and `target_qty` are null. If the virtual book also has no leg, `actual_qty` is 0. That is a signal with no price and no position.

This checkout has no saved live `ws` dump, so the seven names are not in git. The cause for each such coin, in order, is:

1. The symbol is on the TradFi list (`tradfi_symbols.json` or `tradfi_blocked`). Those contracts are not priced and are not opened (`-4411`).
2. The exec log skipped it as `not listed` on Demo. There is no mark and no order.
3. Otherwise `no_mark`: the weight is on the target file, and neither the snapshot nor the positions carry a price, so the qty cannot be sized and the book stays flat.

C4 lists each unpriced signal from the plan with one of those causes. It does not invent the seven names.
