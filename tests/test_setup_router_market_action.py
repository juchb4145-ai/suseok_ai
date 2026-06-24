from datetime import datetime

from trading.strategy.market_action import (
    MARKET_ACTION_DERIVED_FROM_SIDE_REGIME,
    MARKET_ACTION_NORMALIZED,
    MARKET_ACTION_UNMAPPED,
    normalize_market_action,
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
