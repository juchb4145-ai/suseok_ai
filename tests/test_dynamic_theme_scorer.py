import json
from pathlib import Path

from tests.theme_naver_helpers import repo_with_naver_fixture
from trading.theme_engine.models import ThemeMembership
from trading.theme_engine.scorer import ThemeScoringEngine
from trading.theme_engine.stock_snapshot import snapshot_from_dict


TICKS = Path("tests/fixtures/theme_engine/furiosa_ticks.json")
LEADER_ONLY_TICKS = Path("tests/fixtures/theme_engine/furiosa_leader_only_ticks.json")


def _repo_with_fixture(tmp_path):
    return repo_with_naver_fixture(tmp_path)


def test_scorer_calculates_breadth_leader_and_clamps_score(tmp_path):
    db, repo = _repo_with_fixture(tmp_path)
    memberships = repo.get_members_by_theme("furiosa_ai")
    snapshots = [snapshot_from_dict(item) for item in json.loads(TICKS.read_text(encoding="utf-8"))]

    ranked = ThemeScoringEngine(repo).score_and_rank([("furiosa_ai", "퓨리오사AI", memberships)], snapshots)

    item = ranked[0]
    assert 0 <= item.theme_score <= 100
    assert item.breadth > 0.5
    assert item.leader_code == "000001"
    assert "LEADER_ONLY_THEME" not in item.details["reason_codes"]
    assert item.details["top_stocks"]
    assert item.details["top_stocks"][0]["stock_code"] == item.leader_code
    assert len(item.details["top_stocks"]) <= 5
    db.close()


def test_scorer_flags_leader_only_theme(tmp_path):
    db, repo = _repo_with_fixture(tmp_path)
    memberships = repo.get_members_by_theme("furiosa_ai")
    snapshots = [snapshot_from_dict(item) for item in json.loads(LEADER_ONLY_TICKS.read_text(encoding="utf-8"))]

    item = ThemeScoringEngine().score_theme("furiosa_ai", "퓨리오사AI", memberships, snapshots)

    assert "LEADER_ONLY_THEME" in item.details["reason_codes"]
    assert item.leader_gap >= 3.0
    db.close()


def test_scorer_records_scored_members_without_snapshots():
    memberships = _memberships()
    ticks = json.loads(TICKS.read_text(encoding="utf-8"))
    snapshots = [snapshot_from_dict(item) for item in ticks[:3]]

    item = ThemeScoringEngine().score_theme("furiosa_ai", "Furiosa AI", memberships, snapshots)

    scored_members = item.details["scored_members"]
    assert len(scored_members) == len(memberships)
    assert {member["stock_code"] for member in scored_members if not member["has_snapshot"]} == {"000004", "000005"}


def test_scorer_flags_low_snapshot_coverage():
    memberships = _memberships()
    ticks = json.loads(TICKS.read_text(encoding="utf-8"))
    snapshots = [snapshot_from_dict(item) for item in ticks[:2]]

    item = ThemeScoringEngine().score_theme("furiosa_ai", "?⑤━?ㅼ궗AI", memberships, snapshots)

    assert "LOW_SNAPSHOT_COVERAGE" in item.details["reason_codes"]
    assert item.details["snapshot_quality"]["active_member_count"] == 5
    assert item.details["snapshot_quality"]["valid_snapshot_count"] == 2
    assert item.details["snapshot_quality"]["snapshot_coverage"] == 0.4
    assert item.details["snapshot_quality"]["missing_snapshot_count"] == 3


def test_scorer_flags_estimated_turnover_heavy():
    memberships = _memberships()
    ticks_payload = json.loads(TICKS.read_text(encoding="utf-8"))
    ticks = [
        {**ticks_payload[0], "metadata": {"reason_codes": ["TURNOVER_ESTIMATED"]}},
        {**ticks_payload[1], "metadata": {"reason_codes": ["TURNOVER_ESTIMATED"]}},
        ticks_payload[2],
    ]
    snapshots = [snapshot_from_dict(item) for item in ticks]

    item = ThemeScoringEngine().score_theme("furiosa_ai", "?⑤━?ㅼ궗AI", memberships, snapshots)

    assert "ESTIMATED_TURNOVER_HEAVY" in item.details["reason_codes"]
    assert item.details["snapshot_quality"]["estimated_turnover_count"] == 2
    assert item.details["snapshot_quality"]["estimated_turnover_ratio"] == round(2 / 3, 4)
    assert "TURNOVER_ESTIMATED" in item.details["top_stocks"][0]["metadata_reason_codes"]


def test_repository_restores_extended_activity_details(tmp_path):
    db, repo = _repo_with_fixture(tmp_path)
    memberships = repo.get_members_by_theme("furiosa_ai")
    snapshots = [snapshot_from_dict(item) for item in json.loads(LEADER_ONLY_TICKS.read_text(encoding="utf-8"))]

    item = ThemeScoringEngine().score_theme("furiosa_ai", "?⑤━?ㅼ궗AI", memberships, snapshots)
    repo.save_activity_snapshot(item)
    loaded = repo.latest_activity_snapshots(limit=1)[0]

    assert loaded.details["top_stocks"] == item.details["top_stocks"]
    assert loaded.details["scored_members"] == item.details["scored_members"]
    assert loaded.details["snapshot_quality"] == item.details["snapshot_quality"]
    db.close()


def _membership(stock_code, stock_name="MOCK", membership_score=0.8):
    return ThemeMembership(
        "furiosa_ai",
        stock_code,
        stock_name=stock_name,
        membership_score=membership_score,
        active=True,
        trade_eligible=True,
    )


def _memberships():
    return [
        _membership("000001"),
        _membership("000002"),
        _membership("000003"),
        _membership("000004"),
        _membership("000005"),
    ]
