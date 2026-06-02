const state = {
  snapshot: null,
  statusFilter: "ALL",
  selectedSymbol: "",
};

const statusClass = {
  READY: "ready",
  READY_SMALL: "ready-small",
  WAIT: "wait",
  BLOCKED: "blocked",
  OBSERVE: "observe",
  EXPANSION: "ready",
  SELECTIVE: "ready-small",
  CHOPPY: "wait",
  WEAK: "observe",
  RISK_OFF: "blocked",
  DEGRADED: "blocked",
  WARNING: "warning",
  OK: "ready",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function text(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value == null || value === "" ? "-" : String(value);
}

function badge(value) {
  const label = String(value || "UNKNOWN");
  return `<span class="badge ${statusClass[label] || "observe"}">${escapeHtml(label)}</span>`;
}

function pct(value) {
  if (value == null || value === "") return "UNKNOWN";
  const number = Number(value);
  if (!Number.isFinite(number)) return "UNKNOWN";
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function money(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return "-";
  if (number >= 100000000) return `${(number / 100000000).toFixed(1)}억`;
  return number.toLocaleString("ko-KR");
}

function render(snapshot) {
  state.snapshot = snapshot;
  renderHeader(snapshot);
  renderThemes(snapshot.ranked_themes || []);
  renderWatchset(snapshot.watchset || []);
  renderOrders(snapshot.entry_candidates || []);
  renderChart(snapshot.selected_chart || {});
  renderGate(snapshot.gate_detail || {});
  renderConditions(snapshot.condition_statuses || []);
  renderDataQuality(snapshot.data_quality || {});
}

function renderHeader(snapshot) {
  const market = snapshot.market || {};
  const summary = snapshot.summary || {};
  text("flow-updated", snapshot.available ? `계산 ${snapshot.calculated_at || "-"} / 갱신 ${snapshot.last_updated_at || "-"}` : "ThemeLabFlow 결과 대기 중");
  document.getElementById("market-strip").innerHTML = [
    badge(market.market_status || "WAITING"),
    `<span class="counter">KOSPI ${pct(market.kospi_return_pct)}</span>`,
    `<span class="counter">KOSDAQ ${pct(market.kosdaq_return_pct)}</span>`,
    `<span class="counter">+3% ${market.market_strong_count || 0}</span>`,
    `<span class="counter">+5% ${market.market_leader_count || 0}</span>`,
    `<span class="counter">WatchSet ${summary.watchset_size || 0}</span>`,
    badge((snapshot.data_quality || {}).status || "UNKNOWN"),
  ].join("");
  text("theme-count", summary.theme_count || 0);
  text("order-count", summary.order_candidate_count || 0);
}

function renderThemes(themes) {
  const node = document.getElementById("theme-rank-list");
  if (!themes.length) {
    node.innerHTML = `<div class="theme-row muted">강한 테마가 형성되면 여기에 표시됩니다.</div>`;
    return;
  }
  node.innerHTML = themes.map((item) => `
    <article class="theme-row" data-theme="${escapeHtml(item.theme_id)}" data-symbol="${escapeHtml(item.top_leader_symbol)}">
      <div class="row-top">
        <span class="row-title">${item.rank}. ${escapeHtml(item.theme_name)}</span>
        ${badge(item.theme_status)}
      </div>
      <div class="row-meta">${themeBreadthLine(item)}</div>
      ${themeConditionLine(item)}
      <div class="row-meta">${leaderLine(item)} · 대장대금 ${money(item.top_leader_turnover_krw)} · ${escapeHtml(item.turnover_label || "수신대금")} ${money(item.theme_turnover_krw)}</div>
      <div class="row-meta muted">대금 기준: ${escapeHtml(item.member_data_coverage_label || "수신 커버리지 확인 중")}</div>
      ${themeQualityLine(item)}
    </article>
  `).join("");
}

function themeBreadthLine(item) {
  return `생존 ${item.alive_count}/${item.eligible_total_members} · 강세 ${(item.strong_ratio * 100).toFixed(0)}% · 주도 ${(item.leader_ratio * 100).toFixed(0)}%`;
}

function leaderLine(item) {
  const leader = item.top_leader_name || item.top_leader_symbol;
  const source = item.top_leader_source ? `/${item.top_leader_source}` : "";
  return leader ? `대장후보${source} ${escapeHtml(leader)}` : "대장후보 미확정";
}

function themeConditionLine(item) {
  if (!item.condition_signal_source) return "";
  const priceStrong = Math.round((item.price_strong_ratio || 0) * 100);
  const priceLeader = Math.round((item.price_leader_ratio || 0) * 100);
  return `<div class="row-meta">조건식 반영: 생존 ${item.condition_alive_count || 0} · 강세 ${item.condition_strong_count || 0} · 주도 ${item.condition_leader_count || 0} <span class="muted">가격기준 강세 ${priceStrong}% / 주도 ${priceLeader}%</span></div>`;
}

function themeQualityLine(item) {
  const flags = item.data_quality_flags || [];
  if (item.quality_label) {
    return `<div class="row-meta muted">데이터 품질: ${escapeHtml(item.quality_label)}</div>`;
  }
  if (!flags.length) return "";
  const missing = flags.filter((flag) => /MISSING/.test(flag)).slice(0, 3).join(", ");
  return `<div class="row-meta warning">데이터 품질: ${escapeHtml(missing || flags.slice(0, 3).join(", "))}</div>`;
}

function renderChart(chart) {
  text("chart-title", `${chart.name || "KOSDAQ"} ${chart.symbol ? `[${chart.symbol}]` : ""}`);
  text("chart-subtitle", `${chart.reason || "INDEX"} / ${chart.chart_data_status || "NO_CANDLE_DATA"}`);
  const stage = document.getElementById("chart-stage");
  const status = chart.chart_data_status || "NO_CANDLE_DATA";
  stage.innerHTML = `
    <div class="empty-chart">
      <strong>${status === "QUOTE_ONLY" ? "실시간 현재가만 수신 중" : "분봉 데이터 없음"}</strong>
      <span>${escapeHtml(chart.symbol || "KOSDAQ")} · ChartUniverse 대상 · VWAP/마커는 데이터가 있을 때만 표시</span>
    </div>
  `;
}

function renderGate(detail) {
  const gate = detail.gate_status || "OBSERVE";
  const gateNode = document.getElementById("gate-status");
  gateNode.textContent = gate;
  gateNode.className = `badge ${statusClass[gate] || "observe"}`;
  text("gate-summary", detail.summary_message || detail.summary_reason || "선택된 WatchSet 종목이 없습니다.");
  const metrics = detail.metrics || {};
  document.getElementById("gate-metrics").innerHTML = [
    ["종목", `${detail.stock_name || "-"} ${detail.symbol || ""}`],
    ["테마", detail.primary_theme || "UNKNOWN"],
    ["역할", detail.stock_role || "UNKNOWN"],
    ["등락률", pct(detail.return_pct)],
    ["가격위치", detail.price_location_status || "UNKNOWN"],
    ["위치 점수", detail.price_location_score ?? "UNKNOWN"],
    ["리스크", detail.risk_level || "UNKNOWN"],
    ["비중", `${detail.position_size_multiplier || 1}배`],
    ["VWAP", metricPct(metrics.vwap_gap_pct)],
    ["고점 이격", metricPct(metrics.distance_to_session_high_pct)],
  ].map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
  const reasons = [...(detail.risk_reason_codes || []), ...(detail.price_location_reason_codes || []), ...(detail.missing_data || [])];
  document.getElementById("gate-reasons").innerHTML = reasons.length
    ? reasons.map((reason) => `<div class="reason-row">${escapeHtml(reason)}</div>`).join("")
    : `<div class="reason-row muted">사유 데이터 없음</div>`;
}

function metricPct(value) {
  if (value == null || value === "") return "UNKNOWN";
  return pct(value);
}

function renderWatchset(items) {
  const filtered = state.statusFilter === "ALL" ? items : items.filter((item) => item.gate_status === state.statusFilter);
  const body = document.getElementById("watchset-body");
  if (!filtered.length) {
    body.innerHTML = `<tr><td colspan="10" class="muted">표시할 WatchSet 데이터가 없습니다.</td></tr>`;
    return;
  }
  body.innerHTML = filtered.map((item) => `
    <tr data-symbol="${escapeHtml(item.symbol)}">
      <td>${badge(item.gate_status)}</td>
      <td>${escapeHtml(item.stock_name || item.symbol)}</td>
      <td>${escapeHtml(item.primary_theme || "-")}</td>
      <td>${escapeHtml(item.stock_role || "-")}</td>
      <td class="num ${Number(item.return_pct || 0) >= 0 ? "positive" : "negative"}">${pct(item.return_pct)}</td>
      <td class="num">${money(item.turnover_krw)}</td>
      <td>${escapeHtml(item.price_location_status || "UNKNOWN")}</td>
      <td>${escapeHtml(item.risk_level || "UNKNOWN")}</td>
      <td class="num">${escapeHtml(item.position_size_multiplier || 1)}배</td>
      <td>${escapeHtml(item.summary_reason || "-")}</td>
    </tr>
  `).join("");
}

function renderOrders(items) {
  const node = document.getElementById("order-candidates");
  if (!items.length) {
    node.innerHTML = `<div class="order-row muted">READY / READY_SMALL 주문 후보가 없습니다.</div>`;
    return;
  }
  node.innerHTML = items.map((item) => `
    <div class="order-row">
      <div class="row-top"><span class="row-title">${item.priority}. ${escapeHtml(item.stock_name || item.symbol)}</span>${badge(item.gate_status)}</div>
      <div class="row-meta">${escapeHtml(item.theme_name || "-")} · ${escapeHtml(item.stock_role || "-")} · ${item.position_size_multiplier}배</div>
      <div class="row-meta">${escapeHtml(item.reason || "-")}</div>
    </div>
  `).join("");
}

function renderConditions(items) {
  const registered = items.filter((item) => item.registered).length;
  text("condition-summary", `${registered}/${items.length || 3}`);
  document.getElementById("condition-list").innerHTML = items.length
    ? items.map((item) => {
      const details = [
        item.purpose,
        `index=${item.resolved_index || "UNKNOWN"}`,
        item.screen_no ? `screen=${item.screen_no}` : "",
      ].filter(Boolean).join(" · ");
      return `
        <div class="quality-row">
          <div class="row-top"><span class="row-title">${escapeHtml(item.condition_name)}</span>${badge(item.registered ? "OK" : "WARNING")}</div>
          <div class="row-meta">${escapeHtml(details)}</div>
          <div class="row-meta">${escapeHtml(item.warning || "정상")}</div>
        </div>
      `;
    }).join("")
    : `<div class="quality-row muted">조건식 상태 데이터 없음</div>`;
}

function renderDataQuality(item) {
  const status = item.status || "UNKNOWN";
  const node = document.getElementById("data-quality-status");
  node.textContent = status;
  node.className = `badge ${statusClass[status] || "observe"}`;
  document.getElementById("data-quality-list").innerHTML = [
    ["요약", item.message || "-"],
    ["quote stale", item.quote_stale_count ?? 0],
    ["전일종가 누락", item.prev_close_missing_count ?? 0],
    ["분봉 누락", item.candle_missing_count ?? 0],
    ["VWAP 누락", item.vwap_missing_count ?? 0],
    ["session high 누락", item.session_high_missing_count ?? 0],
    ["VI 상태", item.vi_status_supported ? "지원" : "미지원"],
    ["실시간 구독", `${item.realtime_subscription_count || 0} / ${item.realtime_subscription_limit || "UNKNOWN"}`],
  ].map(([key, value]) => `<div class="quality-row"><div class="row-top"><span>${escapeHtml(key)}</span><strong>${escapeHtml(value)}</strong></div></div>`).join("");
}

function initFilters() {
  document.getElementById("watch-filters").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-status]");
    if (!button) return;
    state.statusFilter = button.dataset.status;
    document.querySelectorAll("#watch-filters button").forEach((node) => node.classList.toggle("active", node === button));
    renderWatchset((state.snapshot || {}).watchset || []);
  });
  document.getElementById("watchset-body").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-symbol]");
    if (!row || !state.snapshot) return;
    const item = (state.snapshot.watchset || []).find((candidate) => candidate.symbol === row.dataset.symbol);
    if (item) renderGate(item);
  });
}

async function fetchSnapshot() {
  const response = await fetch("/api/themelab/snapshot", { cache: "no-store" });
  if (!response.ok) throw new Error(`snapshot ${response.status}`);
  render(await response.json());
}

function connectWs() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${protocol}://${window.location.host}/ws/dashboard`);
  ws.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    const snapshot = payload.snapshot || {};
    if (snapshot.theme_lab) render(snapshot.theme_lab);
  };
  ws.onclose = () => setTimeout(connectWs, 1500);
}

initFilters();
fetchSnapshot().catch(() => {});
connectWs();
setInterval(fetchSnapshot, 5000);
