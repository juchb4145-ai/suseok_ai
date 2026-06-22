const state = {
  ws: null,
  pollTimer: null,
  reconnectTimer: null,
  pollInFlight: false,
  wsConnected: false,
  lastSnapshotAt: 0,
  latestSnapshot: null,
  pollSeq: 0,
  pollController: null,
  dashboardV2Snapshot: {
    identity: null,
    rejectedSnapshotCount: 0,
    duplicateSnapshotCount: 0,
    lastRejectReason: "",
  },
  tables: {},
  buyZeroRca: {
    inFlight: false,
    lastFetchedAt: 0,
    readyRows: [],
    rallyRows: [],
    stageRows: [],
    stage: "",
    error: "",
  },
  shadowSmallEntryOps: {
    activationToken: "",
    activationTokenId: "",
  },
  shadowSmallEntryPilot: {
    report: null,
    inFlight: false,
    lastFetchedAt: 0,
  },
};

const SNAPSHOT_POLL_INTERVAL_MS = 30000;
const SNAPSHOT_INITIAL_FALLBACK_MS = 7000;
const SNAPSHOT_RECONNECT_MS = 3000;
const DASHBOARD_V2_SCHEMA_VERSION = "dashboard_v2.reboot_ops.v1";
const DASHBOARD_V2_NAMESPACE = "reboot_v2.main";
const SHADOW_SMALL_ENTRY_OPS_ENDPOINTS = {
  preflight: "/api/shadow-small-entry-ops/preflight",
  arm: "/api/shadow-small-entry-ops/arm",
  confirm: "/api/shadow-small-entry-ops/confirm",
  pause: "/api/shadow-small-entry-ops/pause",
  rollback: "/api/shadow-small-entry-ops/rollback",
  "emergency-pause": "/api/shadow-small-entry-ops/pause",
};
const SHADOW_SMALL_ENTRY_PILOT_ENDPOINTS = {
  start: "/api/shadow-small-entry-pilot/start",
  complete: "/api/shadow-small-entry-pilot/complete",
  "generate-report": "/api/shadow-small-entry-pilot/generate-report",
  report: "/api/shadow-small-entry-pilot/report",
};

const tableConfigs = {
  transportLatency: {
    endpoint: "/api/gateway/transport/latency",
    bodyId: "transportLatency-body",
    statusId: "transportLatency-status",
    paginationId: "transportLatency-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `전송 지연 샘플 ${item.sample_id || ""}`,
    detailEndpoint: (item) => item.sample_id ? `/api/gateway/transport/latency/${encodeURIComponent(item.sample_id)}` : "",
    actionLabel: "전송 리포트 재생성",
    actionEndpoint: (filters) => `/api/gateway/transport/latency/rebuild?persist=true&export=true${filters.trade_date ? `&trade_date=${encodeURIComponent(filters.trade_date)}` : ""}`,
    columns: [
      (item) => textCell(formatDateTime(item.created_at)),
      (item) => badge(item.transport_mode || "-"),
      (item) => textCell(item.direction || "-"),
      (item) => textCell(item.message_type || "-"),
      (item) => compactId(item.command_id || item.event_id || "-"),
      (item) => textCell(formatMs(item.total_wall_ms)),
      (item) => textCell(formatMs(item.long_poll_wait_ms)),
      (item) => textCell(formatMs(item.gateway_execute_ms)),
      (item) => textCell(formatMs(item.ack_round_trip_ms)),
      (item) => textCell(item.error || "-"),
    ],
  },
  transportExperiments: {
    endpoint: "/api/gateway/transport/experiments",
    bodyId: "transportExperiments-body",
    statusId: "transportExperiments-status",
    paginationId: "transportExperiments-pagination",
    defaultLimit: 25,
    detailTitle: (item) => `WebSocket Mock 실험 ${item.experiment_id || ""}`,
    detailEndpoint: (item) => item.experiment_id ? `/api/gateway/transport/experiments/${encodeURIComponent(item.experiment_id)}${item.scenario ? `?scenario=${encodeURIComponent(item.scenario)}` : ""}` : "",
    actionLabel: "비교 리포트 재생성",
    actionEndpoint: (filters) => {
      const params = buildQuery({ experiment_id: filters.experiment_id, scenario: filters.scenario, persist: true, export: true });
      return `/api/gateway/transport/experiments/rebuild?${params}`;
    },
    columns: [
      (item) => compactId(item.experiment_id || "-"),
      (item) => textCell(item.scenario || "-"),
      (item) => textCell(formatDateTime(item.started_at)),
      (item) => textCell(formatDateTime(item.ended_at)),
      (item) => textCell((item.sample_counts || {}).rest_long_poll ?? "-"),
      (item) => textCell((item.sample_counts || {}).websocket_mock ?? "-"),
      (item) => textCell(formatMs((item.rest_summary || {}).command_latency_p95_ms)),
      (item) => textCell(formatMs((item.websocket_summary || {}).command_latency_p95_ms)),
      (item) => textCell(formatMs((item.delta || {}).command_p95_delta_ms)),
      (item) => badge(item.latest_recommendation || "-"),
      (item) => textCell(item.real_gateway_switch_ready ? "예" : "아니오"),
    ],
  },
  dryRunOrders: {
    endpoint: "/api/runtime/orders/dry-run",
    bodyId: "dryRunOrders-body",
    statusId: "dryRunOrders-status",
    paginationId: "dryRunOrders-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `DRY_RUN 주문 의도 ${item.intent_id || ""}`,
    detailEndpoint: (item) => item.intent_id ? `/api/runtime/orders/dry-run/${encodeURIComponent(item.intent_id)}` : "",
    columns: [
      (item) => textCell(formatDateTime(item.created_at)),
      (item) => textCell(item.code || "-"),
      (item) => textCell(item.side || "-"),
      (item) => textCell(item.order_phase || "-"),
      (item) => textCell(item.quantity ?? 0),
      (item) => textCell(item.price ?? 0),
      (item) => badge(item.status || "-"),
      (item) => textCell(item.reason || "-"),
      (item) => item.live_would_pass ? badge("PASS", "ok") : badge(item.live_reject_reason || "REJECT", "warn"),
      (item) => textCell(item.candidate_id ?? "-"),
      (item) => textCell([item.virtual_order_id, item.virtual_position_id, item.exit_decision_id].filter(Boolean).join(" / ") || "-"),
    ],
  },
  intradayDecisions: {
    endpoint: "/api/runtime/decisions/intraday",
    bodyId: "intradayDecisions-body",
    statusId: "intradayDecisions-status",
    paginationId: "intradayDecisions-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `Intraday Decision ${item.decision_id || ""}`,
    columns: [
      (item) => textCell(formatDateTime(item.decision_at || item.created_at)),
      (item) => textCell(`${item.code || "-"} ${item.name || ""}`.trim()),
      (item) => textCell(item.theme_name || "-"),
      (item) => badge(item.gate_status || "-"),
      (item) => textCell(item.action_type || "-"),
      (item) => badge(item.action_result || "-"),
      (item) => textCell((item.reason_codes || []).slice(0, 3).join(", ") || item.gate_reason || "-"),
      (item) => textCell([item.gate_score, item.hybrid_score, item.theme_score].map((value) => value == null ? "-" : fmtNumber(value, 1)).join(" / ")),
      (item) => compactId(item.order_intent_id || item.virtual_order_id || item.virtual_position_id || item.exit_decision_id || item.candidate_instance_id || "-"),
    ],
  },
  intradayOutcomes: {
    endpoint: "/api/runtime/outcomes/intraday",
    bodyId: "intradayOutcomes-body",
    statusId: "intradayOutcomes-status",
    paginationId: "intradayOutcomes-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `Intraday Outcome ${item.outcome_id || ""}`,
    columns: [
      (item) => textCell(formatDateTime(item.evaluated_at || item.decision_at)),
      (item) => textCell(`${item.code || "-"} ${item.name || ""}`.trim()),
      (item) => badge(item.gate_status || "-"),
      (item) => textCell(item.action_type || "-"),
      (item) => textCell(`${item.horizon_sec || 0}s`),
      (item) => textCell(formatPercentValue(item.max_return_pct)),
      (item) => textCell(formatPercentValue(item.max_drawdown_pct)),
      (item) => textCell(formatPercentValue(item.current_return_pct)),
      (item) => badge(item.outcome_label || "-"),
      (item) => textCell((item.reason_codes || []).slice(0, 3).join(", ") || item.outcome_reason || "-"),
    ],
  },
  shadowEvaluations: {
    endpoint: "/api/runtime/shadow-strategies/evaluations",
    bodyId: "shadowEvaluations-body",
    statusId: "shadowEvaluations-status",
    paginationId: "shadowEvaluations-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `Shadow Evaluation ${item.evaluation_id || ""}`,
    columns: [
      (item) => textCell(`${item.code || "-"} ${item.name || ""}`.trim()),
      (item) => textCell(item.theme_name || "-"),
      (item) => textCell(item.policy_name || item.policy_id || "-"),
      (item) => badge(item.baseline_gate_status || "-"),
      (item) => badge(item.shadow_gate_status || "-"),
      (item) => badge(item.change_type || "-"),
      (item) => textCell((item.baseline_reason_codes || []).slice(0, 3).join(", ") || "-"),
      (item) => textCell((item.shadow_reason_codes || []).slice(0, 3).join(", ") || "-"),
      (item) => badge(item.outcome_label || "PENDING"),
      (item) => textCell(formatPercentValue(item.max_return_pct)),
      (item) => textCell(formatPercentValue(item.max_drawdown_pct)),
    ],
  },
  shadowRiskCandidates: {
    endpoint: "/api/runtime/shadow-strategies/evaluations",
    bodyId: "shadowRiskCandidates-body",
    statusId: "shadowRiskCandidates-status",
    paginationId: "shadowRiskCandidates-pagination",
    defaultLimit: 50,
    defaultFilters: { changed_decision: true },
    detailTitle: (item) => `Shadow Risk ${item.evaluation_id || ""}`,
    columns: [
      (item) => textCell(item.policy_name || item.policy_id || "-"),
      (item) => textCell(`${item.code || "-"} ${item.name || ""}`.trim()),
      (item) => badge(item.change_type || "-"),
      (item) => textCell(item.expected_risk || "-"),
      (item) => textCell(item.expected_effect || "-"),
      (item) => badge(item.outcome_label || "PENDING"),
      (item) => textCell(formatPercentValue(item.max_return_pct)),
      (item) => textCell(formatPercentValue(item.max_drawdown_pct)),
    ],
  },
  dryRunPerformance: {
    endpoint: "/api/runtime/performance/dry-run",
    bodyId: "dryRunPerformance-body",
    statusId: "dryRunPerformance-status",
    paginationId: "dryRunPerformance-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `성과 라이프사이클 ${item.lifecycle_id || ""}`,
    detailEndpoint: (item, filters) => item.lifecycle_id ? `/api/runtime/performance/dry-run/lifecycles/${encodeURIComponent(item.lifecycle_id)}${filters.trade_date ? `?trade_date=${encodeURIComponent(filters.trade_date)}` : ""}` : "",
    actionLabel: "성과 리포트 재생성",
    actionEndpoint: (filters) => `/api/runtime/performance/dry-run/rebuild?persist=true&export=true&format=all${filters.trade_date ? `&trade_date=${encodeURIComponent(filters.trade_date)}` : ""}`,
    columns: [
      (item) => textCell(item.code || "-"),
      (item) => textCell(item.strategy_name || "-"),
      (item) => textCell(item.theme_name || "-"),
      (item) => badge(item.quality_bucket || item.signal_classification || "-"),
      (item) => textCell(item.dry_run_false_positive_type || "-"),
      (item) => textCell(item.dry_run_false_negative_type || item.opportunity_loss_type || "-"),
      (item) => textCell(formatPercentValue(item.realized_return_pct)),
      (item) => textCell(formatPercentValue(item.net_return_pct)),
      (item) => textCell([
        item.limit_price_hit === true ? "limit hit" : item.limit_price_hit === false ? "limit miss" : "limit ?",
        item.partial_fill_risk || "fill ?",
        item.spread_risk || "spread ?",
      ].join(" / ")),
      (item) => textCell([item.hybrid_status || item.gate_status || "-", item.hybrid_position_tier || item.stock_role || ""].filter(Boolean).join(" / ")),
      (item) => textCell(formatPercentValue(item.max_return_20m)),
      (item) => textCell(formatPercentValue(item.max_drawdown_20m)),
      (item) => textCell(item.gate_reason || "-"),
    ],
  },
  falseSignals: {
    endpoint: "/api/runtime/performance/dry-run/false-signals",
    bodyId: "falseSignals-body",
    statusId: "falseSignals-status",
    paginationId: "falseSignals-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `오탐/미탐 신호 ${item.lifecycle_id || item.code || ""}`,
    detailEndpoint: (item, filters) => item.lifecycle_id ? `/api/runtime/performance/dry-run/lifecycles/${encodeURIComponent(item.lifecycle_id)}${filters.trade_date ? `?trade_date=${encodeURIComponent(filters.trade_date)}` : ""}` : "",
    columns: [
      (item) => textCell(item.code || "-"),
      (item) => badge(item.signal_classification || "-"),
      (item) => textCell(item.dry_run_false_positive_type || item.dry_run_false_negative_type || item.opportunity_loss_type || "-"),
      (item) => textCell(formatPercentValue(item.realized_return_pct)),
      (item) => textCell(formatPercentValue(item.max_return_20m)),
      (item) => textCell(formatPercentValue(item.max_drawdown_20m)),
      (item) => textCell(item.entry_live_reject_reason || item.entry_decision_safety_reason || "-"),
      (item) => textCell(item.gate_reason || "-"),
      (item) => compactId(item.lifecycle_id || "-"),
    ],
  },
  thresholdAB: {
    endpoint: "/api/runtime/threshold-ab/dry-run",
    bodyId: "thresholdAB-body",
    statusId: "thresholdAB-status",
    paginationId: "thresholdAB-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `기준 제안 후보 ${item.label_ko || item.candidate_id || ""}`,
    detailEndpoint: (item, filters) => item.candidate_id ? `/api/runtime/threshold-ab/dry-run/candidates/${encodeURIComponent(item.candidate_id)}${filters.trade_date ? `?trade_date=${encodeURIComponent(filters.trade_date)}` : ""}` : "",
    actionLabel: "A/B 제안 리포트 재생성",
    actionEndpoint: (filters) => {
      const params = buildQuery({
        trade_date: filters.trade_date,
        min_sample_count: filters.min_sample_count,
        persist: true,
        export: true,
        format: "all",
      });
      return `/api/runtime/threshold-ab/dry-run/rebuild?${params}`;
    },
    columns: [
      (item) => textCell(item.label_ko || item.candidate_id || "-"),
      (item) => textCell(categoryKo(item.category)),
      (item) => textCell(item.parameter_name || "-"),
      (item) => textCell(item.baseline_value ?? "-"),
      (item) => textCell(item.candidate_value ?? "-"),
      (item) => badge(gradeKo(((item.result || {}).recommendation || {}).grade || item.grade || item.recommendation_grade || "-")),
      (item) => textCell((((item.result || {}).recommendation || {}).sample_count ?? item.sample_count ?? 0)),
      (item) => textCell(item.expected_effect_ko || "-"),
      (item) => textCell(item.expected_risk_ko || "-"),
      (item) => textCell((((item.result || {}).delta || {}).avoided_false_positive_count ?? item.avoided_false_positive_count ?? 0)),
      (item) => textCell((((item.result || {}).delta || {}).newly_created_false_negative_count ?? item.newly_created_false_negative_count ?? 0)),
      (item) => textCell(formatRate((((item.result || {}).recommendation || {}).confidence ?? item.confidence))),
    ],
  },
  changeProposals: {
    endpoint: "/api/runtime/change-proposals",
    bodyId: "changeProposals-body",
    statusId: "changeProposals-status",
    paginationId: "changeProposals-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `Strategy Change Proposal ${item.proposal_id || ""}`,
    detailEndpoint: (item) => item.proposal_id ? `/api/runtime/change-proposals/${encodeURIComponent(item.proposal_id)}` : "",
    actionLabel: "Generate proposals",
    actionEndpoint: (filters) => {
      const tradeDate = filters.trade_date || new Date().toISOString().substring(0, 10);
      const params = buildQuery({ trade_date: tradeDate, source_type: filters.source_type || "combined", persist: true });
      return `/api/runtime/change-proposals/generate?${params}`;
    },
    columns: [
      (item) => textCell(formatDateTime(item.created_at)),
      (item) => badge(item.recommendation_grade || "-"),
      (item) => badge(item.status || "-"),
      (item) => textCell(item.category || "-"),
      (item) => textCell(item.target_component || "-"),
      (item) => textCell(item.title || "-"),
      (item) => textCell(formatRate(item.confidence)),
      (item) => textCell(fmtNumber(item.net_benefit_score, 2)),
      (item) => textCell(item.expected_effect_ko || "-"),
      (item) => textCell(item.expected_risk_ko || "-"),
      (item) => item.guardrail_passed ? badge("PASS", "ok") : badge(item.blocked_by_guardrail_reason || "BLOCKED", "warn"),
    ],
  },
  changeProposalEvidence: {
    endpoint: "/api/runtime/change-proposals/evidence",
    bodyId: "changeProposalEvidence-body",
    statusId: "changeProposalEvidence-status",
    paginationId: "changeProposalEvidence-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `Change Evidence ${item.evidence_id || ""}`,
    columns: [
      (item) => compactId(item.proposal_id || "-"),
      (item) => textCell(item.source_type || "-"),
      (item) => textCell(item.sample_count ?? 0),
      (item) => textCell(item.baseline_value || "-"),
      (item) => textCell(item.candidate_value || "-"),
      (item) => textCell(item.delta_value ?? "-"),
      (item) => textCell(formatRate(item.confidence)),
      (item) => textCell(item.metric_name || "-"),
    ],
  },
  strategyReplayRuns: {
    endpoint: "/api/runtime/replay/runs",
    bodyId: "strategyReplayRuns-body",
    statusId: "strategyReplayRuns-status",
    paginationId: "strategyReplayRuns-pagination",
    defaultLimit: 25,
    detailTitle: (item) => `Strategy Replay ${item.replay_id || ""}`,
    detailEndpoint: (item) => item.replay_id ? `/api/runtime/replay/runs/${encodeURIComponent(item.replay_id)}` : "",
    columns: [
      (item) => compactId(item.replay_id || "-"),
      (item) => textCell(item.trade_date || "-"),
      (item) => badge(item.mode || "-"),
      (item) => badge(item.status || "-"),
      (item) => textCell(item.processed_tick_count ?? 0),
      (item) => textCell(((item.metadata || {}).summary || {}).candidate_count ?? item.processed_candidate_event_count ?? 0),
      (item) => textCell(((item.metadata || {}).summary || {}).ready_count ?? "-"),
      (item) => textCell(((item.metadata || {}).summary || {}).order_intent_count ?? "-"),
      (item) => textCell(formatDateTime(item.started_at || item.created_at)),
    ],
  },
  gatewayCommands: {
    endpoint: "/api/gateway/commands/history",
    bodyId: "gatewayCommands-body",
    statusId: "gatewayCommands-status",
    paginationId: "gatewayCommands-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `Gateway 명령 ${item.command_id || ""}`,
    detailEndpoint: (item) => item.command_id ? `/api/gateway/commands/${encodeURIComponent(item.command_id)}` : "",
    columns: [
      (item) => compactId(item.command_id || "-"),
      (item) => textCell(formatDateTime(item.created_at)),
      (item) => textCell(item.command_type || "-"),
      (item) => badge(item.status || "-"),
      (item) => textCell(`${item.attempts || 0}/${item.max_attempts || 0}`),
      (item) => compactId(item.dedupe_key || item.idempotency_key || "-"),
      (item) => textCell(item.last_error || "-"),
    ],
  },
};

const gradeLabelsKo = {
  STRONG_CANDIDATE: "강한 후보",
  WATCH_CANDIDATE: "관찰 후보",
  RISKY_CANDIDATE: "위험 후보",
  DATA_INSUFFICIENT: "데이터 부족",
  DO_NOT_APPLY: "적용 비추천",
};

const categoryLabelsKo = {
  gate: "게이트",
  risk: "리스크",
  theme: "테마",
  session: "시간대",
  safety: "안전장치",
};

const TOKEN_STORAGE_KEY = "TRADING_CORE_TOKEN";
const BUY_ZERO_RCA_TABLE_REFRESH_MS = 30000;
const BUY_ZERO_RCA_CRITICAL_REASONS = new Set([
  "DATA_INSUFFICIENT",
  "LATE_CHASE_TEMP_WAIT",
  "CHASE_RISK",
  "WAIT_MARKET_CONFIRMATION_PENDING",
  "LIVE_SIM_BLOCKED",
  "ENTRY_PLAN_DIAGNOSTIC_ONLY",
  "ORDER_SINK_NOOP",
  "GATE_RESULT_KEY_MISMATCH",
  "CORE_BLOCKING",
  "ENTRY_BLOCKING",
  "WARMUP_OPTIONAL",
  "BACKFILL_ONLY_OBSERVE",
  "WAIT_DATA_EARLY_SMALL_CANDIDATE",
  "EARLY_SMALL_OBSERVE_ONLY",
]);

const BUY_ZERO_RCA_STAGE_LABELS = {
  CANDIDATE_GENERATED: "후보 생성",
  THEME_ENGINE_EVALUATED: "테마 엔진",
  THEMELAB_GATE_EVALUATED: "ThemeLab Gate",
  HYBRID_GATE_EVALUATED: "Hybrid Gate",
  RISK_GATE_EVALUATED: "Risk Gate",
  LIFECYCLE_UPDATED: "Lifecycle",
  ENTRY_PLAN_CREATED: "EntryPlan",
  VIRTUAL_ORDER_SUBMITTED: "가상 주문",
  DRY_RUN_INTENT_CREATED: "DRY_RUN 의도",
  LIVE_SIM_COMMAND_QUEUED: "LIVE_SIM 큐",
  BROKER_ORDER_ACCEPTED: "브로커 접수",
  PARTIAL_FILLED: "부분체결",
  FILLED: "체결",
};

function gradeKo(value) {
  return gradeLabelsKo[value] || value || "-";
}

function categoryKo(value) {
  return categoryLabelsKo[value] || value || "-";
}

function text(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value == null || value === "" ? "-" : String(value);
}

function cls(id, value) {
  const node = document.getElementById(id);
  if (node) node.className = value;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function textCell(value) {
  return escapeHtml(value == null || value === "" ? "-" : value);
}

function badge(value, tone = "") {
  const normalized = String(value || "-");
  let badgeTone = tone;
  if (!badgeTone) {
    if (/ACKED|ACCEPT|PASS|READY|OK|TRUE_POSITIVE|정상|통과|승인|강한/i.test(normalized)) badgeTone = "ok";
    else if (/FAIL|REJECT|EXPIRED|BLOCK|LOSS|FALSE|오류|실패|거부|차단|위험/i.test(normalized)) badgeTone = "bad";
    else if (/DUP|WAIT|PENDING|WARN|주의|대기|관찰/i.test(normalized)) badgeTone = "warn";
    else badgeTone = "muted";
  }
  return `<span class="badge ${badgeTone}" title="${escapeHtml(normalized)}">${escapeHtml(normalized)}</span>`;
}

function compactId(value) {
  const textValue = String(value || "-");
  if (textValue.length <= 24) return escapeHtml(textValue);
  return `<span class="mono" title="${escapeHtml(textValue)}">${escapeHtml(`${textValue.substring(0, 10)}...${textValue.substring(textValue.length - 8)}`)}</span>`;
}

function fmtNumber(value, digits = 1) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return "0";
  return number.toFixed(digits);
}

function formatRate(value) {
  if (value == null || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${(number * 100).toFixed(1)}%`;
}

function formatPercentValue(value) {
  if (value == null || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${number.toFixed(2)}%`;
}

function fmtOptionalNumber(value, digits = 1) {
  if (value == null || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number.toFixed(digits);
}

function formatBp(value, digits = 1) {
  const formatted = fmtOptionalNumber(value, digits);
  return formatted === "-" ? "-" : `${formatted}bp`;
}

function formatCurrencyShort(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return "0";
  if (number >= 100000000) return `${(number / 100000000).toFixed(1)}억`;
  if (number >= 10000) return `${Math.round(number / 10000)}만`;
  return String(Math.round(number));
}

function formatMs(value) {
  if (value == null || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${number.toFixed(1)}ms`;
}

function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value).replace("T", " ");
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date).reduce((acc, part) => {
    acc[part.type] = part.value;
    return acc;
  }, {});
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} KST`;
}

function yesNo(value) {
  return value ? "예" : "아니오";
}

function buildQuery(params) {
  const search = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    search.set(key, String(value));
  });
  return search.toString();
}

function reasonBadge(reason) {
  const textValue = String(reason || "-");
  const upper = textValue.toUpperCase();
  const critical = BUY_ZERO_RCA_CRITICAL_REASONS.has(upper) || /DATA_INSUFFICIENT|CHASE|LIVE_SIM_BLOCKED|DIAGNOSTIC_ONLY|ORDER_SINK_NOOP|GATE_RESULT_KEY_MISMATCH|WAIT_MARKET_CONFIRMATION/i.test(upper);
  return badge(textValue, critical ? "bad" : "");
}

function reasonBadges(reasons) {
  const values = [];
  for (const reason of reasons || []) {
    const textValue = String(reason || "").trim();
    if (!textValue || values.includes(textValue)) continue;
    values.push(textValue);
  }
  if (!values.length) return '<span class="empty">-</span>';
  return values.map((reason) => reasonBadge(reason)).join(" ");
}

function yesNoUnknown(value) {
  if (value === true) return "예";
  if (value === false) return "아니오";
  return "-";
}

function buyZeroTradeDate() {
  const payload = (state.latestSnapshot || {}).buy_zero_rca || ((state.latestSnapshot || {}).runtime || {}).buy_zero_rca || {};
  return payload.trade_date || new Date().toISOString().substring(0, 10);
}

function getStoredToken() {
  return localStorage.getItem(TOKEN_STORAGE_KEY) || "";
}

function rememberToken(token) {
  if (token) localStorage.setItem(TOKEN_STORAGE_KEY, token);
}

function forgetStoredToken() {
  localStorage.removeItem(TOKEN_STORAGE_KEY);
}

function promptForToken(message = "TRADING_CORE_TOKEN") {
  const token = window.prompt(message) || "";
  rememberToken(token);
  return token;
}

function isInvalidTokenResponse(response, payload) {
  const detail = String((payload || {}).detail || (payload || {}).error || "");
  return response.status === 401 || response.status === 403 || /invalid local gateway token/i.test(detail);
}

async function parseResponsePayload(response) {
  try {
    return await response.json();
  } catch (_) {
    try {
      return { detail: await response.text() };
    } catch (__) {
      return {};
    }
  }
}

async function postWithLocalToken(endpoint, token, body = null) {
  const headers = { "X-Local-Token": token };
  if (body != null) headers["Content-Type"] = "application/json";
  const response = await fetch(endpoint, {
    method: "POST",
    headers,
    body: body == null ? undefined : JSON.stringify(body),
  });
  const payload = await parseResponsePayload(response);
  return { response, payload };
}

async function runWithLocalTokenRetry(requestFn) {
  let token = getStoredToken() || promptForToken();
  if (!token) return null;

  let result = await requestFn(token);
  if (!result.response.ok && isInvalidTokenResponse(result.response, result.payload)) {
    forgetStoredToken();
    token = promptForToken("TRADING_CORE_TOKEN이 맞지 않습니다. 새 토큰을 입력하세요.");
    if (!token) return null;
    result = await requestFn(token);
  }

  if (!result.response.ok) {
    if (isInvalidTokenResponse(result.response, result.payload)) forgetStoredToken();
    throw new Error(result.payload.detail || result.payload.error || `${result.response.status} ${result.response.statusText}`);
  }

  return result.payload;
}

async function apiGet(endpoint, params = {}, tableKey = "") {
  const query = buildQuery(params);
  const url = query ? `${endpoint}?${query}` : endpoint;
  const table = tableKey ? state.tables[tableKey] : null;
  if (table?.abortController) table.abortController.abort();
  const controller = new AbortController();
  if (table) table.abortController = controller;
  const response = await fetch(url, { signal: controller.signal });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function tableFilters(tableKey) {
  const filters = {};
  document.querySelectorAll(`[data-table="${tableKey}"][data-filter]`).forEach((node) => {
    if (node.value !== "") filters[node.dataset.filter] = node.value;
  });
  return filters;
}

function updateTableState(tableKey, patch) {
  state.tables[tableKey] = { ...state.tables[tableKey], ...patch };
}

function setTableStatus(tableKey, status, tone = "muted") {
  const config = tableConfigs[tableKey];
  const node = document.getElementById(config.statusId);
  if (node) {
    node.textContent = status;
    node.className = `counter ${tone}`;
  }
}

function renderToolbar(tableKey) {
  const config = tableConfigs[tableKey];
  const toolbar = document.querySelector(`[data-table-toolbar="${tableKey}"]`);
  if (!toolbar) return;
  const selectedLimit = Number(config.defaultLimit || 50);
  const option = (value) => `<option value="${value}" ${selectedLimit === value ? "selected" : ""}>${value}</option>`;
  toolbar.innerHTML = `
    <button type="button" data-table-action="${tableKey}:apply">필터 적용</button>
    <button type="button" data-table-action="${tableKey}:reset">초기화</button>
    <button type="button" data-table-action="${tableKey}:reload">새로고침</button>
    <label class="inline-control">페이지 크기
      <select data-table-action="${tableKey}:limit">
        ${option(25)}${option(50)}${option(100)}${option(200)}
      </select>
    </label>
    <label class="inline-control inline-check"><input type="checkbox" data-table-action="${tableKey}:auto" /> 자동 새로고침</label>
    ${config.actionEndpoint ? `<button type="button" data-table-action="${tableKey}:protected">${escapeHtml(config.actionLabel || "보호 작업")}</button>` : ""}
    <span id="${tableKey}-freshness" class="freshness">아직 조회 전</span>
  `;
}

function bindTableControls(tableKey) {
  document.querySelectorAll(`[data-table-action^="${tableKey}:"]`).forEach((node) => {
    const action = node.dataset.tableAction.split(":")[1];
    if (action === "limit") {
      node.addEventListener("change", () => {
        updateTableState(tableKey, { limit: Number(node.value), offset: 0 });
        fetchTable(tableKey).catch((error) => setTableStatus(tableKey, error.message, "bad"));
      });
      return;
    }
    if (action === "auto") {
      node.addEventListener("change", () => toggleAutoRefresh(tableKey, node.checked));
      return;
    }
    node.addEventListener("click", () => handleTableAction(tableKey, action, node).catch((error) => setTableStatus(tableKey, error.message, "bad")));
  });
}

async function handleTableAction(tableKey, action, node) {
  if (action === "apply") {
    updateTableState(tableKey, { offset: 0 });
    await fetchTable(tableKey);
  } else if (action === "reset") {
    document.querySelectorAll(`[data-table="${tableKey}"][data-filter]`).forEach((filterNode) => {
      if (filterNode.tagName === "SELECT") filterNode.selectedIndex = 0;
      else filterNode.value = "";
    });
    updateTableState(tableKey, { offset: 0 });
    await fetchTable(tableKey);
  } else if (action === "reload") {
    await fetchTable(tableKey);
  } else if (action === "protected") {
    await runProtectedTableAction(tableKey, node);
  }
}

async function runProtectedTableAction(tableKey, button) {
  const config = tableConfigs[tableKey];
  if (!config.actionEndpoint) return;
  const originalText = button?.textContent || "";
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "실행 중";
  }
  setTableStatus(tableKey, "실행 중", "warn");
  try {
    const filters = tableFilters(tableKey);
    const payload = await runWithLocalTokenRetry((token) => postWithLocalToken(config.actionEndpoint(filters), token));
    if (!payload) {
      setTableStatus(tableKey, "취소", "muted");
      return;
    }
    openDetailPanel(`${config.actionLabel || "작업"} 결과`, payload);
    await fetchTable(tableKey);
  } catch (error) {
    openDetailPanel(`${config.actionLabel || "작업"} 오류`, { error: error.message });
    setTableStatus(tableKey, "오류", "bad");
  } finally {
    if (button) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = originalText;
    }
  }
}

function toggleAutoRefresh(tableKey, enabled) {
  const table = state.tables[tableKey];
  if (table?.autoTimer) clearInterval(table.autoTimer);
  if (!enabled) {
    updateTableState(tableKey, { autoTimer: null });
    return;
  }
  const timer = setInterval(() => fetchTable(tableKey).catch(() => {}), 15000);
  updateTableState(tableKey, { autoTimer: timer });
}

async function fetchTable(tableKey) {
  const config = tableConfigs[tableKey];
  const table = state.tables[tableKey];
  const seq = (table.requestSeq || 0) + 1;
  updateTableState(tableKey, { requestSeq: seq, loading: true, error: "" });
  setTableStatus(tableKey, "불러오는 중", "warn");
  const filters = tableFilters(tableKey);
  const params = { ...(config.defaultFilters || {}), ...filters, limit: table.limit, offset: table.offset };
  try {
    const payload = await apiGet(config.endpoint, params, tableKey);
    if (state.tables[tableKey].requestSeq !== seq) return;
    const items = normalizeItems(payload, tableKey);
    const pagination = payload.pagination || { limit: table.limit, offset: table.offset, count: items.length, has_next: items.length >= table.limit, has_prev: table.offset > 0 };
    updateTableState(tableKey, {
      loading: false,
      payload,
      items,
      pagination,
      lastFetchedAt: new Date(),
    });
    renderTable(tableKey);
  } catch (error) {
    if (error.name === "AbortError") return;
    updateTableState(tableKey, { loading: false, error: error.message });
    renderTableError(tableKey, error.message);
  }
}

function normalizeItems(payload, tableKey = "") {
  if (tableKey === "thresholdAB" && Array.isArray(payload.candidates)) {
    const results = payload.results || {};
    return payload.candidates.map((item) => ({ ...item, result: results[String(item.candidate_id || "")] || item.result || {} }));
  }
  return payload.items || payload.samples || payload.candidates || [];
}

function renderTable(tableKey) {
  const config = tableConfigs[tableKey];
  const table = state.tables[tableKey];
  const body = document.getElementById(config.bodyId);
  if (!body) return;
  const items = table.items || [];
  if (!items.length) {
    body.innerHTML = `<tr><td class="empty" colspan="${config.columns.length}">표시할 데이터가 없습니다</td></tr>`;
  } else {
    body.innerHTML = items.map((item, index) => {
      const cells = config.columns.map((renderer) => `<td>${renderer(item)}</td>`).join("");
      return `<tr class="clickable-row" data-table-row="${tableKey}" data-row-index="${index}">${cells}</tr>`;
    }).join("");
    body.querySelectorAll("[data-table-row]").forEach((row) => {
      row.addEventListener("click", () => openRowDetail(tableKey, Number(row.dataset.rowIndex)).catch(() => {}));
    });
  }
  renderPagination(tableKey);
  updateFreshness(tableKey);
}

function updateFreshness(tableKey) {
  const table = state.tables[tableKey];
  const items = table.items || [];
  const fetched = table.lastFetchedAt ? table.lastFetchedAt.toLocaleTimeString() : "아직 조회 전";
  const stale = table.lastFetchedAt && Date.now() - table.lastFetchedAt.getTime() > 30000;
  const freshness = document.getElementById(`${tableKey}-freshness`);
  if (freshness) {
    freshness.textContent = `${stale ? "오래된 데이터 - " : ""}조회 ${fetched}`;
    freshness.className = `freshness ${stale ? "stale" : ""}`;
  }
  setTableStatus(tableKey, `${(table.pagination || {}).count ?? items.length}건`, stale ? "warn" : "ok");
}

function renderTableError(tableKey, message) {
  const config = tableConfigs[tableKey];
  const body = document.getElementById(config.bodyId);
  if (body) body.innerHTML = `<tr><td class="error-row" colspan="${config.columns.length}">${escapeHtml(message)}</td></tr>`;
  setTableStatus(tableKey, "오류", "bad");
}

function renderPagination(tableKey) {
  const table = state.tables[tableKey];
  const page = table.pagination || {};
  const node = document.getElementById(tableConfigs[tableKey].paginationId);
  if (!node) return;
  const currentPage = Math.floor((page.offset || 0) / Math.max(1, page.limit || table.limit)) + 1;
  const totalText = page.total != null ? ` / ${page.total}` : "";
  node.innerHTML = `
    <button type="button" ${page.has_prev ? "" : "disabled"} data-page="${tableKey}:prev">이전</button>
    <span>${currentPage}페이지 (${page.count || 0}${totalText})</span>
    <button type="button" ${page.has_next ? "" : "disabled"} data-page="${tableKey}:next">다음</button>
  `;
  node.querySelectorAll("[data-page]").forEach((button) => {
    button.addEventListener("click", () => {
      const direction = button.dataset.page.split(":")[1];
      const nextOffset = direction === "next" ? page.next_offset : page.prev_offset;
      updateTableState(tableKey, { offset: Math.max(0, Number(nextOffset || 0)) });
      fetchTable(tableKey).catch((error) => setTableStatus(tableKey, error.message, "bad"));
    });
  });
}

async function openRowDetail(tableKey, index) {
  const config = tableConfigs[tableKey];
  const item = (state.tables[tableKey].items || [])[index];
  if (!item) return;
  const filters = tableFilters(tableKey);
  const endpoint = config.detailEndpoint ? config.detailEndpoint(item, filters) : "";
  if (!endpoint) {
    openDetailPanel(config.detailTitle(item), item);
    return;
  }
  setTableStatus(tableKey, "상세 조회", "warn");
  try {
    const payload = await apiGet(endpoint);
    openDetailPanel(config.detailTitle(item), payload);
  } catch (error) {
    openDetailPanel(config.detailTitle(item), { error: error.message, item });
  } finally {
    renderTable(tableKey);
  }
}

function openDetailPanel(title, payload) {
  text("detail-title", title);
  const summary = document.getElementById("detail-summary");
  const raw = document.getElementById("detail-json");
  if (summary) {
    summary.innerHTML = detailSummaryHtml(payload) + detailActionHtml(payload);
    bindDetailActions(payload);
  }
  if (raw) raw.textContent = JSON.stringify(payload, null, 2);
  document.getElementById("detail-drawer")?.classList.add("open");
  document.getElementById("detail-drawer")?.setAttribute("aria-hidden", "false");
  document.getElementById("detail-backdrop")?.classList.remove("hidden");
}

function detailActionHtml(payload) {
  const proposal = actionProposal(payload);
  if (!proposal) return "";
  return `
    <div class="detail-actions">
      <p class="help-text">자동 적용 아님: 승인 상태만 저장하고 runtime config는 변경하지 않습니다.</p>
      <button type="button" data-proposal-action="approve-observe">Approve Observe</button>
      <button type="button" data-proposal-action="approve-dry-run">Approve DRY_RUN</button>
      <button type="button" data-proposal-action="reject">Reject</button>
      <button type="button" data-proposal-action="expire">Expire</button>
      <button type="button" data-proposal-action="note">Note</button>
    </div>
  `;
}

function bindDetailActions(payload) {
  const proposal = actionProposal(payload);
  if (!proposal) return;
  document.querySelectorAll("[data-proposal-action]").forEach((button) => {
    button.addEventListener("click", () => runProposalAction(proposal.proposal_id, button.dataset.proposalAction).catch((error) => {
      openDetailPanel("Strategy Change Proposal action error", { proposal_id: proposal.proposal_id, error: error.message });
    }));
  });
}

function actionProposal(payload) {
  const proposal = (payload || {}).proposal || {};
  return proposal.proposal_id ? proposal : null;
}

async function runProposalAction(proposalId, action) {
  const endpointMap = {
    "approve-observe": "approve-observe",
    "approve-dry-run": "approve-dry-run",
    reject: "reject",
    expire: "expire",
    note: "note",
  };
  const endpointAction = endpointMap[action];
  if (!endpointAction) return;
  let note = "";
  if (action === "reject" || action === "note") {
    note = window.prompt("note") || "";
    if (!note) return;
  } else if (action === "expire") {
    note = window.prompt("note (optional)") || "";
  }
  const body = { operator: "dashboard", note };
  const payload = await runWithLocalTokenRetry((token) => postWithLocalToken(`/api/runtime/change-proposals/${encodeURIComponent(proposalId)}/${endpointAction}`, token, body));
  if (!payload) return;
  openDetailPanel("Strategy Change Proposal action result", payload);
  fetchTable("changeProposals").catch(() => {});
  fetchTable("changeProposalEvidence").catch(() => {});
  pollSnapshot().catch(() => {});
}

function closeDetailPanel() {
  document.getElementById("detail-drawer")?.classList.remove("open");
  document.getElementById("detail-drawer")?.setAttribute("aria-hidden", "true");
  document.getElementById("detail-backdrop")?.classList.add("hidden");
}

function detailSummaryHtml(payload) {
  if ((payload || {}).detail_type === "buy_zero_trace") return buyZeroTraceDetailHtml(payload);
  const record = payload.record || payload.item || payload.report || payload.candidate || payload;
  const keys = ["decision_id", "command_id", "intent_id", "candidate_id", "report_id", "sample_id", "experiment_id", "lifecycle_id", "status", "action_type", "action_result", "command_type", "code", "reason", "error"];
  return keys
    .filter((key) => record && record[key] != null && record[key] !== "")
    .map((key) => `<div><span>${escapeHtml(key)}</span><strong>${escapeHtml(summaryValue(record[key]))}</strong></div>`)
    .join("") || '<span class="empty">요약할 핵심 필드가 없습니다</span>';
}

function summaryValue(value) {
  if (value == null) return "-";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function renderRows(id, rows, emptyColumns) {
  const body = document.getElementById(id);
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = `<tr><td class="empty" colspan="${emptyColumns}">표시할 데이터가 없습니다</td></tr>`;
    return;
  }
  body.innerHTML = rows.join("");
}

function rowHtml(cells) {
  return `<tr>${cells.map((cell) => `<td>${escapeHtml(cell)}</td>`).join("")}</tr>`;
}

function firstItems(rows, limit) {
  const result = [];
  for (const item of rows || []) {
    if (result.length >= limit) break;
    result.push(item);
  }
  return result;
}

function renderInlineCounts(id, rows, key, emptyText) {
  const node = document.getElementById(id);
  if (!node) return;
  const lines = (rows || []).map((item) => `${item[key] || "-"}: ${item.count || 0}`);
  node.innerHTML = lines.length ? lines.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : `<span class="empty">${escapeHtml(emptyText)}</span>`;
}

function renderThresholdRecommendations(id, rows) {
  const node = document.getElementById(id);
  if (!node) return;
  const lines = firstItems(rows || [], 5).map((item) => {
    const delta = item.delta || {};
    return `${item.label_ko || item.candidate_id || "-"} - ${gradeKo(item.grade)} - FP -${delta.avoided_false_positive_count || 0} / FN +${delta.newly_created_false_negative_count || 0}`;
  });
  node.innerHTML = lines.length ? lines.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">아직 추천 후보가 없습니다. DRY_RUN 표본이 더 쌓이면 자동으로 표시됩니다.</span>';
}

function renderShadowPolicyRanking(id, rows) {
  const node = document.getElementById(id);
  if (!node) return;
  const lines = firstItems(rows || [], 8).map((item) => (
    `${item.policy_name || item.policy_id || "-"} - ${item.recommendation_grade || "-"} - score ${fmtNumber(item.estimated_net_benefit_score, 1)} / changed ${item.changed_decision_count || 0} / ready ${item.ready_delta || 0}`
  ));
  node.innerHTML = lines.length ? lines.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">Shadow policy ranking이 없습니다</span>';
}

function renderOpsAlerts(payload) {
  const ops = payload || { summary: {}, alerts: [] };
  const summary = ops.summary || {};
  const severity = summary.highest_severity || "ok";
  const severityLabel = {
    critical: "긴급 확인",
    warning: "주의 필요",
    info: "참고",
    ok: "정상",
  }[severity] || "점검 대기";
  const severityTone = severity === "critical" ? "bad" : severity === "warning" ? "warn" : severity === "info" ? "muted" : "ok";

  text("ops-alert-severity", severityLabel);
  cls("ops-alert-severity", `counter ${severityTone}`);
  text("ops-alert-critical", summary.critical || 0);
  text("ops-alert-warning", summary.warning || 0);
  text("ops-alert-info", summary.info || 0);
  text("ops-safe-collect", summary.safe_to_collect_data ? "가능" : "확인 필요");
  text("ops-safe-ws-pilot", summary.safe_to_run_ws_pilot ? "가능" : "확인 필요");
  text("ops-safe-live", summary.safe_to_live_order ? "가능" : "차단");

  const node = document.getElementById("ops-alert-lines");
  if (!node) return;
  const alerts = ops.alerts || [];
  if (!alerts.length) {
    node.innerHTML = '<div class="alert-item ok"><strong>정상</strong><span>현재 긴급 운영 알림이 없습니다. OBSERVE/DRY_RUN 수집을 계속해도 됩니다.</span></div>';
    return;
  }
  node.innerHTML = firstItems(alerts, 8)
    .map((alert) => {
      const tone = alert.severity === "critical" ? "bad" : alert.severity === "warning" ? "warn" : "info";
      return `
        <div class="alert-item ${tone}">
          <strong>${escapeHtml(alert.title || alert.id || "-")}</strong>
          <span>${escapeHtml(alert.message || "")}</span>
          ${alert.action ? `<em>${escapeHtml(alert.action)}</em>` : ""}
        </div>
      `;
    })
    .join("");
}

function renderDashboardV2(snapshot) {
  const payload = normalizeSnapshotEnvelope(snapshot).snapshot || {};
  const root = document.getElementById("dashboard-v2-root");
  if (!root) return;
  if (!payload.schema_version) {
    root.hidden = true;
    return;
  }
  root.hidden = false;
  const status = payload.v2_status || {};
  const market = payload.market_overview || {};
  const themes = payload.leading_themes || { items: [] };
  const entry = payload.entry_candidates || { items: [] };
  const positionRisk = payload.position_risk || { positions: [] };
  const orderManager = payload.order_manager || {};
  const marketRsShadow = payload.market_relative_strength_shadow || {};
  const preMarket = payload.pre_market_check || {};
  const reasons = payload.wait_block_reasons || { items: [] };
  const health = payload.system_health || {};
  const readModel = payload.read_model || {};

  const readModelSuffix = readModel.source
    ? ` · ${readModel.source}${readModel.snapshot_age_sec !== undefined ? ` ${Math.round(Number(readModel.snapshot_age_sec || 0))}s` : ""}${readModel.stale ? " STALE" : ""}`
    : "";
  text("dashboard-v2-message", `${status.operator_message_ko || "Reboot V2 운영 상태를 요약합니다."}${readModelSuffix}`);
  text("dashboard-v2-status-label", status.status_label || "관찰전용");
  cls("dashboard-v2-status-label", `counter ${statusTone(status.status_label)}`);
  renderDashboardV2Banners(payload.safety_banners || []);

  const identity = state.dashboardV2Snapshot.identity || snapshotIdentity(payload);
  text("dashboard-v2-snapshot-source", `${identity.sourceKind || readModel.source || "-"}${identity.fallbackUsed ? " fallback" : ""}`);
  text("dashboard-v2-snapshot-generation", identity.generation ? `#${identity.generation}` : "-");
  text("dashboard-v2-market-status", `${market.composite_market_mode_label_ko || market.composite_market_mode || market.global_status || "-"} / ${market.kospi_status || "-"} / ${market.kosdaq_status || "-"}`);
  text("dashboard-v2-pre-market-status", preMarket.go_no_go || "-");
  text("dashboard-v2-order-safety", orderManager.stop_new_buy ? "STOP_NEW_BUY" : orderManager.reduce_only ? "REDUCE_ONLY" : orderManager.reconcile_required_count ? "RECONCILE_REQUIRED" : orderManager.live_sim_orders_allowed ? "LIVE_SIM 허용" : "관찰/차단");
  text("dashboard-v2-broker-account", `${status.broker_env || "UNKNOWN"} / ${status.account || "-"}`);
  text("dashboard-v2-kill-switch", status.kill_switch_state || "NORMAL");
  text("dashboard-v2-data-freshness", `${status.data_freshness_status || "-"}${readModel.stale ? " / STALE" : ""}`);
  text("dashboard-v2-runtime-cycle", formatDateTime(status.last_runtime_cycle_at));

  text("dashboard-v2-pre-market-pill", preMarket.go_no_go || "UNKNOWN");
  cls("dashboard-v2-pre-market-pill", `counter ${preMarketTone(preMarket.go_no_go)}`);
  text("dashboard-v2-pre-market-message", preMarket.operator_message_ko || "장전 Go/No-Go 결과 대기");
  text("dashboard-v2-pre-market-mode", preMarket.requested_mode || "OBSERVE");
  text("dashboard-v2-pre-market-broker", preMarket.broker_env || status.broker_env || "UNKNOWN");
  text("dashboard-v2-pre-market-sqlite", ((preMarket.sqlite_health || {}).status || (preMarket.sqlite_health || {}).message_ko || "-"));
  text("dashboard-v2-pre-market-fail-count", preMarket.fail_count || 0);
  text("dashboard-v2-pre-market-action", preMarket.recommended_action_ko || "점검 결과를 기다립니다.");

  text("dashboard-v2-market-pill", market.systemic_risk_off ? "SYSTEMIC_RISK_OFF" : market.composite_market_mode || market.global_status || "UNKNOWN");
  cls("dashboard-v2-market-pill", `counter ${market.systemic_risk_off ? "bad" : market.risk_off_detected || market.weak_market_detected ? "warn" : "ok"}`);
  text("dashboard-v2-market-message", market.market_operator_message_ko || "시장 데이터 대기");
  text("dashboard-v2-kospi", `${formatPercentValue(market.kospi_return_pct)} / ${formatPercentValue(Number(market.kospi_breadth_pct || 0) * 100)}`);
  text("dashboard-v2-kosdaq", `${formatPercentValue(market.kosdaq_return_pct)} / ${formatPercentValue(Number(market.kosdaq_breadth_pct || 0) * 100)}`);
  text("dashboard-v2-market-blocks", market.block_new_entry_count || 0);
  text("dashboard-v2-market-waits", market.wait_market_count || 0);
  renderDashboardV2MarketRsShadow(marketRsShadow);

  text("dashboard-v2-order-pill", orderManager.mode || "OBSERVE");
  cls("dashboard-v2-order-pill", `counter ${orderManager.real_broker_blocked ? "bad" : orderManager.live_sim_orders_allowed ? "ok" : "muted"}`);
  text("dashboard-v2-live-sim", orderManager.live_sim_orders_allowed ? "ON" : "OFF");
  text("dashboard-v2-open-orders", orderManager.open_order_count || 0);
  text("dashboard-v2-rejected-orders", orderManager.rejected_order_count || 0);
  text("dashboard-v2-pending-cancel", orderManager.pending_cancel_count || 0);
  text("dashboard-v2-last-reject", orderManager.last_reject_reason ? `최근 거부: ${orderManager.last_reject_reason}` : "최근 주문 거부 없음");

  text("dashboard-v2-risk-pill", positionRisk.portfolio_risk_level || "NORMAL");
  cls("dashboard-v2-risk-pill", `counter ${/KILL|REDUCE|STOP|RISK/i.test(positionRisk.portfolio_risk_level || "") ? "bad" : positionRisk.stop_new_entry_recommended ? "warn" : "ok"}`);
  text("dashboard-v2-open-positions", positionRisk.open_position_count || 0);
  text("dashboard-v2-unrealized", formatPercentValue(positionRisk.unrealized_pnl_pct));
  text("dashboard-v2-exit-now", positionRisk.exit_now_count || 0);
  text("dashboard-v2-scale-out", positionRisk.scale_out_count || 0);
  text("dashboard-v2-risk-message", positionRisk.kill_switch_recommended ? "킬스위치 권고: 신규 매수 차단 확인" : positionRisk.stop_new_entry_recommended ? "신규진입 중지 권고" : "보유 리스크 관찰");
  renderDashboardV2MarketSideBudgets(positionRisk.market_side_budgets || {});

  text("dashboard-v2-theme-count", themes.top5_count || (themes.items || []).length || 0);
  renderDashboardV2Themes(themes.items || []);
  text("dashboard-v2-reason-count", (reasons.items || []).length || 0);
  renderDashboardV2Reasons(reasons.items || []);
  text("dashboard-v2-entry-count", (entry.items || []).length || 0);
  renderDashboardV2EntryRows(entry.items || []);
  text("dashboard-v2-position-count", (positionRisk.positions || []).length || 0);
  renderDashboardV2PositionRows(positionRisk.positions || []);

  text("dashboard-v2-health-gateway", (health.gateway_heartbeat || {}).ok ? "정상" : "점검");
  text("dashboard-v2-health-queue", health.command_queue_depth || 0);
  text("dashboard-v2-health-transport", health.transport_status || "-");
  text("dashboard-v2-health-exception", health.last_exception || "-");
}

function renderDashboardV2Banners(rows) {
  const node = document.getElementById("dashboard-v2-banners");
  if (!node) return;
  if (!rows.length) {
    node.innerHTML = '<div class="alert-item ok"><strong>정상</strong><span>주요 안전 경고가 없습니다.</span></div>';
    return;
  }
  node.innerHTML = rows.map((item) => `
    <div class="alert-item ${item.severity === "critical" ? "bad" : item.severity === "warning" ? "warn" : "ok"}">
      <strong>${escapeHtml(item.message_ko || item.reason_code || "-")}</strong>
      <span>${escapeHtml(item.reason_code || "")}</span>
    </div>
  `).join("");
}

function renderDashboardV2MarketRsShadow(payload) {
  const data = payload || {};
  const status = data.status || "NO_DATA";
  text("dashboard-v2-market-rs-shadow-pill", status);
  cls("dashboard-v2-market-rs-shadow-pill", `counter ${status === "OK" || status === "READY" ? "ok" : status === "DISABLED" ? "muted" : "warn"}`);
  text("dashboard-v2-market-rs-shadow-message", data.operator_message_ko || "분석/관측전용 shadow 검증입니다.");
  text("dashboard-v2-market-rs-shadow-total", data.shadow_candidate_count || 0);
  text("dashboard-v2-market-rs-shadow-healthy", data.healthy_side_reduced_count || 0);
  text("dashboard-v2-market-rs-shadow-weak", data.weak_side_shadow_candidate_count || 0);
  text("dashboard-v2-market-rs-shadow-riskoff", data.risk_off_side_diagnostic_count || 0);
  text("dashboard-v2-market-rs-shadow-systemic", data.systemic_excluded_count || 0);
  text("dashboard-v2-market-rs-shadow-labeled", data.labeled_count || 0);
  text("dashboard-v2-market-rs-shadow-tracked", data.tracked_event_count || 0);
  text("dashboard-v2-market-rs-shadow-pending", data.matured_pending_count || 0);
  text("dashboard-v2-market-rs-shadow-persisted", data.persisted_outcome_count || 0);
  text("dashboard-v2-market-rs-shadow-mfe10", formatPercentValue(data.avg_mfe_10m));
  text("dashboard-v2-market-rs-shadow-mae10", formatPercentValue(data.avg_mae_10m));
  text("dashboard-v2-market-rs-shadow-edge", formatRate(data.shadow_edge_rate_10m));
  text("dashboard-v2-market-rs-shadow-risk", formatRate(data.shadow_risk_case_rate_10m));
  text("dashboard-v2-market-rs-shadow-recommendation", data.current_recommendation || "NO_DATA");
  text("dashboard-v2-market-rs-shadow-mode", data.actual_order_mode_label || "분석/관측전용");
  text("dashboard-v2-market-rs-shadow-order", data.actual_order_mode_label || "분석/관측전용");
  renderDashboardV2MarketRsShadowRecent(data.recent_candidates || []);
}

function renderDashboardV2MarketRsShadowRecent(rows) {
  const node = document.getElementById("dashboard-v2-market-rs-shadow-recent");
  if (!node) return;
  if (!rows.length) {
    node.innerHTML = '<span class="empty">관측 후보 데이터가 없습니다</span>';
    return;
  }
  node.innerHTML = rows.slice(0, 5).map((item) => {
    const riskOffNote = item.shadow_scenario === "RISK_OFF_SIDE_DIAGNOSTIC" ? " · no promotion" : "";
    return `
      <div class="dashboard-v2-list-item">
        <div><strong>${escapeHtml(`${item.code || "-"} ${item.name || ""}`.trim())}</strong>${badge(item.actual_order_mode_label || "분석/관측전용", "observe")}</div>
        <p>${escapeHtml(item.shadow_scenario || "-")} · ${escapeHtml(item.market_side || "-")} ${escapeHtml(item.side_market_regime || "")} · RS ${formatPercentValue(item.relative_strength_vs_index_pct)}${riskOffNote}</p>
        <p>${escapeHtml(item.actual_market_action || "-")} / ${escapeHtml(item.actual_entry_status || "-")} → ${escapeHtml(item.counterfactual_action || "-")}</p>
      </div>
    `;
  }).join("");
}

function renderDashboardV2Themes(rows) {
  const node = document.getElementById("dashboard-v2-themes");
  if (!node) return;
  if (!rows.length) {
    node.innerHTML = '<span class="empty">주도테마 후보 데이터가 없습니다</span>';
    return;
  }
  node.innerHTML = rows.slice(0, 5).map((item) => `
    <div class="dashboard-v2-list-item">
      <div><strong>${escapeHtml(item.rank || "-")}. ${escapeHtml(item.theme_name || "-")}</strong><span>${escapeHtml(item.leader_name || item.leader_symbol || "-")}</span></div>
      <div>${badge(item.theme_status_label || item.theme_status || "관찰")}<span>${escapeHtml(fmtOptionalNumber(item.theme_score, 1))}</span></div>
      <p>${escapeHtml(item.reason_summary_ko || "-")}</p>
    </div>
  `).join("");
}

function renderDashboardV2Reasons(rows) {
  const node = document.getElementById("dashboard-v2-reasons");
  if (!node) return;
  if (!rows.length) {
    node.innerHTML = '<span class="empty">집계된 대기/차단 사유가 없습니다</span>';
    return;
  }
  node.innerHTML = rows.slice(0, 8).map((item) => `
    <div class="dashboard-v2-list-item">
      <div><strong>${escapeHtml(item.reason_ko || item.reason_code || "-")}</strong>${badge(item.severity || "normal")}</div>
      <p>${escapeHtml(item.reason_code || "-")} · ${escapeHtml(item.count ?? 0)}건 · ${escapeHtml(item.suggested_action_ko || "")}</p>
    </div>
  `).join("");
}

function renderDashboardV2EntryRows(rows) {
  const node = document.getElementById("dashboard-v2-entry-rows");
  if (!node) return;
  if (!rows.length) {
    node.innerHTML = '<tr><td colspan="5" class="empty">진입 준비 관찰 후보가 없습니다</td></tr>';
    return;
  }
  node.innerHTML = rows.slice(0, 12).map((item) => `
    <tr>
      <td>${textCell(`${item.code || "-"} ${item.name || ""}`.trim())}</td>
      <td>${textCell(item.theme_name || "-")}</td>
      <td>${badge(item.display_bucket_label || item.display_bucket || "-")}</td>
      <td>${textCell(item.current_price || "-")}</td>
      <td>${textCell(item.reason_summary_ko || item.operator_message_ko || "-")}</td>
    </tr>
  `).join("");
}

function renderDashboardV2PositionRows(rows) {
  const node = document.getElementById("dashboard-v2-position-rows");
  if (!node) return;
  if (!rows.length) {
    node.innerHTML = '<tr><td colspan="5" class="empty">보유 포지션이 없습니다</td></tr>';
    return;
  }
  node.innerHTML = rows.slice(0, 12).map((item) => `
    <tr>
      <td>${textCell(`${item.code || "-"} ${item.name || ""}`.trim())}</td>
      <td>${textCell(formatPercentValue(item.current_return_pct))}</td>
      <td>${badge(item.exit_status || item.position_market_action || "HOLD")}</td>
      <td>${textCell(`${item.holding_minutes || 0}분`)}</td>
      <td>${textCell(`${item.market_side || "-"} ${item.market_status || "-"} / ${item.position_market_action || "-"} / ${item.actual_order_status || "OBSERVE_ONLY"}`)}</td>
    </tr>
  `).join("");
}

function renderDashboardV2MarketSideBudgets(budgets) {
  const node = document.getElementById("dashboard-v2-market-side-budgets");
  if (!node) return;
  const sides = ["KOSPI", "KOSDAQ"];
  node.innerHTML = sides.map((side) => {
    const item = budgets[side] || {};
    const reserved = Number(item.reserved_exposure_krw || 0);
    const limit = Number(item.effective_exposure_limit_krw || 0);
    const slots = item.available_position_slots ?? "-";
    return `
      <div class="dashboard-v2-budget-row">
        <strong>${side}</strong>
        ${badge(item.budget_action || "DATA_WAIT")}
        <span>${escapeHtml(item.side_market_regime || "-")}</span>
        <span>${formatCurrencyShort(reserved)} / ${limit ? formatCurrencyShort(limit) : "-"}</span>
        <span>slots ${escapeHtml(slots)}</span>
      </div>
    `;
  }).join("");
}

function statusTone(value) {
  const textValue = String(value || "");
  if (/위험|주문차단/.test(textValue)) return "bad";
  if (/대기|관찰|제한/.test(textValue)) return "warn";
  return "ok";
}

function preMarketTone(value) {
  const textValue = String(value || "");
  if (/NO_GO/i.test(textValue)) return "bad";
  if (/MANUAL|WARN|UNKNOWN/i.test(textValue)) return "warn";
  if (/GO_/i.test(textValue)) return "ok";
  return "muted";
}

function renderThemeLabSummary(themeLab) {
  const payload = themeLab || {};
  const summary = payload.summary || {};
  const dataQuality = payload.data_quality || {};
  const status = summary.operation_status || "SNAPSHOT_UNAVAILABLE";
  const tone = /READY_TO_TRADE/i.test(status)
    ? "ok"
    : /BLOCKED|BROKEN|RISK/i.test(status)
      ? "bad"
      : /WAIT|LIVE_BLOCKED|QUALITY/i.test(status)
        ? "warn"
        : "muted";

  text("themelab-operation-status", status);
  cls("themelab-operation-status", `counter ${tone}`);
  text("themelab-operation-message", summary.operation_message_ko || "ThemeLabFlow 결과 대기 중");
  text("themelab-ready", `${summary.ready_count || 0} / ${summary.ready_small_count || 0}`);
  text("themelab-wait-blocked", `${summary.wait_count || 0} / ${summary.blocked_count || 0}`);
  text("themelab-top-theme", summary.top_theme_name || "-");
  text("themelab-data-quality", dataQuality.status || "UNKNOWN");
  text("themelab-live-readiness", `${summary.live_guard_passed_count || 0} 통과 / ${summary.live_guard_blocked_count || 0} 차단`);
  text("themelab-order-candidates", summary.order_candidate_count || 0);
}

function renderLiveSimPreflight(snapshot) {
  const runtime = snapshot.runtime || {};
  const payload = snapshot.live_sim_preflight || runtime.live_sim_preflight || {};
  const status = payload.status || "NO_DATA";
  const tone = status === "GO" ? "ok" : status === "GO_WITH_WARNINGS" ? "warn" : /FAIL_CLOSED|NO_GO/.test(status) ? "bad" : "muted";
  const account = payload.account_mode_summary || {};
  const modes = account.normalized_modes || {};
  const performance = payload.performance_summary || {};
  const gatewayLoad = payload.gateway_load_summary || {};
  const backfill = payload.backfill_summary || {};
  const safety = payload.safety_summary || {};
  const loadGuard = backfill.load_guard || {};
  const blocking = payload.blocking_reasons || [];
  const warnings = payload.warning_reasons || [];

  text("live-sim-preflight-status", status);
  cls("live-sim-preflight-status", `counter ${tone}`);
  text("live-sim-preflight-message", payload.operator_message_ko || "LIVE_SIM 사전 점검 결과가 아직 없습니다.");
  text(
    "live-sim-preflight-account",
    [
      account.simulation_confirmed ? "SIMULATION" : account.real_detected ? "REAL" : "UNKNOWN",
      account.account_masked || "",
      modes.broker_env ? `broker=${modes.broker_env}` : "",
    ].filter(Boolean).join(" / ")
  );
  text(
    "live-sim-preflight-gateway",
    [
      gatewayLoad.heartbeat_ok ? "heartbeat OK" : "heartbeat 확인 필요",
      `queue ${gatewayLoad.gateway_queue_depth || 0}`,
      `order ${gatewayLoad.order_command_pending_count || 0}`,
    ].join(" / ")
  );
  text("live-sim-preflight-live-real", safety.live_real_enabled ? "ON" : "OFF");
  text("live-sim-preflight-kill-switch", safety.kill_switch_active ? "ON" : "OFF");
  text("live-sim-preflight-net", formatPercentValue(performance.net_expectancy));
  text("live-sim-preflight-accepted", performance.accepted_completed_lifecycle_count || performance.dry_run_accepted_count || 0);
  text("live-sim-preflight-bad-ready", formatRate(performance.bad_ready_rate));
  text(
    "live-sim-preflight-stale",
    `${formatRate(performance.stale_tick_rate)} / ${formatRate(performance.latency_distortion_rate)}`
  );
  text(
    "live-sim-preflight-queue",
    `${gatewayLoad.gateway_queue_depth || 0} total / ${gatewayLoad.backfill_pending_count || 0} backfill`
  );
  text(
    "live-sim-preflight-backfill",
    [
      loadGuard.load_guard_status || backfill.load_guard_status || "-",
      backfill.tr_backfill_caused_ready_count ? `READY evidence ${backfill.tr_backfill_caused_ready_count}` : "",
    ].filter(Boolean).join(" / ")
  );
  renderInlineCounts(
    "live-sim-preflight-blocking",
    [
      ...blocking.map((reason) => ({ reason, count: "BLOCK" })),
      ...warnings.map((reason) => ({ reason, count: "WARN" })),
    ],
    "reason",
    "차단 또는 경고 사유가 없습니다"
  );
  text("live-sim-preflight-action", payload.recommended_action_ko || "-");
}

function renderLiveSimCanary(snapshot) {
  const runtime = snapshot.runtime || {};
  const payload = snapshot.live_sim_canary || runtime.live_sim_canary || {};
  const summary = payload.summary || {};
  const configStatus = payload.config_status || "disabled";
  const status = payload.status || configStatus.toUpperCase().replace("-", "_");
  const tone = configStatus === "order-enabled" ? "warn" : configStatus === "observe-only" ? "ok" : "muted";

  text("live-sim-canary-status", status || "DISABLED");
  cls("live-sim-canary-status", `counter ${tone}`);
  text("live-sim-canary-updated", formatDateTime(payload.last_updated_at));
  text("live-sim-canary-message", payload.watch_provisional_notice_ko || "WATCH/PROVISIONAL은 아직 LIVE_SIM Canary 주문 대상이 아닙니다.");
  text("live-sim-canary-config", `${configStatus} / order ${payload.order_enabled ? "ON" : "OFF"}`);
  text("live-sim-canary-preflight", summary.preflight_status || payload.preflight_status || "NO_DATA");
  text("live-sim-canary-loadguard", summary.load_guard_status || payload.load_guard_status || "NO_DATA");
  text("live-sim-canary-dryrun", summary.dry_run_go_no_go_status || payload.dry_run_go_no_go_status || "NO_DATA");
  text("live-sim-canary-eligible", summary.eligible_count ?? 0);
  text("live-sim-canary-blocked", summary.blocked_count ?? 0);
  text("live-sim-canary-submitted", summary.submitted_count ?? 0);
  text("live-sim-canary-filled", summary.filled_count ?? 0);
  text("live-sim-canary-day-limit", payload.max_orders_per_day ?? 0);
  text("live-sim-canary-max-amount", fmtNumber(payload.max_position_amount_krw ?? 0, 0));
  renderLiveSimCanaryReasons(summary.blocked_reason_top || []);
  renderLiveSimCanaryDecisions(payload.recent_decisions || []);
}

function renderLiveSimCanaryReasons(rows) {
  const node = document.getElementById("live-sim-canary-reasons");
  if (!node) return;
  const items = firstItems(rows || [], 10);
  node.innerHTML = items.length
    ? items.map((item) => `
      <div class="alert-item warn">
        <strong>${escapeHtml(item.reason || item.key || "-")}</strong>
        <span>blocked</span>
        <em>${escapeHtml(item.count ?? 0)}</em>
      </div>
    `).join("")
    : `<span class="empty">차단 사유가 아직 없습니다.</span>`;
}

function renderLiveSimCanaryDecisions(rows) {
  const node = document.getElementById("live-sim-canary-decisions");
  if (!node) return;
  const items = firstItems(rows || [], 8);
  node.innerHTML = items.length
    ? items.map((item) => {
      const reasons = (item.blocking_reasons || item.reason_codes || []).slice(0, 4).join(", ") || "-";
      const detail = {
        decision_id: item.decision_id,
        code: item.code,
        theme_name: item.theme_name,
        hybrid_status: item.hybrid_status,
        hybrid_score: item.hybrid_score,
        hybrid_position_tier: item.hybrid_position_tier,
        stock_role: item.stock_role,
        price_location_status: item.price_location_status,
        price_location_readiness: item.price_location_readiness,
        blocking_reasons: item.blocking_reasons || [],
        limit_price: item.limit_price,
        quantity: item.quantity,
        order_intent_id: item.order_intent_id,
        gateway_command_id: item.gateway_command_id,
        metadata: item.metadata || item.details || {},
      };
      return `
        <details class="canary-decision">
          <summary>
            <span>${escapeHtml(item.code || "-")} ${escapeHtml(item.theme_name || "")}</span>
            ${badge(item.status || "-", item.eligible ? "ok" : "warn")}
            <em>${escapeHtml(reasons)}</em>
          </summary>
          <pre>${escapeHtml(JSON.stringify(detail, null, 2))}</pre>
        </details>
      `;
    }).join("")
    : `<span class="empty">최근 Canary 판단이 없습니다.</span>`;
}

function renderLiveSimAudit(snapshot) {
  const runtime = snapshot.runtime || {};
  const payload = snapshot.live_sim_audit || runtime.live_sim_audit || {};
  const summary = payload.summary || {};
  const available = Boolean(payload.available);
  const status = payload.status || (available ? "UNKNOWN" : "NO_DATA");
  const tone = status === "OK" ? "ok" : status === "BROKEN" ? "bad" : /RECONCILE|WARN/.test(status) ? "warn" : "muted";
  const message = ((payload.operator || {}).status_message_ko) || (available ? "LIVE_SIM audit 확인 중" : "아직 LIVE_SIM 주문 audit 데이터가 없습니다.");

  text("live-sim-audit-status", `${status} · ${message}`);
  cls("live-sim-audit-status", `counter ${tone}`);
  text("live-sim-audit-updated", formatDateTime(payload.last_updated_at));
  text("live-sim-audit-empty", available ? "send_order부터 체결/취소/포지션/reconcile까지 audit 원장을 표시합니다." : "아직 LIVE_SIM 주문 audit 데이터가 없습니다.");
  text("live-sim-audit-open-orders", summary.open_live_sim_order_count ?? 0);
  text("live-sim-audit-unknown-submit", summary.unknown_submit_count ?? 0);
  text("live-sim-audit-reconcile-orders", summary.reconcile_required_order_count ?? 0);
  text("live-sim-audit-broker-missing", summary.broker_order_id_missing_count ?? 0);
  text("live-sim-audit-cancel-stale", summary.cancel_requested_stale_count ?? 0);
  text("live-sim-audit-orphan-exec", summary.orphan_execution_count ?? 0);
  text("live-sim-audit-position-mismatch", summary.position_qty_mismatch_count ?? 0);
  text("live-sim-audit-duplicate-open-position", summary.duplicate_open_position_count ?? 0);
  renderLiveSimAuditLines(
    "live-sim-audit-top-actions",
    ((payload.operator || {}).top_actions || []).map((item) => ({
      label: item.issue_type || "-",
      value: item.count ?? 0,
      detail: item.operator_message_ko || item.suggested_action || "",
      tone: /BROKEN|MISMATCH|MISSING|UNKNOWN|RECONCILE/.test(String(item.issue_type || "")) ? "warn" : "muted",
    })),
    "필요한 운영 조치가 없습니다."
  );
  renderLiveSimAuditLines(
    "live-sim-audit-issues",
    (payload.issues || []).slice(0, 6).map((item) => ({
      label: item.issue_type || "-",
      value: item.severity || "-",
      detail: `${item.code || ""} ${item.operator_message_ko || ""}`.trim(),
      tone: item.severity === "BROKEN" ? "bad" : /RECONCILE|WARN/.test(String(item.severity || "")) ? "warn" : "muted",
    })),
    "최근 audit 이슈가 없습니다."
  );
}

function renderLiveSimAuditLines(id, rows, emptyText) {
  const node = document.getElementById(id);
  if (!node) return;
  const items = firstItems(rows || [], 6);
  node.innerHTML = items.length
    ? items.map((item) => `
      <div class="alert-item ${escapeHtml(item.tone || "info")}">
        <strong>${escapeHtml(item.label || "-")}</strong>
        <span>${escapeHtml(item.detail || "")}</span>
        <em>${escapeHtml(item.value ?? "")}</em>
      </div>
    `).join("")
    : `<span class="empty">${escapeHtml(emptyText)}</span>`;
}

function renderLiveSimCanaryPerformance(snapshot) {
  const runtime = snapshot.runtime || {};
  const payload = snapshot.live_sim_canary_performance || runtime.live_sim_canary_performance || {};
  const summary = payload.summary || {};
  const cases = payload.cases || payload.items || [];
  const available = Boolean(payload.available) || Number(summary.total_lifecycle_count || 0) > 0;
  const status = available ? (payload.status || "READY") : "NO_DATA";
  const tone = /RECONCILE|BAD|CRITICAL/.test(JSON.stringify(summary.issue_counts || {})) ? "warn" : available ? "ok" : "muted";

  text("live-sim-canary-performance-status", status);
  cls("live-sim-canary-performance-status", `counter ${tone}`);
  text("live-sim-canary-performance-updated", formatDateTime(payload.generated_at));
  text(
    "live-sim-canary-performance-empty",
    available
      ? "LIVE_SIM Canary 주문의 실제 체결/청산과 DRY_RUN 대비 차이를 표시합니다."
      : "Canary 주문의 체결 품질과 DRY_RUN 대비 차이를 기다리는 중입니다."
  );
  text("live-sim-canary-performance-total", summary.today_canary_order_count ?? summary.total_lifecycle_count ?? 0);
  text("live-sim-canary-performance-submit-ack", `${summary.submitted_count ?? 0} / ${summary.broker_accepted_count ?? 0}`);
  text("live-sim-canary-performance-partial-full", `${summary.partial_fill_count ?? 0} / ${summary.full_fill_count ?? 0}`);
  text("live-sim-canary-performance-nofill-cancel", `${summary.no_fill_count ?? 0} / ${summary.cancelled_count ?? 0}`);
  text("live-sim-canary-performance-closed-reconcile", `${summary.closed_count ?? 0} / ${summary.reconcile_required_count ?? 0}`);
  text("live-sim-canary-performance-fill-ratio", formatRate(summary.avg_fill_ratio));
  text("live-sim-canary-performance-slippage", formatBp(summary.avg_entry_slippage_bp, 1));
  text("live-sim-canary-performance-net", formatPercentValue(summary.avg_net_return_pct));
  text("live-sim-canary-performance-gap", formatPercentValue(summary.avg_live_vs_dry_run_net_diff_pct));
  text("live-sim-canary-performance-worse", formatRate(summary.live_worse_rate));
  text("live-sim-canary-performance-nofill-rate", formatRate(summary.no_fill_rate));
  text("live-sim-canary-performance-orphan", summary.orphan_case_count ?? 0);
  renderLiveSimCanaryPerformanceRows(cases);
  renderLiveSimCanaryPerformanceRecommendations(payload.recommendations || []);
}

function renderLiveSimCanaryPerformanceRows(rows) {
  const body = document.getElementById("live-sim-canary-performance-rows");
  if (!body) return;
  const items = firstItems(rows || [], 10);
  if (!items.length) {
    body.innerHTML = `<tr><td class="empty" colspan="13">표시할 LIVE_SIM Canary 성과 케이스가 없습니다.</td></tr>`;
    return;
  }
  body.innerHTML = items.map((item) => {
    const issues = (item.issue_types || (item.issues || []).map((issue) => issue.issue_type)).filter(Boolean);
    const detail = {
      linked_ids: item.linked_ids || {},
      order_timeline: item.order_timeline || [],
      fill_timeline: item.fill_timeline || [],
      exit_timeline: item.exit_timeline || [],
      dry_run_vs_live_sim: {
        dry_run_expected_entry_price: item.dry_run_expected_entry_price,
        live_sim_avg_entry_price: item.live_sim_avg_entry_price,
        dry_run_net_return_pct: item.dry_run_net_return_pct,
        live_sim_net_return_pct: item.live_sim_net_return_pct,
        net_return_diff_pct: item.net_return_diff_pct,
        dry_run_exit_reason: item.dry_run_exit_reason,
        live_sim_exit_reason: item.live_sim_exit_reason,
        outcome_match: item.outcome_match,
      },
      raw_metadata: item.raw_metadata || {},
    };
    return `
      <tr>
        <td>${escapeHtml([item.code, item.name].filter(Boolean).join(" ") || "-")}</td>
        <td>${escapeHtml(item.theme || "-")}</td>
        <td>${escapeHtml(fmtOptionalNumber(item.hybrid_score, 1))}</td>
        <td>${escapeHtml(fmtOptionalNumber(item.requested_price, 0))} / ${escapeHtml(fmtOptionalNumber(item.avg_fill_price, 0))}</td>
        <td>${escapeHtml(formatRate(item.fill_ratio))}</td>
        <td>${escapeHtml(formatBp(item.entry_slippage_bp, 1))}</td>
        <td>${escapeHtml(item.exit_reason || "-")}</td>
        <td>${escapeHtml(formatPercentValue(item.net_return_pct))}</td>
        <td>${escapeHtml(formatPercentValue(item.dry_run_net_return_pct))}</td>
        <td>${escapeHtml(formatPercentValue(item.net_return_diff_pct))}</td>
        <td>${badge(item.outcome_match || "INCOMPARABLE")}</td>
        <td>${badge(item.final_status || "UNKNOWN")}</td>
        <td>
          <details class="canary-decision">
            <summary>${escapeHtml(issues.slice(0, 2).join(", ") || item.fill_quality_grade || "-")}</summary>
            <pre>${escapeHtml(JSON.stringify(detail, null, 2))}</pre>
          </details>
        </td>
      </tr>
    `;
  }).join("");
}

function renderLiveSimCanaryPerformanceRecommendations(rows) {
  const node = document.getElementById("live-sim-canary-performance-recommendations");
  if (!node) return;
  const items = firstItems(rows || [], 5);
  node.innerHTML = items.length
    ? items.map((item) => `
      <div class="alert-item info">
        <strong>검토 전용</strong>
        <span>${escapeHtml(item)}</span>
        <em>자동 적용 없음</em>
      </div>
    `).join("")
    : `<span class="empty">검토 전용 권고가 아직 없습니다.</span>`;
}

function renderExitPolicyValidation(snapshot) {
  const runtime = snapshot.runtime || {};
  const payload = snapshot.exit_policy_validation || runtime.exit_policy_validation || {};
  const summary = payload.summary || {};
  const scenarios = payload.scenario_summary || [];
  const cases = payload.cases || payload.items || [];
  const available = Boolean(payload.available) || Number(summary.shadow_case_count || 0) > 0;
  const bestScenario = summary.best_shadow_scenario || "-";

  text("exit-policy-validation-status", available ? (payload.status || "READY") : "NO_DATA");
  cls("exit-policy-validation-status", `counter ${available ? "ok" : "muted"}`);
  text("exit-policy-validation-updated", formatDateTime(payload.generated_at));
  text(
    "exit-policy-validation-empty",
    available
      ? "분석 전용 / 실제 적용 아님. Shadow exit와 실제 LIVE_SIM 청산 품질을 비교합니다."
      : "Exit 정책 검증 표본을 기다리는 중입니다. 가격 경로가 없으면 데이터 부족으로 표시됩니다."
  );
  text("exit-policy-validation-lifecycle", summary.analysis_lifecycle_count ?? 0);
  text("exit-policy-validation-actual-net", formatPercentValue(summary.actual_live_sim_avg_net_return_pct));
  text("exit-policy-validation-best", bestScenario);
  text("exit-policy-validation-best-exp", formatPercentValue(summary.best_shadow_expectancy_pct));
  text("exit-policy-validation-stoploss", summary.stop_loss_hit_count ?? 0);
  text("exit-policy-validation-takeprofit", summary.take_profit_capture_count ?? 0);
  text("exit-policy-validation-timeweak", summary.time_exit_weak_count ?? 0);
  text("exit-policy-validation-trailing", summary.trailing_would_improve_count ?? 0);
  text("exit-policy-validation-giveback", summary.giveback_large_count ?? 0);
  text("exit-policy-validation-insufficient", summary.insufficient_data_count ?? 0);
  renderExitPolicyValidationScenarioRows(scenarios);
  renderExitPolicyValidationCaseRows(cases);
  renderExitPolicyValidationRecommendations(payload.recommendations || []);
}

function renderExitPolicyValidationScenarioRows(rows) {
  const body = document.getElementById("exit-policy-validation-scenarios");
  if (!body) return;
  const items = firstItems(rows || [], 10);
  if (!items.length) {
    body.innerHTML = `<tr><td class="empty" colspan="10">표시할 Exit 정책 검증 scenario가 없습니다.</td></tr>`;
    return;
  }
  body.innerHTML = items.map((item) => `
    <tr>
      <td>${escapeHtml(item.scenario_id || "-")}</td>
      <td>${escapeHtml(item.sample_count ?? 0)}</td>
      <td>${escapeHtml(formatRate(item.win_rate))}</td>
      <td>${escapeHtml(formatPercentValue(item.expectancy_pct))}</td>
      <td>${escapeHtml(formatPercentValue(item.avg_net_return_pct))}</td>
      <td>${escapeHtml(formatPercentValue(item.avg_mae_pct))}</td>
      <td>${escapeHtml(fmtOptionalNumber(item.avg_hold_minutes, 1))}</td>
      <td>${escapeHtml(item.better_than_actual_count ?? 0)}</td>
      <td>${escapeHtml(item.worse_than_actual_count ?? 0)}</td>
      <td>${badge(item.recommendation_grade || "INSUFFICIENT_SAMPLE")}</td>
    </tr>
  `).join("");
}

function renderExitPolicyValidationCaseRows(rows) {
  const body = document.getElementById("exit-policy-validation-cases");
  if (!body) return;
  const items = firstItems(rows || [], 10);
  if (!items.length) {
    body.innerHTML = `<tr><td class="empty" colspan="8">표시할 Exit 정책 검증 case가 없습니다.</td></tr>`;
    return;
  }
  body.innerHTML = items.map((item) => {
    const detail = {
      price_path_summary: item.price_path_summary || {},
      actual: item.actual || {},
      shadow_result: item.shadow_result || {},
      segments: item.segments || {},
      context_risk_signals: item.context_risk_signals || {},
      raw_details_json: item.raw_details_json || {},
    };
    return `
      <tr>
        <td>${escapeHtml([item.code, item.name].filter(Boolean).join(" ") || "-")}</td>
        <td>${escapeHtml(item.scenario_id || "-")}</td>
        <td>${escapeHtml(item.actual_exit_type || "-")} / ${escapeHtml(formatPercentValue(item.actual_net_return_pct))}</td>
        <td>${escapeHtml(item.shadow_exit_type || "-")} / ${escapeHtml(formatPercentValue(item.shadow_net_return_pct))}</td>
        <td>${escapeHtml(formatPercentValue(item.net_return_delta_pct))}</td>
        <td>${badge(item.comparison_label || "INCOMPARABLE")}</td>
        <td>${escapeHtml(item.issue_type || "-")}</td>
        <td>
          <details class="canary-decision">
            <summary>${escapeHtml(item.operator_message_ko || "상세")}</summary>
            <pre>${escapeHtml(JSON.stringify(detail, null, 2))}</pre>
          </details>
        </td>
      </tr>
    `;
  }).join("");
}

function renderExitPolicyValidationRecommendations(rows) {
  const node = document.getElementById("exit-policy-validation-recommendations");
  if (!node) return;
  const items = firstItems(rows || [], 5);
  node.innerHTML = items.length
    ? items.map((item) => `
      <div class="alert-item info">
        <strong>${escapeHtml(item.scenario_id || "검토 전용")}</strong>
        <span>${escapeHtml(item.recommendation_reason_ko || "")}</span>
        <em>${escapeHtml(item.recommendation_grade || "WATCH")}</em>
      </div>
    `).join("")
    : `<span class="empty">Exit 정책 권고는 표본 누적 후 표시됩니다.</span>`;
}

function renderBuyZeroRca(snapshot) {
  const runtime = snapshot.runtime || {};
  const payload = snapshot.buy_zero_rca || runtime.buy_zero_rca || {};
  const summary = payload.summary || payload || {};
  const available = Boolean(payload.available) || Number(payload.total_trace_events || 0) > 0;
  const buyZero = Number(summary.live_sim_submitted_count ?? payload.live_sim_submitted_count ?? 0) === 0;
  const statusText = !available ? "TRACE 대기" : buyZero ? "오늘 LIVE_SIM 매수 0건" : "LIVE_SIM 주문 발생";
  const statusTone = !available ? "muted" : buyZero ? "warn" : "ok";
  const sessionStatus = runtime.market_session_status || payload.market_session_status || "";
  const isRegular = /REGULAR|OPEN|장중|정규/i.test(sessionStatus);

  text("buy-zero-rca-status", statusText);
  cls("buy-zero-rca-status", `counter ${statusTone}`);
  text("buy-zero-rca-market-session", sessionStatus ? (isRegular ? `정규장 ${sessionStatus}` : `정규장 아님: ${sessionStatus}`) : "정규장 확인 중");
  cls("buy-zero-rca-market-session", `counter ${sessionStatus && !isRegular ? "warn" : "muted"}`);
  text(
    "buy-zero-rca-empty",
    available
      ? "RCA trace는 PR 적용 이후 이벤트부터 표시됩니다. 과거 DB backfill은 하지 않습니다."
      : "아직 RCA trace가 없습니다. PR 적용 이후 이벤트부터 쌓입니다."
  );
  text("buy-zero-rca-total-candidates", summary.total_candidates ?? payload.total_candidates ?? 0);
  text("buy-zero-rca-gate-evaluated", summary.gate_evaluated_count ?? payload.gate_evaluated_count ?? 0);
  text("buy-zero-rca-ready-counts", `${summary.ready_exact_count ?? payload.ready_exact_count ?? summary.ready_count ?? payload.ready_count ?? 0} / ${summary.ready_small_count ?? payload.ready_small_count ?? 0}`);
  text("buy-zero-rca-wait-observe-counts", `${summary.wait_count ?? payload.wait_count ?? 0} / ${summary.observe_count ?? payload.observe_count ?? 0}`);
  text("buy-zero-rca-blocked-count", summary.blocked_count ?? payload.blocked_count ?? 0);
  text("buy-zero-rca-entry-plan-count", summary.entry_plan_created_count ?? payload.entry_plan_created_count ?? 0);
  text("buy-zero-rca-entry-submittable-count", summary.entry_plan_submittable_count ?? payload.entry_plan_submittable_count ?? 0);
  text("buy-zero-rca-dry-run-count", summary.dry_run_intent_count ?? payload.dry_run_intent_count ?? 0);
  text("buy-zero-rca-live-submitted-count", summary.live_sim_submitted_count ?? payload.live_sim_submitted_count ?? 0);
  text("buy-zero-rca-live-blocked-count", summary.live_sim_blocked_count ?? payload.live_sim_blocked_count ?? 0);
  const topStage = firstItems(payload.top_block_stage || [], 1)[0] || {};
  text("buy-zero-rca-top-block-stage", topStage.stage ? `${topStage.stage} (${topStage.count || 0})` : "-");
  text("buy-zero-rca-last-updated", formatDateTime(summary.last_updated_at || payload.last_updated_at));
  renderBuyZeroInlineCounts("buy-zero-rca-top-causes", payload.operator_top_3_causes || payload.top_block_reasons || [], "reason", "아직 집계된 차단 사유가 없습니다");
  renderBuyZeroInlineCounts(
    "buy-zero-rca-data-quality-blocks",
    ((payload.data_quality_blocks || {}).reasons || payload.top_data_insufficient_reasons || []),
    "reason",
    "데이터 품질 부족 사유가 없습니다"
  );
  renderBuyZeroDataQualityCounts("buy-zero-rca-data-quality-blocks", payload);
  renderBuyZeroStageFunnel(payload.stage_funnel || []);
  renderBuyZeroReadyRows(state.buyZeroRca.readyRows, available);
  renderBuyZeroRallyRows(state.buyZeroRca.rallyRows, available);
  renderBuyZeroStageRows(state.buyZeroRca.stageRows, state.buyZeroRca.stage);
  if (available && Date.now() - state.buyZeroRca.lastFetchedAt > BUY_ZERO_RCA_TABLE_REFRESH_MS) {
    refreshBuyZeroRcaTables().catch((error) => {
      state.buyZeroRca.error = error.message;
      text("buy-zero-rca-ready-status", `오류: ${error.message}`);
      cls("buy-zero-rca-ready-status", "counter bad");
    });
  }
}

function renderConservativeReasonOutcomes(snapshot) {
  const runtime = snapshot.runtime || {};
  const payload = snapshot.conservative_reason_outcomes || runtime.conservative_reason_outcomes || {};
  const summary = payload.summary || {};
  const review = payload.review_for_small_entry || {};
  const reviewSummary = review.summary || {};
  const available = Boolean(payload.available);
  const status = payload.status || (available ? "READY" : "NO_DATA");
  text("conservative-reason-status", status);
  cls("conservative-reason-status", `counter ${available ? "ok" : "muted"}`);
  text(
    "conservative-reason-empty",
    available
      ? "보수적 차단 사유 outcome을 읽기 전용으로 검증합니다. 주문 설정은 자동 변경하지 않습니다."
      : "아직 outcome 관측 데이터가 부족합니다. 장중에는 5/15/30분 결과가 아직 확정되지 않을 수 있습니다."
  );
  text("conservative-reason-updated", formatDateTime(payload.last_updated_at || payload.generated_at));
  text("conservative-reason-event-count", summary.event_count ?? 0);
  text("conservative-reason-missed-rate", formatRate(summary.missed_opportunity_rate || 0));
  text("conservative-reason-good-rate", formatRate(summary.good_block_rate || 0));
  text("conservative-reason-risk-rate", formatRate(summary.risk_avoided_rate || 0));
  text("conservative-reason-false-count", summary.false_block_candidate_count ?? 0);
  text("conservative-reason-small-count", reviewSummary.candidate_count ?? 0);
  renderConservativeReasonLines("conservative-reason-group-lines", payload.by_group || [], "아직 group outcome이 없습니다.");
  renderConservativeSmallEntryLines("conservative-reason-small-lines", review.by_reason_code || [], "소액 진입 검토 후보가 없습니다.");
  renderConservativeStockLines("conservative-reason-missed-lines", payload.top_missed_opportunity_stocks || [], "놓친 기회 상위 종목이 없습니다.");
  renderConservativeStockLines("conservative-reason-good-lines", payload.top_good_block_stocks || [], "좋은 차단 상위 종목이 없습니다.");
}

function renderShadowSmallEntryPromotion(snapshot) {
  const runtime = snapshot.runtime || {};
  const payload = snapshot.shadow_small_entry_promotion || runtime.shadow_small_entry_promotion || {};
  const summary = payload.summary || {};
  const available = Boolean(payload.available);
  const status = payload.status || (available ? "READY" : "NO_DATA");
  text("shadow-small-entry-promotion-status", status);
  cls("shadow-small-entry-promotion-status", `counter ${available ? "ok" : "muted"}`);
  text("shadow-small-entry-promotion-mode", `${payload.mode || summary.mode || "observe_only"} / order ${payload.order_enabled ? "ON" : "OFF"}`);
  cls("shadow-small-entry-promotion-mode", `counter ${payload.order_enabled ? "warn" : "muted"}`);
  text(
    "shadow-small-entry-promotion-empty",
    payload.order_enabled
      ? "조건 충족 후보는 1차 leg만 LIVE_SIM guarded 주문 가능합니다."
      : "리포트 근거는 있으나 order_enabled=false라 주문하지 않습니다."
  );
  text("shadow-small-entry-promotion-updated", formatDateTime(payload.last_updated_at));
  text("shadow-small-entry-promotion-candidate-count", payload.candidate_count ?? summary.candidate_count ?? 0);
  text("shadow-small-entry-promotion-observe-count", payload.observe_only_count ?? summary.observe_only_count ?? 0);
  text("shadow-small-entry-promotion-promoted-count", payload.promoted_count ?? summary.promoted_count ?? 0);
  text("shadow-small-entry-promotion-blocked-count", payload.blocked_count ?? summary.blocked_count ?? 0);
  text("shadow-small-entry-promotion-used-count", payload.used_promotions_today ?? summary.used_promotions_today ?? 0);
  text("shadow-small-entry-promotion-day-limit", payload.max_promotions_per_day ?? summary.max_promotions_per_day ?? 0);
  renderKeyCountLines("shadow-small-entry-promotion-group-lines", payload.top_reason_groups || summary.top_reason_groups || [], "아직 승격 reason group이 없습니다.");
  renderKeyCountLines("shadow-small-entry-promotion-code-lines", payload.top_reason_codes || summary.top_reason_codes || [], "아직 승격 reason code가 없습니다.");
}

function renderShadowSmallEntryOps(snapshot) {
  const runtime = snapshot.runtime || {};
  const payload = snapshot.shadow_small_entry_ops || runtime.shadow_small_entry_ops || {};
  const today = payload.today || {};
  const audit = payload.audit || {};
  const limits = payload.limits || {};
  const status = payload.status || "OBSERVE_ONLY";
  const orderEnabled = Boolean(payload.order_enabled);
  const preflightStatus = payload.preflight_status || "NO_DATA";
  const activationLabel = payload.activation_armed ? `armed until ${formatDateTime(payload.activation_expires_at)}` : "-";
  text("shadow-small-entry-ops-status", status);
  cls("shadow-small-entry-ops-status", `counter ${status === "LIVE_SIM_ACTIVE" ? "warn" : status.startsWith("PAUSED") || status === "BROKEN" ? "bad" : "muted"}`);
  text("shadow-small-entry-ops-message", payload.operator_message_ko || "현재는 관측 전용입니다. LIVE_SIM 활성화는 preflight와 2단계 확인이 필요합니다.");
  text("shadow-small-entry-ops-mode", `${payload.mode || "observe_only"} / ${orderEnabled ? "ON" : "OFF"}`);
  text("shadow-small-entry-ops-order-enabled", orderEnabled ? "ON" : "OFF");
  text("shadow-small-entry-ops-preflight-status", preflightStatus);
  text("shadow-small-entry-ops-activation", activationLabel);
  text("shadow-small-entry-ops-promotion-count", today.promotion_count ?? 0);
  text("shadow-small-entry-ops-submitted-count", today.submitted_count ?? 0);
  text("shadow-small-entry-ops-filled-count", today.filled_count ?? 0);
  text("shadow-small-entry-ops-open-position-count", today.open_position_count ?? 0);
  text("shadow-small-entry-ops-notional", fmtNumber(today.total_notional_krw ?? 0, 0));
  text("shadow-small-entry-ops-audit-status", audit.live_sim_audit_status || audit.status || "UNKNOWN");
  text("shadow-small-entry-ops-reconcile-status", audit.reconcile_status || "UNKNOWN");
  text("shadow-small-entry-ops-last-change", formatDateTime(payload.last_status_change_at || payload.last_updated_at));
  renderShadowSmallEntryOpsReasons("shadow-small-entry-ops-blocking-reasons", payload.preflight_blocking_reasons || [], "Preflight 차단 사유가 없습니다.");
  renderShadowSmallEntryOpsRiskLines("shadow-small-entry-ops-risk-lines", today, limits, audit);
}

function renderShadowSmallEntryOpsReasons(id, reasons, emptyText) {
  const node = document.getElementById(id);
  if (!node) return;
  const items = firstItems(reasons || [], 8);
  node.innerHTML = items.length
    ? items.map((reason) => `<span class="badge warning">${escapeHtml(reason)}</span>`).join(" ")
    : `<span class="empty">${escapeHtml(emptyText)}</span>`;
}

function renderShadowSmallEntryOpsRiskLines(id, today, limits, audit) {
  const node = document.getElementById(id);
  if (!node) return;
  const lines = [
    `promotions ${today.promotion_count ?? 0}/${limits.max_promotions_per_day ?? "-"}`,
    `submitted ${today.submitted_count ?? 0}/${limits.max_submitted_orders_per_day ?? "-"}`,
    `notional ${fmtNumber(today.total_notional_krw ?? 0, 0)}/${fmtNumber(limits.max_total_notional_krw ?? 0, 0)}`,
    `open ${today.open_position_count ?? 0}/${limits.max_open_positions ?? "-"}`,
    `unknown ${today.unknown_submit_count ?? 0}`,
    `reconcile ${today.reconcile_required_count ?? 0}`,
    `heartbeat ${audit.gateway_heartbeat_ok ? "OK" : "BAD"}`,
  ];
  node.innerHTML = lines.map((line) => `<span class="counter muted">${escapeHtml(line)}</span>`).join(" ");
}

function renderShadowSmallEntryPilot(snapshot) {
  const runtime = snapshot.runtime || {};
  const payload = snapshot.shadow_small_entry_pilot || runtime.shadow_small_entry_pilot || {};
  const report = state.shadowSmallEntryPilot.report || {};
  const summary = payload.summary || (report.summary || {});
  const status = payload.status || "NO_DATA";
  const recommendation = payload.recommendation || report.recommendation || "-";
  const available = Boolean(payload.available || report.available);
  text("shadow-small-entry-pilot-status", status);
  cls("shadow-small-entry-pilot-status", `counter ${status === "REVIEW_READY" || status === "COMPLETED" ? "ok" : status === "NO_DATA" ? "muted" : "warn"}`);
  text("shadow-small-entry-pilot-id", compactPlain(payload.pilot_id || report.pilot_id || "-"));
  text("shadow-small-entry-pilot-recommendation", recommendation || "-");
  text("shadow-small-entry-pilot-message", payload.operator_message_ko || report.operator_message_ko || "아직 Shadow Small Entry pilot run 데이터가 없습니다. PR 적용 이후 이벤트부터 쌓입니다.");
  text("shadow-small-entry-pilot-candidate-count", summary.candidate_count ?? 0);
  text("shadow-small-entry-pilot-promoted-count", summary.promoted_count ?? 0);
  text("shadow-small-entry-pilot-submitted-count", summary.submitted_order_count ?? 0);
  text("shadow-small-entry-pilot-filled-count", summary.filled_order_count ?? 0);
  text("shadow-small-entry-pilot-open-position-count", summary.open_position_count ?? 0);
  text("shadow-small-entry-pilot-total-pnl", fmtNumber(summary.total_pnl_krw ?? 0, 0));
  text("shadow-small-entry-pilot-win-rate", summary.win_rate == null ? "-" : formatRate(summary.win_rate));
  text("shadow-small-entry-pilot-avg-return", summary.avg_return_pct == null ? "-" : `${fmtNumber(summary.avg_return_pct, 2)}%`);
  text("shadow-small-entry-pilot-mfe-mae", `${summary.avg_mfe_pct == null ? "-" : fmtNumber(summary.avg_mfe_pct, 2)} / ${summary.avg_mae_pct == null ? "-" : fmtNumber(summary.avg_mae_pct, 2)}`);
  text("shadow-small-entry-pilot-updated", formatDateTime(payload.last_updated_at || report.last_updated_at));
  renderShadowSmallEntryPilotReasons(payload.recommendation_reason_codes || report.recommendation_reason_codes || []);
  renderShadowSmallEntryPilotSafety(report.safety_checklist || []);
  renderShadowSmallEntryPilotItems(report.items || [], available);
  maybeFetchShadowSmallEntryPilotReport(payload);
}

function renderShadowSmallEntryPilotReasons(reasons) {
  const node = document.getElementById("shadow-small-entry-pilot-reasons");
  if (!node) return;
  const items = firstItems(reasons || [], 8);
  node.innerHTML = items.length
    ? items.map((reason) => `<span class="badge ${/BROKEN|RECONCILE|UNKNOWN|LOSS|ERROR/.test(String(reason)) ? "warning" : "info"}">${escapeHtml(reason)}</span>`).join(" ")
    : `<span class="empty">추천 reason code가 아직 없습니다.</span>`;
}

function renderShadowSmallEntryPilotSafety(checks) {
  const node = document.getElementById("shadow-small-entry-pilot-safety-lines");
  if (!node) return;
  const items = firstItems(checks || [], 8);
  node.innerHTML = items.length
    ? items.map((item) => `
      <div class="alert-item ${item.status === "PASS" ? "ok" : item.status === "FAIL" ? "bad" : item.status === "WARN" ? "warn" : "info"}">
        <strong>${escapeHtml(item.check_id || "-")}</strong>
        <span>${escapeHtml(item.status || "-")} · ${escapeHtml(item.operator_message_ko || "")}</span>
      </div>
    `).join("")
    : `<span class="empty">안전 체크리스트는 리포트 생성 후 표시됩니다.</span>`;
}

function renderShadowSmallEntryPilotItems(items, available) {
  const node = document.getElementById("shadow-small-entry-pilot-items");
  if (!node) return;
  const rows = firstItems(items || [], 20);
  if (!rows.length) {
    node.innerHTML = `<tr><td colspan="6" class="empty">${available ? "파일럿 후보 상세 rows가 아직 없습니다." : "아직 수집된 pilot trace가 없습니다."}</td></tr>`;
    return;
  }
  node.innerHTML = rows.map((item) => `
    <tr>
      <td>${escapeHtml([item.code, item.name].filter(Boolean).join(" ") || "-")}</td>
      <td>${badge(item.pilot_status || item.promotion_status || "-")}</td>
      <td>${escapeHtml(item.order_status || "-")}</td>
      <td>${escapeHtml(item.fill_qty ?? item.submitted_qty ?? 0)}</td>
      <td>${escapeHtml(fmtNumber(item.realized_pnl_krw ?? item.unrealized_pnl_krw ?? 0, 0))}</td>
      <td>${escapeHtml(item.recommendation || "-")}</td>
    </tr>
  `).join("");
}

function maybeFetchShadowSmallEntryPilotReport(payload) {
  const now = Date.now();
  if (state.shadowSmallEntryPilot.inFlight) return;
  if (now - state.shadowSmallEntryPilot.lastFetchedAt < 60000) return;
  state.shadowSmallEntryPilot.inFlight = true;
  state.shadowSmallEntryPilot.lastFetchedAt = now;
  const params = {};
  if (payload.trade_date) params.trade_date = payload.trade_date;
  apiGet(SHADOW_SMALL_ENTRY_PILOT_ENDPOINTS.report, params)
    .then((report) => {
      state.shadowSmallEntryPilot.report = report;
      renderShadowSmallEntryPilot(state.latestSnapshot || {});
    })
    .catch(() => {})
    .finally(() => {
      state.shadowSmallEntryPilot.inFlight = false;
    });
}

function renderKeyCountLines(id, rows, emptyText) {
  const node = document.getElementById(id);
  if (!node) return;
  const items = firstItems(rows || [], 6);
  node.innerHTML = items.length
    ? items.map((item) => `
      <div class="alert-item info">
        <strong>${escapeHtml(item.key || item.reason || item.group || "-")}</strong>
        <span>${escapeHtml(item.count ?? 0)}건</span>
      </div>
    `).join("")
    : `<span class="empty">${escapeHtml(emptyText)}</span>`;
}

function renderConservativeReasonLines(id, rows, emptyText) {
  const node = document.getElementById(id);
  if (!node) return;
  const items = firstItems(rows || [], 6);
  node.innerHTML = items.length
    ? items.map((item) => `
      <div class="alert-item ${item.recommendation === "REVIEW_FOR_SMALL_ENTRY" ? "warn" : item.recommendation === "KEEP_BLOCK" ? "ok" : "info"}">
        <strong>${escapeHtml(item.group || item.reason_code || "-")}</strong>
        <span>missed ${formatRate(item.missed_opportunity_rate || 0)} · good ${formatRate(item.good_block_rate || 0)} · risk ${formatRate(item.risk_avoided_rate || 0)}</span>
        <em>${escapeHtml(item.recommendation || "-")}</em>
      </div>
    `).join("")
    : `<span class="empty">${escapeHtml(emptyText)}</span>`;
}

function renderConservativeSmallEntryLines(id, rows, emptyText) {
  const node = document.getElementById(id);
  if (!node) return;
  const items = firstItems(rows || [], 6);
  node.innerHTML = items.length
    ? items.map((item) => `
      <div class="alert-item warn">
        <strong>${escapeHtml(item.reason_code || item.group || "-")}</strong>
        <span>${escapeHtml(item.candidate_count ?? 0)} 후보 · MFE ${formatPercentValue(item.avg_mfe_15m_pct)} / MAE ${formatPercentValue(item.avg_mae_15m_pct)}</span>
        <em>x${escapeHtml(fmtNumber(item.suggested_position_size_multiplier, 2))}</em>
      </div>
    `).join("")
    : `<span class="empty">${escapeHtml(emptyText)}</span>`;
}

function renderConservativeStockLines(id, rows, emptyText) {
  const node = document.getElementById(id);
  if (!node) return;
  const items = firstItems(rows || [], 6);
  node.innerHTML = items.length
    ? items.map((item) => `
      <div class="alert-item ${item.good_block ? "ok" : item.missed_opportunity ? "warn" : "info"}">
        <strong>${escapeHtml(`${item.code || "-"} ${item.name || ""}`.trim())}</strong>
        <span>${escapeHtml(item.primary_group || item.primary_reason || "-")} · MFE ${formatPercentValue(item.mfe_15m_pct)} / MAE ${formatPercentValue(item.mae_15m_pct)}</span>
        <em>${escapeHtml(item.recommendation || item.outcome_label || "-")}</em>
      </div>
    `).join("")
    : `<span class="empty">${escapeHtml(emptyText)}</span>`;
}

function renderBuyZeroInlineCounts(id, rows, key, emptyText) {
  const node = document.getElementById(id);
  if (!node) return;
  const items = firstItems(rows || [], 5);
  node.innerHTML = items.length
    ? items.map((item) => `<div>${reasonBadge(item[key] || item.category || "-")} <strong>${escapeHtml(item.count ?? 0)}</strong></div>`).join("")
    : `<span class="empty">${escapeHtml(emptyText)}</span>`;
}

function renderBuyZeroDataQualityCounts(id, payload) {
  const blocks = (payload || {}).data_quality_blocks || {};
  const taxonomy = (payload || {}).data_quality_taxonomy || ((payload || {}).summary || {}).data_quality_taxonomy || {};
  const bucketRows = blocks.buckets || taxonomy.buckets || [];
  const actionRows = blocks.actions || taxonomy.actions || [];
  const reasonRows = blocks.reasons || (payload || {}).top_data_insufficient_reasons || [];
  const rows = [];
  bucketRows.forEach((item) => rows.push({ label: item.bucket || item.reason || "-", count: item.count || 0 }));
  actionRows.forEach((item) => rows.push({ label: item.action || "-", count: item.count || 0 }));
  [
    ["early-small 후보", taxonomy.early_small_candidate_count],
    ["주문 비활성 관찰", taxonomy.early_small_observe_only_count],
    ["소액 주문 허용", taxonomy.early_small_order_enabled_count],
  ].forEach(([label, count]) => {
    if (Number(count || 0) > 0) rows.push({ label, count });
  });
  if (!rows.length) {
    reasonRows.forEach((item) => rows.push({ label: item.reason || "-", count: item.count || 0 }));
  }
  renderBuyZeroInlineCounts(id, rows, "label", "데이터 품질 부족 사유가 없습니다");
}

function renderBuyZeroStageFunnel(rows) {
  const node = document.getElementById("buy-zero-rca-stage-funnel");
  if (!node) return;
  const items = rows || [];
  const visible = items.length ? items : Object.keys(BUY_ZERO_RCA_STAGE_LABELS).map((stage) => ({ stage, total: 0, passed: 0, failed: 0, top_reason: "" }));
  const hasData = visible.some((item) => Number(item.total || 0) > 0);
  text("buy-zero-rca-stage-funnel-status", hasData ? `${visible.reduce((sum, item) => sum + Number(item.total || 0), 0)} events` : "TRACE 대기");
  cls("buy-zero-rca-stage-funnel-status", `counter ${hasData ? "ok" : "muted"}`);
  node.innerHTML = visible.map((item) => {
    const failed = Number(item.failed || 0);
    const tone = failed > 0 ? "warn" : Number(item.total || 0) > 0 ? "ok" : "muted";
    return `
      <button type="button" class="stage-step ${tone}" data-buy-zero-stage="${escapeHtml(item.stage || "")}">
        <span>${escapeHtml(BUY_ZERO_RCA_STAGE_LABELS[item.stage] || item.stage || "-")}</span>
        <strong>${escapeHtml(item.total ?? 0)}</strong>
        <em>${escapeHtml(item.passed ?? 0)} 통과 / ${escapeHtml(failed)} 차단</em>
        <small>${escapeHtml(item.top_reason || "차단 사유 없음")}</small>
      </button>
    `;
  }).join("");
  node.querySelectorAll("[data-buy-zero-stage]").forEach((button) => {
    button.addEventListener("click", () => fetchBuyZeroStageCandidates(button.dataset.buyZeroStage || "").catch((error) => {
      text("buy-zero-rca-stage-funnel-status", `오류: ${error.message}`);
      cls("buy-zero-rca-stage-funnel-status", "counter bad");
    }));
  });
}

async function refreshBuyZeroRcaTables({ force = false } = {}) {
  if (state.buyZeroRca.inFlight) return;
  if (!force && Date.now() - state.buyZeroRca.lastFetchedAt <= BUY_ZERO_RCA_TABLE_REFRESH_MS) return;
  if (!document.getElementById("buy-zero-rca-ready-table-body")) return;
  state.buyZeroRca.inFlight = true;
  text("buy-zero-rca-ready-status", "조회 중");
  text("buy-zero-rca-rally-status", "조회 중");
  try {
    const tradeDate = buyZeroTradeDate();
    const [ready, rally] = await Promise.all([
      apiGet("/api/runtime/buy-zero/ready-not-ordered", { trade_date: tradeDate, limit: 100 }),
      apiGet("/api/runtime/buy-zero/missed-opportunities", { trade_date: tradeDate, limit: 100 }),
    ]);
    state.buyZeroRca.readyRows = ready.items || [];
    state.buyZeroRca.rallyRows = rally.top_observe_then_rally_candidates || [];
    state.buyZeroRca.lastFetchedAt = Date.now();
    renderBuyZeroReadyRows(state.buyZeroRca.readyRows, true);
    renderBuyZeroRallyRows(state.buyZeroRca.rallyRows, true);
  } finally {
    state.buyZeroRca.inFlight = false;
  }
}

async function fetchBuyZeroStageCandidates(stage) {
  if (!stage) return;
  state.buyZeroRca.stage = stage;
  text("buy-zero-rca-stage-funnel-status", `${stage} 조회 중`);
  cls("buy-zero-rca-stage-funnel-status", "counter warn");
  const payload = await apiGet("/api/runtime/buy-zero/traces", {
    trade_date: buyZeroTradeDate(),
    stage,
    pass_fail: "FAIL",
    limit: 100,
  });
  state.buyZeroRca.stageRows = payload.items || [];
  renderBuyZeroStageRows(state.buyZeroRca.stageRows, stage);
  text("buy-zero-rca-stage-funnel-status", `${stage} 차단 ${state.buyZeroRca.stageRows.length}`);
  cls("buy-zero-rca-stage-funnel-status", `counter ${state.buyZeroRca.stageRows.length ? "warn" : "ok"}`);
}

function renderBuyZeroStageRows(rows, stage) {
  const body = document.getElementById("buy-zero-rca-stage-candidates-body");
  if (!body) return;
  if (!stage) {
    body.innerHTML = '<tr><td class="empty" colspan="6">퍼널 단계를 클릭하면 해당 단계에서 막힌 종목을 표시합니다</td></tr>';
    return;
  }
  if (!rows.length) {
    body.innerHTML = `<tr><td class="empty" colspan="6">${escapeHtml(stage)} 단계에서 막힌 trace가 없습니다</td></tr>`;
    return;
  }
  body.innerHTML = firstItems(rows, 50).map((item, index) => `
    <tr class="clickable-row" data-buy-zero-stage-row="${index}">
      <td>${escapeHtml(item.stage || stage)}</td>
      <td>${escapeHtml(`${item.code || "-"} ${item.name || ""}`.trim())}</td>
      <td>${badge(item.stage_status || item.pass_fail || "-")}</td>
      <td>${reasonBadges(item.reason_codes || [item.primary_block_reason].filter(Boolean))}</td>
      <td>${badge(item.gate_status || "-")}</td>
      <td>${compactId(item.candidate_instance_id || item.trace_id || "-")}</td>
    </tr>
  `).join("");
  body.querySelectorAll("[data-buy-zero-stage-row]").forEach((row) => {
    row.addEventListener("click", () => openBuyZeroTraceDetail(state.buyZeroRca.stageRows[Number(row.dataset.buyZeroStageRow)]));
  });
}

function renderBuyZeroReadyRows(rows, traceAvailable = true) {
  const body = document.getElementById("buy-zero-rca-ready-table-body");
  if (!body) return;
  text("buy-zero-rca-ready-status", `${rows.length}건`);
  cls("buy-zero-rca-ready-status", `counter ${rows.length ? "warn" : "muted"}`);
  if (!traceAvailable) {
    body.innerHTML = '<tr><td class="empty" colspan="14">아직 수집된 RCA trace가 없습니다</td></tr>';
    return;
  }
  if (!rows.length) {
    body.innerHTML = '<tr><td class="empty" colspan="14">READY였지만 주문 안 나간 종목이 없습니다</td></tr>';
    return;
  }
  body.innerHTML = firstItems(rows, 100).map((item, index) => {
    const entry = `제출 ${yesNoUnknown(item.entry_plan_submittable)} / 지지 ${yesNoUnknown(item.support_ready)} / ${item.selected_support_source || "-"} ${fmtOptionalNumber(item.selected_support_price, 0)}`;
    return `
      <tr class="clickable-row" data-buy-zero-ready-row="${index}">
        <td>${escapeHtml(`${item.code || "-"} ${item.name || ""}`.trim())}</td>
        <td>${compactId(item.candidate_instance_id || "-")}</td>
        <td>${escapeHtml(item.theme_name || "-")}</td>
        <td>${reasonBadge(item.classification || "-")}</td>
        <td>${badge(item.gate_status || "-")}</td>
        <td>${escapeHtml(fmtOptionalNumber(item.gate_score, 1))}</td>
        <td>${reasonBadge(item.primary_block_reason || "-")}</td>
        <td>${reasonBadges(item.reason_codes || [])}</td>
        <td>${escapeHtml(entry)}</td>
        <td>${badge(item.dry_run_status || "-")} ${escapeHtml(item.dry_run_reason || "")}</td>
        <td>${badge(item.live_sim_status || "-")} ${escapeHtml(item.live_sim_reason || "")}</td>
        <td>${escapeHtml(fmtOptionalNumber(item.latest_tick_age_sec, 1))}s</td>
        <td>${escapeHtml(yesNoUnknown(item.support_ready))}</td>
        <td>${escapeHtml(formatDateTime(item.trace_updated_at || item.created_at))}</td>
      </tr>
    `;
  }).join("");
  body.querySelectorAll("[data-buy-zero-ready-row]").forEach((row) => {
    row.addEventListener("click", () => openBuyZeroTraceDetail(state.buyZeroRca.readyRows[Number(row.dataset.buyZeroReadyRow)]));
  });
}

function renderBuyZeroRallyRows(rows, traceAvailable = true) {
  const body = document.getElementById("buy-zero-rca-rally-table-body");
  if (!body) return;
  text("buy-zero-rca-rally-status", `${rows.length}건`);
  cls("buy-zero-rca-rally-status", `counter ${rows.length ? "warn" : "muted"}`);
  if (!traceAvailable) {
    body.innerHTML = '<tr><td class="empty" colspan="15">아직 수집된 RCA trace가 없습니다</td></tr>';
    return;
  }
  if (!rows.length) {
    body.innerHTML = '<tr><td class="empty" colspan="15">OBSERVE/BLOCKED 이후 급등 후보가 없습니다</td></tr>';
    return;
  }
  body.innerHTML = firstItems(rows, 100).map((item, index) => `
    <tr class="clickable-row" data-buy-zero-rally-row="${index}">
      <td>${escapeHtml(`${item.code || "-"} ${item.name || ""}`.trim())}</td>
      <td>${badge(item.status || "-")}</td>
      <td>${reasonBadge(item.primary_reason || "-")}</td>
      <td>${reasonBadges(item.reason_codes || [])}</td>
      <td>${escapeHtml(fmtOptionalNumber(item.base_price, 0))}</td>
      <td>${escapeHtml(formatPercentValue(item.return_5m_pct))}</td>
      <td>${escapeHtml(formatPercentValue(item.return_15m_pct))}</td>
      <td>${escapeHtml(formatPercentValue(item.return_30m_pct))}</td>
      <td>${escapeHtml(`${formatPercentValue(item.mfe_15m_pct)} / ${formatPercentValue(item.mae_15m_pct)}`)}</td>
      <td>${badge(item.missed_opportunity ? "기회손실" : "아님", item.missed_opportunity ? "bad" : "ok")}</td>
      <td>${badge(item.good_block ? "좋은 차단" : "재검토", item.good_block ? "ok" : "warn")}</td>
      <td>${escapeHtml(item.minutes_to_ready == null ? "-" : `${item.minutes_to_ready}분`)}</td>
      <td>${escapeHtml(item.theme_name || "-")}</td>
      <td>${escapeHtml(item.stock_role || "-")}</td>
      <td>${escapeHtml(item.price_location_status || "-")}</td>
    </tr>
  `).join("");
  body.querySelectorAll("[data-buy-zero-rally-row]").forEach((row) => {
    row.addEventListener("click", () => openBuyZeroTraceDetail(state.buyZeroRca.rallyRows[Number(row.dataset.buyZeroRallyRow)]));
  });
}

async function openBuyZeroTraceDetail(item) {
  if (!item) return;
  const params = {
    trade_date: buyZeroTradeDate(),
    candidate_instance_id: item.candidate_instance_id || "",
    code: item.candidate_instance_id ? "" : item.code || "",
    limit: 200,
  };
  const payload = await apiGet("/api/runtime/buy-zero/traces", params);
  payload.detail_type = "buy_zero_trace";
  payload.selected_candidate = item;
  openDetailPanel(`RCA Trace ${item.code || ""} ${item.name || ""}`.trim(), payload);
}

function buyZeroTraceDetailHtml(payload) {
  const selected = payload.selected_candidate || {};
  const items = [...(payload.items || [])].sort((left, right) => String(left.created_at || "").localeCompare(String(right.created_at || "")));
  const head = `
    <div><span>종목</span><strong>${escapeHtml(`${selected.code || "-"} ${selected.name || ""}`.trim())}</strong></div>
    <div><span>Candidate</span><strong>${compactId(selected.candidate_instance_id || "-")}</strong></div>
    <div><span>분류</span><strong>${escapeHtml(selected.classification || selected.status || "-")}</strong></div>
    <div><span>Trace events</span><strong>${escapeHtml(items.length)}</strong></div>
  `;
  if (!items.length) return `${head}<span class="empty">해당 종목의 RCA trace가 없습니다</span>`;
  const timeline = items.map((item) => `
    <div class="trace-event ${String(item.pass_fail || "").toLowerCase()}">
      <div class="trace-event-head">
        <strong>${escapeHtml(item.stage || "-")}</strong>
        <span>${badge(item.stage_status || item.pass_fail || "-")}</span>
        <time>${escapeHtml(formatDateTime(item.created_at))}</time>
      </div>
      <p>${escapeHtml(buyZeroTraceMessage(item))}</p>
      <div class="trace-event-fields">
        <span>사유 ${reasonBadges(item.reason_codes || [item.primary_block_reason].filter(Boolean))}</span>
        <span>Gate ${escapeHtml(item.gate_status || "-")} / score ${escapeHtml(fmtOptionalNumber(item.gate_score, 1))}</span>
        <span>Theme ${escapeHtml(fmtOptionalNumber(item.theme_score, 1))} / role ${escapeHtml(item.stock_role || "-")}</span>
        <span>위치 ${escapeHtml(item.price_location_status || "-")} / ${escapeHtml(item.price_location_readiness || "-")}</span>
        <span>Tick ${escapeHtml(yesNoUnknown(item.latest_tick_ready))} / age ${escapeHtml(fmtOptionalNumber(item.latest_tick_age_sec, 1))}s</span>
        <span>Support ${escapeHtml(yesNoUnknown(item.support_ready))} / ${escapeHtml(item.selected_support_source || "-")} ${escapeHtml(fmtOptionalNumber(item.selected_support_price, 0))}</span>
        <span>EntryPlan ${compactId(item.entry_plan_id || "-")}</span>
        <span>DRY_RUN ${compactId(item.dry_run_intent_id || "-")} / ${escapeHtml(item.dry_run_status || "-")} ${escapeHtml(item.dry_run_reason || "")}</span>
        <span>LIVE_SIM ${compactId(item.live_sim_intent_id || "-")} / ${escapeHtml(item.live_sim_status || "-")} ${escapeHtml(item.live_sim_reason || "")}</span>
        <span>Command ${compactId(item.command_id || "-")} / Broker ${compactId(item.broker_order_id || "-")}</span>
      </div>
    </div>
  `).join("");
  return `${head}<div id="buy-zero-rca-timeline" class="trace-timeline">${timeline}</div>`;
}

function buyZeroTraceMessage(item) {
  const textValue = [
    item.primary_block_reason,
    item.dry_run_reason,
    item.live_sim_reason,
    ...(item.reason_codes || []),
  ].join(" ").toUpperCase();
  if (/LATEST_TICK|TICK_.*OLD|REALTIME_RELIABILITY/.test(textValue)) return "실시간 틱이 오래되어 주문 보류";
  if (/SUPPORT_NOT_READY|SUPPORT/.test(textValue) && item.entry_plan_diagnostic_only) return "지지선 미확정으로 진단 전용";
  if (/LIVE_SIM|ACCOUNT_GUARD|EXIT_GUARD|KILL_SWITCH/.test(textValue)) return "LIVE_SIM guard에서 차단";
  if (/GATE_RESULT_KEY_MISMATCH|CANDIDATE_STATE/.test(textValue)) return "READY였지만 candidate state/gate_result_key 불일치";
  if (/DUPLICATE/.test(textValue)) return "중복 주문 방지로 차단";
  if (/CHASE|LATE_CHASE|VWAP_OVEREXTENDED/.test(textValue)) return "추격 위험으로 OBSERVE";
  if (String(item.pass_fail || "").toUpperCase() === "PASS") return "이 단계는 통과";
  return item.primary_block_reason || item.stage_status || "trace event";
}

function render(snapshot) {
  state.latestSnapshot = snapshot;
  const core = snapshot.core || {};
  const gateway = snapshot.gateway || {};
  const commands = snapshot.commands || {};
  const transport = snapshot.transport || {};
  const transportExperiment = snapshot.transport_experiment || {};
  const runtime = snapshot.runtime || {};
  const dryRunOrders = snapshot.dry_run_orders || runtime.dry_run_orders || { summary: {} };
  const intradayDecisions = snapshot.intraday_decisions || runtime.intraday_decisions || { funnel: {} };
  const intradayOutcomes = snapshot.intraday_outcomes || runtime.intraday_outcomes || {};
  const shadowStrategies = snapshot.shadow_strategies || runtime.shadow_strategies || {};
  const dryRunPerformance = snapshot.dry_run_performance || runtime.dry_run_performance || {};
  const thresholdAB = snapshot.threshold_ab || runtime.threshold_ab || { summary: {}, recommendations: [] };
  const strategyReplay = snapshot.strategy_replay || runtime.strategy_replay || { summary: {}, funnel: {}, shadow_ranking: [], data_quality: [], diff_summary: {} };
  const changeProposals = snapshot.change_proposals || runtime.change_proposals || { summary: {}, top_recommendations: [] };
  const candidates = snapshot.candidates || { summary: {}, items: [] };
  const themes = snapshot.themes || { summary: {}, items: [] };
  const orders = snapshot.orders || { summary: {}, order_results: [], executions: [] };
  const reviews = snapshot.reviews || { summary: {}, items: [] };
  const logs = snapshot.logs || { core: [], gateway: [], warnings: [] };
  const opsAlerts = snapshot.ops_alerts || { summary: {}, alerts: [] };
  const themeLab = snapshot.theme_lab || {};

  renderDashboardV2(snapshot);
  text("snapshot-time", snapshot.timestamp || "대기 중");
  text("core-mode", core.mode || "OBSERVE");
  cls("core-mode", `pill ${core.mode === "LIVE" ? "warn" : core.mode === "DRY_RUN" ? "ok" : "muted"}`);
  const coreHealthy = Boolean(core.service || core.running || (runtime.lightweight_status || {}).running);
  text("core-state", coreHealthy ? "정상" : "대기");
  cls("core-state", `pill ${coreHealthy ? "ok" : "warn"}`);
  const gatewayState = gateway.connection_state || "DISCONNECTED";
  text("gateway-state", gatewayState);
  text("gateway-connection", gatewayState);
  text("kiwoom-login", yesNo(gateway.kiwoom_logged_in));
  text("heartbeat-age", gateway.heartbeat_age_sec == null ? "-" : `${fmtNumber(gateway.heartbeat_age_sec, 0)}s`);
  text("orderable-state", gateway.orderable ? "주문 가능" : "주문 차단");
  cls("gateway-state", `pill ${gateway.heartbeat_ok ? "ok" : gateway.connected ? "warn" : "bad"}`);
  cls("orderable-state", `pill ${gateway.orderable ? "ok" : "muted"}`);
  renderOpsAlerts(opsAlerts);
  renderLiveSimPreflight(snapshot);
  renderLiveSimCanary(snapshot);
  renderThemeLabSummary(themeLab);
  renderLiveSimAudit(snapshot);
  renderLiveSimCanaryPerformance(snapshot);
  renderExitPolicyValidation(snapshot);
  renderBuyZeroRca(snapshot);
  renderConservativeReasonOutcomes(snapshot);
  renderShadowSmallEntryPromotion(snapshot);
  renderShadowSmallEntryOps(snapshot);
  renderShadowSmallEntryPilot(snapshot);

  text("transport-mode", transport.mode || "rest_long_poll");
  text("transport-event-p95", formatMs(transport.event_latency_p95_ms));
  text("transport-command-p95", formatMs(transport.command_latency_p95_ms));
  text("transport-ack-p95", formatMs(transport.ack_latency_p95_ms));
  text("transport-longpoll-p95", formatMs(transport.long_poll_wait_p95_ms));
  text("transport-execute-p95", formatMs(transport.gateway_execute_p95_ms));
  text("transport-rate-p95", formatMs(transport.rate_limit_wait_p95_ms));
  text("transport-empty-rate", formatRate(transport.empty_poll_rate || 0));
  text("transport-errors", transport.transport_error_count || 0);
  text("transport-reconnect", transport.reconnect_count || 0);
  text("transport-ws-decision", transport.websocket_recommendation || "KEEP_REST_LONG_POLL");
  const realPilot = transport.real_gateway_websocket_pilot || {};
  text("transport-real-pilot-state", realPilot.enabled ? `${realPilot.connected ? "연결됨" : "미연결"} / ${realPilot.state || "-"}` : "비활성");
  text("transport-real-pilot-session", compactPlain(realPilot.ws_session_id || "-"));
  text("transport-real-pilot-reconnect", realPilot.reconnect_count || 0);
  text("transport-real-pilot-blocked-orders", realPilot.blocked_order_command_count || 0);
  text("transport-real-pilot-session-loss", realPilot.session_loss_count || 0);
  text("transport-real-pilot-duplicate-ack", realPilot.duplicate_ack_count || 0);
  text("transport-real-pilot-unknown-ack", realPilot.unknown_ack_count || 0);
  text("transport-real-pilot-fallback", realPilot.fallback_reason || realPilot.fallback_state || "-");
  text("transport-real-pilot-price-sample-rate", formatRate(realPilot.price_tick_sample_rate || 0));
  text("transport-real-pilot-price-sampled", realPilot.price_tick_sampled_count || 0);
  text("transport-real-pilot-price-fallback", realPilot.price_tick_fallback_count || 0);
  text("transport-real-pilot-event-fallback", realPilot.event_fallback_count || 0);
  text("transport-real-pilot-last-event", formatDateTime(realPilot.last_ws_event_at || ""));
  text("transport-real-pilot-last-ack", formatDateTime(realPilot.last_ws_ack_at || ""));
  const transportWarnings = [transport.websocket_recommendation_reason || "", ...((transport.warning_flags || []).map((flag) => `경고: ${flag}`))].filter(Boolean);
  const transportNode = document.getElementById("transport-warning-lines");
  if (transportNode) transportNode.innerHTML = transportWarnings.length ? transportWarnings.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">전송 상태가 안정적입니다</span>';

  text("transport-exp-id", transportExperiment.latest_experiment_id || "-");
  text("transport-exp-scenario", transportExperiment.latest_scenario || "-");
  text("transport-exp-decision", transportExperiment.recommendation || "NO_EXPERIMENT");
  text("transport-exp-rest-cmd", formatMs(transportExperiment.rest_command_p95_ms));
  text("transport-exp-ws-cmd", formatMs(transportExperiment.websocket_command_p95_ms));
  text("transport-exp-cmd-delta", formatMs(transportExperiment.command_p95_delta_ms));
  text("transport-exp-rest-ack", formatMs(transportExperiment.rest_ack_p95_ms));
  text("transport-exp-ws-ack", formatMs(transportExperiment.websocket_ack_p95_ms));
  text("transport-exp-ack-delta", formatMs(transportExperiment.ack_p95_delta_ms));
  text("transport-exp-ready", yesNo(transportExperiment.real_gateway_switch_ready));
  const expBlockerNode = document.getElementById("transport-exp-blockers");
  if (expBlockerNode) {
    const blockers = transportExperiment.blockers || [];
    expBlockerNode.innerHTML = blockers.length ? blockers.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">최근 Mock 비교에서 확인된 차단 요인이 없습니다</span>';
  }

  text("runtime-enabled", yesNo(runtime.enabled));
  text("runtime-running", yesNo(runtime.running));
  text("runtime-mode", runtime.mode || "OBSERVE");
  text("runtime-last-cycle", runtime.last_cycle_at || "-");
  text("runtime-cycle-count", runtime.cycle_count || 0);
  text("runtime-failed-count", runtime.failed_cycle_count || 0);
  text("runtime-skipped-count", runtime.skipped_cycle_count || 0);
  text("runtime-duration", `${runtime.last_cycle_duration_ms || 0}ms`);
  text("runtime-active-candidates", runtime.active_candidate_count || 0);
  text("runtime-gate-results", runtime.gate_result_count || 0);
  text("runtime-virtual-orders", runtime.virtual_order_count || 0);
  text("runtime-reviews", runtime.review_count || 0);
  const runtimeWarnings = [runtime.last_error ? `오류: ${runtime.last_error}` : "", ...(runtime.warnings || [])].filter(Boolean);
  const runtimeNode = document.getElementById("runtime-warning-lines");
  if (runtimeNode) runtimeNode.innerHTML = runtimeWarnings.length ? runtimeWarnings.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">Runtime 경고가 없습니다</span>';

  const decisionFunnel = intradayDecisions.funnel || {};
  text("decision-ledger-event-count", `${intradayDecisions.event_count || 0} events`);
  text("decision-funnel-detected", decisionFunnel.detected || 0);
  text("decision-funnel-evaluated", decisionFunnel.evaluated || 0);
  text("decision-funnel-ready", decisionFunnel.ready || 0);
  text("decision-funnel-wait", decisionFunnel.wait || 0);
  text("decision-funnel-blocked", decisionFunnel.blocked || 0);
  text("decision-funnel-entry-plan", decisionFunnel.entry_plan || 0);
  text("decision-funnel-order-intent", decisionFunnel.order_intent || 0);
  text("decision-funnel-open-position", decisionFunnel.open_position || 0);
  text("decision-funnel-exit-decision", decisionFunnel.exit_decision || 0);
  text("decision-ready-without-order", intradayDecisions.ready_without_order_count || 0);
  text("decision-order-rejected", intradayDecisions.order_rejected_count || 0);
  renderInlineCounts("decision-block-reason-lines", intradayDecisions.top_block_reasons || [], "reason", "BLOCK reason이 없습니다");
  renderInlineCounts("decision-wait-reason-lines", intradayDecisions.top_wait_reasons || [], "reason", "WAIT reason이 없습니다");
  renderInlineCounts("decision-data-quality-lines", [
    ...((intradayDecisions.major_reason_distribution || []).map((item) => ({ ...item, reason: `major:${item.reason}` }))),
    ...((intradayDecisions.top_data_quality_issues || []).map((item) => ({ ...item, reason: `data:${item.reason}` }))),
  ], "reason", "데이터 품질 이슈가 없습니다");

  text("outcome-labeled-count", intradayOutcomes.labeled_count || 0);
  text("outcome-insufficient-count", intradayOutcomes.insufficient_count || 0);
  text("outcome-early-tp-count", intradayOutcomes.early_true_positive_count || 0);
  text("outcome-early-fp-count", intradayOutcomes.early_false_positive_count || 0);
  text("outcome-opportunity-loss-count", intradayOutcomes.wait_block_opportunity_loss_count || 0);
  text("outcome-risk-effective-count", (intradayOutcomes.by_label || {}).RISK_BLOCK_EFFECTIVE || 0);
  text("outcome-exit-late-count", intradayOutcomes.exit_too_late_count || 0);
  text("outcome-exit-early-count", intradayOutcomes.exit_too_early_count || 0);
  renderInlineCounts(
    "outcome-label-lines",
    Object.entries(intradayOutcomes.by_label || {}).map(([label, count]) => ({ label, count })),
    "label",
    "Outcome label이 없습니다"
  );
  renderInlineCounts("outcome-quality-lines", intradayOutcomes.data_quality_issues || [], "reason", "Outcome data quality 이슈가 없습니다");

  text("shadow-evaluation-total", shadowStrategies.total_evaluations || 0);
  text("shadow-changed-count", shadowStrategies.changed_decision_count || 0);
  text("shadow-baseline-ready", shadowStrategies.baseline_ready_count || 0);
  text("shadow-ready", shadowStrategies.shadow_ready_count || 0);
  text("shadow-ready-delta", shadowStrategies.ready_delta || 0);
  text("shadow-opportunity-reduced", shadowStrategies.estimated_opportunity_loss_reduced_count || 0);
  text("shadow-fp-increase", shadowStrategies.estimated_false_positive_increase_count || 0);
  text("shadow-risk-effective", shadowStrategies.estimated_risk_block_effective_count || 0);
  text("shadow-exit-late-reduced", shadowStrategies.estimated_exit_too_late_reduced_count || 0);
  renderInlineCounts(
    "shadow-change-lines",
    Object.entries(shadowStrategies.by_change_type || {}).map(([reason, count]) => ({ reason, count })),
    "reason",
    "Shadow change가 없습니다"
  );
  renderShadowPolicyRanking("shadow-policy-ranking-lines", shadowStrategies.policy_ranking || []);

  const drySummary = dryRunOrders.summary || {};
  text("dryrun-order-policy", runtime.dry_run_order_sink_enabled ? runtime.dry_run_order_policy || "enabled" : runtime.dry_run_order_policy || "disabled");
  text("dryrun-order-total", drySummary.total ?? runtime.dry_run_order_intent_count ?? 0);
  text("dryrun-order-entry-buy", `${drySummary.entry_total ?? runtime.dry_run_entry_order_intent_count ?? 0} / ${drySummary.buy_total ?? 0}`);
  text("dryrun-order-exit-sell", `${drySummary.exit_total ?? runtime.dry_run_exit_order_intent_count ?? 0} / ${drySummary.sell_total ?? runtime.dry_run_sell_order_intent_count ?? 0}`);
  text("dryrun-order-accepted", drySummary.accepted ?? runtime.dry_run_order_accepted_count ?? 0);
  text("dryrun-order-rejected", drySummary.rejected ?? runtime.dry_run_order_rejected_count ?? 0);
  text("dryrun-order-duplicate", drySummary.duplicate ?? runtime.dry_run_order_duplicate_count ?? 0);
  text("dryrun-sell-accepted", drySummary.exit_accepted ?? runtime.dry_run_exit_accepted_count ?? 0);
  text("dryrun-sell-rejected", drySummary.exit_rejected ?? runtime.dry_run_exit_rejected_count ?? 0);
  text("dryrun-sell-duplicate", drySummary.exit_duplicate ?? runtime.dry_run_exit_duplicate_count ?? 0);
  text("dryrun-order-live-pass", drySummary.live_would_pass ?? runtime.dry_run_order_live_would_pass_count ?? 0);
  text("dryrun-order-live-reject", drySummary.live_would_reject ?? runtime.dry_run_order_live_would_reject_count ?? 0);
  renderInlineCounts("dryrun-reject-reasons", drySummary.top_reject_reasons || [], "reason", "거부 사유가 없습니다");
  renderInlineCounts("dryrun-exit-type-lines", drySummary.exit_by_decision_type || [], "decision_type", "청산 판단 유형이 없습니다");

  text("dryrun-performance-generated", dryRunPerformance.generated_at || "-");
  text("dryrun-perf-total", dryRunPerformance.total_lifecycle_count || 0);
  text("dryrun-perf-completed", dryRunPerformance.completed_lifecycle_count || 0);
  text("dryrun-perf-win-rate", formatRate(dryRunPerformance.win_rate));
  text("dryrun-perf-avg-return", formatPercentValue(dryRunPerformance.avg_realized_return_pct));
  text("dryrun-perf-net-expectancy", formatPercentValue(dryRunPerformance.net_expectancy));
  text("dryrun-perf-net-win-rate", formatRate(dryRunPerformance.net_win_rate));
  text("dryrun-perf-fp", dryRunPerformance.false_positive_count || 0);
  text("dryrun-perf-fn", dryRunPerformance.false_negative_count || 0);
  text("dryrun-perf-opp", dryRunPerformance.opportunity_loss_count || 0);
  text("dryrun-perf-net-bad-ready", `${dryRunPerformance.cost_adjusted_bad_ready_count || 0} / ${formatRate(dryRunPerformance.cost_adjusted_bad_ready_rate)}`);
  text("dryrun-perf-net-opp", `${dryRunPerformance.cost_adjusted_opportunity_loss_count || 0} / ${formatRate(dryRunPerformance.cost_adjusted_opportunity_loss_rate)}`);
  const goNoGo = dryRunPerformance.go_no_go || {};
  const realism = dryRunPerformance.execution_realism || {};
  text("dryrun-perf-go-nogo", goNoGo.decision || "NO_GO");
  text("dryrun-perf-limit-hit", formatRate(realism.limit_price_hit_rate));
  text("dryrun-perf-stale-tick", `${realism.stale_tick_count || 0} / ${formatRate(realism.stale_tick_rate)}`);
  text("dryrun-perf-latency-risk", `${realism.gateway_latency_high_count || 0} / ${formatRate(realism.gateway_latency_high_rate)}`);
  text("dryrun-perf-live-pass-win", formatRate(dryRunPerformance.live_would_pass_win_rate));
  renderInlineCounts(
    "dryrun-perf-go-nogo-lines",
    (goNoGo.criteria || []).map((item) => ({ reason: `${item.passed ? "PASS" : "FAIL"} ${item.code} (${item.value ?? "-"} / ${item.threshold || "-"})`, count: "" })),
    "reason",
    "Go/No-Go 기준 데이터가 없습니다"
  );
  renderInlineCounts(
    "dryrun-perf-cost-lines",
    (dryRunPerformance.cost_scenario_expectancy || []).slice(0, 8).map((item) => ({
      reason: `${item.slippage_bp}bp ${item.entry_delay_sec}s net ${formatPercentValue(item.net_expectancy)} win ${formatRate(item.net_win_rate)}`,
      count: item.sample_count || 0,
    })),
    "reason",
    "비용/지연 시나리오 표본이 없습니다"
  );
  renderInlineCounts(
    "dryrun-perf-realism-lines",
    [
      { reason: "partial_fill_high", count: realism.partial_fill_high_risk_count || 0 },
      { reason: "spread_high", count: realism.spread_high_risk_count || 0 },
      { reason: "stale_tick", count: realism.stale_tick_count || 0 },
      { reason: "latency_high", count: realism.gateway_latency_high_count || 0 },
    ],
    "reason",
    "체결 현실성 경고가 없습니다"
  );
  renderInlineCounts("dryrun-perf-fp-lines", dryRunPerformance.top_false_positive_types || [], "type", "오탐 유형이 없습니다");
  renderInlineCounts("dryrun-perf-fn-lines", dryRunPerformance.top_false_negative_types || [], "type", "미탐 유형이 없습니다");
  renderInlineCounts("dryrun-perf-reject-rally-lines", dryRunPerformance.top_reject_reasons_with_rally || [], "reason", "상승을 놓친 거부 사유가 없습니다");

  const thresholdSummary = thresholdAB.summary || {};
  text("threshold-ab-report-id", thresholdAB.report_id || "실제 적용 아님");
  text("threshold-ab-total", thresholdSummary.candidate_count || 0);
  text("threshold-ab-strong", thresholdSummary.strong_candidate_count || 0);
  text("threshold-ab-watch", thresholdSummary.watch_candidate_count || 0);
  text("threshold-ab-risky", thresholdSummary.risky_candidate_count || 0);
  text("threshold-ab-insufficient", thresholdSummary.data_insufficient_count || 0);
  text("threshold-ab-fp-reduction", thresholdSummary.total_avoided_false_positive_count || 0);
  text("threshold-ab-fn-increase", thresholdSummary.total_new_false_negative_count || 0);
  text("threshold-ab-opp-delta", thresholdSummary.total_opportunity_loss_delta || 0);
  renderThresholdRecommendations("threshold-ab-recommendations", thresholdAB.recommendations || []);

  const changeSummary = changeProposals.summary || {};
  const byStatus = changeSummary.by_status || {};
  const byGrade = changeSummary.by_grade || {};
  text("change-proposal-total", changeSummary.total_count || 0);
  text("change-proposal-review-ready", byStatus.REVIEW_READY || 0);
  text("change-proposal-strong", byGrade.STRONG_CANDIDATE || 0);
  text("change-proposal-watch", byGrade.WATCH_CANDIDATE || 0);
  text("change-proposal-risky", byGrade.RISKY_CANDIDATE || 0);
  text("change-proposal-insufficient", byGrade.DATA_INSUFFICIENT || 0);
  text("change-proposal-pending", (byStatus.REVIEW_READY || 0) + (byStatus.DRAFT || 0));
  text("change-proposal-expiring", changeSummary.expiring_soon_count || 0);
  renderRows("change-proposal-top-rows", firstItems(changeSummary.top_recommendations || [], 5).map((item) => rowHtml([
    item.recommendation_grade || "-",
    item.status || "-",
    item.category || "-",
    item.title || "-",
    fmtNumber(item.net_benefit_score, 2),
    item.guardrail_passed ? "PASS" : (item.blocked_by_guardrail_reason || "BLOCKED"),
  ])), 6);

  const replaySummary = strategyReplay.summary || {};
  const replayFunnel = strategyReplay.funnel || replaySummary.funnel || {};
  const replayLatest = strategyReplay.latest || {};
  text("strategy-replay-latest-id", replayLatest.replay_id || replaySummary.replay_id || "-");
  text("strategy-replay-status", replaySummary.status || replayLatest.status || "-");
  text("strategy-replay-ticks", replaySummary.processed_tick_count || 0);
  text("strategy-replay-candidates", replaySummary.candidate_count || 0);
  text("strategy-replay-ready", replaySummary.ready_count || 0);
  text("strategy-replay-orders", replaySummary.order_intent_count || 0);
  text("strategy-replay-outcomes", replaySummary.outcome_labeled_count || 0);
  text("strategy-replay-shadow", replaySummary.shadow_evaluation_count || 0);
  text("strategy-replay-funnel", [
    replayFunnel.candidate_detected || 0,
    replayFunnel.gate_evaluated || 0,
    replayFunnel.ready || 0,
    replayFunnel.order_intent_created || 0,
    replayFunnel.position_opened || 0,
    replayFunnel.position_closed || 0,
    replayFunnel.outcome_labeled || 0,
  ].join(" -> "));
  renderRows("strategy-replay-ranking-rows", firstItems(strategyReplay.shadow_ranking || [], 8).map((item) => rowHtml([
    item.policy_name || item.policy_id || "-",
    item.ready_delta ?? 0,
    item.estimated_opportunity_loss_reduced_count ?? 0,
    item.estimated_false_positive_increase_count ?? 0,
    item.net_benefit_score ?? 0,
    item.recommendation_grade || "-",
  ])), 6);
  const replayWarnings = replaySummary.warnings || strategyReplay.data_quality || [];
  const replayWarningNode = document.getElementById("strategy-replay-quality-lines");
  if (replayWarningNode) replayWarningNode.innerHTML = replayWarnings.length ? replayWarnings.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">Replay data quality warnings are empty</span>';
  const replayDiff = strategyReplay.diff_summary || {};
  const replayDiffNode = document.getElementById("strategy-replay-diff-lines");
  if (replayDiffNode) {
    const lines = [
      `status: ${replayDiff.status || "NO_REPORT"}`,
      `diff_count: ${replayDiff.diff_count || 0}`,
      ...(replayDiff.notes || []),
    ];
    replayDiffNode.innerHTML = lines.map((line) => `<div>${escapeHtml(line)}</div>`).join("");
  }

  text("command-queued", commands.queued_count || 0);
  text("command-dispatched", commands.dispatched_count || 0);
  text("command-acked", commands.acked_count || 0);
  text("command-failed", commands.failed_count || 0);
  text("command-expired", commands.expired_count || 0);
  text("command-duplicate", commands.duplicate_rejected_count || 0);
  text("command-rate-limited", commands.rate_limited_count || 0);
  text("command-stale", commands.stale_dispatched_count || 0);
  text("command-last-order", commands.last_order_command_at || "-");

  text("candidate-total", candidates.summary.total || 0);
  text("candidate-ready", candidates.summary.ready || 0);
  text("candidate-blocked", candidates.summary.blocked || 0);
  text("candidate-wait", candidates.summary.wait || 0);
  renderRows("candidate-rows", firstItems(candidates.items, 20).map((item) => rowHtml([`${item.code} ${item.name || ""}`, item.display_state || item.state, item.theme_id || "-", `${fmtNumber(item.hybrid_score)} / T ${fmtNumber(item.theme_score)} / M ${fmtNumber(item.membership_score)}`, (item.reason_codes || []).join(", ")])), 5);

  text("theme-active", themes.summary.active || 0);
  text("theme-watch", themes.summary.watch || 0);
  text("top-theme", themes.summary.top_theme || "-");
  text("theme-top-score", fmtNumber(themes.summary.top_theme_score || 0));
  renderRows("theme-rows", firstItems(themes.items, 20).map((item) => rowHtml([item.rank || "-", item.theme_name || item.theme_id || "-", fmtNumber(item.theme_score), fmtNumber(item.breadth), fmtNumber(item.leader_gap), fmtNumber(item.top3_concentration)])), 6);

  const orderRows = [];
  for (const item of firstItems(orders.executions || [], 20)) orderRows.push(rowHtml([item.created_at, item.code, item.side, item.filled_quantity, item.price, `잔량 ${item.remaining_quantity}`]));
  for (const item of firstItems(orders.order_results || [], Math.max(0, 30 - orderRows.length))) {
    const request = item.request || {};
    orderRows.push(rowHtml([item.created_at, request.code || "-", request.side || "-", request.quantity || 0, request.price || 0, item.ok ? "OK" : item.message]));
  }
  text("order-count", orders.summary.execution_count || orders.summary.order_result_count || 0);
  renderRows("order-rows", orderRows, 6);

  text("review-count", reviews.summary.total || 0);
  renderRows("review-rows", firstItems(reviews.items, 30).map((item) => rowHtml([item.code || item.candidate_id || "-", item.final_status || "-", fmtNumber(item.max_return_5m || 0), fmtNumber(item.max_return_10m || 0), fmtNumber(item.max_return_20m || 0), [item.false_positive_flag ? "false_positive" : "", item.false_negative_flag ? "false_negative" : "", item.blocked_but_later_rallied ? "blocked_rallied" : "", item.expired_but_later_rallied ? "expired_rallied" : ""].filter(Boolean).join(", ")])), 6);

  const logLines = (logs.items || []).length
    ? firstItems(logs.items || [], 100).map((item) => item.line || `${item.timestamp || ""} ${item.type || ""}`.trim())
    : [...firstItems(logs.core || [], 80), ...firstItems(logs.gateway || [], 20).map((item) => `${item.timestamp} ${item.type}`)];
  text("log-count", logLines.length);
  const logNode = document.getElementById("log-lines");
  if (logNode) {
    const staleText = logs.stale_core_log_count ? ` <span class="muted">(${escapeHtml(logs.stale_core_log_count)}개 이전 로그 숨김)</span>` : "";
    logNode.innerHTML = logLines.length ? logLines.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : `<span class="empty">최근 로그가 없습니다${staleText}</span>`;
  }
}

function compactPlain(value) {
  const textValue = String(value || "-");
  if (textValue.length <= 24) return textValue;
  return `${textValue.substring(0, 10)}...${textValue.substring(textValue.length - 8)}`;
}

function normalizeSnapshotEnvelope(payload) {
  const raw = payload || {};
  const directV2 = raw.schema_version === DASHBOARD_V2_SCHEMA_VERSION ? raw : null;
  const nestedV2 = raw.dashboard_v2 || null;
  const snapshot = directV2 || nestedV2 || {};
  const readModel = snapshot.read_model || (raw.runtime || {}).read_model || {};
  if (directV2) {
    return {
      wrapper: {
        snapshot_detail: "slim",
        timestamp: snapshot.generated_at || readModel.snapshot_at || "",
        runtime: { read_model: readModel },
        dashboard_v2: snapshot,
      },
      snapshot,
      readModel,
    };
  }
  return { wrapper: raw, snapshot, readModel };
}

function snapshotIdentity(snapshot) {
  const payload = snapshot || {};
  const readModel = payload.read_model || {};
  return {
    namespace: String(readModel.snapshot_namespace || payload.snapshot_namespace || readModel.view_name || payload.view_name || ""),
    schemaVersion: String(payload.schema_version || ""),
    generation: numberOrZero(readModel.generation || payload.generation),
    sourceCycle: numberOrZero(readModel.source_runtime_cycle_count || payload.source_runtime_cycle_count),
    sourceEpoch: String(readModel.source_epoch_id || readModel.boot_id || payload.source_epoch_id || payload.boot_id || ""),
    snapshotAt: String(readModel.snapshot_at || payload.generated_at || payload.snapshot_at || ""),
    checksum: String(readModel.checksum || payload.checksum || ""),
    sourceKind: String(readModel.source_kind || readModel.source || payload.source_kind || ""),
    stale: Boolean(readModel.stale),
    fallbackUsed: Boolean(readModel.fallback_used),
  };
}

function compareSnapshotOrder(current, incoming) {
  if (!incoming.schemaVersion) return { order: -1, reason: "SNAPSHOT_SCHEMA_MISSING" };
  if (incoming.schemaVersion !== DASHBOARD_V2_SCHEMA_VERSION) return { order: -1, reason: "SCHEMA_MISMATCH" };
  if (incoming.namespace && incoming.namespace !== DASHBOARD_V2_NAMESPACE) return { order: -1, reason: "NAMESPACE_MISMATCH" };
  if (!current || !current.schemaVersion) return { order: 1, reason: "FIRST_ACCEPTED" };
  if (incoming.generation < current.generation) return { order: -1, reason: "STALE_GENERATION_REJECTED" };
  if (incoming.generation > current.generation) return { order: 1, reason: "NEWER_GENERATION" };
  if (incoming.sourceCycle < current.sourceCycle) return { order: -1, reason: "STALE_RUNTIME_CYCLE_REJECTED" };
  if (incoming.sourceCycle > current.sourceCycle) return { order: 1, reason: "NEWER_RUNTIME_CYCLE" };
  if (incoming.checksum && incoming.checksum === current.checksum) return { order: 0, reason: "DUPLICATE_CHECKSUM" };
  if (incoming.snapshotAt && current.snapshotAt && incoming.snapshotAt < current.snapshotAt) {
    return { order: -1, reason: "STALE_SNAPSHOT_AT_REJECTED" };
  }
  if (incoming.checksum && current.checksum && incoming.checksum !== current.checksum && incoming.generation === current.generation) {
    return { order: -1, reason: "SAME_GENERATION_CHECKSUM_CONFLICT" };
  }
  return { order: 1, reason: "NEWER_OR_UNCHECKED" };
}

function shouldAcceptSnapshot(current, incoming) {
  const decision = compareSnapshotOrder(current, incoming);
  return { accept: decision.order > 0, duplicate: decision.order === 0, reason: decision.reason };
}

function applySnapshotIfNewer(snapshot, transport) {
  const normalized = normalizeSnapshotEnvelope(snapshot);
  const incoming = snapshotIdentity(normalized.snapshot);
  const decision = shouldAcceptSnapshot(state.dashboardV2Snapshot.identity, incoming);
  if (decision.duplicate) {
    state.dashboardV2Snapshot.duplicateSnapshotCount += 1;
    state.dashboardV2Snapshot.lastRejectReason = decision.reason;
    return false;
  }
  if (!decision.accept) {
    state.dashboardV2Snapshot.rejectedSnapshotCount += 1;
    state.dashboardV2Snapshot.lastRejectReason = decision.reason;
    return false;
  }
  state.dashboardV2Snapshot.identity = { ...incoming, transport };
  state.dashboardV2Snapshot.lastRejectReason = "";
  state.lastSnapshotAt = Date.now();
  render(normalized.wrapper);
  return true;
}

function numberOrZero(value) {
  const parsed = Number(value || 0);
  return Number.isFinite(parsed) ? parsed : 0;
}

async function pollSnapshot() {
  if (state.pollInFlight) return;
  const pollSeq = state.pollSeq + 1;
  state.pollSeq = pollSeq;
  const controller = new AbortController();
  state.pollController = controller;
  state.pollInFlight = true;
  try {
    const response = await fetch("/api/snapshot?view=v2", { signal: controller.signal });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const snapshot = await response.json();
    if (pollSeq !== state.pollSeq) return;
    applySnapshotIfNewer(snapshot, "poll");
  } finally {
    if (state.pollController === controller) state.pollController = null;
    state.pollInFlight = false;
  }
}

async function shadowSmallEntryOpsAction(action, button) {
  const originalText = button?.textContent || "";
  const body = { operator: "dashboard", note: `dashboard ${action}` };
  if (action === "confirm") {
    const token = state.shadowSmallEntryOps.activationToken || window.prompt("Shadow Small Entry activation token") || "";
    body.activation_token = token;
    body.note = window.prompt("operator note") || "dashboard confirm";
  }
  if (action === "pause") {
    body.reason = "SHADOW_SMALL_ENTRY_OPERATOR_PAUSE";
  }
  if (action === "emergency-pause") {
    body.emergency = true;
    body.reason = "SHADOW_SMALL_ENTRY_EMERGENCY_PAUSE";
    body.note = "dashboard emergency pause";
  }
  const endpoint = SHADOW_SMALL_ENTRY_OPS_ENDPOINTS[action] || SHADOW_SMALL_ENTRY_OPS_ENDPOINTS.preflight;
  if (button) {
    button.disabled = true;
    button.textContent = "running";
  }
  try {
    const payload = await runWithLocalTokenRetry((token) => postWithLocalToken(endpoint, token, body));
    if (!payload) return;
    if (payload.activation_token) {
      state.shadowSmallEntryOps.activationToken = payload.activation_token;
      state.shadowSmallEntryOps.activationTokenId = payload.activation_token_id || "";
      openDetailPanel("Shadow Small Entry arm token", {
        activation_token: payload.activation_token,
        activation_token_id: payload.activation_token_id,
        activation_expires_at: payload.activation_expires_at,
        note: "Confirm 전용 토큰입니다. 로컬 화면 세션에만 보관됩니다.",
      });
    } else if (action === "confirm") {
      state.shadowSmallEntryOps.activationToken = "";
      state.shadowSmallEntryOps.activationTokenId = "";
    }
    await pollSnapshot();
  } catch (error) {
    openDetailPanel("Shadow Small Entry 운영 제어 오류", { action, error: error.message });
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

async function shadowSmallEntryPilotAction(action, button) {
  const originalText = button?.textContent || "";
  const endpoint = SHADOW_SMALL_ENTRY_PILOT_ENDPOINTS[action] || SHADOW_SMALL_ENTRY_PILOT_ENDPOINTS.report;
  const body = { operator: "dashboard", note: `dashboard ${action}` };
  if (action === "generate-report") {
    body.export = true;
    body.format = "all";
  }
  if (button) {
    button.disabled = true;
    button.textContent = "running";
  }
  try {
    const payload = await runWithLocalTokenRetry((token) => postWithLocalToken(endpoint, token, body));
    if (!payload) return;
    state.shadowSmallEntryPilot.report = payload.report || payload;
    state.shadowSmallEntryPilot.lastFetchedAt = Date.now();
    await pollSnapshot();
  } catch (error) {
    openDetailPanel("Shadow Small Entry Pilot 오류", { action, error: error.message });
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

async function runtimeCommand(action, button) {
  const originalText = button?.textContent || "";
  if (button) {
    button.disabled = true;
    button.textContent = "실행 중";
  }
  try {
    const payload = await runWithLocalTokenRetry((token) => postWithLocalToken(`/api/runtime/${action}`, token));
    if (!payload) return;
    await pollSnapshot();
  } catch (error) {
    openDetailPanel("Runtime 작업 오류", { action, error: error.message });
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

function stopPolling() {
  if (!state.pollTimer) return;
  clearInterval(state.pollTimer);
  state.pollTimer = null;
}

function startPolling({ immediate = false } = {}) {
  if (state.pollTimer) return;
  if (immediate && !state.wsConnected) pollSnapshot().catch(() => {});
  state.pollTimer = setInterval(() => {
    if (!state.wsConnected) pollSnapshot().catch(() => {});
  }, SNAPSHOT_POLL_INTERVAL_MS);
}

function connectWebSocket() {
  if (state.ws && [WebSocket.CONNECTING, WebSocket.OPEN].includes(state.ws.readyState)) return;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${protocol}//${window.location.host}/ws/dashboard`);
  state.ws = ws;
  ws.onopen = () => {
    state.wsConnected = true;
    if (state.pollController) state.pollController.abort();
    stopPolling();
  };
  ws.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "snapshot") {
      state.wsConnected = true;
      stopPolling();
      applySnapshotIfNewer(payload.snapshot, "ws");
    }
  };
  ws.onclose = () => {
    if (state.ws === ws) state.ws = null;
    state.wsConnected = false;
    startPolling({ immediate: true });
    if (!state.reconnectTimer) {
      state.reconnectTimer = setTimeout(() => {
        state.reconnectTimer = null;
        connectWebSocket();
      }, SNAPSHOT_RECONNECT_MS);
    }
  };
  ws.onerror = () => {
    state.wsConnected = false;
    startPolling({ immediate: true });
  };
}

function initTabs() {
  document.querySelectorAll("[data-tab-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = button.dataset.tabTarget;
      document.querySelectorAll("[data-tab-target]").forEach((item) => item.classList.toggle("active", item === button));
      document.querySelectorAll("[data-tab-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.tabPanel === target));
    });
  });
}

function initTables() {
  Object.entries(tableConfigs).forEach(([tableKey, config]) => {
    state.tables[tableKey] = { limit: config.defaultLimit || 50, offset: 0, requestSeq: 0 };
    renderToolbar(tableKey);
    bindTableControls(tableKey);
    fetchTable(tableKey).catch((error) => setTableStatus(tableKey, error.message, "bad"));
  });
}

function initDashboard() {
  initTabs();
  connectWebSocket();
  setTimeout(() => {
    if (!state.lastSnapshotAt) startPolling({ immediate: true });
  }, SNAPSHOT_INITIAL_FALLBACK_MS);
  initTables();
  document.getElementById("runtime-start")?.addEventListener("click", (event) => runtimeCommand("start", event.currentTarget).catch(() => {}));
  document.getElementById("runtime-stop")?.addEventListener("click", (event) => runtimeCommand("stop", event.currentTarget).catch(() => {}));
  document.getElementById("runtime-cycle")?.addEventListener("click", (event) => runtimeCommand("cycle", event.currentTarget).catch(() => {}));
  [
    ["shadow-small-entry-ops-preflight", "preflight"],
    ["shadow-small-entry-ops-arm", "arm"],
    ["shadow-small-entry-ops-confirm", "confirm"],
    ["shadow-small-entry-ops-pause", "pause"],
    ["shadow-small-entry-ops-rollback", "rollback"],
    ["shadow-small-entry-ops-emergency-pause", "emergency-pause"],
  ].forEach(([id, action]) => {
    document.getElementById(id)?.addEventListener("click", (event) => shadowSmallEntryOpsAction(action, event.currentTarget).catch(() => {}));
  });
  [
    ["shadow-small-entry-pilot-start", "start"],
    ["shadow-small-entry-pilot-complete", "complete"],
    ["shadow-small-entry-pilot-generate-report", "generate-report"],
  ].forEach(([id, action]) => {
    document.getElementById(id)?.addEventListener("click", (event) => shadowSmallEntryPilotAction(action, event.currentTarget).catch(() => {}));
  });
  document.getElementById("buy-zero-rca-refresh")?.addEventListener("click", () => {
    state.buyZeroRca.lastFetchedAt = 0;
    refreshBuyZeroRcaTables({ force: true }).catch((error) => {
      text("buy-zero-rca-ready-status", `오류: ${error.message}`);
      cls("buy-zero-rca-ready-status", "counter bad");
    });
    pollSnapshot().catch(() => {});
  });
  document.getElementById("detail-close")?.addEventListener("click", closeDetailPanel);
  document.getElementById("detail-backdrop")?.addEventListener("click", closeDetailPanel);
}

initDashboard();
