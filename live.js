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
  let allocChart = null;
  let modalProfile = null;

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
    const capital = alloc.capital || 5000;
    const holdings = alloc.holdings || {};
    const usdtCash = alloc.usdtCash || 0;
    const equity = alloc.equity || 0;

    // 實際持倉：不含 USDT 現金列
    const actualRows = [];
    for (const [asset, h] of Object.entries(holdings)) {
      const qty = h.qty || 0;
      const spot = h.price;
      let entry = null, stop = null, upnl = null, upct = null, side = "LONG";
      let targetPx = null; // 目標／出場參考：通道下軌或 stop 旁註
      for (const p of strategyPayloads) {
        const pt = p.paper || {};
        for (const o of pt.open_positions || []) {
          const os = String(o.symbol || "").toUpperCase();
          if (os === asset + "USDT" || os === asset || os.startsWith(asset)) {
            entry = o.entry_price != null ? Number(o.entry_price) : entry;
            stop = o.stop != null ? Number(o.stop) : stop;
            side = o.side || side;
            if (spot != null && entry != null && o.qty != null) {
              const diff = String(side).toUpperCase() === "SHORT" ? entry - spot : spot - entry;
              upnl = diff * Number(o.qty);
              upct = entry !== 0 ? (diff / entry) * 100 : null;
            }
          }
        }
        if (pt.signal && (String(pt.symbol || "").toUpperCase() === asset || String(pt.market_symbol || "").toUpperCase() === asset + "USDT")) {
          if (pt.signal.donch_lo != null) targetPx = Number(pt.signal.donch_lo);
        }
      }
      actualRows.push({ asset, qty, spot, entry, stop, targetPx, upnl, upct, source: h.source || "—", value: h.value || 0 });
    }

    const actualHtml = actualRows.length
      ? actualRows.map((r) => {
          const tgtStop = [
            r.targetPx != null ? `目標參考(下軌) ${fmtNum(r.targetPx, 4)}` : "目標：跌破下軌／trail",
            r.stop != null ? `止損 ${fmtNum(r.stop, 4)}` : "止損 未設定",
          ].join(" · ");
          const pnl = r.upnl == null ? "—" : `${fmtNum(r.upnl, 2)} (${fmtPct(r.upct)})`;
          return `<tr>
            <td><strong>${escapeHtml(r.asset)}</strong></td>
            <td class="num">${fmtNum(r.qty, 3)}</td>
            <td class="num">${r.spot != null ? fmtNum(r.spot, 4) : "—"}<div class="kpi-sub">${escapeHtml(r.source)}</div></td>
            <td class="num">${r.entry != null ? fmtNum(r.entry, 4) : "—"}</td>
            <td>${escapeHtml(tgtStop)}</td>
            <td class="num">${fmtNum(r.value, 2)}</td>
            <td class="num ${clsSigned(r.upnl)}"><strong>${pnl}</strong></td>
          </tr>`;
        }).join("")
      : `<tr><td colspan="7" class="empty-row">尚無幣種持倉</td></tr>`;

    // 預計持倉：排除已 open、CASH/reserve
    const planned = (Array.isArray(book.planned_positions) ? book.planned_positions : []).filter(
      (pl) => pl.status !== "open" && pl.status !== "reserve" && pl.asset !== "CASH"
    );

    const plannedHtml = planned.length
      ? planned.map((pl) => {
          const targetUsdt = ((Number(pl.target_pct) || 0) / 100) * capital;
          const pair = (pl.asset || "") + "USDT";
          const mark = markPrices[pair];
          const ref = mark && mark.price != null ? Number(mark.price) : pl.ref_price != null ? Number(pl.ref_price) : null;
          const notional = pl.target_notional_usdt != null ? Number(pl.target_notional_usdt) : targetUsdt;
          let buyQty = pl.target_buy_qty;
          if (pl.no_order && pl.asset === "DOGE") {
            buyQty = 0;
          } else if ((buyQty == null || buyQty === "") && ref && ref > 0) {
            buyQty = notional / ref;
          }
          let buyLabel;
          if (pl.asset === "DOGE" || pl.status === "pending_swap") {
            buyLabel = `不購買<div class="kpi-sub">槽位待置換→ARB</div>`;
          } else if (buyQty != null) {
            buyLabel = fmtNum(buyQty, 3) + `<div class="kpi-sub">≈ 目標 ${fmtNum(notional, 0)} USDT` + (ref ? ` ÷ ${fmtNum(ref, 4)}` : "") + `</div>`;
          } else {
            buyLabel = `待定<div class="kpi-sub">≈ ${fmtNum(notional, 0)} USDT · 綠燈後依市價</div>`;
          }
          const stopLabel = pl.stop_mode === "pending" ? "未開倉 → 無有效止損" : (pl.stop_note || "—");
          const sid = pl.strategy_id || ("planned-" + pl.asset);
          return `<tr class="click-row" data-profile="${escapeHtml(sid)}" role="button" tabindex="0">
            <td><strong>${escapeHtml(pl.asset)}</strong><div class="kpi-sub">${escapeHtml(pl.strategy_name || pl.notes || "")}</div></td>
            <td class="num">${fmtNum(pl.target_pct, 0)}%</td>
            <td class="num">${fmtNum(targetUsdt, 2)}</td>
            <td class="num">${buyLabel}</td>
            <td>${escapeHtml(pl.status_label || pl.status || "—")}</td>
            <td>${escapeHtml(pl.entry_rule || "—")}</td>
            <td>${escapeHtml(pl.exit_rule || "—")}</td>
            <td>${escapeHtml(stopLabel)}</td>
            <td>${escapeHtml(pl.leverage || "1x")}</td>
          </tr>`;
        }).join("")
      : `<tr><td colspan="9" class="empty-row">目前沒有待開的預計持倉（黃燈衛星等綠燈）</td></tr>`;

    // pie data from actual coin holdings + cash
    const pieParts = [];
    if (usdtCash > 0) pieParts.push({ label: "USDT 現金", value: usdtCash });
    for (const [asset, h] of Object.entries(holdings)) {
      if ((h.value || 0) > 0) pieParts.push({ label: asset, value: h.value });
    }
    if (!pieParts.length && equity > 0) pieParts.push({ label: "權益", value: equity });

    return `
      <section class="section">
        <div class="section-head"><h2>資產配置</h2><span class="hint">圓餅＝實際市值比重（含現金）</span></div>
        <div class="card" style="padding:16px">
          <div class="alloc-pie-wrap"><canvas id="allocPie" height="220"></canvas></div>
          <script type="application/json" id="allocPieData">${escapeHtml(JSON.stringify(pieParts))}</script>
        </div>
      </section>
      <section class="section">
        <div class="section-head"><h2>實際持倉</h2><span class="hint">現金見上方帳戶總覽；此處只列幣種倉</span></div>
        <div class="card"><div class="table-scroll"><table class="data">
          <thead><tr>
            <th>資產</th><th class="num">數量</th><th class="num">現價</th><th class="num">進場價</th><th>目標價格／止損</th><th class="num">USDT 市值</th><th class="num">目前損益</th>
          </tr></thead>
          <tbody>${actualHtml}</tbody>
        </table></div></div>
      </section>
      <section class="section">
        <div class="section-head"><h2>預計持倉</h2><span class="hint">尚未開倉的配置計畫 · 點列可看策略說明</span></div>
        <div class="card"><div class="table-scroll"><table class="data">
          <thead><tr>
            <th>資產</th><th class="num">目標%</th><th class="num">目標 USDT</th><th class="num">預計購買量</th><th>狀態</th><th>進場條件</th><th>結算／出場</th><th>止損</th><th>倍數</th>
          </tr></thead>
          <tbody>${plannedHtml}</tbody>
        </table></div>
        <p class="hint" style="padding:10px 16px 14px;margin:0">已進場標的不重複列在這裡。點列開啟策略簡介；回測詳情可從彈窗進入。</p>
        </div>
      </section>`;
  }

  function renderKpis(alloc) {
    const cashTarget = (Array.isArray(book.target_allocation) ? book.target_allocation : [])
      .find((t) => t.asset === "CASH" || (t.symbol || "").toUpperCase() === "USDT");
    const cashPct = cashTarget ? Number(cashTarget.pct) || 15 : 15;
    const cashTargetUsdt = (cashPct / 100) * (alloc.capital || 5000);
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
            <div class="label">起始資金／保留現金目標</div>
            <div class="value neutral">${fmtNum(alloc.capital, 2)}</div>
            <div class="sublabel">目標保留現金 ${fmtNum(cashPct, 0)}% ≈ ${fmtNum(cashTargetUsdt, 0)} USDT · 目前現金 ${fmtNum(alloc.usdtCash, 2)}</div>
          </div>
          <div class="kpi">
            <div class="label">損益 %</div>
            <div class="value ${clsSigned(alloc.pnlPct)}">${fmtPct(alloc.pnlPct)}</div>
            <div class="sublabel">${fmtNum(alloc.pnl, 2)} USDT</div>
          </div>
          <div class="kpi">
            <div class="label">持倉市值</div>
            <div class="value">${fmtNum(alloc.positionsValue, 2)}</div>
            <div class="sublabel">幣種 mark-to-market</div>
          </div>
        </div>
      </section>`;
  }

  function profileFromMeta(meta, paper) {
    const pt = paper || {};
    return {
      id: meta.id,
      name: meta.name || pt.strategy_name || meta.id,
      summary: meta.summary || pt.notes || "",
      description: meta.description || "",
      entry_rule: meta.entry_rule || "",
      exit_rule: meta.exit_rule || "",
      stop_rule: meta.stop_rule || "",
      timeframe: meta.timeframe || pt.timeframe || "",
      symbol: meta.symbol || pt.market_symbol || pt.symbol || "",
      leverage: meta.leverage || pt.leverage || "1x",
      oos_note: meta.oos_note || "",
      backtest_url: meta.backtest_url || `./backtest.html?strategy=${encodeURIComponent(meta.id)}`,
      status: pt.status || meta.status || "",
    };
  }

  function openStrategyModal(profile) {
    modalProfile = profile;
    const modal = $("strategyModal");
    const body = $("modalBody");
    if (!modal || !body || !profile) return;
    body.innerHTML = `
      <h2 style="margin:0 0 8px;font-size:1.15rem">${escapeHtml(profile.name)}</h2>
      <div class="chips" style="margin-bottom:12px">
        ${profile.symbol ? `<span class="chip">${escapeHtml(profile.symbol)}</span>` : ""}
        ${profile.timeframe ? `<span class="chip">${escapeHtml(profile.timeframe)}</span>` : ""}
        ${profile.leverage ? `<span class="chip">${escapeHtml(profile.leverage)} 現貨</span>` : ""}
        ${profile.status ? `<span class="badge">${escapeHtml(profile.status)}</span>` : ""}
      </div>
      <p style="color:var(--text);margin:0 0 10px">${escapeHtml(profile.summary || "—")}</p>
      ${profile.description ? `<p class="hint" style="margin:0 0 14px">${escapeHtml(profile.description)}</p>` : ""}
      <div class="grid-2">
        <div class="card"><div class="card-body">
          <div class="section-head" style="margin-bottom:8px"><h2 style="text-transform:none;letter-spacing:0;font-size:0.85rem;color:var(--text)">進場規則</h2></div>
          <p style="margin:0">${escapeHtml(profile.entry_rule || "—")}</p>
        </div></div>
        <div class="card"><div class="card-body">
          <div class="section-head" style="margin-bottom:8px"><h2 style="text-transform:none;letter-spacing:0;font-size:0.85rem;color:var(--text)">出場／結算</h2></div>
          <p style="margin:0">${escapeHtml(profile.exit_rule || "—")}</p>
        </div></div>
        <div class="card"><div class="card-body">
          <div class="section-head" style="margin-bottom:8px"><h2 style="text-transform:none;letter-spacing:0;font-size:0.85rem;color:var(--text)">止損</h2></div>
          <p style="margin:0">${escapeHtml(profile.stop_rule || "—")}</p>
        </div></div>
        <div class="card"><div class="card-body">
          <div class="section-head" style="margin-bottom:8px"><h2 style="text-transform:none;letter-spacing:0;font-size:0.85rem;color:var(--text)">回測／OOS</h2></div>
          <p style="margin:0 0 10px">${escapeHtml(profile.oos_note || "可至回測頁查看 IS／OOS")}</p>
          <a class="nav-link active" style="display:inline-block" href="${escapeHtml(profile.backtest_url)}">打開回測資料 →</a>
        </div></div>
      </div>`;
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
  }

  function closeStrategyModal() {
    const modal = $("strategyModal");
    if (!modal) return;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    modalProfile = null;
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
            : opens.map((o) => `${o.side || "?"} ${fmtNum(o.qty, 3)} @ ${fmtNum(o.entry_price, 4)}`).join(" · ");
        const errNote = p.error ? `<div class="card-err">${escapeHtml(p.error)}</div>` : "";
        return `<div class="strategy-card-wrap">
          <button type="button" class="strategy-card${active}" data-id="${escapeHtml(p.meta.id)}">
            <div class="sc-top">
              <strong>${escapeHtml(p.meta.name || pt.strategy_name || p.meta.id)}</strong>
              <span class="badge ${status === "live_demo" || status === "live" ? "ok" : status === "error" ? "warn" : ""}">${escapeHtml(status)}</span>
            </div>
            <div class="sc-meta"><span>${escapeHtml(symbol)}</span><span class="dim">·</span><span class="mono dim">${escapeHtml(variant)}</span></div>
            <div class="sc-row"><span class="dim">最近動作</span><span>${escapeHtml(lastAction)}</span></div>
            <div class="sc-row"><span class="dim">未平倉</span><span>${escapeHtml(openSummary)}</span></div>
            <div class="sc-row"><span class="dim">權益</span><span class="mono">${equity}</span></div>
            ${errNote}
          </button>
          <button type="button" class="ghost strategy-info-btn" data-profile-id="${escapeHtml(p.meta.id)}">策略說明／回測</button>
        </div>`;
      })
      .join("");

    const sats = Array.isArray(book.satellite_strategies) ? book.satellite_strategies : [];
    const satCards = sats
      .map((s) => `<div class="strategy-card-wrap">
        <button type="button" class="strategy-card dim-card" data-sat="${escapeHtml(s.id)}">
          <div class="sc-top"><strong>${escapeHtml(s.name)}</strong><span class="badge warn">${escapeHtml(s.status || "pending")}</span></div>
          <div class="sc-meta"><span>${escapeHtml(s.symbol || s.asset || "")}</span><span class="dim">·</span><span>${escapeHtml(s.timeframe || "")}</span></div>
          <div class="sc-row"><span class="dim">目標配置</span><span>${fmtNum(s.target_pct, 0)}%</span></div>
          <div class="sc-row"><span class="dim">狀態</span><span>黃燈暫緩 · 未開倉</span></div>
        </button>
        <button type="button" class="ghost strategy-info-btn" data-sat-profile="${escapeHtml(s.id)}">策略說明</button>
      </div>`)
      .join("");

    return `
      <section class="section">
        <div class="section-head"><h2>正在跑的策略</h2><span class="hint">點卡片看倉位詳情；「策略說明」看規則與回測</span></div>
        <div class="strategy-cards">${cards || `<div class="empty-state">尚未配置策略</div>`}</div>
      </section>
      <section class="section">
        <div class="section-head"><h2>預計策略（衛星）</h2><span class="hint">黃燈未解除前不會真開</span></div>
        <div class="strategy-cards">${satCards || `<div class="empty-state">無衛星策略</div>`}</div>
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
    const levDefault = pt.leverage || (payload.meta && payload.meta.leverage) || "1x";

    const openRows =
      opens.length === 0
        ? `<tr><td colspan="10" class="empty-row">尚無未平倉</td></tr>`
        : opens
            .map((o) => {
              const pair = (o.symbol || "").toUpperCase();
              const mark = markPrices[pair];
              const entry = o.entry_price != null ? Number(o.entry_price) : null;
              const qty = o.qty != null ? Number(o.qty) : null;
              const markPx = mark && mark.price != null ? Number(mark.price) : null;
              let upnl = o.unrealized_pnl;
              let upct = o.unrealized_pct;
              if (markPx != null && entry != null && qty != null) {
                const side = (o.side || "LONG").toUpperCase();
                const diff = side === "SHORT" ? entry - markPx : markPx - entry;
                upnl = diff * qty;
                upct = entry !== 0 ? (diff / entry) * 100 : null;
              }
              const notional = qty != null && (markPx != null || entry != null) ? qty * (markPx != null ? markPx : entry) : null;
              const lev = o.leverage || levDefault || "1x";
              const targetPx = latestSignal && latestSignal.donch_lo != null ? Number(latestSignal.donch_lo) : null;
              const tgtStop = [
                targetPx != null ? `目標參考 ${fmtNum(targetPx, 4)}` : "目標：跌破下軌／trail",
                o.stop != null ? `止損 ${fmtNum(o.stop, 4)}` : "止損 未設定",
              ].join(" · ");
              const pnlCell = upnl == null ? "—" : `${fmtNum(upnl, 2)} USDT` + (upct != null ? `<div class="kpi-sub">${fmtPct(upct)}</div>` : "");
              return `<tr>
                <td>${escapeHtml(o.opened_at || "—")}</td>
                <td>${escapeHtml(o.symbol || "—")}</td>
                <td><span class="badge">${escapeHtml(o.side || "—")}</span></td>
                <td class="num">${fmtNum(qty, 3)}</td>
                <td class="num">${entry != null ? fmtNum(entry, 4) : "—"}</td>
                <td class="num">${markPx != null ? fmtNum(markPx, 4) : "—"}<div class="kpi-sub">${escapeHtml((mark && mark.source) || "—")}</div></td>
                <td class="num">${notional != null ? fmtNum(notional, 2) : "—"}</td>
                <td class="num ${clsSigned(upnl)}"><strong>${pnlCell}</strong></td>
                <td>${escapeHtml(tgtStop)}</td>
                <td>${escapeHtml(String(lev))} · 現貨</td>
              </tr>`;
            })
            .join("");

    const fillRows =
      settlements.length === 0
        ? `<tr><td colspan="8" class="empty-row">尚無成交</td></tr>`
        : settlements.slice(0, 30).map((s) => `<tr>
              <td>${escapeHtml(s.ts || s.entry_time || "—")}</td>
              <td>${escapeHtml(s.symbol || "—")}</td>
              <td><span class="badge">${escapeHtml(s.side || "—")}</span></td>
              <td class="num">${fmtNum(s.qty != null ? s.qty : s.shares, 4)}</td>
              <td class="num">${fmtNum(s.price != null ? s.price : s.entry_price, 4)}</td>
              <td>${escapeHtml(s.status || "—")}</td>
              <td>${escapeHtml(s.reason || s.note || "—")}</td>
              <td>${s.orderId != null ? escapeHtml(String(s.orderId)) : "—"}</td>
            </tr>`).join("");

    return `
      <section class="section">
        <div class="section-head">
          <h2>策略詳情 · ${escapeHtml(payload.meta.name || payload.meta.id)}</h2>
          <span class="hint">更新：${escapeHtml(pt.updated_at_taipei || "—")} · <button type="button" class="linkish" id="btnOpenProfile">策略說明／回測</button></span>
        </div>
        <div class="card" style="margin-bottom:14px">
          <div class="section-head" style="padding:12px 16px 0;margin:0">
            <h2 style="text-transform:none;letter-spacing:0;font-size:0.9rem;color:var(--text)">未平倉 · 目前損益</h2>
          </div>
          <div class="table-scroll"><table class="data">
            <thead><tr>
              <th>時間</th><th>標的</th><th>方向</th><th class="num">數量</th><th class="num">進場價</th><th class="num">現價</th><th class="num">USDT 價值</th><th class="num">目前損益</th><th>目標價格／止損</th><th>倍數</th>
            </tr></thead>
            <tbody>${openRows}</tbody>
          </table></div>
        </div>
        <div class="card">
          <div class="section-head" style="padding:12px 16px 0;margin:0">
            <h2 style="text-transform:none;letter-spacing:0;font-size:0.9rem;color:var(--text)">最近成交</h2>
          </div>
          <div class="table-scroll"><table class="data">
            <thead><tr>
              <th>時間</th><th>標的</th><th>方向</th><th class="num">數量</th><th class="num">價格</th><th>狀態</th><th>原因</th><th>orderId</th>
            </tr></thead>
            <tbody>${fillRows}</tbody>
          </table></div>
        </div>
      </section>`;
  }


  function mountAllocPie() {
    const canvas = $("allocPie");
    const dataEl = $("allocPieData");
    if (!canvas || !dataEl || typeof Chart === "undefined") return;
    let parts = [];
    try { parts = JSON.parse(dataEl.textContent || "[]"); } catch (_) { parts = []; }
    if (allocChart) {
      allocChart.destroy();
      allocChart = null;
    }
    if (!parts.length) return;
    allocChart = new Chart(canvas.getContext("2d"), {
      type: "doughnut",
      data: {
        labels: parts.map((p) => p.label),
        datasets: [{
          data: parts.map((p) => p.value),
          backgroundColor: ["#3b82f6", "#22c55e", "#f59e0b", "#a855f7", "#ef4444", "#22d3ee"],
          borderWidth: 0,
        }],
      },
      options: {
        plugins: {
          legend: { position: "bottom", labels: { color: "#8b9bb0" } },
        },
      },
    });
  }

  function wireLiveClicks() {
    document.querySelectorAll(".strategy-info-btn[data-profile-id]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const id = btn.getAttribute("data-profile-id");
        const payload = strategyPayloads.find((x) => x.meta.id === id);
        if (payload) openStrategyModal(profileFromMeta(payload.meta, payload.paper));
      });
    });
    document.querySelectorAll(".strategy-info-btn[data-sat-profile]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const id = btn.getAttribute("data-sat-profile");
        const sat = (book.satellite_strategies || []).find((s) => s.id === id);
        if (sat) openStrategyModal(sat);
      });
    });
    document.querySelectorAll(".strategy-card[data-sat]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.getAttribute("data-sat");
        const sat = (book.satellite_strategies || []).find((s) => s.id === id);
        if (sat) openStrategyModal(sat);
      });
    });
    document.querySelectorAll("tr.click-row[data-profile]").forEach((tr) => {
      tr.addEventListener("click", () => {
        const id = tr.getAttribute("data-profile");
        const sat = (book.satellite_strategies || []).find((s) => s.id === id || s.asset && id.includes(s.asset.toLowerCase()));
        const planned = (book.planned_positions || []).find((p) => (p.strategy_id || ("planned-" + p.asset)) === id);
        if (sat) return openStrategyModal(sat);
        if (planned) {
          openStrategyModal({
            id,
            name: planned.strategy_name || planned.asset,
            summary: planned.status_label || "",
            description: planned.notes || "",
            entry_rule: planned.entry_rule,
            exit_rule: planned.exit_rule,
            stop_rule: planned.stop_note || planned.stop_mode,
            timeframe: "",
            symbol: planned.asset + "USDT",
            leverage: planned.leverage || "1x",
            status: planned.status,
            backtest_url: "./backtest.html",
            oos_note: "衛星策略回測待綠燈後補檔",
          });
        }
      });
    });
    const btnProf = $("btnOpenProfile");
    if (btnProf) {
      btnProf.addEventListener("click", () => {
        const payload = strategyPayloads.find((x) => x.meta.id === selectedId);
        if (payload) openStrategyModal(profileFromMeta(payload.meta, payload.paper));
      });
    }
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
    mountAllocPie();
    wireLiveClicks();

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
