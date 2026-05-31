from pathlib import Path

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_html_has_paginated_tables_and_detail_drawer():
    html = (ROOT / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    for table_key in [
        "transportLatency",
        "transportExperiments",
        "dryRunOrders",
        "dryRunPerformance",
        "falseSignals",
        "thresholdAB",
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
    assert "DRY_RUN 기준 제안" in html
    assert "게이트/리스크 A/B 후보" in html


def test_dashboard_js_declares_table_state_and_fetch_helpers():
    js = (ROOT / "web" / "static" / "dashboard.js").read_text(encoding="utf-8")

    assert "const tableConfigs" in js
    assert "function fetchTable" in js
    assert "function initDashboard" in js
    assert "AbortController" in js
    assert "openDetailPanel" in js
    assert "function renderOpsAlerts" in js
    assert "ops_alerts" in js
    assert "thresholdAB" in js
    assert "gradeLabelsKo" in js
    assert "/api/runtime/threshold-ab/dry-run" in js
    assert ".slice(0, 10)" not in js
    assert ".slice(0, 20)" not in js
    assert "/api/gateway/transport/latency" in js
    assert "/api/runtime/performance/dry-run/false-signals" in js
