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
    detailTitle: (item) => `Transport sample ${item.sample_id || ""}`,
    detailEndpoint: (item) => item.sample_id ? `/api/gateway/transport/latency/${encodeURIComponent(item.sample_id)}` : "",
    actionLabel: "Rebuild transport report",
    actionEndpoint: (filters) => `/api/gateway/transport/latency/rebuild?persist=true&export=true${filters.trade_date ? `&trade_date=${encodeURIComponent(filters.trade_date)}` : ""}`,
    columns: [
      (item) => formatDateTime(item.created_at),
      (item) => badge(item.transport_mode || "-"),
      (item) => item.direction || "-",
      (item) => item.message_type || "-",
      (item) => compactId(item.command_id || item.event_id || "-"),
      (item) => formatMs(item.total_wall_ms),
      (item) => formatMs(item.long_poll_wait_ms),
      (item) => formatMs(item.gateway_execute_ms),
      (item) => formatMs(item.ack_round_trip_ms),
      (item) => item.error || "-",
    ],
  },
  transportExperiments: {
    endpoint: "/api/gateway/transport/experiments",
    bodyId: "transportExperiments-body",
    statusId: "transportExperiments-status",
    paginationId: "transportExperiments-pagination",
    defaultLimit: 25,
    detailTitle: (item) => `Experiment ${item.experiment_id || ""}`,
    detailEndpoint: (item) => item.experiment_id ? `/api/gateway/transport/experiments/${encodeURIComponent(item.experiment_id)}${item.scenario ? `?scenario=${encodeURIComponent(item.scenario)}` : ""}` : "",
    actionLabel: "Rebuild comparison",
    actionEndpoint: (filters) => {
      const params = buildQuery({ experiment_id: filters.experiment_id, scenario: filters.scenario, persist: true, export: true });
      return `/api/gateway/transport/experiments/rebuild?${params}`;
    },
    columns: [
      (item) => compactId(item.experiment_id || "-"),
      (item) => item.scenario || "-",
      (item) => formatDateTime(item.started_at),
      (item) => formatDateTime(item.ended_at),
      (item) => (item.sample_counts || {}).rest_long_poll ?? "-",
      (item) => (item.sample_counts || {}).websocket_mock ?? "-",
      (item) => formatMs((item.rest_summary || {}).command_latency_p95_ms),
      (item) => formatMs((item.websocket_summary || {}).command_latency_p95_ms),
      (item) => formatMs((item.delta || {}).command_p95_delta_ms),
      (item) => badge(item.latest_recommendation || "-"),
      (item) => item.real_gateway_switch_ready ? "YES" : "NO",
    ],
  },
  dryRunOrders: {
    endpoint: "/api/runtime/orders/dry-run",
    bodyId: "dryRunOrders-body",
    statusId: "dryRunOrders-status",
    paginationId: "dryRunOrders-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `DRY_RUN intent ${item.intent_id || ""}`,
    detailEndpoint: (item) => item.intent_id ? `/api/runtime/orders/dry-run/${encodeURIComponent(item.intent_id)}` : "",
    columns: [
      (item) => formatDateTime(item.created_at),
      (item) => item.code || "-",
      (item) => item.side || "-",
      (item) => item.order_phase || "-",
      (item) => item.quantity ?? 0,
      (item) => item.price ?? 0,
      (item) => badge(item.status || "-"),
      (item) => item.reason || "-",
      (item) => item.live_would_pass ? badge("PASS", "ok") : badge(item.live_reject_reason || "REJECT", "warn"),
      (item) => item.candidate_id ?? "-",
      (item) => [item.virtual_order_id, item.virtual_position_id, item.exit_decision_id].filter(Boolean).join(" / ") || "-",
    ],
  },
  dryRunPerformance: {
    endpoint: "/api/runtime/performance/dry-run",
    bodyId: "dryRunPerformance-body",
    statusId: "dryRunPerformance-status",
    paginationId: "dryRunPerformance-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `Lifecycle ${item.lifecycle_id || ""}`,
    detailEndpoint: (item, filters) => item.lifecycle_id ? `/api/runtime/performance/dry-run/lifecycles/${encodeURIComponent(item.lifecycle_id)}${filters.trade_date ? `?trade_date=${encodeURIComponent(filters.trade_date)}` : ""}` : "",
    actionLabel: "Rebuild performance report",
    actionEndpoint: (filters) => `/api/runtime/performance/dry-run/rebuild?persist=true&export=true&format=all${filters.trade_date ? `&trade_date=${encodeURIComponent(filters.trade_date)}` : ""}`,
    columns: [
      (item) => item.code || "-",
      (item) => item.strategy_name || "-",
      (item) => item.theme_name || "-",
      (item) => badge(item.quality_bucket || item.signal_classification || "-"),
      (item) => item.dry_run_false_positive_type || "-",
      (item) => item.dry_run_false_negative_type || item.opportunity_loss_type || "-",
      (item) => formatPercentValue(item.realized_return_pct),
      (item) => formatPercentValue(item.max_return_20m),
      (item) => formatPercentValue(item.max_drawdown_20m),
      (item) => item.gate_reason || "-",
    ],
  },
  falseSignals: {
    endpoint: "/api/runtime/performance/dry-run/false-signals",
    bodyId: "falseSignals-body",
    statusId: "falseSignals-status",
    paginationId: "falseSignals-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `False signal ${item.lifecycle_id || item.code || ""}`,
    detailEndpoint: (item, filters) => item.lifecycle_id ? `/api/runtime/performance/dry-run/lifecycles/${encodeURIComponent(item.lifecycle_id)}${filters.trade_date ? `?trade_date=${encodeURIComponent(filters.trade_date)}` : ""}` : "",
    columns: [
      (item) => item.code || "-",
      (item) => badge(item.signal_classification || "-"),
      (item) => item.dry_run_false_positive_type || item.dry_run_false_negative_type || item.opportunity_loss_type || "-",
      (item) => formatPercentValue(item.realized_return_pct),
      (item) => formatPercentValue(item.max_return_20m),
      (item) => formatPercentValue(item.max_drawdown_20m),
      (item) => item.entry_live_reject_reason || item.entry_decision_safety_reason || "-",
      (item) => item.gate_reason || "-",
      (item) => compactId(item.lifecycle_id || "-"),
    ],
  },
  gatewayCommands: {
    endpoint: "/api/gateway/commands/history",
    bodyId: "gatewayCommands-body",
    statusId: "gatewayCommands-status",
    paginationId: "gatewayCommands-pagination",
    defaultLimit: 50,
    detailTitle: (item) => `Command ${item.command_id || ""}`,
    detailEndpoint: (item) => item.command_id ? `/api/gateway/commands/${encodeURIComponent(item.command_id)}` : "",
    columns: [
      (item) => compactId(item.command_id || "-"),
      (item) => formatDateTime(item.created_at),
      (item) => item.command_type || "-",
      (item) => badge(item.status || "-"),
      (item) => `${item.attempts || 0}/${item.max_attempts || 0}`,
      (item) => compactId(item.dedupe_key || item.idempotency_key || "-"),
      (item) => item.last_error || "-",
    ],
  },
};

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

function badge(value, tone = "") {
  const normalized = String(value || "-");
  let badgeTone = tone;
  if (!badgeTone) {
    if (/ACKED|ACCEPT|PASS|READY|OK|TRUE_POSITIVE/i.test(normalized)) badgeTone = "ok";
    else if (/FAIL|REJECT|EXPIRED|BLOCK|LOSS|FALSE/i.test(normalized)) badgeTone = "bad";
    else if (/DUP|WAIT|PENDING|WARN/i.test(normalized)) badgeTone = "warn";
    else badgeTone = "muted";
  }
  return `<span class="badge ${badgeTone}">${escapeHtml(normalized)}</span>`;
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

function buildQuery(params) {
  const search = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    search.set(key, String(value));
  });
  return search.toString();
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
    <button type="button" data-table-action="${tableKey}:apply">Apply</button>
    <button type="button" data-table-action="${tableKey}:reset">Reset</button>
    <button type="button" data-table-action="${tableKey}:reload">Reload</button>
    <label class="inline-control">Page size
      <select data-table-action="${tableKey}:limit">
        ${option(25)}${option(50)}${option(100)}${option(200)}
      </select>
    </label>
    <label class="inline-control"><input type="checkbox" data-table-action="${tableKey}:auto" /> Auto refresh</label>
    ${config.actionEndpoint ? `<button type="button" data-table-action="${tableKey}:protected">${escapeHtml(config.actionLabel || "Rebuild")}</button>` : ""}
    <span id="${tableKey}-freshness" class="freshness">not fetched</span>
  `;
}

function bindTableControls(tableKey) {
  document.querySelectorAll(`[data-table-action^="${tableKey}:"]`).forEach((node) => {
    const action = node.dataset.tableAction.split(":")[1];
    node.addEventListener("click", () => handleTableAction(tableKey, action, node).catch((error) => setTableStatus(tableKey, error.message, "bad")));
    if (action === "limit") {
      node.addEventListener("change", () => {
        updateTableState(tableKey, { limit: Number(node.value), offset: 0 });
        fetchTable(tableKey).catch((error) => setTableStatus(tableKey, error.message, "bad"));
      });
    }
    if (action === "auto") {
      node.addEventListener("change", () => toggleAutoRefresh(tableKey, node.checked));
    }
  });
}

async function handleTableAction(tableKey, action) {
  if (action === "apply") {
    updateTableState(tableKey, { offset: 0 });
    await fetchTable(tableKey);
  } else if (action === "reset") {
    document.querySelectorAll(`[data-table="${tableKey}"][data-filter]`).forEach((node) => {
      if (node.tagName === "SELECT") node.selectedIndex = 0;
      else node.value = "";
    });
    updateTableState(tableKey, { offset: 0 });
    await fetchTable(tableKey);
  } else if (action === "reload") {
    await fetchTable(tableKey);
  } else if (action === "protected") {
    await runProtectedTableAction(tableKey);
  }
}

async function runProtectedTableAction(tableKey) {
  const config = tableConfigs[tableKey];
  let token = localStorage.getItem("TRADING_CORE_TOKEN") || "";
  if (!token) {
    token = window.prompt("TRADING_CORE_TOKEN") || "";
    if (token) localStorage.setItem("TRADING_CORE_TOKEN", token);
  }
  if (!token || !config.actionEndpoint) return;
  setTableStatus(tableKey, "running", "warn");
  const filters = tableFilters(tableKey);
  const response = await fetch(config.actionEndpoint(filters), {
    method: "POST",
    headers: { "X-Local-Token": token },
  });
  const payload = await response.json();
  openDetailPanel(`${config.actionLabel || "Action"} result`, payload);
  await fetchTable(tableKey);
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
  setTableStatus(tableKey, "loading", "warn");
  const filters = tableFilters(tableKey);
  const params = { ...filters, limit: table.limit, offset: table.offset };
  try {
    const payload = await apiGet(config.endpoint, params, tableKey);
    if (state.tables[tableKey].requestSeq !== seq) return;
    const items = normalizeItems(payload);
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

function normalizeItems(payload) {
  return payload.items || payload.samples || [];
}

function renderTable(tableKey) {
  const config = tableConfigs[tableKey];
  const table = state.tables[tableKey];
  const body = document.getElementById(config.bodyId);
  if (!body) return;
  const items = table.items || [];
  if (!items.length) {
    body.innerHTML = `<tr><td class="empty" colspan="${config.columns.length}">No rows</td></tr>`;
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
  const fetched = table.lastFetchedAt ? table.lastFetchedAt.toLocaleTimeString() : "not fetched";
  const stale = table.lastFetchedAt && Date.now() - table.lastFetchedAt.getTime() > 30000;
  const freshness = document.getElementById(`${tableKey}-freshness`);
  if (freshness) {
    freshness.textContent = `${stale ? "stale " : ""}fetched ${fetched}`;
    freshness.className = `freshness ${stale ? "stale" : ""}`;
  }
  setTableStatus(tableKey, `${(table.pagination || {}).count ?? items.length} rows`, stale ? "warn" : "ok");
}

function renderTableError(tableKey, message) {
  const config = tableConfigs[tableKey];
  const body = document.getElementById(config.bodyId);
  if (body) body.innerHTML = `<tr><td class="error-row" colspan="${config.columns.length}">${escapeHtml(message)}</td></tr>`;
  setTableStatus(tableKey, "error", "bad");
}

function renderPagination(tableKey) {
  const table = state.tables[tableKey];
  const page = table.pagination || {};
  const node = document.getElementById(tableConfigs[tableKey].paginationId);
  if (!node) return;
  const currentPage = Math.floor((page.offset || 0) / Math.max(1, page.limit || table.limit)) + 1;
  const totalText = page.total != null ? ` / ${page.total}` : "";
  node.innerHTML = `
    <button type="button" ${page.has_prev ? "" : "disabled"} data-page="${tableKey}:prev">Prev</button>
    <span>Page ${currentPage} (${page.count || 0}${totalText})</span>
    <button type="button" ${page.has_next ? "" : "disabled"} data-page="${tableKey}:next">Next</button>
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
  setTableStatus(tableKey, "detail", "warn");
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
  const record = payload.record || payload.item || payload.report || payload;
  const keys = ["command_id", "intent_id", "sample_id", "experiment_id", "lifecycle_id", "status", "command_type", "code", "reason", "error"];
  return keys
    .filter((key) => record && record[key] != null && record[key] !== "")
    .map((key) => `<div><span>${escapeHtml(key)}</span><strong>${escapeHtml(record[key])}</strong></div>`)
    .join("") || '<span class="empty">No compact summary</span>';
}

function renderRows(id, rows, emptyColumns) {
  const body = document.getElementById(id);
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = `<tr><td class="empty" colspan="${emptyColumns}">No rows</td></tr>`;
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

function render(snapshot) {
  state.latestSnapshot = snapshot;
  const core = snapshot.core || {};
  const gateway = snapshot.gateway || {};
  const commands = snapshot.commands || {};
  const transport = snapshot.transport || {};
  const transportExperiment = snapshot.transport_experiment || {};
  const runtime = snapshot.runtime || {};
  const dryRunOrders = snapshot.dry_run_orders || runtime.dry_run_orders || { summary: {} };
  const dryRunPerformance = snapshot.dry_run_performance || runtime.dry_run_performance || {};
  const candidates = snapshot.candidates || { summary: {}, items: [] };
  const themes = snapshot.themes || { summary: {}, items: [] };
  const orders = snapshot.orders || { summary: {}, order_results: [], executions: [] };
  const reviews = snapshot.reviews || { summary: {}, items: [] };
  const logs = snapshot.logs || { core: [], gateway: [], warnings: [] };

  text("snapshot-time", snapshot.timestamp || "");
  text("core-mode", core.mode || "OBSERVE");
  text("core-state", core.service ? "OK" : "WAIT");
  const gatewayState = gateway.connection_state || "DISCONNECTED";
  text("gateway-state", gatewayState);
  text("gateway-connection", gatewayState);
  text("kiwoom-login", gateway.kiwoom_logged_in ? "YES" : "NO");
  text("heartbeat-age", gateway.heartbeat_age_sec == null ? "-" : `${fmtNumber(gateway.heartbeat_age_sec, 0)}s`);
  text("orderable-state", gateway.orderable ? "ORDERABLE" : "ORDER BLOCKED");
  cls("gateway-state", `pill ${gateway.heartbeat_ok ? "ok" : gateway.connected ? "warn" : "bad"}`);
  cls("orderable-state", `pill ${gateway.orderable ? "ok" : "muted"}`);

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
  const transportWarnings = [transport.websocket_recommendation_reason || "", ...((transport.warning_flags || []).map((flag) => `WARN: ${flag}`))].filter(Boolean);
  const transportNode = document.getElementById("transport-warning-lines");
  if (transportNode) transportNode.innerHTML = transportWarnings.length ? transportWarnings.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">Transport healthy</span>';

  text("transport-exp-id", transportExperiment.latest_experiment_id || "-");
  text("transport-exp-scenario", transportExperiment.latest_scenario || "-");
  text("transport-exp-decision", transportExperiment.recommendation || "NO_EXPERIMENT");
  text("transport-exp-rest-cmd", formatMs(transportExperiment.rest_command_p95_ms));
  text("transport-exp-ws-cmd", formatMs(transportExperiment.websocket_command_p95_ms));
  text("transport-exp-cmd-delta", formatMs(transportExperiment.command_p95_delta_ms));
  text("transport-exp-rest-ack", formatMs(transportExperiment.rest_ack_p95_ms));
  text("transport-exp-ws-ack", formatMs(transportExperiment.websocket_ack_p95_ms));
  text("transport-exp-ack-delta", formatMs(transportExperiment.ack_p95_delta_ms));
  text("transport-exp-ready", transportExperiment.real_gateway_switch_ready ? "YES" : "NO");
  const expBlockerNode = document.getElementById("transport-exp-blockers");
  if (expBlockerNode) {
    const blockers = transportExperiment.blockers || [];
    expBlockerNode.innerHTML = blockers.length ? blockers.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">No blockers from latest mock comparison</span>';
  }

  text("runtime-enabled", runtime.enabled ? "YES" : "NO");
  text("runtime-running", runtime.running ? "YES" : "NO");
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
  const runtimeWarnings = [runtime.last_error ? `ERROR: ${runtime.last_error}` : "", ...(runtime.warnings || [])].filter(Boolean);
  const runtimeNode = document.getElementById("runtime-warning-lines");
  if (runtimeNode) runtimeNode.innerHTML = runtimeWarnings.length ? runtimeWarnings.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">No runtime warnings</span>';

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
  renderInlineCounts("dryrun-reject-reasons", drySummary.top_reject_reasons || [], "reason", "No reject reasons");
  renderInlineCounts("dryrun-exit-type-lines", drySummary.exit_by_decision_type || [], "decision_type", "No exit decision types");

  text("dryrun-performance-generated", dryRunPerformance.generated_at || "-");
  text("dryrun-perf-total", dryRunPerformance.total_lifecycle_count || 0);
  text("dryrun-perf-completed", dryRunPerformance.completed_lifecycle_count || 0);
  text("dryrun-perf-win-rate", formatRate(dryRunPerformance.win_rate));
  text("dryrun-perf-avg-return", formatPercentValue(dryRunPerformance.avg_realized_return_pct));
  text("dryrun-perf-fp", dryRunPerformance.false_positive_count || 0);
  text("dryrun-perf-fn", dryRunPerformance.false_negative_count || 0);
  text("dryrun-perf-opp", dryRunPerformance.opportunity_loss_count || 0);
  text("dryrun-perf-live-pass-win", formatRate(dryRunPerformance.live_would_pass_win_rate));
  renderInlineCounts("dryrun-perf-fp-lines", dryRunPerformance.top_false_positive_types || [], "type", "No false positive types");
  renderInlineCounts("dryrun-perf-fn-lines", dryRunPerformance.top_false_negative_types || [], "type", "No false negative types");
  renderInlineCounts("dryrun-perf-reject-rally-lines", dryRunPerformance.top_reject_reasons_with_rally || [], "reason", "No reject reasons with rally");

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
  renderRows("candidate-rows", firstItems(candidates.items, 20).map((item) => rowHtml([`${item.code} ${item.name || ""}`, item.state, item.theme_id || "-", `${fmtNumber(item.hybrid_score)} / T ${fmtNumber(item.theme_score)} / M ${fmtNumber(item.membership_score)}`, (item.reason_codes || []).join(", ")])), 5);

  text("theme-active", themes.summary.active || 0);
  text("theme-watch", themes.summary.watch || 0);
  text("top-theme", themes.summary.top_theme || "-");
  text("theme-top-score", fmtNumber(themes.summary.top_theme_score || 0));
  renderRows("theme-rows", firstItems(themes.items, 20).map((item) => rowHtml([item.rank || "-", item.theme_name || item.theme_id || "-", fmtNumber(item.theme_score), fmtNumber(item.breadth), fmtNumber(item.leader_gap), fmtNumber(item.top3_concentration)])), 6);

  const orderRows = [];
  for (const item of firstItems(orders.executions || [], 20)) orderRows.push(rowHtml([item.created_at, item.code, item.side, item.filled_quantity, item.price, `remain ${item.remaining_quantity}`]));
  for (const item of firstItems(orders.order_results || [], Math.max(0, 30 - orderRows.length))) {
    const request = item.request || {};
    orderRows.push(rowHtml([item.created_at, request.code || "-", request.side || "-", request.quantity || 0, request.price || 0, item.ok ? "OK" : item.message]));
  }
  text("order-count", orders.summary.execution_count || orders.summary.order_result_count || 0);
  renderRows("order-rows", orderRows, 6);

  text("review-count", reviews.summary.total || 0);
  renderRows("review-rows", firstItems(reviews.items, 30).map((item) => rowHtml([item.code || item.candidate_id || "-", item.final_status || "-", fmtNumber(item.max_return_5m || 0), fmtNumber(item.max_return_10m || 0), fmtNumber(item.max_return_20m || 0), [item.false_positive_flag ? "false_positive" : "", item.false_negative_flag ? "false_negative" : "", item.blocked_but_later_rallied ? "blocked_rallied" : "", item.expired_but_later_rallied ? "expired_rallied" : ""].filter(Boolean).join(", ")])), 6);

  const logLines = [...(logs.warnings || []), ...firstItems(logs.core || [], 80), ...firstItems(logs.gateway || [], 20).map((item) => `${item.timestamp} ${item.type}`)];
  text("log-count", logLines.length);
  const logNode = document.getElementById("log-lines");
  if (logNode) logNode.innerHTML = logLines.length ? logLines.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">No logs</span>';
}

async function pollSnapshot() {
  const response = await fetch("/api/snapshot");
  render(await response.json());
}

async function runtimeCommand(action) {
  let token = localStorage.getItem("TRADING_CORE_TOKEN") || "";
  if (!token) {
    token = window.prompt("TRADING_CORE_TOKEN") || "";
    if (token) localStorage.setItem("TRADING_CORE_TOKEN", token);
  }
  if (!token) return;
  await fetch(`/api/runtime/${action}`, {
    method: "POST",
    headers: { "X-Local-Token": token },
  });
  await pollSnapshot();
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

function initTables() {
  Object.entries(tableConfigs).forEach(([tableKey, config]) => {
    state.tables[tableKey] = { limit: config.defaultLimit || 50, offset: 0, requestSeq: 0 };
    renderToolbar(tableKey);
    bindTableControls(tableKey);
    fetchTable(tableKey).catch((error) => setTableStatus(tableKey, error.message, "bad"));
  });
}

function initDashboard() {
  connectWebSocket();
  setTimeout(startPolling, 2000);
  initTables();
  document.getElementById("runtime-start")?.addEventListener("click", () => runtimeCommand("start").catch(() => {}));
  document.getElementById("runtime-stop")?.addEventListener("click", () => runtimeCommand("stop").catch(() => {}));
  document.getElementById("runtime-cycle")?.addEventListener("click", () => runtimeCommand("cycle").catch(() => {}));
  document.getElementById("detail-close")?.addEventListener("click", closeDetailPanel);
  document.getElementById("detail-backdrop")?.addEventListener("click", closeDetailPanel);
}

initDashboard();
