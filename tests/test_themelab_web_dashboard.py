from __future__ import annotations

import importlib
from datetime import datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading.broker.command_queue import CommandPriority
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayCommand, GatewayEvent, utc_timestamp
from trading.strategy.conditions import ConditionProfile, ConditionProfileRepository
from trading.strategy.models import Candidate, CandidateState
from trading.strategy.models import StrategyProfile
from trading.theme_engine.backfill import THEME_BACKFILL_PURPOSE
from trading.theme_engine.models import CanonicalTheme, ThemeMembership, ThemeSourceSyncResult, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository
from trading_app.themelab_dashboard import build_theme_lab_dashboard_snapshot


ROOT = Path(__file__).resolve().parents[1]


def test_themelab_page_is_standalone_dark_terminal():
    html = (ROOT / "web" / "templates" / "themelab.html").read_text(encoding="utf-8")
    css = (ROOT / "web" / "static" / "themelab.css").read_text(encoding="utf-8")
    js = (ROOT / "web" / "static" / "themelab.js").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    assert soup.select_one(".terminal-shell") is not None
    assert soup.select_one("#operating-cockpit") is not None
    assert soup.select_one("#operation-status") is not None
    assert soup.select_one("#naver-theme-sync") is not None
    assert soup.select_one("#naver-theme-sync-status") is not None
    assert soup.select_one("#kiwoom-gateway-start") is not None
    assert soup.select_one("#operator-alert-panel") is not None
    assert soup.select_one("#operator-alert-count") is not None
    assert soup.select_one("#operator-alert-filters") is not None
    assert soup.select_one("#operator-alert-list") is not None
    assert soup.select_one("#operator-timeline-list") is not None
    assert soup.select_one("#operator-alert-ack-all") is not None
    assert soup.select_one("#operator-alert-hide-acknowledged") is not None
    assert soup.select_one("#operator-session-review") is not None
    assert soup.select_one("#operator-event-sync-status") is not None
    assert soup.select_one("#operator-session-summary-cards") is not None
    assert soup.select_one("#operator-event-journal") is not None
    assert soup.select_one("#operator-event-journal-filters") is not None
    assert soup.select_one("#operator-event-journal-list") is not None
    assert soup.select_one("#operator-action-center") is not None
    assert soup.select_one("#operator-action-context") is not None
    assert soup.select_one("#operator-action-recommendations") is not None
    assert soup.select_one("#operator-action-history") is not None
    assert soup.select_one("#operator-action-summary") is not None
    assert soup.select_one("#operator-action-confirm-modal") is not None
    assert soup.select_one("#operator-runbook-panel") is not None
    assert soup.select_one("#operator-runbook-title") is not None
    assert soup.select_one("#operator-runbook-body") is not None
    assert soup.select_one("#postmarket-review-panel") is not None
    assert soup.select_one(".postmarket-review-toolbar") is not None
    assert soup.select_one("#postmarket-review-summary") is not None
    assert soup.select_one("#postmarket-review-tabs") is not None
    assert soup.select_one("#missed-opportunity-list") is not None
    assert soup.select_one("#good-block-list") is not None
    assert soup.select_one("#review-needed-list") is not None
    assert soup.select_one("#protected-from-chase-list") is not None
    assert soup.select_one("#data-insufficient-list") is not None
    assert soup.select_one("#block-reason-summary") is not None
    assert soup.select_one("#postmarket-review-detail") is not None
    assert soup.select_one('[data-alert-filter="OPPORTUNITY"]') is not None
    assert soup.select_one('[data-alert-filter="CRITICAL"]') is not None
    assert soup.select_one('[data-journal-filter="SYMBOL"]') is not None
    assert soup.select_one("#cockpit-market-sides") is not None
    assert soup.select_one("#cockpit-live-readiness") is not None
    assert soup.select_one("#themelab-buy-zero-rca-card") is not None
    assert soup.select_one("#themelab-buy-zero-rca-status") is not None
    assert soup.select_one("#themelab-buy-zero-stage-funnel") is not None
    assert soup.select_one("#themelab-buy-zero-ready-table-body") is not None
    assert soup.select_one("#themelab-buy-zero-rally-table-body") is not None
    assert soup.select_one("#themelab-buy-zero-rca-timeline") is not None
    assert soup.select_one("#themelab-buy-zero-rca-refresh") is not None
    assert soup.select_one("#themelab-live-sim-audit-card") is not None
    assert soup.select_one("#themelab-live-sim-audit-status") is not None
    assert soup.select_one("#themelab-live-sim-audit-open-orders") is not None
    assert soup.select_one("#themelab-live-sim-audit-top-actions") is not None
    assert soup.select_one("#themelab-live-sim-audit-issues") is not None
    assert soup.select_one("#themelab-conservative-reason-card") is not None
    assert soup.select_one("#themelab-conservative-reason-status") is not None
    assert soup.select_one("#themelab-conservative-reason-group-body") is not None
    assert soup.select_one("#themelab-conservative-reason-code-body") is not None
    assert soup.select_one("#themelab-conservative-reason-small-body") is not None
    assert soup.select_one("#themelab-conservative-reason-data-body") is not None
    assert soup.select_one("#themelab-conservative-reason-missed-body") is not None
    assert soup.select_one("#themelab-conservative-reason-good-body") is not None
    assert soup.select_one("#themelab-shadow-small-entry-promotion-card") is not None
    assert soup.select_one("#themelab-shadow-small-entry-promotion-empty") is not None
    assert soup.select_one("#themelab-shadow-small-entry-promotion-status") is not None
    assert soup.select_one("#themelab-shadow-small-entry-promotion-mode") is not None
    assert soup.select_one("#themelab-shadow-small-entry-promotion-candidate-count") is not None
    assert soup.select_one("#themelab-shadow-small-entry-promotion-observe-count") is not None
    assert soup.select_one("#themelab-shadow-small-entry-promotion-promoted-count") is not None
    assert soup.select_one("#themelab-shadow-small-entry-promotion-blocked-count") is not None
    assert soup.select_one("#themelab-shadow-small-entry-promotion-group-lines") is not None
    assert soup.select_one("#themelab-shadow-small-entry-promotion-code-lines") is not None
    assert soup.select_one("#themelab-shadow-small-entry-promotion-candidate-body") is not None
    assert soup.select_one("#promotion-cockpit") is not None
    assert soup.select_one("#promotion-window-controls") is not None
    assert soup.select_one('[data-promotion-window="300"]') is not None
    assert soup.select_one("#promotion-decision-refresh") is not None
    assert soup.select_one("#promotion-action-status") is not None
    assert soup.select_one("#promotion-confidence") is not None
    assert soup.select_one("#promotion-stage-lines") is not None
    assert soup.select_one("#promotion-metric-lines") is not None
    assert soup.select_one("#promotion-blocker-list") is not None
    assert soup.select_one("#promotion-rollout-lines") is not None
    assert soup.select_one("#promotion-stage-matrix") is not None
    assert soup.select_one("#promotion-drilldown-panel") is not None
    assert soup.select_one("#promotion-drilldown-title") is not None
    assert soup.select_one("#promotion-drilldown-status") is not None
    assert soup.select_one("#promotion-drilldown-summary") is not None
    assert soup.select_one("#promotion-drilldown-list") is not None
    assert soup.select_one("#shadow-ab-status") is not None
    assert soup.select_one("#shadow-ab-best") is not None
    assert soup.select_one("#shadow-ab-summary") is not None
    assert soup.select_one("#shadow-ab-body") is not None
    assert soup.select_one("#theme-rank-list") is not None
    assert soup.select_one("#candidate-focus-panel") is not None
    assert soup.select_one("#focus-summary") is not None
    assert soup.select_one("#decision-checklist") is not None
    assert soup.select_one("#price-map") is not None
    assert soup.select_one("#chart-timeframe") is not None
    assert soup.select_one('[data-chart-interval="1m"]') is not None
    assert soup.select_one('[data-chart-interval="3m"]') is not None
    assert soup.select_one('[data-chart-interval="5m"][disabled]') is not None
    assert soup.select_one("#chart-stage") is not None
    assert soup.select_one("#gate-status") is not None
    assert soup.select_one("#gate-display-status") is not None
    assert soup.select_one("#gate-detail-sections") is not None
    assert soup.select_one("#watchset-body") is not None
    assert soup.select_one("#order-candidates") is not None
    assert soup.select_one("#themelab-shadow-small-entry-ops-card") is not None
    assert soup.select_one("#themelab-shadow-small-entry-ops-status") is not None
    assert soup.select_one("#themelab-shadow-small-entry-ops-mode") is not None
    assert soup.select_one("#themelab-shadow-small-entry-ops-preflight-status") is not None
    assert soup.select_one("#themelab-shadow-small-entry-ops-blocking-reasons") is not None
    assert soup.select_one("#themelab-shadow-small-entry-ops-risk-lines") is not None
    assert soup.select_one("#themelab-shadow-small-entry-ops-arm") is not None
    assert soup.select_one("#themelab-shadow-small-entry-ops-confirm") is not None
    assert soup.select_one("#themelab-shadow-small-entry-ops-rollback") is not None
    assert soup.select_one("#themelab-shadow-small-entry-pilot-card") is not None
    assert soup.select_one("#themelab-shadow-small-entry-pilot-status") is not None
    assert soup.select_one("#themelab-shadow-small-entry-pilot-message") is not None
    assert soup.select_one("#themelab-shadow-small-entry-pilot-candidate-count") is not None
    assert soup.select_one("#themelab-shadow-small-entry-pilot-safety-lines") is not None
    assert soup.select_one("#themelab-shadow-small-entry-pilot-start") is not None
    assert soup.select_one("#themelab-shadow-small-entry-pilot-complete") is not None
    assert soup.select_one("#themelab-shadow-small-entry-pilot-generate-report") is not None
    assert soup.select_one('[data-filter-value="LIVE_GUARD_BLOCKED"]') is not None
    assert soup.select_one('[data-filter-value="MISSING_VWAP"]') is not None
    assert soup.select_one('[data-filter-value="ORDER_INTENT_CREATED"]') is not None
    assert soup.select_one('[data-filter-value="WAIT_FAILED_BREAKOUT"]') is not None
    assert soup.select_one('[data-filter-value="WAIT_DEEP_PULLBACK"]') is not None
    assert soup.select_one('[data-filter-value="WAIT_PRICE_LOCATION_WARMUP"]') is not None
    assert soup.select_one('[data-filter-value="WAIT_PRICE_LOCATION_PROVISIONAL"]') is not None
    assert soup.select_one('[data-filter-value="WAIT_PRICE_LOCATION_UNKNOWN"]') is not None
    assert "/static/themelab.css" in html
    assert "/static/themelab.js" in html
    assert "--app-bg: #0b0f14" in css
    assert "cockpit-grid" in css
    assert "/ws/dashboard" in js
    assert "/api/themelab/snapshot" in js
    assert "isFullThemeLabSnapshot" in js
    assert "snapshot.theme_lab && isFullThemeLabSnapshot(snapshot.theme_lab)" in js
    assert "/api/gateway/kiwoom/start" in js
    assert "startKiwoomGateway" in js
    assert "/api/themes/sync/naver" in js
    assert "syncNaverThemeUniverse" in js
    assert "renderNaverThemeSyncStatus" in js
    assert "requestLocalToken" in js
    assert "local-token-modal" in js
    assert "window.prompt" not in js
    assert "gateway_unhealthy_display" in js
    assert "shadow_small_entry_ab" in js
    assert "renderShadowAb" in js
    assert "renderShadowSmallEntryPromotionPanel" in js
    assert "renderShadowSmallEntryOpsPanel" in js
    assert "renderShadowSmallEntryPilotPanel" in js
    assert "shadowSmallEntryPilotAction" in js
    assert "shadow_small_entry_ops" in js
    assert "shadow_small_entry_pilot" in js
    assert "/api/shadow-small-entry-ops/arm" in js
    assert "/api/shadow-small-entry-ops/confirm" in js
    assert "/api/shadow-small-entry-ops/rollback" in js
    assert "/api/shadow-small-entry-pilot/start" in js
    assert "/api/shadow-small-entry-pilot/generate-report" in js
    assert "shadow_small_entry_promotion" in js
    assert "matchesFilters" in js
    assert "renderCockpit" in js
    assert "renderPromotionDecisionPanel" in js
    assert "fetchPromotionDecision" in js
    assert "fetchPromotionDrilldown" in js
    assert "renderPromotionDrilldownPanel" in js
    assert "promotionDrilldownGroup" in js
    assert "renderPromotionStageMatrix" in js
    assert "promotionStageMatrixRow" in js
    assert "promotionReasonLabel" in js
    assert "실시간 신뢰도 부족" in js or "label_ko" in js
    assert "승격확신" in js
    assert "failed_checks" in js
    assert "grouped_items" in js
    assert "initPromotionCockpit" in js
    assert "/api/runtime/promotion/decision" in js
    assert "/api/runtime/promotion/drilldown" in js
    assert "data-promotion-blocker" in js
    assert "promotionWindowSec" in js
    assert "promotion-decision-grid" in css
    assert "promotion-metric-grid" in css
    assert "promotion-stage-matrix" in css
    assert "promotion-stage-matrix-row" in css
    assert "promotion-reason-row" in css
    assert "promotion-drilldown-panel" in css
    assert "promotion-drilldown-row" in css
    assert "promotion-drilldown-group" in css
    assert "previousSnapshot" in js
    assert "operatorEvents" in js
    assert "acknowledgedEventIds" in js
    assert "persistedEventIds" in js
    assert "operatorSessionSummary" in js
    assert "journalFilter" in js
    assert "actionCatalog" in js
    assert "actionRecommendations" in js
    assert "selectedEventId" in js
    assert "selectedActionContext" in js
    assert "pendingAction" in js
    assert "actionSummary" in js
    assert "postmarketReviewSummary" in js
    assert "postmarketReviewItems" in js
    assert "postmarketReviewFilters" in js
    assert "postmarketReviewSelectedItem" in js
    assert "postmarketReviewLoading" in js
    assert "postmarketReviewLastGeneratedAt" in js
    assert "alertFilters" in js
    assert "maxOperatorEvents: 200" in js
    assert "deriveOperatorEvents" in js
    assert "appendOperatorEvents" in js
    assert "makeEventId" in js
    assert "persistOperatorEvents" in js
    assert "fetchOperatorEvents" in js
    assert "fetchOperatorSessionReview" in js
    assert "renderOperatorAlerts" in js
    assert "renderDecisionTimeline" in js
    assert "renderOperatorSessionReview" in js
    assert "renderOperatorEventJournal" in js
    assert "acknowledgeOperatorEvents" in js
    assert "hideOperatorEvents" in js
    assert "fetchActionCatalog" in js
    assert "fetchActionRecommendations" in js
    assert "executeOperatorAction" in js
    assert "confirmOperatorAction" in js
    assert "renderOperatorActionCenter" in js
    assert "renderActionRecommendations" in js
    assert "renderActionHistory" in js
    assert "renderActionSummary" in js
    assert "renderRunbook" in js
    assert "fetchPostmarketReviewSummary" in js
    assert "fetchPostmarketReviewItems" in js
    assert "rebuildPostmarketReview" in js
    assert "renderPostmarketReviewPanel" in js
    assert "renderPostmarketReviewSummary" in js
    assert "renderMissedOpportunityList" in js
    assert "renderGoodBlockList" in js
    assert "renderReviewNeededList" in js
    assert "renderProtectedFromChaseList" in js
    assert "renderDataInsufficientList" in js
    assert "renderBlockReasonSummary" in js
    assert "renderPostmarketReviewDetail" in js
    assert "formatReturnPct" in js
    assert "outcomeLabelKo" in js
    assert "confidenceBadge" in js
    assert "selectReviewItem" in js
    assert "runbookForEvent" in js
    assert "runbookForStatus" in js
    assert "appendActionResultToEvents" in js
    assert "/api/themelab/operator-events" in js
    assert "/api/themelab/operator-events/summary" in js
    assert "/api/themelab/operator-events/ack" in js
    assert "/api/themelab/operator-actions/catalog" in js
    assert "/api/themelab/operator-actions/recommendations" in js
    assert "/api/themelab/operator-actions/execute" in js
    assert "/api/themelab/postmarket-review" in js
    assert "/api/themelab/postmarket-review/summary" in js
    assert "/api/runtime/buy-zero/summary" in js
    assert "/api/runtime/buy-zero/ready-not-ordered" in js
    assert "/api/runtime/buy-zero/missed-opportunities" in js
    assert "/api/runtime/buy-zero/traces" in js
    assert "/api/conservative-reason-outcomes/summary" in js
    assert "function renderBuyZeroRcaPanel" in js
    assert "function renderConservativeReasonPanel" in js
    assert "function renderConservativeReasonStockRows" in js
    assert "function renderLiveSimAuditPanel" in js
    assert "live_sim_audit" in js
    assert "function renderBuyZeroDataQualityCounts" in js
    assert "function openBuyZeroTraceDetail" in js
    assert "BUY_ZERO_RCA_CRITICAL_REASONS" in js
    assert "CORE_BLOCKING" in js
    assert "WAIT_DATA_EARLY_SMALL_CANDIDATE" in js
    assert "REBUILD_POSTMARKET_REVIEW" in js
    assert "LIVE_BUY" in js
    assert "LIVE_SELL" in js
    assert "CANCEL_LIVE_ORDER" in js
    assert "OVERRIDE_LIVE_GUARD" in js
    assert "RUNTIME_CYCLE_ONCE" in js
    assert "CHECK_GATEWAY_STATUS" in js
    assert "START_KIWOOM_GATEWAY" in js
    assert "ADD_OPERATOR_NOTE" in js
    assert "OPEN_RUNBOOK" in js
    assert "MISSED_OPPORTUNITY" in js
    assert "GOOD_BLOCK" in js
    assert "REVIEW_NEEDED" in js
    assert "DATA_INSUFFICIENT" in js
    assert "PROTECTED_FROM_CHASE" in js
    assert "eventSeverity" in js
    assert "eventCategory" in js
    assert "selectSymbol" in js
    assert "watchsetBySymbol" in js
    assert "summaryDiffEvents" in js
    assert "watchsetDiffEvents" in js
    assert "gatewayDiffEvents" in js
    assert "dataQualityDiffEvents" in js
    assert "themeDiffEvents" in js
    assert 'selectSymbol(row.dataset.symbol, { source: "watchset" })' in js
    assert 'selectSymbol(eventItem.symbol, { source: "alert", acknowledgeEventId: eventItem.id })' in js
    for event_type in (
        "BUY_READY_NEW",
        "READY_BUT_LIVE_BLOCKED",
        "DATA_QUALITY_DEGRADED",
        "MARKET_WAIT_STARTED",
        "ORDER_INTENT_CREATED",
        "GATEWAY_DISCONNECTED",
        "SNAPSHOT_STALE",
    ):
        assert event_type in js
    assert 'chartInterval: "1m"' in js
    assert "renderFocusPanel" in js
    assert "selectedChartForSymbol" in js
    assert "chartFromWatchItem" in js
    assert "renderDecisionChecklist" in js
    assert "renderPriceMap" in js
    assert "renderFocusPanel(item, selectedChart)" in js
    assert "renderChart(selectedChart)" in js
    assert "minuteChartSvg" in js
    assert "RUNTIME_INACTIVE" in js
    assert "snapshot_age_label" in js
    assert ".chart-ref.vwap" in css
    assert "operator-alert-panel" in css
    assert "session-review-card" in css
    assert "operator-event-journal-list" in css
    assert "action-center-panel" in css
    assert "action-card" in css
    assert "action-history-row" in css
    assert "action-confirm-modal" in css
    assert "runbook-panel" in css
    assert "runbook-step" in css
    assert "action-status-success" in css
    assert "action-status-failed" in css
    assert "action-status-blocked" in css
    assert "action-status-running" in css
    assert "postmarket-review-panel" in css
    assert "postmarket-review-toolbar" in css
    assert "postmarket-summary-grid" in css
    assert "postmarket-summary-card" in css
    assert "buy-zero-rca-panel" in css
    assert "buy-zero-stage-funnel" in css
    assert "buy-zero-trace-timeline" in css
    assert "outcome-missed" in css
    assert "outcome-good-block" in css
    assert "outcome-review-needed" in css
    assert "outcome-data-insufficient" in css
    assert "outcome-protected" in css
    assert "confidence-high" in css
    assert "confidence-medium" in css
    assert "confidence-low" in css
    assert "review-table" in css
    assert "review-detail-panel" in css
    assert "return-positive" in css
    assert "return-negative" in css
    assert "return-neutral" in css
    assert ".critical" in css
    assert ".warning" in css
    assert ".opportunity" in css
    assert ".info" in css
    assert "candidate-focus-panel" in css
    assert "decision-checklist" in css
    assert "price-track" in css
    assert "button:disabled" in css


def test_theme_lab_snapshot_sorts_watchset_and_filters_entry_candidates(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_theme_lab_flow_result(
            "09:01:00",
            {
                "market_status": {"market_status": "SELECTIVE", "kospi_return_pct": 0.3, "kosdaq_return_pct": 0.9},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [
                    _watch("000003", "BLOCKED", role="LATE_LAGGARD"),
                    _watch("000002", "READY_SMALL", role="CO_LEADER", multiplier=0.5),
                    _watch("000001", "READY", role="LEADER"),
                    _watch("000004", "OBSERVE", condition_level=1),
                ],
                "gate_decisions": [],
                "data_quality": {"vi_status_supported": 0},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    assert payload["available"] is True
    assert [item["gate_status"] for item in payload["watchset"]] == ["READY", "READY_SMALL", "OBSERVE", "BLOCKED"]
    assert [item["symbol"] for item in payload["entry_candidates"]] == ["000001", "000002"]
    assert payload["summary"]["ready_count"] == 1
    assert payload["summary"]["ready_small_count"] == 1
    assert payload["summary"]["observe_count"] == 1
    assert payload["summary"]["blocked_count"] == 1
    assert payload["summary"]["live_guard_blocked_count"] == 2
    universe = {item["symbol"]: item for item in payload["chart_universe"]}
    assert {"KOSPI", "KOSDAQ", "000001", "000002"}.issubset(universe)
    assert "000004" not in universe
    assert payload["data_quality"]["vi_status_supported"] is False


def test_theme_lab_snapshot_includes_shadow_small_entry_ab_outcomes(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    at = datetime(2026, 6, 4, 9, 1, 0)
    try:
        wait = _watch("000901", "WAIT", role="LEADER")
        wait.update(
            {
                "current_price": 100,
                "price_location_readiness": "PROVISIONAL",
                "price_location_provisional": True,
                "price_location_readiness_reason_codes": ["PRICE_LOCATION_PROVISIONAL"],
                "candidate_market_status": "HEALTHY",
            }
        )
        db.save_theme_lab_flow_result(
            at.isoformat(),
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [wait],
                "gate_decisions": [],
                "data_quality": {},
            },
        )
        db.save_theme_lab_outcome_observations(
            [
                {
                    "observed_at": (at + timedelta(minutes=15)).isoformat(),
                    "trade_date": "2026-06-04",
                    "stock_code": "000901",
                    "price": 104,
                    "source": "theme_lab_outcome_tracking",
                }
            ]
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    assert payload["gate_reason_outcomes"]["trade_date"] == "2026-06-04"
    assert payload["gate_reason_outcomes"]["summary"]["event_count"] == 1
    assert payload["shadow_small_entry"]["summary"]["candidate_count"] == 1
    assert payload["shadow_small_entry"]["summary"]["win_rate_15m"] == 1.0
    assert payload["shadow_small_entry_ab"]["scenarios"]
    assert payload["shadow_small_entry_ab"]["best_scenarios"]
    assert payload["shadow_small_entry_ab"]["best_scenarios"][0]["candidate_count"] == 1


def test_theme_lab_snapshot_adds_candidate_focus_operating_fields(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        ready = _watch("000101", "READY", role="LEADER")
        ready.update(
            {
                "current_price": 1060,
                "vwap": 1055,
                "recent_support_price": 1030,
                "support_price": 1020,
                "breakout_level": 1050,
                "upper_limit_price": 1300,
                "live_order_enabled": True,
                "live_order_guard_passed": True,
                "submittable": True,
                "recheck_after_sec": 45,
            }
        )
        data_wait = _watch("000102", "WAIT")
        data_wait.update({"support_ready_reason": "WAIT_DATA_SUPPORT_NOT_READY", "diagnostic_only": True})
        market_wait = _watch("000103", "WAIT")
        market_wait.update({"candidate_market": "KOSDAQ", "candidate_market_confirmation_pending": True, "market_side_recheck_after_sec": 30})
        chase_blocked = _watch("000104", "BLOCKED")
        chase_blocked.update({"chase_risk": True, "risk_reason_codes": ["CHASE_RISK"]})
        db.save_theme_lab_flow_result(
            "09:01:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [data_wait, market_wait, chase_blocked, ready],
                "gate_decisions": [],
                "data_quality": {"status": "OK"},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    rows = {item["symbol"]: item for item in payload["watchset"]}
    assert rows["000101"]["operator_action"] == "BUY_READY"
    assert rows["000101"]["next_recheck_after_sec"] == 45
    assert rows["000101"]["decision_checklist"]["order_link"] == "READY"
    assert rows["000101"]["price_map"]["current_price"] == 1060
    assert rows["000102"]["operator_action"] == "DATA_WAIT"
    assert rows["000102"]["decision_checklist"]["data"] == "DEGRADED"
    assert rows["000103"]["operator_action"] == "MARKET_WAIT"
    assert rows["000103"]["decision_checklist"]["market"] == "WAIT"
    assert rows["000104"]["operator_action"] == "CHASE_BLOCKED"
    assert rows["000104"]["decision_checklist"]["chase_risk"] == "BLOCK"


def test_theme_lab_snapshot_merges_risk_off_details_from_gate_decisions(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        watch = _watch("000001", "WAIT", role="LEADER")
        db.save_theme_lab_flow_result(
            "09:01:00",
            {
                "market_status": {"market_status": "RISK_OFF"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [watch],
                "gate_decisions": [
                    {
                        "symbol": "000001",
                        "status": "WAIT",
                        "reason_codes": ["GLOBAL_MARKET_RISK_OFF", "WAIT_MARKET_RECOVERY"],
                        "candidate_market": "KOSDAQ",
                        "candidate_market_status": "RISK_OFF",
                        "candidate_market_raw_status": "RISK_OFF",
                        "candidate_market_confirmed_status": "RISK_OFF",
                        "candidate_market_confirmation_pending": False,
                        "candidate_market_recovery_pending": False,
                        "market_side_reason_codes": ["MARKET_RISK_OFF_CONFIRMED", "WAIT_CANDIDATE_MARKET_RISK_OFF"],
                        "risk_off_entry_details": {
                            "risk_off_entry_enabled": True,
                            "risk_off_entry_observe_only": True,
                            "risk_off_entry_allowed": False,
                            "risk_off_entry_rejected_reason": "STALE_QUOTE",
                            "risk_off_relative_strength_pct": 7.5,
                            "risk_off_candidate_breadth_pct": 0.67,
                            "risk_off_candidate_index_return_pct": -3.7,
                            "risk_off_max_position_size_multiplier": 0.25,
                            "risk_off_exit_hint": {"max_hold_minutes": 20},
                        },
                    }
                ],
                "data_quality": {"vi_status_supported": 0},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    row = payload["watchset"][0]
    assert row["display_status"] == "WAIT_CANDIDATE_MARKET_RISK_OFF"
    assert row["candidate_market_confirmation_pending"] is False
    assert row["risk_off_entry_enabled"] is True
    assert row["risk_off_entry_allowed"] is False
    assert row["risk_off_entry_rejected_reason"] == "STALE_QUOTE"
    assert payload["summary"]["risk_off_small_entry_candidate_count"] == 1
    assert payload["summary"]["risk_off_small_entry_rejected_count"] == 1
    assert payload["summary"]["risk_off_small_entry_reject_reason_counts"] == {"STALE_QUOTE": 1}
    assert payload["summary"]["market_confirmation_pending_count"] == 0
    assert payload["summary"]["market_risk_off_wait_count"] == 1


def test_theme_lab_snapshot_carries_minute_chart_context(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        ready = _watch("000777", "READY", role="LEADER")
        ready.update(
            {
                "current_price": 1060,
                "vwap": 1055.5,
                "recent_support_price": 1000,
                "upper_limit_price": 1300,
                "breakout_level": 1050,
                "recent_candles_1m": [
                    {
                        "start_at": "2026-06-04T09:01:00",
                        "open": 1000,
                        "high": 1060,
                        "low": 995,
                        "close": 1060,
                        "volume": 120,
                        "completed": True,
                    }
                ],
                "completed_minute_bar_count": 1,
                "minute_bar_present": True,
                "recent_support_source": "completed_1m_low",
                "recent_support_ready": True,
                "prev_close_inferred_from_change_rate": True,
            }
        )
        db.save_theme_lab_flow_result(
            "2026-06-04T09:12:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [ready],
                "gate_decisions": [],
                "data_quality": {"status": "OK", "candle_missing_count": 0},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    chart = payload["selected_chart"]
    assert chart["symbol"] == "000777"
    assert chart["chart_data_status"] == "READY"
    assert chart["has_candle_data"] is True
    assert chart["candles"][0]["close"] == 1060
    assert chart["vwap"] == 1055.5
    assert chart["recent_support_source"] == "completed_1m_low"
    assert chart["prev_close_inferred_from_change_rate"] is True


def test_theme_lab_snapshot_prefers_watchset_chart_data_over_empty_theme_leader(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        watch = _watch("000778", "WAIT", role="FOLLOWER")
        watch.update(
            {
                "current_price": 1000,
                "vwap": 998,
                "recent_support_price": 995,
                "recent_candles_1m": [
                    {
                        "start_at": "2026-06-04T09:02:00",
                        "open": 995,
                        "high": 1001,
                        "low": 995,
                        "close": 1000,
                        "volume": 50,
                        "completed": False,
                    }
                ],
                "minute_bar_present": True,
                "recent_support_source": "active_1m_low_provisional",
            }
        )
        theme = _theme()
        theme.update({"top_leader_symbol": "999999", "top_leader_name": "empty leader"})
        db.save_theme_lab_flow_result(
            "2026-06-04T09:13:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [theme],
                "watchset_snapshots": [watch],
                "gate_decisions": [],
                "data_quality": {"status": "OK", "candle_missing_count": 0},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    assert payload["selected_chart"]["symbol"] == "000778"
    assert payload["selected_chart"]["chart_data_status"] == "READY"


def test_theme_lab_snapshot_summary_counts_operating_cockpit_fields(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        ready = _watch("000101", "READY", role="LEADER")
        ready.update(
            {
                "candidate_market": "KOSDAQ",
                "live_order_enabled": True,
                "live_order_guard_passed": True,
                "runtime_order_intent_created": True,
                "virtual_order_created": True,
                "submittable": True,
            }
        )
        ready_small = _watch("000102", "READY_SMALL", role="CO_LEADER", multiplier=0.5)
        ready_small.update({"candidate_market": "KOSPI", "live_order_enabled": True, "live_order_guard_passed": False})
        late_chase = _watch("000103", "WAIT", role="FOLLOWER")
        late_chase.update({"late_chase_level": "soft_block", "late_chase_recheck_after_sec": 45})
        chase_block = _watch("000104", "BLOCKED", role="LATE_LAGGARD")
        chase_block.update({"chase_risk": True, "risk_reason_codes": ["CHASE_RISK"]})
        market_pending = _watch("000105", "WAIT")
        market_pending.update({"candidate_market": "KOSDAQ", "candidate_market_confirmation_pending": True})
        data_pending = _watch("000106", "WAIT")
        data_pending.update({"support_ready_reason": "WAIT_DATA_SUPPORT_NOT_READY", "diagnostic_only": True})
        db.save_theme_lab_flow_result(
            "2026-06-03T09:07:00",
            {
                "market_status": {
                    "market_status": "SELECTIVE",
                    "kospi_return_pct": 0.2,
                    "kosdaq_return_pct": 0.7,
                    "side_statuses": {
                        "KOSPI": {"status": "CHOPPY", "index_return_pct": 0.2, "breadth_pct": 0.52, "breadth_source": "REALTIME", "breadth_trust_level": "HIGH"},
                        "KOSDAQ": {"status": "SELECTIVE", "index_return_pct": 0.7, "breadth_pct": 0.61, "breadth_source": "REALTIME", "breadth_trust_level": "MEDIUM"},
                    },
                },
                "theme_rankings": [_theme()],
                "watchset_snapshots": [data_pending, chase_block, market_pending, ready_small, late_chase, ready],
                "gate_decisions": [],
                "data_quality": {"status": "OK", "candle_missing_count": 0},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    summary = payload["summary"]
    assert summary["ready_count"] == 1
    assert summary["ready_small_count"] == 1
    assert summary["wait_count"] == 3
    assert summary["blocked_count"] == 1
    assert summary["late_chase_wait_count"] == 1
    assert summary["chase_risk_blocked_count"] == 1
    assert summary["market_pending_count"] == 1
    assert summary["data_not_ready_count"] == 1
    assert summary["diagnostic_only_count"] == 1
    assert summary["runtime_order_intent_created_count"] == 1
    assert summary["virtual_order_created_count"] == 1
    assert summary["live_guard_passed_count"] == 1
    assert summary["live_guard_blocked_count"] == 1
    assert summary["leader_count"] == 1
    assert summary["co_leader_count"] == 1
    assert summary["late_laggard_count"] == 1
    assert summary["top_theme_name"] == "전력기기"
    assert summary["operation_status"] == "READY_TO_TRADE"
    assert summary["operation_message_ko"] == "READY 후보가 있고 데이터 품질이 정상입니다."
    assert payload["market"]["sides"][0]["side"] == "KOSPI"
    assert payload["market"]["sides"][1]["breadth_trust_level"] == "MEDIUM"


def test_theme_lab_snapshot_operation_status_for_ready_live_blocked_and_data_quality(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        blocked_ready = _watch("000201", "READY")
        blocked_ready.update({"live_order_enabled": True, "live_order_guard_passed": False})
        db.save_theme_lab_flow_result(
            "2026-06-03T09:08:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [blocked_ready],
                "gate_decisions": [],
                "data_quality": {"status": "OK", "candle_missing_count": 0},
            },
        )
        payload = build_theme_lab_dashboard_snapshot(db)
        assert payload["summary"]["operation_status"] == "READY_BUT_LIVE_BLOCKED"
        assert payload["summary"]["operation_message_ko"] == "READY 후보는 있으나 LIVE Guard 통과 후보가 없습니다."

        data_wait = _watch("000202", "WAIT")
        data_wait.update({"support_ready_reason": "WAIT_DATA_SUPPORT_NOT_READY", "diagnostic_only": True})
        db.save_theme_lab_flow_result(
            "2026-06-03T09:09:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [data_wait],
                "gate_decisions": [],
                "data_quality": {"status": "DEGRADED", "candle_missing_count": 1},
            },
        )
        payload = build_theme_lab_dashboard_snapshot(db)
        assert payload["summary"]["operation_status"] == "WAIT_DATA_QUALITY"
        assert payload["summary"]["operation_message_ko"] == "VWAP/지지선/틱 데이터 부족으로 진단 전용 후보가 많습니다."
    finally:
        db.close()


def test_theme_lab_snapshot_marks_runtime_inactive_and_stale_age(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        ready = _watch("000211", "READY")
        ready.update({"live_order_enabled": True, "live_order_guard_passed": True})
        db.save_theme_lab_flow_result(
            "2026-06-04T09:00:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [ready],
                "gate_decisions": [],
                "data_quality": {"status": "OK", "candle_missing_count": 0},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(
            db,
            runtime_status={"enabled": False, "auto_start": False, "running": False, "mode": "OBSERVE", "cycle_count": 0},
            now=datetime.fromisoformat("2026-06-04T09:05:00"),
        )
        assert payload["runtime"]["status"] == "RUNTIME_INACTIVE"
        assert payload["summary"]["operation_status"] == "RUNTIME_INACTIVE"
        assert payload["summary"]["runtime_running"] is False
        assert payload["summary"]["snapshot_age_sec"] == 300
        assert payload["summary"]["snapshot_age_label"] == "5m 0s"
        assert payload["summary"]["snapshot_stale"] is True
        assert payload["data_quality"]["snapshot_age_sec"] == 300

        stale_payload = build_theme_lab_dashboard_snapshot(
            db,
            runtime_status={"enabled": True, "auto_start": True, "running": True, "mode": "OBSERVE", "cycle_count": 3},
            now=datetime.fromisoformat("2026-06-04T09:05:00"),
        )
        assert stale_payload["summary"]["operation_status"] == "SNAPSHOT_STALE"
        assert stale_payload["summary"]["runtime_running"] is True
    finally:
        db.close()


def test_theme_lab_data_quality_uses_actual_watchset_candles(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        watch = _watch("000203", "WAIT")
        watch.update(
            {
                "recent_candles_1m": [
                    {
                        "start_at": "2026-06-04T09:01:00",
                        "open": 1000,
                        "high": 1010,
                        "low": 995,
                        "close": 1005,
                    }
                ],
                "completed_minute_bar_count": 1,
                "minute_bar_present": True,
            }
        )
        db.save_theme_lab_flow_result(
            "2026-06-03T09:09:30",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [watch],
                "gate_decisions": [],
                "data_quality": {
                    "missing_current_price_count": 43,
                    "missing_prev_close_count": 43,
                },
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    quality = payload["data_quality"]
    assert quality["status"] == "WARNING"
    assert quality["candle_missing_count"] == 0
    assert quality["current_price_missing_count"] == 43
    assert quality["prev_close_missing_count"] == 43
    assert "테마 universe 현재가 43종목 누락" in quality["reasons"]
    assert "WatchSet 분봉 1종목 누락" not in quality["reasons"]


def test_theme_lab_snapshot_operation_status_for_empty_watchset_with_theme_data_wait(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        theme = _theme()
        theme.update({"data_quality_flags": ["MISSING_CURRENT_PRICE", "MISSING_PREV_CLOSE"]})
        db.save_theme_lab_flow_result(
            "2026-06-03T09:10:00",
            {
                "market_status": {"market_status": "CHOPPY"},
                "theme_rankings": [theme],
                "watchset_snapshots": [],
                "gate_decisions": [],
                "data_quality": {"status": "OK", "candle_missing_count": 0},
            },
        )
        payload = build_theme_lab_dashboard_snapshot(db)
        assert payload["summary"]["operation_status"] == "WAIT_DATA_QUALITY"
        assert payload["summary"]["operation_message_ko"] == "테마 결과는 있으나 지수/현재가 데이터 워밍업 중입니다."

        clean_theme = _theme()
        db.save_theme_lab_flow_result(
            "2026-06-03T09:11:00",
            {
                "market_status": {"market_status": "CHOPPY"},
                "theme_rankings": [clean_theme],
                "watchset_snapshots": [],
                "gate_decisions": [],
                "data_quality": {"status": "OK", "candle_missing_count": 0},
            },
        )
        payload = build_theme_lab_dashboard_snapshot(db)
        assert payload["summary"]["operation_status"] == "OBSERVE_ONLY"
        assert payload["summary"]["operation_message_ko"] == "ThemeLabFlow 결과는 있으나 WatchSet 조건을 통과한 종목이 없습니다."
    finally:
        db.close()


def test_theme_lab_snapshot_demotes_themes_without_live_price_signal(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        missing_price = _theme()
        missing_price.update(
            {
                "theme_id": "missing",
                "theme_name": "현재가 없는 테마",
                "alive_count": 0,
                "alive_ratio": 0,
                "strong_count": 0,
                "strong_ratio": 0,
                "leader_count": 0,
                "leader_ratio": 0,
                "condition_score": 99,
                "theme_turnover_krw": 0,
                "data_quality_flags": ["MISSING_CURRENT_PRICE", "MISSING_PREV_CLOSE"],
            }
        )
        live_price = _theme()
        live_price.update(
            {
                "theme_id": "live",
                "theme_name": "현재가 있는 테마",
                "condition_score": 10,
                "data_quality_flags": ["MISSING_CURRENT_PRICE", "MISSING_PREV_CLOSE"],
            }
        )
        db.save_theme_lab_flow_result(
            "09:03:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [missing_price, live_price],
                "watchset_snapshots": [],
                "gate_decisions": [],
                "data_quality": {},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    ranked = payload["ranked_themes"]
    assert ranked[0]["theme_id"] == "live"
    assert ranked[0]["has_live_price_signal"] is True
    assert ranked[0]["theme_quality_status"] == "WARNING"
    assert ranked[0]["theme_backfill_priority"] == "MEDIUM"
    assert "테마 폭 신뢰 보통" in ranked[0]["quality_label"]
    assert "현재가 3종목 대기" in ranked[0]["quality_label"]
    assert "전일종가 3종목 보강 필요" in ranked[0]["quality_label"]
    assert ranked[1]["theme_id"] == "missing"
    assert ranked[1]["theme_quality_status"] == "BROKEN"
    assert ranked[1]["theme_backfill_priority"] == "HIGH"
    assert "테마 폭 산출 불가" in ranked[1]["quality_label"]
    assert "실시간 현재가 보강 필요" in ranked[1]["quality_label"]


def test_theme_lab_snapshot_adds_theme_backfill_command_status(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    state = GatewayStateStore()
    try:
        db.save_theme_lab_flow_result(
            "09:04:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [],
                "gate_decisions": [],
                "data_quality": {},
                "theme_backfill_runtime": {"enabled": True, "tr_backfill_caused_ready_count": 0},
            },
        )
        state.enqueue_command(
            GatewayCommand(
                type="tr_request",
                command_id="cmd-backfill-power",
                payload={
                    "purpose": THEME_BACKFILL_PURPOSE,
                    "primary_theme_id": "power",
                    "related_theme_ids": ["power"],
                    "code": "000001",
                    "tr_code": "opt10001",
                },
            ),
            priority=CommandPriority.LOW,
            ttl_sec=90,
            max_attempts=1,
        )

        payload = build_theme_lab_dashboard_snapshot(db, gateway_state=state)
    finally:
        db.close()

    row = payload["ranked_themes"][0]
    assert row["theme_backfill_status"] == "대기"
    assert row["theme_backfill_raw_status"] == "QUEUED"
    assert payload["theme_backfill_runtime"]["queued_count"] == 1
    assert payload["theme_backfill_runtime"]["history_window"] == "recent_500_commands"
    assert payload["theme_backfill_runtime"]["parser_miss_ratio"] is None
    assert payload["theme_backfill_runtime"]["tr_backfill_caused_ready_count"] == 0


def test_theme_lab_snapshot_counts_backfill_parser_miss_ratio(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    state = GatewayStateStore()
    try:
        db.save_theme_lab_flow_result(
            "09:05:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [],
                "gate_decisions": [],
                "data_quality": {},
                "theme_backfill_runtime": {"enabled": True},
            },
        )
        for index, (command_id, parser_status) in enumerate((("cmd-ok", "OK"), ("cmd-partial", "PARTIAL")), start=1):
            state.enqueue_command(
                GatewayCommand(
                    type="tr_request",
                    command_id=command_id,
                    idempotency_key=f"theme-backfill-test-{index}",
                    payload={"purpose": THEME_BACKFILL_PURPOSE, "primary_theme_id": "power", "code": f"00000{index}"},
                ),
                priority=CommandPriority.LOW,
            )
            state.ack_command(
                command_id,
                status="ACKED",
                result_payload={"purpose": THEME_BACKFILL_PURPOSE, "parser_status": parser_status},
            )

        payload = build_theme_lab_dashboard_snapshot(db, gateway_state=state)
    finally:
        db.close()

    runtime = payload["theme_backfill_runtime"]
    assert runtime["success_count"] == 2
    assert runtime["parser_miss_count"] == 1
    assert runtime["parser_miss_ratio"] == 0.5


def test_theme_lab_snapshot_explains_gateway_unhealthy_kiwoom_login(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    state = GatewayStateStore()
    try:
        db.save_theme_lab_flow_result(
            "09:06:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [],
                "gate_decisions": [],
                "data_quality": {},
                "theme_backfill_runtime": {
                    "enabled": True,
                    "trading_mode": "OBSERVE",
                    "paused_reason": "GATEWAY_UNHEALTHY",
                },
            },
        )
        state.record_event(
            GatewayEvent(
                type="heartbeat",
                event_id="evt-heartbeat-not-logged-in",
                timestamp=utc_timestamp(),
                payload={"kiwoom_logged_in": False, "orderable": False, "mode": "OBSERVE"},
            )
        )

        payload = build_theme_lab_dashboard_snapshot(db, gateway_state=state)
    finally:
        db.close()

    runtime = payload["theme_backfill_runtime"]
    assert runtime["paused_reason"] == "GATEWAY_UNHEALTHY"
    assert runtime["gateway_unhealthy_detail"] == "KIWOOM_NOT_LOGGED_IN"
    assert runtime["gateway_unhealthy_display"] == "\ud0a4\uc6c0 \ubbf8\ub85c\uadf8\uc778"
    assert payload["gateway"]["connected"] is True
    assert payload["gateway"]["heartbeat_ok"] is True
    assert payload["gateway"]["kiwoom_logged_in"] is False


def test_theme_lab_snapshot_overlays_condition_event_breadth(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        repo = ThemeEngineRepository(db)
        repo.upsert_canonical_theme(
            CanonicalTheme(
                theme_id="power",
                canonical_name="전력기기",
                display_name="전력기기",
                status=ThemeStatus.ACTIVE,
                trade_eligible=True,
            )
        )
        for code in ("000001", "000002", "000003", "000004", "000005"):
            repo.upsert_current_membership(ThemeMembership(theme_id="power", stock_code=code, active=True, trade_eligible=True))
        db.save_candidate(
            Candidate(
                trade_date="2026-06-02",
                code="000001",
                state=CandidateState.WATCHING,
                condition_names=["테마랩_강세_3"],
                metadata={"condition_purposes": {"테마랩_강세_3": "theme_lab_strong"}},
            )
        )
        db.save_candidate(
            Candidate(
                trade_date="2026-06-02",
                code="000002",
                state=CandidateState.WATCHING,
                condition_names=["테마랩_주도_5"],
                metadata={"condition_purposes": {"테마랩_주도_5": "theme_lab_leader"}},
            )
        )
        db.save_theme_lab_flow_result(
            "2026-06-02T09:04:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [
                    {
                        **_theme(),
                        "alive_count": 0,
                        "alive_ratio": 0,
                        "strong_count": 0,
                        "strong_ratio": 0,
                        "leader_count": 0,
                        "leader_ratio": 0,
                    }
                ],
                "watchset_snapshots": [],
                "gate_decisions": [],
                "data_quality": {},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    row = payload["ranked_themes"][0]
    assert row["price_strong_count"] == 0
    assert row["condition_strong_count"] == 2


def test_condition_status_uses_send_condition_ack_not_resolved_index(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        repo = ConditionProfileRepository(db)
        repo.upsert_profile(
            ConditionProfile(
                condition_name="테마랩_주도_5",
                strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE,
                enabled=True,
                priority=200,
                purpose="theme_lab_leader",
                last_resolved_index=85,
            )
        )
        state = GatewayStateStore()
        state.enqueue_command(
            GatewayCommand(
                type="send_condition",
                command_id="cmd-cond-failed",
                payload={
                    "condition_name": "테마랩_주도_5",
                    "condition_index": 85,
                    "screen_no": "7602",
                },
            )
        )
        state.ack_command("cmd-cond-failed", status="FAILED", error="CONDITION_SEND_FAILED")
        db.save_theme_lab_flow_result(
            "2026-06-02T09:04:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [],
                "watchset_snapshots": [],
                "gate_decisions": [],
                "data_quality": {},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db, gateway_state=state)
    finally:
        db.close()

    leader = next(item for item in payload["condition_statuses"] if item["purpose"] == "theme_lab_leader")
    assert leader["resolved_index"] == 85
    assert leader["registered"] is False
    assert leader["registered_label"] == "확인 필요"
    assert leader["command_status"] == "FAILED"
    assert leader["command_status_label"] == "등록 확인 실패"
    assert leader["screen_no"] == "7602"
    assert leader["warning"] == "CONDITION_SEND_FAILED"
    assert leader["warning_label"] == "등록 확인 실패"
    assert "ACK" in leader["warning_detail"]
    assert leader["action_hint"]


def test_condition_status_prefers_current_session_ack_over_later_failed_duplicate(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        repo = ConditionProfileRepository(db)
        repo.upsert_profile(
            ConditionProfile(
                condition_name="theme-lab-leader",
                strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE,
                enabled=True,
                priority=200,
                purpose="theme_lab_leader",
                last_resolved_index=85,
            )
        )
        state = GatewayStateStore()
        state.status.connected = True
        state.status.last_heartbeat_at = utc_timestamp()
        state.status.last_heartbeat_payload = {"ws_session_id": "current-session"}
        state.enqueue_command(
            GatewayCommand(
                type="send_condition",
                command_id="cmd-cond-acked",
                payload={
                    "condition_name": "theme-lab-leader",
                    "condition_index": 85,
                    "screen_no": "7602",
                },
            )
        )
        state.ack_command(
            "cmd-cond-acked",
            status="ACKED",
            result_payload={"message": "condition sent", "transport_trace": {"ws_session_id": "current-session"}},
        )
        state.enqueue_command(
            GatewayCommand(
                type="send_condition",
                command_id="cmd-cond-failed-later",
                idempotency_key="runtime:send_condition_recover:theme-lab-leader:85:7602:20260608094500",
                payload={
                    "condition_name": "theme-lab-leader",
                    "condition_index": 85,
                    "screen_no": "7602",
                },
            )
        )
        state.ack_command("cmd-cond-failed-later", status="FAILED", error="condition sent")
        db.save_theme_lab_flow_result(
            "2026-06-02T09:04:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [],
                "watchset_snapshots": [],
                "gate_decisions": [],
                "data_quality": {},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db, gateway_state=state)
    finally:
        db.close()

    leader = next(item for item in payload["condition_statuses"] if item["purpose"] == "theme_lab_leader")
    assert leader["registered"] is True
    assert leader["registered_label"] == "정상"
    assert leader["command_status"] == "ACKED"
    assert leader["command_status_label"] == "등록 확인 완료"
    assert leader["screen_no"] == "7602"
    assert leader["warning"] == ""
    assert leader["warning_label"] == "정상"
    assert leader["action_hint"] == ""


def test_theme_lab_api_route_and_dashboard_snapshot_include_theme_lab(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api

    api = importlib.reload(api)
    db = TradingDatabase(str(db_path))
    try:
        db.save_theme_lab_flow_result(
            "09:02:00",
            {
                "market_status": {"market_status": "EXPANSION"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [_watch("000001", "READY")],
                "gate_decisions": [],
                "data_quality": {},
            },
        )
    finally:
        db.close()

    with TestClient(api.app) as client:
        page = client.get("/themelab")
        direct = client.get("/api/themelab/snapshot").json()
        snapshot = client.get("/api/snapshot").json()

    assert page.status_code == 200
    assert direct["summary"]["ready_count"] == 1
    assert snapshot["theme_lab"]["summary"]["ready_count"] == 1


def test_theme_lab_snapshot_exposes_naver_sync_status(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        ThemeEngineRepository(db).save_source_sync_run(
            ThemeSourceSyncResult(
                source="naver_theme_universe",
                status="success",
                theme_count=3,
                member_count=42,
                started_at="2026-06-10T09:00:00",
                finished_at="2026-06-10T09:01:00",
            )
        )
        db.save_theme_lab_flow_result(
            "09:02:00",
            {
                "market_status": {"market_status": "EXPANSION"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [_watch("000001", "READY")],
                "gate_decisions": [],
                "data_quality": {},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    assert payload["theme_source_sync"]["source"] == "naver_theme_universe"
    assert payload["theme_source_sync"]["status"] == "success"
    assert payload["theme_source_sync"]["theme_count"] == 3
    assert payload["theme_source_sync"]["member_count"] == 42


def test_theme_lab_snapshot_exposes_defensive_gate_observability_columns(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        late_chase = _watch("000011", "WAIT")
        late_chase.update(
            {
                "late_chase_level": "soft_block",
                "late_chase_block_type": "temporary",
                "late_chase_recoverable": True,
                "late_chase_recheck_after_sec": 60,
                "risk_reason_codes": ["LATE_CHASE_TEMP_WAIT"],
            }
        )
        market_pending = _watch("000012", "WAIT")
        market_pending.update(
            {
                "candidate_market": "KOSPI",
                "candidate_market_confirmation_pending": True,
                "market_side_weak_consecutive_cycles": 1,
                "market_side_recheck_after_sec": 60,
            }
        )
        db.save_theme_lab_flow_result(
            "2026-06-03T09:05:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [late_chase, market_pending],
                "gate_decisions": [],
                "data_quality": {},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    rows = {row["symbol"]: row for row in payload["watchset"]}
    assert rows["000011"]["gate_status"] == "WAIT"
    assert rows["000011"]["display_status"] == "LATE_CHASE_TEMP_WAIT"
    assert rows["000011"]["strategy_eligible"] is False
    assert rows["000011"]["late_chase_temp_wait"] is True
    assert rows["000012"]["display_status"] == "WAIT_MARKET_CONFIRMATION_PENDING"
    assert rows["000012"]["candidate_market"] == "KOSPI"
    assert rows["000012"]["market_confirmation_pending"] is True
    for key in (
        "chase_risk",
        "market_wait_reason",
        "market_confirmation_state_restored",
        "market_session_type",
        "runtime_order_intent_created",
        "virtual_order_created",
        "live_order_guard_passed",
    ):
        assert key in rows["000011"]


def test_theme_lab_snapshot_explains_unknown_price_location_wait(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        wait = _watch("000013", "WAIT")
        wait.update(
            {
                "price_location_status": "UNKNOWN",
                "price_location_score": 40,
                "price_location_reason_codes": ["PRICE_LOCATION_UNKNOWN"],
                "pullback_from_high_pct": 4.2961,
                "vwap_gap_pct": -0.1354,
                "breakout_level_gap_pct": 0.0,
                "support_gap_pct": 0.3465,
                "recheck_after_sec": 30,
            }
        )
        db.save_theme_lab_flow_result(
            "2026-06-03T09:05:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [wait],
                "gate_decisions": [],
                "data_quality": {},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    row = payload["watchset"][0]
    assert row["display_status"] == "WAIT_PRICE_LOCATION_UNKNOWN"
    assert row["summary_reason"].startswith("가격 위치 미확정")
    assert "고점대비 4.30%" in row["summary_reason"]
    assert "VWAP -0.14%" in row["summary_reason"]
    assert "돌파선 +0.00%" in row["summary_reason"]
    assert "지지선 +0.35%" in row["summary_reason"]
    assert "30초 후 재확인" in row["summary_reason"]


def test_theme_lab_snapshot_splits_price_location_warmup_status(tmp_path):
    db = TradingDatabase(str(tmp_path / "dashboard.sqlite3"))
    try:
        wait = _watch("000001", "WAIT", role="LEADER")
        wait.update(
            {
                "price_location_status": "UNKNOWN",
                "price_location_reason_codes": ["PRICE_LOCATION_UNKNOWN", "PRICE_LOCATION_WARMUP"],
                "price_location_readiness": "WARMUP",
                "price_location_readiness_reason_codes": ["PRICE_LOCATION_WARMUP", "PRICE_LOCATION_NO_MINUTE_BAR"],
            }
        )
        db.save_theme_lab_flow_result(
            "2026-06-03T09:05:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [wait],
                "gate_decisions": [],
                "data_quality": {},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    row = payload["watchset"][0]
    assert row["display_status"] == "WAIT_PRICE_LOCATION_WARMUP"
    assert row["price_location_readiness"] == "WARMUP"
    assert "PRICE_LOCATION_NO_MINUTE_BAR" in row["price_location_readiness_reason_codes"]


def test_theme_lab_snapshot_explains_deep_pullback_wait(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        wait = _watch("000014", "WAIT")
        wait.update(
            {
                "price_location_status": "DEEP_PULLBACK",
                "price_location_score": 35,
                "price_location_reason_codes": ["DEEP_PULLBACK"],
                "pullback_from_high_pct": 8.7132,
                "vwap_gap_pct": -3.231,
                "support_gap_pct": 0.213,
                "momentum_3m": -0.42,
                "recheck_after_sec": 60,
            }
        )
        db.save_theme_lab_flow_result(
            "2026-06-03T09:05:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [wait],
                "gate_decisions": [],
                "data_quality": {},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    row = payload["watchset"][0]
    assert row["display_status"] == "WAIT_DEEP_PULLBACK"
    assert row["summary_reason"].startswith("과도한 눌림 대기")
    assert "고점대비 8.71% 눌림" in row["summary_reason"]
    assert "VWAP -3.23%" in row["summary_reason"]
    assert "3분 모멘텀 -0.42%" in row["summary_reason"]
    assert "지지선 +0.21%" in row["summary_reason"]
    assert "60초 후 재확인" in row["summary_reason"]


def test_theme_lab_snapshot_explains_failed_breakout_wait(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        wait = _watch("000015", "WAIT")
        wait.update(
            {
                "price_location_status": "FAILED_BREAKOUT",
                "price_location_score": 25,
                "price_location_reason_codes": ["FAILED_BREAKOUT"],
                "breakout_level_gap_pct": -0.6485,
                "vwap_gap_pct": -2.4986,
                "recent_candles_1m": [
                    {
                        "start_at": "2026-06-04T09:01:00",
                        "open": 1000,
                        "high": 1002,
                        "low": 996,
                        "close": 996.9,
                    }
                ],
                "upper_wick_risk": True,
                "failed_breakout": True,
                "recheck_after_sec": 60,
            }
        )
        db.save_theme_lab_flow_result(
            "2026-06-03T09:05:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [wait],
                "gate_decisions": [],
                "data_quality": {},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    row = payload["watchset"][0]
    assert row["display_status"] == "WAIT_FAILED_BREAKOUT"
    assert row["summary_reason"].startswith("돌파 실패 대기")
    assert "돌파선 -0.65% 이탈" in row["summary_reason"]
    assert "윗꼬리 리스크 있음" in row["summary_reason"]
    assert "1분 모멘텀 -0.31%" in row["summary_reason"]
    assert "VWAP -2.50%" in row["summary_reason"]
    assert "60초 후 재확인" in row["summary_reason"]


def test_theme_lab_snapshot_explains_missing_momentum_reason(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        wait = _watch("000016", "WAIT")
        wait.update(
            {
                "price_location_status": "FAILED_BREAKOUT",
                "price_location_score": 25,
                "price_location_reason_codes": ["FAILED_BREAKOUT"],
                "breakout_level_gap_pct": -0.5,
                "upper_wick_risk": True,
                "failed_breakout": True,
                "recheck_after_sec": 60,
            }
        )
        db.save_theme_lab_flow_result(
            "2026-06-03T09:05:00",
            {
                "market_status": {"market_status": "SELECTIVE"},
                "theme_rankings": [_theme()],
                "watchset_snapshots": [wait],
                "gate_decisions": [],
                "data_quality": {},
            },
        )

        payload = build_theme_lab_dashboard_snapshot(db)
    finally:
        db.close()

    row = payload["watchset"][0]
    assert row["display_status"] == "WAIT_FAILED_BREAKOUT"
    assert row["momentum_1m"] is None
    assert row["momentum_1m_missing_reason"] == "완성 1분봉 없음"
    assert "1분 모멘텀 미확인(완성 1분봉 없음)" in row["summary_reason"]


def _theme():
    return {
        "theme_id": "power",
        "theme_name": "전력기기",
        "theme_status": "LEADING_THEME",
        "eligible_total_members": 5,
        "alive_count": 4,
        "alive_ratio": 0.8,
        "strong_count": 3,
        "strong_ratio": 0.6,
        "leader_count": 1,
        "leader_ratio": 0.2,
        "condition_score": 80,
        "theme_turnover_krw": 12000000000,
        "top_leader_symbol": "000001",
        "top_leader_name": "제룡전기",
        "member_hits": [
            {
                "symbol": "000001",
                "name": "제룡전기",
                "return_pct": 4.2,
                "turnover_krw": 3000000000,
                "alive_hit": True,
                "strong_hit": True,
                "leader_hit": False,
                "excluded": False,
                "data_quality_flags": [],
            },
            {
                "symbol": "000002",
                "name": "후보전기",
                "return_pct": 5.5,
                "turnover_krw": 7000000000,
                "alive_hit": True,
                "strong_hit": True,
                "leader_hit": True,
                "excluded": False,
                "data_quality_flags": [],
            },
        ],
    }


def _watch(symbol: str, gate: str, *, role: str = "FOLLOWER", condition_level: int = 3, multiplier: float = 1.0):
    return {
        "calculated_at": "09:01:00",
        "symbol": symbol,
        "name": f"종목{symbol}",
        "primary_theme": "전력기기",
        "return_pct": 6.0,
        "turnover_krw": 5000000000,
        "condition_level": condition_level,
        "stock_role": role,
        "gate_status": gate,
        "final_gate_status": gate,
        "risk_level": "PASS" if gate != "BLOCKED" else "HARD_BLOCK",
        "price_location_status": "GOOD_PULLBACK",
        "price_location_score": 70,
        "position_size_multiplier": multiplier,
        "risk_reason_codes": ["LATE_LAGGARD"] if gate == "BLOCKED" else [],
    }
