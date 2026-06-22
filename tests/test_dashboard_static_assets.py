from pathlib import Path

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_html_has_tabs_paginated_tables_and_detail_drawer():
    html = (ROOT / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    tab_targets = {button["data-tab-target"] for button in soup.select("[data-tab-target]")}
    assert tab_targets == {"overview", "gateway", "runtime", "analysis", "logs"}
    for panel_key in tab_targets:
        assert soup.select_one(f'[data-tab-panel="{panel_key}"]') is not None

    for table_key in [
        "transportLatency",
        "transportExperiments",
        "dryRunOrders",
        "intradayDecisions",
        "intradayOutcomes",
        "shadowEvaluations",
        "shadowRiskCandidates",
        "dryRunPerformance",
        "falseSignals",
        "thresholdAB",
        "changeProposals",
        "changeProposalEvidence",
        "strategyReplayRuns",
        "gatewayCommands",
    ]:
        assert soup.select_one(f'[data-table-section="{table_key}"]') is not None
        assert soup.select_one(f'[data-table-toolbar="{table_key}"]') is not None
        assert soup.select_one(f"#{table_key}-body") is not None
        assert soup.select_one(f"#{table_key}-pagination") is not None

    assert soup.select_one("#detail-drawer") is not None
    assert soup.select_one("#detail-json") is not None
    assert soup.select_one("#ops-alert-lines") is not None
    assert soup.select_one("#ops-alert-severity") is not None
    assert soup.select_one("#themelab-operation-status") is not None
    assert soup.select_one("#themelab-ready") is not None
    for node_id in [
        "live-sim-audit-card",
        "live-sim-audit-status",
        "live-sim-audit-open-orders",
        "live-sim-audit-unknown-submit",
        "live-sim-audit-reconcile-orders",
        "live-sim-audit-broker-missing",
        "live-sim-audit-top-actions",
        "live-sim-audit-issues",
        "live-sim-preflight-card",
        "live-sim-preflight-status",
        "live-sim-preflight-message",
        "live-sim-preflight-account",
        "live-sim-preflight-gateway",
        "live-sim-preflight-live-real",
        "live-sim-preflight-kill-switch",
        "live-sim-preflight-net",
        "live-sim-preflight-accepted",
        "live-sim-preflight-bad-ready",
        "live-sim-preflight-stale",
        "live-sim-preflight-queue",
        "live-sim-preflight-backfill",
        "live-sim-preflight-blocking",
        "live-sim-preflight-action",
        "exit-policy-validation-card",
        "exit-policy-validation-status",
        "exit-policy-validation-empty",
        "exit-policy-validation-lifecycle",
        "exit-policy-validation-actual-net",
        "exit-policy-validation-best",
        "exit-policy-validation-scenarios",
        "exit-policy-validation-cases",
        "exit-policy-validation-recommendations",
        "buy-zero-rca-card",
        "buy-zero-rca-empty",
        "buy-zero-rca-market-session",
        "buy-zero-rca-status",
        "buy-zero-rca-stage-funnel",
        "buy-zero-rca-stage-candidates-body",
        "buy-zero-rca-ready-table-body",
        "buy-zero-rca-rally-table-body",
        "buy-zero-rca-refresh",
        "conservative-reason-card",
        "conservative-reason-status",
        "conservative-reason-event-count",
        "conservative-reason-group-lines",
        "conservative-reason-small-lines",
        "conservative-reason-missed-lines",
        "conservative-reason-good-lines",
        "shadow-small-entry-promotion-card",
        "shadow-small-entry-promotion-empty",
        "shadow-small-entry-promotion-status",
        "shadow-small-entry-promotion-mode",
        "shadow-small-entry-promotion-candidate-count",
        "shadow-small-entry-promotion-observe-count",
        "shadow-small-entry-promotion-promoted-count",
        "shadow-small-entry-promotion-blocked-count",
        "shadow-small-entry-promotion-group-lines",
        "shadow-small-entry-promotion-code-lines",
        "shadow-small-entry-pilot-card",
        "shadow-small-entry-pilot-status",
        "shadow-small-entry-pilot-message",
        "shadow-small-entry-pilot-recommendation",
        "shadow-small-entry-pilot-candidate-count",
        "shadow-small-entry-pilot-submitted-count",
        "shadow-small-entry-pilot-filled-count",
        "shadow-small-entry-pilot-safety-lines",
        "shadow-small-entry-pilot-items",
        "shadow-small-entry-pilot-start",
        "shadow-small-entry-pilot-complete",
        "shadow-small-entry-pilot-generate-report",
        "dashboard-v2-snapshot-source",
        "dashboard-v2-snapshot-generation",
        "dashboard-v2-market-rs-shadow-pill",
        "dashboard-v2-market-rs-shadow-message",
        "dashboard-v2-market-rs-shadow-total",
        "dashboard-v2-market-rs-shadow-healthy",
        "dashboard-v2-market-rs-shadow-weak",
        "dashboard-v2-market-rs-shadow-riskoff",
        "dashboard-v2-market-rs-shadow-systemic",
        "dashboard-v2-market-rs-shadow-labeled",
        "dashboard-v2-market-rs-shadow-mfe10",
        "dashboard-v2-market-rs-shadow-mae10",
        "dashboard-v2-market-rs-shadow-edge",
        "dashboard-v2-market-rs-shadow-risk",
        "dashboard-v2-market-rs-shadow-recommendation",
        "dashboard-v2-market-rs-shadow-order",
        "dashboard-v2-market-rs-shadow-recent",
    ]:
        assert soup.select_one(f"#{node_id}") is not None
    assert soup.select_one("#transport-real-pilot-price-sample-rate") is not None
    assert soup.select_one("#transport-real-pilot-price-sampled") is not None
    assert soup.select_one("#transport-real-pilot-price-fallback") is not None
    assert soup.select_one("#transport-real-pilot-event-fallback") is not None
    assert soup.select_one("#transport-real-pilot-last-event") is not None
    assert soup.select_one("#transport-real-pilot-last-ack") is not None
    assert soup.select_one('a[href="/themelab"]') is not None
    assert "DRY_RUN 기준 제안" in html
    assert "게이트/리스크 A/B 후보" in html
    assert "Gateway/Transport" in html
    assert "Runtime/DRY_RUN" in html
    assert "LIVE 자동주문" in html
    assert "ThemeLab 운용 요약" in html
    assert "매수 0건 RCA" in html
    assert "Exit 정책 검증" in html
    assert "보수적 차단 사유 검증" in html
    assert "READY인데 주문 안 나간 종목" in html
    assert "OBSERVE/BLOCKED 이후 급등 후보" in html


def test_dashboard_html_keeps_safety_boundary_buttons_read_only():
    html = (ROOT / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    button_text = " ".join(button.get_text(" ", strip=True) for button in soup.select("button"))
    assert "실제 주문" not in button_text
    assert "전송 전환" not in button_text
    assert "LIVE 자동주문" not in button_text


def test_dashboard_js_declares_table_state_and_fetch_helpers():
    js = (ROOT / "web" / "static" / "dashboard.js").read_text(encoding="utf-8")

    assert "const tableConfigs" in js
    assert "function fetchTable" in js
    assert "function initDashboard" in js
    assert "function initTabs" in js
    assert "AbortController" in js
    assert "openDetailPanel" in js
    assert "updateFreshness" in js
    assert "runProtectedTableAction" in js
    assert "runWithLocalTokenRetry" in js
    assert "isInvalidTokenResponse" in js
    assert "TOKEN_STORAGE_KEY" in js
    assert "invalid local gateway token" in js
    assert "function renderOpsAlerts" in js
    assert "function renderThemeLabSummary" in js
    assert 'timeZone: "Asia/Seoul"' in js
    assert " KST" in js
    assert "composite_market_mode" in js
    assert "systemic_risk_off" in js
    assert "SYSTEMIC_RISK_OFF" in js
    assert "market_relative_strength_shadow" in js
    assert "function renderDashboardV2MarketRsShadow" in js
    assert "function renderDashboardV2MarketRsShadowRecent" in js
    assert "matured_pending_count" in js
    assert "persisted_outcome_count" in js
    assert "RISK_OFF_SIDE_DIAGNOSTIC" in js
    assert "SNAPSHOT_POLL_INTERVAL_MS = 30000" in js
    assert "SNAPSHOT_INITIAL_FALLBACK_MS = 7000" in js
    assert "function stopPolling" in js
    assert "state.wsConnected" in js
    assert "function normalizeSnapshotEnvelope" in js
    assert "function snapshotIdentity" in js
    assert "function compareSnapshotOrder" in js
    assert "function shouldAcceptSnapshot" in js
    assert "function applySnapshotIfNewer" in js
    assert 'fetch("/api/snapshot?view=v2", { signal: controller.signal })' in js
    assert 'applySnapshotIfNewer(snapshot, "poll")' in js
    assert 'applySnapshotIfNewer(payload.snapshot, "ws")' in js
    assert 'render(payload.snapshot)' not in js
    assert "setTimeout(startPolling, 2000)" not in js
    assert "ops_alerts" in js
    assert "theme_lab" in js
    assert "price_tick_sample_rate" in js
    assert "price_tick_sampled_count" in js
    assert "price_tick_fallback_count" in js
    assert "event_fallback_count" in js
    assert "last_ws_event_at" in js
    assert "last_ws_ack_at" in js
    assert "thresholdAB" in js
    assert "changeProposals" in js
    assert "changeProposalEvidence" in js
    assert "strategyReplayRuns" in js
    assert "/api/runtime/change-proposals" in js
    assert "Approve Observe" in js
    assert "자동 적용 아님" in js
    assert "function actionProposal" in js
    assert "(payload || {}).proposal" in js
    assert "/api/runtime/replay/runs" in js
    assert "gradeLabelsKo" in js
    assert "/api/runtime/threshold-ab/dry-run" in js
    assert "/api/runtime/shadow-strategies/evaluations" in js
    assert "shadowEvaluations" in js
    assert "shadowRiskCandidates" in js
    assert "function renderBuyZeroRca" in js
    assert "function renderLiveSimAudit" in js
    assert "function renderLiveSimPreflight" in js
    assert "function renderExitPolicyValidation" in js
    assert "exit_policy_validation" in js
    assert "live_sim_preflight" in js
    assert "LIVE_SIM 사전 점검" in js
    assert "function renderConservativeReasonOutcomes" in js
    assert "function renderConservativeStockLines" in js
    assert "function renderShadowSmallEntryPromotion" in js
    assert "function renderShadowSmallEntryOps" in js
    assert "function renderShadowSmallEntryPilot" in js
    assert "function shadowSmallEntryPilotAction" in js
    assert "shadow_small_entry_ops" in js
    assert "shadow_small_entry_pilot" in js
    assert "/api/shadow-small-entry-ops/arm" in js
    assert "/api/shadow-small-entry-ops/confirm" in js
    assert "/api/shadow-small-entry-ops/rollback" in js
    assert "/api/shadow-small-entry-pilot/start" in js
    assert "/api/shadow-small-entry-pilot/generate-report" in js
    assert "shadow_small_entry_promotion" in js
    assert "conservative_reason_outcomes" in js
    assert "live_sim_audit" in js
    assert "function renderBuyZeroDataQualityCounts" in js
    assert "function openBuyZeroTraceDetail" in js
    assert "/api/runtime/buy-zero/ready-not-ordered" in js
    assert "/api/runtime/buy-zero/missed-opportunities" in js
    assert "/api/runtime/buy-zero/traces" in js
    assert "BUY_ZERO_RCA_CRITICAL_REASONS" in js
    assert "WARMUP_OPTIONAL" in js
    assert "buy-zero-rca-timeline" in js
    assert ".slice(0, 10)" not in js
    assert ".slice(0, 20)" not in js
    assert "/api/gateway/transport/latency" in js
    assert "/api/runtime/performance/dry-run/false-signals" in js


def test_dashboard_shadow_small_entry_ops_dom_ids():
    html = (ROOT / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    js = (ROOT / "web" / "static" / "dashboard.js").read_text(encoding="utf-8")

    for selector in [
        'id="shadow-small-entry-ops-card"',
        'id="shadow-small-entry-ops-status"',
        'id="shadow-small-entry-ops-mode"',
        'id="shadow-small-entry-ops-order-enabled"',
        'id="shadow-small-entry-ops-preflight-status"',
        'id="shadow-small-entry-ops-blocking-reasons"',
        'id="shadow-small-entry-ops-risk-lines"',
        'id="shadow-small-entry-ops-preflight"',
        'id="shadow-small-entry-ops-arm"',
        'id="shadow-small-entry-ops-confirm"',
        'id="shadow-small-entry-ops-pause"',
        'id="shadow-small-entry-ops-rollback"',
        'id="shadow-small-entry-ops-emergency-pause"',
    ]:
        assert selector in html
    assert "표시할 데이터가 없습니다" in js
    assert "오래된 데이터" in js
