# 衛星開倉規則（2026-09-19 15:25:54 UTC+08:00）

## 綠燈 · box_paper_vision（勿等 Windows）

| 槽 | 標的 | 變體 | 名義 | 狀態 |
|----|------|------|------|------|
| 核心 | NEAR | donchian 4h | ≤40% | 續抱不加倉 |
| 衛星A | FET | `donchian55_s2.0_t3.0__FET__1h` | ~1250 | 等 1h 破 Donch55 |
| 衛星B | **OP** | `donchian20_s1.5_t1.5__OP__4h` | ~1000 | 等 4h 破 Donch20；備援 s1.5_t2.5 |
| — | FIL | — | — | **不上**（已停損換掉） |
| — | PEPE | — | — | 觀察 only |

## OP 進場（Demo／box paper）
```
symbol: OPUSDT
side: BUY
type: MARKET
quoteOrderQty: 1000
trigger: 4h close > Donchian(20) upper
stop: entry - 1.5*ATR
trail: 1.5*ATR
```
