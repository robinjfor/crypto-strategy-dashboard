#!/usr/bin/env python3
"""External reviewer surface: streamable-HTTP MCP plus REST mirrors.

Reviewers (ChatGPT, Claude, or another named reviewer) may read the same
Binance Demo figures the dashboard /status route uses, and may leave comments.
They cannot approve, trade, revoke, or change settings. Comments live in the
private GCS object research/reviews/comments.json (a local file when
GCS_BUCKET is unset). Objects stay private.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from strategy import now_iso_taipei

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
DEFAULT_PROTOCOL = "2025-03-26"
SERVER_NAME = "crypto-strategy-review"
SERVER_VERSION = "1.0.0"

INSTRUCTIONS = (
    "Read-only reviewer for the Binance Demo crypto book (never live funds). "
    "Call list_strategies, get_performance, get_positions, get_trades, get_backtest, "
    "get_reconciliation, get_risk, and get_orchestrator_plan before judging. "
    "Numbers are the dashboard /status feed. "
    "Leave findings with post_comment (reviewer chatgpt, claude, or auto_trader). "
    "You cannot approve, trade, revoke, or change settings. Times are Asia/Taipei. "
    "Critique strategy logic, overfitting, risk, and live-versus-backtest deviation."
)

REVIEWERS = {"chatgpt", "claude", "auto_trader", "other"}
REVIEWER_LABELS = {
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "auto_trader": "自動交易專家",
    "team": "團隊",
    "other": "其他",
}
_REPLY_ID = re.compile(r"^r_[0-9a-f]{16}$")
SEVERITIES = {"info", "low", "medium", "high", "critical"}
STATUSES = {"open", "acknowledged", "done", "rejected"}
_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_COMMENT_ID = re.compile(r"^c_[0-9a-f]{16}$")
_SECRET_KEY = re.compile(
    r"(api[_-]?key|api[_-]?secret|apisecret|secret|password|passwd|\bpin\b|"
    r"token|authorization|credential|private[_-]?key|account[_-]?id|accountid|"
    r"listen[_-]?key|\bsignature\b)",
    re.I,
)

COMMENTS_OBJECT = "research/reviews/comments.json"
TOKEN_MIN_LEN = 32
MAX_COMMENTS = 500
DEFAULT_BODY_MAX = 8000
DEFAULT_REPLY_MAX = 4000
DEFAULT_ACTION_MAX = 2000
DEFAULT_TOPIC_MAX = 160

_limit_lock = threading.Lock()
_buckets: dict[str, list[float]] = {}
_comment_lock = threading.Lock()


def reviewer_token() -> str:
    return (os.environ.get("REVIEWER_TOKEN") or "").strip()


def reviewer_configured() -> bool:
    return len(reviewer_token()) >= TOKEN_MIN_LEN


def is_reviewer_token(token: str | None) -> bool:
    expected = reviewer_token()
    if not expected or len(expected) < TOKEN_MIN_LEN or not token:
        return False
    got = str(token).strip()
    if len(got) != len(expected):
        return False
    return hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8"))


def query_token_enabled() -> bool:
    return (os.environ.get("REVIEW_ALLOW_QUERY_TOKEN") or "1") != "0"


def reset_limits() -> None:
    with _limit_lock:
        _buckets.clear()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _charge(ip: str, slots: list[tuple[str, int, float]]) -> float | None:
    """Record one hit across slots. Return retry-after seconds when any slot is full."""
    now = time.time()
    ip = (ip or "unknown")[:64]
    with _limit_lock:
        current: list[tuple[str, list[float], int, float]] = []
        for name, max_n, window in slots:
            key = f"{name}|{ip}"
            bucket = [t for t in _buckets.get(key, []) if now - t < window]
            current.append((key, bucket, max_n, window))
        for key, bucket, max_n, window in current:
            if max_n >= 0 and len(bucket) >= max_n and bucket:
                retry = window - (now - bucket[0])
                _buckets[key] = bucket
                return max(1.0, retry)
        for key, bucket, _max_n, _window in current:
            bucket.append(now)
            _buckets[key] = bucket
    return None


def rate_limit(ip: str, kind: str) -> float | None:
    if kind == "write":
        return _charge(ip, [
            ("write-burst", _env_int("REVIEW_WRITE_BURST", 8), _env_float("REVIEW_WRITE_BURST_SEC", 60)),
            ("write-hour", _env_int("REVIEW_WRITE_MAX", 30), _env_float("REVIEW_WRITE_WINDOW_SEC", 3600)),
        ])
    return _charge(ip, [
        ("read", _env_int("REVIEW_READ_MAX", 120), _env_float("REVIEW_READ_WINDOW_SEC", 60)),
    ])


def _scrub(text: str) -> str:
    tok = reviewer_token()
    if tok and tok in text:
        text = text.replace(tok, "[redacted]")
    if "-----BEGIN" in text:
        return "[redacted]"
    return text


def redact(obj: Any) -> Any:
    """Replace secret-shaped keys and any copy of REVIEWER_TOKEN. Keep numbers."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _SECRET_KEY.search(str(k)):
                out[str(k)] = "[redacted]"
            else:
                out[str(k)] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return _scrub(obj)
    return obj


def redact_text(text: str) -> str:
    return _scrub(text or "")


def _clip(raw: Any, limit: int) -> str:
    return str(raw or "").strip()[:limit]


def display_name(author: str | None) -> str:
    key = str(author or "")
    return REVIEWER_LABELS.get(key, key)


def normalize_reviewer(raw: Any, *, allow_team: bool = False) -> str | None:
    s = str(raw or "").strip().lower()
    if s == "team":
        return "team" if allow_team else None
    if s in REVIEWERS or _SLUG.match(s):
        return s
    return None


class Outcome:
    def __init__(self, body: dict, status: int = 200, retry_after: float | None = None):
        self.body = body
        self.status = status
        self.retry_after = retry_after


class CommentStore:
    """JSON comment log. GCS when GCS_BUCKET is set, otherwise a local file.

    The object stays private. This store does not publish it.
    """

    def __init__(self, bucket: str | None = None, local_path: str | None = None):
        self.bucket = bucket if bucket is not None else (os.environ.get("GCS_BUCKET") or "")
        self.object_name = COMMENTS_OBJECT
        self.local_path = Path(
            local_path or os.environ.get("REVIEW_COMMENTS_PATH") or "/tmp/review_comments.json"
        )
        self._client = None
        self._generation: int | None = None

    def _gcs(self):
        if self._client is None:
            from google.cloud import storage
            self._client = storage.Client()
        return self._client

    def load(self) -> dict:
        if self.bucket:
            try:
                blob = self._gcs().bucket(self.bucket).blob(self.object_name)
                if blob.exists():
                    blob.reload()
                    self._generation = blob.generation
                    data = json.loads(blob.download_as_text())
                    if isinstance(data, dict) and isinstance(data.get("comments"), list):
                        return data
                else:
                    self._generation = 0
            except Exception:
                # Fall through to the local file. Save will still try GCS and raise
                # if the write cannot be persisted there.
                self._generation = None
        if self.local_path.is_file():
            try:
                data = json.loads(self.local_path.read_text())
                if isinstance(data, dict) and isinstance(data.get("comments"), list):
                    return data
            except Exception:
                pass
        return {"version": 1, "comments": []}

    def save(self, data: dict, *, if_generation_match: int | None = None) -> None:
        payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self.local_path.write_text(payload)
        if not self.bucket:
            return
        blob = self._gcs().bucket(self.bucket).blob(self.object_name)
        kwargs: dict[str, Any] = {"content_type": "application/json"}
        gen = if_generation_match if if_generation_match is not None else self._generation
        if gen is not None:
            kwargs["if_generation_match"] = gen
        blob.upload_from_string(payload, **kwargs)
        blob.reload()
        self._generation = blob.generation

    def mutate(self, fn: Callable[[dict], dict], retries: int = 5) -> dict:
        last: Exception | None = None
        with _comment_lock:
            for i in range(retries):
                state = self.load()
                gen = self._generation
                new_state = fn(json.loads(json.dumps(state)))
                try:
                    self.save(new_state, if_generation_match=gen)
                    return new_state
                except Exception as e:  # noqa: BLE001
                    last = e
                    time.sleep(0.05 * (i + 1))
        raise RuntimeError(f"comment_store_failed: {type(last).__name__ if last else 'unknown'}")


def _public_reply(r: dict) -> dict:
    author = str(r.get("author") or "")
    return {
        "id": r.get("id"),
        "author": author,
        "display_name": display_name(author),
        "body": r.get("body"),
        "created_at": r.get("created_at"),
        "timezone": "Asia/Taipei",
        "in_reply_to": r.get("in_reply_to"),
    }


def _thread_nodes(flat: list[dict]) -> list[dict]:
    """Nest replies under the comment. A reply with in_reply_to sits under that reply."""
    nodes: list[dict] = []
    by_id: dict[str, dict] = {}
    order: dict[str, int] = {}
    for i, r in enumerate(flat):
        node = {k: v for k, v in r.items() if k != "replies"}
        node["replies"] = []
        rid = str(r.get("id") or "")
        by_id[rid] = node
        order[rid] = i
        nodes.append(node)
    roots: list[dict] = []
    for node in nodes:
        parent = str(node.get("in_reply_to") or "")
        host = by_id.get(parent)
        if host is not None and order.get(parent, 10**9) < order.get(str(node.get("id") or ""), -1):
            host["replies"].append(node)
        else:
            roots.append(node)
    return roots


def _public_comment(c: dict) -> dict:
    flat = [_public_reply(r) for r in (c.get("replies") or []) if isinstance(r, dict)]
    return redact({
        "id": c.get("id"),
        "reviewer": c.get("reviewer"),
        "display_name": display_name(str(c.get("reviewer") or "")),
        "topic": c.get("topic"),
        "strategy": c.get("strategy"),
        "severity": c.get("severity"),
        "body": c.get("body"),
        "suggested_action": c.get("suggested_action"),
        "status": c.get("status"),
        "created_at": c.get("created_at"),
        "updated_at": c.get("updated_at"),
        "timezone": "Asia/Taipei",
        "replies": flat,
        "thread": _thread_nodes(flat),
        "status_events": c.get("status_events") or [],
    })


def _trim_store(comments: list) -> list:
    if len(comments) < MAX_COMMENTS:
        return comments
    closed = [c for c in comments if c.get("status") in ("done", "rejected")]
    openish = [c for c in comments if c.get("status") not in ("done", "rejected")]
    drop = len(comments) - MAX_COMMENTS + 1
    closed.sort(key=lambda c: str(c.get("created_at") or ""))
    kept_closed = closed[drop:] if drop < len(closed) else []
    if len(openish) + len(kept_closed) >= MAX_COMMENTS and len(openish) >= MAX_COMMENTS:
        return openish
    return openish + kept_closed


def list_comments_view(status_filter: str | None = None, limit: int = 50) -> Outcome:
    if status_filter and status_filter not in STATUSES:
        return Outcome({"ok": False, "error": "狀態須為 open、acknowledged、done 或 rejected"}, 400)
    try:
        limit_n = int(limit)
    except (TypeError, ValueError):
        return Outcome({"ok": False, "error": "limit 必須是整數"}, 400)
    limit_n = min(200, max(1, limit_n))
    data = CommentStore().load()
    rows = [c for c in data.get("comments") or [] if isinstance(c, dict)]
    if status_filter:
        rows = [c for c in rows if c.get("status") == status_filter]
    rows.sort(key=lambda c: str(c.get("created_at") or ""), reverse=True)
    pub = [_public_comment(c) for c in rows[:limit_n]]
    return Outcome({
        "ok": True,
        "timezone": "Asia/Taipei",
        "count": len(rows),
        "comments": pub,
    })


def post_comment(args: dict, ip: str) -> Outcome:
    if str(args.get("reviewer") or "").strip().lower() == "team":
        return Outcome({"ok": False, "error": "reviewer team 只能由儀表板登入使用"}, 400)
    reviewer = normalize_reviewer(args.get("reviewer"))
    if not reviewer:
        return Outcome({"ok": False, "error": "reviewer 須為 chatgpt、claude、auto_trader、other，或短名稱"}, 400)
    topic = _clip(args.get("topic") or args.get("strategy"), _env_int("REVIEW_TOPIC_MAX", DEFAULT_TOPIC_MAX))
    if not topic:
        return Outcome({"ok": False, "error": "缺少 topic 或 strategy"}, 400)
    strategy = _clip(args.get("strategy"), _env_int("REVIEW_TOPIC_MAX", DEFAULT_TOPIC_MAX)) or None
    if args.get("strategy") and strategy is None:
        return Outcome({"ok": False, "error": "strategy 過長"}, 400)
    severity = str(args.get("severity") or "medium").strip().lower()
    if severity not in SEVERITIES:
        return Outcome({"ok": False, "error": "severity 須為 info、low、medium、high 或 critical"}, 400)
    body_max = _env_int("REVIEW_BODY_MAX", DEFAULT_BODY_MAX)
    body_raw = args.get("body")
    if not str(body_raw or "").strip():
        return Outcome({"ok": False, "error": "缺少 body"}, 400)
    if len(str(body_raw)) > body_max:
        return Outcome({"ok": False, "error": f"body 超過 {body_max} 字"}, 400)
    action_max = _env_int("REVIEW_ACTION_MAX", DEFAULT_ACTION_MAX)
    action_raw = args.get("suggested_action")
    if action_raw and len(str(action_raw)) > action_max:
        return Outcome({"ok": False, "error": f"suggested_action 超過 {action_max} 字"}, 400)
    retry = rate_limit(ip, "write")
    if retry is not None:
        return Outcome({"ok": False, "error": "評論過於頻繁，請稍後再試"}, 429, retry_after=retry)
    now = now_iso_taipei()
    comment = {
        "id": "c_" + uuid.uuid4().hex[:16],
        "reviewer": reviewer,
        "topic": _scrub(topic),
        "strategy": _scrub(strategy) if strategy else None,
        "severity": severity,
        "body": _scrub(str(body_raw).strip()),
        "suggested_action": _scrub(str(action_raw).strip()) if action_raw else None,
        "status": "open",
        "created_at": now,
        "updated_at": now,
        "timezone": "Asia/Taipei",
        "replies": [],
        "status_events": [],
    }

    def mut(st: dict) -> dict:
        rows = [c for c in (st.get("comments") or []) if isinstance(c, dict)]
        rows = _trim_store(rows)
        if len(rows) >= MAX_COMMENTS:
            raise RuntimeError("comment_store_full")
        rows.append(comment)
        st["version"] = 1
        st["comments"] = rows
        return st

    try:
        CommentStore().mutate(mut)
    except RuntimeError as e:
        if "comment_store_full" in str(e):
            return Outcome({"ok": False, "error": "評論已滿，請先由團隊結案舊評論"}, 429)
        return Outcome({"ok": False, "error": "評論未能寫入"}, 500)
    return Outcome({"ok": True, "comment": _public_comment(comment)})


def reply_comment(args: dict, ip: str, *, author: str) -> Outcome:
    cid = str(args.get("comment_id") or args.get("id") or "").strip()
    if not _COMMENT_ID.match(cid):
        return Outcome({"ok": False, "error": "comment_id 無效"}, 400)
    body_raw = args.get("body")
    reply_max = _env_int("REVIEW_REPLY_MAX", DEFAULT_REPLY_MAX)
    if not str(body_raw or "").strip():
        return Outcome({"ok": False, "error": "缺少 body"}, 400)
    if len(str(body_raw)) > reply_max:
        return Outcome({"ok": False, "error": f"body 超過 {reply_max} 字"}, 400)
    if str(args.get("reviewer") or "").strip().lower() == "team" and author != "team":
        return Outcome({"ok": False, "error": "reviewer team 只能由儀表板登入使用"}, 400)
    who = author
    if author != "team":
        who = normalize_reviewer(args.get("reviewer") or author) or ""
        if not who:
            return Outcome({"ok": False, "error": "reply 需要 reviewer"}, 400)
    parent = str(args.get("in_reply_to") or "").strip() or None
    if parent and not _REPLY_ID.match(parent):
        return Outcome({"ok": False, "error": "in_reply_to 無效"}, 400)
    retry = rate_limit(ip, "write")
    if retry is not None:
        return Outcome({"ok": False, "error": "回覆過於頻繁，請稍後再試"}, 429, retry_after=retry)
    now = now_iso_taipei()
    reply = {
        "id": "r_" + uuid.uuid4().hex[:16],
        "author": who,
        "display_name": display_name(who),
        "body": _scrub(str(body_raw).strip()),
        "created_at": now,
        "timezone": "Asia/Taipei",
        "in_reply_to": parent,
    }
    found: dict = {}

    def mut(st: dict) -> dict:
        for c in st.get("comments") or []:
            if isinstance(c, dict) and c.get("id") == cid:
                existing = [x.get("id") for x in (c.get("replies") or []) if isinstance(x, dict)]
                if parent and parent not in existing:
                    found["missing_parent"] = True
                    return st
                c.setdefault("replies", []).append(reply)
                c["replies"] = c["replies"][-50:]
                c["updated_at"] = now
                found["c"] = c
                return st
        return st

    try:
        CommentStore().mutate(mut)
    except RuntimeError:
        return Outcome({"ok": False, "error": "回覆未能寫入"}, 500)
    if found.get("missing_parent"):
        return Outcome({"ok": False, "error": "in_reply_to 不在這則評論"}, 400)
    if "c" not in found:
        return Outcome({"ok": False, "error": "找不到評論"}, 404)
    return Outcome({"ok": True, "comment": _public_comment(found["c"])})


def set_comment_status(args: dict, ip: str) -> Outcome:
    cid = str(args.get("comment_id") or args.get("id") or "").strip()
    if not _COMMENT_ID.match(cid):
        return Outcome({"ok": False, "error": "comment_id 無效"}, 400)
    status = str(args.get("status") or "").strip().lower()
    if status not in STATUSES:
        return Outcome({"ok": False, "error": "狀態須為 open、acknowledged、done 或 rejected"}, 400)
    note_raw = args.get("note") or args.get("body") or ""
    if len(str(note_raw)) > _env_int("REVIEW_ACTION_MAX", DEFAULT_ACTION_MAX):
        return Outcome({"ok": False, "error": "備註過長"}, 400)
    retry = rate_limit(ip, "write")
    if retry is not None:
        return Outcome({"ok": False, "error": "更新過於頻繁，請稍後再試"}, 429, retry_after=retry)
    now = now_iso_taipei()
    event = {
        "at": now,
        "by": "team",
        "status": status,
        "note": _scrub(str(note_raw).strip()) or None,
        "timezone": "Asia/Taipei",
    }
    found: dict = {}

    def mut(st: dict) -> dict:
        for c in st.get("comments") or []:
            if isinstance(c, dict) and c.get("id") == cid:
                c["status"] = status
                c["updated_at"] = now
                c.setdefault("status_events", []).append(event)
                c["status_events"] = c["status_events"][-20:]
                found["c"] = c
                return st
        return st

    try:
        CommentStore().mutate(mut)
    except RuntimeError:
        return Outcome({"ok": False, "error": "狀態未能寫入"}, 500)
    if "c" not in found:
        return Outcome({"ok": False, "error": "找不到評論"}, 404)
    return Outcome({"ok": True, "comment": _public_comment(found["c"])})


def _pick(row: dict, keys: tuple[str, ...]) -> dict:
    return {k: row.get(k) for k in keys if k in row}


def list_strategies(status: dict, _state: dict) -> Outcome:
    orch = status.get("orchestrator") or {}
    weights = orch.get("weights") or {}
    targets = orch.get("targets") or {}
    alloc = {
        (s.get("id") or s.get("slot")): s
        for s in ((status.get("allocation") or {}).get("slots") or [])
        if isinstance(s, dict)
    }
    paused_slots = status.get("paused_slots") or {}
    rows = []
    for slot in status.get("armed_slots") or []:
        if not isinstance(slot, dict):
            continue
        sid = slot.get("slot")
        spec = alloc.get(sid) or {}
        rows.append({
            "slot": sid,
            "strategy_id": slot.get("strategy_id"),
            "code": slot.get("code"),
            "symbol": slot.get("symbol"),
            "family": slot.get("family"),
            "venue": slot.get("venue"),
            "timeframe": slot.get("tf"),
            "status": slot.get("status"),
            "status_zh": slot.get("status_zh"),
            "approved": slot.get("approved"),
            "order_mode": slot.get("order_mode"),
            "label_zh": slot.get("label_zh"),
            "weight": weights.get(sid) if isinstance(weights, dict) else None,
            "target": targets.get(sid) if isinstance(targets, dict) else None,
            "enabled": spec.get("enabled"),
            "global_paused": bool(status.get("paused")),
            "slot_paused": sid in paused_slots,
            "params_summary": slot.get("entry_condition"),
            "leverage": slot.get("leverage"),
            "quote_usdt": slot.get("quote_usdt"),
        })
    return Outcome(redact({
        "ok": True,
        "account": "binance_demo",
        "updated_at": status.get("updated_at"),
        "mode": status.get("mode"),
        "source": status.get("source_label") or status.get("source"),
        "family_approvals": status.get("family_approvals") or [],
        "strategies": rows,
    }))


def _fee_summary(status: dict) -> dict:
    books = status.get("books") or {}
    book_fees = {}
    if isinstance(books, dict):
        for quote, book in books.items():
            if not isinstance(book, dict):
                continue
            picked = _pick(book, ("fees_usdt", "fees", "realized_pnl", "unrealized_pnl", "equity"))
            if picked:
                book_fees[str(quote)] = picked
    sleeves = {}
    for key, val in status.items():
        if str(key).endswith("_exec") and isinstance(val, dict):
            picked = _pick(val, ("fees", "fee", "funding", "cash", "equity", "pnl"))
            if picked:
                sleeves[str(key)] = picked
    return {"books": book_fees, "sleeves": sleeves}


def _nav_history(status: dict, state: dict) -> list:
    import capital_basis
    basis = status.get("capital_basis") if isinstance(status.get("capital_basis"), dict) else {}
    history = state.get("capital_basis_history") or []
    if not isinstance(history, list):
        history = []
    start = basis.get("starting_total")
    if start is None:
        start = 10000.0
    return capital_basis.display_nav_history(history, start)


def _with_current_drawdown(basis: dict) -> dict:
    import capital_basis
    out = dict(basis or {})
    if out.get("current_drawdown") is None:
        out["current_drawdown"] = capital_basis.current_drawdown(out.get("nav"), out.get("peak"))
    return out


def get_performance(status: dict, state: dict) -> Outcome:
    import capital_basis
    nav_history = _nav_history(status, state)
    book = capital_basis.book_history(state.get("orchestrator") if isinstance(state.get("orchestrator"), dict) else None)
    netting = (status.get("netting") or {}).get("slots") if isinstance(status.get("netting"), dict) else {}
    books = {}
    if isinstance(netting, dict):
        for slot, row in netting.items():
            if isinstance(row, dict):
                books[str(slot)] = _pick(row, ("pnl", "equity", "cash", "capital", "positions"))
    return Outcome(redact({
        "ok": True,
        "account": "binance_demo",
        "updated_at": status.get("updated_at"),
        "capital_basis": _with_current_drawdown(status.get("capital_basis") or {}),
        "nav_history": nav_history[-400:],
        "history": nav_history[-400:],
        "book_history": book[-400:],
        "nav_history_note": (
            "每日合約淨值（USDT-M＋USDC 合約錢包＋未實現，不含現貨）。"
            "序列起點是起始資金（沒有更早的點時為 10000）。"
            "book_history 才是虛擬帳本合計，只供回撤警示對照，不是這條淨值。"
        ),
        "virtual_books": books,
        "fees": _fee_summary(status),
        "capital_bridge": __import__("review_audit").capital_bridge(status, state),
        "portfolio_risk": __import__("review_audit").portfolio_risk(
            {**status, "nav_history": nav_history}, state),
    }))


def _futures_liq(status: dict) -> list:
    out = []
    books = status.get("books") or {}
    if not isinstance(books, dict):
        return out
    for quote, book in books.items():
        fut = (book or {}).get("futures") if isinstance(book, dict) else None
        for pos in (fut or {}).get("positions") or []:
            if isinstance(pos, dict):
                out.append({"quote": quote, **_pick(pos, (
                    "symbol", "qty", "entry", "mark", "unrealized_pnl", "leverage",
                    "liq", "liq_distance", "margin_type", "notional",
                ))})
    return out


def get_positions(status: dict, state: dict) -> Outcome:
    import review_audit
    view = review_audit.annotate_positions(status, state)
    by_sym = {str(r.get("raw_symbol") or ""): r for r in view["positions"]}
    futures = []
    for row in _futures_liq(status):
        owned = by_sym.get(str(row.get("symbol") or "").upper()) or {}
        futures.append({
            **row,
            "owner": owned.get("owner", "unowned"),
            "strategy": owned.get("strategy", "unowned"),
            "owners": owned.get("owners") or [],
            "leverage": owned.get("leverage", row.get("leverage")),
        })
    return Outcome(redact({
        "ok": True,
        "account": "binance_demo",
        "updated_at": status.get("updated_at"),
        "positions": view["positions"],
        "vbo_3x": view["vbo_3x"],
        "e5_owned_3x": view["e5_owned_3x"],
        "e5_topup": view["e5_topup"],
        "vbo_3x_note": view["vbo_3x_note"],
        "liq_distance": status.get("liq_distance"),
        "futures_positions": futures,
    }))


def _trade_rows(status: dict, state: dict | None = None) -> list:
    import review_audit
    rows = []
    for src, kind in (("recent_fills", "fill"), ("closed_trades", "closed")):
        for t in status.get(src) or []:
            if not isinstance(t, dict):
                continue
            row = _pick(t, (
                "symbol", "code", "strategy_id", "slot", "side", "qty", "price",
                "quoteQty", "commission", "commissionAsset", "fee", "pnl_usdt",
                "realized_pnl_usdt", "time", "closed_at", "id", "orderId",
                "clientOrderId", "cause", "reason",
            ))
            row["kind"] = kind
            row["time"] = t.get("time") or t.get("closed_at")
            row["liquidation"] = bool(t.get("liquidation")) or review_audit._looks_like_liquidation(t)
            if row["liquidation"]:
                row["kind"] = "liquidation"
            rows.append(row)
    seen = {(str(r.get("symbol") or ""), str(r.get("time") or ""), str(r.get("id") or "")) for r in rows}
    for extra in review_audit.liquidation_trades(status, state or {}):
        key = (str(extra.get("symbol") or ""), str(extra.get("time") or ""), str(extra.get("id") or ""))
        if key in seen and key != ("", "", ""):
            continue
        if not extra.get("symbol"):
            continue
        seen.add(key)
        rows.append(extra)
    rows.sort(key=lambda r: str(r.get("time") or ""), reverse=True)
    return rows


def get_trades(status: dict, state: dict, args: dict | None = None) -> Outcome:
    args = args or {}
    try:
        limit = int(args.get("limit") if args.get("limit") is not None else 30)
    except (TypeError, ValueError):
        return Outcome({"ok": False, "error": "limit 必須是整數"}, 400)
    limit = min(100, max(1, limit))
    cursor_raw = args.get("cursor")
    offset = 0
    if cursor_raw not in (None, ""):
        try:
            offset = int(cursor_raw)
        except (TypeError, ValueError):
            return Outcome({"ok": False, "error": "cursor 無效"}, 400)
        if offset < 0:
            return Outcome({"ok": False, "error": "cursor 無效"}, 400)
    rows = _trade_rows(status, state)
    page = rows[offset:offset + limit]
    next_off = offset + limit
    return Outcome(redact({
        "ok": True,
        "account": "binance_demo",
        "updated_at": status.get("updated_at"),
        "total": len(rows),
        "trades": page,
        "next_cursor": str(next_off) if next_off < len(rows) else None,
    }))


def get_backtest(status: dict, state: dict, args: dict | None = None) -> Outcome:
    import e5_tracking
    args = args or {}
    want = str(args.get("strategy") or "").strip()
    lv = status.get("live_vs_backtest") or {}
    if not isinstance(lv, dict):
        lv = {}
    rows = []
    for row in lv.get("rows") or []:
        if not isinstance(row, dict):
            continue
        if want and want not in {
            str(row.get("strategy_id") or ""), str(row.get("code") or ""),
            str(row.get("symbol") or ""), str(row.get("slot") or ""),
        }:
            continue
        rows.append(row)
    summary = {k: lv.get(k) for k in ("at", "day_utc", "n_match", "n_mismatch", "n_unsupported") if k in lv}
    e5 = e5_tracking.compare(state, status)
    body = {
        "ok": True,
        "account": "binance_demo",
        "updated_at": status.get("updated_at"),
        "parity": status.get("parity"),
        "live_vs_backtest": summary,
        "rows": rows[:200],
    }
    import review_audit
    body["reconciliation"] = review_audit.reconciliation(status, state)
    if e5_tracking.wanted(want, e5):
        if want and want not in e5_tracking.E5_CODES and want.upper() != "E5":
            for day in e5.get("days") or []:
                day["symbols"] = [r for r in day.get("symbols") or [] if r.get("symbol") == want]
                day["summary"] = e5_tracking.summarize_rows(day["symbols"])
        body["e5"] = e5
    return Outcome(redact(body))


def get_reconciliation(status: dict, state: dict) -> Outcome:
    import review_audit
    return Outcome(redact({
        "ok": True,
        "account": "binance_demo",
        "updated_at": status.get("updated_at"),
        **review_audit.reconciliation(status, state),
    }))


def get_risk(status: dict, state: dict) -> Outcome:
    import review_audit
    nav_history = _nav_history(status, state)
    return Outcome(redact({
        "ok": True,
        "account": "binance_demo",
        "updated_at": status.get("updated_at"),
        **review_audit.portfolio_risk({**status, "nav_history": nav_history}, state),
    }))


def get_bridge(status: dict, state: dict) -> Outcome:
    import review_bridge
    return Outcome(redact({
        **review_bridge.build_bridge(status, state),
        "updated_at": status.get("updated_at"),
    }))


def get_orchestrator_plan(status: dict, state: dict, args: dict | None = None) -> Outcome:
    args = args or {}
    try:
        limit = int(args.get("history_limit") if args.get("history_limit") is not None else 30)
    except (TypeError, ValueError):
        return Outcome({"ok": False, "error": "history_limit 必須是整數"}, 400)
    limit = min(120, max(1, limit))
    import capital_basis
    stored = state.get("orchestrator") if isinstance(state.get("orchestrator"), dict) else {}
    hist = capital_basis.book_history(stored)
    return Outcome(redact({
        "ok": True,
        "account": "binance_demo",
        "updated_at": status.get("updated_at"),
        "latest": status.get("orchestrator"),
        "policy": status.get("orchestrator_policy"),
        "slot_caps": status.get("slot_caps"),
        "history": hist[-limit:],
        "book_history": hist[-limit:],
        "nav_history": _nav_history(status, state)[-400:],
        "history_note": (
            "latest 的 portfolio_equity、peak、current_drawdown 與 /status 相同，是合約錢包。"
            "history 與 book_history 是虛擬帳本合計（book_portfolio_equity、book_drawdown），只供回撤警示對照。"
            "淨值走勢用 nav_history（capital_basis_history，起點為起始資金）。"
        ),
    }))


def _snapshot_or_error(snapshot: Callable[[], tuple[dict, dict]]) -> tuple[dict, dict] | Outcome:
    try:
        status, state = snapshot()
    except Exception as e:  # noqa: BLE001
        return Outcome({"ok": False, "error": redact_text(str(e))[:300] or "讀取狀態失敗"}, 500)
    if not isinstance(status, dict):
        status = {}
    if not isinstance(state, dict):
        state = {}
    return status, state


def dispatch_read(name: str, args: dict, snapshot: Callable[[], tuple[dict, dict]], ip: str) -> Outcome:
    retry = rate_limit(ip, "read")
    if retry is not None:
        return Outcome({"ok": False, "error": "讀取過於頻繁，請稍後再試"}, 429, retry_after=retry)
    if name == "list_comments":
        return list_comments_view(args.get("status"), args.get("limit") or 50)
    loaded = _snapshot_or_error(snapshot)
    if isinstance(loaded, Outcome):
        return loaded
    status, state = loaded
    if name == "list_strategies":
        return list_strategies(status, state)
    if name == "get_performance":
        return get_performance(status, state)
    if name == "get_positions":
        return get_positions(status, state)
    if name == "get_trades":
        return get_trades(status, state, args)
    if name == "get_backtest":
        return get_backtest(status, state, args)
    if name == "get_reconciliation":
        return get_reconciliation(status, state)
    if name == "get_risk":
        return get_risk(status, state)
    if name == "get_bridge":
        return get_bridge(status, state)
    if name == "get_orchestrator_plan":
        return get_orchestrator_plan(status, state, args)
    return Outcome({"ok": False, "error": "未知的工具"}, 404)


TOOLS: list[dict] = [
    {
        "name": "list_strategies",
        "description": "List Binance Demo strategies with status, orchestrator weight, approval, and a params summary. Use this first when reviewing what is live.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "get_performance",
        "description": "Capital-basis NAV, PnL, drawdown, the daily wallet NAV, the capital bridge (start, per-strategy net, fees, funding, wallet, residual), and display-only portfolio risk.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "get_positions",
        "description": "Demo positions with owner and strategy from the virtual ledgers (E5, VBO, R2S, FA, U1; unowned when none), leverage, and a read-only VBO 3x liquidation-distance list.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "get_trades",
        "description": "Recent Demo fills, closed trades, and recorded forced liquidations (liquidation true), newest first. Page with cursor from the previous next_cursor.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Page size. Default 30."},
                "cursor": {"type": "string", "description": "Offset cursor returned as next_cursor."},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "get_backtest",
        "description": "Live-versus-backtest parity, E5 target versus actual, and reconciliation: mismatch reasons, before/after parity-fix dates, rolling match rate, and realized slippage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string", "description": "Optional strategy id, code, symbol, or slot filter."},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "get_reconciliation",
        "description": "Parity mismatches split on the repo's parity-fix dates, each with a reason (APT 2026-10-03 included), rolling match rate, and realized slippage versus the signal or target price.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "get_bridge",
        "description": "Demo capital bridge. Shows USDC unallocated and the USDT book-versus-wallet overcount. The residual is not absorbed. Does not change ledgers.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "get_risk",
        "description": "Display-only portfolio risk: gross long, gross short, net exposure, +30% and -30% book stress with a rough isolated-liquidation cap, and the worst single-day NAV loss. Does not change positions.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "get_orchestrator_plan",
        "description": "Latest orchestrator plan (weights and targets; portfolio fields match /status capital basis) and stored plan history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "history_limit": {"type": "integer", "minimum": 1, "maximum": 120, "description": "History rows. Default 30."},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "list_comments",
        "description": "List comment threads. Replies nest under the comment and show author identity (chatgpt, claude, auto_trader, team). Times are Asia/Taipei.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "acknowledged", "done", "rejected"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "post_comment",
        "description": "File a review comment. Does not change trading, approvals, or settings. reviewer is chatgpt, claude, or auto_trader.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reviewer": {"type": "string", "description": "chatgpt, claude, auto_trader, other, or a short slug. Not team."},
                "topic": {"type": "string", "description": "Subject, often a strategy code or risk theme."},
                "strategy": {"type": "string", "description": "Optional strategy id or slot."},
                "severity": {"type": "string", "enum": ["info", "low", "medium", "high", "critical"]},
                "body": {"type": "string", "description": "The finding. Max 8000 characters."},
                "suggested_action": {"type": "string", "description": "What the team should consider. Not executed."},
            },
            "required": ["reviewer", "topic", "body"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "reply_comment",
        "description": "Reply inside a comment thread. Set in_reply_to to nest under an existing reply. Does not change status, approvals, or trading.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "comment_id": {"type": "string"},
                "reviewer": {"type": "string", "description": "chatgpt, claude, auto_trader, other, or a short slug. Not team."},
                "body": {"type": "string"},
                "in_reply_to": {"type": "string", "description": "Optional reply id in this thread. Omit to reply to the comment itself."},
            },
            "required": ["comment_id", "body"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
]

_READ_TOOLS = {
    "list_strategies", "get_performance", "get_positions", "get_trades",
    "get_backtest", "get_reconciliation", "get_risk", "get_bridge", "get_orchestrator_plan", "list_comments",
}


def call_tool(name: str, args: dict, snapshot: Callable[[], tuple[dict, dict]], ip: str) -> Outcome:
    args = args if isinstance(args, dict) else {}
    if name in _READ_TOOLS:
        return dispatch_read(name, args, snapshot, ip)
    if name == "post_comment":
        return post_comment(args, ip)
    if name == "reply_comment":
        return reply_comment(args, ip, author=str(args.get("reviewer") or "other"))
    return Outcome({"ok": False, "error": f"未知的工具 {name}"}, 404)


def _tool_result(out: Outcome) -> dict:
    text = json.dumps(out.body, ensure_ascii=False, default=str)
    if len(text) > 100_000:
        text = text[:100_000] + "\n…truncated"
    return {"content": [{"type": "text", "text": text}], "isError": out.status >= 400}


def openapi_spec(base_url: str) -> dict:
    def ok(schema_name: str) -> dict:
        return {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema_name}"}}}}}

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Crypto strategy external review",
            "version": "1.0.0",
            "description": (
                "Read Binance Demo strategy and performance data and leave review comments. "
                "This action cannot approve, trade, revoke, or change settings. "
                "Authenticate with Authorization: Bearer plus the reviewer token. "
                "Times are Asia/Taipei."
            ),
        },
        "servers": [{"url": base_url.rstrip("/")}],
        "components": {
            "securitySchemes": {
                "reviewerBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "REVIEWER_TOKEN. Send as Authorization: Bearer <token>. Query tokens are not part of this schema.",
                }
            },
            "schemas": {
                "StrategyList": {"type": "object"},
                "Performance": {"type": "object"},
                "Positions": {"type": "object"},
                "Trades": {"type": "object"},
                "Backtest": {"type": "object"},
                "Reconciliation": {"type": "object"},
                "Risk": {"type": "object"},
                "Bridge": {"type": "object"},
                "Orchestrator": {"type": "object"},
                "Comments": {"type": "object"},
                "CommentWrite": {
                    "type": "object",
                    "required": ["reviewer", "topic", "body"],
                    "properties": {
                        "reviewer": {"type": "string", "enum": ["chatgpt", "claude", "auto_trader", "other"]},
                        "topic": {"type": "string"},
                        "strategy": {"type": "string"},
                        "severity": {"type": "string", "enum": ["info", "low", "medium", "high", "critical"]},
                        "body": {"type": "string"},
                        "suggested_action": {"type": "string"},
                    },
                },
                "ReplyWrite": {
                    "type": "object",
                    "required": ["body"],
                    "properties": {
                        "reviewer": {"type": "string", "enum": ["chatgpt", "claude", "auto_trader", "other"]},
                        "body": {"type": "string"},
                        "in_reply_to": {"type": "string", "description": "Reply id to nest under. Omit to reply to the comment."},
                    },
                },
            },
        },
        "security": [{"reviewerBearer": []}],
        "paths": {
            "/review/strategies": {"get": {"operationId": "listStrategies", "summary": "List strategies", "responses": ok("StrategyList")}},
            "/review/performance": {"get": {"operationId": "getPerformance", "summary": "NAV, capital bridge, drawdown, risk, fees", "responses": ok("Performance")}},
            "/review/positions": {"get": {"operationId": "getPositions", "summary": "Positions, owner, leverage, VBO 3x distance", "responses": ok("Positions")}},
            "/review/trades": {
                "get": {
                    "operationId": "getTrades",
                    "summary": "Fills, including flagged liquidations, paged",
                    "parameters": [
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                        {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                    ],
                    "responses": ok("Trades"),
                }
            },
            "/review/backtest": {
                "get": {
                    "operationId": "getBacktest",
                    "summary": "Satellite and U1 parity, plus E5 target versus actual",
                    "parameters": [{"name": "strategy", "in": "query", "schema": {"type": "string"}}],
                    "responses": ok("Backtest"),
                }
            },
            "/review/reconciliation": {"get": {"operationId": "getReconciliation", "summary": "Parity periods, mismatch reasons, slippage", "responses": ok("Reconciliation")}},
            "/review/risk": {"get": {"operationId": "getRisk", "summary": "Display-only portfolio risk", "responses": ok("Risk")}},
            "/review/bridge": {"get": {"operationId": "getBridge", "summary": "Capital bridge with the residual left visible", "responses": ok("Bridge")}},
            "/review/orchestrator": {
                "get": {
                    "operationId": "getOrchestratorPlan",
                    "summary": "Latest plan and history",
                    "parameters": [{"name": "history_limit", "in": "query", "schema": {"type": "integer"}}],
                    "responses": ok("Orchestrator"),
                }
            },
            "/review/comments": {
                "get": {
                    "operationId": "listComments",
                    "summary": "List review comments",
                    "parameters": [
                        {"name": "status", "in": "query", "schema": {"type": "string"}},
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": ok("Comments"),
                },
                "post": {
                    "operationId": "postComment",
                    "summary": "Post a review comment",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CommentWrite"}}}},
                    "responses": ok("Comments"),
                },
            },
            "/review/comments/{comment_id}/reply": {
                "post": {
                    "operationId": "replyComment",
                    "summary": "Reply to a review comment",
                    "parameters": [{"name": "comment_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ReplyWrite"}}}},
                    "responses": ok("Comments"),
                }
            },
        },
    }


def _rpc_result(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": redact_text(message)[:300]}}


def handle_rpc(message: dict, snapshot: Callable[[], tuple[dict, dict]], ip: str) -> tuple[dict | None, int, float | None]:
    """Return (payload or None for a notification, http status, retry-after)."""
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "Invalid Request"), 400, None
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    is_notification = "id" not in message
    if not isinstance(method, str):
        return _rpc_error(req_id, -32600, "Invalid Request"), 400, None
    if method == "initialize":
        requested = str(params.get("protocolVersion") or DEFAULT_PROTOCOL)
        protocol = requested if requested in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
        return _rpc_result(req_id, {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }), 200, None
    if method in ("notifications/initialized", "notifications/cancelled") or method.startswith("notifications/"):
        return None, 202, None
    if method == "ping":
        return _rpc_result(req_id, {}), 200, None
    if method == "tools/list":
        return _rpc_result(req_id, {"tools": TOOLS}), 200, None
    if method == "tools/call":
        name = str(params.get("name") or "")
        if name not in {t["name"] for t in TOOLS}:
            return _rpc_error(req_id, -32602, "unknown tool"), 200, None
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        out = call_tool(name, args, snapshot, ip)
        if out.status == 429:
            return _rpc_error(req_id, -32029, out.body.get("error") or "rate limited"), 429, out.retry_after
        return _rpc_result(req_id, _tool_result(out)), 200, None
    if is_notification:
        return None, 202, None
    return _rpc_error(req_id, -32601, "Method not found"), 200, None


def register(app, *, snapshot: Callable[[], tuple[dict, dict]], check_read, check_write, check_reply, check_team, client_ip) -> None:
    """Attach /mcp and /review/* to the Flask app. Auth callables return (ok, error, http_status)."""
    from flask import Response, jsonify, request

    def _json(body: dict, status: int = 200, retry: float | None = None, extra_headers: dict | None = None):
        resp = jsonify(body)
        resp.status_code = status
        if retry is not None:
            resp.headers["Retry-After"] = str(int(retry) + 1)
        resp.headers["Cache-Control"] = "no-store"
        for k, v in (extra_headers or {}).items():
            resp.headers[k] = v
        return resp

    def _deny(result):
        ok, err, code = result
        if ok:
            return None
        return _json({"ok": False, "error": err}, code)

    def _mcp_body(payload: dict | None, status: int, protocol: str, retry: float | None):
        headers = {
            "MCP-Protocol-Version": protocol,
            "Cache-Control": "no-store",
        }
        if retry is not None:
            headers["Retry-After"] = str(int(retry) + 1)
        if payload is None:
            resp = app.make_response(("", status))
            for k, v in headers.items():
                resp.headers[k] = v
            return resp
        accept = request.headers.get("Accept") or ""
        wants_sse = "text/event-stream" in accept and "application/json" not in accept
        raw = json.dumps(payload, ensure_ascii=False)
        if wants_sse:
            sse = "event: message\ndata: " + raw + "\n\n"
            resp = Response(sse, status=status, mimetype="text/event-stream")
        else:
            resp = Response(raw, status=status, mimetype="application/json")
        for k, v in headers.items():
            resp.headers[k] = v
        return resp

    @app.route("/mcp", methods=["OPTIONS"])
    @app.route("/review", methods=["OPTIONS"])
    @app.route("/review/<path:_sub>", methods=["OPTIONS"])
    def review_options(_sub=None):
        return app.make_response(("", 204))

    @app.route("/mcp", methods=["GET"])
    def mcp_get():
        denied = _deny(check_write())
        if denied:
            return denied
        resp = jsonify({
            "ok": False,
            "error": "此端點使用 streamable HTTP。請以 POST 傳送 JSON-RPC。",
            "protocol": DEFAULT_PROTOCOL,
        })
        resp.status_code = 405
        resp.headers["Allow"] = "POST, DELETE, OPTIONS"
        resp.headers["MCP-Protocol-Version"] = DEFAULT_PROTOCOL
        return resp

    @app.route("/mcp", methods=["DELETE"])
    def mcp_delete():
        denied = _deny(check_write())
        if denied:
            return denied
        resp = app.make_response(("", 200))
        resp.headers["MCP-Protocol-Version"] = DEFAULT_PROTOCOL
        return resp

    @app.route("/mcp", methods=["POST"])
    def mcp_post():
        denied = _deny(check_write())
        if denied:
            return denied
        header_proto = (request.headers.get("MCP-Protocol-Version") or "").strip()
        if header_proto and header_proto not in PROTOCOL_VERSIONS:
            return _mcp_body(_rpc_error(None, -32600, "Unsupported MCP protocol version"), 400, DEFAULT_PROTOCOL, None)
        raw = request.get_data(cache=False, as_text=True) or ""
        try:
            message = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return _mcp_body(_rpc_error(None, -32700, "Parse error"), 400, header_proto or DEFAULT_PROTOCOL, None)
        if isinstance(message, list):
            return _mcp_body(_rpc_error(None, -32600, "JSON-RPC batches are not supported"), 400, header_proto or DEFAULT_PROTOCOL, None)
        payload, status, retry = handle_rpc(message, snapshot, client_ip())
        protocol = DEFAULT_PROTOCOL
        if isinstance(message, dict) and message.get("method") == "initialize" and isinstance(payload, dict):
            protocol = ((payload.get("result") or {}).get("protocolVersion")) or DEFAULT_PROTOCOL
        elif header_proto in PROTOCOL_VERSIONS:
            protocol = header_proto
        return _mcp_body(payload, status, protocol, retry)

    def _read(name: str, args: dict | None = None):
        denied = _deny(check_read())
        if denied:
            return denied
        out = dispatch_read(name, args or {}, snapshot, client_ip())
        return _json(out.body, out.status, out.retry_after)

    @app.route("/review/strategies", methods=["GET"])
    def review_strategies():
        return _read("list_strategies")

    @app.route("/review/performance", methods=["GET"])
    def review_performance():
        return _read("get_performance")

    @app.route("/review/positions", methods=["GET"])
    def review_positions():
        return _read("get_positions")

    @app.route("/review/trades", methods=["GET"])
    def review_trades():
        return _read("get_trades", {"limit": request.args.get("limit"), "cursor": request.args.get("cursor")})

    @app.route("/review/backtest", methods=["GET"])
    def review_backtest():
        return _read("get_backtest", {"strategy": request.args.get("strategy")})

    @app.route("/review/reconciliation", methods=["GET"])
    def review_reconciliation():
        return _read("get_reconciliation")

    @app.route("/review/risk", methods=["GET"])
    def review_risk():
        return _read("get_risk")

    @app.route("/review/bridge", methods=["GET"])
    def review_bridge_route():
        return _read("get_bridge")

    @app.route("/review/orchestrator", methods=["GET"])
    def review_orchestrator():
        return _read("get_orchestrator_plan", {"history_limit": request.args.get("history_limit")})

    @app.route("/review/comments", methods=["GET"])
    def review_comments():
        return _read("list_comments", {"status": request.args.get("status"), "limit": request.args.get("limit") or 50})

    @app.route("/review/comments", methods=["POST"])
    def review_comments_post():
        denied = _deny(check_write())
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        out = post_comment(body, client_ip())
        return _json(out.body, out.status, out.retry_after)

    @app.route("/review/comments/<comment_id>/reply", methods=["POST"])
    def review_comments_reply(comment_id: str):
        denied = _deny(check_reply())
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        body = {**body, "comment_id": comment_id}
        author = "team"
        if is_reviewer_token(_presented_from_request()):
            author = str(body.get("reviewer") or "other")
        out = reply_comment(body, client_ip(), author=author)
        return _json(out.body, out.status, out.retry_after)

    @app.route("/review/comments/<comment_id>/status", methods=["POST"])
    def review_comments_status(comment_id: str):
        if is_reviewer_token(_presented_from_request()):
            return _json({"ok": False, "error": "外部審閱權杖不能更改狀態、不能核准、不能交易或撤銷"}, 403)
        denied = _deny(check_team())
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        out = set_comment_status({**body, "comment_id": comment_id}, client_ip())
        return _json(out.body, out.status, out.retry_after)

    @app.route("/review/openapi.json", methods=["GET"])
    def review_openapi():
        denied = _deny(check_read())
        if denied:
            return denied
        proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
        host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
        base = f"{proto}://{host}" if host else request.host_url.rstrip("/")
        return _json(openapi_spec(base))


def _presented_from_request() -> str:
    """Bearer header, else the query token when that option is enabled. Never logged."""
    from flask import request
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if not query_token_enabled():
        return ""
    return (request.args.get("access_token") or request.args.get("token") or "").strip()
