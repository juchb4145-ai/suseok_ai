from __future__ import annotations

import re
from pathlib import Path

import pytest


PROFILE_CHOICES = ("quick", "unit", "integration", "slow", "full")

E2E_TEST_FILES = {
    "test_market_open_live_sim_script.py",
    "test_phase2_acceptance.py",
    "test_websocket_real_pilot.py",
}

SLOW_TEST_FILES = {
    "test_core_runtime_api.py",
    "test_dry_run_performance_analyzer.py",
    "test_exit_decisions.py",
    "test_gateway_transport_ws_endpoint.py",
    "test_live_sim_canary.py",
    "test_live_sim_order_execution.py",
    "test_market_regime.py",
    "test_market_side_confirmation_persistence.py",
    "test_opening_theme_burst_runtime_wiring.py",
    "test_phase2_acceptance.py",
    "test_reboot_v2_runtime_cutover.py",
    "test_runtime_gateway_adapters.py",
    "test_runtime_supervisor.py",
    "test_strategy_context_v3.py",
    "test_theme_backfill.py",
    "test_theme_core_v3_runtime.py",
    "test_theme_lab_flow.py",
    "test_theme_lab_runtime_wiring.py",
    "test_themelab_dry_run_bridge.py",
    "test_themelab_web_dashboard.py",
    "test_trade_reviews.py",
    "test_websocket_real_pilot.py",
}

INTEGRATION_FILE_KEYWORDS = (
    "_api",
    "_db",
    "_gateway",
    "_runtime",
    "_storage",
    "_websocket",
    "_ws_",
    "dashboard",
    "kiwoom",
    "live_sim",
    "market_open",
    "phase2",
    "pre_market",
    "postmarket",
    "reconcile",
    "themelab",
    "theme_lab",
    "transport",
)

SERIAL_TEST_FILES = {
    "test_core_runtime_api.py",
    "test_gateway_transport_ws_endpoint.py",
    "test_websocket_real_pilot.py",
}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("trading test selection")
    group.addoption(
        "--profile",
        action="store",
        choices=PROFILE_CHOICES,
        help=(
            "Select a test profile: quick excludes slow/e2e, unit selects fast unit "
            "tests, integration selects non-slow integration tests, slow selects "
            "slow/e2e tests, and full keeps the complete suite."
        ),
    )
    group.addoption(
        "--shard",
        action="store",
        metavar="N/M",
        help="Run a deterministic shard of the selected test set, for example 1/4.",
    )


def pytest_report_header(config: pytest.Config) -> list[str]:
    header = []
    profile = config.getoption("--profile")
    shard = config.getoption("--shard")
    if profile:
        header.append(f"test profile: {profile}")
    if shard:
        header.append(f"test shard: {shard}")
    return header


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        _mark_item_by_file(item)

    selected = list(items)

    profile = config.getoption("--profile")
    if profile and profile != "full":
        selected, deselected = _select_profile(selected, profile)
        if deselected:
            config.hook.pytest_deselected(items=deselected)

    shard_value = config.getoption("--shard")
    if shard_value:
        selected, deselected = _select_shard(selected, shard_value)
        if deselected:
            config.hook.pytest_deselected(items=deselected)

    items[:] = selected


def _mark_item_by_file(item: pytest.Item) -> None:
    path = _item_path(item)
    filename = path.name

    if filename in E2E_TEST_FILES:
        item.add_marker(pytest.mark.e2e)
        item.add_marker(pytest.mark.integration)
        item.add_marker(pytest.mark.slow)
    elif filename in SLOW_TEST_FILES:
        item.add_marker(pytest.mark.slow)

    if filename in SERIAL_TEST_FILES:
        item.add_marker(pytest.mark.serial)

    if _is_integration_file(filename):
        item.add_marker(pytest.mark.integration)

    if not _has_any_marker(item, {"integration", "e2e"}):
        item.add_marker(pytest.mark.unit)


def _select_profile(
    items: list[pytest.Item],
    profile: str,
) -> tuple[list[pytest.Item], list[pytest.Item]]:
    selected = []
    deselected = []

    for item in items:
        markers = {marker.name for marker in item.iter_markers()}
        if _profile_matches(markers, profile):
            selected.append(item)
        else:
            deselected.append(item)

    return selected, deselected


def _profile_matches(markers: set[str], profile: str) -> bool:
    if profile == "quick":
        return "slow" not in markers and "e2e" not in markers
    if profile == "unit":
        return "unit" in markers and "slow" not in markers
    if profile == "integration":
        return (
            "integration" in markers
            and "slow" not in markers
            and "e2e" not in markers
        )
    if profile == "slow":
        return "slow" in markers or "e2e" in markers
    return True


def _select_shard(
    items: list[pytest.Item],
    shard_value: str,
) -> tuple[list[pytest.Item], list[pytest.Item]]:
    shard_index, shard_total = _parse_shard(shard_value)
    selected = []
    deselected = []

    for position, item in enumerate(items):
        if position % shard_total == shard_index - 1:
            selected.append(item)
        else:
            deselected.append(item)

    return selected, deselected


def _parse_shard(shard_value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)/(\d+)", shard_value.strip())
    if not match:
        raise pytest.UsageError("--shard must use N/M format, for example --shard=1/4")

    shard_index = int(match.group(1))
    shard_total = int(match.group(2))
    if shard_total < 1:
        raise pytest.UsageError("--shard total must be at least 1")
    if shard_index < 1 or shard_index > shard_total:
        raise pytest.UsageError("--shard index must be between 1 and the shard total")

    return shard_index, shard_total


def _is_integration_file(filename: str) -> bool:
    return any(keyword in filename for keyword in INTEGRATION_FILE_KEYWORDS)


def _has_any_marker(item: pytest.Item, marker_names: set[str]) -> bool:
    return any(marker.name in marker_names for marker in item.iter_markers())


def _item_path(item: pytest.Item) -> Path:
    return Path(str(getattr(item, "path", None) or getattr(item, "fspath")))
