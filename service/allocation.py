"""Allocation file load + validate. Used by API, job, and sync-allocation workflow."""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("trader.allocation")

# Catalog family ids the Cloud Run job can execute today
RUNNER_FAMILIES = frozenset({
    "donchian_atr",
    "donchian_btc_regime",
    "ema_cross_atr",
    # Futures families re-locked until futures-order-probe passes
    # "donchian_lev_vol", "donchian_long_short_btc_regime", "donchian_fear_greed",
})
FUTURES_FAMILIES = frozenset({
    "donchian_lev_vol",
    "donchian_long_short_btc_regime",
    "donchian_lev",
    "donchian_fear_greed",  # lev>1 rows need futures; family not in RUNNER yet
})
# Map legacy slot family names → catalog ids
FAMILY_ALIASES = {
    "donchian": "donchian_atr",
    "donchian_atr": "donchian_atr",
    "donchian_btc_regime": "donchian_btc_regime",
    "ema_cross_atr": "ema_cross_atr",
    "donchian_fear_greed": "donchian_fear_greed",
    "donchian_lev_vol": "donchian_lev_vol",
    "donchian_long_short_btc_regime": "donchian_long_short_btc_regime",
    "donchian_lev": "donchian_lev",
}

DEFAULT_BOOK = 5000.0
DEFAULT_MAX_ORDER = 1500.0
REPO_ALLOC_PATH = Path(__file__).resolve().parents[1] / "config" / "allocation.json"
GCS_OBJECT = "trader/allocation.json"


def normalize_family(family: str | None) -> str:
    f = (family or "").strip().lower()
    return FAMILY_ALIASES.get(f, f)


def _require(slot: dict, key: str, errors: list[str], idx: int) -> Any:
    if key not in slot or slot[key] is None or slot[key] == "":
        errors.append(f"slots[{idx}].{key} 必填")
        return None
    return slot[key]


def _check_atr_mode(p: dict, errors: list[str], idx: int) -> None:
    if "atr_mode" in p and p.get("atr_mode") is not None:
        mode = str(p.get("atr_mode") or "").strip().lower()
        if mode not in ("sma", "wilder"):
            errors.append(
                f"slots[{idx}].params.atr_mode 必須是 sma 或 wilder，收到：{p.get('atr_mode')!r}"
            )


def _stop_trail_keys(p: dict) -> tuple:
    """Accept stop_atr_mult/trail_atr_mult or catalog stop_m/trail_m."""
    stop = p.get("stop_atr_mult", p.get("stop_m"))
    trail = p.get("trail_atr_mult", p.get("trail_m"))
    return stop, trail


def validate_params(family: str, params: dict | None, errors: list[str], idx: int) -> None:
    p = params if isinstance(params, dict) else {}
    if family == "donchian_atr":
        for k in ("donch_n", "stop_atr_mult", "trail_atr_mult"):
            if k not in p and not (k == "stop_atr_mult" and "stop_m" in p) and not (
                k == "trail_atr_mult" and "trail_m" in p
            ):
                if k == "stop_atr_mult" and p.get("stop_m") is not None:
                    continue
                if k == "trail_atr_mult" and p.get("trail_m") is not None:
                    continue
                if k in p:
                    continue
                # require either naming
                if k == "donch_n" and "donch_n" not in p:
                    errors.append(f"slots[{idx}].params.{k} 必填（donchian_atr）")
                elif k == "stop_atr_mult" and p.get("stop_m") is None and p.get("stop_atr_mult") is None:
                    errors.append(f"slots[{idx}].params.stop_atr_mult/stop_m 必填（donchian_atr）")
                elif k == "trail_atr_mult" and p.get("trail_m") is None and p.get("trail_atr_mult") is None:
                    errors.append(f"slots[{idx}].params.trail_atr_mult/trail_m 必填（donchian_atr）")
        n = p.get("donch_n")
        if n is not None and (not isinstance(n, (int, float)) or n < 5 or n > 200):
            errors.append(f"slots[{idx}].params.donch_n 超出範圍")
        _check_atr_mode(p, errors, idx)
        if "require_reset_below_hi" in p and p.get("require_reset_below_hi") is not None:
            if not isinstance(p.get("require_reset_below_hi"), bool):
                errors.append(f"slots[{idx}].params.require_reset_below_hi 必須是 boolean")
    elif family == "donchian_btc_regime":
        if "donch_n" not in p:
            errors.append(f"slots[{idx}].params.donch_n 必填（donchian_btc_regime）")
        _check_atr_mode(p, errors, idx)
    elif family == "ema_cross_atr":
        fast = p.get("ema_fast", p.get("fast"))
        slow = p.get("ema_slow", p.get("slow"))
        if fast is None or slow is None:
            errors.append(f"slots[{idx}].params.ema_fast/fast 與 ema_slow/slow 必填（ema_cross_atr）")
        stop, trail = _stop_trail_keys(p)
        if stop is None or trail is None:
            errors.append(f"slots[{idx}].params.stop/trail 倍數必填（ema_cross_atr）")
        _check_atr_mode(p, errors, idx)
    elif family == "donchian_fear_greed":
        if "donch_n" not in p:
            errors.append(f"slots[{idx}].params.donch_n 必填（donchian_fear_greed）")
        stop, trail = _stop_trail_keys(p)
        if stop is None or trail is None:
            errors.append(f"slots[{idx}].params.stop/trail 倍數必填（donchian_fear_greed）")
        fg = str(p.get("fg_mode") or "none").strip().lower()
        if fg not in ("none", "gt25", "lt75", "mid25_75", "fggt25", "fglt75", "fgmid25_75", "mid"):
            errors.append(f"slots[{idx}].params.fg_mode 不支援：{p.get('fg_mode')!r}")
        lev = float(p.get("leverage") or 1.0)
        if lev > 3 + 1e-9:
            errors.append(f"slots[{idx}].params.leverage={lev} 超過硬頂 3")
        _check_atr_mode(p, errors, idx)
        if "require_reset_below_hi" in p and p.get("require_reset_below_hi") is not None:
            if not isinstance(p.get("require_reset_below_hi"), bool):
                errors.append(f"slots[{idx}].params.require_reset_below_hi 必須是 boolean")
    elif family == "donchian_lev_vol":
        if "donch_n" not in p:
            errors.append(f"slots[{idx}].params.donch_n 必填（donchian_lev_vol）")
        stop, trail = _stop_trail_keys(p)
        if stop is None or trail is None:
            errors.append(f"slots[{idx}].params.stop/trail 倍數必填（donchian_lev_vol）")
        lev = float(p.get("leverage") or 1.0)
        if lev > 3 + 1e-9:
            errors.append(f"slots[{idx}].params.leverage={lev} 超過硬頂 3")
        _check_atr_mode(p, errors, idx)
    elif family == "donchian_long_short_btc_regime":
        if "donch_n" not in p:
            errors.append(f"slots[{idx}].params.donch_n 必填（donchian_long_short_btc_regime）")
        stop, trail = _stop_trail_keys(p)
        if stop is None or trail is None:
            errors.append(f"slots[{idx}].params.stop/trail 倍數必填（donchian_long_short_btc_regime）")
        lev = float(p.get("leverage") or 1.0)
        if lev > 3 + 1e-9:
            errors.append(f"slots[{idx}].params.leverage={lev} 超過硬頂 3")
        _check_atr_mode(p, errors, idx)
    elif family in FUTURES_FAMILIES:
        errors.append(f"slots[{idx}].family 合約支援準備中：{family}")
    else:
        errors.append(f"slots[{idx}].family 雲端尚未支援：{family}")



def fetch_usdt_symbols() -> set[str] | None:
    """Return Binance spot USDT symbols, or None if network failed (skip soft-check)."""
    url = os.environ.get("BINANCE_EXCHANGE_INFO_URL") or "https://api.binance.com/api/v3/exchangeInfo"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310
            data = json.loads(resp.read().decode())
        out = set()
        for s in data.get("symbols") or []:
            if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
                out.add(s.get("symbol"))
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("exchangeInfo_fail err=%s", e)
        return None


def validate_allocation(
    doc: dict,
    *,
    check_binance: bool = True,
    usdt_symbols: set[str] | None = None,
) -> list[str]:
    """Return list of error strings; empty means OK."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["allocation 必須是 JSON object"]
    book = float(doc.get("book_usdt") or DEFAULT_BOOK)
    max_order = float(doc.get("max_notional_per_order_usdt") or DEFAULT_MAX_ORDER)
    slots = doc.get("slots")
    if not isinstance(slots, list) or not slots:
        return ["slots 必須是非空陣列"]

    seen_slot: set[str] = set()
    seen_sid: set[str] = set()
    seen_sym_enabled: set[str] = set()
    total_enabled = 0.0

    if check_binance and usdt_symbols is None:
        usdt_symbols = fetch_usdt_symbols()

    for i, slot in enumerate(slots):
        if not isinstance(slot, dict):
            errors.append(f"slots[{i}] 必須是 object")
            continue
        slot_id = _require(slot, "slot", errors, i)
        family = normalize_family(_require(slot, "family", errors, i) or "")
        sid = _require(slot, "strategy_id", errors, i)
        symbol = _require(slot, "symbol", errors, i)
        tf = _require(slot, "timeframe", errors, i)
        notion = slot.get("notional_usdt")
        enabled = slot.get("enabled")
        params = slot.get("params")

        if slot_id:
            if slot_id in seen_slot:
                errors.append(f"重複 slot：{slot_id}")
            seen_slot.add(str(slot_id))
        if sid:
            if sid in seen_sid:
                errors.append(f"重複 strategy_id：{sid}")
            seen_sid.add(str(sid))

        is_en = enabled is True

        if symbol:
            sym = str(symbol).upper()
            if not sym.endswith("USDT"):
                errors.append(f"slots[{i}].symbol 必須是 USDT 交易對：{symbol}")
            # 僅對 enabled slot 檢查重複／交易所存在（候選可暫列同幣）
            if is_en:
                if sym in seen_sym_enabled:
                    errors.append(f"重複 symbol（enabled）：{sym}")
                seen_sym_enabled.add(sym)
                if usdt_symbols is not None and sym not in usdt_symbols:
                    errors.append(f"Binance 現貨找不到或未交易：{sym}")

        if tf is not None and str(tf).lower() not in {"1h", "4h", "1d"}:
            errors.append(f"slots[{i}].timeframe 不支援：{tf}")

        if not isinstance(enabled, bool):
            errors.append(f"slots[{i}].enabled 必須是 boolean")

        try:
            notion_f = float(notion)
        except Exception:  # noqa: BLE001
            errors.append(f"slots[{i}].notional_usdt 必須是數字")
            notion_f = 0.0
        if notion_f <= 0:
            errors.append(f"slots[{i}].notional_usdt 必須 > 0")
        if notion_f > max_order + 1e-9:
            errors.append(
                f"slots[{i}].notional_usdt {notion_f} 超過單筆上限 {max_order}"
            )
        if is_en:
            total_enabled += max(notion_f, 0.0)

        if family:
            if family not in RUNNER_FAMILIES:
                errors.append(f"slots[{i}].family 雲端尚未支援：{family}")
            else:
                validate_params(family, params if isinstance(params, dict) else {}, errors, i)

    if total_enabled > book + 1e-6:
        errors.append(
            f"enabled notional 加總 {total_enabled:.2f} 超過 book_usdt {book:.2f}"
        )

    return errors


def load_json_file(path: Path | str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_repo_allocation() -> dict:
    return load_json_file(REPO_ALLOC_PATH)


def load_allocation_from_gcs(bucket: str | None = None) -> dict | None:
    bucket = bucket or os.environ.get("GCS_BUCKET") or ""
    if not bucket:
        return None
    try:
        from google.cloud import storage

        blob = storage.Client().bucket(bucket).blob(GCS_OBJECT)
        if not blob.exists():
            return None
        return json.loads(blob.download_as_text())
    except Exception as e:  # noqa: BLE001
        log.warning("allocation_gcs_load_fail err=%s", e)
        return None


def save_allocation_to_gcs(doc: dict, bucket: str | None = None) -> None:
    bucket = bucket or os.environ.get("GCS_BUCKET") or ""
    if not bucket:
        raise RuntimeError("GCS_BUCKET 未設定")
    from google.cloud import storage

    payload = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    blob = storage.Client().bucket(bucket).blob(GCS_OBJECT)
    blob.upload_from_string(payload, content_type="application/json")


def resolve_allocation() -> tuple[dict, str]:
    """Prefer GCS, fall back to repo file. Returns (doc, source)."""
    gcs = load_allocation_from_gcs()
    if gcs:
        return gcs, "gcs"
    return load_repo_allocation(), "repo"


def live_slots(
    doc: dict,
    approved_families: list[str] | set[str] | None,
) -> list[dict]:
    """Slots that may place new orders: enabled + family approved."""
    approved = {normalize_family(f) for f in (approved_families or [])}
    out = []
    for s in doc.get("slots") or []:
        if not s.get("enabled"):
            continue
        fam = normalize_family(s.get("family"))
        if fam not in approved:
            continue
        row = dict(s)
        row["family"] = fam
        row["order_mode"] = "live"
        out.append(row)
    return out


def signal_only_slots(
    doc: dict,
    approved_families: list[str] | set[str] | None,
) -> list[dict]:
    """Enabled but family not approved, or explicitly disabled candidates of interest."""
    approved = {normalize_family(f) for f in (approved_families or [])}
    out = []
    for s in doc.get("slots") or []:
        fam = normalize_family(s.get("family"))
        row = dict(s)
        row["family"] = fam
        if s.get("enabled") and fam not in approved:
            row["order_mode"] = "signal_only"
            out.append(row)
        elif not s.get("enabled"):
            row["order_mode"] = "disabled"
            # keep as candidate listing optional — caller may filter
            out.append(row)
    return out


def slot_to_runtime(slot: dict) -> dict:
    """Normalize allocation slot → runner slot shape (compatible with legacy SLOTS)."""
    params = slot.get("params") or {}
    symbol = str(slot.get("symbol") or "").upper()
    tf = str(slot.get("timeframe") or "").lower()
    fam = normalize_family(slot.get("family"))
    # EMA catalog default atr_mode=sma; others wilder
    default_atr = "sma" if fam == "ema_cross_atr" else "wilder"
    _atr_mode = str(params.get("atr_mode") or default_atr).strip().lower()
    if _atr_mode not in ("sma", "wilder"):
        raise ValueError(f"atr_mode 必須是 sma 或 wilder，收到：{params.get('atr_mode')!r}")
    stop = params.get("stop_atr_mult", params.get("stop_m"))
    trail = params.get("trail_atr_mult", params.get("trail_m"))
    max_hold = params.get("max_hold_bars", params.get("max_hold"))
    reset = params.get("require_reset_below_hi", params.get("reset_below_hi", False))
    return {
        "id": slot.get("slot"),
        "strategy_id": slot.get("strategy_id"),
        "family": fam,
        "symbol": symbol,
        "tf": tf,
        "donch_n": params.get("donch_n"),
        "ema_fast": params.get("ema_fast", params.get("fast")),
        "ema_slow": params.get("ema_slow", params.get("slow")),
        "stop_atr_mult": stop,
        "trail_atr_mult": trail,
        "max_hold_bars": max_hold,
        "quote_usdt": float(slot.get("notional_usdt") or 0),
        "require_reset_below_hi": bool(reset),
        "atr_mode": _atr_mode,
        "btc_regime": bool(params.get("btc_regime", False)),
        "fg_mode": params.get("fg_mode") or "none",
        "leverage": float(params.get("leverage") or 1.0),
        "vol_target": params.get("vol_target"),
        "kind": params.get("kind") or "long",
        "armed": True,
        "mode": slot.get("order_mode") or ("live" if slot.get("enabled") else "signal_only"),
        "note": slot.get("note"),
    }


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else str(REPO_ALLOC_PATH)
    doc = load_json_file(path)
    errs = validate_allocation(doc, check_binance=True)
    if errs:
        print("INVALID")
        for e in errs:
            print(" -", e)
        sys.exit(1)
    print("OK", path)
