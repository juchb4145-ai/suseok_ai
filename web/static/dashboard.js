const state = {
  ws: null,
  pollTimer: null,
  latestSnapshot: null,
  tables: {},
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

function formatMs(value) {
  if (value == null || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${number.toFixed(1)}ms`;
}

function formatDateTime(value) {
  if (!value) return "-";
  return String(value).replace("T", " ").replace("+00:00", "Z");
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

async function postWithLocalToken(endpoint, token) {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "X-Local-Token": token },
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
  const params = { ...filters, limit: table.limit, offset: table.offset };
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
  if (summary) summary.innerHTML = detailSummaryHtml(payload);
  if (raw) raw.textContent = JSON.stringify(payload, null, 2);
  document.getElementById("detail-drawer")?.classList.add("open");
  document.getElementById("detail-drawer")?.setAttribute("aria-hidden", "false");
  document.getElementById("detail-backdrop")?.classList.remove("hidden");
}

function closeDetailPanel() {
  document.getElementById("detail-drawer")?.classList.remove("open");
  document.getElementById("detail-drawer")?.setAttribute("aria-hidden", "true");
  document.getElementById("detail-backdrop")?.classList.add("hidden");
}

function detailSummaryHtml(payload) {
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
  const dryRunPerformance = snapshot.dry_run_performance || runtime.dry_run_performance || {};
  const thresholdAB = snapshot.threshold_ab || runtime.threshold_ab || { summary: {}, recommendations: [] };
  const candidates = snapshot.candidates || { summary: {}, items: [] };
  const themes = snapshot.themes || { summary: {}, items: [] };
  const orders = snapshot.orders || { summary: {}, order_results: [], executions: [] };
  const reviews = snapshot.reviews || { summary: {}, items: [] };
  const logs = snapshot.logs || { core: [], gateway: [], warnings: [] };
  const opsAlerts = snapshot.ops_alerts || { summary: {}, alerts: [] };
  const themeLab = snapshot.theme_lab || {};

  text("snapshot-time", snapshot.timestamp || "대기 중");
  text("core-mode", core.mode || "OBSERVE");
  cls("core-mode", `pill ${core.mode === "LIVE" ? "warn" : core.mode === "DRY_RUN" ? "ok" : "muted"}`);
  text("core-state", core.service ? "정상" : "대기");
  const gatewayState = gateway.connection_state || "DISCONNECTED";
  text("gateway-state", gatewayState);
  text("gateway-connection", gatewayState);
  text("kiwoom-login", yesNo(gateway.kiwoom_logged_in));
  text("heartbeat-age", gateway.heartbeat_age_sec == null ? "-" : `${fmtNumber(gateway.heartbeat_age_sec, 0)}s`);
  text("orderable-state", gateway.orderable ? "주문 가능" : "주문 차단");
  cls("gateway-state", `pill ${gateway.heartbeat_ok ? "ok" : gateway.connected ? "warn" : "bad"}`);
  cls("orderable-state", `pill ${gateway.orderable ? "ok" : "muted"}`);
  renderOpsAlerts(opsAlerts);
  renderThemeLabSummary(themeLab);

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
  text("dryrun-perf-fp", dryRunPerformance.false_positive_count || 0);
  text("dryrun-perf-fn", dryRunPerformance.false_negative_count || 0);
  text("dryrun-perf-opp", dryRunPerformance.opportunity_loss_count || 0);
  text("dryrun-perf-live-pass-win", formatRate(dryRunPerformance.live_would_pass_win_rate));
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

async function pollSnapshot() {
  const response = await fetch("/api/snapshot");
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  render(await response.json());
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

function startPolling() {
  if (state.pollTimer) return;
  pollSnapshot().catch(() => {});
  state.pollTimer = setInterval(() => pollSnapshot().catch(() => {}), 3000);
}

function connectWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${protocol}//${window.location.host}/ws/dashboard`);
  state.ws = ws;
  ws.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "snapshot") render(payload.snapshot);
  };
  ws.onclose = () => {
    startPolling();
    setTimeout(connectWebSocket, 3000);
  };
  ws.onerror = () => startPolling();
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
  setTimeout(startPolling, 2000);
  initTables();
  document.getElementById("runtime-start")?.addEventListener("click", (event) => runtimeCommand("start", event.currentTarget).catch(() => {}));
  document.getElementById("runtime-stop")?.addEventListener("click", (event) => runtimeCommand("stop", event.currentTarget).catch(() => {}));
  document.getElementById("runtime-cycle")?.addEventListener("click", (event) => runtimeCommand("cycle", event.currentTarget).catch(() => {}));
  document.getElementById("detail-close")?.addEventListener("click", closeDetailPanel);
  document.getElementById("detail-backdrop")?.addEventListener("click", closeDetailPanel);
}

initDashboard();
