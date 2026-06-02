from __future__ import annotations

import importlib
from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading.strategy.models import Candidate, CandidateState
from trading.theme_engine.models import CanonicalTheme, ThemeMembership, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository
from trading_app.themelab_dashboard import build_theme_lab_dashboard_snapshot


ROOT = Path(__file__).resolve().parents[1]


def test_themelab_page_is_standalone_dark_terminal():
    html = (ROOT / "web" / "templates" / "themelab.html").read_text(encoding="utf-8")
    css = (ROOT / "web" / "static" / "themelab.css").read_text(encoding="utf-8")
    js = (ROOT / "web" / "static" / "themelab.js").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    assert soup.select_one(".terminal-shell") is not None
    assert soup.select_one("#theme-rank-list") is not None
    assert soup.select_one("#chart-stage") is not None
    assert soup.select_one("#gate-status") is not None
    assert soup.select_one("#watchset-body") is not None
    assert soup.select_one("#order-candidates") is not None
    assert "/static/themelab.css" in html
    assert "/static/themelab.js" in html
    assert "--app-bg: #0b0f14" in css
    assert "/ws/dashboard" in js
    assert "/api/themelab/snapshot" in js


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
    universe = {item["symbol"]: item for item in payload["chart_universe"]}
    assert {"KOSPI", "KOSDAQ", "000001", "000002"}.issubset(universe)
    assert "000004" not in universe
    assert payload["data_quality"]["vi_status_supported"] is False


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
    assert ranked[0]["quality_label"] == "일부 구성종목 데이터 대기, 전일종가 일부 누락"
    assert ranked[1]["theme_id"] == "missing"
    assert ranked[1]["quality_label"] == "현재가 미수신, 전일종가 일부 누락"


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
    assert row["condition_leader_count"] == 1
    assert row["strong_count"] == 2
    assert row["strong_ratio"] == 0.4
    assert row["leader_ratio"] == 0.2
    assert row["condition_signal_source"] == "condition_events"
    assert row["top_leader_symbol"] == "000002"
    assert row["top_leader_name"] == "후보전기"
    assert row["top_leader_turnover_krw"] == 7000000000
    assert row["turnover_label"] == "수신대금"
    assert row["member_data_coverage_label"] == "2/5 종목 수신"


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
