/* Watchlist page — reads state/watchlist.json */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const URLS = ["./state/watchlist.json", "./data/watchlist.json"];

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function fmtNum(v, d) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    return Number(v).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d });
  }
  function fmtPct(v) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    return (n > 0 ? "+" : "") + n.toFixed(2) + "%";
  }
  function gradeClass(g) {
    const x = String(g || "").toUpperCase();
    if (x === "QUALIFIED") return "grade-qualified";
    if (x === "CANDIDATE") return "grade-candidate";
    if (x === "REJECT") return "grade-reject";
    return "grade-watch";
  }
  function slotLabel(s) {
    const x = String(s || "idle");
    const map = { idle: "空閒", ARMED: "ARMED", FILLED: "持倉中", blacklist: "黑名單", observe: "觀察" };
    return map[x] || x;
  }
  async function fetchFirst(urls) {
    let last;
    for (const u of urls) {
      try {
        const r = await fetch(u, { cache: "no-store" });
        if (r.ok) return await r.json();
        last = new Error("HTTP " + r.status + " " + u);
      } catch (e) {
        last = e;
      }
    }
    throw last || new Error("no watchlist");
  }
  function render(data) {
    const items = Array.isArray(data.items) ? data.items.slice() : [];
    items.sort((a, b) => {
      const rank = { QUALIFIED: 0, CANDIDATE: 1, WATCH: 2, REJECT: 3 };
      const ga = rank[String(a.grade || "").toUpperCase()] ?? 9;
      const gb = rank[String(b.grade || "").toUpperCase()] ?? 9;
      if (ga !== gb) return ga - gb;
      return (b.oos_wins || 0) - (a.oos_wins || 0);
    });
    const rows = items.length
      ? items
          .map((it) => {
            const grade = String(it.grade || "WATCH").toUpperCase();
            return `<tr>
              <td><strong>${escapeHtml(it.symbol || it.base || "—")}</strong></td>
              <td class="mono">${escapeHtml(it.best_variant || "—")}</td>
              <td class="num">${fmtPct(it.is_return_pct)}<div class="kpi-sub">B&H ${fmtPct(it.bh_return_pct)}</div></td>
              <td class="num">${it.oos_wins != null ? it.oos_wins + "/" + (it.oos_folds || 6) : "—"}</td>
              <td class="num">${fmtPct(it.max_drawdown_pct)}</td>
              <td><span class="grade-pill ${gradeClass(grade)}">${escapeHtml(grade)}</span></td>
              <td class="num">${it.dist_to_breakout_atr != null ? fmtNum(it.dist_to_breakout_atr, 2) + " ATR" : "—"}</td>
              <td>${escapeHtml(slotLabel(it.slot_status))}</td>
            </tr>`;
          })
          .join("")
      : `<tr><td colspan="8" class="empty-row">尚無掃描結果（等待每日 scanner）</td></tr>`;

    $("main").className = "";
    $("main").innerHTML = `
      <section class="section">
        <div class="section-head">
          <h2>宇宙掃描結果</h2>
          <span class="hint">更新 ${escapeHtml(data.updated_at || "—")} · 宇宙 N=${escapeHtml(String(data.universe_n ?? data.universe_n ?? "—"))} · 已掃 ${escapeHtml(String(data.scanned ?? items.length))}</span>
        </div>
        <div class="card"><div class="table-scroll"><table class="data">
          <thead><tr>
            <th>標的</th><th>最佳變體</th><th class="num">IS vs B&H</th><th class="num">OOS</th><th class="num">MaxDD</th><th>分級</th><th class="num">距突破</th><th>狀態</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table></div></div>
        <p class="hint" style="padding:10px 4px">${escapeHtml(data.note || "核心 NEAR 不會被掃描自動替換；僅衛星槽可 ARMED。")}</p>
      </section>`;
  }
  async function load() {
    $("errBanner").style.display = "none";
    try {
      const data = await fetchFirst(URLS);
      render(data);
      $("lastUpdated").textContent = "最後更新：" + (data.updated_at || "—");
    } catch (e) {
      $("errBanner").style.display = "block";
      $("errBanner").textContent = "載入失敗：" + (e.message || e);
      $("main").textContent = "無法載入 state/watchlist.json";
    }
  }
  $("btnRefresh").addEventListener("click", load);
  load();
})();
