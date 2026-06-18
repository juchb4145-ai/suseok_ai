from trading.theme_engine.models import ThemeMembership
from trading.theme_engine.universe import ThemeRegistry


def test_theme_registry_builds_theme_universe_without_condition_profiles():
    kospi = ThemeMembership("ai", "000001", membership_score=0.92, active=True, trade_eligible=True)
    kosdaq = ThemeMembership("ai", "000002", membership_score=0.88, active=True, trade_eligible=True)
    setattr(kospi, "market", "KOSPI")
    setattr(kosdaq, "market", "KOSDAQ")

    snapshots = ThemeRegistry().build([("ai", "AI", [kospi, kosdaq])])

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.theme_id == "ai"
    assert snapshot.member_count == 2
    assert snapshot.tradable_member_count == 2
    assert snapshot.kospi_member_count == 1
    assert snapshot.kosdaq_member_count == 1
    assert snapshot.membership_quality > 0.8
    assert snapshot.reason_codes == ()


def test_theme_registry_marks_data_wait_reasons_for_empty_or_low_quality_universe():
    weak = ThemeMembership("weak", "000003", membership_score=0.2, active=True, trade_eligible=False)

    snapshot = ThemeRegistry().build([("weak", "Weak", [weak])])[0]

    assert snapshot.tradable_member_count == 0
    assert "NO_TRADABLE_MEMBERS" in snapshot.reason_codes
    assert "LOW_MEMBERSHIP_QUALITY" in snapshot.reason_codes
