from storage.db import TradingDatabase


TRADE_DATE = "2026-06-22"


def test_setup_observation_storage_upserts_latest_and_dedupes_same_fingerprint(tmp_path):
    db = TradingDatabase(str(tmp_path / "setup-storage.db"))
    payload = _observation("PENDING", "FORMING", "fp-1")

    assert db.save_setup_observations([payload]) == 1
    assert db.save_setup_observations([payload]) == 0

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

    first_counts = db.save_setup_router_states([first])
    second_counts = db.save_setup_router_states([second])

    states = db.list_setup_router_states(trade_date=TRADE_DATE, candidate_instance_ids=["ci-1"], router_version="setup_router_v3.5.1")
    transitions = db.conn.execute("SELECT * FROM setup_router_state_transitions_v3 WHERE trade_date = ?", (TRADE_DATE,)).fetchall()

    assert len(transitions) == 1
    assert states[0]["last_evaluated_at"] == "2026-06-22T09:05:00"
    assert states[0]["last_material_change_at"] == "2026-06-22T09:05:00"
    assert first_counts["state_write_count"] == 1
    assert second_counts["state_write_count"] == 0
    assert second_counts["state_no_change_skip_count"] == 1


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

    states = db.list_setup_router_states(trade_date=TRADE_DATE, candidate_instance_ids=["ci-1"], router_version="setup_router_v3.5.1")
    by_theme = {row["theme_id"]: row for row in states}

    assert by_theme["theme-a"]["lifecycle_state"] == "EXPIRED"
    assert by_theme["theme-b"]["lifecycle_state"] == "FORMING"
    assert by_theme["theme-a"]["state_payload"]["expired_reason"] == "SETUP_SELECTED_THEME_CHANGED"


def test_pending_completed_reopens_with_new_epoch_and_reset_backlog_fields(tmp_path):
    db = TradingDatabase(str(tmp_path / "pending-reopen.db"))
    base = {
        "trade_date": TRADE_DATE,
        "candidate_instance_id": "ci-1",
        "code": "000001",
        "router_version": "setup_router_v3.5.1",
        "state_version": "setup_router_v3.state.v3.2",
        "selected_theme_id": "ai",
        "pending_reasons": ["ENTRY_DECISION_CHANGED"],
        "first_pending_at": "2026-06-22T09:05:00",
        "last_pending_at": "2026-06-22T09:05:00",
    }

    assert db.save_setup_router_pending_evaluations([base]) == 1
    first = db.list_setup_router_pending_evaluations(trade_date=TRADE_DATE)[0]
    assert first["pending_epoch"] == 1
    assert first["pending_priority"] == 0
    assert db.update_setup_router_pending_evaluations([{**base, "status": "COMPLETED", "completed_at": "2026-06-22T09:05:10"}]) == 1

    assert db.save_setup_router_pending_evaluations([{**base, "pending_reasons": ["PERIODIC_RECONCILE"], "first_pending_at": "2026-06-22T09:10:00", "last_pending_at": "2026-06-22T09:10:00"}]) == 1
    reopened = db.list_setup_router_pending_evaluations(trade_date=TRADE_DATE)[0]

    assert reopened["status"] == "PENDING"
    assert reopened["pending_epoch"] == 2
    assert reopened["opened_from_status"] == "COMPLETED"
    assert reopened["pending_reasons"] == ["PERIODIC_RECONCILE"]
    assert reopened["pending_priority"] == 3
    assert reopened["first_pending_at"] == "2026-06-22T09:10:00"
    assert reopened["attempt_count"] == 0
    assert reopened["failure_count"] == 0
    assert reopened["last_error"] == ""


def test_retry_backoff_is_not_reopened_by_periodic_but_material_event_releases_it(tmp_path):
    db = TradingDatabase(str(tmp_path / "pending-retry.db"))
    base = {
        "trade_date": TRADE_DATE,
        "candidate_instance_id": "ci-1",
        "code": "000001",
        "router_version": "setup_router_v3.5.1",
        "state_version": "setup_router_v3.state.v3.2",
        "selected_theme_id": "ai",
        "pending_reasons": ["ENTRY_DECISION_CHANGED"],
        "first_pending_at": "2026-06-22T09:05:00",
        "last_pending_at": "2026-06-22T09:05:00",
    }
    db.save_setup_router_pending_evaluations([base])
    db.update_setup_router_pending_evaluations(
        [
            {
                **base,
                "status": "RETRY",
                "last_attempt_at": "2026-06-22T09:05:05",
                "last_error": "RuntimeError:test",
                "last_error_class": "RuntimeError",
                "next_retry_at": "2026-06-22T09:05:30",
            }
        ]
    )

    db.save_setup_router_pending_evaluations([{**base, "pending_reasons": ["PERIODIC_RECONCILE"], "last_pending_at": "2026-06-22T09:05:10"}])
    retry = db.list_setup_router_pending_evaluations(trade_date=TRADE_DATE, statuses=("RETRY",))[0]
    assert retry["status"] == "RETRY"
    assert retry["next_retry_at"] == "2026-06-22T09:05:30"

    db.save_setup_router_pending_evaluations([{**base, "pending_reasons": ["ENTRY_DECISION_CHANGED"], "last_pending_at": "2026-06-22T09:05:11"}])
    reopened = db.list_setup_router_pending_evaluations(trade_date=TRADE_DATE, statuses=("PENDING",))[0]
    assert reopened["status"] == "PENDING"
    assert reopened["next_retry_at"] == ""
    assert "ENTRY_DECISION_CHANGED" in reopened["pending_reasons"]


def test_complete_setup_router_evaluation_commits_observation_runtime_and_pending(tmp_path):
    db = TradingDatabase(str(tmp_path / "complete-evaluation.db"))
    pending = {
        "trade_date": TRADE_DATE,
        "candidate_instance_id": "ci-1",
        "code": "000001",
        "router_version": "setup_router_v3.5.1",
        "state_version": "setup_router_v3.state.v3.2",
        "pending_reasons": ["ENTRY_DECISION_CHANGED"],
        "first_pending_at": "2026-06-22T09:05:00",
        "last_pending_at": "2026-06-22T09:05:00",
    }
    db.save_setup_router_pending_evaluations([pending])
    selected = db.list_setup_router_pending_evaluations(trade_date=TRADE_DATE)[0]
    db.update_setup_router_pending_evaluations([{**pending, "status": "SELECTED", "selected_at": "2026-06-22T09:05:01", "last_attempt_at": "2026-06-22T09:05:01"}])
    counts = db.complete_setup_router_evaluation(
        observations=[_observation("VALID_OBSERVE", "MATCHED", "fp-complete")],
        runtime_update={
            "trade_date": TRADE_DATE,
            "candidate_instance_id": "ci-1",
            "code": "000001",
            "router_version": "setup_router_v3.5.1",
            "state_version": "setup_router_v3.state.v3.2",
            "last_evaluated_at": "2026-06-22T09:05:05",
            "last_success_at": "2026-06-22T09:05:05",
            "processed_entry_signature": "entry",
            "evaluation_count": 1,
        },
        pending_update={**pending, "pending_epoch": selected["pending_epoch"], "pending_instance_id": selected["pending_instance_id"], "status": "COMPLETED", "completed_at": "2026-06-22T09:05:05"},
    )

    assert counts["observation_write_count"] == 1
    assert counts["runtime_write_count"] == 1
    assert counts["pending_completed_count"] == 1
    assert db.list_setup_observations_latest(trade_date=TRADE_DATE)[0]["router_status"] == "VALID_OBSERVE"
    assert db.list_setup_router_candidate_runtime(trade_date=TRADE_DATE, candidate_instance_ids=["ci-1"])[0]["last_success_at"] == "2026-06-22T09:05:05"
    assert db.list_setup_router_pending_evaluations(trade_date=TRADE_DATE, statuses=("COMPLETED",))[0]["status"] == "COMPLETED"


def test_atomic_completion_rolls_back_partial_writes_on_injected_failure(tmp_path):
    db = TradingDatabase(str(tmp_path / "atomic-rollback.db"))
    pending = {
        "trade_date": TRADE_DATE,
        "candidate_instance_id": "ci-1",
        "code": "000001",
        "router_version": "setup_router_v3.5.1",
        "state_version": "setup_router_v3.state.v3.2",
        "pending_reasons": ["ENTRY_DECISION_CHANGED"],
        "first_pending_at": "2026-06-22T09:05:00",
        "last_pending_at": "2026-06-22T09:05:00",
    }
    db.save_setup_router_pending_evaluations([pending])
    selected = db.list_setup_router_pending_evaluations(trade_date=TRADE_DATE)[0]
    db.update_setup_router_pending_evaluations([{**pending, "status": "SELECTED", "selected_at": "2026-06-22T09:05:01", "last_attempt_at": "2026-06-22T09:05:01"}])

    result = db.complete_setup_router_evaluation_atomic(
        {
            "observations": [_observation("VALID_OBSERVE", "MATCHED", "fp-rollback")],
            "runtime_update": {
                "trade_date": TRADE_DATE,
                "candidate_instance_id": "ci-1",
                "code": "000001",
                "router_version": "setup_router_v3.5.1",
                "state_version": "setup_router_v3.state.v3.2",
                "last_evaluated_at": "2026-06-22T09:05:05",
                "last_success_at": "2026-06-22T09:05:05",
                "processed_entry_signature": "entry",
                "evaluation_count": 1,
            },
            "pending_update": {**pending, "pending_epoch": selected["pending_epoch"], "pending_instance_id": selected["pending_instance_id"], "status": "COMPLETED", "completed_at": "2026-06-22T09:05:05"},
            "completion_metadata": {"fail_after_observation_write": True},
        }
    )

    assert result["status"] == "STORAGE_ERROR"
    assert db.list_setup_observations_latest(trade_date=TRADE_DATE) == []
    assert db.list_setup_router_states(trade_date=TRADE_DATE, candidate_instance_ids=["ci-1"], router_version="setup_router_v3.5.1") == []
    assert db.list_setup_router_candidate_runtime(trade_date=TRADE_DATE, candidate_instance_ids=["ci-1"]) == []
    assert db.list_setup_router_pending_evaluations(trade_date=TRADE_DATE, statuses=("SELECTED",))[0]["status"] == "SELECTED"


def test_setup_router_state_version_isolation_between_v34_and_v35(tmp_path):
    db = TradingDatabase(str(tmp_path / "state-version-isolation.db"))
    legacy = {
        **_observation("PENDING", "FORMING", "legacy-fp"),
        "router_version": "setup_router_v3.4",
        "state_version": "setup_router_v3.state.v3",
        "material_state_fingerprint": "legacy-material",
    }
    current = {
        **_observation("PENDING", "FORMING", "current-fp"),
        "router_version": "setup_router_v3.5.1",
        "state_version": "setup_router_v3.state.v3.2",
        "material_state_fingerprint": "current-material",
    }

    db.save_setup_router_states([legacy])
    db.save_setup_router_states([current])

    legacy_rows = db.list_setup_router_states(trade_date=TRADE_DATE, candidate_instance_ids=["ci-1"], router_version="setup_router_v3.4")
    current_rows = db.list_setup_router_states(trade_date=TRADE_DATE, candidate_instance_ids=["ci-1"], router_version="setup_router_v3.5.1")
    old_table_count = db.conn.execute("SELECT COUNT(*) FROM setup_router_state_v2 WHERE trade_date = ? AND router_version = 'setup_router_v3.4'", (TRADE_DATE,)).fetchone()[0]
    new_table_count = db.conn.execute("SELECT COUNT(*) FROM setup_router_state_v3 WHERE trade_date = ? AND router_version = 'setup_router_v3.5.1'", (TRADE_DATE,)).fetchone()[0]

    assert legacy_rows[0]["router_version"] == "setup_router_v3.4"
    assert current_rows[0]["router_version"] == "setup_router_v3.5.1"
    assert old_table_count == 1
    assert new_table_count == 1


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
        "schema_version": "setup_router_v3.observe.v5.1",
        "feature_schema_version": "setup_router_v3.features.v4.2",
        "router_version": "setup_router_v3.5.1",
        "state_version": "setup_router_v3.state.v3.2",
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
