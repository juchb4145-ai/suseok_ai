from __future__ import annotations

from datetime import datetime, timedelta

from trading.strategy.candidate_identity import CandidateGenerationConfig, decide_candidate_instance, identity_metadata


NOW = datetime(2026, 6, 1, 10, 0, 0)


def _existing_metadata(**overrides):
    metadata = {
        "candidate_instance_id": "ci:2026-06-01:000001:1:old",
        "candidate_generation_seq": 1,
        "candidate_instance_theme_id": "ai",
        "candidate_instance_source": "themelab_flow",
        "candidate_instance_strategy_name": "kosdaq_theme_profile",
        "candidate_instance_first_seen_at": (NOW - timedelta(hours=2)).isoformat(),
        "candidate_instance_last_seen_at": (NOW - timedelta(minutes=30)).isoformat(),
    }
    metadata.update(overrides)
    return metadata


def test_stale_redetect_minutes_creates_new_generation_after_threshold():
    decision = decide_candidate_instance(
        trade_date="2026-06-01",
        code="000001",
        source="themelab_flow",
        strategy_name="kosdaq_theme_profile",
        theme_id="ai",
        first_seen_at=(NOW - timedelta(hours=2)).isoformat(),
        existing_metadata=_existing_metadata(candidate_instance_last_seen_at=(NOW - timedelta(minutes=91)).isoformat()),
        now=NOW,
        config=CandidateGenerationConfig(stale_redetect_minutes=90, generation_min_gap_minutes=20),
    )

    assert decision.candidate_generation_seq == 2
    assert decision.generation_reason == "stale_re_detected"
    assert decision.minutes_since_previous_signal == 91


def test_stale_redetect_within_threshold_keeps_same_generation():
    decision = decide_candidate_instance(
        trade_date="2026-06-01",
        code="000001",
        source="themelab_flow",
        strategy_name="kosdaq_theme_profile",
        theme_id="ai",
        first_seen_at=(NOW - timedelta(hours=2)).isoformat(),
        existing_metadata=_existing_metadata(candidate_instance_last_seen_at=(NOW - timedelta(minutes=89)).isoformat()),
        now=NOW,
        config=CandidateGenerationConfig(stale_redetect_minutes=90, generation_min_gap_minutes=20),
    )

    assert decision.candidate_generation_seq == 1
    assert decision.generation_reason == "same_generation"


def test_theme_change_respects_config_flag():
    disabled = decide_candidate_instance(
        trade_date="2026-06-01",
        code="000001",
        source="themelab_flow",
        strategy_name="kosdaq_theme_profile",
        theme_id="robotics",
        first_seen_at=(NOW - timedelta(hours=2)).isoformat(),
        existing_metadata=_existing_metadata(candidate_instance_last_seen_at=(NOW - timedelta(minutes=30)).isoformat()),
        now=NOW,
        config=CandidateGenerationConfig(new_generation_on_theme_change=False, generation_min_gap_minutes=20),
    )
    enabled = decide_candidate_instance(
        trade_date="2026-06-01",
        code="000001",
        source="themelab_flow",
        strategy_name="kosdaq_theme_profile",
        theme_id="robotics",
        first_seen_at=(NOW - timedelta(hours=2)).isoformat(),
        existing_metadata=_existing_metadata(candidate_instance_last_seen_at=(NOW - timedelta(minutes=30)).isoformat()),
        now=NOW,
        config=CandidateGenerationConfig(new_generation_on_theme_change=True, generation_min_gap_minutes=20),
    )

    assert disabled.generation_reason == "same_generation"
    assert enabled.generation_reason == "theme_changed"
    assert enabled.candidate_generation_seq == 2


def test_theme_name_change_can_create_new_generation():
    decision = decide_candidate_instance(
        trade_date="2026-06-01",
        code="000001",
        source="themelab_flow",
        strategy_name="kosdaq_theme_profile",
        theme_id="ai",
        theme_name="AI Rebranded",
        first_seen_at=(NOW - timedelta(hours=2)).isoformat(),
        existing_metadata=_existing_metadata(
            candidate_instance_theme_name="AI",
            candidate_instance_last_seen_at=(NOW - timedelta(minutes=30)).isoformat(),
        ),
        now=NOW,
        config=CandidateGenerationConfig(new_generation_on_theme_change=True, generation_min_gap_minutes=20),
    )

    assert decision.generation_reason == "theme_changed"
    assert decision.candidate_generation_seq == 2


def test_source_change_respects_config_flag():
    disabled = decide_candidate_instance(
        trade_date="2026-06-01",
        code="000001",
        source="manual_scan",
        strategy_name="kosdaq_theme_profile",
        theme_id="ai",
        first_seen_at=(NOW - timedelta(hours=2)).isoformat(),
        existing_metadata=_existing_metadata(candidate_instance_last_seen_at=(NOW - timedelta(minutes=30)).isoformat()),
        now=NOW,
        config=CandidateGenerationConfig(new_generation_on_source_change=False, generation_min_gap_minutes=20),
    )
    enabled = decide_candidate_instance(
        trade_date="2026-06-01",
        code="000001",
        source="manual_scan",
        strategy_name="kosdaq_theme_profile",
        theme_id="ai",
        first_seen_at=(NOW - timedelta(hours=2)).isoformat(),
        existing_metadata=_existing_metadata(candidate_instance_last_seen_at=(NOW - timedelta(minutes=30)).isoformat()),
        now=NOW,
        config=CandidateGenerationConfig(new_generation_on_source_change=True, generation_min_gap_minutes=20),
    )

    assert disabled.generation_reason == "same_generation"
    assert enabled.generation_reason == "source_changed"
    assert enabled.candidate_generation_seq == 2


def test_min_generation_gap_blocks_excessive_generation():
    decision = decide_candidate_instance(
        trade_date="2026-06-01",
        code="000001",
        source="themelab_flow",
        strategy_name="kosdaq_theme_profile",
        theme_id="robotics",
        first_seen_at=(NOW - timedelta(hours=2)).isoformat(),
        existing_metadata=_existing_metadata(candidate_instance_last_seen_at=(NOW - timedelta(minutes=5)).isoformat()),
        now=NOW,
        config=CandidateGenerationConfig(new_generation_on_theme_change=True, generation_min_gap_minutes=20),
    )

    assert decision.candidate_generation_seq == 1
    assert decision.generation_reason == "same_generation_min_gap_guardrail"
    assert decision.blocked_generation_reason == "theme_changed"
    assert decision.excessive_generation_blocked is True


def test_generation_reason_metadata_is_preserved():
    decision = decide_candidate_instance(
        trade_date="2026-06-01",
        code="000001",
        source="themelab_flow",
        strategy_name="kosdaq_theme_profile",
        theme_id="ai",
        first_seen_at=NOW.isoformat(),
        existing_metadata=None,
        now=NOW,
        config=CandidateGenerationConfig(),
    )

    metadata = identity_metadata(
        decision,
        source="themelab_flow",
        strategy_name="kosdaq_theme_profile",
        theme_id="ai",
        first_seen_at=NOW.isoformat(),
        last_seen_at=NOW.isoformat(),
        config=CandidateGenerationConfig(),
    )

    assert metadata["generation_reason"] == "initial_generation"
    assert metadata["candidate_generation_reason"] == "initial_generation"
    assert "candidate_generation_config" in metadata
