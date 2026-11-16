"""orch-summary latest.json uses the same wallet overlay as /status.

The workflow does not check out the repo, so the overlay is inlined in
.github/workflows/orch-summary.yml. Book-sum portfolio_equity/peak/drawdown
move to book_* ; wallet NAV replaces them when capital_basis.nav is present.
"""
import ast
import textwrap
from pathlib import Path

import capital_basis as C

ROOT = Path(__file__).resolve().parents[2]
YML = ROOT / ".github/workflows/orch-summary.yml"

BOOK = {
    "portfolio_equity": 6698.47,
    "peak": 9953.03,
    "drawdown": 0.3270,
    "dd_alert": True,
    "targets": {"demo_e5_min5_x175_t05top20": 3875.17},
    "history": [{"bar": "2026-11-14", "P": 6698.47, "dd": 0.3270}],
}


def _workflow_python() -> str:
    text = YML.read_text()
    start = text.index("python3 - <<'PY'\n") + len("python3 - <<'PY'\n")
    end = text.index("\n          PY\n", start)
    return textwrap.dedent(text[start:end])


def _overlay_fn():
    tree = ast.parse(_workflow_python())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "overlay_orchestrator")
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict = {}
    exec(compile(module, str(YML), "exec"), ns)
    return ns["overlay_orchestrator"]


def test_workflow_python_parses_and_does_not_checkout():
    text = YML.read_text()
    assert "actions/checkout" not in text
    ast.parse(_workflow_python())
    src = _workflow_python()
    assert "s.get('capital_basis')" in src
    tree = ast.parse(src)
    calls = []

    class V(ast.NodeVisitor):
        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == "overlay_orchestrator":
                calls.append(node)
            self.generic_visit(node)

    V().visit(tree)
    assert len(calls) == 2


def test_overlay_matches_status_wallet_fields_and_keeps_the_book_sum():
    """Book sum 6698.47 / peak 9953.03 / 32.70% moves to book_*. NAV replaces the live fields."""
    overlay = _overlay_fn()
    basis = {
        "nav": 9776.78,
        "peak": 10000,
        "max_drawdown_pct": 0.0293,
        "max_drawdown": 293.0,
        "starting_total": 10000,
        "nav_usdt_m": 4900.11,
        "nav_usdc_m": 4876.67,
        "nav_scope": "usd_m_futures_usdt_plus_usdc",
    }
    stored = dict(BOOK)
    view = overlay(stored, basis)
    wallet = C.wallet_portfolio(basis)
    status = C.overlay_orchestrator(stored, basis)
    for k, v in wallet.items():
        assert view[k] == v == status[k]
    assert view["portfolio_equity"] == 9776.78
    assert view["peak"] == 10000
    assert view["drawdown"] == 0.0293
    assert view["book_portfolio_equity"] == 6698.47
    assert view["book_peak"] == 9953.03
    assert view["book_drawdown"] == 0.3270
    assert view["dd_alert"] is True
    assert view["targets"]["demo_e5_min5_x175_t05top20"] == 3875.17
    assert "history" not in view
    assert stored["portfolio_equity"] == 6698.47 and stored["history"][0]["P"] == 6698.47
    assert "book_portfolio_equity" not in stored


def test_missing_nav_scope_uses_the_status_default_basis():
    overlay = _overlay_fn()
    view = overlay(BOOK, {"nav": 9776.78, "peak": 10000, "max_drawdown_pct": 0.0293})
    assert view["portfolio_basis"] == "usd_m_futures_usdt_plus_usdc"
    assert view["portfolio_equity"] == 9776.78
    assert view["drawdown"] == 0.0293


def test_missing_nav_leaves_the_book_sum_in_place():
    overlay = _overlay_fn()
    basis = C.summarize([], {"USDT": 4868.68, "USDC": None}, [])
    assert basis["nav"] is None
    view = overlay(dict(BOOK), basis)
    assert view["portfolio_equity"] == 6698.47
    assert view["peak"] == 9953.03
    assert view["drawdown"] == 0.3270
    assert view["book_portfolio_equity"] == 6698.47
    assert "max_drawdown" not in view
    assert "portfolio_basis" not in view
