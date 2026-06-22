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


def test_latest_entry_decisions_per_candidate_prefers_candidate_specific_rows(tmp_path):
    db = TradingDatabase(str(tmp_path / "entry-per-candidate.db"))
    db.save_entry_decisions(
        [
            {
                "trade_date": TRADE_DATE,
                "calculated_at": "2026-06-22T09:05:00",
                "candidate_id": None,
                "code": "000001",
                "entry_status": "PRICE_WAIT",
                "price_location": "CODE_FALLBACK",
            },
            {
                "trade_date": TRADE_DATE,
                "calculated_at": "2026-06-22T09:04:00",
                "candidate_id": 101,
                "code": "000001",
                "entry_status": "OBSERVE_READY",
                "price_location": "CANDIDATE_SPECIFIC",
            },
        ]
    )

    rows = db.latest_entry_decisions_per_candidate(trade_date=TRADE_DATE, candidate_ids=[101], codes=["000001"])
    by_candidate = {row.get("candidate_id"): row for row in rows}
    by_code = {row.get("code"): row for row in rows if row.get("candidate_id") is None}

    assert by_candidate[101]["price_location"] == "CANDIDATE_SPECIFIC"
    assert by_code["000001"]["price_location"] == "CODE_FALLBACK"


def test_setup_router_state_ignores_price_only_observation_transition(tmp_path):
    db = TradingDatabase(str(tmp_path / "setup-state-price-only.db"))
    first = {
        **_observation("PENDING", "FORMING", "obs-1"),
        "material_state_fingerprint": "material-fixed",
        "observation_fingerprint": "obs-1",
        "detector_phase": "BELOW_CONFIRMED",
        "material_change_kind": "STATE_CREATED",
        "last_material_change_at": "2026-06-22T09:05:00",
        "current_price": 1000,
    }
    second = {
        **first,
        "fingerprint": "obs-2",
        "observation_fingerprint": "obs-2",
        "material_change_kind": "NONE",
        "calculated_at": "2026-06-22T09:05:05",
        "current_price": 1001,
    }

    db.save_setup_router_states([first])
    db.save_setup_router_states([second])

    states = db.list_setup_router_states(trade_date=TRADE_DATE, candidate_instance_ids=["ci-1"], router_version="setup_router_v3.3")
    transitions = db.conn.execute("SELECT * FROM setup_router_state_transitions_v2 WHERE trade_date = ?", (TRADE_DATE,)).fetchall()

    assert len(transitions) == 1
    assert states[0]["last_evaluated_at"] == "2026-06-22T09:05:05"
    assert states[0]["last_material_change_at"] == "2026-06-22T09:05:00"


def test_setup_router_state_is_theme_scoped_and_expires_previous_selected_theme(tmp_path):
    db = TradingDatabase(str(tmp_path / "setup-state-theme-scope.db"))
    theme_a = {
        **_observation("PENDING", "FORMING", "obs-a"),
        "theme_id": "theme-a",
        "material_state_fingerprint": "material-a",
        "observation_fingerprint": "obs-a",
        "detector_phase": "PULLBACK_SCAN",
        "material_change_kind": "STATE_CREATED",
    }
    theme_b = {
        **_observation("PENDING", "FORMING", "obs-b"),
        "theme_id": "theme-b",
        "material_state_fingerprint": "material-b",
        "observation_fingerprint": "obs-b",
        "detector_phase": "PULLBACK_SCAN",
        "material_change_kind": "STATE_CREATED",
        "calculated_at": "2026-06-22T09:06:00",
    }

    db.save_setup_router_states([theme_a])
    db.save_setup_router_states([theme_b])

    states = db.list_setup_router_states(trade_date=TRADE_DATE, candidate_instance_ids=["ci-1"], router_version="setup_router_v3.3")
    by_theme = {row["theme_id"]: row for row in states}

    assert by_theme["theme-a"]["lifecycle_state"] == "EXPIRED"
    assert by_theme["theme-b"]["lifecycle_state"] == "FORMING"
    assert by_theme["theme-a"]["state_payload"]["expired_reason"] == "SETUP_SELECTED_THEME_CHANGED"


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
        "schema_version": "setup_router_v3.observe.v3",
        "feature_schema_version": "setup_router_v3.features.v3",
        "router_version": "setup_router_v3.3",
        "state_version": "setup_router_v3.state.v2",
        "setup_generation": 1,
        "setup_instance_id": "setup-ci-1-vwap-1",
        "lifecycle_state": "MATCHED" if shape_status == "MATCHED" else "FORMING",
        "post_subscription_tick_verified": True,
        "entry_decision_at": "2026-06-22T09:05:00",
        "entry_decision_id": 1,
        "entry_decision_age_sec": 0,
        "entry_decision_fresh": True,
        "entry_decision_source": "entry_engine",
        "state_payload": {"vwap": 990},
        "last_material_change_at": "2026-06-22T09:05:00",
        "quantity": 0,
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
