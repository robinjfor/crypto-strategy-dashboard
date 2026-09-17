/* Live paper-trading dashboard — static GitHub Pages client */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const BOOK_URL = "./data/live_book.json";
  const BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price?symbol=";

  let book = null;
  let strategyPayloads = []; // [{ meta, paper, settlement, error }]
  let selectedId = null;
  let markPrices = {}; // symbol -> { price, source }
  let autoOn = true;
  let refreshSec = 15;
  let countdown = 15;
  let timerId = null;
  let loading = false;

  function fmtPct(v, digits = 2) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    const sign = n > 0 ? "+" : "";
    return sign + n.toFixed(digits) + "%";
  }
  function fmtNum(v, digits = 2) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    return Number(v).toLocaleString("en-US", {
      maximumFractionDigits: digits,
      minimumFractionDigits: digits,
    });
  }
  function fmtInt(v) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    return String(Math.round(Number(v)));
  }
  function clsSigned(v) {
    if (v == null || Number.isNaN(Number(v))) return "neutral";
    const n = Number(v);
    if (n > 0) return "pos";
    if (n < 0) return "neg";
    return "neutral";
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function showErr(msg) {
    const el = $("errBanner");
    if (!msg) {
      el.style.display = "none";
      el.textContent = "";
      return;
    }
    el.style.display = "block";
    el.textContent = msg;
  }

  async function fetchJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status} · ${url}`);
    return r.json();
  }

  function loadSettlements(raw) {
    if (!raw) return [];
    if (Array.isArray(raw)) return raw;
    if (Array.isArray(raw.settlements)) return raw.settlements;
    if (Array.isArray(raw.fills)) return raw.fills;
    return [];
  }

  function nowTaipeiLabel() {
    try {
      return new Date().toLocaleString("zh-TW", {
        timeZone: "Asia/Taipei",
        hour12: false,
      }) + " CST";
    } catch (_) {
      return new Date().toISOString();
    }
  }

  /** Aggregate balances / equity across all strategy papers */
  function aggregateAccount(payloads) {
    const balances = {};
    let equitySum = 0;
    let equityCount = 0;
    const openPositions = [];
    for (const p of payloads) {
      if (!p.paper) continue;
      const pt = p.paper;
      if (pt.virtual_equity != null) {
        equitySum += Number(pt.virtual_equity);
        equityCount += 1;
      }
      const bal = pt.balances || {};
      for (const [asset, qty] of Object.entries(bal)) {
        balances[asset] = (balances[asset] || 0) + Number(qty || 0);
      }
      const opens = Array.isArray(pt.open_positions) ? pt.open_positions : [];
      for (const o of opens) openPositions.push({ ...o, strategy_id: p.meta.id });
    }
    // Prefer single-strategy equity; if multiple, sum (each strategy may have own book)
    const virtualEquity =
      equityCount === 1
        ? equitySum
        : equityCount > 1
          ? equitySum
          : null;
    return { balances, virtualEquity, openPositions };
  }

  function resolveMarkPrice(asset, payloads) {
    if (asset === "CASH" || asset === "USDT") {
      return { price: 1, source: "現金" };
    }
    const pair = asset + "USDT";
    if (markPrices[pair] && markPrices[pair].price != null) {
      return markPrices[pair];
    }
    // Fallback from paper signal / open entry
    for (const p of payloads) {
      const pt = p.paper;
      if (!pt) continue;
      const sym = (pt.symbol || "").toUpperCase();
      const msym = (pt.market_symbol || "").toUpperCase();
      if (sym === asset || msym === pair || msym === asset + "USDT") {
        if (pt.signal && pt.signal.close != null) {
          return { price: Number(pt.signal.close), source: "paper.signal.close" };
        }
      }
      const opens = Array.isArray(pt.open_positions) ? pt.open_positions : [];
      for (const o of opens) {
        const os = (o.symbol || "").toUpperCase();
        if (os === pair || os === asset || os.startsWith(asset)) {
          if (o.entry_price != null) {
            return { price: Number(o.entry_price), source: "paper.entry" };
          }
        }
      }
    }
    return { price: null, source: "無" };
  }

  function computeAllocation(book, payloads) {
    const { balances, virtualEquity } = aggregateAccount(payloads);
    const capital = Number(book.total_capital_usdt) || 5000;
    const usdtCash = Number(balances.USDT || 0);

    // Mark-to-market holdings
    const holdings = {};
    let positionsValue = 0;
    for (const [asset, qty] of Object.entries(balances)) {
      if (asset === "USDT") continue;
      const { price, source } = resolveMarkPrice(asset, payloads);
      const q = Number(qty) || 0;
      const value = price != null ? q * price : 0;
      holdings[asset] = { qty: q, price, source, value };
      positionsValue += value;
    }

    const equity =
      virtualEquity != null
        ? virtualEquity
        : usdtCash + positionsValue;

    const targets = Array.isArray(book.target_allocation)
      ? book.target_allocation
      : [];

    const rows = targets.map((t) => {
      const asset = t.asset;
      const targetPct = Number(t.pct) || 0;
      let actualValue = 0;
      let markSource = "—";
      if (asset === "CASH" || (t.symbol || "").toUpperCase() === "USDT") {
        actualValue = usdtCash;
        markSource = "balances.USDT";
      } else {
        const h = holdings[asset];
        if (h) {
          actualValue = h.value;
          markSource = h.source;
        } else {
          actualValue = 0;
          markSource = "無持倉";
        }
      }
      const actualPct = equity > 0 ? (actualValue / equity) * 100 : 0;
      return {
        asset,
        label: asset === "CASH" ? "現金 (USDT)" : asset,
        targetPct,
        actualPct,
        actualValue,
        markSource,
      };
    });

    const pnl = equity - capital;
    const pnlPct = capital > 0 ? (pnl / capital) * 100 : null;

    return {
      equity,
      capital,
      pnl,
      pnlPct,
      usdtCash,
      positionsValue,
      holdings,
      rows,
    };
  }

  async function fetchMarkPrices(payloads) {
    const symbols = new Set();
    for (const p of payloads) {
      if (!p.paper) continue;
      const m = p.paper.market_symbol;
      if (m) symbols.add(String(m).toUpperCase());
      else if (p.paper.symbol) symbols.add(String(p.paper.symbol).toUpperCase() + "USDT");
      const opens = Array.isArray(p.paper.open_positions) ? p.paper.open_positions : [];
      for (const o of opens) {
        if (o.symbol) symbols.add(String(o.symbol).toUpperCase());
      }
    }
    // Also try target allocation assets
    if (book && Array.isArray(book.target_allocation)) {
      for (const t of book.target_allocation) {
        if (t.asset && t.asset !== "CASH") {
          symbols.add(String(t.asset).toUpperCase() + "USDT");
        }
      }
    }

    const next = { ...markPrices };
    await Promise.all(
      [...symbols].map(async (sym) => {
        try {
          const data = await fetchJSON(BINANCE_TICKER + encodeURIComponent(sym));
          const price = data && data.price != null ? Number(data.price) : null;
          if (price != null && !Number.isNaN(price)) {
            next[sym] = { price, source: "Binance 公開價" };
          }
        } catch (_) {
          // CORS / network — keep previous or fall back later
        }
      })
    );
    markPrices = next;
  }

  function renderAllocation(alloc) {
    const rowsHtml = alloc.rows
      .map((r) => {
        const gap = r.actualPct - r.targetPct;
        const barTarget = Math.min(100, Math.max(0, r.targetPct));
        const barActual = Math.min(100, Math.max(0, r.actualPct));
        return `<tr>
          <td><strong>${escapeHtml(r.label)}</strong></td>
          <td>${fmtNum(r.targetPct, 0)}%</td>
          <td>${fmtNum(r.actualPct, 1)}%</td>
          <td class="${clsSigned(gap)}">${fmtPct(gap, 1)}</td>
          <td>${fmtNum(r.actualValue, 2)}</td>
          <td class="mark-src">${escapeHtml(r.markSource)}</td>
        </tr>
        <tr class="alloc-bar-row">
          <td colspan="6">
            <div class="alloc-bar" title="目標 ${r.targetPct}% / 實際 ${r.actualPct.toFixed(1)}%">
              <div class="alloc-bar-track">
                <div class="alloc-bar-target" style="width:${barTarget}%"></div>
                <div class="alloc-bar-actual" style="width:${barActual}%"></div>
              </div>
            </div>
          </td>
        </tr>`;
      })
      .join("");

    return `
      <section class="section">
        <div class="section-head">
          <h2>目標配置 vs 實際</h2>
          <span class="hint">標記價來源見各列小字；CORS 失敗則用 paper close／entry</span>
        </div>
        <div class="card">
          <div class="table-scroll">
            <table class="data">
              <thead>
                <tr>
                  <th>資產</th><th>目標</th><th>實際</th><th>差距</th><th>市值 USDT</th><th>標記價來源</th>
                </tr>
              </thead>
              <tbody>${rowsHtml || `<tr><td colspan="6" class="empty-row">無配置資料</td></tr>`}</tbody>
            </table>
          </div>
        </div>
      </section>`;
  }

  function renderKpis(alloc) {
    return `
      <section class="section">
        <div class="section-head"><h2>帳戶總覽</h2>
          <span class="hint">${escapeHtml(book.exchange || "")} · ${escapeHtml(book.api_base || "")}</span>
        </div>
        <div class="kpi-grid">
          <div class="kpi">
            <div class="label">虛擬權益</div>
            <div class="value">${fmtNum(alloc.equity, 2)}</div>
            <div class="sublabel">USDT</div>
          </div>
          <div class="kpi">
            <div class="label">起始資金</div>
            <div class="value neutral">${fmtNum(alloc.capital, 2)}</div>
            <div class="sublabel">USDT</div>
          </div>
          <div class="kpi">
            <div class="label">損益 %</div>
            <div class="value ${clsSigned(alloc.pnlPct)}">${fmtPct(alloc.pnlPct)}</div>
            <div class="sublabel">${fmtNum(alloc.pnl, 2)} USDT</div>
          </div>
          <div class="kpi">
            <div class="label">USDT 現金</div>
            <div class="value">${fmtNum(alloc.usdtCash, 2)}</div>
          </div>
          <div class="kpi">
            <div class="label">持倉市值</div>
            <div class="value">${fmtNum(alloc.positionsValue, 2)}</div>
            <div class="sublabel">mark-to-market</div>
          </div>
        </div>
      </section>`;
  }

  function renderStrategyCards(payloads) {
    const cards = payloads
      .map((p) => {
        const pt = p.paper || {};
        const active = p.meta.id === selectedId ? " active" : "";
        const status = pt.status || (p.error ? "error" : "—");
        const variant = pt.selected_variant || "—";
        const symbol = pt.market_symbol || pt.symbol || "—";
        const lastAction = pt.last_action || "—";
        const equity = pt.virtual_equity != null ? fmtNum(pt.virtual_equity, 2) : "—";
        const opens = Array.isArray(pt.open_positions) ? pt.open_positions : [];
        const openSummary =
          opens.length === 0
            ? "無未平倉"
            : opens
                .map(
                  (o) =>
                    `${o.side || "?"} ${fmtNum(o.qty, 3)} @ ${fmtNum(o.entry_price, 4)}`
                )
                .join(" · ");
        const errNote = p.error
          ? `<div class="card-err">${escapeHtml(p.error)}</div>`
          : "";
        return `<button type="button" class="strategy-card${active}" data-id="${escapeHtml(p.meta.id)}">
          <div class="sc-top">
            <strong>${escapeHtml(p.meta.name || pt.strategy_name || p.meta.id)}</strong>
            <span class="badge ${status === "live_demo" || status === "live" ? "ok" : status === "error" ? "warn" : ""}">${escapeHtml(status)}</span>
          </div>
          <div class="sc-meta">
            <span>${escapeHtml(symbol)}</span>
            <span class="dim">·</span>
            <span class="mono dim">${escapeHtml(variant)}</span>
          </div>
          <div class="sc-row"><span class="dim">最近動作</span><span>${escapeHtml(lastAction)}</span></div>
          <div class="sc-row"><span class="dim">未平倉</span><span>${escapeHtml(openSummary)}</span></div>
          <div class="sc-row"><span class="dim">權益</span><span class="mono">${equity}</span></div>
          ${errNote}
        </button>`;
      })
      .join("");

    return `
      <section class="section">
        <div class="section-head"><h2>正在跑的策略</h2>
          <span class="hint">點選卡片檢視詳情</span>
        </div>
        <div class="strategy-cards">${cards || `<div class="empty-state">live_book 尚未配置策略</div>`}</div>
      </section>`;
  }

  function renderDetail(payload) {
    if (!payload) {
      return `<section class="section"><div class="empty-state">請選擇策略</div></section>`;
    }
    const pt = payload.paper || {};
    const settlements = loadSettlements(payload.settlement).slice().reverse();
    const opens = Array.isArray(pt.open_positions) ? pt.open_positions : [];
    const signals = Array.isArray(pt.signals) ? pt.signals.slice().reverse() : [];
    const latestSignal = pt.signal || null;

    const openRows =
      opens.length === 0
        ? `<tr><td colspan="7" class="empty-row">尚無未平倉</td></tr>`
        : opens
            .map((o) => {
              const pair = (o.symbol || "").toUpperCase();
              const mark = markPrices[pair];
              let upnl = o.unrealized_pnl;
              let upct = o.unrealized_pct;
              if (mark && mark.price != null && o.entry_price != null && o.qty != null) {
                const side = (o.side || "LONG").toUpperCase();
                const diff =
                  side === "SHORT"
                    ? Number(o.entry_price) - mark.price
                    : mark.price - Number(o.entry_price);
                upnl = diff * Number(o.qty);
                upct =
                  Number(o.entry_price) !== 0
                    ? (diff / Number(o.entry_price)) * 100
                    : null;
              }
              return `<tr>
                <td>${escapeHtml(o.symbol || "—")}</td>
                <td><span class="badge">${escapeHtml(o.side || "—")}</span></td>
                <td>${fmtNum(o.qty, 4)}</td>
                <td>${fmtNum(o.entry_price, 4)}</td>
                <td>${mark && mark.price != null ? fmtNum(mark.price, 4) : "—"}</td>
                <td class="${clsSigned(upnl)}">${upnl == null ? "—" : fmtNum(upnl, 2)}</td>
                <td>${escapeHtml(o.opened_at || "—")}</td>
              </tr>`;
            })
            .join("");

    const fillRows =
      settlements.length === 0
        ? `<tr><td colspan="8" class="empty-row">尚無成交</td></tr>`
        : settlements
            .slice(0, 30)
            .map((s) => {
              return `<tr>
                <td>${escapeHtml(s.ts || s.entry_time || "—")}</td>
                <td>${escapeHtml(s.symbol || "—")}</td>
                <td><span class="badge">${escapeHtml(s.side || "—")}</span></td>
                <td>${fmtNum(s.qty != null ? s.qty : s.shares, 4)}</td>
                <td>${fmtNum(s.price != null ? s.price : s.entry_price, 4)}</td>
                <td>${escapeHtml(s.status || "—")}</td>
                <td>${escapeHtml(s.reason || s.note || "—")}</td>
                <td>${s.orderId != null ? escapeHtml(String(s.orderId)) : "—"}</td>
              </tr>`;
            })
            .join("");

    const signalRows =
      signals.length === 0 && !latestSignal
        ? `<tr><td colspan="5" class="empty-row">尚無信號</td></tr>`
        : (signals.length
            ? signals.slice(0, 20)
            : [
                {
                  time: pt.updated_at_taipei,
                  symbol: pt.market_symbol,
                  side: latestSignal && latestSignal.long_entry ? "BUY" : latestSignal && latestSignal.exit_long ? "EXIT" : "—",
                  price: latestSignal && latestSignal.close,
                  note: pt.last_action,
                },
              ]
          )
            .map(
              (s) => `<tr>
                <td>${escapeHtml(s.time || "—")}</td>
                <td>${escapeHtml(s.symbol || "—")}</td>
                <td>${escapeHtml(s.side || "—")}</td>
                <td>${fmtNum(s.price, 4)}</td>
                <td>${escapeHtml(s.note || "—")}</td>
              </tr>`
            )
            .join("");

    let liveSignalHtml = "";
    if (latestSignal) {
      liveSignalHtml = `
        <div class="signal-snapshot card-body" style="border-bottom:1px solid var(--border-soft)">
          <div class="chips">
            <span class="chip">close ${fmtNum(latestSignal.close, 4)}</span>
            <span class="chip">donch_hi ${fmtNum(latestSignal.donch_hi, 4)}</span>
            <span class="chip">donch_lo ${fmtNum(latestSignal.donch_lo, 4)}</span>
            <span class="chip">ATR ${fmtNum(latestSignal.atr, 5)}</span>
            <span class="chip stock">stop ${fmtNum(latestSignal.stop, 4)}</span>
            ${latestSignal.long_entry ? '<span class="badge ok">long_entry</span>' : ""}
            ${latestSignal.exit_long ? '<span class="badge warn">exit_long</span>' : ""}
            ${latestSignal.ready ? '<span class="badge ok">ready</span>' : ""}
          </div>
          ${pt.notes ? `<p class="hint" style="margin-top:10px">${escapeHtml(pt.notes)}</p>` : ""}
        </div>`;
    }

    return `
      <section class="section">
        <div class="section-head">
          <h2>策略詳情 · ${escapeHtml(payload.meta.name || payload.meta.id)}</h2>
          <span class="hint">更新：${escapeHtml(pt.updated_at_taipei || "—")}</span>
        </div>
        <div class="card" style="margin-bottom:14px">
          <div class="section-head" style="padding:12px 16px 0;margin:0">
            <h2 style="text-transform:none;letter-spacing:0;font-size:0.9rem;color:var(--text)">未平倉</h2>
          </div>
          <div class="table-scroll">
            <table class="data">
              <thead><tr>
                <th>標的</th><th>方向</th><th>數量</th><th>進場</th><th>標記</th><th>未實現損益</th><th>開倉時間</th>
              </tr></thead>
              <tbody>${openRows}</tbody>
            </table>
          </div>
        </div>
        <div class="grid-2">
          <div class="card">
            <div class="section-head" style="padding:12px 16px 0;margin:0">
              <h2 style="text-transform:none;letter-spacing:0;font-size:0.9rem;color:var(--text)">最近成交</h2>
              <span class="hint">settlement</span>
            </div>
            <div class="table-scroll">
              <table class="data">
                <thead><tr>
                  <th>時間</th><th>標的</th><th>方向</th><th>數量</th><th>價格</th><th>狀態</th><th>原因</th><th>orderId</th>
                </tr></thead>
                <tbody>${fillRows}</tbody>
              </table>
            </div>
          </div>
          <div class="card">
            <div class="section-head" style="padding:12px 16px 0;margin:0">
              <h2 style="text-transform:none;letter-spacing:0;font-size:0.9rem;color:var(--text)">最新信號</h2>
              <span class="hint">paper.signal / signals[]</span>
            </div>
            ${liveSignalHtml}
            <div class="table-scroll">
              <table class="data">
                <thead><tr>
                  <th>時間</th><th>標的</th><th>方向</th><th>價格</th><th>備註</th>
                </tr></thead>
                <tbody>${signalRows}</tbody>
              </table>
            </div>
          </div>
        </div>
      </section>`;
  }

  function renderAll() {
    const main = $("main");
    if (!book) {
      main.innerHTML = `<div class="empty-state">無法載入 live_book.json</div>`;
      return;
    }
    const alloc = computeAllocation(book, strategyPayloads);
    const selected =
      strategyPayloads.find((p) => p.meta.id === selectedId) ||
      strategyPayloads[0] ||
      null;
    if (selected && selectedId !== selected.meta.id) {
      selectedId = selected.meta.id;
    }

    main.className = "";
    main.innerHTML =
      renderKpis(alloc) +
      renderAllocation(alloc) +
      renderStrategyCards(strategyPayloads) +
      renderDetail(selected);

    main.querySelectorAll(".strategy-card").forEach((btn) => {
      btn.addEventListener("click", () => {
        selectedId = btn.getAttribute("data-id");
        renderAll();
      });
    });
  }

  async function loadAll() {
    if (loading) return;
    loading = true;
    showErr("");
    try {
      book = await fetchJSON(BOOK_URL);
      refreshSec = Number(book.auto_refresh_sec) || 15;
      if ($("pageTitle")) {
        $("pageTitle").textContent =
          "模擬倉即時 · " + (book.exchange || book.title || "Binance Demo");
      }
      if ($("pageSub") && book.api_base) {
        $("pageSub").textContent =
          (book.title || "活策略模擬倉") + " · " + book.api_base;
      }

      const strategies = Array.isArray(book.strategies) ? book.strategies : [];
      const payloads = await Promise.all(
        strategies.map(async (meta) => {
          try {
            const [paper, settlementRaw] = await Promise.all([
              fetchJSON(meta.paper_path),
              fetchJSON(meta.settlement_path).catch(() => []),
            ]);
            return {
              meta,
              paper,
              settlement: loadSettlements(settlementRaw),
              error: null,
            };
          } catch (e) {
            return {
              meta,
              paper: null,
              settlement: [],
              error: String(e.message || e),
            };
          }
        })
      );
      strategyPayloads = payloads;
      if (!selectedId && payloads.length) selectedId = payloads[0].meta.id;

      await fetchMarkPrices(payloads);
      renderAll();
      $("lastUpdated").innerHTML =
        "最後更新：<strong>" + escapeHtml(nowTaipeiLabel()) + "</strong>";
    } catch (e) {
      showErr("載入失敗：" + (e.message || e));
      if ($("main").classList.contains("loading")) {
        $("main").textContent = "載入失敗";
      }
    } finally {
      loading = false;
      countdown = refreshSec;
      updateCountdown();
    }
  }

  function updateCountdown() {
    const el = $("countdownPill");
    if (!el) return;
    if (!autoOn) {
      el.textContent = "自動重整：關";
      return;
    }
    el.textContent = "倒數：" + countdown + "s";
  }

  function startTimer() {
    if (timerId) clearInterval(timerId);
    timerId = setInterval(() => {
      if (!autoOn) {
        updateCountdown();
        return;
      }
      countdown -= 1;
      if (countdown <= 0) {
        loadAll();
      } else {
        updateCountdown();
      }
    }, 1000);
  }

  function init() {
    $("btnRefresh").addEventListener("click", () => {
      countdown = refreshSec;
      loadAll();
    });
    const chk = $("autoRefresh");
    chk.addEventListener("change", () => {
      autoOn = chk.checked;
      countdown = refreshSec;
      updateCountdown();
    });
    autoOn = chk.checked;
    loadAll();
    startTimer();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
