from storage.db import TradingDatabase


TRADE_DATE = "2026-06-22"


def test_setup_observation_storage_upserts_latest_and_dedupes_same_fingerprint(tmp_path):
    db = TradingDatabase(str(tmp_path / "setup-storage.db"))
    payload = _observation("PENDING", "FORMING", "fp-1")

    assert db.save_setup_observations([payload]) == 1
    assert db.save_setup_observations([payload]) == 1

    latest = db.list_setup_observations_latest(trade_date=TRADE_DATE)
    transitions = db.list_setup_observation_transitions(trade_date=TRADE_DATE)

    assert len(latest) == 1
    assert latest[0]["router_status"] == "PENDING"
    assert len(transitions) == 1

    changed = _observation("VALID_OBSERVE", "MATCHED", "fp-2")
    db.save_setup_observations([changed])

    latest = db.list_setup_observations_latest(trade_date=TRADE_DATE)
    transitions = db.list_setup_observation_transitions(trade_date=TRADE_DATE)

    assert latest[0]["router_status"] == "VALID_OBSERVE"
    assert len(transitions) == 2
    assert transitions[0]["previous_router_status"] == "PENDING"
    assert transitions[0]["current_router_status"] == "VALID_OBSERVE"


def _observation(router_status, shape_status, fingerprint):
    return {
        "trade_date": TRADE_DATE,
        "calculated_at": "2026-06-22T09:05:00",
        "candidate_id": 1,
        "candidate_instance_id": "ci-1",
        "code": "000001",
        "name": "테스트",
        "setup_type": "VWAP_RECLAIM",
        "shape_status": shape_status,
        "context_status": "ELIGIBLE",
        "router_status": router_status,
        "entry_alignment_status": "ENTRY_OBSERVE_READY",
        "primary_setup": True,
        "setup_quality_score": 91.0,
        "context_id": "ctx-1",
        "theme_id": "ai",
        "theme_name": "AI",
        "theme_state": "LEADING_THEME",
        "leadership_status": "INCUMBENT",
        "stock_role": "LEADER_CONFIRMED",
        "market_side": "KOSDAQ",
        "market_action": "ALLOW_NORMAL",
        "session_phase": "MORNING_TREND",
        "current_price": 1000,
        "fingerprint": fingerprint,
        "reason_codes": ["SETUP_ROUTER_V3_OBSERVE_ONLY"],
        "price_structure": {"vwap": 990},
        "safety": {
            "ready_allowed": False,
            "candidate_promotion_allowed": False,
            "opportunity_rank_allowed": False,
            "order_intent_allowed": False,
            "live_order_allowed": False,
        },
    }
