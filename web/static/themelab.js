const state = {
  snapshot: null,
  filters: {
    status: "ALL",
    role: "ALL",
    market: "ALL",
    data: "ALL",
    order: "ALL",
  },
  selectedSymbol: "",
};

const statusClass = {
  READY: "ready",
  READY_SMALL: "ready-small",
  WAIT: "wait",
  BLOCKED: "blocked",
  OBSERVE: "observe",
  LATE_CHASE_TEMP_WAIT: "wait",
  CHASE_RISK_BLOCKED: "blocked",
  WAIT_MARKET_CONFIRMATION_PENDING: "wait",
  WAIT_MARKET_RECOVERY_PENDING: "wait",
  WAIT_MARKET_STATE_CONSERVATIVE_FALLBACK: "wait",
  WAIT_CANDIDATE_MARKET_RISK_OFF: "blocked",
  WAIT_CANDIDATE_MARKET_WEAK: "wait",
  WAIT_FAILED_BREAKOUT: "wait",
  WAIT_DEEP_PULLBACK: "wait",
  WAIT_PRICE_LOCATION_UNKNOWN: "wait",
  WAIT_DATA_SUPPORT_NOT_READY: "warning",
  WAIT_DATA_LATEST_TICK_STALE: "warning",
  READY_TO_TRADE: "ready",
  READY_BUT_LIVE_BLOCKED: "warning",
  WAIT_MARKET_CONFIRMATION: "wait",
  WAIT_DATA_QUALITY: "warning",
  OBSERVE_ONLY: "observe",
  RISK_BLOCKED: "blocked",
  NO_SIGNAL: "observe",
  SNAPSHOT_UNAVAILABLE: "observe",
  RUNTIME_INACTIVE: "warning",
  SNAPSHOT_STALE: "warning",
  EXPANSION: "ready",
  SELECTIVE: "ready-small",
  CHOPPY: "wait",
  WEAK: "observe",
  RISK_OFF: "blocked",
  LEADING: "ready",
  ACTIVE: "ready-small",
  WATCH: "wait",
  DEGRADED: "blocked",
  BROKEN: "blocked",
  WARNING: "warning",
  OK: "ready",
};

const displayStatusDescriptions = {
  READY: "매수 게이트 통과",
  READY_SMALL: "소액 관찰 진입 후보",
  LATE_CHASE_TEMP_WAIT: "추격매수 대기",
  CHASE_RISK_BLOCKED: "추격매수 차단",
  WAIT_MARKET_CONFIRMATION_PENDING: "시장 확인 대기",
  WAIT_MARKET_RECOVERY_PENDING: "시장 회복 대기",
  WAIT_FAILED_BREAKOUT: "돌파 실패 대기",
  WAIT_DEEP_PULLBACK: "과도한 눌림 대기",
  WAIT_PRICE_LOCATION_UNKNOWN: "가격 위치 확인 대기",
  WAIT_DATA_SUPPORT_NOT_READY: "지지선 데이터 대기",
  WAIT_DATA_LATEST_TICK_STALE: "틱 데이터 갱신 대기",
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

function setBadge(id, value) {
  const node = document.getElementById(id);
  if (!node) return;
  const label = String(value || "UNKNOWN");
  node.textContent = label;
  node.className = `badge ${statusClass[label] || "observe"}`;
}

function badge(value, tone = "") {
  const label = String(value || "UNKNOWN");
  return `<span class="badge ${tone || statusClass[label] || "observe"}">${escapeHtml(label)}</span>`;
}

function boolBadge(value, yes = "예", no = "아니오") {
  return badge(value ? yes : no, value ? "ready" : "observe");
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

function seconds(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return "-";
  return `${Math.round(number)}초 후 재확인`;
}

function score(value, digits = 1) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number.toFixed(digits);
}

function shortId(value) {
  const label = String(value || "");
  if (!label) return "-";
  if (label.length <= 18) return escapeHtml(label);
  return `<span title="${escapeHtml(label)}">${escapeHtml(`${label.substring(0, 8)}...${label.substring(label.length - 6)}`)}</span>`;
}

function render(snapshot) {
  state.snapshot = snapshot || {};
  if (!state.selectedSymbol && (snapshot.watchset || []).length) {
    state.selectedSymbol = (snapshot.watchset || [])[0].symbol || "";
  }
  renderHeader(snapshot);
  renderCockpit(snapshot);
  renderThemes(snapshot.ranked_themes || []);
  renderWatchset(snapshot.watchset || []);
  renderOrders(snapshot.entry_candidates || []);
  renderChart(snapshot.selected_chart || {});
  const selected = (snapshot.watchset || []).find((item) => item.symbol === state.selectedSymbol) || snapshot.gate_detail || {};
  renderGate(selected);
  renderConditions(snapshot.condition_statuses || []);
  renderDataQuality(snapshot.data_quality || {});
}

function renderHeader(snapshot) {
  const market = snapshot.market || {};
  const summary = snapshot.summary || {};
  const dataQuality = snapshot.data_quality || {};
  const snapshotAge = summary.snapshot_age_label ? ` / age ${summary.snapshot_age_label}` : "";
  text("flow-updated", snapshot.available ? `계산 ${snapshot.calculated_at || "-"} / 갱신 ${snapshot.last_updated_at || "-"}${snapshotAge}` : "ThemeLabFlow 결과 대기 중");
  document.getElementById("market-strip").innerHTML = [
    badge(market.market_status || "WAITING"),
    `<span class="counter">KOSPI ${pct(market.kospi_return_pct)}</span>`,
    `<span class="counter">KOSDAQ ${pct(market.kosdaq_return_pct)}</span>`,
    `<span class="counter">WatchSet ${summary.watchset_size || 0}</span>`,
    badge(dataQuality.status || "UNKNOWN"),
  ].join("");
  text("theme-count", summary.theme_count || 0);
  text("order-count", summary.order_candidate_count || 0);
}

function renderCockpit(snapshot) {
  const summary = snapshot.summary || {};
  const market = snapshot.market || {};
  const dataQuality = snapshot.data_quality || {};
  setBadge("operation-status", summary.operation_status || "SNAPSHOT_UNAVAILABLE");
  text("operation-message", summary.operation_message_ko || "ThemeLabFlow 결과 대기 중");
  const snapshotState = summary.snapshot_stale ? "STALE" : "FRESH";
  const snapshotAge = summary.snapshot_age_label ? `age ${summary.snapshot_age_label}` : `갱신 ${snapshot.last_updated_at || "-"}`;
  text("cockpit-snapshot-state", snapshot.available ? `${snapshotState} / ${snapshotAge}` : "대기");

  const sides = market.sides || [];
  document.getElementById("cockpit-market-sides").innerHTML = sides.length
    ? sides.map((side) => `
      <div class="cockpit-line">
        <strong>${escapeHtml(side.side || "-")}</strong>
        ${badge(side.status || "UNKNOWN")}
        <span>${pct(side.index_return_pct)} · breadth ${pct(side.breadth_pct)} · ${escapeHtml(side.breadth_trust_level || "UNKNOWN")} / ${escapeHtml(side.breadth_source || "-")}</span>
      </div>
    `).join("")
    : `<div class="muted">시장 사이드 데이터 대기</div>`;

  const themeCounts = summary.theme_status_counts || {};
  document.getElementById("cockpit-theme-status").innerHTML = [
    countLine("LEADING", themeCounts.LEADING),
    countLine("ACTIVE", themeCounts.ACTIVE),
    countLine("WATCH", themeCounts.WATCH),
    countLine("WEAK", themeCounts.WEAK),
    `<div class="cockpit-line"><strong>TOP</strong><span>${escapeHtml(summary.top_theme_name || "-")} ${summary.top_theme_score ? `· ${score(summary.top_theme_score, 0)}점` : ""}</span></div>`,
  ].join("");

  document.getElementById("cockpit-candidate-status").innerHTML = [
    countLine("READY", summary.ready_count),
    countLine("READY_SMALL", summary.ready_small_count),
    countLine("WAIT", summary.wait_count),
    countLine("OBSERVE", summary.observe_count),
    countLine("BLOCKED", summary.blocked_count),
  ].join("");

  document.getElementById("cockpit-order-status").innerHTML = [
    countLine("주문 후보", summary.order_candidate_count),
    countLine("제출 가능", summary.submittable_count),
    countLine("의도 생성", summary.runtime_order_intent_created_count),
    countLine("가상 주문", summary.virtual_order_created_count),
  ].join("");

  const dataQualityReasons = (dataQuality.reasons || [])
    .slice(0, 3)
    .map((reason) => `<div class="cockpit-line"><strong>원인</strong><span>${escapeHtml(reason)}</span></div>`);
  document.getElementById("cockpit-data-quality").innerHTML = [
    `<div class="cockpit-line"><strong>상태</strong>${badge(dataQuality.status || "UNKNOWN")}<span>${escapeHtml(dataQuality.message || "-")}</span></div>`,
    ...dataQualityReasons,
    countLine("데이터 대기", summary.data_not_ready_count),
    countLine("진단 전용", summary.diagnostic_only_count),
  ].join("");

  document.getElementById("cockpit-live-readiness").innerHTML = [
    `<div class="cockpit-line"><strong>Runtime</strong>${badge(summary.runtime_status || "UNKNOWN")}<span>${summary.runtime_running ? "running" : "inactive"} ${escapeHtml(summary.runtime_mode || "")}</span></div>`,
    `<div class="cockpit-line"><strong>Snapshot</strong>${badge(summary.snapshot_stale ? "SNAPSHOT_STALE" : "OK")}<span>${escapeHtml(summary.snapshot_age_label || "-")}</span></div>`,
    `<div class="cockpit-line"><strong>LIVE</strong>${boolBadge(summary.live_order_enabled, "활성", "비활성")}</div>`,
    countLine("Guard 통과", summary.live_guard_passed_count),
    countLine("Guard 차단", summary.live_guard_blocked_count),
    countLine("추격 대기", summary.late_chase_wait_count),
    countLine("추격 차단", summary.chase_risk_blocked_count),
  ].join("");
}

function countLine(label, value) {
  return `<div class="cockpit-line"><strong>${escapeHtml(label)}</strong><span>${Number(value || 0).toLocaleString("ko-KR")}</span></div>`;
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
      <div class="row-meta">조건식: 생존 ${item.condition_alive_count || 0} · 강세 ${item.condition_strong_count || 0} · 주도 ${item.condition_leader_count || 0} · 점수 ${score(item.condition_score, 0)}</div>
      <div class="row-meta">가격기반: 생존 ${item.price_alive_count || 0} · 강세 ${item.price_strong_count || 0} · 주도 ${item.price_leader_count || 0} · LIVE ${item.has_live_price_signal ? "수신" : "대기"}</div>
      <div class="row-meta">${leaderLine(item)} · 대장 등락 ${pct(item.top_leader_return_pct)} · 대장대금 ${money(item.top_leader_turnover_krw)}</div>
      <div class="row-meta">${escapeHtml(item.turnover_label || "수신대금")} ${money(item.theme_turnover_krw)} · ${escapeHtml(item.member_data_coverage_label || "수신 커버리지 확인 중")}</div>
      ${themeQualityLine(item)}
    </article>
  `).join("");
}

function themeBreadthLine(item) {
  const total = item.eligible_total_members || 0;
  return `통합 폭: 생존 ${item.alive_count}/${total} · 강세 ${item.strong_count}/${total} · 주도 ${item.leader_count}/${total}`;
}

function leaderLine(item) {
  const leader = item.top_leader_name || item.top_leader_symbol;
  const source = item.top_leader_source ? `/${item.top_leader_source}` : "";
  return leader ? `대장후보${source} ${escapeHtml(leader)} ${item.top_leader_symbol ? `[${escapeHtml(item.top_leader_symbol)}]` : ""}` : "대장후보 미확정";
}

function themeQualityLine(item) {
  const flags = item.data_quality_flags || [];
  const status = item.theme_quality_status || (item.quality_label || flags.length ? "WARNING" : "OK");
  const tone = item.theme_quality_tone || statusClass[status] || (status === "OK" ? "ready" : "warning");
  const label = item.quality_label || (!flags.length ? "정상" : flags.slice(0, 3).join(", "));
  const detail = themeQualityDetail(item);
  return [
    `<div class="row-meta ${tone}">데이터 품질: ${badge(status, tone)} ${escapeHtml(label)}</div>`,
    detail ? `<div class="row-meta muted">원인/조치: ${escapeHtml(detail)}</div>` : "",
  ].filter(Boolean).join("");
}

function themeQualityDetail(item) {
  const reasons = (item.theme_quality_reasons || []).slice(0, 2);
  const parts = [];
  if (reasons.length) parts.push(reasons.join(" · "));
  if (item.theme_quality_action && item.theme_quality_status !== "OK") parts.push(item.theme_quality_action);
  const priority = item.theme_backfill_priority || "NONE";
  if (priority !== "NONE") {
    const trs = (item.theme_backfill_trs || []).slice(0, 2).join(", ");
    const symbols = (item.theme_backfill_symbols || []).slice(0, 5).join(", ");
    const status = item.theme_backfill_status ? ` · 상태 ${item.theme_backfill_status}` : "";
    const failure = item.theme_backfill_failure_reason ? `(${item.theme_backfill_failure_reason})` : "";
    parts.push(`TR 보강 ${priority}${status}${failure}${trs ? `: ${trs}` : ""}${symbols ? ` · 후보 ${symbols}` : ""}`);
  }
  return parts.join(" · ");
}

function renderChart(chart) {
  text("chart-title", `${chart.name || "KOSDAQ"} ${chart.symbol ? `[${chart.symbol}]` : ""}`);
  text("chart-subtitle", `${chart.reason || "INDEX"} / ${chart.chart_data_status || "NO_CANDLE_DATA"} / bars ${chart.completed_minute_bar_count || 0}`);
  const stage = document.getElementById("chart-stage");
  const status = chart.chart_data_status || "NO_CANDLE_DATA";
  const candles = normalizeCandles(chart.candles || chart.recent_candles_1m || chart.recent_candles_3m);
  if (candles.length) {
    stage.innerHTML = minuteChartSvg(chart, candles);
    return;
  }
  stage.innerHTML = `
    <div class="empty-chart">
      <strong>${status === "QUOTE_ONLY" ? "실시간 현재가만 수신 중" : "분봉 데이터 없음"}</strong>
      <span>${escapeHtml(chart.symbol || "KOSDAQ")} · ChartUniverse 대상 · VWAP/마커는 데이터가 있을 때만 표시</span>
    </div>
  `;
}

function normalizeCandles(values) {
  if (!Array.isArray(values)) return [];
  return values.map((item) => ({
    start_at: item.start_at || "",
    open: numberOrNull(item.open),
    high: numberOrNull(item.high),
    low: numberOrNull(item.low),
    close: numberOrNull(item.close),
    volume: numberOrNull(item.volume) || 0,
    completed: item.completed !== false,
  })).filter((item) => item.open !== null && item.high !== null && item.low !== null && item.close !== null);
}

function minuteChartSvg(chart, candles) {
  const width = 900;
  const height = 420;
  const margin = { top: 20, right: 82, bottom: 34, left: 48 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;
  const refs = chartReferenceLines(chart);
  const priceValues = candles.flatMap((item) => [item.open, item.high, item.low, item.close])
    .concat(refs.map((item) => item.value))
    .filter((value) => value !== null && Number.isFinite(value));
  let minPrice = Math.min(...priceValues);
  let maxPrice = Math.max(...priceValues);
  if (minPrice === maxPrice) {
    minPrice -= Math.max(1, minPrice * 0.01);
    maxPrice += Math.max(1, maxPrice * 0.01);
  }
  const pad = Math.max(1, (maxPrice - minPrice) * 0.08);
  minPrice -= pad;
  maxPrice += pad;
  const y = (price) => margin.top + ((maxPrice - price) / (maxPrice - minPrice)) * innerHeight;
  const xStep = innerWidth / Math.max(1, candles.length);
  const candleWidth = Math.max(5, Math.min(18, xStep * 0.55));
  const grid = [0, 0.25, 0.5, 0.75, 1].map((ratio) => {
    const yy = margin.top + innerHeight * ratio;
    const price = maxPrice - (maxPrice - minPrice) * ratio;
    return `
      <line class="chart-grid-line" x1="${margin.left}" y1="${yy.toFixed(2)}" x2="${width - margin.right}" y2="${yy.toFixed(2)}"></line>
      <text class="chart-axis-label" x="${width - margin.right + 8}" y="${(yy + 4).toFixed(2)}">${escapeHtml(formatPrice(price))}</text>
    `;
  }).join("");
  const bodies = candles.map((item, index) => {
    const x = margin.left + xStep * (index + 0.5);
    const openY = y(item.open);
    const closeY = y(item.close);
    const highY = y(item.high);
    const lowY = y(item.low);
    const bodyY = Math.min(openY, closeY);
    const bodyHeight = Math.max(2, Math.abs(closeY - openY));
    const tone = item.close >= item.open ? "up" : "down";
    const completeClass = item.completed ? "" : " provisional";
    return `
      <line class="chart-wick ${tone}${completeClass}" x1="${x.toFixed(2)}" y1="${highY.toFixed(2)}" x2="${x.toFixed(2)}" y2="${lowY.toFixed(2)}"></line>
      <rect class="chart-candle ${tone}${completeClass}" x="${(x - candleWidth / 2).toFixed(2)}" y="${bodyY.toFixed(2)}" width="${candleWidth.toFixed(2)}" height="${bodyHeight.toFixed(2)}"></rect>
    `;
  }).join("");
  const overlays = refs.map((item) => {
    const yy = y(item.value);
    return `
      <line class="chart-ref ${item.kind}" x1="${margin.left}" y1="${yy.toFixed(2)}" x2="${width - margin.right}" y2="${yy.toFixed(2)}"></line>
      <text class="chart-ref-label ${item.kind}" x="${margin.left + 8}" y="${(yy - 5).toFixed(2)}">${escapeHtml(item.label)} ${escapeHtml(formatPrice(item.value))}</text>
    `;
  }).join("");
  const footer = [
    candles[candles.length - 1]?.start_at || "",
    chart.recent_support_source || "",
    chart.prev_close_inferred_from_change_rate ? "prev_close inferred" : "",
  ].filter(Boolean).join(" / ");
  return `
    <svg class="minute-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="minute candlestick chart">
      <rect class="chart-bg" x="0" y="0" width="${width}" height="${height}"></rect>
      ${grid}
      ${overlays}
      ${bodies}
      <text class="chart-footer" x="${margin.left}" y="${height - 10}">${escapeHtml(footer)}</text>
    </svg>
  `;
}

function chartReferenceLines(chart) {
  return [
    { key: "vwap", label: "VWAP", kind: "vwap" },
    { key: "recent_support_price", label: "SUPPORT", kind: "support" },
    { key: "breakout_level", label: "BREAKOUT", kind: "breakout" },
    { key: "upper_limit_price", label: "UPPER", kind: "upper" },
    { key: "current_price", label: "LAST", kind: "last" },
  ].map((item) => ({ ...item, value: numberOrNull(chart[item.key]) }))
    .filter((item) => item.value !== null && Number.isFinite(item.value));
}

function numberOrNull(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function formatPrice(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number.toLocaleString("ko-KR", { maximumFractionDigits: number >= 100 ? 0 : 2 });
}

function renderGate(detail) {
  const gate = detail.gate_status || "OBSERVE";
  const display = detail.display_status || gate;
  setBadge("gate-status", gate);
  setBadge("gate-display-status", display);
  const description = displayStatusDescriptions[display] ? ` · ${displayStatusDescriptions[display]}` : "";
  text("gate-summary", `${detail.summary_message || detail.summary_reason || "선택된 WatchSet 종목이 없습니다."}${description}`);
  document.getElementById("gate-detail-sections").innerHTML = gateSections(detail)
    .map((section) => gateSectionHtml(section.title, section.fields, detail, section.open))
    .join("");
}

function gateSections(detail) {
  return [
    {
      title: "최종 판단",
      open: true,
      fields: [
        ["gate_status", "게이트"],
        ["display_status", "표시 상태"],
        ["submittable", "제출 가능"],
        ["diagnostic_only", "진단 전용"],
        ["order_eligibility", "주문 적격"],
        ["summary_reason", "요약 사유"],
      ],
    },
    {
      title: "테마/역할",
      open: true,
      fields: [
        ["primary_theme", "테마"],
        ["theme_score", "테마 점수"],
        ["stock_role", "역할"],
        ["condition_level", "조건 레벨"],
        ["position_size_multiplier", "비중 배수"],
      ],
    },
    {
      title: "가격 위치/눌림",
      open: true,
      fields: [
        ["price_location_status", "가격 위치"],
        ["price_location_score", "위치 점수"],
        ["support_source", "지지선 출처"],
        ["support_price", "지지 가격"],
        ["support_ready", "지지선 준비"],
        ["support_ready_reason", "지지선 사유"],
        ["vwap_ready", "VWAP 준비"],
        ["recent_support_ready", "최근 지지"],
        ["base_line_120_ready", "120선 준비"],
        ["base_line_120_candle_count", "120선 캔들"],
        ["metrics.support_gap_pct", "지지선 이격"],
        ["metrics.vwap_gap_pct", "VWAP 이격"],
        ["metrics.pullback_from_high_pct", "고점 눌림"],
        ["metrics.distance_to_session_high_pct", "당일고점 이격"],
        ["momentum_1m", "1분 모멘텀"],
        ["momentum_1m_missing_reason", "1분 모멘텀 사유"],
        ["momentum_3m", "3분 모멘텀"],
        ["momentum_3m_missing_reason", "3분 모멘텀 사유"],
      ],
    },
    {
      title: "추격매수/리스크",
      open: false,
      fields: [
        ["chase_risk", "추격 리스크"],
        ["chase_risk_reason", "추격 사유"],
        ["late_chase_level", "추격 레벨"],
        ["late_chase_score", "추격 점수"],
        ["late_chase_block_type", "차단 유형"],
        ["late_chase_recoverable", "회복 가능"],
        ["late_chase_recheck_after_sec", "재확인"],
        ["risk_level", "리스크"],
        ["risk_reason_codes", "리스크 코드"],
      ],
    },
    {
      title: "시장 확인",
      open: false,
      fields: [
        ["candidate_market", "시장"],
        ["market_raw_status", "원시 상태"],
        ["market_confirmed_status", "확정 상태"],
        ["market_confirmation_pending", "확인 대기"],
        ["market_recovery_pending", "회복 대기"],
        ["market_weak_consecutive_cycles", "약세 연속"],
        ["market_risk_off_consecutive_cycles", "Risk-off 연속"],
        ["market_healthy_consecutive_cycles", "건강 연속"],
        ["market_wait_reason", "대기 사유"],
        ["market_wait_recheck_after_sec", "재확인"],
        ["market_side_breadth_pct", "시장 폭"],
        ["market_side_index_return_pct", "지수 등락"],
        ["market_side_turnover_weighted_return_pct", "대금가중 등락"],
        ["market_side_breadth_source", "폭 출처"],
        ["market_side_breadth_trust_level", "폭 신뢰도"],
        ["market_side_breadth_gate_usable", "폭 게이트 사용"],
        ["market_side_source_conflict", "출처 충돌"],
        ["market_side_valid_quote_ratio", "유효 quote"],
        ["market_side_sample_count", "표본 수"],
      ],
    },
    {
      title: "주문 연결",
      open: false,
      fields: [
        ["entry_plan_created", "진입 플랜"],
        ["runtime_order_intent_created", "주문 의도"],
        ["virtual_order_created", "가상 주문"],
        ["live_order_enabled", "LIVE 활성"],
        ["live_order_guard_passed", "LIVE Guard"],
        ["blocked_reason", "차단 사유"],
        ["blocked_reason_codes", "차단 코드"],
        ["candidate_instance_id", "후보 ID"],
      ],
    },
    {
      title: "데이터 품질",
      open: false,
      fields: [
        ["latest_tick_ready", "틱 준비"],
        ["latest_tick_age_sec", "틱 나이"],
        ["data_quality_flags", "품질 플래그"],
        ["price_location_data_quality_flags", "가격 품질 플래그"],
        ["missing_data", "누락 데이터"],
      ],
    },
  ];
}

function gateSectionHtml(title, fields, detail, open) {
  return `
    <details class="gate-section" ${open ? "open" : ""}>
      <summary>${escapeHtml(title)}</summary>
      <dl class="metric-list">
        ${fields.map(([path, label]) => {
          const value = readPath(detail, path);
          const important = ["support_ready_reason", "market_wait_reason", "market_wait_recheck_after_sec", "late_chase_recheck_after_sec", "missing_data"].includes(path);
          return `<div class="${important ? "attention" : ""}"><dt>${escapeHtml(label)}</dt><dd>${formatField(path, value)}</dd></div>`;
        }).join("")}
      </dl>
    </details>
  `;
}

function readPath(item, path) {
  return path.split(".").reduce((current, key) => (current == null ? undefined : current[key]), item);
}

function formatField(path, value) {
  if (Array.isArray(value)) return escapeHtml(value.length ? value.join(", ") : "-");
  if (typeof value === "boolean") return boolBadge(value);
  if (path.endsWith("_pct")) return escapeHtml(pct(value));
  if (path.endsWith("_krw") || path === "support_price") return escapeHtml(money(value));
  if (path.endsWith("_after_sec") || path.endsWith("_age_sec")) return escapeHtml(seconds(value));
  if (path === "position_size_multiplier") return escapeHtml(`${value ?? 1}배`);
  return escapeHtml(value == null || value === "" ? "-" : value);
}

function renderWatchset(items) {
  const filtered = items.filter(matchesFilters);
  text("watch-count", `${filtered.length}/${items.length}`);
  const body = document.getElementById("watchset-body");
  if (!filtered.length) {
    body.innerHTML = `<tr><td colspan="14" class="muted">표시할 WatchSet 데이터가 없습니다.</td></tr>`;
    return;
  }
  body.innerHTML = filtered.map((item) => `
    <tr data-symbol="${escapeHtml(item.symbol)}" class="${item.symbol === state.selectedSymbol ? "selected" : ""}">
      <td>${statusCell(item)}</td>
      <td><strong>${escapeHtml(item.stock_name || item.symbol)}</strong><br><span class="muted">${escapeHtml(item.code || item.symbol || "-")}</span></td>
      <td>${escapeHtml(item.primary_theme || "-")}</td>
      <td>${badge(item.stock_role || "-")}</td>
      <td>${badge(item.candidate_market || "UNKNOWN")}</td>
      <td class="num ${Number(item.return_pct || 0) >= 0 ? "positive" : "negative"}">${pct(item.return_pct)}</td>
      <td class="num">${money(item.turnover_krw)}</td>
      <td>${score(item.theme_score, 0)} / L${escapeHtml(item.condition_level ?? "-")}</td>
      <td>${escapeHtml(item.price_location_status || "UNKNOWN")}<br><span class="muted">${score(item.price_location_score, 0)}점</span></td>
      <td>${readinessLine(item)}</td>
      <td>${chaseLine(item)}</td>
      <td>${marketLine(item)}</td>
      <td>${orderLinkLine(item)}</td>
      <td class="reason-cell">${escapeHtml(item.summary_reason || "-")}</td>
    </tr>
  `).join("");
}

function statusCell(item) {
  const display = item.display_status || item.gate_status;
  const displayBadge = display && display !== item.gate_status ? `<br>${badge(display)}` : "";
  return `${badge(item.gate_status)}${displayBadge}`;
}

function readinessLine(item) {
  return [
    `지지 ${item.support_ready ? "OK" : "대기"}`,
    `VWAP ${item.vwap_ready ? "OK" : "대기"}`,
    `최근 ${item.recent_support_ready ? "OK" : "대기"}`,
    `120선 ${item.base_line_120_ready ? "OK" : "대기"}${item.base_line_120_candle_count ? `/${item.base_line_120_candle_count}` : ""}`,
  ].map(escapeHtml).join("<br>");
}

function chaseLine(item) {
  return [
    item.chase_risk ? badge("CHASE_RISK_BLOCKED") : badge("PASS", "ready"),
    escapeHtml(item.late_chase_level || "-"),
    item.late_chase_score == null ? "" : `점수 ${score(item.late_chase_score, 0)}`,
    seconds(item.late_chase_recheck_after_sec),
  ].filter(Boolean).join("<br>");
}

function marketLine(item) {
  return [
    badge(item.market_confirmed_status || "UNKNOWN"),
    item.market_confirmation_pending ? escapeHtml("확인 대기") : "",
    item.market_recovery_pending ? escapeHtml("회복 대기") : "",
    escapeHtml(`폭 ${pct(item.market_side_breadth_pct)} / ${item.market_side_breadth_trust_level || "UNKNOWN"}`),
    item.market_side_source_conflict ? escapeHtml("출처 충돌") : "",
    escapeHtml(seconds(item.market_wait_recheck_after_sec)),
  ].filter(Boolean).join("<br>");
}

function orderLinkLine(item) {
  return [
    item.runtime_order_intent_created ? badge("의도", "ready") : badge("의도 없음", "observe"),
    item.virtual_order_created ? badge("가상", "ready-small") : "",
    item.live_order_guard_passed ? badge("LIVE 통과", "ready") : badge("LIVE 거부", "warning"),
    item.submittable ? badge("제출 가능", "ready") : "",
    item.diagnostic_only ? badge("진단 전용", "warning") : "",
  ].filter(Boolean).join("<br>");
}

function matchesFilters(item) {
  return matchesStatus(item, state.filters.status)
    && matchesRole(item, state.filters.role)
    && matchesMarket(item, state.filters.market)
    && matchesData(item, state.filters.data)
    && matchesOrder(item, state.filters.order);
}

function matchesStatus(item, value) {
  if (value === "ALL") return true;
  const gate = item.gate_status || "";
  const display = item.display_status || "";
  if (["READY", "READY_SMALL", "OBSERVE", "BLOCKED"].includes(value)) return gate === value;
  if (value === "WAIT") return gate === "WAIT" || display.startsWith("WAIT") || display === "LATE_CHASE_TEMP_WAIT";
  if (value === "LATE_CHASE_TEMP_WAIT") return display === "LATE_CHASE_TEMP_WAIT";
  if (value === "WAIT_FAILED_BREAKOUT") return display === "WAIT_FAILED_BREAKOUT";
  if (value === "WAIT_DEEP_PULLBACK") return display === "WAIT_DEEP_PULLBACK";
  if (value === "WAIT_PRICE_LOCATION_UNKNOWN") return display === "WAIT_PRICE_LOCATION_UNKNOWN";
  if (value === "MARKET_PENDING") return isMarketPending(item);
  if (value === "DATA_NOT_READY") return isDataNotReady(item);
  if (value === "LIVE_GUARD_BLOCKED") return gateIsReadyLike(item) && !item.live_order_guard_passed;
  return display === value;
}

function matchesRole(item, value) {
  return value === "ALL" || (item.stock_role || "UNKNOWN") === value;
}

function matchesMarket(item, value) {
  return value === "ALL" || (item.candidate_market || "UNKNOWN") === value;
}

function matchesData(item, value) {
  if (value === "ALL") return true;
  const flags = [...(item.data_quality_flags || []), ...(item.price_location_data_quality_flags || [])];
  if (value === "READY_DATA") return !item.diagnostic_only && item.latest_tick_ready !== false && item.support_ready && item.vwap_ready && !flags.some((flag) => /MISSING|STALE/.test(flag));
  if (value === "MISSING_SUPPORT") return !item.support_ready || flags.includes("MISSING_SUPPORT") || Boolean(item.support_ready_reason);
  if (value === "MISSING_VWAP") return !item.vwap_ready || flags.includes("MISSING_VWAP");
  if (value === "STALE_TICK") return item.latest_tick_ready === false || flags.some((flag) => /STALE/.test(flag));
  if (value === "DIAGNOSTIC_ONLY") return Boolean(item.diagnostic_only);
  return true;
}

function matchesOrder(item, value) {
  if (value === "ALL") return true;
  if (value === "ORDER_INTENT_CREATED") return Boolean(item.runtime_order_intent_created);
  if (value === "VIRTUAL_ORDER_CREATED") return Boolean(item.virtual_order_created);
  if (value === "LIVE_GUARD_PASSED") return Boolean(item.live_order_guard_passed);
  if (value === "LIVE_GUARD_REJECTED") return gateIsReadyLike(item) && !item.live_order_guard_passed;
  return true;
}

function gateIsReadyLike(item) {
  return ["READY", "READY_SMALL"].includes(item.gate_status);
}

function isMarketPending(item) {
  const display = item.display_status || "";
  return display.startsWith("WAIT_MARKET") || display.startsWith("WAIT_CANDIDATE_MARKET") || item.market_confirmation_pending || item.market_recovery_pending;
}

function isDataNotReady(item) {
  const display = item.display_status || "";
  const flags = [...(item.data_quality_flags || []), ...(item.price_location_data_quality_flags || [])];
  return display.startsWith("WAIT_DATA") || item.diagnostic_only || item.latest_tick_ready === false || flags.some((flag) => /MISSING|STALE/.test(flag));
}

function renderOrders(items) {
  const node = document.getElementById("order-candidates");
  if (!items.length) {
    node.innerHTML = `<div class="order-row muted">READY / READY_SMALL 주문 후보가 없습니다.</div>`;
    return;
  }
  node.innerHTML = items.map((item) => {
    const liveBlocked = item.gate_status === "READY" && !item.live_order_guard_passed;
    const small = item.gate_status === "READY_SMALL";
    return `
      <div class="order-row">
        <div class="row-top"><span class="row-title">${item.priority}. ${escapeHtml(item.stock_name || item.symbol)} ${item.code ? `[${escapeHtml(item.code)}]` : ""}</span>${badge(item.display_status || item.gate_status)}</div>
        <div class="row-meta">${escapeHtml(item.theme_name || "-")} · ${escapeHtml(item.stock_role || "-")} · ${item.position_size_multiplier}배</div>
        <div class="row-meta">진입 ${escapeHtml(item.entry_reference || "-")} · 손절 ${escapeHtml(item.stop_reference || "-")}</div>
        <div class="row-meta">${orderCandidateBadges(item, liveBlocked, small)}</div>
        <div class="row-meta">${shortId(item.candidate_instance_id)} · ${escapeHtml(item.reason || "-")}</div>
      </div>
    `;
  }).join("");
}

function orderCandidateBadges(item, liveBlocked, small) {
  return [
    item.live_order_enabled ? badge("LIVE 활성", "ready") : badge("LIVE 비활성", "observe"),
    item.live_order_guard_passed ? badge("LIVE Guard 통과", "ready") : badge(liveBlocked ? "READY지만 LIVE 주문 차단" : "LIVE Guard 미통과", "warning"),
    item.runtime_order_intent_created ? badge("주문 의도 생성", "ready") : badge("의도 없음", "observe"),
    item.virtual_order_created ? badge("가상 주문 생성", "ready-small") : "",
    item.diagnostic_only ? badge("진단 전용", "warning") : "",
    small ? badge("소액 관찰 진입 후보", "ready-small") : "",
  ].filter(Boolean).join(" ");
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
  setBadge("data-quality-status", status);
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
    const button = event.target.closest("button[data-filter-kind]");
    if (!button) return;
    const kind = button.dataset.filterKind;
    state.filters[kind] = button.dataset.filterValue;
    const group = button.closest("[data-filter-group]");
    group.querySelectorAll("button").forEach((node) => node.classList.toggle("active", node === button));
    renderWatchset((state.snapshot || {}).watchset || []);
  });
  document.getElementById("watchset-body").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-symbol]");
    if (!row || !state.snapshot) return;
    const item = (state.snapshot.watchset || []).find((candidate) => candidate.symbol === row.dataset.symbol);
    if (!item) return;
    state.selectedSymbol = item.symbol;
    renderGate(item);
    renderWatchset(state.snapshot.watchset || []);
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
setInterval(() => fetchSnapshot().catch(() => {}), 5000);
