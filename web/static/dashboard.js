const state = {
  ws: null,
  pollTimer: null,
};

function text(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value == null || value === "" ? "-" : String(value);
}

function cls(id, value) {
  const node = document.getElementById(id);
  if (node) node.className = value;
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

function rowHtml(cells) {
  return `<tr>${cells.map((cell) => `<td>${escapeHtml(cell)}</td>`).join("")}</tr>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
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

function renderInlineCounts(id, rows, key, emptyText) {
  const node = document.getElementById(id);
  if (!node) return;
  const lines = (rows || []).map((item) => `${item[key] || "-"}: ${item.count || 0}`);
  node.innerHTML = lines.length ? lines.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : `<span class="empty">${escapeHtml(emptyText)}</span>`;
}

function render(snapshot) {
  const core = snapshot.core || {};
  const gateway = snapshot.gateway || {};
  const commands = snapshot.commands || {};
  const transport = snapshot.transport || {};
  const transportExperiment = snapshot.transport_experiment || {};
  const runtime = snapshot.runtime || {};
  const dryRunOrders = snapshot.dry_run_orders || runtime.dry_run_orders || { summary: {}, items: [] };
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
  text("transport-event-p95", `${fmtNumber(transport.event_latency_p95_ms, 1)}ms`);
  text("transport-command-p95", `${fmtNumber(transport.command_latency_p95_ms, 1)}ms`);
  text("transport-ack-p95", `${fmtNumber(transport.ack_latency_p95_ms, 1)}ms`);
  text("transport-longpoll-p95", `${fmtNumber(transport.long_poll_wait_p95_ms, 1)}ms`);
  text("transport-execute-p95", `${fmtNumber(transport.gateway_execute_p95_ms, 1)}ms`);
  text("transport-rate-p95", `${fmtNumber(transport.rate_limit_wait_p95_ms, 1)}ms`);
  text("transport-empty-rate", formatRate(transport.empty_poll_rate || 0));
  text("transport-errors", transport.transport_error_count || 0);
  text("transport-reconnect", transport.reconnect_count || 0);
  text("transport-ws-decision", transport.websocket_recommendation || "KEEP_REST_LONG_POLL");
  const transportWarnings = [
    transport.websocket_recommendation_reason || "",
    ...((transport.warning_flags || []).map((flag) => `WARN: ${flag}`)),
  ].filter(Boolean);
  const transportNode = document.getElementById("transport-warning-lines");
  if (transportNode) {
    transportNode.innerHTML = transportWarnings.length ? transportWarnings.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">Transport healthy</span>';
  }
  renderRows(
    "transport-error-rows",
    (transport.recent_errors || []).slice(0, 10).map((item) =>
      rowHtml([
        item.created_at || "-",
        item.direction || "-",
        item.message_type || "-",
        item.command_id || item.event_id || "-",
        item.error || "-",
      ]),
    ),
    5,
  );

  text("transport-exp-id", transportExperiment.latest_experiment_id || "-");
  text("transport-exp-scenario", transportExperiment.latest_scenario || "-");
  text("transport-exp-decision", transportExperiment.recommendation || "NO_EXPERIMENT");
  text("transport-exp-rest-cmd", `${fmtNumber(transportExperiment.rest_command_p95_ms, 1)}ms`);
  text("transport-exp-ws-cmd", `${fmtNumber(transportExperiment.websocket_command_p95_ms, 1)}ms`);
  text("transport-exp-cmd-delta", `${fmtNumber(transportExperiment.command_p95_delta_ms, 1)}ms`);
  text("transport-exp-rest-ack", `${fmtNumber(transportExperiment.rest_ack_p95_ms, 1)}ms`);
  text("transport-exp-ws-ack", `${fmtNumber(transportExperiment.websocket_ack_p95_ms, 1)}ms`);
  text("transport-exp-ack-delta", `${fmtNumber(transportExperiment.ack_p95_delta_ms, 1)}ms`);
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
  const runtimeWarnings = [
    runtime.last_error ? `ERROR: ${runtime.last_error}` : "",
    ...(runtime.warnings || []),
  ].filter(Boolean);
  const runtimeNode = document.getElementById("runtime-warning-lines");
  if (runtimeNode) {
    runtimeNode.innerHTML = runtimeWarnings.length ? runtimeWarnings.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">No runtime warnings</span>';
  }

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
  const rejectNode = document.getElementById("dryrun-reject-reasons");
  if (rejectNode) {
    const reasons = (drySummary.top_reject_reasons || []).map((item) => `${item.reason}: ${item.count}`);
    rejectNode.innerHTML = reasons.length ? reasons.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">No reject reasons</span>';
  }
  const exitTypeNode = document.getElementById("dryrun-exit-type-lines");
  if (exitTypeNode) {
    const decisionTypes = (drySummary.exit_by_decision_type || []).map((item) => `${item.decision_type}: ${item.count}`);
    exitTypeNode.innerHTML = decisionTypes.length ? decisionTypes.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">No exit decision types</span>';
  }
  renderRows(
    "dryrun-order-rows",
    (dryRunOrders.items || []).slice(0, 20).map((item) =>
      rowHtml([
        item.created_at || "-",
        item.code || "-",
        item.side || "-",
        item.quantity || 0,
        item.price || 0,
        item.status || "-",
        item.reason || "-",
        item.live_would_pass ? "PASS" : item.live_reject_reason || "REJECT",
        item.candidate_id || "-",
        item.virtual_order_id || "-",
      ]),
    ),
    10,
  );
  renderRows(
    "dryrun-sell-order-rows",
    (dryRunOrders.recent_sell || []).slice(0, 20).map((item) =>
      rowHtml([
        item.created_at || "-",
        item.code || "-",
        item.exit_decision_type || "-",
        item.quantity || 0,
        item.price || 0,
        item.status || "-",
        item.reason || "-",
        item.live_would_pass ? "PASS" : item.live_reject_reason || "REJECT",
        item.virtual_position_id || "-",
        item.exit_decision_id || "-",
      ]),
    ),
    10,
  );

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
  renderRows(
    "dryrun-perf-case-rows",
    (dryRunPerformance.bad_cases || []).slice(0, 10).map((item) =>
      rowHtml([
        item.code || "-",
        item.quality_bucket || item.signal_classification || "-",
        item.dry_run_false_positive_type || "-",
        item.dry_run_false_negative_type || item.opportunity_loss_type || "-",
        formatPercentValue(item.realized_return_pct),
        formatPercentValue(item.max_return_20m),
        formatPercentValue(item.max_drawdown_20m),
        item.gate_reason || "-",
      ]),
    ),
    8,
  );

  text("command-queued", commands.queued_count || 0);
  text("command-dispatched", commands.dispatched_count || 0);
  text("command-acked", commands.acked_count || 0);
  text("command-failed", commands.failed_count || 0);
  text("command-expired", commands.expired_count || 0);
  text("command-duplicate", commands.duplicate_rejected_count || 0);
  text("command-rate-limited", commands.rate_limited_count || 0);
  text("command-stale", commands.stale_dispatched_count || 0);
  text("command-last-order", commands.last_order_command_at || "-");
  renderRows(
    "command-rows",
    (commands.recent || []).slice(0, 12).map((item) =>
      rowHtml([
        item.command_id || "-",
        item.created_at || "-",
        item.command_type || "-",
        item.status || "-",
        `${item.attempts || 0}/${item.max_attempts || 0}`,
        item.dedupe_key || item.idempotency_key || "-",
        item.last_error || "-",
      ]),
    ),
    7,
  );

  text("candidate-total", candidates.summary.total || 0);
  text("candidate-ready", candidates.summary.ready || 0);
  text("candidate-blocked", candidates.summary.blocked || 0);
  text("candidate-wait", candidates.summary.wait || 0);
  renderRows(
    "candidate-rows",
    (candidates.items || []).slice(0, 20).map((item) =>
      rowHtml([
        `${item.code} ${item.name || ""}`,
        item.state,
        item.theme_id || "-",
        `${fmtNumber(item.hybrid_score)} / T ${fmtNumber(item.theme_score)} / M ${fmtNumber(item.membership_score)}`,
        (item.reason_codes || []).join(", "),
      ]),
    ),
    5,
  );

  text("theme-active", themes.summary.active || 0);
  text("theme-watch", themes.summary.watch || 0);
  text("top-theme", themes.summary.top_theme || "-");
  text("theme-top-score", fmtNumber(themes.summary.top_theme_score || 0));
  renderRows(
    "theme-rows",
    (themes.items || []).slice(0, 20).map((item) =>
      rowHtml([
        item.rank || "-",
        item.theme_name || item.theme_id || "-",
        fmtNumber(item.theme_score),
        fmtNumber(item.breadth),
        fmtNumber(item.leader_gap),
        fmtNumber(item.top3_concentration),
      ]),
    ),
    6,
  );

  const orderRows = [
    ...(orders.executions || []).map((item) =>
      rowHtml([item.created_at, item.code, item.side, item.filled_quantity, item.price, `remain ${item.remaining_quantity}`]),
    ),
    ...(orders.order_results || []).map((item) => {
      const request = item.request || {};
      return rowHtml([item.created_at, request.code || "-", request.side || "-", request.quantity || 0, request.price || 0, item.ok ? "OK" : item.message]);
    }),
  ].slice(0, 30);
  text("order-count", orders.summary.execution_count || orders.summary.order_result_count || 0);
  renderRows("order-rows", orderRows, 6);

  text("review-count", reviews.summary.total || 0);
  renderRows(
    "review-rows",
    (reviews.items || []).slice(0, 30).map((item) =>
      rowHtml([
        item.code || item.candidate_id || "-",
        item.final_status || "-",
        fmtNumber(item.max_return_5m || 0),
        fmtNumber(item.max_return_10m || 0),
        fmtNumber(item.max_return_20m || 0),
        [
          item.false_positive_flag ? "false_positive" : "",
          item.false_negative_flag ? "false_negative" : "",
          item.blocked_but_later_rallied ? "blocked_rallied" : "",
          item.expired_but_later_rallied ? "expired_rallied" : "",
        ].filter(Boolean).join(", "),
      ]),
    ),
    6,
  );

  const logLines = [...(logs.warnings || []), ...(logs.core || []).slice(-80), ...(logs.gateway || []).slice(-20).map((item) => `${item.timestamp} ${item.type}`)];
  text("log-count", logLines.length);
  const logNode = document.getElementById("log-lines");
  if (logNode) {
    logNode.innerHTML = logLines.length ? logLines.map((line) => `<div>${escapeHtml(line)}</div>`).join("") : '<span class="empty">No logs</span>';
  }
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

connectWebSocket();
setTimeout(startPolling, 2000);

document.getElementById("runtime-start")?.addEventListener("click", () => runtimeCommand("start").catch(() => {}));
document.getElementById("runtime-stop")?.addEventListener("click", () => runtimeCommand("stop").catch(() => {}));
document.getElementById("runtime-cycle")?.addEventListener("click", () => runtimeCommand("cycle").catch(() => {}));
