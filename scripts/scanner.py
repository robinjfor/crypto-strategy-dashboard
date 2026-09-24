#!/usr/bin/env python3
"""Daily universe scanner: Donchian+ATR grid, OOS grade, optional auto-ARMED.

  python scanner.py --state-dir ./dry_run --top 15 --smoke --no-arm
"""
from __future__ import annotations

import argparse
import json
import time
from itertools import product
from pathlib import Path
from typing import Any

from indicators import (
    BLACKLIST,
    OBSERVE_ONLY,
    VISION,
    add_donch_atr,
    base_of,
    buy_hold_return,
    fetch_klines,
    fetch_klines_deep,
    http_json,
    http_json_multi,
    is_leveraged_or_stable,
    metrics,
    now_iso_taipei,
    now_taipei,
    run_long_only,
    signal_donchian,
    split_closed,
    walk_forward_oos,
)

ROOT = Path(__file__).resolve().parent

DONCH_NS = (20, 55)
TFS = ("1h", "4h")
STOPS = (1.5, 2.0, 2.5)
TRAILS = (1.5, 2.0, 2.5, 3.0)

SMOKE_DONCH = (20, 55)
SMOKE_TFS = ("4h", "1h")
SMOKE_STOPS = (1.5, 2.0)
SMOKE_TRAILS = (1.5, 3.0)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    with path.open() as f:
        return json.load(f)


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def fetch_universe(top_n: int) -> list[dict]:
    rows = http_json_multi("/api/v3/ticker/24hr")
    out: list[dict] = []
    for r in rows:
        sym = r.get("symbol") or ""
        if not sym.endswith("USDT"):
            continue
        if is_leveraged_or_stable(sym):
            continue
        base = base_of(sym)
        out.append({
            "symbol": sym,
            "base": base,
            "quote_volume_24h": float(r.get("quoteVolume") or 0),
            "last_price": float(r.get("lastPrice") or 0),
            "blacklist": base in BLACKLIST,
            "observe_only": base in OBSERVE_ONLY,
        })
    out.sort(key=lambda x: x["quote_volume_24h"], reverse=True)
    return out[:top_n]


def max_hold(tf: str, donch_n: int) -> int:
    if tf == "1h":
        return 192 if donch_n >= 55 else 96
    return 48 if donch_n >= 55 else 36


def bars_per_year(tf: str) -> float:
    return (24 * 365.25) if tf == "1h" else (6 * 365.25)


def eval_variant(raw, base: str, tf: str, donch_n: int, stop_m: float, trail_m: float) -> dict | None:
    d = add_donch_atr(raw, donch_n).dropna(subset=["atr", "donch_hi", "donch_lo"])
    if len(d) < 120:
        return None
    sig = signal_donchian(d)
    eq, trades = run_long_only(d, sig, stop_m, trail_m, max_hold(tf, donch_n))
    bh = buy_hold_return(d)
    m = metrics(eq, trades, bh, bars_per_year(tf))
    if not m:
        return None
    oos = walk_forward_oos(raw, donch_n, stop_m, trail_m, tf)
    return {
        "variant": f"donchian{donch_n}_s{stop_m}_t{trail_m}__{base}__{tf}",
        "tf": tf,
        "donch_n": donch_n,
        "stop_atr_mult": stop_m,
        "trail_atr_mult": trail_m,
        "is_return_pct": m["total_return_pct"],
        "bh_return_pct": m["buy_hold_return_pct"],
        "beats_bh": m["beats_bh"],
        "max_drawdown_pct": m["max_drawdown_pct"],
        "n_trades": m["n_trades"],
        "sharpe": m["sharpe"],
        "oos_wins": oos.get("oos_wins", 0),
        "oos_folds": oos.get("oos_folds", 0),
        "oos_status": oos.get("status"),
    }


def grade_item(best: dict | None, *, blacklist: bool, observe: bool) -> str:
    if blacklist:
        return "REJECT"
    if observe:
        return "WATCH"
    if not best:
        return "WATCH"
    is_ok = bool(best.get("beats_bh")) and float(best.get("is_return_pct") or 0) > 0
    dd_ok = abs(float(best.get("max_drawdown_pct") or 99)) <= 45.0
    trades_ok = int(best.get("n_trades") or 0) >= 12
    oos_w = int(best.get("oos_wins") or 0)
    oos_f = int(best.get("oos_folds") or 0)
    oos_ok = oos_f >= 4 and oos_w >= 4
    if is_ok and dd_ok and trades_ok and oos_ok:
        return "QUALIFIED"
    if is_ok and dd_ok and (oos_w >= 3 or trades_ok):
        return "CANDIDATE"
    return "WATCH"


def score_key(v: dict) -> tuple:
    return (
        int(v.get("oos_wins") or 0),
        float(v.get("is_return_pct") or 0) - float(v.get("bh_return_pct") or 0),
        -abs(float(v.get("max_drawdown_pct") or 99)),
        float(v.get("sharpe") or 0),
    )


def dist_to_breakout(symbol: str, tf: str, donch_n: int) -> dict:
    try:
        raw = fetch_klines(symbol, tf, limit=max(200, donch_n + 50))
        closed, forming = split_closed(raw, tf)
        ind = add_donch_atr(closed, donch_n).dropna(subset=["atr", "donch_hi"])
        if ind.empty:
            return {}
        bar = ind.iloc[-1]
        atr = float(bar["atr"])
        close = float(bar["Close"])
        hi = float(bar["donch_hi"])
        mark = float(forming["Close"]) if forming is not None else close
        return {
            "mark": mark,
            "donch_hi": hi,
            "dist_to_breakout_atr": round((close - hi) / atr, 4) if atr else None,
        }
    except Exception:  # noqa: BLE001
        return {}


def build_grid(smoke: bool) -> list[tuple]:
    if smoke:
        return list(product(SMOKE_DONCH, SMOKE_TFS, SMOKE_STOPS, SMOKE_TRAILS))
    return list(product(DONCH_NS, TFS, STOPS, TRAILS))


def scan_symbol(item: dict, grid: list[tuple], deep_bars: int) -> dict:
    base = item["base"]
    symbol = item["symbol"]
    variants: list[dict] = []
    frames: dict[str, Any] = {}
    for tf in sorted({g[1] for g in grid}):
        try:
            frames[tf] = fetch_klines_deep(symbol, tf, target=deep_bars)
            time.sleep(0.05)
        except Exception as e:  # noqa: BLE001
            return {**item, "grade": "WATCH", "error": f"fetch {tf}: {e}"}

    for donch_n, tf, stop_m, trail_m in grid:
        raw = frames.get(tf)
        if raw is None or getattr(raw, "empty", True):
            continue
        try:
            v = eval_variant(raw, base, tf, donch_n, stop_m, trail_m)
            if v:
                variants.append(v)
        except Exception:  # noqa: BLE001
            continue

    best = max(variants, key=score_key) if variants else None
    out = {
        **item,
        "best_variant": best.get("variant") if best else None,
        "tf": best.get("tf") if best else None,
        "donch_n": best.get("donch_n") if best else None,
        "stop_atr_mult": best.get("stop_atr_mult") if best else None,
        "trail_atr_mult": best.get("trail_atr_mult") if best else None,
        "is_return_pct": best.get("is_return_pct") if best else None,
        "bh_return_pct": best.get("bh_return_pct") if best else None,
        "beats_bh": best.get("beats_bh") if best else None,
        "oos_wins": best.get("oos_wins") if best else 0,
        "oos_folds": best.get("oos_folds") if best else 0,
        "max_drawdown_pct": best.get("max_drawdown_pct") if best else None,
        "n_trades": best.get("n_trades") if best else 0,
        "sharpe": best.get("sharpe") if best else None,
        "grade": grade_item(best, blacklist=item["blacklist"], observe=item["observe_only"]),
        "n_variants_tested": len(variants),
    }
    if best:
        out.update(dist_to_breakout(symbol, best["tf"], int(best["donch_n"])))
    return out


def maybe_arm(watchlist: dict, state_dir: Path) -> list[str]:
    news = load_json(state_dir / "news_light.json", {"light": "GREEN"})
    if (news.get("light") or "GREEN").upper() != "GREEN":
        return ["skip arm: light not GREEN"]
    orders = load_json(state_dir / "orders.json", {"orders": []})
    positions = load_json(state_dir / "positions.json", {"positions": []})
    held = {p["symbol"] for p in positions.get("positions") or [] if p.get("status") == "FILLED"}
    busy = {
        o["symbol"] for o in orders.get("orders") or []
        if o.get("status") in ("ARMED", "SIGNAL", "PENDING", "FILLED", "PAUSED")
    }
    used_slots = {
        o.get("slot") for o in orders.get("orders") or []
        if o.get("status") in ("ARMED", "SIGNAL", "PENDING", "FILLED", "PAUSED")
    }
    free = [s for s in ("satellite_A", "satellite_B") if s not in used_slots]
    if not free:
        return ["no free satellite slots"]
    quals = [
        it for it in watchlist.get("items") or []
        if it.get("grade") == "QUALIFIED"
        and it["symbol"] not in held
        and it["symbol"] not in busy
        and not it.get("blacklist")
        and not it.get("observe_only")
    ]
    quals.sort(key=lambda x: (x.get("oos_wins") or 0, x.get("is_return_pct") or 0), reverse=True)
    log: list[str] = []
    for it, slot in zip(quals, free):
        quote = 1250.0 if slot == "satellite_A" else 1000.0
        orders.setdefault("orders", []).append({
            "id": f"ord_{base_of(it['symbol']).lower()}",
            "slot": slot,
            "symbol": it["symbol"],
            "status": "ARMED",
            "variant": it["best_variant"],
            "quote_usdt": quote,
            "tf": it["tf"],
            "donch_n": it["donch_n"],
            "stop_atr_mult": it["stop_atr_mult"],
            "trail_atr_mult": it["trail_atr_mult"],
            "mark": it.get("mark"),
            "armed_at": now_iso_taipei(),
            "source": "scanner_auto",
        })
        log.append(f"ARMED {it['symbol']} -> {slot} {it['best_variant']}")
    orders["updated_at"] = now_iso_taipei()
    save_json(state_dir / "orders.json", orders)
    return log or ["no QUALIFIED to arm"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", type=Path, default=ROOT / "dry_run")
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--no-arm", action="store_true")
    ap.add_argument("--deep-bars", type=int, default=0)
    args = ap.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True)

    deep = args.deep_bars or (3000 if args.smoke else 5000)
    grid = build_grid(args.smoke)
    universe = fetch_universe(args.top)
    print(f"universe={len(universe)} grid={len(grid)} smoke={args.smoke} @ {now_taipei()}")

    items: list[dict] = []
    for i, u in enumerate(universe, 1):
        print(f"[{i}/{len(universe)}] {u['symbol']} ...", flush=True)
        if u["blacklist"]:
            items.append({**u, "grade": "REJECT", "best_variant": None, "note": "blacklist"})
            continue
        items.append(scan_symbol(u, grid, deep))

    watchlist = {
        "updated_at": now_iso_taipei(),
        "universe_n": args.top,
        "scanned": len(items),
        "smoke": args.smoke,
        "grid_size": len(grid),
        "items": items,
    }
    save_json(args.state_dir / "watchlist.json", watchlist)

    arm_log: list[str] = []
    if not args.no_arm:
        arm_log = maybe_arm(watchlist, args.state_dir)

    health = load_json(args.state_dir / "health.json", {})
    health["last_scanner_at"] = now_iso_taipei()
    health["last_scanner_ok"] = True
    save_json(args.state_dir / "health.json", health)

    grades = {g: sum(1 for it in items if it.get("grade") == g)
              for g in ("QUALIFIED", "CANDIDATE", "WATCH", "REJECT")}
    preview = [
        {k: it.get(k) for k in (
            "symbol", "grade", "best_variant", "is_return_pct",
            "oos_wins", "oos_folds", "max_drawdown_pct", "dist_to_breakout_atr"
        )}
        for it in sorted(
            [x for x in items if x.get("best_variant")],
            key=lambda x: (x.get("grade") == "QUALIFIED", x.get("oos_wins") or 0),
            reverse=True,
        )[:10]
    ]
    summary = {"checked_at": now_taipei(), "arm_log": arm_log, "grades": grades, "top_preview": preview}
    save_json(args.state_dir / "scanner_last.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
