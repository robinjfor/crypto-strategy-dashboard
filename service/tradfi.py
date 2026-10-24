"""Crypto-only universe for the Demo strategies (Robin 2026-10-24): Binance USD-M also lists TradFi perps (stocks,
ETFs, commodities, FX, pre-IPO: contractType TRADIFI_PERPETUAL; trading them needs a separate agreement -> -4411).
Only crypto USDT/USDC perps (contractType PERPETUAL, underlyingType COIN/INDEX/crypto PREMARKET) are tradable.
Some tickers were crypto before being re-used for TradFi (MDT, BOT, PENG...): history before the TradFi onboardDate
stays crypto, from it on the symbol is masked."""
from __future__ import annotations

import json
from pathlib import Path

TRADFI_UNDERLYING = {"EQUITY", "HK_EQUITY", "KR_EQUITY", "CN_EQUITY", "COMMODITY", "FX"}
# manual overrides (Robin 2026-10-24): gold-backed tokens are excluded over their whole history; SPX (SPX6900 meme coin) is crypto
EXTRA = {"PAXGUSDT": 0, "XAUTUSDT": 0}
ALLOW = {"SPXUSDT"}
# no TradFi perp existed before XAUUSDT (2025-12-11): an older onboardDate is a re-used crypto ticker's (e.g. demo OMGUSDT
# 2019-09-25 = OMG Network, now a pending Pre-IPO contract) -> the TradFi period starts no earlier than this
EPOCH_MS = 1764547200000   # 2025-12-01 UTC
STATIC = Path(__file__).with_name("tradfi_symbols.json")   # {sym: onboard_ms}, snapshot of fapi+demo-fapi exchangeInfo


def is_tradfi_meta(s: dict) -> bool:
    ct = str(s.get("contractType") or "").upper()
    return ct.startswith("TRADIFI") or str(s.get("underlyingType") or "").upper() in TRADFI_UNDERLYING \
        or "TradFi" in (s.get("underlyingSubType") or [])


def is_crypto_perp(s: dict) -> bool:
    if s.get("symbol") in ALLOW:
        return str(s.get("contractType") or "") == "PERPETUAL"
    if s.get("symbol") in EXTRA:
        return False
    return str(s.get("contractType") or "") == "PERPETUAL" and not is_tradfi_meta(s) \
        and str(s.get("quoteAsset") or "") in ("USDT", "USDC")


def tradfi_map(*infos: dict | None, static: bool = True) -> dict[str, int]:
    """{symbol: TradFi onboardDate ms} from exchangeInfo docs (+ the committed snapshot)."""
    out: dict[str, int] = {}
    if static:
        try:
            out.update({k: int(v) for k, v in json.loads(STATIC.read_text()).items()})
        except Exception:  # noqa: BLE001
            pass
    for info in infos:
        for s in (info or {}).get("symbols") or []:
            if is_tradfi_meta(s):
                t = int(s.get("onboardDate") or 0)
                out[s["symbol"]] = min(out.get(s["symbol"], t), t) if s["symbol"] in out and t else (t or out.get(s["symbol"], 0))
    out = {k: max(v, EPOCH_MS) for k, v in out.items()}
    out.update(EXTRA)
    for a in ALLOW:
        out.pop(a, None)
    return out


def blocked(state: dict | None) -> set[str]:
    """symbols that answered -4411 (TradFi agreement) once: never tried again."""
    return set((state or {}).get("tradfi_blocked") or [])


def note_error(state: dict | None, sym: str, err: str) -> bool:
    if state is not None and ("-4411" in str(err) or "TradFi" in str(err)):
        b = sorted(blocked(state) | {sym}); state["tradfi_blocked"] = b
        return True
    return False


def is_tradfi(sym: str, info: dict | None = None, state: dict | None = None) -> bool:
    if sym in blocked(state) or sym in EXTRA:
        return True
    if sym in ALLOW:
        return False
    for s in (info or {}).get("symbols") or []:
        if s.get("symbol") == sym:
            return is_tradfi_meta(s)
    return sym in tradfi_map(static=True)
