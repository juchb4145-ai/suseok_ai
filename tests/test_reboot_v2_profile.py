from trading.strategy.reboot_v2 import RebootV2RuntimeProfile, reboot_v2_runtime_profile, strategy_reboot_v2_enabled


def test_runtime_profile_defaults_to_v2_observe(monkeypatch):
    monkeypatch.delenv("STRATEGY_RUNTIME_PROFILE", raising=False)
    monkeypatch.delenv("STRATEGY_REBOOT_V2_PROFILE", raising=False)
    monkeypatch.delenv("STRATEGY_REBOOT_V2_ENABLED", raising=False)

    assert reboot_v2_runtime_profile() == RebootV2RuntimeProfile.V2_OBSERVE
    assert strategy_reboot_v2_enabled() is True


def test_runtime_profile_accepts_theme_core_v3_alias(monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNTIME_PROFILE", "THEME_CORE_V3")

    assert reboot_v2_runtime_profile() == RebootV2RuntimeProfile.THEME_CORE_V3
    assert strategy_reboot_v2_enabled() is True


def test_runtime_profile_legacy_requires_explicit_profile_or_compat_disable(monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNTIME_PROFILE", "LEGACY")
    assert reboot_v2_runtime_profile() == RebootV2RuntimeProfile.LEGACY
    assert strategy_reboot_v2_enabled() is False

    monkeypatch.delenv("STRATEGY_RUNTIME_PROFILE", raising=False)
    monkeypatch.delenv("STRATEGY_REBOOT_V2_PROFILE", raising=False)
    monkeypatch.setenv("STRATEGY_REBOOT_V2_ENABLED", "0")
    assert reboot_v2_runtime_profile() == RebootV2RuntimeProfile.LEGACY
