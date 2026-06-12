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
        "buy-zero-rca-card",
        "buy-zero-rca-empty",
        "buy-zero-rca-market-session",
        "buy-zero-rca-status",
        "buy-zero-rca-stage-funnel",
        "buy-zero-rca-stage-candidates-body",
        "buy-zero-rca-ready-table-body",
        "buy-zero-rca-rally-table-body",
        "buy-zero-rca-refresh",
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
    assert "SNAPSHOT_POLL_INTERVAL_MS = 30000" in js
    assert "SNAPSHOT_INITIAL_FALLBACK_MS = 7000" in js
    assert "function stopPolling" in js
    assert "state.wsConnected" in js
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
    assert "function openBuyZeroTraceDetail" in js
    assert "/api/runtime/buy-zero/ready-not-ordered" in js
    assert "/api/runtime/buy-zero/missed-opportunities" in js
    assert "/api/runtime/buy-zero/traces" in js
    assert "BUY_ZERO_RCA_CRITICAL_REASONS" in js
    assert "buy-zero-rca-timeline" in js
    assert ".slice(0, 10)" not in js
    assert ".slice(0, 20)" not in js
    assert "/api/gateway/transport/latency" in js
    assert "/api/runtime/performance/dry-run/false-signals" in js
    assert "표시할 데이터가 없습니다" in js
    assert "오래된 데이터" in js
