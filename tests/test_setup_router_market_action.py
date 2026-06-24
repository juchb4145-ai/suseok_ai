from datetime import datetime
from types import SimpleNamespace

from trading.strategy.market_context_view import market_context_view_from_snapshot
from trading.strategy.market_action import (
    MARKET_ACTION_DERIVED_FROM_SIDE_REGIME,
    MARKET_ACTION_NORMALIZED,
    MARKET_ACTION_UNMAPPED,
    normalize_market_action,
)
from trading.strategy.market_regime import (
    CandidateMarketAction,
    CandidateMarketPolicy,
    CompositeMarketMode,
    MarketRegimeStatus,
    MarketSide,
)
from trading.strategy.setup_data_readiness import SetupDataReadinessStatus

from tests.test_setup_router_data_readiness import _readiness
from tests.test_setup_router_v3 import _context


def test_market_action_normalization_contract():
    assert normalize_market_action("ALLOW_NORMAL").action == "ALLOW_NORMAL"
    assert normalize_market_action("ALLOW_REDUCED").action == "ALLOW_REDUCED"
    assert normalize_market_action("", side_market_regime="EXPANSION").action == "ALLOW_NORMAL"
    assert normalize_market_action("UNKNOWN", side_market_regime="SELECTIVE").action == "ALLOW_REDUCED"
    assert normalize_market_action("CHOPPY").action == "WAIT_MARKET"
    assert normalize_market_action("", side_market_regime="WEAK").action == "WAIT_MARKET"
    assert normalize_market_action("RISK_OFF").action == "BLOCK_NEW_ENTRY"

    unmapped = normalize_market_action("BROKEN", side_market_regime="")

    assert unmapped.action == "DATA_WAIT"
    assert MARKET_ACTION_UNMAPPED in unmapped.reason_codes


def test_setup_data_readiness_wait_market_is_context_not_data_failure():
    context = _context()
    context["market"] = {**context["market"], "market_action": "UNKNOWN", "side_market_regime": "CHOPPY"}

    snapshot = _readiness(context=context)

    assert snapshot.canonical_market_action == "WAIT_MARKET"
    assert MARKET_ACTION_DERIVED_FROM_SIDE_REGIME in snapshot.market_action_reason_codes
    assert snapshot.readiness_status == SetupDataReadinessStatus.READY.value
    assert snapshot.readiness_ready is True


def test_setup_data_readiness_unmapped_market_action_waits_for_market_action():
    context = _context()
    context["market"] = {**context["market"], "market_action": "", "side_market_regime": ""}

    snapshot = _readiness(context=context)

    assert snapshot.canonical_market_action == "DATA_WAIT"
    assert MARKET_ACTION_UNMAPPED in snapshot.market_action_reason_codes
    assert snapshot.readiness_status == SetupDataReadinessStatus.WAIT_MARKET_ACTION.value
    assert snapshot.readiness_ready is False
    assert "UNKNOWN" not in snapshot.canonical_market_action


def test_raw_market_action_aliases_are_normalized():
    wait = normalize_market_action("WAIT")
    block = normalize_market_action("BLOCK")

    assert wait.action == "WAIT_MARKET"
    assert MARKET_ACTION_NORMALIZED in wait.reason_codes
    assert block.action == "BLOCK_NEW_ENTRY"
    assert MARKET_ACTION_NORMALIZED in block.reason_codes


def test_enum_repr_market_action_aliases_are_normalized():
    assert normalize_market_action("CandidateMarketAction.ALLOW_NORMAL").action == "ALLOW_NORMAL"
    assert normalize_market_action("", side_market_regime="MarketRegimeStatus.EXPANSION").action == "ALLOW_NORMAL"
    assert normalize_market_action("CANDIDATEMARKETACTION.ALLOW_REDUCED").action == "ALLOW_REDUCED"


def test_market_context_view_policy_for_returns_serialized_policy_values():
    policy = CandidateMarketPolicy(
        code="000001",
        market_side=MarketSide.KOSPI,
        market_side_source="kiwoom_master",
        market_side_resolution_status="RESOLVED",
        market_status=MarketRegimeStatus.EXPANSION,
        global_market_status=MarketRegimeStatus.EXPANSION,
        market_action=CandidateMarketAction.ALLOW_NORMAL,
        position_size_multiplier_hint=1.0,
        block_new_entry=False,
        reason_codes=("MARKET_EXPANSION_ALLOW",),
    )
    snapshot = SimpleNamespace(
        trade_date="2026-06-22",
        calculated_at="2026-06-22T09:05:00",
        global_status=MarketRegimeStatus.EXPANSION,
        kospi_status=MarketRegimeStatus.EXPANSION,
        kosdaq_status=MarketRegimeStatus.SELECTIVE,
        composite_market_mode=CompositeMarketMode.BROAD_RISK_ON,
        systemic_risk_off=False,
        market_session_status="open",
        market_open=True,
        market_closed=False,
        risk_off_detected=False,
        weak_market_detected=False,
        reason_codes=("INDEX_UP",),
        candidate_policy_by_code={"000001": policy},
        kospi_snapshot={"status": MarketRegimeStatus.EXPANSION},
        kosdaq_snapshot={"status": MarketRegimeStatus.SELECTIVE},
    )

    view = market_context_view_from_snapshot(snapshot)
    serialized = view.policy_for("000001")

    assert serialized["market_side"] == "KOSPI"
    assert serialized["market_status"] == "EXPANSION"
    assert serialized["global_market_status"] == "EXPANSION"
    assert serialized["market_action"] == "ALLOW_NORMAL"
    assert normalize_market_action(
        serialized["market_action"],
        side_market_regime=serialized["market_status"],
        global_market_regime=serialized["global_market_status"],
    ).action == "ALLOW_NORMAL"
