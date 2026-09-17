/* Crypto strategy dashboard — static GitHub Pages client */
(function () {
  "use strict";

  const DATA_BASE = "./data";
  const $ = (id) => document.getElementById(id);

  let equityChart = null;
  let currentId = null;
  let strategiesMeta = null;

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

  function isCryptoEntry(s) {
    return !!(s && (s.is_crypto || /crypto/i.test(s.id || "")));
  }

  async function fetchText(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status} · ${url}`);
    return r.text();
  }

  async function fetchJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status} · ${url}`);
    return r.json();
  }

  function periodReturnFromEquity(equity, n) {
    if (!equity || n < 2) return null;
    const window = equity.length >= n ? equity.slice(-n) : equity;
    if (window.length < 2) return null;
    const start = window[0].equity;
    const end = window[window.length - 1].equity;
    if (start == null || end == null || start === 0) return null;
    return Math.round((end / start - 1) * 10000) / 10000;
  }

  function enrichShortHorizonMetrics(metrics, equity) {
    const out = Object.assign({}, metrics || {});
    if (out.weekly_return_pct == null) {
      const w = periodReturnFromEquity(equity, 7);
      if (w != null) {
        out.weekly_return_pct = w;
        out.weekly_return_pct_source = "equity_curve";
      } else {
        out.weekly_return_pct_source = null;
      }
    } else {
      out.weekly_return_pct_source = "metrics";
    }
    if (out.period_return_pct == null) {
      const p = periodReturnFromEquity(equity, 30);
      if (p != null) {
        out.period_return_pct = p;
        out.period_return_pct_source = "equity_curve";
      } else {
        out.period_return_pct_source = null;
      }
    } else {
      out.period_return_pct_source = "metrics";
    }
    return out;
  }

  function parseEquityCSV(text) {
    const lines = text.trim().split(/\r?\n/);
    if (lines.length < 2) return [];
    const headers = lines[0].split(",").map((h) => h.trim());
    const idx = (name) => headers.indexOf(name);
    const iDate = idx("date");
    const iEquity = idx("equity");
    const iClose = idx("close");
    const iHalted = idx("halted");
    const iDd = idx("drawdown_pct");
    const rows = [];
    for (let i = 1; i < lines.length; i++) {
      const cols = lines[i].split(",");
      if (!cols.length) continue;
      const equityRaw = iEquity >= 0 ? cols[iEquity] : "";
      const closeRaw = iClose >= 0 ? cols[iClose] : "";
      const ddRaw = iDd >= 0 ? cols[iDd] : "";
      const haltedRaw = iHalted >= 0 ? (cols[iHalted] || "").toLowerCase() : "";
      try {
        rows.push({
          date: iDate >= 0 ? cols[iDate] : "",
          equity: equityRaw !== "" ? parseFloat(equityRaw) : null,
          close: closeRaw !== "" ? parseFloat(closeRaw) : null,
          halted: ["1", "true", "yes"].includes(haltedRaw),
          drawdown_pct: ddRaw !== "" ? parseFloat(ddRaw) : null,
        });
      } catch (_) {
        /* skip bad row */
      }
    }
    return rows;
  }

  function loadSettlements(data) {
    if (Array.isArray(data)) return data;
    if (data && Array.isArray(data.settlements)) return data.settlements;
    return [];
  }

  async function loadStrategies() {
    const data = await fetchJSON(`${DATA_BASE}/strategies.json`);
    strategiesMeta = data;
    const sel = $("strategySelect");
    const prev = currentId || sel.value;
    sel.innerHTML = "";
    const strategies = data.strategies || [];
    const crypto = strategies.filter(isCryptoEntry);
    const other = strategies.filter((s) => !isCryptoEntry(s));

    function addOpt(parent, s) {
      const opt = document.createElement("option");
      opt.value = s.id;
      const ret = s.total_return_pct != null ? ` (${fmtPct(s.total_return_pct)})` : "";
      const sym = s.symbol ? ` · ${s.symbol}` : "";
      const badge =
        s.approval_status === "research_unapproved"
          ? " 【未核准】"
          : s.approval_label
            ? ` 【${s.approval_label}】`
            : "";
      opt.textContent =
        s.id +
        (s.selected_variant ? ` · ${s.selected_variant}` : "") +
        sym +
        ret +
        badge;
      parent.appendChild(opt);
    }

    if (crypto.length && other.length) {
      const gCrypto = document.createElement("optgroup");
      gCrypto.label = "Crypto 短線";
      crypto.forEach((s) => addOpt(gCrypto, s));
      sel.appendChild(gCrypto);
      const gOther = document.createElement("optgroup");
      gOther.label = "股票／歷史";
      other.forEach((s) => addOpt(gOther, s));
      sel.appendChild(gOther);
    } else {
      strategies.forEach((s) => addOpt(sel, s));
    }

    if (!strategies.length) {
      showErr("找不到策略（請檢查 data/strategies.json）。");
      $("main").innerHTML =
        '<div class="empty-state">尚無策略資料（需有 data/strategies.json）</div>';
      return null;
    }

    const defaultId =
      data.default ||
      (crypto[0] && crypto[0].id) ||
      strategies[0].id;
    if (prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
    else if (defaultId && [...sel.options].some((o) => o.value === defaultId))
      sel.value = defaultId;
    currentId = sel.value;
    return currentId;
  }

  function symbolChips(m) {
    const list = [];
    if (Array.isArray(m.symbols) && m.symbols.length) {
      m.symbols.forEach((s) => {
        if (s && !list.includes(s)) list.push(s);
      });
    }
    if (m.symbol && !list.includes(m.symbol)) list.unshift(m.symbol);
    if (!list.length) return "";
    return `<div class="chips">${list
      .map((s) => `<span class="chip">${escapeHtml(s)}</span>`)
      .join("")}</div>`;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }


  function metaFor(id) {
    return (
      (strategiesMeta &&
        (strategiesMeta.strategies || []).find((s) => s.id === id)) ||
      {}
    );
  }

  function renderApprovalBanner(payload) {
    const meta = metaFor(payload.id);
    const status = meta.approval_status || "";
    const label = meta.approval_label || "";
    const note = meta.approval_note || "";
    if (!status && !label) return "";
    const isUnapproved = status === "research_unapproved";
    const isCandidate = status === "candidate_pending_sim";
    const isMainline = status === "mainline_candidate_paper";
    const cls = isUnapproved
      ? "approval-banner warn"
      : isMainline || isCandidate
        ? "approval-banner candidate"
        : "approval-banner info";
    const title = label || status;
    return `<div class="${cls}" role="status">
      <strong>${escapeHtml(title)}</strong>
      ${note ? `<span>${escapeHtml(note)}</span>` : ""}
      ${
        isUnapproved
          ? "<span>尚未贏過 B&amp;H，不可當作已核准上線策略。</span>"
          : isMainline
            ? "<span>Emily 已認可回測；模擬盤進行中，結算／信號接入 settlement 與 paper_trading。</span>"
            : isCandidate
              ? "<span>已贏 B&amp;H，但仍無 OOS／模擬盤；僅候選預設，實盤前需再過模擬。</span>"
              : ""
      }
    </div>`;
  }

  function renderKPIs(payload) {
    const r = payload.results || {};
    const m = r.metrics || {};
    const name = r.selected_variant || payload.id;
    const vsBH =
      m.total_return_pct != null && m.buy_hold_return_pct != null
        ? Number(m.total_return_pct) - Number(m.buy_hold_return_pct)
        : null;
    const chips = symbolChips(m);
    const periodLabel =
      m.period_return_pct_source === "metrics" ? "期間報酬 %" : "近 30 日報酬 %";
    const weeklyLabel =
      m.weekly_return_pct_source === "metrics" ? "週報酬 %" : "近 7 交易日 %";

    const cards = [
      {
        label: "選定策略／變體",
        value: escapeHtml(name),
        cls: "neutral",
        wide: true,
        sub:
          m.period_start || m.period_end
            ? `${m.period_start || "?"} → ${m.period_end || "?"}`
            : "",
        extra: chips,
      },
      {
        label: "總報酬 %",
        value: fmtPct(m.total_return_pct),
        cls: clsSigned(m.total_return_pct),
      },
      { label: "CAGR %", value: fmtPct(m.cagr_pct), cls: clsSigned(m.cagr_pct) },
      {
        label: "Max DD %",
        value: fmtPct(m.max_drawdown_pct),
        cls: clsSigned(m.max_drawdown_pct),
      },
      { label: "Sharpe", value: fmtNum(m.sharpe, 4), cls: clsSigned(m.sharpe) },
      { label: "勝率 %", value: fmtPct(m.win_rate_pct), cls: "neutral" },
      { label: "成交筆數", value: fmtInt(m.n_trades), cls: "neutral" },
      {
        label: "在市／持倉 %",
        value: fmtPct(m.time_in_market_pct),
        cls: "neutral",
      },
      {
        label: "vs Buy&Hold 落差",
        value: fmtPct(vsBH),
        cls: clsSigned(vsBH),
        sub: `B&H ${fmtPct(m.buy_hold_return_pct)}`,
      },
      {
        label: weeklyLabel,
        value: fmtPct(m.weekly_return_pct),
        cls: clsSigned(m.weekly_return_pct),
        sub:
          m.weekly_return_pct_source === "equity_curve"
            ? "由 equity_curve 推算"
            : "",
      },
      {
        label: periodLabel,
        value: fmtPct(m.period_return_pct),
        cls: clsSigned(m.period_return_pct),
        sub:
          m.period_return_pct_source === "equity_curve"
            ? "由 equity_curve 推算"
            : "",
      },
    ];

    return `
      <section class="section">
        <div class="section-head"><h2>總覽 KPI</h2>
          <span class="hint">初始 ${fmtNum(m.initial_capital, 0)} → 期末 ${fmtNum(m.final_equity, 2)}</span>
        </div>
        <div class="kpi-grid">
          ${cards
            .map(
              (c) => `
            <div class="kpi${c.wide ? " wide" : ""}">
              <div class="label">${c.label}</div>
              <div class="value ${c.cls}">${c.value}</div>
              ${c.sub ? `<div class="sublabel">${c.sub}</div>` : ""}
              ${c.extra || ""}
            </div>`
            )
            .join("")}
        </div>
      </section>`;
  }

  function renderChart() {
    return `
      <section class="section">
        <div class="section-head">
          <h2>資金曲線</h2>
          <div class="chart-actions hint">Chart.js · equity_curve.csv</div>
        </div>
        <div class="card"><div class="card-body">
          <div class="chart-wrap"><canvas id="equityChart"></canvas></div>
        </div></div>
      </section>`;
  }

  function mountChart(equity) {
    const ctx = document.getElementById("equityChart");
    if (!ctx) return;
    if (equityChart) {
      equityChart.destroy();
      equityChart = null;
    }
    const labels = (equity || []).map((r) => r.date);
    const data = (equity || []).map((r) => r.equity);
    const dd = (equity || []).map((r) => r.drawdown_pct);
    equityChart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "資金曲線 Equity",
            data,
            borderColor: "#3b82f6",
            backgroundColor: "rgba(59,130,246,0.12)",
            fill: true,
            tension: 0.15,
            pointRadius: 0,
            borderWidth: 2,
            yAxisID: "y",
          },
          {
            label: "回撤 %",
            data: dd,
            borderColor: "#ef4444",
            backgroundColor: "transparent",
            fill: false,
            tension: 0.15,
            pointRadius: 0,
            borderWidth: 1,
            borderDash: [4, 3],
            yAxisID: "y1",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { color: "#8b9bb0", boxWidth: 12 } },
          tooltip: {
            callbacks: {
              label(ctx) {
                const v = ctx.parsed.y;
                if (ctx.dataset.yAxisID === "y1") return `回撤: ${fmtPct(v)}`;
                return `權益: ${fmtNum(v, 2)}`;
              },
            },
          },
        },
        scales: {
          x: {
            ticks: { color: "#5e6e82", maxTicksLimit: 10 },
            grid: { color: "rgba(36,48,65,0.6)" },
          },
          y: {
            position: "left",
            ticks: {
              color: "#8b9bb0",
              callback: (v) => Number(v).toLocaleString(),
            },
            grid: { color: "rgba(36,48,65,0.6)" },
            title: { display: true, text: "權益 Equity", color: "#5e6e82" },
          },
          y1: {
            position: "right",
            ticks: { color: "#ef4444", callback: (v) => v + "%" },
            grid: { drawOnChartArea: false },
            title: { display: true, text: "Drawdown %", color: "#5e6e82" },
          },
        },
      },
    });
  }

  function renderVariants(payload) {
    const variants = (payload.results && payload.results.all_variants) || [];
    const selected = payload.results && payload.results.selected_variant;
    const rows = variants
      .map((v) => {
        const sel = v.name === selected ? " selected" : "";
        const badge =
          v.name === selected ? ' <span class="badge ok">選定</span>' : "";
        return `<tr class="${sel}">
          <td>${escapeHtml(v.name || "—")}${badge}</td>
          <td>${escapeHtml(v.note || "—")}</td>
          <td>${fmtNum(v.score, 4)}</td>
          <td class="${clsSigned(v.total_return_pct)}">${fmtPct(v.total_return_pct)}</td>
          <td class="${clsSigned(v.cagr_pct)}">${fmtPct(v.cagr_pct)}</td>
          <td class="${clsSigned(v.max_drawdown_pct)}">${fmtPct(v.max_drawdown_pct)}</td>
          <td class="${clsSigned(v.sharpe)}">${fmtNum(v.sharpe, 4)}</td>
          <td>${fmtInt(v.n_trades)}</td>
          <td>${fmtPct(v.win_rate_pct)}</td>
          <td>${fmtNum(v.profit_factor, 4)}</td>
          <td>${fmtPct(v.time_in_market_pct)}</td>
        </tr>`;
      })
      .join("");
    return `
      <section class="section">
        <div class="section-head"><h2>策略變體比較</h2>
          <span class="hint">${variants.length} 個變體</span>
        </div>
        <div class="card"><div class="table-scroll">
          <table class="data">
            <thead><tr>
              <th>名稱</th><th>說明</th><th>Score</th><th>總報酬%</th><th>CAGR%</th>
              <th>MaxDD%</th><th>Sharpe</th><th>筆數</th><th>勝率%</th><th>PF</th><th>在市%</th>
            </tr></thead>
            <tbody>${rows || '<tr><td colspan="11">無變體資料</td></tr>'}</tbody>
          </table>
        </div></div>
      </section>`;
  }

  function renderTrades(payload) {
    const trades = (payload.results && payload.results.trades) || [];
    const hasSymbol = trades.some((t) => t.symbol);
    const sorted = [...trades].sort((a, b) =>
      String(b.exit_date || "").localeCompare(String(a.exit_date || ""))
    );
    const rows = sorted
      .map(
        (t) => `<tr>
        ${
          hasSymbol
            ? `<td>${
                t.symbol
                  ? `<span class="chip">${escapeHtml(t.symbol)}</span>`
                  : "—"
              }</td>`
            : ""
        }
        <td>${escapeHtml(t.entry_date || "—")}</td>
        <td>${fmtNum(t.entry_price, 3)}</td>
        <td>${escapeHtml(t.exit_date || "—")}</td>
        <td>${fmtNum(t.exit_price, 3)}</td>
        <td>${fmtNum(t.shares, 4)}</td>
        <td class="${clsSigned(t.pnl)}">${fmtNum(t.pnl, 2)}</td>
        <td class="${clsSigned(t.pnl_pct)}">${fmtPct(t.pnl_pct)}</td>
        <td><span class="badge">${escapeHtml(t.reason || "—")}</span></td>
      </tr>`
      )
      .join("");
    const cols = hasSymbol ? 9 : 8;
    return `
      <section class="section">
        <div class="section-head"><h2>成交明細</h2>
          <span class="hint">共 ${trades.length} 筆 · 依出場時間新→舊</span>
        </div>
        <div class="card"><div class="table-scroll">
          <table class="data">
            <thead><tr>
              ${hasSymbol ? "<th>標的</th>" : ""}
              <th>進場</th><th>進場價</th><th>出場</th><th>出場價</th>
              <th>數量</th><th>損益</th><th>損益%</th><th>原因</th>
            </tr></thead>
            <tbody>${rows || `<tr><td colspan="${cols}">無成交</td></tr>`}</tbody>
          </table>
        </div></div>
      </section>`;
  }

  function renderPaperTrading(payload) {
    const pt = payload.paper_trading || {};
    const settlements = Array.isArray(payload.settlements)
      ? payload.settlements.slice()
      : [];
    const recent = settlements
      .slice()
      .sort((a, b) => String(b.exit_time || b.exit_date || "").localeCompare(String(a.entry_time || a.entry_date || "")))
      .slice(0, 12);
    const opens = Array.isArray(pt.open_positions) ? pt.open_positions : [];
    const exchange = pt.exchange || pt.venue || "—";
    const strategyName =
      pt.strategy_name ||
      payload.id ||
      "—";
    const variant = pt.selected_variant || (payload.results && payload.results.selected_variant) || "";
    const equity =
      pt.virtual_equity != null
        ? fmtNum(pt.virtual_equity, 2)
        : "待 API";
    const equityHint = pt.virtual_equity_source || pt.status || "";
    const currency = pt.currency || "USDT";
    const statusLabel = pt.label || "模擬倉";

    const openRows = opens.length
      ? opens
          .map((s) => `<tr>
              <td>${escapeHtml(s.symbol || "—")}</td>
              <td>${escapeHtml(s.side || "—")}</td>
              <td class="num">${escapeHtml(s.qty != null ? s.qty : "—")}</td>
              <td class="num">${escapeHtml(s.entry_price != null ? s.entry_price : "—")}</td>
              <td class="num ${clsSigned(s.unrealized_pnl)}">${escapeHtml(
                s.unrealized_pnl != null ? fmtNum(s.unrealized_pnl, 2) : "—"
              )}</td>
              <td>${escapeHtml(s.opened_at || "—")}</td>
            </tr>`)
          .join("")
      : `<tr><td colspan="6" class="empty-row">尚無未平倉（API 接通後寫入 paper_trading.json → open_positions[]）</td></tr>`;

    const fillRows = recent.length
      ? recent
          .map((s) => {
            const pnl = s.pnl;
            return `<tr>
              <td>${escapeHtml(s.exit_time || s.exit_date || s.entry_time || "—")}</td>
              <td>${escapeHtml(s.symbol || pt.symbol || "—")}</td>
              <td>${escapeHtml(s.side || "—")}</td>
              <td class="num ${clsSigned(pnl)}">${escapeHtml(
                pnl != null ? fmtNum(pnl, 2) : "—"
              )}</td>
              <td class="num">${escapeHtml(s.fee != null ? fmtNum(s.fee, 4) : "—")}</td>
              <td class="num">${escapeHtml(
                s.cumulative_equity != null ? fmtNum(s.cumulative_equity, 2) : "—"
              )}</td>
              <td>${escapeHtml(s.note || "")}</td>
            </tr>`;
          })
          .join("")
      : `<tr><td colspan="7" class="empty-row">尚無成交（讀 settlement.json；API／模擬結算寫入後顯示）</td></tr>`;

    return `<section class="section">
      <div class="section-head"><h2>模擬倉</h2><span class="hint">${escapeHtml(statusLabel)}</span></div>
      <div class="kpi-grid paper-account">
        <div class="kpi"><div class="kpi-label">交易所</div><div class="kpi-value">${escapeHtml(exchange)}</div></div>
        <div class="kpi"><div class="kpi-label">策略</div><div class="kpi-value">${escapeHtml(strategyName)}${
          variant ? `<div class="kpi-sub">${escapeHtml(variant)}</div>` : ""
        }</div></div>
        <div class="kpi"><div class="kpi-label">虛擬權益（${escapeHtml(currency)}）</div><div class="kpi-value">${escapeHtml(equity)}</div><div class="kpi-sub">${escapeHtml(equityHint)}</div></div>
        <div class="kpi"><div class="kpi-label">未平倉筆數</div><div class="kpi-value">${fmtInt(opens.length)}</div></div>
        <div class="kpi"><div class="kpi-label">最近成交筆數</div><div class="kpi-value">${fmtInt(recent.length)}</div><div class="kpi-sub">來源 settlement.json</div></div>
      </div>
      <div class="grid-2" style="margin-top:14px">
        <div class="card"><div class="card-body">
          <div class="section-head" style="margin-bottom:10px"><h2 style="text-transform:none;letter-spacing:0;font-size:0.9rem;color:var(--text)">未平倉</h2></div>
          <div class="table-wrap"><table>
            <thead><tr><th>標的</th><th>方向</th><th class="num">數量</th><th class="num">進場價</th><th class="num">未實現損益</th><th>時間</th></tr></thead>
            <tbody>${openRows}</tbody>
          </table></div>
        </div></div>
        <div class="card"><div class="card-body">
          <div class="section-head" style="margin-bottom:10px"><h2 style="text-transform:none;letter-spacing:0;font-size:0.9rem;color:var(--text)">最近成交</h2><span class="hint">settlement</span></div>
          <div class="table-wrap"><table>
            <thead><tr><th>時間</th><th>標的</th><th>方向</th><th class="num">損益</th><th class="num">手續費</th><th class="num">累計權益</th><th>備註</th></tr></thead>
            <tbody>${fillRows}</tbody>
          </table></div>
          <p style="margin-top:10px;font-size:0.75rem;color:var(--text-muted)">
            Bybit Demo API 接通後更新 <code>paper_trading.json</code>（virtual_equity / open_positions）；成交結算寫 <code>settlement.json</code>。
          </p>
        </div></div>
      </div>
    </section>`;
  }

  function renderSettlement(payload) {
    const settlements = payload.settlements || [];
    const rows = settlements
      .map(
        (s) => `<tr>
        <td>${escapeHtml(s.strategy || "—")}</td>
        <td>${escapeHtml(s.entry_time || "—")}</td>
        <td>${escapeHtml(s.exit_time || "—")}</td>
        <td class="${clsSigned(s.pnl)}">${fmtNum(s.pnl, 2)}</td>
        <td>${fmtNum(s.fee, 2)}</td>
        <td>${fmtNum(s.cumulative_equity, 2)}</td>
        <td>${escapeHtml(s.note || "")}</td>
      </tr>`
      )
      .join("");
    const example = `[
  {
    "strategy": "${payload.id}",
    "entry_time": "2026-09-17T09:00:00+08:00",
    "exit_time": "2026-09-17T11:30:00+08:00",
    "pnl": 128.5,
    "fee": 1.2,
    "cumulative_equity": 100128.5,
    "batch_id": "scalp-20260917-a",
    "horizon": "scalp",
    "timeframe": "5m",
    "trade_count": 2,
    "symbol": "<from metrics>",
    "fills": [],
    "note": "短線多筆批次結算"
  }
]`;
    return `
      <section class="section">
        <div class="section-head"><h2>結算區</h2>
          <span class="hint">靜態站：寫入 <code>data/${escapeHtml(payload.id)}/settlement.json</code> 後重整</span>
        </div>
        <div class="settlement-grid">
          <div class="card"><div class="card-body">
            <div class="section-head" style="margin-bottom:10px"><h2 style="text-transform:none;letter-spacing:0;font-size:0.9rem;color:var(--text)">既有結算</h2></div>
            ${
              settlements.length
                ? `
            <div class="table-scroll">
              <table class="data">
                <thead><tr>
                  <th>策略</th><th>進場</th><th>出場</th><th>損益</th><th>手續費</th><th>累計權益</th><th>備註</th>
                </tr></thead>
                <tbody>${rows}</tbody>
              </table>
            </div>`
                : `
            <div class="empty-state">
              尚無結算紀錄（空殼已就緒）<br/><br/>
              檔案：<code>data/${escapeHtml(payload.id)}/settlement.json</code>
            </div>`
            }
          </div></div>
          <div class="card"><div class="card-body">
            <div class="section-head" style="margin-bottom:10px"><h2 style="text-transform:none;letter-spacing:0;font-size:0.9rem;color:var(--text)">Schema 範例</h2></div>
            <pre class="schema-hint">${escapeHtml(example)}</pre>
            <p style="margin-top:10px;font-size:0.75rem;color:var(--text-muted)">
              短線可一次寫多筆到 <code>settlement.json</code> 陣列，或用 <code>batch_id</code> / <code>fills[]</code> 記批次。<br/>
              本站為純靜態：更新 JSON 後按「重整」即可。
            </p>
          </div></div>
        </div>
      </section>`;
  }

  function renderFooter(payload) {
    return `<footer>
      <span>相對路徑 ${escapeHtml(payload.path)}</span>
      <span>generated_at ${escapeHtml(payload.last_updated || "—")}</span>
    </footer>`;
  }

  async function loadStrategy(id) {
    showErr("");
    $("main").innerHTML = '<div class="loading">載入策略…</div>';
    const base = `${DATA_BASE}/${encodeURIComponent(id)}`;
    try {
      const [results, csvText, settlementRaw, paperRaw] = await Promise.all([
        fetchJSON(`${base}/results.json`),
        fetchText(`${base}/equity_curve.csv`).catch(() => ""),
        fetchJSON(`${base}/settlement.json`).catch(() => []),
        fetchJSON(`${base}/paper_trading.json`).catch(() => null),
      ]);
      const equity = csvText ? parseEquityCSV(csvText) : [];
      const metrics = enrichShortHorizonMetrics(results.metrics || {}, equity);
      const resultsCopy = Object.assign({}, results, { metrics });
      const meta =
        (strategiesMeta &&
          (strategiesMeta.strategies || []).find((s) => s.id === id)) ||
        {};
      const payload = {
        id,
        path: `./data/${id}`,
        results: resultsCopy,
        equity,
        settlements: loadSettlements(settlementRaw),
        paper_trading: paperRaw,
        last_updated:
          metrics.generated_at_taipei ||
          results.generated_at_taipei ||
          meta.generated_at_taipei ||
          meta.results_mtime ||
          "—",
        is_crypto: isCryptoEntry({ id, is_crypto: meta.is_crypto }),
      };
      currentId = id;
      $("lastUpdated").innerHTML = `最後更新：<strong>${escapeHtml(
        payload.last_updated
      )}</strong>`;
      $("main").innerHTML =
        renderApprovalBanner(payload) +
        renderKPIs(payload) +
        renderChart() +
        renderVariants(payload) +
        renderTrades(payload) +
        renderPaperTrading(payload) +
        renderSettlement(payload) +
        renderFooter(payload);
      mountChart(payload.equity || []);
    } catch (e) {
      showErr("載入失敗：" + e.message);
      $("main").innerHTML = `<div class="empty-state">${escapeHtml(
        e.message
      )}</div>`;
    }
  }

  async function refreshAll() {
    try {
      const id = await loadStrategies();
      if (id) await loadStrategy(id);
    } catch (e) {
      showErr("無法讀取資料：" + e.message);
      $("main").innerHTML = `<div class="empty-state">${escapeHtml(
        e.message
      )}</div>`;
    }
  }

  $("btnRefresh").addEventListener("click", refreshAll);
  $("strategySelect").addEventListener("change", (e) =>
    loadStrategy(e.target.value)
  );
  refreshAll();
})();
