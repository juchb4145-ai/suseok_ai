const state = {
  snapshot: null,
  previousSnapshot: null,
  filters: {
    status: "ALL",
    role: "ALL",
    market: "ALL",
    data: "ALL",
    order: "ALL",
  },
  selectedSymbol: "",
  chartInterval: "1m",
  operatorEvents: [],
  acknowledgedEventIds: new Set(),
  persistedEventIds: new Set(),
  operatorEventSyncPending: [],
  operatorEventLastSyncAt: "",
  operatorEventServerBacked: false,
  operatorSessionSummary: {},
  actionCatalog: {},
  disabledActionCatalog: {},
  actionRecommendations: [],
  disabledActionRecommendations: [],
  actionHistory: [],
  selectedEventId: "",
  selectedActionContext: {},
  pendingAction: null,
  actionSummary: {},
  runbook: null,
  postmarketReviewSummary: {},
  postmarketReviewItems: [],
  postmarketReviewFilters: {
    outcome_label: "ALL",
  },
  postmarketReviewSelectedItem: null,
  postmarketReviewLoading: false,
  postmarketReviewLastGeneratedAt: "",
  promotionDecision: null,
  promotionDecisionLoading: false,
  promotionDecisionError: "",
  promotionDecisionLastFetchedAt: "",
  promotionWindowSec: 0,
  promotionSelectedBlocker: "",
  promotionDrilldown: null,
  promotionDrilldownLoading: false,
  promotionDrilldownError: "",
  buyZeroRca: {
    summary: {},
    readyRows: [],
    rallyRows: [],
    stageRows: [],
    stage: "",
    loading: false,
    lastFetchedAt: 0,
    tradeDate: "",
  },
  naverThemeSyncBusy: false,
  alertFilters: {
    category: "ALL",
    hideAcknowledged: false,
  },
  journalFilter: "ALL",
  maxOperatorEvents: 200,
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
  WAIT_PRICE_LOCATION_DATA: "wait",
  WAIT_PRICE_LOCATION_WARMUP: "wait",
  WAIT_PRICE_LOCATION_PROVISIONAL: "wait",
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
  PROMOTE: "ready",
  HOLD: "wait",
  DEMOTE: "blocked",
  BLOCK: "blocked",
  LOADING: "observe",
  PROMISING_SHADOW: "ready",
  OBSERVE_MORE: "ready-small",
  INSUFFICIENT_SAMPLE: "warning",
  RISK_TOO_HIGH: "blocked",
  DO_NOT_PROMOTE: "blocked",
  NO_CANDIDATES: "observe",
  NO_DATA: "observe",
  ERROR: "blocked",
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
  WAIT_PRICE_LOCATION_DATA: "가격 위치 데이터 대기",
  WAIT_PRICE_LOCATION_WARMUP: "가격 위치 워밍업",
  WAIT_PRICE_LOCATION_PROVISIONAL: "임시 가격 위치",
  WAIT_PRICE_LOCATION_UNKNOWN: "가격 위치 확인 대기",
  WAIT_DATA_SUPPORT_NOT_READY: "지지선 데이터 대기",
  WAIT_DATA_LATEST_TICK_STALE: "틱 데이터 갱신 대기",
};

const operatorStorageKeys = {
  acknowledged: "themeLabOperatorAcknowledgedEventIds",
  alertFilter: "themeLabOperatorAlertFilter",
  hideAcknowledged: "themeLabOperatorHideAcknowledged",
};

const safeOperatorActionTypes = [
  "REFRESH_SNAPSHOT",
  "RUNTIME_CYCLE_ONCE",
  "RUNTIME_START",
  "RUNTIME_STOP",
  "RUNTIME_RESTART",
  "CHECK_RUNTIME_READINESS",
  "CHECK_GATEWAY_STATUS",
  "START_KIWOOM_GATEWAY",
  "OPEN_DRY_RUN_ORDER_DETAIL",
  "REBUILD_DRY_RUN_PERFORMANCE",
  "REBUILD_POSTMARKET_REVIEW",
  "REBUILD_TRANSPORT_LATENCY_REPORT",
  "EXPORT_TRANSPORT_LATENCY_REPORT",
  "ACK_EVENT",
  "HIDE_EVENT",
  "SNOOZE_EVENT",
  "ADD_OPERATOR_NOTE",
  "OPEN_RUNBOOK",
];

const blockedOperatorActionTypes = [
  "LIVE_BUY",
  "LIVE_SELL",
  "CANCEL_LIVE_ORDER",
  "OVERRIDE_LIVE_GUARD",
  "FORCE_READY",
  "CHANGE_RISK_THRESHOLD",
  "CHANGE_STRATEGY_PARAMETER",
  "DISABLE_RISK_GATE",
];

const BUY_ZERO_RCA_REFRESH_MS = 30000;
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

function reasonBadge(reason) {
  const label = String(reason || "-");
  const upper = label.toUpperCase();
  const critical = BUY_ZERO_RCA_CRITICAL_REASONS.has(upper) || /DATA_INSUFFICIENT|CHASE|LIVE_SIM_BLOCKED|DIAGNOSTIC_ONLY|ORDER_SINK_NOOP|GATE_RESULT_KEY_MISMATCH|WAIT_MARKET_CONFIRMATION/i.test(upper);
  return badge(label, critical ? "critical" : "");
}

function reasonBadges(reasons) {
  const values = [];
  (reasons || []).forEach((reason) => {
    const label = String(reason || "").trim();
    if (label && !values.includes(label)) values.push(label);
  });
  return values.length ? values.map((reason) => reasonBadge(reason)).join(" ") : `<span class="muted">-</span>`;
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

function ratio(value) {
  if (value == null || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${(number * 100).toFixed(1)}%`;
}

function optionalNumber(value, digits = 1) {
  if (value == null || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number.toFixed(digits);
}

function yesNoUnknown(value) {
  if (value === true) return "예";
  if (value === false) return "아니오";
  return "-";
}

function localTradeDate() {
  const now = new Date();
  const year = String(now.getFullYear());
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function formatDateTime(value) {
  if (!value) return "-";
  return String(value).replace("T", " ").replace("+00:00", "Z");
}

function getStoredToken() {
  try {
    return window.localStorage.getItem("tradingCoreToken") || "";
  } catch (_) {
    return "";
  }
}

function rememberToken(token) {
  try {
    if (token) window.localStorage.setItem("tradingCoreToken", token);
  } catch (_) {}
}

function forgetStoredToken() {
  try {
    window.localStorage.removeItem("tradingCoreToken");
  } catch (_) {}
}

function promptForToken(message = "TRADING_CORE_TOKEN") {
  return requestLocalToken(message);
}

function requestLocalToken(message = "TRADING_CORE_TOKEN") {
  return new Promise((resolve) => {
    let modal = document.getElementById("local-token-modal");
    if (!modal) {
      modal = document.createElement("div");
      modal.id = "local-token-modal";
      modal.className = "action-confirm-modal";
      modal.hidden = true;
      modal.innerHTML = `
        <div class="action-confirm-dialog local-token-dialog" role="dialog" aria-modal="true" aria-labelledby="local-token-title">
          <h2 id="local-token-title">로컬 토큰 입력</h2>
          <div class="action-confirm-body">
            <div class="operator-alert-empty" id="local-token-message">TRADING_CORE_TOKEN</div>
            <input id="local-token-input" class="local-token-input" type="password" autocomplete="current-password" />
          </div>
          <div class="operator-alert-actions">
            <button id="local-token-cancel" type="button">취소</button>
            <button id="local-token-submit" type="button">확인</button>
          </div>
        </div>
      `;
      document.body.appendChild(modal);
    }
    const input = modal.querySelector("#local-token-input");
    const label = modal.querySelector("#local-token-message");
    const submit = modal.querySelector("#local-token-submit");
    const cancel = modal.querySelector("#local-token-cancel");
    const cleanup = (token = "") => {
      modal.hidden = true;
      submit.removeEventListener("click", onSubmit);
      cancel.removeEventListener("click", onCancel);
      input.removeEventListener("keydown", onKeydown);
      rememberToken(token);
      resolve(token);
    };
    const onSubmit = () => cleanup(String(input.value || "").trim());
    const onCancel = () => cleanup("");
    const onKeydown = (event) => {
      if (event.key === "Enter") onSubmit();
      if (event.key === "Escape") onCancel();
    };
    if (label) label.textContent = message;
    input.value = "";
    submit.addEventListener("click", onSubmit);
    cancel.addEventListener("click", onCancel);
    input.addEventListener("keydown", onKeydown);
    modal.hidden = false;
    setTimeout(() => input.focus(), 0);
  });
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

async function runWithLocalTokenRetry(requestFn) {
  let token = getStoredToken() || await promptForToken();
  if (!token) return null;
  let result = await requestFn(token);
  if (!result.response.ok && isInvalidTokenResponse(result.response, result.payload)) {
    forgetStoredToken();
    token = await promptForToken("TRADING_CORE_TOKEN");
    if (!token) return null;
    result = await requestFn(token);
  }
  if (!result.response.ok) {
    if (isInvalidTokenResponse(result.response, result.payload)) forgetStoredToken();
    throw new Error(result.payload.detail || result.payload.error || `${result.response.status} ${result.response.statusText}`);
  }
  return result.payload;
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

function compactNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number.toLocaleString("ko-KR");
}

function multiplier(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `x${number.toFixed(2)}`;
}

function shortId(value) {
  const label = String(value || "");
  if (!label) return "-";
  if (label.length <= 18) return escapeHtml(label);
  return `<span title="${escapeHtml(label)}">${escapeHtml(`${label.substring(0, 8)}...${label.substring(label.length - 6)}`)}</span>`;
}

function selectedWatchItem(snapshot) {
  const watchset = (snapshot || {}).watchset || [];
  const selected = watchset.find((item) => item.symbol === state.selectedSymbol);
  if (selected) return selected;
  return watchset[0] || (snapshot || {}).gate_detail || {};
}

function selectedChartForSymbol(snapshot, symbol) {
  const payload = snapshot || {};
  const selectedSymbol = String(symbol || "");
  const universe = payload.chart_universe || [];
  const chart = universe.find((item) => item.symbol === selectedSymbol);
  if (chart) return chart;
  const watchItem = (payload.watchset || []).find((item) => item.symbol === selectedSymbol);
  if (watchItem) return chartFromWatchItem(watchItem);
  if (payload.selected_chart && Object.keys(payload.selected_chart).length) return payload.selected_chart;
  return chartFromWatchItem({});
}

function chartFromWatchItem(item) {
  const candles1m = item.recent_candles_1m || [];
  const candles3m = item.recent_candles_3m || [];
  const quoteValues = ["current_price", "vwap", "recent_support_price", "support_price", "upper_limit_price", "breakout_level"]
    .some((key) => numberOrNull(item[key]) !== null);
  const hasCandles = Boolean(candles1m.length || candles3m.length);
  return {
    symbol: item.symbol || "",
    name: item.stock_name || item.name || item.symbol || "WatchSet",
    type: "stock",
    reason: item.display_status || item.gate_status || "WATCHSET",
    has_candle_data: hasCandles,
    chart_data_status: hasCandles ? "READY" : quoteValues ? "QUOTE_ONLY" : "NO_CANDLE_DATA",
    candles: candles1m.length ? candles1m : candles3m,
    recent_candles_1m: candles1m,
    recent_candles_3m: candles3m,
    completed_minute_bar_count: item.completed_minute_bar_count || candles1m.length || 0,
    recent_3m_bar_count: item.recent_3m_bar_count || candles3m.length || 0,
    current_price: item.current_price,
    vwap: item.vwap,
    recent_support_price: item.recent_support_price,
    support_price: item.support_price,
    upper_limit_price: item.upper_limit_price,
    breakout_level: item.breakout_level,
    recent_support_source: item.recent_support_source || item.support_source || "",
    prev_close_inferred_from_change_rate: Boolean(item.prev_close_inferred_from_change_rate),
  };
}

function loadOperatorPreferences() {
  try {
    const raw = window.localStorage.getItem(operatorStorageKeys.acknowledged);
    const values = JSON.parse(raw || "[]");
    state.acknowledgedEventIds = new Set(Array.isArray(values) ? values : []);
  } catch (_) {
    state.acknowledgedEventIds = new Set();
  }
  try {
    state.alertFilters.category = window.localStorage.getItem(operatorStorageKeys.alertFilter) || "ALL";
    state.alertFilters.hideAcknowledged = window.localStorage.getItem(operatorStorageKeys.hideAcknowledged) === "1";
  } catch (_) {}
}

function saveAcknowledgedEventIds() {
  try {
    const ids = [...state.acknowledgedEventIds].slice(-500);
    window.localStorage.setItem(operatorStorageKeys.acknowledged, JSON.stringify(ids));
  } catch (_) {}
}

function saveOperatorAlertFilter() {
  try {
    window.localStorage.setItem(operatorStorageKeys.alertFilter, state.alertFilters.category || "ALL");
    window.localStorage.setItem(operatorStorageKeys.hideAcknowledged, state.alertFilters.hideAcknowledged ? "1" : "0");
  } catch (_) {}
}

function normalizeEventCategory(value) {
  const category = String(value || "").trim().toLowerCase();
  return category || "info";
}

function normalizeOperatorEvent(event, options = {}) {
  const type = String(event.event_type || event.type || "").trim();
  const eventId = String(event.event_id || event.id || makeEventId({ ...event, type })).trim();
  const occurredAt = String(event.occurred_at || event.created_at || new Date().toISOString());
  const severity = String(event.severity || eventSeverity({ ...event, type })).toUpperCase();
  const category = normalizeEventCategory(event.category || eventCategory({ ...event, type, severity }));
  const acknowledged = Boolean(event.acknowledged || event.acknowledged_at);
  const normalized = {
    ...event,
    id: eventId,
    event_id: eventId,
    type,
    event_type: type,
    created_at: occurredAt,
    occurred_at: occurredAt,
    severity,
    category,
    message: event.message_ko || event.message || type,
    message_ko: event.message_ko || event.message || type,
    acknowledged,
    hidden: Boolean(event.hidden),
    persisted: Boolean(options.persisted || event.persisted || event.received_at),
    pending_sync: Boolean(event.pending_sync),
  };
  if (acknowledged) state.acknowledgedEventIds.add(eventId);
  if (normalized.persisted) state.persistedEventIds.add(eventId);
  return normalized;
}

function mergeOperatorEvents(events, options = {}) {
  const existing = new Map(state.operatorEvents.map((event) => [event.id, event]));
  (events || []).forEach((event) => {
    const normalized = normalizeOperatorEvent(event, options);
    if (!normalized.id || !normalized.type) return;
    const previous = existing.get(normalized.id) || {};
    existing.set(normalized.id, { ...previous, ...normalized });
  });
  state.operatorEvents = [...existing.values()]
    .sort((left, right) => new Date(right.created_at || 0) - new Date(left.created_at || 0))
    .slice(0, state.maxOperatorEvents);
}

function watchsetBySymbol(snapshot) {
  return new Map(((snapshot || {}).watchset || []).map((item) => [String(item.symbol || ""), item]));
}

function selectSymbol(symbol, options = {}) {
  const snapshot = state.snapshot || {};
  const targetSymbol = String(symbol || "");
  const item = (snapshot.watchset || []).find((candidate) => candidate.symbol === targetSymbol) || selectedWatchItem(snapshot);
  if (targetSymbol) state.selectedSymbol = targetSymbol;
  if (options.acknowledgeEventId) {
    state.acknowledgedEventIds.add(options.acknowledgeEventId);
    saveAcknowledgedEventIds();
  }
  const selectedChart = selectedChartForSymbol(snapshot, item.symbol || state.selectedSymbol);
  renderFocusPanel(item, selectedChart);
  renderChart(selectedChart);
  renderGate(item);
  renderWatchset(snapshot.watchset || []);
  renderOperatorAlerts();
  renderDecisionTimeline();
  renderOperatorEventJournal();
  if (options.source) {
    const context = actionContextFromCandidate(item);
    state.selectedActionContext = context;
    fetchActionRecommendations(context).catch((error) => {
      text("operator-action-context", `추천 액션 실패: ${error.message || error}`);
    });
  }
}

function deriveOperatorEvents(previousSnapshot, currentSnapshot) {
  if (!previousSnapshot || !currentSnapshot) return [];
  return [
    ...summaryDiffEvents(previousSnapshot.summary || {}, currentSnapshot.summary || {}),
    ...watchsetDiffEvents(previousSnapshot.watchset || [], currentSnapshot.watchset || []),
    ...gatewayDiffEvents(previousSnapshot.gateway || {}, currentSnapshot.gateway || {}),
    ...dataQualityDiffEvents(previousSnapshot.data_quality || {}, currentSnapshot.data_quality || {}),
    ...themeDiffEvents(previousSnapshot.ranked_themes || [], currentSnapshot.ranked_themes || []),
  ].map((event) => ({
    created_at: new Date().toISOString(),
    ...event,
  }));
}

function appendOperatorEvents(events) {
  if (!events.length) return [];
  const existingIds = new Set(state.operatorEvents.map((event) => event.id));
  const additions = [];
  events.forEach((event) => {
    const id = makeEventId(event);
    if (existingIds.has(id)) return;
    existingIds.add(id);
    additions.push(normalizeOperatorEvent({ ...event, id, event_id: id, pending_sync: true }));
  });
  if (!additions.length) return [];
  state.operatorEvents = [...additions, ...state.operatorEvents].slice(0, state.maxOperatorEvents);
  return additions;
}

function makeEventId(event) {
  if (event.event_id) return String(event.event_id);
  if (event.id) return String(event.id);
  if (["ORDER_INTENT_CREATED", "VIRTUAL_ORDER_CREATED"].includes(event.type) && event.candidate_instance_id) {
    return [event.type, event.candidate_instance_id].join(":");
  }
  const bucket = String(event.created_at || new Date().toISOString()).slice(0, 16);
  return [
    event.type,
    event.symbol || "GLOBAL",
    event.from_status || "",
    event.to_status || "",
    event.candidate_instance_id || "",
    bucket,
  ].join(":");
}

function summaryDiffEvents(previousSummary, currentSummary) {
  const events = [];
  if (!previousSummary.snapshot_stale && currentSummary.snapshot_stale) {
    events.push({
      type: "SNAPSHOT_STALE",
      from_status: "FRESH",
      to_status: "STALE",
      message: `스냅샷 지연: ${currentSummary.snapshot_age_label || "-"} 전 계산된 결과`,
    });
  }
  if (previousSummary.snapshot_stale && !currentSummary.snapshot_stale) {
    events.push({
      type: "SNAPSHOT_RECOVERED",
      from_status: "STALE",
      to_status: "FRESH",
      message: "스냅샷 지연 회복: 최신 ThemeLab 결과 수신",
    });
  }
  if (previousSummary.operation_status !== "READY_BUT_LIVE_BLOCKED" && currentSummary.operation_status === "READY_BUT_LIVE_BLOCKED") {
    events.push({
      type: "READY_BUT_LIVE_BLOCKED",
      from_status: previousSummary.operation_status || "",
      to_status: "READY_BUT_LIVE_BLOCKED",
      message: currentSummary.operation_message_ko || "READY지만 LIVE Guard 통과 후보가 없습니다.",
    });
  }
  return events;
}

function watchsetDiffEvents(previousWatchset, currentWatchset) {
  const previousBySymbol = new Map(previousWatchset.map((item) => [String(item.symbol || ""), item]));
  return currentWatchset.flatMap((item) => {
    const previous = previousBySymbol.get(String(item.symbol || "")) || {};
    const events = [];
    const gate = item.gate_status || "";
    const previousGate = previous.gate_status || "";
    const display = item.display_status || gate;
    const previousDisplay = previous.display_status || previousGate;
    if (gate === "READY" && previousGate !== "READY") events.push(stockEvent("BUY_READY_NEW", item, previousGate, gate, readyMessage(item)));
    if (gate === "READY_SMALL" && previousGate !== "READY_SMALL") events.push(stockEvent("BUY_READY_SMALL_NEW", item, previousGate, gate, readyMessage(item)));
    if (previousGate === "READY" && gate === "WAIT") events.push(stockEvent("READY_TO_WAIT", item, previousGate, gate, `READY 이탈: ${stockLabel(item)} / ${display}`));
    if (gateIsReadyLike(item) && !item.live_order_guard_passed && (!gateIsReadyLike(previous) || previous.live_order_guard_passed)) {
      events.push(stockEvent("READY_BUT_LIVE_BLOCKED", item, previousDisplay, display, `READY지만 LIVE 차단: ${stockLabel(item)} / ${blockReason(item)}`));
    }
    if (item.runtime_order_intent_created && !previous.runtime_order_intent_created) {
      events.push(stockEvent("ORDER_INTENT_CREATED", item, previousDisplay, display, `주문 의도 생성: ${stockLabel(item)} / ${item.candidate_instance_id || "-"}`));
    }
    if (item.virtual_order_created && !previous.virtual_order_created) {
      events.push(stockEvent("VIRTUAL_ORDER_CREATED", item, previousDisplay, display, `가상 주문 생성: ${stockLabel(item)} / ${item.candidate_instance_id || "-"}`));
    }
    if (isMarketPending(item) && !isMarketPending(previous)) {
      events.push(stockEvent("MARKET_WAIT_STARTED", item, previousDisplay, display, `시장 대기 전환: ${stockLabel(item)} / ${display} / ${item.market_wait_reason || "-"}`));
    }
    if (!isMarketPending(item) && isMarketPending(previous)) {
      events.push(stockEvent("MARKET_RECOVERED", item, previousDisplay, display, `시장 대기 해소: ${stockLabel(item)} / ${display || gate}`));
    }
    if ((item.chase_risk || display === "CHASE_RISK_BLOCKED") && !(previous.chase_risk || previousDisplay === "CHASE_RISK_BLOCKED")) {
      events.push(stockEvent("CHASE_RISK_BLOCKED", item, previousDisplay, display, chaseRiskMessage(item)));
    }
    if (display === "LATE_CHASE_TEMP_WAIT" && previousDisplay !== "LATE_CHASE_TEMP_WAIT") {
      events.push(stockEvent("LATE_CHASE_TEMP_WAIT", item, previousDisplay, display, chaseRiskMessage(item)));
    }
    return events;
  });
}

function gatewayDiffEvents(previousGateway, currentGateway) {
  const wasHealthy = Boolean(previousGateway.connected && previousGateway.heartbeat_ok);
  const healthy = Boolean(currentGateway.connected && currentGateway.heartbeat_ok);
  if (wasHealthy && !healthy) {
    return [{
      type: "GATEWAY_DISCONNECTED",
      from_status: "CONNECTED",
      to_status: currentGateway.connection_state || "DISCONNECTED",
      message: "Gateway 연결 끊김: heartbeat 또는 connected 상태 확인 필요",
    }];
  }
  if (!wasHealthy && healthy) {
    return [{
      type: "GATEWAY_RECOVERED",
      from_status: previousGateway.connection_state || "DISCONNECTED",
      to_status: "CONNECTED",
      message: "Gateway 연결 회복: heartbeat 정상 수신",
    }];
  }
  return [];
}

function dataQualityDiffEvents(previousDataQuality, currentDataQuality) {
  const previousStatus = previousDataQuality.status || "UNKNOWN";
  const currentStatus = currentDataQuality.status || "UNKNOWN";
  const previousBad = ["DEGRADED", "BROKEN"].includes(previousStatus);
  const currentBad = ["DEGRADED", "BROKEN"].includes(currentStatus);
  if (!previousBad && currentBad) {
    return [{
      type: "DATA_QUALITY_DEGRADED",
      from_status: previousStatus,
      to_status: currentStatus,
      data_status: currentStatus,
      message: `데이터 품질 저하: ${currentDataQuality.message || currentStatus}`,
    }];
  }
  if (previousBad && !currentBad && currentStatus === "OK") {
    return [{
      type: "DATA_QUALITY_RECOVERED",
      from_status: previousStatus,
      to_status: currentStatus,
      message: "데이터 품질 회복: WatchSet 보조 데이터 정상화",
    }];
  }
  return [];
}

function themeDiffEvents(previousThemes, currentThemes) {
  const previousTop = previousThemes[0] || {};
  const currentTop = currentThemes[0] || {};
  const events = [];
  if (previousTop.theme_name && currentTop.theme_name && previousTop.theme_name !== currentTop.theme_name) {
    events.push({
      type: "TOP_THEME_CHANGED",
      from_status: previousTop.theme_name,
      to_status: currentTop.theme_name,
      symbol: currentTop.top_leader_symbol || "",
      message: `Top 테마 변경: ${previousTop.theme_name} → ${currentTop.theme_name}`,
    });
  }
  if (previousTop.top_leader_symbol && currentTop.top_leader_symbol && previousTop.top_leader_symbol !== currentTop.top_leader_symbol) {
    events.push({
      type: "TOP_LEADER_CHANGED",
      from_status: previousTop.top_leader_symbol,
      to_status: currentTop.top_leader_symbol,
      symbol: currentTop.top_leader_symbol || "",
      message: `Top 리더 변경: ${previousTop.top_leader_name || previousTop.top_leader_symbol} → ${currentTop.top_leader_name || currentTop.top_leader_symbol}`,
    });
  }
  return events;
}

function stockEvent(type, item, fromStatus, toStatus, message) {
  return {
    type,
    symbol: item.symbol || "",
    stock_name: item.stock_name || item.name || "",
    primary_theme: item.primary_theme || item.theme_name || "",
    stock_role: item.stock_role || "",
    candidate_instance_id: item.candidate_instance_id || "",
    from_status: fromStatus || "",
    to_status: toStatus || "",
    message,
  };
}

function readyMessage(item) {
  return `READY 발생: ${stockLabel(item)} / ${item.primary_theme || "-"} / ${item.stock_role || "-"} / LIVE Guard ${item.live_order_guard_passed ? "통과" : "미통과"}`;
}

function chaseRiskMessage(item) {
  return `추격매수 차단: ${stockLabel(item)} / ${item.late_chase_level || "-"} / ${Number(item.late_chase_recheck_after_sec || 0)}초 후 재확인`;
}

function stockLabel(item) {
  return `${item.stock_name || item.name || item.symbol || "-"}[${item.symbol || "-"}]`;
}

function blockReason(item) {
  return item.blocked_reason || (item.blocked_reason_codes || item.risk_reason_codes || []).join(", ") || "-";
}

function eventSeverity(event) {
  if (event.type === "DATA_QUALITY_DEGRADED" && event.data_status === "BROKEN") return "CRITICAL";
  if (["GATEWAY_DISCONNECTED", "SNAPSHOT_STALE", "READY_BUT_LIVE_BLOCKED"].includes(event.type)) return "CRITICAL";
  if (["MARKET_WAIT_STARTED", "DATA_QUALITY_DEGRADED", "CHASE_RISK_BLOCKED", "LATE_CHASE_TEMP_WAIT", "READY_TO_WAIT"].includes(event.type)) return "WARNING";
  if (["BUY_READY_NEW", "BUY_READY_SMALL_NEW", "MARKET_RECOVERED", "TOP_THEME_CHANGED", "TOP_LEADER_CHANGED"].includes(event.type)) return "OPPORTUNITY";
  return "INFO";
}

function eventCategory(event) {
  if (["ORDER_INTENT_CREATED", "VIRTUAL_ORDER_CREATED"].includes(event.type)) return "order";
  if (["DATA_QUALITY_DEGRADED", "DATA_QUALITY_RECOVERED"].includes(event.type)) return "data";
  if (["SNAPSHOT_STALE", "SNAPSHOT_RECOVERED"].includes(event.type)) return "snapshot";
  if (["GATEWAY_DISCONNECTED", "GATEWAY_RECOVERED"].includes(event.type)) return "gateway";
  if (["MARKET_WAIT_STARTED", "MARKET_RECOVERED"].includes(event.type)) return "market";
  if (["TOP_THEME_CHANGED", "TOP_LEADER_CHANGED"].includes(event.type)) return "theme";
  if (["CHASE_RISK_BLOCKED", "LATE_CHASE_TEMP_WAIT", "READY_BUT_LIVE_BLOCKED"].includes(event.type)) return "risk";
  const severity = event.severity || eventSeverity(event);
  if (severity === "OPPORTUNITY") return "opportunity";
  if (severity === "CRITICAL") return "critical";
  if (severity === "WARNING") return "warning";
  return "info";
}

function renderOperatorAlerts() {
  const list = document.getElementById("operator-alert-list");
  const count = document.getElementById("operator-alert-count");
  const hideButton = document.getElementById("operator-alert-hide-acknowledged");
  if (!list || !count) return;
  updateOperatorFilterButtons();
  const filtered = filteredOperatorEvents();
  const unackCount = state.operatorEvents.filter((event) => !event.hidden && !state.acknowledgedEventIds.has(event.id)).length;
  text("operator-alert-count", unackCount);
  if (hideButton) {
    hideButton.classList.toggle("active", state.alertFilters.hideAcknowledged);
    hideButton.setAttribute("aria-pressed", state.alertFilters.hideAcknowledged ? "true" : "false");
  }
  list.innerHTML = filtered.length ? filtered.slice(0, 40).map(operatorEventRow).join("") : `<div class="operator-alert-empty">표시할 신규 알림이 없습니다.</div>`;
}

function updateOperatorFilterButtons() {
  document.querySelectorAll("[data-alert-filter]").forEach((button) => {
    button.classList.toggle("active", button.dataset.alertFilter === state.alertFilters.category);
  });
}

function renderDecisionTimeline() {
  const list = document.getElementById("operator-timeline-list");
  if (!list) return;
  const visible = state.operatorEvents.filter((event) => !event.hidden);
  list.innerHTML = visible.length
    ? visible.slice(0, 20).map((event) => operatorEventRow(event, true)).join("")
    : `<div class="operator-alert-empty">상태 변화가 감지되면 여기에 누적됩니다.</div>`;
}

function filteredOperatorEvents() {
  const filter = state.alertFilters.category || "ALL";
  return state.operatorEvents.filter((event) => {
    if (event.hidden) return false;
    const acknowledged = state.acknowledgedEventIds.has(event.id);
    if (state.alertFilters.hideAcknowledged && acknowledged) return false;
    return matchesOperatorFilter(event, filter);
  });
}

function matchesOperatorFilter(event, filter) {
  const normalizedFilter = String(filter || "ALL").toUpperCase();
  if (normalizedFilter === "ALL") return true;
  return String(event.severity || "").toUpperCase() === normalizedFilter
    || String(event.category || "").toUpperCase() === normalizedFilter;
}

function operatorEventRow(event, compact = false) {
  const severity = event.severity || eventSeverity(event);
  const acknowledged = state.acknowledgedEventIds.has(event.id);
  const symbol = event.symbol || "";
  const syncState = event.persisted ? "persisted" : event.pending_sync ? "pending-sync" : "";
  return `
    <button class="operator-event-row ${severity.toLowerCase()} ${acknowledged ? "acknowledged" : ""} ${compact ? "compact" : ""} ${syncState}" data-event-id="${escapeHtml(event.id)}" data-symbol="${escapeHtml(symbol)}" type="button">
      <span class="operator-event-main">
        ${badge(severity, severity.toLowerCase())}
        <strong>${escapeHtml(event.message || event.type)}</strong>
      </span>
      <span class="operator-event-meta">${escapeHtml(formatEventTime(event.created_at))} · ${escapeHtml(event.type)}${symbol ? ` · ${escapeHtml(symbol)}` : ""}</span>
    </button>
  `;
}

function renderOperatorSessionReview(summary = state.operatorSessionSummary) {
  const node = document.getElementById("operator-session-summary-cards");
  if (!node) return;
  const cards = [
    ["총 이벤트", summary.total_count, "info"],
    ["CRITICAL", summary.critical_count, "critical"],
    ["WARNING", summary.warning_count, "warning"],
    ["OPPORTUNITY", summary.opportunity_count, "opportunity"],
    ["READY", summary.ready_event_count, "ready"],
    ["LIVE 차단", summary.live_guard_blocked_count, "warning"],
    ["주문 의도", summary.order_intent_created_count, "ready-small"],
    ["데이터 저하", summary.data_quality_degraded_count, "warning"],
    ["시장 대기", summary.market_wait_started_count, "wait"],
    ["추격 차단", summary.chase_risk_blocked_count, "blocked"],
    ["Gateway", summary.gateway_disconnected_count, "critical"],
    ["Snapshot", summary.snapshot_stale_count, "warning"],
  ];
  node.innerHTML = cards.map(([label, value, tone]) => `
    <article class="session-review-card ${tone}">
      <span>${escapeHtml(label)}</span>
      <strong>${Number(value || 0).toLocaleString("ko-KR")}</strong>
    </article>
  `).join("");
}

function renderOperatorEventJournal() {
  const list = document.getElementById("operator-event-journal-list");
  if (!list) return;
  document.querySelectorAll("[data-journal-filter]").forEach((button) => {
    button.classList.toggle("active", button.dataset.journalFilter === state.journalFilter);
  });
  const visible = state.operatorEvents.filter((event) => !event.hidden);
  const filtered = visible.filter((event) => {
    if (state.journalFilter === "SYMBOL") return Boolean(state.selectedSymbol && event.symbol === state.selectedSymbol);
    return matchesOperatorFilter(event, state.journalFilter);
  });
  text("operator-event-journal-count", `${filtered.length}/${visible.length}`);
  list.innerHTML = filtered.length
    ? filtered.slice(0, 120).map(eventJournalRow).join("")
    : `<div class="operator-alert-empty">저장된 운영 이벤트가 없습니다.</div>`;
}

function eventJournalRow(event) {
  const severity = event.severity || eventSeverity(event);
  const acknowledged = state.acknowledgedEventIds.has(event.id);
  const symbol = event.symbol || "";
  const category = String(event.category || "info").toUpperCase();
  const persisted = event.persisted ? "DB" : "PENDING";
  return `
    <button class="operator-event-row event-journal-row ${severity.toLowerCase()} ${acknowledged ? "acknowledged" : ""}" data-event-id="${escapeHtml(event.id)}" data-symbol="${escapeHtml(symbol)}" type="button">
      <span class="operator-event-main">
        ${badge(severity, severity.toLowerCase())}
        <strong>${escapeHtml(event.message || event.type)}</strong>
      </span>
      <span class="operator-event-meta">${escapeHtml(formatEventTime(event.created_at))} · ${escapeHtml(event.type)} · ${escapeHtml(category)}${symbol ? ` · ${escapeHtml(symbol)}` : ""} · ${escapeHtml(persisted)}</span>
    </button>
  `;
}

function updateOperatorSyncStatus(message = "") {
  const node = document.getElementById("operator-event-sync-status");
  if (!node) return;
  if (message) {
    node.textContent = message;
    return;
  }
  if (state.operatorEventSyncPending.length) {
    node.textContent = `저장 지연 ${state.operatorEventSyncPending.length}건`;
    return;
  }
  node.textContent = state.operatorEventServerBacked
    ? `DB 동기화 ${formatEventTime(state.operatorEventLastSyncAt)}`
    : "DB 연결 대기";
}

function operatorEventPayload(event) {
  return {
    event_id: event.id,
    event_type: event.type,
    severity: event.severity,
    category: normalizeEventCategory(event.category),
    occurred_at: event.created_at,
    source: "themelab_dashboard",
    symbol: event.symbol || "",
    stock_name: event.stock_name || "",
    primary_theme: event.primary_theme || "",
    stock_role: event.stock_role || "",
    candidate_instance_id: event.candidate_instance_id || "",
    from_status: event.from_status || "",
    to_status: event.to_status || "",
    gate_status: event.gate_status || "",
    display_status: event.display_status || "",
    message_ko: event.message || event.type,
    payload: { ...event },
  };
}

async function persistOperatorEvents(events) {
  const pending = (events || [])
    .map((event) => normalizeOperatorEvent(event))
    .filter((event) => event.id && !state.persistedEventIds.has(event.id));
  if (!pending.length) return;
  state.operatorEventSyncPending = pending.map((event) => event.id);
  updateOperatorSyncStatus();
  const response = await fetch("/api/themelab/operator-events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ events: pending.map(operatorEventPayload) }),
  });
  const payload = await parseResponsePayload(response);
  if (!response.ok) throw new Error(payload.detail || payload.error || `operator-events ${response.status}`);
  pending.forEach((event) => {
    state.persistedEventIds.add(event.id);
    const current = state.operatorEvents.find((item) => item.id === event.id);
    if (current) {
      current.persisted = true;
      current.pending_sync = false;
    }
  });
  state.operatorEventSyncPending = state.operatorEventSyncPending.filter((eventId) => !state.persistedEventIds.has(eventId));
  state.operatorEventServerBacked = true;
  state.operatorEventLastSyncAt = new Date().toISOString();
  updateOperatorSyncStatus();
  renderOperatorEventJournal();
  await fetchOperatorSessionReview();
}

async function fetchOperatorEvents() {
  const response = await fetch("/api/themelab/operator-events?include_hidden=false&limit=200", { cache: "no-store" });
  const payload = await parseResponsePayload(response);
  if (!response.ok) throw new Error(payload.detail || payload.error || `operator-events ${response.status}`);
  mergeOperatorEvents(payload.events || [], { persisted: true });
  state.operatorEventServerBacked = true;
  state.operatorEventLastSyncAt = new Date().toISOString();
  saveAcknowledgedEventIds();
  updateOperatorSyncStatus();
  renderOperatorAlerts();
  renderDecisionTimeline();
  renderOperatorEventJournal();
}

async function fetchOperatorSessionReview() {
  const response = await fetch("/api/themelab/operator-events/summary", { cache: "no-store" });
  const payload = await parseResponsePayload(response);
  if (!response.ok) throw new Error(payload.detail || payload.error || `operator-events summary ${response.status}`);
  state.operatorSessionSummary = payload || {};
  renderOperatorSessionReview(state.operatorSessionSummary);
}

async function acknowledgeOperatorEvents(eventIds) {
  const ids = (eventIds || []).filter(Boolean);
  if (!ids.length) return 0;
  const response = await fetch("/api/themelab/operator-events/ack", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event_ids: ids, acknowledged_by: "operator" }),
  });
  const payload = await parseResponsePayload(response);
  if (!response.ok) throw new Error(payload.detail || payload.error || `operator-events ack ${response.status}`);
  if (Number(payload.updated_count || 0) < ids.length) throw new Error("ACK 대상이 DB에 아직 저장되지 않았습니다.");
  const acknowledgedAt = new Date().toISOString();
  ids.forEach((eventId) => {
    state.acknowledgedEventIds.add(eventId);
    const item = state.operatorEvents.find((event) => event.id === eventId);
    if (item) {
      item.acknowledged = true;
      item.acknowledged_at = item.acknowledged_at || acknowledgedAt;
    }
  });
  state.operatorEventServerBacked = true;
  state.operatorEventLastSyncAt = acknowledgedAt;
  saveAcknowledgedEventIds();
  updateOperatorSyncStatus();
  renderOperatorAlerts();
  renderDecisionTimeline();
  renderOperatorEventJournal();
  return Number(payload.updated_count || 0);
}

async function hideOperatorEvents(eventIds) {
  const ids = (eventIds || []).filter(Boolean);
  if (!ids.length) return 0;
  const response = await fetch("/api/themelab/operator-events/hide", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event_ids: ids }),
  });
  const payload = await parseResponsePayload(response);
  if (!response.ok) throw new Error(payload.detail || payload.error || `operator-events hide ${response.status}`);
  if (Number(payload.updated_count || 0) < ids.length) throw new Error("숨김 대상이 DB에 아직 저장되지 않았습니다.");
  ids.forEach((eventId) => {
    const item = state.operatorEvents.find((event) => event.id === eventId);
    if (item) item.hidden = true;
  });
  state.operatorEventServerBacked = true;
  state.operatorEventLastSyncAt = new Date().toISOString();
  updateOperatorSyncStatus();
  renderOperatorAlerts();
  renderDecisionTimeline();
  renderOperatorEventJournal();
  await fetchOperatorSessionReview();
  return Number(payload.updated_count || 0);
}

async function initOperatorEventJournal() {
  try {
    await Promise.all([fetchOperatorEvents(), fetchOperatorSessionReview()]);
  } catch (error) {
    updateOperatorSyncStatus(`DB 복원 실패: ${error.message || error}`);
    renderOperatorSessionReview();
    renderOperatorEventJournal();
  }
}

async function fetchActionCatalog() {
  const response = await fetch("/api/themelab/operator-actions/catalog", { cache: "no-store" });
  const payload = await parseResponsePayload(response);
  if (!response.ok) throw new Error(payload.detail || payload.error || `operator-actions catalog ${response.status}`);
  state.actionCatalog = Object.fromEntries((payload.actions || []).map((item) => [item.action_type, item]));
  state.disabledActionCatalog = Object.fromEntries((payload.disabled_actions || []).map((item) => [item.action_type, item]));
  renderOperatorActionCenter();
}

async function fetchActionRecommendations(context = state.selectedActionContext) {
  const params = new URLSearchParams();
  const nextContext = { ...(context || {}) };
  if (nextContext.event_id) params.set("event_id", nextContext.event_id);
  if (nextContext.symbol) params.set("symbol", nextContext.symbol);
  if (nextContext.candidate_instance_id) params.set("candidate_instance_id", nextContext.candidate_instance_id);
  const response = await fetch(`/api/themelab/operator-actions/recommendations?${params.toString()}`, { cache: "no-store" });
  const payload = await parseResponsePayload(response);
  if (!response.ok) throw new Error(payload.detail || payload.error || `operator-actions recommendations ${response.status}`);
  state.selectedActionContext = { ...nextContext, ...(payload.context || {}) };
  state.selectedEventId = state.selectedActionContext.event_id || "";
  state.actionRecommendations = payload.recommendations || [];
  state.disabledActionRecommendations = payload.disabled_actions || [];
  state.runbook = payload.runbook || state.runbook;
  renderOperatorActionCenter();
  renderRunbook(state.runbook);
}

async function fetchActionHistory() {
  const response = await fetch("/api/themelab/operator-actions?limit=20", { cache: "no-store" });
  const payload = await parseResponsePayload(response);
  if (!response.ok) throw new Error(payload.detail || payload.error || `operator-actions ${response.status}`);
  state.actionHistory = payload.actions || payload.items || [];
  renderActionHistory(state.actionHistory);
}

async function fetchActionSummary() {
  const response = await fetch("/api/themelab/operator-actions/summary", { cache: "no-store" });
  const payload = await parseResponsePayload(response);
  if (!response.ok) throw new Error(payload.detail || payload.error || `operator-actions summary ${response.status}`);
  state.actionSummary = payload || {};
  renderActionSummary(state.actionSummary);
}

async function initOperatorActionCenter() {
  try {
    await fetchActionCatalog();
    await Promise.all([fetchActionHistory(), fetchActionSummary(), fetchActionRecommendations(state.selectedActionContext || {})]);
  } catch (error) {
    text("operator-action-context", `Action Center 복원 실패: ${error.message || error}`);
    renderOperatorActionCenter();
    renderRunbook(runbookForStatus("GENERAL"));
  }
}

function renderOperatorActionCenter() {
  renderActionContext();
  renderActionRecommendations(state.actionRecommendations);
  renderActionHistory(state.actionHistory);
  renderActionSummary(state.actionSummary);
}

function renderActionContext() {
  const context = state.selectedActionContext || {};
  const parts = [
    context.event_type ? `event ${context.event_type}` : "",
    context.symbol ? `symbol ${context.symbol}` : "",
    context.candidate_instance_id ? `candidate ${context.candidate_instance_id}` : "",
  ].filter(Boolean);
  text("operator-action-context", parts.length ? parts.join(" / ") : "선택 이벤트 또는 후보 기준 추천 액션");
}

function renderActionRecommendations(recommendations = []) {
  const node = document.getElementById("operator-action-recommendations");
  if (!node) return;
  const disabled = state.disabledActionRecommendations || [];
  const cards = [
    ...recommendations.map((item) => actionCard(item)),
    ...disabled.map((item) => actionCard(item, true)),
  ];
  node.innerHTML = cards.length ? cards.join("") : `<div class="operator-alert-empty">추천 액션이 없습니다.</div>`;
}

function actionCard(action, disabled = false) {
  const actionType = action.action_type || "";
  const risk = String(action.risk_level || (disabled ? "BLOCKED" : "LOW")).toLowerCase();
  const reason = action.reason_ko || action.reason || action.reason_ko || "";
  const token = action.requires_token ? "token 필요" : "token 불필요";
  const confirm = action.confirmation_required ? "확인 필요" : "즉시 기록";
  const title = disabled ? (action.reason_ko || "이번 PR 범위에서 금지된 액션입니다.") : "";
  return `
    <article class="action-card ${disabled ? "disabled" : risk}">
      <div class="action-card-head">
        <div class="action-card-title">
          <strong>${escapeHtml(action.label_ko || actionType)}</strong>
          <span>${escapeHtml(actionType)} · ${escapeHtml(token)} · ${escapeHtml(confirm)}</span>
        </div>
        ${badge(disabled ? "BLOCKED" : String(action.risk_level || "LOW"), disabled ? "blocked" : actionRiskLevel(actionType).toLowerCase())}
      </div>
      <p>${escapeHtml(reason || "현재 컨텍스트에서 실행 가능한 안전 액션입니다.")}</p>
      <button type="button" data-action-type="${escapeHtml(actionType)}" ${disabled ? "disabled" : ""} title="${escapeHtml(title)}">${disabled ? "금지됨" : "실행"}</button>
    </article>
  `;
}

function renderActionHistory(items = []) {
  const node = document.getElementById("operator-action-history");
  if (!node) return;
  node.innerHTML = items.length
    ? items.slice(0, 20).map((item) => `
      <div class="action-history-row">
        <div class="action-card-title">
          <strong>${escapeHtml(item.action_type || "-")}</strong>
          <span>${escapeHtml(formatEventTime(item.requested_at))}${item.symbol ? ` · ${escapeHtml(item.symbol)}` : ""}</span>
        </div>
        <span class="action-status-${escapeHtml(String(item.status || "").toLowerCase())}">${escapeHtml(item.status || "-")}</span>
      </div>
    `).join("")
    : `<div class="operator-alert-empty">아직 실행 기록이 없습니다.</div>`;
}

function renderActionSummary(summary = {}) {
  const total = Number(summary.total_count || 0);
  const success = Number(summary.success_count || 0);
  const failed = Number(summary.failed_count || 0);
  const blocked = Number(summary.blocked_count || 0);
  text("operator-action-summary", `${total} actions / success ${success} / failed ${failed} / blocked ${blocked}`);
}

async function executeOperatorAction(actionType, context = state.selectedActionContext, options = {}) {
  const action = state.actionCatalog[actionType] || state.disabledActionCatalog[actionType] || { action_type: actionType };
  const needsModal = !options.confirm && (action.confirmation_required || actionType === "ADD_OPERATOR_NOTE");
  if (needsModal) {
    confirmOperatorAction({ ...action, action_type: actionType, context });
    return null;
  }
  const body = {
    ...(context || {}),
    action_type: actionType,
    confirm: Boolean(options.confirm || !action.confirmation_required),
    note: options.note || "",
  };
  const runRequest = async (token = "") => {
    const headers = { "Content-Type": "application/json" };
    if (token) headers["X-Local-Token"] = token;
    const response = await fetch("/api/themelab/operator-actions/execute", {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });
    return { response, payload: await parseResponsePayload(response) };
  };
  let result;
  try {
    if (actionRequiresToken(actionType)) {
      result = await runWithLocalTokenRetry((token) => runRequest(token));
      if (!result) return null;
    } else {
      const { response, payload } = await runRequest();
      if (!response.ok) throw new Error(payload.detail || payload.error || `${response.status} ${response.statusText}`);
      result = payload;
    }
  } catch (error) {
    text("operator-action-context", `액션 실패: ${error.message || error}`);
    return null;
  }
  if (result.confirmation_required) {
    confirmOperatorAction({ ...action, action_type: actionType, context });
    return result;
  }
  appendActionResultToEvents(result);
  await Promise.all([fetchActionHistory(), fetchActionSummary(), fetchOperatorEvents().catch(() => {})]);
  if (actionType === "REFRESH_SNAPSHOT") await fetchSnapshot().catch(() => {});
  if (actionType === "OPEN_RUNBOOK") renderRunbook((result.result || {}).runbook || runbookForStatus((context || {}).event_type));
  if (actionType === "REBUILD_POSTMARKET_REVIEW") {
    await Promise.all([fetchPostmarketReviewSummary(), fetchPostmarketReviewItems()]);
  }
  return result;
}

function confirmOperatorAction(action) {
  state.pendingAction = action;
  const modal = document.getElementById("operator-action-confirm-modal");
  const body = document.getElementById("operator-action-confirm-body");
  const note = document.getElementById("operator-action-note");
  if (!modal || !body) return;
  const context = action.context || state.selectedActionContext || {};
  body.innerHTML = [
    actionLine("액션", `${action.label_ko || action.action_type} (${action.action_type})`),
    actionLine("대상 종목", context.symbol || "-"),
    actionLine("연결 이벤트", context.event_id || "-"),
    actionLine("예상 API", action.endpoint || "-"),
    actionLine("위험도", action.risk_level || actionRiskLevel(action.action_type)),
    actionLine("토큰", actionRequiresToken(action.action_type) ? "필요" : "불필요"),
    `<div class="operator-alert-empty">이 액션은 주문을 실행하지 않습니다. LIVE Guard 우회는 지원하지 않습니다.</div>`,
  ].join("");
  if (note) note.value = "";
  modal.hidden = false;
}

function actionLine(label, value) {
  return `<div class="cockpit-line"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(value)}</span></div>`;
}

function actionRequiresToken(actionType) {
  return Boolean((state.actionCatalog[actionType] || {}).requires_token);
}

function actionRiskLevel(actionType) {
  return String((state.actionCatalog[actionType] || {}).risk_level || "LOW");
}

async function fetchPostmarketReviewSummary() {
  const response = await fetch("/api/themelab/postmarket-review/summary", { cache: "no-store" });
  const payload = await parseResponsePayload(response);
  if (!response.ok) throw new Error(payload.detail || payload.error || `postmarket-review summary ${response.status}`);
  state.postmarketReviewSummary = payload || {};
  state.postmarketReviewLastGeneratedAt = latestGeneratedAt(state.postmarketReviewItems);
  renderPostmarketReviewPanel();
  return state.postmarketReviewSummary;
}

async function fetchPostmarketReviewItems(filters = state.postmarketReviewFilters) {
  const params = new URLSearchParams();
  params.set("limit", "1000");
  if (filters.symbol) params.set("symbol", filters.symbol);
  if (filters.primary_theme) params.set("primary_theme", filters.primary_theme);
  if (filters.event_type) params.set("event_type", filters.event_type);
  if (filters.min_return_5m_pct != null && filters.min_return_5m_pct !== "") {
    params.set("min_return_5m_pct", filters.min_return_5m_pct);
  }
  const response = await fetch(`/api/themelab/postmarket-review?${params.toString()}`, { cache: "no-store" });
  const payload = await parseResponsePayload(response);
  if (!response.ok) throw new Error(payload.detail || payload.error || `postmarket-review ${response.status}`);
  state.postmarketReviewItems = payload.items || [];
  state.postmarketReviewLastGeneratedAt = latestGeneratedAt(state.postmarketReviewItems);
  const exportLink = document.getElementById("postmarket-review-export");
  if (exportLink) exportLink.href = `/api/themelab/postmarket-review/export?trade_date=${encodeURIComponent(payload.trade_date || "")}&format=csv`;
  renderPostmarketReviewPanel();
  return state.postmarketReviewItems;
}

async function rebuildPostmarketReview(options = {}) {
  if (!state.actionCatalog.REBUILD_POSTMARKET_REVIEW) {
    state.actionCatalog.REBUILD_POSTMARKET_REVIEW = {
      action_type: "REBUILD_POSTMARKET_REVIEW",
      label_ko: "Post-market 리뷰 재생성",
      risk_level: "MEDIUM",
      requires_token: true,
      confirmation_required: true,
      endpoint: "/api/themelab/postmarket-review/rebuild",
    };
  }
  const context = {
    trade_date: options.trade_date || state.postmarketReviewSummary.trade_date || "",
    review_scope: options.review_scope || "postmarket",
    force: options.force !== false,
  };
  return executeOperatorAction("REBUILD_POSTMARKET_REVIEW", context, options);
}

function renderPostmarketReviewPanel() {
  renderPostmarketReviewSummary(state.postmarketReviewSummary || {});
  renderMissedOpportunityList(state.postmarketReviewItems || []);
  renderGoodBlockList(state.postmarketReviewItems || []);
  renderReviewNeededList(state.postmarketReviewItems || []);
  renderProtectedFromChaseList(state.postmarketReviewItems || []);
  renderDataInsufficientList(state.postmarketReviewItems || []);
  renderBlockReasonSummary(state.postmarketReviewSummary || {});
  renderPostmarketReviewDetail(state.postmarketReviewSelectedItem);
  const loading = state.postmarketReviewLoading ? "loading" : "";
  const generatedAt = state.postmarketReviewLastGeneratedAt || latestGeneratedAt(state.postmarketReviewItems);
  text("postmarket-review-status", loading || (generatedAt ? `generated ${formatEventTime(generatedAt)}` : "no review data"));
}

function renderPostmarketReviewSummary(summary = {}) {
  const node = document.getElementById("postmarket-review-summary");
  if (!node) return;
  const cards = [
    ["Missed Opportunity", summary.missed_opportunity_count || 0, "outcome-missed"],
    ["Good Block", summary.good_block_count || 0, "outcome-good-block"],
    ["Review Needed", summary.review_needed_count || 0, "outcome-review-needed"],
    ["Protected", summary.protected_from_chase_count || 0, "outcome-protected"],
    ["Data Insufficient", summary.data_insufficient_count || 0, "outcome-data-insufficient"],
    ["READY no order", summary.ready_without_order_count || 0, ""],
  ];
  node.innerHTML = cards.map(([label, value, tone]) => `
    <div class="postmarket-summary-card ${tone}">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </div>
  `).join("");
}

function renderMissedOpportunityList(items) {
  renderReviewTable("missed-opportunity-list", items, "MISSED_OPPORTUNITY");
}

function renderGoodBlockList(items) {
  renderReviewTable("good-block-list", items, "GOOD_BLOCK");
}

function renderReviewNeededList(items) {
  renderReviewTable("review-needed-list", items, "REVIEW_NEEDED");
}

function renderProtectedFromChaseList(items) {
  renderReviewTable("protected-from-chase-list", items, "PROTECTED_FROM_CHASE");
}

function renderDataInsufficientList(items) {
  renderReviewTable("data-insufficient-list", items, "DATA_INSUFFICIENT");
}

function renderReviewTable(id, items, outcomeLabel) {
  const node = document.getElementById(id);
  if (!node) return;
  const visible = reviewOutcomeVisible(outcomeLabel);
  node.closest(".postmarket-review-list-section")?.classList.toggle("hidden", !visible);
  if (!visible) return;
  const rows = (items || []).filter((item) => item.outcome_label === outcomeLabel).slice(0, 12);
  node.innerHTML = rows.length ? rows.map((item) => reviewRow(item)).join("") : `<div class="operator-alert-empty">해당 분류가 없습니다.</div>`;
}

function reviewRow(item) {
  const selected = state.postmarketReviewSelectedItem && state.postmarketReviewSelectedItem.review_id === item.review_id;
  return `
    <button type="button" class="review-row ${selected ? "selected" : ""}" data-review-id="${escapeHtml(item.review_id || "")}">
      <span>${escapeHtml(formatEventTime(item.base_time || item.generated_at))}</span>
      <strong>${escapeHtml(item.stock_name || item.symbol || "-")}</strong>
      <span>${escapeHtml(item.primary_theme || "-")} · ${escapeHtml(item.event_type || "-")}</span>
      <span class="${returnClass(item.return_3m_pct)}">${formatReturnPct(item.return_3m_pct)}</span>
      <span class="${returnClass(item.return_5m_pct)}">${formatReturnPct(item.return_5m_pct)}</span>
      ${confidenceBadge(item.confidence)}
    </button>
  `;
}

function renderBlockReasonSummary(summary = {}) {
  const node = document.getElementById("block-reason-summary");
  if (!node) return;
  const rows = summary.by_block_reason || [];
  node.innerHTML = rows.length ? rows.slice(0, 12).map((item) => `
    <div class="block-reason-row">
      <span>${escapeHtml(item.block_reason || "UNKNOWN")}</span>
      <strong>${escapeHtml(item.count || 0)}</strong>
    </div>
  `).join("") : `<div class="operator-alert-empty">차단 사유 집계가 없습니다.</div>`;
}

function renderPostmarketReviewDetail(item) {
  const node = document.getElementById("postmarket-review-detail");
  if (!node) return;
  if (!item) {
    node.innerHTML = `<div class="operator-alert-empty">리뷰 항목을 선택하세요.</div>`;
    return;
  }
  node.innerHTML = [
    actionLine("Outcome", `${outcomeLabelKo(item.outcome_label)} / ${item.confidence || "LOW"}`),
    actionLine("종목", `${item.stock_name || "-"} ${item.symbol ? `[${item.symbol}]` : ""}`),
    actionLine("이벤트", `${item.event_type || "-"} · ${formatEventTime(item.base_time || item.generated_at)}`),
    actionLine("사유", item.block_reason || "-"),
    actionLine("기준가", item.base_price ?? "-"),
    actionLine("+1m / +3m", `${formatReturnPct(item.return_1m_pct)} / ${formatReturnPct(item.return_3m_pct)}`),
    actionLine("+5m / +10m", `${formatReturnPct(item.return_5m_pct)} / ${formatReturnPct(item.return_10m_pct)}`),
    actionLine("close/last", formatReturnPct(item.return_close_or_last_pct)),
    actionLine("추천", item.recommendation_ko || "-"),
    `<pre>${escapeHtml(JSON.stringify((item.payload || {}).event || item.payload || {}, null, 2)).slice(0, 2400)}</pre>`,
  ].join("");
}

function formatReturnPct(value) {
  if (value == null || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function outcomeLabelKo(label) {
  return {
    MISSED_OPPORTUNITY: "Missed Opportunity",
    GOOD_BLOCK: "Good Block",
    REVIEW_NEEDED: "Review Needed",
    DATA_INSUFFICIENT: "Data Insufficient",
    PROTECTED_FROM_CHASE: "Protected from Chase",
    NEUTRAL: "Neutral",
  }[String(label || "").toUpperCase()] || label || "-";
}

function confidenceBadge(confidence) {
  const label = String(confidence || "LOW").toUpperCase();
  const tone = label === "HIGH" ? "confidence-high" : label === "MEDIUM" ? "confidence-medium" : "confidence-low";
  return `<span class="confidence-badge ${tone}">${escapeHtml(label)}</span>`;
}

function selectReviewItem(item) {
  state.postmarketReviewSelectedItem = item || null;
  if (item) {
    state.selectedEventId = item.event_id || state.selectedEventId;
    state.selectedActionContext = {
      event_id: item.event_id || "",
      event_type: item.event_type || "",
      symbol: item.symbol || "",
      stock_name: item.stock_name || "",
      candidate_instance_id: item.candidate_instance_id || "",
    };
    if (item.symbol && state.snapshot) selectSymbol(item.symbol, { source: "postmarket-review" });
    fetchActionRecommendations(state.selectedActionContext).catch(() => {});
  }
  renderPostmarketReviewPanel();
}

function reviewOutcomeVisible(outcomeLabel) {
  const selected = state.postmarketReviewFilters.outcome_label || "ALL";
  return selected === "ALL" || selected === outcomeLabel;
}

function returnClass(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || Math.abs(number) < 0.05) return "return-neutral";
  return number > 0 ? "return-positive" : "return-negative";
}

function latestGeneratedAt(items = []) {
  return (items || []).map((item) => item.generated_at).filter(Boolean).sort().at(-1) || "";
}

async function initPostmarketReviewPanel() {
  state.postmarketReviewLoading = true;
  renderPostmarketReviewPanel();
  try {
    await Promise.all([fetchPostmarketReviewSummary(), fetchPostmarketReviewItems()]);
  } catch (error) {
    text("postmarket-review-status", `review load failed: ${error.message || error}`);
    renderPostmarketReviewPanel();
  } finally {
    state.postmarketReviewLoading = false;
    renderPostmarketReviewPanel();
  }
}

function initPromotionCockpit() {
  renderPromotionDecisionPanel();
  document.getElementById("promotion-window-controls")?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-promotion-window]");
    if (!button) return;
    state.promotionWindowSec = Number(button.dataset.promotionWindow || 0);
    state.promotionDrilldown = null;
    state.promotionDrilldownError = "";
    renderPromotionWindowButtons();
    fetchPromotionDecision(state.snapshot || {}).catch(() => {});
  });
  document.getElementById("promotion-decision-refresh")?.addEventListener("click", () => {
    fetchPromotionDecision(state.snapshot || {}).catch(() => {});
  });
  document.getElementById("promotion-blocker-list")?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-promotion-blocker]");
    if (!button) return;
    state.promotionSelectedBlocker = button.dataset.promotionBlocker || "";
    renderPromotionDecisionPanel();
    fetchPromotionDrilldown(state.snapshot || {}).catch(() => {});
  });
}

function renderRunbook(context) {
  const runbook = context && context.steps_ko ? context : runbookForStatus(String((context || {}).event_type || context || "GENERAL"));
  state.runbook = runbook;
  text("operator-runbook-title", runbook.title_ko || "Runbook");
  const node = document.getElementById("operator-runbook-body");
  if (!node) return;
  node.innerHTML = (runbook.steps_ko || []).map((step) => `
    <div class="runbook-step"><span>${escapeHtml(step)}</span></div>
  `).join("") || `<div class="operator-alert-empty">Runbook 단계가 없습니다.</div>`;
}

function runbookForEvent(event) {
  return runbookForStatus((event || {}).event_type || (event || {}).type || (event || {}).display_status || "GENERAL");
}

function runbookForStatus(status) {
  const key = String(status || "GENERAL").toUpperCase();
  const map = {
    GATEWAY_DISCONNECTED: ["Gateway 연결 복구", ["Gateway 상태를 확인합니다.", "heartbeat, 로그인, orderable 상태를 봅니다.", "Gateway 실행 후 30초 내 회복 여부를 확인합니다."]],
    SNAPSHOT_STALE: ["스냅샷 지연 복구", ["Runtime 준비 상태를 확인합니다.", "Runtime 1회 평가를 실행합니다.", "snapshot_age가 줄어드는지 확인합니다."]],
    READY_BUT_LIVE_BLOCKED: ["LIVE Guard 차단 점검", ["차단 사유를 확인합니다.", "주문 실행이나 Guard 우회는 하지 않습니다.", "운영 메모로 판단 근거를 남깁니다."]],
    DATA_QUALITY_DEGRADED: ["데이터 품질 저하 점검", ["tick, VWAP, support 상태를 확인합니다.", "스냅샷 새로고침 또는 Runtime 1회 평가를 실행합니다.", "회복 후 READY 전환을 확인합니다."]],
    CHASE_RISK_BLOCKED: ["추격 리스크 차단 점검", ["late_chase_level을 확인합니다.", "강제 매수하지 않습니다.", "재확인 시점까지 보류할 수 있습니다."]],
    LATE_CHASE_TEMP_WAIT: ["추격 대기 점검", ["재확인 시간을 확인합니다.", "즉시 주문하지 않습니다.", "필요하면 운영 메모를 남깁니다."]],
    MARKET_WAIT_STARTED: ["시장 대기 점검", ["시장 breadth와 후보 시장을 확인합니다.", "시장 회복 조건을 기다립니다.", "재확인 시점까지 보류할 수 있습니다."]],
  };
  const [title, steps] = map[key] || ["일반 운영 점검", ["스냅샷과 Runtime 준비 상태를 확인합니다.", "필요하면 이벤트를 ACK 처리하고 메모를 남깁니다.", "LIVE 주문/취소/Guard 우회는 지원하지 않습니다."]];
  return { key, title_ko: title, steps_ko: steps };
}

async function fetchBuyZeroRca(options = {}) {
  const force = Boolean(options.force);
  if (state.buyZeroRca.loading) return;
  if (!force && Date.now() - state.buyZeroRca.lastFetchedAt < BUY_ZERO_RCA_REFRESH_MS) return;
  state.buyZeroRca.loading = true;
  setBuyZeroStatus("조회 중", "warning");
  try {
    const tradeDate = localTradeDate();
    const params = new URLSearchParams({ trade_date: tradeDate });
    const [summaryResponse, readyResponse, rallyResponse] = await Promise.all([
      fetch(`/api/runtime/buy-zero/summary?${params.toString()}`, { cache: "no-store" }),
      fetch(`/api/runtime/buy-zero/ready-not-ordered?${params.toString()}&limit=100`, { cache: "no-store" }),
      fetch(`/api/runtime/buy-zero/missed-opportunities?${params.toString()}&limit=100`, { cache: "no-store" }),
    ]);
    if (!summaryResponse.ok) throw new Error(`summary ${summaryResponse.status}`);
    if (!readyResponse.ok) throw new Error(`ready ${readyResponse.status}`);
    if (!rallyResponse.ok) throw new Error(`rally ${rallyResponse.status}`);
    const summaryPayload = await summaryResponse.json();
    const readyPayload = await readyResponse.json();
    const rallyPayload = await rallyResponse.json();
    state.buyZeroRca.summary = summaryPayload.summary || {};
    state.buyZeroRca.readyRows = readyPayload.items || [];
    state.buyZeroRca.rallyRows = rallyPayload.top_observe_then_rally_candidates || [];
    state.buyZeroRca.tradeDate = tradeDate;
    state.buyZeroRca.lastFetchedAt = Date.now();
    renderBuyZeroRcaPanel();
  } catch (error) {
    setBuyZeroStatus(`조회 실패: ${error.message || error}`, "critical");
  } finally {
    state.buyZeroRca.loading = false;
  }
}

function setBuyZeroStatus(label, tone = "observe") {
  const node = document.getElementById("themelab-buy-zero-rca-status");
  if (!node) return;
  node.textContent = label;
  node.className = `badge ${tone}`;
}

function renderBuyZeroRcaPanel() {
  const payload = state.buyZeroRca.summary || {};
  const summary = payload.summary || payload;
  const available = Boolean(payload.available) || Number(payload.total_trace_events || 0) > 0;
  const liveSubmitted = Number(summary.live_sim_submitted_count ?? payload.live_sim_submitted_count ?? 0);
  const buyZero = liveSubmitted === 0;
  const empty = document.getElementById("themelab-buy-zero-rca-empty");
  if (empty) {
    empty.hidden = available;
    empty.textContent = "아직 RCA trace가 없습니다. PR 적용 이후 이벤트부터 쌓입니다.";
  }
  text("themelab-buy-zero-rca-note", "trace는 PR 적용 이후 데이터만 표시하며, 과거 DB backfill은 하지 않습니다.");
  if (!available) {
    const traceDetail = document.getElementById("themelab-buy-zero-rca-trace-detail");
    if (traceDetail) traceDetail.hidden = true;
  }
  setBuyZeroStatus(!available ? "TRACE 대기" : buyZero ? "오늘 LIVE_SIM 매수 0건" : "LIVE_SIM 주문 발생", !available ? "observe" : buyZero ? "warning" : "ready");
  text("themelab-buy-zero-rca-updated", `trace ${formatDateTime(summary.last_updated_at || payload.last_updated_at)}`);
  text("themelab-buy-zero-total-candidates", summary.total_candidates ?? payload.total_candidates ?? 0);
  text("themelab-buy-zero-gate-evaluated", summary.gate_evaluated_count ?? payload.gate_evaluated_count ?? 0);
  text("themelab-buy-zero-ready-counts", `${summary.ready_exact_count ?? payload.ready_exact_count ?? summary.ready_count ?? payload.ready_count ?? 0} / ${summary.ready_small_count ?? payload.ready_small_count ?? 0}`);
  text("themelab-buy-zero-wait-observe-counts", `${summary.wait_count ?? payload.wait_count ?? 0} / ${summary.observe_count ?? payload.observe_count ?? 0}`);
  text("themelab-buy-zero-blocked-count", summary.blocked_count ?? payload.blocked_count ?? 0);
  text("themelab-buy-zero-live-blocked-count", summary.live_sim_blocked_count ?? payload.live_sim_blocked_count ?? 0);
  renderBuyZeroInlineCounts("themelab-buy-zero-top-causes", payload.operator_top_3_causes || payload.top_block_reasons || [], "reason", "아직 집계된 차단 사유가 없습니다");
  renderBuyZeroDataQualityCounts("themelab-buy-zero-data-quality-blocks", payload);
  renderBuyZeroStageFunnel(payload.stage_funnel || []);
  renderBuyZeroReadyRows(state.buyZeroRca.readyRows, available);
  renderBuyZeroRallyRows(state.buyZeroRca.rallyRows, available);
  renderBuyZeroStageRows(state.buyZeroRca.stageRows, state.buyZeroRca.stage);
}

function renderBuyZeroInlineCounts(id, rows, key, emptyText) {
  const node = document.getElementById(id);
  if (!node) return;
  const items = (rows || []).slice(0, 5);
  node.innerHTML = items.length
    ? items.map((item) => `<div>${reasonBadge(item[key] || item.category || "-")} <strong>${escapeHtml(item.count ?? 0)}</strong></div>`).join("")
    : `<div class="muted">${escapeHtml(emptyText)}</div>`;
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
  const node = document.getElementById("themelab-buy-zero-stage-funnel");
  if (!node) return;
  const items = rows && rows.length ? rows : Object.keys(BUY_ZERO_RCA_STAGE_LABELS).map((stage) => ({ stage, total: 0, passed: 0, failed: 0, top_reason: "" }));
  const eventCount = items.reduce((total, item) => total + Number(item.total || 0), 0);
  text("themelab-buy-zero-stage-status", eventCount ? `${eventCount} events` : "stage 대기");
  node.innerHTML = items.map((item) => {
    const failed = Number(item.failed || 0);
    const tone = failed ? "warning" : Number(item.total || 0) ? "ready" : "observe";
    return `
      <button type="button" class="buy-zero-stage-step ${tone}" data-buy-zero-stage="${escapeHtml(item.stage || "")}">
        <span>${escapeHtml(BUY_ZERO_RCA_STAGE_LABELS[item.stage] || item.stage || "-")}</span>
        <strong>${escapeHtml(item.total ?? 0)}</strong>
        <em>${escapeHtml(item.passed ?? 0)} 통과 / ${escapeHtml(failed)} 차단</em>
        <small>${escapeHtml(item.top_reason || "차단 사유 없음")}</small>
      </button>
    `;
  }).join("");
  node.querySelectorAll("[data-buy-zero-stage]").forEach((button) => {
    button.addEventListener("click", () => fetchBuyZeroStageCandidates(button.dataset.buyZeroStage || "").catch((error) => {
      text("themelab-buy-zero-stage-status", `오류: ${error.message || error}`);
    }));
  });
}

async function fetchBuyZeroStageCandidates(stage) {
  if (!stage) return;
  state.buyZeroRca.stage = stage;
  text("themelab-buy-zero-stage-status", `${stage} 조회 중`);
  const params = new URLSearchParams({
    trade_date: state.buyZeroRca.tradeDate || localTradeDate(),
    stage,
    pass_fail: "FAIL",
    limit: "100",
  });
  const response = await fetch(`/api/runtime/buy-zero/traces?${params.toString()}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`stage ${response.status}`);
  const payload = await response.json();
  state.buyZeroRca.stageRows = payload.items || [];
  renderBuyZeroStageRows(state.buyZeroRca.stageRows, stage);
  text("themelab-buy-zero-stage-status", `${stage} 차단 ${state.buyZeroRca.stageRows.length}`);
}

function renderBuyZeroReadyRows(rows, traceAvailable) {
  const body = document.getElementById("themelab-buy-zero-ready-table-body");
  if (!body) return;
  text("themelab-buy-zero-ready-status", rows.length);
  if (!traceAvailable) {
    body.innerHTML = `<tr><td colspan="6" class="muted">아직 수집된 RCA trace가 없습니다</td></tr>`;
    return;
  }
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="6" class="muted">READY였지만 주문 안 나간 종목이 없습니다</td></tr>`;
    return;
  }
  body.innerHTML = rows.slice(0, 50).map((item, index) => `
    <tr data-buy-zero-ready-row="${index}">
      <td>${escapeHtml(`${item.code || "-"} ${item.name || ""}`.trim())}<div class="row-meta">${escapeHtml(item.theme_name || "-")} · ${shortId(item.candidate_instance_id || "")}</div></td>
      <td>${reasonBadge(item.classification || "-")}</td>
      <td>${badge(item.gate_status || "-")}</td>
      <td>${reasonBadges(item.reason_codes || [item.primary_block_reason].filter(Boolean))}</td>
      <td>${badge(item.dry_run_status || "-")}<div class="row-meta">${escapeHtml(item.live_sim_status || "-")} ${escapeHtml(item.live_sim_reason || "")}</div></td>
      <td>tick ${escapeHtml(optionalNumber(item.latest_tick_age_sec, 1))}s<div class="row-meta">support ${escapeHtml(yesNoUnknown(item.support_ready))}</div></td>
    </tr>
  `).join("");
  body.querySelectorAll("[data-buy-zero-ready-row]").forEach((row) => {
    row.addEventListener("click", () => {
      openBuyZeroTraceDetail(state.buyZeroRca.readyRows[Number(row.dataset.buyZeroReadyRow)]).catch((error) => {
        text("themelab-buy-zero-trace-status", `오류: ${error.message || error}`);
      });
    });
  });
}

function renderBuyZeroRallyRows(rows, traceAvailable) {
  const body = document.getElementById("themelab-buy-zero-rally-table-body");
  if (!body) return;
  text("themelab-buy-zero-rally-status", rows.length);
  if (!traceAvailable) {
    body.innerHTML = `<tr><td colspan="6" class="muted">아직 수집된 RCA trace가 없습니다</td></tr>`;
    return;
  }
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="6" class="muted">OBSERVE/BLOCKED 이후 급등 후보가 없습니다</td></tr>`;
    return;
  }
  body.innerHTML = rows.slice(0, 50).map((item, index) => `
    <tr data-buy-zero-rally-row="${index}">
      <td>${escapeHtml(`${item.code || "-"} ${item.name || ""}`.trim())}<div class="row-meta">${escapeHtml(item.theme_name || "-")}</div></td>
      <td>${badge(item.status || "-")}</td>
      <td>${reasonBadges(item.reason_codes || [item.primary_reason].filter(Boolean))}</td>
      <td>${pct(item.return_5m_pct)} / ${pct(item.return_15m_pct)} / ${pct(item.return_30m_pct)}</td>
      <td>${pct(item.mfe_15m_pct)} / ${pct(item.mae_15m_pct)}</td>
      <td>${escapeHtml(item.price_location_status || "-")}<div class="row-meta">${item.missed_opportunity ? "기회손실" : "기회손실 아님"}</div></td>
    </tr>
  `).join("");
  body.querySelectorAll("[data-buy-zero-rally-row]").forEach((row) => {
    row.addEventListener("click", () => {
      openBuyZeroTraceDetail(state.buyZeroRca.rallyRows[Number(row.dataset.buyZeroRallyRow)]).catch((error) => {
        text("themelab-buy-zero-trace-status", `오류: ${error.message || error}`);
      });
    });
  });
}

function renderBuyZeroStageRows(rows, stage) {
  const body = document.getElementById("themelab-buy-zero-stage-candidates-body");
  if (!body) return;
  text("themelab-buy-zero-stage-title", stage ? `${stage} 차단 후보` : "Stage 차단 후보");
  if (!stage) {
    body.innerHTML = `<div class="muted">퍼널 단계를 클릭하면 해당 단계에서 막힌 종목을 표시합니다</div>`;
    return;
  }
  if (!rows.length) {
    body.innerHTML = `<div class="muted">${escapeHtml(stage)} 단계에서 막힌 trace가 없습니다</div>`;
    return;
  }
  body.innerHTML = rows.slice(0, 50).map((item, index) => `
    <button type="button" data-buy-zero-stage-row="${index}">
      <strong>${escapeHtml(`${item.code || "-"} ${item.name || ""}`.trim())}</strong>
      <span>${badge(item.stage_status || item.pass_fail || "-")} ${reasonBadges(item.reason_codes || [item.primary_block_reason].filter(Boolean))}</span>
    </button>
  `).join("");
  body.querySelectorAll("[data-buy-zero-stage-row]").forEach((row) => {
    row.addEventListener("click", () => {
      openBuyZeroTraceDetail(state.buyZeroRca.stageRows[Number(row.dataset.buyZeroStageRow)]).catch((error) => {
        text("themelab-buy-zero-trace-status", `오류: ${error.message || error}`);
      });
    });
  });
}

async function openBuyZeroTraceDetail(item) {
  if (!item) return;
  const detail = document.getElementById("themelab-buy-zero-rca-trace-detail");
  if (detail) detail.hidden = false;
  text("themelab-buy-zero-trace-title", `Trace ${item.code || ""} ${item.name || ""}`.trim());
  text("themelab-buy-zero-trace-status", "조회 중");
  const params = new URLSearchParams({
    trade_date: state.buyZeroRca.tradeDate || localTradeDate(),
    limit: "200",
  });
  if (item.candidate_instance_id) params.set("candidate_instance_id", item.candidate_instance_id);
  else if (item.code) params.set("code", item.code);
  const response = await fetch(`/api/runtime/buy-zero/traces?${params.toString()}`, { cache: "no-store" });
  if (!response.ok) {
    text("themelab-buy-zero-trace-status", `오류 ${response.status}`);
    return;
  }
  const payload = await response.json();
  renderBuyZeroTraceDetail(item, payload.items || []);
}

function renderBuyZeroTraceDetail(selected, rows) {
  const summary = document.getElementById("themelab-buy-zero-trace-summary");
  const timeline = document.getElementById("themelab-buy-zero-rca-timeline");
  const ordered = [...rows].sort((left, right) => String(left.created_at || "").localeCompare(String(right.created_at || "")));
  text("themelab-buy-zero-trace-status", `${ordered.length} events`);
  if (summary) {
    summary.innerHTML = [
      `<div><span>종목</span><strong>${escapeHtml(`${selected.code || "-"} ${selected.name || ""}`.trim())}</strong></div>`,
      `<div><span>Candidate</span><strong>${shortId(selected.candidate_instance_id || "-")}</strong></div>`,
      `<div><span>분류</span><strong>${escapeHtml(selected.classification || selected.status || "-")}</strong></div>`,
      `<div><span>테마</span><strong>${escapeHtml(selected.theme_name || "-")}</strong></div>`,
    ].join("");
  }
  if (!timeline) return;
  if (!ordered.length) {
    timeline.innerHTML = `<div class="muted">해당 종목의 RCA trace가 없습니다</div>`;
    return;
  }
  timeline.innerHTML = ordered.map((item) => `
    <article class="buy-zero-trace-event ${String(item.pass_fail || "").toLowerCase()}">
      <header><strong>${escapeHtml(item.stage || "-")}</strong>${badge(item.stage_status || item.pass_fail || "-")}<time>${escapeHtml(formatDateTime(item.created_at))}</time></header>
      <p>${escapeHtml(buyZeroTraceMessage(item))}</p>
      <div class="buy-zero-trace-fields">
        <span>사유 ${reasonBadges(item.reason_codes || [item.primary_block_reason].filter(Boolean))}</span>
        <span>Gate ${escapeHtml(item.gate_status || "-")} / score ${escapeHtml(optionalNumber(item.gate_score, 1))}</span>
        <span>Data ${escapeHtml(item.data_quality_bucket || "-")} / ${escapeHtml(item.data_quality_action || "-")}</span>
        <span>Missing ${reasonBadges([...(item.missing_core_fields || []), ...(item.missing_entry_fields || []), ...(item.missing_optional_fields || [])])}</span>
        <span>Early-small ${escapeHtml(yesNoUnknown(item.early_small_candidate))} / order ${escapeHtml(yesNoUnknown(item.early_small_order_enabled))} / x${escapeHtml(optionalNumber(item.early_small_position_size_multiplier, 2))}</span>
        <span>Tick ${escapeHtml(yesNoUnknown(item.latest_tick_ready))} / ${escapeHtml(optionalNumber(item.latest_tick_age_sec, 1))}s</span>
        <span>Support ${escapeHtml(yesNoUnknown(item.support_ready))} / ${escapeHtml(item.selected_support_source || "-")}</span>
        <span>DRY_RUN ${escapeHtml(item.dry_run_status || "-")} ${escapeHtml(item.dry_run_reason || "")}</span>
        <span>LIVE_SIM ${escapeHtml(item.live_sim_status || "-")} ${escapeHtml(item.live_sim_reason || "")}</span>
        <span>Early-small 거절 ${escapeHtml(item.early_small_rejected_reason || "-")}</span>
        <span>Command ${shortId(item.command_id || "-")}</span>
        <span>Broker ${shortId(item.broker_order_id || "-")}</span>
      </div>
    </article>
  `).join("");
}

function buyZeroTraceMessage(item) {
  const textValue = [
    item.primary_block_reason,
    item.dry_run_reason,
    item.live_sim_reason,
    item.data_quality_bucket,
    item.data_quality_action,
    ...(item.reason_codes || []),
  ].join(" ").toUpperCase();
  if (item.operator_message_ko && item.data_quality_bucket) return item.operator_message_ko;
  if (/CORE_BLOCKING/.test(textValue)) return "핵심 실시간 데이터 부족으로 주문 금지";
  if (/ENTRY_BLOCKING/.test(textValue)) return "진입 판단 데이터 부족으로 WAIT_DATA";
  if (/WARMUP_OPTIONAL/.test(textValue)) return "보조 warmup 부족으로 early-small 후보 관찰";
  if (/BACKFILL_ONLY_OBSERVE/.test(textValue)) return "TR backfill만 있어 실시간 확인 전까지 관찰";
  if (/LATEST_TICK|TICK_.*OLD|REALTIME_RELIABILITY/.test(textValue)) return "실시간 틱이 오래되어 주문 보류";
  if (/SUPPORT_NOT_READY|SUPPORT/.test(textValue) && item.entry_plan_diagnostic_only) return "지지선 미확정으로 진단 전용";
  if (/LIVE_SIM|ACCOUNT_GUARD|EXIT_GUARD|KILL_SWITCH/.test(textValue)) return "LIVE_SIM guard에서 차단";
  if (/GATE_RESULT_KEY_MISMATCH|CANDIDATE_STATE/.test(textValue)) return "READY였지만 candidate state/gate_result_key 불일치";
  if (/DUPLICATE/.test(textValue)) return "중복 주문 방지로 차단";
  if (/CHASE|LATE_CHASE|VWAP_OVEREXTENDED/.test(textValue)) return "추격 위험으로 OBSERVE";
  if (String(item.pass_fail || "").toUpperCase() === "PASS") return "이 단계는 통과";
  return item.primary_block_reason || item.stage_status || "trace event";
}

function appendActionResultToEvents(result) {
  const action = (result || {}).action || {};
  const status = String((result || {}).status || action.status || "INFO").toUpperCase();
  const eventType = status === "SUCCESS" ? "ACTION_EXECUTED" : status === "BLOCKED" ? "ACTION_BLOCKED" : "ACTION_FAILED";
  const event = normalizeOperatorEvent({
    id: `operator-action:${action.action_id || Date.now()}:${status}`,
    event_id: `operator-action:${action.action_id || Date.now()}:${status}`,
    event_type: eventType,
    type: eventType,
    severity: status === "SUCCESS" ? "INFO" : "WARNING",
    category: "action",
    symbol: action.symbol || "",
    candidate_instance_id: action.candidate_instance_id || "",
    message: `운영 액션 ${status}: ${action.action_type || ""}`,
    created_at: new Date().toISOString(),
    persisted: true,
  }, { persisted: true });
  mergeOperatorEvents([event], { persisted: true });
  renderOperatorAlerts();
  renderDecisionTimeline();
  renderOperatorEventJournal();
}

function actionContextFromEvent(event) {
  return {
    event_id: event.id || event.event_id || "",
    event_type: event.type || event.event_type || "",
    symbol: event.symbol || "",
    stock_name: event.stock_name || "",
    candidate_instance_id: event.candidate_instance_id || "",
  };
}

function actionContextFromCandidate(item = {}) {
  return {
    event_id: state.selectedEventId || "",
    symbol: item.symbol || state.selectedSymbol || "",
    stock_name: item.stock_name || item.name || "",
    candidate_instance_id: item.candidate_instance_id || "",
    gate_status: item.gate_status || "",
    display_status: item.display_status || item.gate_status || "",
  };
}

function formatEventTime(value) {
  const date = new Date(value || Date.now());
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function render(snapshot) {
  const currentSnapshot = snapshot || {};
  const previousSnapshot = state.previousSnapshot;
  const additions = previousSnapshot ? appendOperatorEvents(deriveOperatorEvents(previousSnapshot, currentSnapshot)) : [];
  if (additions.length) {
    persistOperatorEvents(additions).catch((error) => {
      updateOperatorSyncStatus(`저장 실패: ${error.message || error}`);
    });
  }
  state.snapshot = currentSnapshot;
  const selected = selectedWatchItem(state.snapshot);
  state.selectedSymbol = selected.symbol || state.selectedSymbol || "";
  const selectedChart = selectedChartForSymbol(state.snapshot, state.selectedSymbol);
  renderHeader(currentSnapshot);
  renderCockpit(currentSnapshot);
  renderBuyZeroRcaPanel();
  renderShadowAb(currentSnapshot);
  renderThemes(currentSnapshot.ranked_themes || []);
  renderWatchset(currentSnapshot.watchset || []);
  renderOrders(currentSnapshot.entry_candidates || []);
  renderFocusPanel(selected, selectedChart);
  renderChart(selectedChart);
  renderGate(selected);
  renderConditions(currentSnapshot.condition_statuses || []);
  renderDataQuality(currentSnapshot.data_quality || {});
  renderOperatorAlerts();
  renderDecisionTimeline();
  renderOperatorSessionReview();
  renderOperatorEventJournal();
  if (!state.naverThemeSyncBusy) renderNaverThemeSyncStatus(currentSnapshot.theme_source_sync || {});
  updateNaverThemeSyncButton();
  updateKiwoomGatewayButton(currentSnapshot);
  state.previousSnapshot = currentSnapshot;
}

function renderHeader(snapshot) {
  const market = snapshot.market || {};
  const summary = snapshot.summary || {};
  const dataQuality = snapshot.data_quality || {};
  const backfill = snapshot.theme_backfill_runtime || {};
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
  const backfill = snapshot.theme_backfill_runtime || {};
  const gateway = snapshot.gateway || {};
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
    `<div class="cockpit-line"><strong>Kiwoom</strong>${boolBadge(gateway.kiwoom_logged_in, "\ub85c\uadf8\uc778", "\ubbf8\ub85c\uadf8\uc778")}<span>${gateway.heartbeat_ok ? "heartbeat OK" : "heartbeat wait"} · ${gateway.connected ? "connected" : "disconnected"}</span></div>`,
    `<div class="cockpit-line"><strong>Snapshot</strong>${badge(summary.snapshot_stale ? "SNAPSHOT_STALE" : "OK")}<span>${escapeHtml(summary.snapshot_age_label || "-")}</span></div>`,
    `<div class="cockpit-line"><strong>TR_BACKFILL</strong>${boolBadge(backfill.enabled, "ON", "OFF")}<span>${escapeHtml(backfillStatusText(backfill))} · parser miss ${ratio(backfill.parser_miss_ratio)} · ${escapeHtml(backfill.history_window || "recent_500_commands")}</span></div>`,
    `<div class="cockpit-line"><strong>LIVE</strong>${boolBadge(summary.live_order_enabled, "활성", "비활성")}</div>`,
    countLine("Guard 통과", summary.live_guard_passed_count),
    countLine("Guard 차단", summary.live_guard_blocked_count),
    countLine("추격 대기", summary.late_chase_wait_count),
    countLine("추격 차단", summary.chase_risk_blocked_count),
  ].join("");
}

function renderPromotionDecisionPanel() {
  const panel = document.getElementById("promotion-cockpit");
  if (!panel) return;
  const payload = state.promotionDecision || {};
  const decision = payload.decision || {};
  const evidence = payload.evidence || {};
  const metrics = decision.metrics || {};
  const filters = payload.filters || {};
  const rollout = decision.rollout_plan || {};
  const blockers = decision.blockers || [];
  const warnings = decision.warnings || [];
  const blockerDetails = promotionReasonDetailMap(decision.blocker_details || []);
  const warningDetails = promotionReasonDetailMap(decision.warning_details || []);
  const hasDecision = Boolean(decision.action || payload.action);
  const status = state.promotionDecisionError
    ? "ERROR"
    : state.promotionDecisionLoading && !hasDecision
      ? "LOADING"
      : decision.action || payload.action || "NO_DATA";

  setBadge("promotion-action-status", status);
  const actionStatusNode = document.getElementById("promotion-action-status");
  if (actionStatusNode) actionStatusNode.textContent = promotionActionLabel(status);
  text("promotion-confidence", hasDecision ? `승격확신 ${ratio(decision.confidence ?? payload.confidence)}` : "승격확신 -");
  text("promotion-decision-message", promotionDecisionMessage(payload, decision, filters));
  renderPromotionWindowButtons();

  const currentStage = decision.current_stage || payload.current_stage || evidence.current_stage || "-";
  const targetStage = decision.target_stage || payload.target_stage || "-";
  const recommendedStage = decision.recommended_stage || payload.recommended_stage || "-";
  const stageNode = document.getElementById("promotion-stage-lines");
  if (stageNode) {
    stageNode.innerHTML = [
      `<div class="cockpit-line"><strong>현재</strong>${promotionStageBadge(currentStage)}<span>${escapeHtml(currentStage)}</span></div>`,
      `<div class="cockpit-line"><strong>목표</strong>${promotionStageBadge(targetStage)}<span>${escapeHtml(targetStage)}</span></div>`,
      `<div class="cockpit-line"><strong>추천</strong>${promotionStageBadge(recommendedStage)}<span>${escapeHtml(recommendedStage)}</span></div>`,
      `<div class="cockpit-line"><strong>근거</strong><span>${escapeHtml(promotionWindowLabel(filters))}</span></div>`,
    ].join("");
  }

  const metricNode = document.getElementById("promotion-metric-lines");
  if (metricNode) {
    metricNode.innerHTML = [
      promotionMetric("판단 수", metrics.decision_count ?? evidence.decision_count, compactNumber),
      promotionMetric("거래일", metrics.trade_day_count ?? evidence.trade_day_count, compactNumber),
      promotionMetric("실시간 HIGH", metrics.realtime_high_ratio, ratio),
      promotionMetric("기회손실", metrics.opportunity_loss_rate, ratio),
      promotionMetric("오진입", metrics.false_positive_rate, ratio),
      promotionMetric("주문오류", metrics.order_error_rate, ratio),
      promotionMetric("평균수익", metrics.avg_return_pct ?? evidence.avg_return_pct, pct),
      promotionMetric("LOW 후 손실", metrics.realtime_low_missed_count ?? evidence.realtime_low_missed_count, compactNumber),
    ].join("");
  }

  const blockerNode = document.getElementById("promotion-blocker-list");
  if (blockerNode) {
    const rows = [
      ...blockers.slice(0, 5).map((item) => promotionReasonRow(item, "blocked", blockerDetails[item])),
      ...warnings.slice(0, 3).map((item) => promotionReasonRow(item, "warning", warningDetails[item])),
    ];
    blockerNode.innerHTML = rows.length
      ? rows.join("")
      : hasDecision
        ? `<div class="promotion-reason-row ready"><strong>READY</strong><span>승급 조건 충족</span></div>`
        : `<div class="operator-alert-empty">판단 데이터 대기</div>`;
  }

  const rolloutNode = document.getElementById("promotion-rollout-lines");
  if (rolloutNode) {
    rolloutNode.innerHTML = [
      `<div class="cockpit-line"><strong>Mode</strong><span>${escapeHtml(rollout.mode || "-")}</span></div>`,
      `<div class="cockpit-line"><strong>금액</strong><span>${money(rollout.order_notional_krw)}</span></div>`,
      `<div class="cockpit-line"><strong>종목수</strong><span>${compactNumber(rollout.max_symbols)}</span></div>`,
      `<div class="cockpit-line"><strong>승인</strong>${boolBadge(rollout.requires_operator_approval, "필요", "불필요")}</div>`,
      state.promotionDecisionLastFetchedAt
        ? `<div class="cockpit-line"><strong>갱신</strong><span>${escapeHtml(formatEventTime(state.promotionDecisionLastFetchedAt))}</span></div>`
        : "",
    ].filter(Boolean).join("");
  }
  renderPromotionStageMatrix(payload);
  renderPromotionDrilldownPanel();
}

function promotionDecisionMessage(payload = {}, decision = {}, filters = {}) {
  if (state.promotionDecisionError) return `Promotion API 실패: ${state.promotionDecisionError}`;
  if (state.promotionDecisionLoading && !(decision.action || payload.action)) return "승급 판단 갱신 중";
  const action = decision.action || payload.action;
  if (!action) return "승급 판단 대기";
  const current = decision.current_stage || payload.current_stage || "-";
  const recommended = decision.recommended_stage || payload.recommended_stage || "-";
  const blockers = decision.blockers || [];
  const blockerDetails = promotionReasonDetailMap(decision.blocker_details || []);
  const blockerText = blockers.length
    ? ` / 확인 필요 ${blockers.slice(0, 2).map((item) => promotionReasonLabel(item, blockerDetails[item])).join(", ")}`
    : "";
  return `${promotionActionLabel(action)}: ${current} → ${recommended} / ${promotionWindowLabel(filters)}${blockerText}`;
}

function promotionActionLabel(action) {
  return {
    PROMOTE: "승격",
    HOLD: "보류",
    DEMOTE: "강등",
    BLOCK: "차단",
    LOADING: "갱신 중",
    NO_DATA: "데이터 대기",
    ERROR: "오류",
  }[String(action || "").toUpperCase()] || String(action || "-");
}

function promotionActionBadge(action, tone = "") {
  return `<span class="badge ${tone || statusClass[action] || "observe"}">${escapeHtml(promotionActionLabel(action))}</span>`;
}

function promotionWindowLabel(filters = {}) {
  const windowSec = Number(filters.window_sec || state.promotionWindowSec || 0);
  if (windowSec > 0) return `최근 ${Math.round(windowSec / 60)}분`;
  return `거래일 ${filters.trade_date || "최근 데이터"}`;
}

function renderPromotionStageMatrix(payload = {}) {
  const node = document.getElementById("promotion-stage-matrix");
  if (!node) return;
  const rows = (((payload || {}).stage_matrix || {}).rows || []);
  if (!rows.length) {
    node.innerHTML = `<div class="operator-alert-empty">Stage matrix 데이터 대기</div>`;
    return;
  }
  node.innerHTML = rows.map((row) => promotionStageMatrixRow(row)).join("");
}

function promotionStageMatrixRow(row = {}) {
  const failedChecks = row.failed_checks || [];
  const blockers = row.blockers || [];
  const blockerDetails = promotionReasonDetailMap(row.blocker_details || []);
  const metrics = row.metrics || {};
  const transition = row.transition_type === "maintain"
    ? `${String(row.current_stage || "-")} 유지 점검`
    : `${String(row.current_stage || "-")} → ${String(row.target_stage || "-")}`;
  const outcomeTone = row.action === "PROMOTE"
    ? "ready"
    : row.action === "DEMOTE"
      ? "blocked"
      : blockers.length
        ? "wait"
        : "ready-small";
  const requirementLines = failedChecks.length
    ? failedChecks.slice(0, 4).map((check) => `<span>${escapeHtml(promotionRequirementText(check))}</span>`).join("")
    : `<span>필수 조건 충족</span>`;
  const blockerText = blockers.length
    ? blockers.slice(0, 3).map((item) => promotionReasonLabel(item, blockerDetails[item])).join(", ")
    : "조건 충족";
  return `
    <div class="promotion-stage-matrix-row ${outcomeTone}">
      <div class="promotion-stage-matrix-main">
        <strong>${escapeHtml(transition)}</strong>
        ${promotionActionBadge(row.action || "NO_DATA", outcomeTone)}
        <span>${promotionStageBadge(row.recommended_stage || "-")}</span>
      </div>
      <div class="promotion-stage-matrix-metrics">
        <span>판단 ${compactNumber(metrics.decision_count)}</span>
        <span>거래일 ${compactNumber(metrics.trade_day_count)}</span>
        <span>주문 ${compactNumber(metrics.order_count)}</span>
        <span>실시간 ${ratio(metrics.realtime_high_ratio)}</span>
        <span>평균 ${pct(metrics.avg_return_pct)}</span>
      </div>
      <div class="promotion-stage-matrix-blockers">
        <strong>${escapeHtml(blockerText)}</strong>
        <span>승격확신 ${ratio(row.confidence)}</span>
      </div>
      <div class="promotion-stage-matrix-requirements">${requirementLines}</div>
    </div>
  `;
}

function promotionRequirementText(check = {}) {
  const label = check.label || check.code || "Requirement";
  const direction = check.direction === "max" ? "기준 이하" : check.direction === "min" ? "기준 이상" : "";
  const actual = promotionRequirementValue(check.actual, check.unit);
  const threshold = promotionRequirementValue(check.threshold, check.unit);
  if (check.unit === "flag") return label;
  return `${label}: 현재 ${actual}, 필요 ${direction} ${threshold}`;
}

function promotionRequirementValue(value, unit) {
  if (unit === "ratio") return ratio(value);
  if (unit === "pct") return pct(value);
  if (unit === "count") return compactNumber(value);
  if (value == null || value === "") return "-";
  return String(value);
}

function promotionMetric(label, value, formatter = compactNumber) {
  return `
    <div class="promotion-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(formatter(value))}</strong>
    </div>
  `;
}

function promotionReasonDetailMap(details = []) {
  return Object.fromEntries((details || []).map((item) => [String(item.code || ""), item]));
}

function promotionReasonLabel(reason, detail = null) {
  return String((detail || {}).label_ko || reason || "-");
}

function promotionReasonDescription(reason, detail = null) {
  return String((detail || {}).description_ko || reason || "");
}

function promotionReasonRow(reason, tone = "warning", detail = null) {
  const selected = state.promotionSelectedBlocker === reason;
  const label = promotionReasonLabel(reason, detail);
  const description = promotionReasonDescription(reason, detail);
  if (tone === "blocked") {
    return `
      <button type="button" class="promotion-reason-row blocked ${selected ? "selected" : ""}" data-promotion-blocker="${escapeHtml(reason)}" aria-pressed="${selected ? "true" : "false"}">
        <strong>차단</strong>
        <span>${escapeHtml(label)}</span>
        <small>${escapeHtml(description || reason)}</small>
      </button>
    `;
  }
  return `
    <div class="promotion-reason-row ${escapeHtml(tone)}">
      <strong>${escapeHtml(tone === "blocked" ? "차단" : "주의")}</strong>
      <span>${escapeHtml(label)}</span>
      <small>${escapeHtml(description || reason)}</small>
    </div>
  `;
}

function renderPromotionDrilldownPanel() {
  const titleNode = document.getElementById("promotion-drilldown-title");
  const statusNode = document.getElementById("promotion-drilldown-status");
  const summaryNode = document.getElementById("promotion-drilldown-summary");
  const listNode = document.getElementById("promotion-drilldown-list");
  if (!titleNode || !statusNode || !summaryNode || !listNode) return;
  const selected = state.promotionSelectedBlocker;
  const section = ((state.promotionDrilldown || {}).sections || [])[0] || {};
  const summary = section.summary || {};
  const selectedDetail = (state.promotionDrilldown || {}).selected_blocker_detail || summary.blocker_detail || null;
  titleNode.textContent = selected ? `원인 상세 · ${promotionReasonLabel(selected, selectedDetail)}` : "원인 상세";
  if (state.promotionDrilldownError) {
    statusNode.textContent = "load failed";
    summaryNode.innerHTML = `<div class="operator-alert-empty">Promotion drilldown API 실패: ${escapeHtml(state.promotionDrilldownError)}</div>`;
    listNode.innerHTML = "";
    return;
  }
  if (state.promotionDrilldownLoading) {
    statusNode.textContent = "loading";
    summaryNode.innerHTML = `<div class="operator-alert-empty">관련 evidence 조회 중</div>`;
    listNode.innerHTML = "";
    return;
  }
  if (!selected) {
    statusNode.textContent = "원인 선택";
    summaryNode.innerHTML = `<div class="operator-alert-empty">확인할 원인을 선택하면 관련 판단, 결과, 주문 근거가 표시됩니다.</div>`;
    listNode.innerHTML = "";
    return;
  }
  const items = (state.promotionDrilldown || {}).items || section.items || [];
  const groups = (state.promotionDrilldown || {}).grouped_items || section.grouped_items || [];
  const hasGroups = groups.length > 0;
  statusNode.textContent = hasGroups
    ? `${compactNumber(summary.shown_group_count || groups.length)} / ${compactNumber(summary.group_count || groups.length)} 종목 · 원시 ${compactNumber(summary.matching_count || items.length)}건`
    : `${compactNumber(summary.shown_count || items.length)} / ${compactNumber(summary.matching_count || items.length)}건`;
  summaryNode.innerHTML = [
    promotionDrilldownSummaryMetric("지표값", summary.metric_value, promotionMetricFormatter(selected)),
    promotionDrilldownSummaryMetric("종목", summary.group_count, compactNumber),
    promotionDrilldownSummaryMetric("결과", summary.outcome_count, compactNumber),
    promotionDrilldownSummaryMetric("주문의도", summary.intent_count, compactNumber),
    promotionDrilldownSummaryMetric("가상주문", summary.live_order_count, compactNumber),
    `<div class="promotion-drilldown-explain">${escapeHtml(summary.explanation_ko || "관련 evidence rows를 확인합니다.")}</div>`,
  ].join("");
  listNode.innerHTML = hasGroups
    ? groups.map((group) => promotionDrilldownGroup(group)).join("")
    : items.length
      ? items.map((item) => promotionDrilldownRow(item)).join("")
    : `<div class="operator-alert-empty">선택한 blocker에 매칭되는 evidence row가 없습니다.</div>`;
}

function promotionDrilldownSummaryMetric(label, value, formatter = compactNumber) {
  return `
    <div class="promotion-drilldown-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(formatter(value))}</strong>
    </div>
  `;
}

function promotionMetricFormatter(blocker) {
  if (String(blocker || "").includes("RATE") || blocker === "REALTIME_HIGH_RATIO_LOW") return ratio;
  if (blocker === "EXPECTANCY_BELOW_THRESHOLD") return pct;
  return compactNumber;
}

function promotionDrilldownRow(item = {}) {
  const label = item.outcome_label || item.status || item.reason || "-";
  const code = [item.code, item.name].filter(Boolean).join(" ");
  const returns = item.source_type === "decision_outcome"
    ? `ret ${pct(item.current_return_pct)} / mfe ${pct(item.max_return_pct)} / mae ${pct(item.max_drawdown_pct)}`
    : `${escapeHtml(item.side || "-")} ${escapeHtml(item.order_phase || "")} ${escapeHtml(item.reason || "")}`;
  const reasons = (item.reason_codes || []).slice(0, 4).join(", ");
  return `
    <article class="promotion-drilldown-row">
      <div class="promotion-drilldown-main">
        <strong>${escapeHtml(code || item.id || "-")}</strong>
        ${badge(item.realtime_bucket || "NO_DATA")}
        ${badge(label)}
      </div>
      <div class="operator-event-meta">${escapeHtml(item.source_type || "-")} · ${escapeHtml(formatEventTime(item.event_at))} · ${escapeHtml(returns)}</div>
      <div class="operator-event-meta">${escapeHtml(reasons || item.summary || "-")}</div>
    </article>
  `;
}

function promotionDrilldownGroup(group = {}) {
  const code = [group.code, group.name].filter(Boolean).join(" ");
  const label = group.representative_label || topCountLabel(group.outcome_counts) || "-";
  const horizons = (group.horizons_sec || []).slice(0, 6).map((item) => `${item}s`).join(", ");
  const bucketCounts = countSummary(group.bucket_counts);
  const outcomeCounts = countSummary(group.outcome_counts);
  const returns = `ret ${pct(group.current_return_pct)} / mfe ${pct(group.max_return_pct)} / mae ${pct(group.max_drawdown_pct)}`;
  const rawItems = (group.items || []).slice(0, 3);
  return `
    <article class="promotion-drilldown-row promotion-drilldown-group">
      <div class="promotion-drilldown-main">
        <strong>${escapeHtml(code || group.key || "-")}</strong>
        ${badge(group.realtime_bucket || "NO_DATA")}
        ${badge(`${compactNumber(group.row_count || 0)} rows`)}
        ${badge(label)}
      </div>
      <div class="operator-event-meta">${escapeHtml(group.theme_name || "-")} · ${escapeHtml(formatEventTime(group.latest_event_at))} · ${escapeHtml(returns)}</div>
      <div class="operator-event-meta">bucket ${escapeHtml(bucketCounts || "-")} · outcome ${escapeHtml(outcomeCounts || "-")}${horizons ? ` · horizon ${escapeHtml(horizons)}` : ""}</div>
      ${rawItems.length ? `<div class="promotion-drilldown-subrows">${rawItems.map((item) => promotionDrilldownSubrow(item)).join("")}</div>` : ""}
    </article>
  `;
}

function promotionDrilldownSubrow(item = {}) {
  const horizon = item.horizon_sec ? `${item.horizon_sec}s` : "";
  const label = item.outcome_label || item.status || item.reason || "-";
  return `
    <div class="promotion-drilldown-subrow">
      <span>${escapeHtml([item.source_type, horizon].filter(Boolean).join(" · ") || "-")}</span>
      <strong>${escapeHtml(label)}</strong>
      <span>${escapeHtml(item.summary || formatEventTime(item.event_at) || "-")}</span>
    </div>
  `;
}

function countSummary(counts = {}) {
  return Object.entries(counts || {})
    .filter(([, value]) => Number(value || 0) > 0)
    .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0) || String(a[0]).localeCompare(String(b[0])))
    .slice(0, 4)
    .map(([key, value]) => `${key}:${compactNumber(value)}`)
    .join(", ");
}

function topCountLabel(counts = {}) {
  const first = Object.entries(counts || {})
    .filter(([, value]) => Number(value || 0) > 0)
    .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0) || String(a[0]).localeCompare(String(b[0])))[0];
  return first ? first[0] : "";
}

function promotionStageBadge(stage) {
  const label = String(stage || "-").toUpperCase();
  const tone = {
    OBSERVE: "observe",
    DRY_RUN: "ready-small",
    LIVE_SIM: "ready",
    REAL_MICRO: "warning",
  }[label] || "observe";
  return badge(label, tone);
}

function renderPromotionWindowButtons() {
  document.querySelectorAll("[data-promotion-window]").forEach((button) => {
    const value = Number(button.dataset.promotionWindow || 0);
    button.classList.toggle("active", value === Number(state.promotionWindowSec || 0));
  });
}

function promotionTradeDate(snapshot = state.snapshot || {}) {
  const outcomesDate = ((snapshot || {}).gate_reason_outcomes || {}).trade_date;
  if (outcomesDate) return String(outcomesDate).slice(0, 10);
  const calculatedAt = String((snapshot || {}).calculated_at || (snapshot || {}).created_at || "");
  return calculatedAt.length >= 10 ? calculatedAt.slice(0, 10) : "";
}

async function fetchPromotionDecision(snapshot = state.snapshot || {}) {
  if (state.promotionDecisionLoading) return;
  state.promotionDecisionLoading = true;
  state.promotionDecisionError = "";
  renderPromotionDecisionPanel();
  try {
    const params = new URLSearchParams();
    const windowSec = Number(state.promotionWindowSec || 0);
    const tradeDate = promotionTradeDate(snapshot);
    if (tradeDate) params.set("trade_date", tradeDate);
    if (windowSec > 0) params.set("window_sec", String(windowSec));
    const response = await fetch(`/api/runtime/promotion/decision?${params.toString()}`, { cache: "no-store" });
    const payload = await parseResponsePayload(response);
    if (!response.ok) throw new Error(payload.detail || payload.error || `promotion ${response.status}`);
    state.promotionDecision = payload || {};
    state.promotionDecisionError = "";
    state.promotionDecisionLastFetchedAt = new Date().toISOString();
    const blockers = ((payload || {}).decision || {}).blockers || [];
    if (!blockers.length) {
      state.promotionSelectedBlocker = "";
      state.promotionDrilldown = null;
      state.promotionDrilldownError = "";
    } else if (!blockers.includes(state.promotionSelectedBlocker)) {
      state.promotionSelectedBlocker = blockers[0];
    }
  } catch (error) {
    state.promotionDecisionError = error.message || String(error);
  } finally {
    state.promotionDecisionLoading = false;
    renderPromotionDecisionPanel();
    if (state.promotionSelectedBlocker) {
      fetchPromotionDrilldown(snapshot).catch(() => {});
    } else {
      renderPromotionDrilldownPanel();
    }
  }
}

async function fetchPromotionDrilldown(snapshot = state.snapshot || {}) {
  if (!state.promotionSelectedBlocker || state.promotionDrilldownLoading) return;
  state.promotionDrilldownLoading = true;
  state.promotionDrilldownError = "";
  renderPromotionDrilldownPanel();
  try {
    const params = new URLSearchParams();
    const windowSec = Number(state.promotionWindowSec || 0);
    const tradeDate = promotionTradeDate(snapshot);
    params.set("blocker", state.promotionSelectedBlocker);
    params.set("detail_limit", "20");
    if (tradeDate) params.set("trade_date", tradeDate);
    if (windowSec > 0) params.set("window_sec", String(windowSec));
    const response = await fetch(`/api/runtime/promotion/drilldown?${params.toString()}`, { cache: "no-store" });
    const payload = await parseResponsePayload(response);
    if (!response.ok) throw new Error(payload.detail || payload.error || `promotion drilldown ${response.status}`);
    state.promotionDrilldown = payload || {};
    state.promotionDrilldownError = "";
  } catch (error) {
    state.promotionDrilldownError = error.message || String(error);
  } finally {
    state.promotionDrilldownLoading = false;
    renderPromotionDrilldownPanel();
  }
}

function backfillStatusText(backfill) {
  if (backfill.gateway_unhealthy_display) {
    return `${backfill.paused_reason || "GATEWAY_UNHEALTHY"}: ${backfill.gateway_unhealthy_display}`;
  }
  return backfill.paused_reason || (backfill.observe_pilot_active ? "OBSERVE_PILOT" : "IDLE");
}

function updateKiwoomGatewayButton(snapshot) {
  const button = document.getElementById("kiwoom-gateway-start");
  if (!button) return;
  const gateway = (snapshot || {}).gateway || {};
  const loggedIn = Boolean(gateway.kiwoom_logged_in);
  const connected = Boolean(gateway.connected && gateway.heartbeat_ok);
  button.disabled = connected || button.dataset.busy === "1";
  if (button.dataset.busy === "1") {
    button.textContent = "Gateway \uc2e4\ud589 \uc911";
  } else if (loggedIn) {
    button.textContent = "Gateway \ub85c\uadf8\uc778\ub428";
  } else if (connected) {
    button.textContent = "\uc790\ub3d9\ub85c\uadf8\uc778 \ub300\uae30";
  } else {
    button.textContent = "32bit Gateway \uc2e4\ud589";
  }
}

function updateNaverThemeSyncButton() {
  const button = document.getElementById("naver-theme-sync");
  if (!button) return;
  const busy = state.naverThemeSyncBusy || button.dataset.busy === "1";
  button.disabled = busy;
  button.textContent = busy ? "네이버 업데이트 중" : "네이버 테마 업데이트";
}

function renderNaverThemeSyncStatus(sync = {}) {
  const node = document.getElementById("naver-theme-sync-status");
  if (!node) return;
  const status = String(sync.status || "NOT_SYNCED").toUpperCase();
  const themeCount = Number(sync.theme_count || 0);
  const memberCount = Number(sync.member_count || 0);
  const finishedAt = sync.finished_at ? formatEventTime(sync.finished_at) : "";
  const message = sync.message ? ` · ${sync.message}` : "";
  if (status === "SUCCESS") {
    node.textContent = `Naver ${themeCount}개 테마 / ${memberCount}종목${finishedAt ? ` · ${finishedAt}` : ""}`;
  } else if (status === "FAILED") {
    node.textContent = `Naver sync 실패${message}`;
  } else {
    node.textContent = "Naver sync 없음";
  }
  node.title = [
    `source=${sync.source || "naver_theme_universe"}`,
    `status=${status}`,
    sync.started_at ? `started=${sync.started_at}` : "",
    sync.finished_at ? `finished=${sync.finished_at}` : "",
  ].filter(Boolean).join(" / ");
}

async function syncNaverThemeUniverse(button) {
  if (!button || state.naverThemeSyncBusy) return;
  state.naverThemeSyncBusy = true;
  button.dataset.busy = "1";
  updateNaverThemeSyncButton();
  text("naver-theme-sync-status", "네이버 테마 크롤링 중");
  try {
    const payload = await runWithLocalTokenRetry(async (token) => {
      const response = await fetch("/api/themes/sync/naver?replace=true&max_pages=20", {
        method: "POST",
        headers: { "X-Local-Token": token },
      });
      return { response, payload: await parseResponsePayload(response) };
    });
    if (!payload) return;
    if (String(payload.status || "").toLowerCase() !== "success") {
      throw new Error(payload.message || "Naver theme sync failed");
    }
    renderNaverThemeSyncStatus(payload);
    await fetchSnapshot().catch(() => {});
  } catch (error) {
    renderNaverThemeSyncStatus({
      source: "naver_theme_universe",
      status: "failed",
      message: error.message || String(error),
    });
  } finally {
    state.naverThemeSyncBusy = false;
    button.dataset.busy = "0";
    updateNaverThemeSyncButton();
  }
}

async function startKiwoomGateway(button) {
  if (!button || button.disabled) return;
  button.dataset.busy = "1";
  updateKiwoomGatewayButton(state.snapshot || {});
  try {
    const payload = await runWithLocalTokenRetry(async (token) => {
      const response = await fetch("/api/gateway/kiwoom/start", {
        method: "POST",
        headers: { "X-Local-Token": token },
      });
      return { response, payload: await parseResponsePayload(response) };
    });
    if (payload) {
      button.title = payload.started
        ? "32bit Gateway \uc2e4\ud589 \uc694\uccad \uc644\ub8cc"
        : `Gateway \ubbf8\uc2e4\ud589: ${payload.reason || "UNKNOWN"}`;
    }
    await fetchSnapshot();
  } catch (error) {
    button.title = error.message || String(error);
  } finally {
    button.dataset.busy = "0";
    updateKiwoomGatewayButton(state.snapshot || {});
  }
}

function countLine(label, value) {
  return `<div class="cockpit-line"><strong>${escapeHtml(label)}</strong><span>${Number(value || 0).toLocaleString("ko-KR")}</span></div>`;
}

function renderShadowAb(snapshot) {
  const body = document.getElementById("shadow-ab-body");
  if (!body) return;
  const outcomes = (snapshot || {}).gate_reason_outcomes || {};
  const shadow = (snapshot || {}).shadow_small_entry || outcomes.shadow_small_entry || {};
  const ab = (snapshot || {}).shadow_small_entry_ab || outcomes.shadow_small_entry_ab || {};
  const summary = shadow.summary || {};
  const scenarios = ab.scenarios || [];
  const best = (ab.best_scenarios || [])[0] || scenarios.find((row) => Number(row.candidate_count || 0) > 0) || null;
  const panelStatus = best ? best.recommendation || "OBSERVE_MORE" : scenarios.length ? "NO_CANDIDATES" : outcomes.status || "NO_DATA";

  setBadge("shadow-ab-status", panelStatus);
  text("shadow-ab-best", best ? best.scenario_id || best.label : "-");
  text(
    "shadow-ab-message",
    outcomes.status === "ERROR"
      ? `Outcome report error: ${outcomes.error || "-"}`
      : `trade_date ${outcomes.trade_date || "-"} / generated ${outcomes.generated_at || "-"}`
  );

  const summaryNode = document.getElementById("shadow-ab-summary");
  if (summaryNode) {
    summaryNode.innerHTML = [
      shadowMetric("events", outcomes.summary?.event_count),
      shadowMetric("labeled", outcomes.summary?.labeled_event_count),
      shadowMetric("shadow cand", summary.candidate_count),
      shadowMetric("win15", summary.win_rate_15m, ratio),
      shadowMetric("risk15", summary.risk_case_rate_15m, ratio),
      shadowMetric("capture", summary.missed_opportunity_reduction_estimate, ratio),
      shadowMetric("size", summary.position_size_multiplier, multiplier),
    ].join("");
  }

  if (!scenarios.length) {
    body.innerHTML = `<tr><td colspan="10" class="muted">No shadow A/B scenarios yet.</td></tr>`;
    return;
  }
  body.innerHTML = scenarios.slice(0, 8).map((row) => `
    <tr>
      <td><strong>${escapeHtml(row.label || row.scenario_id || "-")}</strong><br><span class="muted">${escapeHtml(row.scenario_id || "-")}</span></td>
      <td class="num">${compactNumber(row.candidate_count)} / ${compactNumber(row.labeled_count)}</td>
      <td class="num">${ratio(row.win_rate_15m)}</td>
      <td class="num">${ratio(row.risk_case_rate_15m)}</td>
      <td class="num ${Number(row.avg_mfe_15m_pct || 0) >= 0 ? "positive" : "negative"}">${pct(row.avg_mfe_15m_pct)}</td>
      <td class="num ${Number(row.avg_mae_15m_pct || 0) >= 0 ? "positive" : "negative"}">${pct(row.avg_mae_15m_pct)}</td>
      <td class="num">${ratio(row.missed_opportunity_reduction_estimate)}</td>
      <td class="num">${multiplier(row.position_size_multiplier)}</td>
      <td class="num">${score(row.net_shadow_score, 1)}</td>
      <td>${badge(row.recommendation || "UNKNOWN")}</td>
    </tr>
  `).join("");
}

function shadowMetric(label, value, formatter = compactNumber) {
  return `
    <div class="shadow-ab-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(formatter(value))}</strong>
    </div>
  `;
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

function renderFocusPanel(item = {}, chart = {}) {
  const summaryNode = document.getElementById("focus-summary");
  const checklistNode = document.getElementById("decision-checklist");
  const priceNode = document.getElementById("price-map");
  if (!summaryNode || !checklistNode || !priceNode) return;
  const merged = { ...chart, ...item };
  const title = item.symbol ? `${item.stock_name || item.name || item.symbol} [${item.symbol}]` : "선택 후보 대기";
  const action = operatorAction(item);
  const nextCheck = nextRecheckAfterSec(item);
  text("chart-title", title);
  text("focus-next-check", nextCheck ? seconds(nextCheck) : "다음 확인 없음");
  summaryNode.innerHTML = item.symbol ? [
    focusCard("최종 판단", `${badge(item.gate_status || "OBSERVE")} ${badge(item.display_status || item.gate_status || "OBSERVE")}`, item.summary_reason || "-"),
    focusCard("운영 액션", badge(operatorActionLabel(action), operatorActionTone(action)), operatorActionHint(action)),
    focusCard("주문 연결", orderLinkLine(item), orderConnectionSummary(item)),
    focusCard("데이터 신뢰도", dataTrustLine(item), dataFlagsLine(item)),
    focusCard("리스크", riskFocusLine(item), riskCodesLine(item)),
    focusCard("시장", marketFocusLine(item), marketBreadthLine(item)),
  ].join("") : `
    <article class="focus-card empty-focus">
      <span>선택 후보</span>
      <strong>WatchSet 데이터 대기</strong>
      <p>후보가 들어오면 운영 판단과 가격 위치가 여기에 표시됩니다.</p>
    </article>
  `;
  checklistNode.innerHTML = renderDecisionChecklist(item);
  priceNode.innerHTML = renderPriceMap(merged);
}

function focusCard(label, body, detail = "") {
  return `
    <article class="focus-card">
      <span>${escapeHtml(label)}</span>
      <strong>${body || "-"}</strong>
      <p>${escapeHtml(detail || "-")}</p>
    </article>
  `;
}

function operatorAction(item) {
  if (item.operator_action) return item.operator_action;
  const gate = item.gate_status || "";
  const display = item.display_status || gate;
  if (gateIsReadyLike(item) && item.submittable && item.live_order_guard_passed) return "BUY_READY";
  if (gateIsReadyLike(item) && !item.live_order_guard_passed) return "LIVE_GUARD_BLOCKED";
  if (item.diagnostic_only || display.startsWith("WAIT_DATA")) return "DATA_WAIT";
  if (isMarketPending(item) || display.startsWith("WAIT_CANDIDATE_MARKET") || item.market_confirmed_status === "RISK_OFF") return "MARKET_WAIT";
  if (item.chase_risk || display === "CHASE_RISK_BLOCKED") return "CHASE_BLOCKED";
  return "OBSERVE";
}

function operatorActionLabel(action) {
  return {
    BUY_READY: "주문 가능",
    MARKET_WAIT: "시장 대기",
    DATA_WAIT: "데이터 대기",
    CHASE_BLOCKED: "추격 차단",
    LIVE_GUARD_BLOCKED: "LIVE Guard 차단",
    OBSERVE: "OBSERVE",
  }[action] || action || "OBSERVE";
}

function operatorActionTone(action) {
  return {
    BUY_READY: "ready",
    MARKET_WAIT: "wait",
    DATA_WAIT: "warning",
    CHASE_BLOCKED: "blocked",
    LIVE_GUARD_BLOCKED: "warning",
    OBSERVE: "observe",
  }[action] || "observe";
}

function operatorActionHint(action) {
  return {
    BUY_READY: "게이트와 LIVE Guard가 주문 가능 상태입니다.",
    MARKET_WAIT: "시장 확인 또는 회복 조건을 기다립니다.",
    DATA_WAIT: "틱, 지지선, VWAP 등 보조 데이터가 더 필요합니다.",
    CHASE_BLOCKED: "늦은 추격 또는 과열 리스크로 진입을 막았습니다.",
    LIVE_GUARD_BLOCKED: "전략 후보지만 LIVE 주문 안전장치가 막고 있습니다.",
    OBSERVE: "즉시 주문보다 관찰이 우선입니다.",
  }[action] || "관찰 상태입니다.";
}

function nextRecheckAfterSec(item) {
  const candidates = [
    item.next_recheck_after_sec,
    item.recheck_after_sec,
    item.late_chase_recheck_after_sec,
    item.market_wait_recheck_after_sec,
  ].map((value) => Number(value || 0)).filter((value) => Number.isFinite(value) && value > 0);
  return candidates.length ? Math.min(...candidates) : null;
}

function orderConnectionSummary(item) {
  return [
    item.entry_plan_created ? "entry plan" : "",
    item.runtime_order_intent_created ? "runtime intent" : "",
    item.virtual_order_created ? "virtual order" : "",
    item.live_order_enabled ? "LIVE on" : "LIVE off",
    item.live_order_guard_passed ? "guard pass" : "guard wait",
  ].filter(Boolean).join(" / ");
}

function dataTrustLine(item) {
  return [
    `틱 ${item.latest_tick_ready !== false ? "OK" : "대기"}`,
    item.latest_tick_age_sec == null ? "" : seconds(item.latest_tick_age_sec),
    `지지 ${item.support_ready ? "OK" : "대기"}`,
    `VWAP ${item.vwap_ready ? "OK" : "대기"}`,
    `최근지지 ${item.recent_support_ready ? "OK" : "대기"}`,
  ].filter(Boolean).map(escapeHtml).join("<br>");
}

function dataFlagsLine(item) {
  const flags = [...(item.data_quality_flags || []), ...(item.price_location_data_quality_flags || [])];
  return flags.length ? flags.slice(0, 4).join(", ") : "품질 플래그 없음";
}

function riskFocusLine(item) {
  return [
    item.chase_risk ? badge("CHASE_RISK_BLOCKED") : badge("추격 PASS", "ready"),
    item.late_chase_level ? escapeHtml(item.late_chase_level) : "",
    item.late_chase_score == null ? "" : escapeHtml(`점수 ${score(item.late_chase_score, 0)}`),
    seconds(item.late_chase_recheck_after_sec),
  ].filter(Boolean).join("<br>");
}

function riskCodesLine(item) {
  const codes = item.risk_reason_codes || [];
  return codes.length ? codes.slice(0, 4).join(", ") : "리스크 코드 없음";
}

function marketFocusLine(item) {
  return [
    badge(item.candidate_market || "UNKNOWN"),
    badge(item.market_confirmed_status || "UNKNOWN"),
    item.market_confirmation_pending ? escapeHtml("확인 대기") : "",
    item.market_recovery_pending ? escapeHtml("회복 대기") : "",
  ].filter(Boolean).join("<br>");
}

function marketBreadthLine(item) {
  return `폭 ${pct(item.market_side_breadth_pct)} / 신뢰 ${item.market_side_breadth_trust_level || "UNKNOWN"}`;
}

function renderDecisionChecklist(item) {
  const checklist = item.decision_checklist || calculatedDecisionChecklist(item);
  const rows = [
    ["market", "시장"],
    ["theme", "테마"],
    ["role", "역할"],
    ["price_location", "가격위치"],
    ["data", "데이터"],
    ["chase_risk", "추격리스크"],
    ["order_link", "주문연결"],
  ];
  return rows.map(([key, label]) => {
    const value = checklist[key] || "OBSERVE";
    return `<span class="check-pill ${decisionTone(value)}"><em>${escapeHtml(label)}</em>${escapeHtml(value)}</span>`;
  }).join("");
}

function calculatedDecisionChecklist(item) {
  return {
    market: marketDecision(item),
    theme: themeDecision(item),
    role: roleDecision(item),
    price_location: priceLocationDecision(item),
    data: dataDecision(item),
    chase_risk: chaseDecision(item),
    order_link: orderDecision(item),
  };
}

function marketDecision(item) {
  const status = item.market_confirmed_status || item.candidate_market_status || "";
  if (status === "RISK_OFF" || String(item.display_status || "").includes("RISK_OFF")) return "BLOCK";
  if (isMarketPending(item) || status === "WEAK" || status === "CHOPPY") return "WAIT";
  return "PASS";
}

function themeDecision(item) {
  const status = String(item.theme_status || "").toUpperCase();
  const scoreValue = Number(item.theme_score || 0);
  if (status.includes("WEAK") || scoreValue < 40) return "WEAK";
  if (status.includes("WATCH") || scoreValue < 65) return "WATCH";
  return "PASS";
}

function roleDecision(item) {
  const role = item.stock_role || "WEAK_MEMBER";
  return ["LEADER", "CO_LEADER", "FOLLOWER", "LATE_LAGGARD", "WEAK_MEMBER"].includes(role) ? role : "WEAK_MEMBER";
}

function priceLocationDecision(item) {
  const status = item.price_location_status || "";
  const display = item.display_status || "";
  if (display.startsWith("WAIT_DATA") || status === "UNKNOWN") return "DATA_WAIT";
  if (["FAILED_BREAKOUT", "DEEP_PULLBACK"].includes(status)) return "WAIT";
  if (display === "CHASE_RISK_BLOCKED" || item.chase_risk) return "BLOCK";
  return "PASS";
}

function dataDecision(item) {
  const flags = [...(item.data_quality_flags || []), ...(item.price_location_data_quality_flags || [])];
  if (isDataNotReady(item)) return "DEGRADED";
  return flags.length ? "WARNING" : "OK";
}

function chaseDecision(item) {
  const display = item.display_status || "";
  if (item.chase_risk || display === "CHASE_RISK_BLOCKED") return "BLOCK";
  if (display === "LATE_CHASE_TEMP_WAIT" || item.late_chase_level) return "WAIT";
  return "PASS";
}

function orderDecision(item) {
  if (item.runtime_order_intent_created) return "INTENT_CREATED";
  if (gateIsReadyLike(item) && !item.live_order_guard_passed) return "LIVE_BLOCKED";
  if (item.submittable && item.live_order_guard_passed) return "READY";
  return "OBSERVE";
}

function decisionTone(value) {
  if (["PASS", "OK", "READY", "LEADER", "CO_LEADER"].includes(value)) return "ready";
  if (["WATCH", "WAIT", "FOLLOWER", "INTENT_CREATED"].includes(value)) return "wait";
  if (["DATA_WAIT", "WARNING", "LIVE_BLOCKED", "LATE_LAGGARD"].includes(value)) return "warning";
  if (["BLOCK", "DEGRADED", "WEAK", "WEAK_MEMBER"].includes(value)) return "blocked";
  return "observe";
}

function renderPriceMap(itemOrChart) {
  const definitions = [
    ["support_price", "Support"],
    ["recent_support_price", "Recent"],
    ["vwap", "VWAP"],
    ["breakout_level", "Breakout"],
    ["current_price", "Last"],
    ["upper_limit_price", "Upper"],
  ];
  const points = definitions.map(([key, label]) => ({ key, label, value: numberOrNull(itemOrChart[key]) }))
    .filter((point) => point.value !== null);
  text("focus-price-state", points.length ? `${points.length}개 기준` : "데이터 대기");
  if (!points.length) {
    return `<div class="price-map-empty">데이터 대기</div>`;
  }
  const values = points.map((point) => point.value);
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const span = Math.max(1, maxValue - minValue);
  const markers = points.map((point) => {
    const left = minValue === maxValue ? 50 : ((point.value - minValue) / span) * 100;
    return `
      <span class="price-marker ${point.key}" style="left: ${left.toFixed(2)}%">
        <i></i><b>${escapeHtml(point.label)}</b>
      </span>
    `;
  }).join("");
  const metrics = points.map((point) => `
    <div class="price-metric ${point.key}">
      <span>${escapeHtml(point.label)}</span>
      <strong>${escapeHtml(formatPrice(point.value))}</strong>
    </div>
  `).join("");
  return `
    <div class="price-track" aria-hidden="true">${markers}</div>
    <div class="price-metrics">${metrics}</div>
  `;
}

function renderChart(chart) {
  const activeChart = chart || {};
  if (state.chartInterval === "5m" && !hasIntervalData(activeChart, "5m")) {
    state.chartInterval = "1m";
  }
  updateTimeframeButtons(activeChart);
  text("chart-title", `${activeChart.name || "KOSDAQ"} ${activeChart.symbol ? `[${activeChart.symbol}]` : ""}`);
  text("chart-subtitle", `${activeChart.reason || "INDEX"} / ${activeChart.chart_data_status || "NO_CANDLE_DATA"} / ${state.chartInterval} bars ${intervalBarCount(activeChart)}`);
  text("chart-interval-state", state.chartInterval);
  const stage = document.getElementById("chart-stage");
  const status = activeChart.chart_data_status || "NO_CANDLE_DATA";
  const candles = normalizeCandles(chartCandlesForInterval(activeChart));
  if (candles.length) {
    stage.innerHTML = minuteChartSvg(activeChart, candles);
    return;
  }
  stage.innerHTML = `
    <div class="empty-chart">
      <strong>${status === "QUOTE_ONLY" ? "실시간 현재가만 수신 중" : "분봉 데이터 없음"}</strong>
      <span>${escapeHtml(activeChart.symbol || "KOSDAQ")} · ${escapeHtml(state.chartInterval)} 데이터 대기 · VWAP/마커는 가능한 값만 표시</span>
    </div>
  `;
}

function chartCandlesForInterval(chart) {
  if (state.chartInterval === "3m") return chart.recent_candles_3m || [];
  if (state.chartInterval === "5m") return chart.recent_candles_5m || [];
  return chart.recent_candles_1m || chart.candles || [];
}

function intervalBarCount(chart) {
  if (state.chartInterval === "3m") return (chart.recent_candles_3m || []).length || chart.recent_3m_bar_count || 0;
  if (state.chartInterval === "5m") return (chart.recent_candles_5m || []).length || chart.recent_5m_bar_count || 0;
  return (chart.recent_candles_1m || chart.candles || []).length || chart.completed_minute_bar_count || 0;
}

function hasIntervalData(chart, interval) {
  if (interval === "5m") return Boolean((chart.recent_candles_5m || []).length);
  if (interval === "3m") return true;
  return true;
}

function updateTimeframeButtons(chart) {
  document.querySelectorAll("[data-chart-interval]").forEach((button) => {
    const interval = button.dataset.chartInterval;
    const unsupported = interval === "5m" && !hasIntervalData(chart, "5m");
    button.disabled = unsupported;
    button.classList.toggle("active", interval === state.chartInterval && !unsupported);
    button.setAttribute("aria-pressed", interval === state.chartInterval && !unsupported ? "true" : "false");
    if (unsupported) button.title = "5m 분봉 데이터 미지원";
  });
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
    { key: "support_price", label: "SUPPORT", kind: "support" },
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
  if (value === "WAIT_PRICE_LOCATION_DATA") return display === "WAIT_PRICE_LOCATION_DATA";
  if (value === "WAIT_PRICE_LOCATION_WARMUP") return display === "WAIT_PRICE_LOCATION_WARMUP";
  if (value === "WAIT_PRICE_LOCATION_PROVISIONAL") return display === "WAIT_PRICE_LOCATION_PROVISIONAL";
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
      <div class="order-row" data-symbol="${escapeHtml(item.symbol)}">
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
  text("condition-summary", `${registered}/${items.length || 3} 등록 확인`);
  document.getElementById("condition-list").innerHTML = items.length
    ? items.map((item) => {
      const statusTone = item.registered ? "ready" : "warning";
      const title = item.purpose_label
        ? `${item.purpose_label}: ${item.condition_name || "-"}`
        : (item.condition_name || "-");
      const details = [
        item.command_status_label || item.command_status || "등록 확인 대기",
        `인덱스 ${item.resolved_index || "UNKNOWN"}`,
        item.screen_no ? `화면 ${item.screen_no}` : "",
        item.gateway_heartbeat_ok === false ? "Heartbeat 확인 필요" : "",
      ].filter(Boolean).join(" · ");
      const warningLabel = item.warning_label || (item.warning ? "확인 필요" : "정상");
      const warningText = item.warning_detail || item.warning_label || item.warning || "정상";
      const actionHint = item.action_hint
        ? `<div class="row-meta action-hint">조치: ${escapeHtml(item.action_hint)}</div>`
        : "";
      return `
        <div class="quality-row">
          <div class="row-top"><span class="row-title">${escapeHtml(title)}</span>${badge(item.registered_label || (item.registered ? "정상" : "확인 필요"), statusTone)}</div>
          <div class="row-meta">${escapeHtml(details)}</div>
          <div class="row-meta">${badge(warningLabel, statusTone)} ${escapeHtml(warningText)}</div>
          ${actionHint}
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
    selectSymbol(row.dataset.symbol, { source: "watchset" });
  });
  document.getElementById("order-candidates")?.addEventListener("click", (event) => {
    const row = event.target.closest("[data-symbol]");
    if (!row || !state.snapshot) return;
    selectSymbol(row.dataset.symbol, { source: "order" });
  });
  document.getElementById("operator-alert-filters")?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-alert-filter]");
    if (!button) return;
    state.alertFilters.category = button.dataset.alertFilter || "ALL";
    saveOperatorAlertFilter();
    renderOperatorAlerts();
  });
  document.getElementById("operator-alert-ack-all")?.addEventListener("click", () => {
    const ids = state.operatorEvents
      .filter((event) => !event.hidden && state.persistedEventIds.has(event.id) && !state.acknowledgedEventIds.has(event.id))
      .map((event) => event.id);
    acknowledgeOperatorEvents(ids).catch((error) => {
      updateOperatorSyncStatus(`ACK 실패: ${error.message || error}`);
    });
  });
  document.getElementById("operator-alert-hide-acknowledged")?.addEventListener("click", () => {
    state.alertFilters.hideAcknowledged = !state.alertFilters.hideAcknowledged;
    saveOperatorAlertFilter();
    renderOperatorAlerts();
    if (state.alertFilters.hideAcknowledged) {
      const ids = state.operatorEvents
        .filter((event) => !event.hidden && state.persistedEventIds.has(event.id) && state.acknowledgedEventIds.has(event.id))
        .map((event) => event.id);
      hideOperatorEvents(ids).catch((error) => {
        updateOperatorSyncStatus(`숨김 실패: ${error.message || error}`);
      });
    }
  });
  document.getElementById("operator-event-journal-filters")?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-journal-filter]");
    if (!button) return;
    state.journalFilter = button.dataset.journalFilter || "ALL";
    renderOperatorEventJournal();
  });
  document.getElementById("operator-action-refresh")?.addEventListener("click", () => {
    Promise.all([
      fetchActionRecommendations(state.selectedActionContext || {}),
      fetchActionHistory(),
      fetchActionSummary(),
    ]).catch((error) => {
      text("operator-action-context", `Action Center 갱신 실패: ${error.message || error}`);
    });
  });
  document.getElementById("operator-action-recommendations")?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action-type]");
    if (!button || button.disabled) return;
    executeOperatorAction(button.dataset.actionType, state.selectedActionContext || {}).catch((error) => {
      text("operator-action-context", `액션 실패: ${error.message || error}`);
    });
  });
  document.getElementById("operator-action-confirm-cancel")?.addEventListener("click", () => {
    state.pendingAction = null;
    const modal = document.getElementById("operator-action-confirm-modal");
    if (modal) modal.hidden = true;
  });
  document.getElementById("operator-action-confirm-submit")?.addEventListener("click", () => {
    const pending = state.pendingAction;
    const modal = document.getElementById("operator-action-confirm-modal");
    const note = document.getElementById("operator-action-note");
    if (!pending) return;
    if (modal) modal.hidden = true;
    state.pendingAction = null;
    executeOperatorAction(pending.action_type, pending.context || state.selectedActionContext || {}, {
      confirm: true,
      note: note ? note.value : "",
    }).catch((error) => {
      text("operator-action-context", `액션 실패: ${error.message || error}`);
    });
  });
  document.getElementById("postmarket-review-refresh")?.addEventListener("click", () => {
    initPostmarketReviewPanel().catch((error) => {
      text("postmarket-review-status", `refresh failed: ${error.message || error}`);
    });
  });
  document.getElementById("postmarket-review-rebuild")?.addEventListener("click", () => {
    rebuildPostmarketReview({ force: true }).catch((error) => {
      text("postmarket-review-status", `rebuild failed: ${error.message || error}`);
    });
  });
  document.getElementById("postmarket-review-tabs")?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-postmarket-outcome]");
    if (!button) return;
    state.postmarketReviewFilters.outcome_label = button.dataset.postmarketOutcome || "ALL";
    button.closest("[data-filter-group]")?.querySelectorAll("button").forEach((node) => node.classList.toggle("active", node === button));
    renderPostmarketReviewPanel();
  });
  [
    "missed-opportunity-list",
    "good-block-list",
    "review-needed-list",
    "protected-from-chase-list",
    "data-insufficient-list",
  ].forEach((id) => {
    document.getElementById(id)?.addEventListener("click", (event) => {
      const row = event.target.closest("[data-review-id]");
      if (!row) return;
      const item = state.postmarketReviewItems.find((review) => review.review_id === row.dataset.reviewId);
      selectReviewItem(item);
    });
  });
  ["operator-alert-list", "operator-timeline-list", "operator-event-journal-list"].forEach((id) => {
    document.getElementById(id)?.addEventListener("click", (event) => {
      const row = event.target.closest("[data-event-id]");
      if (!row) return;
      const eventItem = state.operatorEvents.find((item) => item.id === row.dataset.eventId);
      if (!eventItem) return;
      const actionContext = actionContextFromEvent(eventItem);
      state.selectedEventId = eventItem.id;
      state.selectedActionContext = actionContext;
      fetchActionRecommendations(actionContext).catch((error) => {
        text("operator-action-context", `추천 액션 실패: ${error.message || error}`);
      });
      renderRunbook(runbookForEvent(eventItem));
      if (eventItem.symbol) {
        selectSymbol(eventItem.symbol, { source: "alert" });
      }
      acknowledgeOperatorEvents([eventItem.id])
        .then(() => {
          if (eventItem.symbol) selectSymbol(eventItem.symbol, { source: "alert", acknowledgeEventId: eventItem.id });
        })
        .catch((error) => {
          updateOperatorSyncStatus(`ACK 실패: ${error.message || error}`);
        });
    });
  });
  document.getElementById("chart-timeframe")?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-chart-interval]");
    if (!button || button.disabled) return;
    state.chartInterval = button.dataset.chartInterval || "1m";
    const item = selectedWatchItem(state.snapshot || {});
    const selectedChart = selectedChartForSymbol(state.snapshot || {}, item.symbol || state.selectedSymbol);
    renderFocusPanel(item, selectedChart);
    renderChart(selectedChart);
  });
  document.getElementById("kiwoom-gateway-start")?.addEventListener("click", (event) => {
    startKiwoomGateway(event.currentTarget).catch(() => {});
  });
  document.getElementById("naver-theme-sync")?.addEventListener("click", (event) => {
    syncNaverThemeUniverse(event.currentTarget).catch(() => {});
  });
}

async function fetchSnapshot() {
  const response = await fetch("/api/themelab/snapshot", { cache: "no-store" });
  if (!response.ok) throw new Error(`snapshot ${response.status}`);
  render(await response.json());
  fetchBuyZeroRca().catch(() => {});
  fetchPromotionDecision(state.snapshot || {}).catch(() => {});
}

function initBuyZeroRcaPanel() {
  renderBuyZeroRcaPanel();
  document.getElementById("themelab-buy-zero-rca-refresh")?.addEventListener("click", () => {
    state.buyZeroRca.lastFetchedAt = 0;
    fetchBuyZeroRca({ force: true }).catch((error) => {
      setBuyZeroStatus(`조회 실패: ${error.message || error}`, "critical");
    });
  });
}

function isFullThemeLabSnapshot(snapshot) {
  const payload = snapshot || {};
  const market = payload.market || {};
  return Array.isArray(payload.ranked_themes) || Array.isArray(payload.watchset) || Array.isArray(market.sides);
}

function connectWs() {
  if (typeof WebSocket !== "function") return;
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${protocol}://${window.location.host}/ws/dashboard`);
  ws.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    const snapshot = payload.snapshot || {};
    if (snapshot.theme_lab && isFullThemeLabSnapshot(snapshot.theme_lab)) render(snapshot.theme_lab);
  };
  ws.onclose = () => setTimeout(connectWs, 1500);
}

loadOperatorPreferences();
initFilters();
initOperatorEventJournal();
initOperatorActionCenter();
initPostmarketReviewPanel();
initPromotionCockpit();
initBuyZeroRcaPanel();
fetchSnapshot().catch(() => {});
connectWs();
setInterval(() => fetchSnapshot().catch(() => {}), 5000);
