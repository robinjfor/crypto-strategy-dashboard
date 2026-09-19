# 衛星開倉規則（2026-09-19 09:10:11 UTC+08:00）

## 燈號：綠燈（紅燈協議仍有效；未達 LIVE_GRADUATION 不建議實盤）

| 槽 | 標的 | 變體 | 名義 | 狀態 |
|----|------|------|------|------|
| 核心 | NEAR | donchian_breakout_atr 4h | ≤40% | 續抱不加倉 |
| 衛星A | FET | `donchian55_s2.0_t3.0__FET__1h` | ~1250 | 等 1h 破 Donch55 上軌 |
| 衛星B | **FIL** | `donchian55_s1.5_t2.5__FIL__1h` | ~1000 | 等 1h 破 Donch55 上軌 |
| 現金 | USDT | — | 15% | 底 |

## OP
- Demo pending fill：**取消**，勿再追成交
- 舊變體：備援觀察 only（含 s1.5_t1.5）；−53.6% 版作廢

## FIL 進場規格（Demo）
```
symbol: FILUSDT
side: BUY
type: MARKET
quoteOrderQty: 1000
trigger: 1h close > Donchian(55) upper
stop: entry - 1.5*ATR14
trail: 2.5*ATR (ratchet up)
exit: Donch lower or stop/trail
```

## FET 進場規格（不變）
quoteOrderQty 1250；1h close > Donch55；stop 2×ATR；trail 3×ATR
