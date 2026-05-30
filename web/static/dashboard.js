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

function render(snapshot) {
  const core = snapshot.core || {};
  const gateway = snapshot.gateway || {};
  const commands = snapshot.commands || {};
  const runtime = snapshot.runtime || {};
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
