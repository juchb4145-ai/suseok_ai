import csv
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kiwoom.client import MockKiwoomClient
from kiwoom.tr import KiwoomTrRunner
from scripts.generate_theme_mappings import _print_result, parse_args
from trading.strategy.theme_template import (
    THEME_MAPPING_CSV_COLUMNS,
    ThemeTemplateResult,
    generate_theme_mappings_auto_csv,
)


def test_generate_theme_mappings_auto_csv_from_fake_tr_data(tmp_path):
    client = MockKiwoomClient()
    client.set_market_codes("0", ["005930", "000660", "005380"])
    client.set_market_codes("10", ["123456", "277810"])
    client.set_tr_pages(
        "opt90001",
        "",
        [
            {
                "prev_next": "2",
                "rows": [
                    {"종목코드": "550", "테마명": "반도체", "종목수": "4", "주요종목": "삼성전자, SK하이닉스"},
                ],
            },
            {
                "prev_next": "",
                "rows": [
                    {"테마코드": "770", "테마명": "로봇", "종목수": "3", "주요종목": "로보스타"},
                ],
            },
        ],
    )
    client.set_tr_pages(
        "opt90002",
        "550",
        [
            {
                "prev_next": "2",
                "rows": [
                    {"종목코드": "A005930", "종목명": "삼성전자"},
                    {"종목코드": "005380", "종목명": "현대차"},
                ],
            },
            {
                "prev_next": "",
                "rows": [
                    {"종목코드": "000660", "종목명": "SK하이닉스"},
                    {"종목코드": "123456", "종목명": "코스닥부품"},
                ],
            },
        ],
    )
    client.set_tr_pages(
        "opt90002",
        "770",
        [
            {
                "prev_next": "",
                "rows": [
                    {"종목코드": "A277810", "종목명": "로보스타"},
                    {"종목코드": "277810", "종목명": "로보스타"},
                    {"종목코드": "999999", "종목명": "미상종목"},
                ],
            }
        ],
    )
    sleeps = []
    runner = KiwoomTrRunner(client, request_delay_ms=1200, sleeper=lambda seconds: sleeps.append(seconds))
    output = tmp_path / "theme_mappings_auto.csv"

    result = generate_theme_mappings_auto_csv(
        client,
        output_path=output,
        overwrite=False,
        default_enabled=1,
        runner=runner,
        request_delay_ms=1200,
        now=_fixed_now(),
    )

    assert output.read_bytes().startswith(b"\xef\xbb\xbf")
    with output.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == THEME_MAPPING_CSV_COLUMNS
    assert [(row["theme_id"], row["code"]) for row in rows] == [
        ("kiwoom_550", "000660"),
        ("kiwoom_550", "005380"),
        ("kiwoom_550", "005930"),
        ("kiwoom_550", "123456"),
        ("kiwoom_770", "277810"),
        ("kiwoom_770", "999999"),
    ]
    by_code = {(row["theme_id"], row["code"]): row for row in rows}
    assert by_code[("kiwoom_550", "005930")]["strategy_profile"] == "SEMICONDUCTOR_SIGNAL_PROFILE"
    assert by_code[("kiwoom_550", "005930")]["is_signal_stock"] == "1"
    assert by_code[("kiwoom_550", "005930")]["is_large_cap"] == "1"
    assert by_code[("kiwoom_550", "005930")]["is_leader_candidate"] == "1"
    assert by_code[("kiwoom_550", "005930")]["base_priority"] == "100"
    assert by_code[("kiwoom_550", "005380")]["strategy_profile"] == "KOSPI_LEADER_PROFILE"
    assert by_code[("kiwoom_550", "005380")]["is_large_cap"] == "1"
    assert by_code[("kiwoom_550", "005380")]["base_priority"] == "70"
    assert by_code[("kiwoom_550", "123456")]["strategy_profile"] == "KOSDAQ_THEME_PROFILE"
    assert by_code[("kiwoom_550", "123456")]["base_priority"] == "60"
    assert by_code[("kiwoom_770", "277810")]["is_leader_candidate"] == "1"
    assert int(by_code[("kiwoom_770", "277810")]["base_priority"]) >= 80
    assert by_code[("kiwoom_770", "999999")]["market"] == "UNKNOWN"
    assert by_code[("kiwoom_770", "999999")]["enabled"] == "0"
    assert "market_unresolved=1" in by_code[("kiwoom_770", "999999")]["memo"]
    assert "generated_at=2026-05-29T08:30:00" in by_code[("kiwoom_550", "005930")]["memo"]
    assert result.rows_written == 6
    assert any(warning.startswith("MARKET_UNRESOLVED") for warning in result.warnings)
    assert [call["tr_code"] for call in client.tr_calls] == ["OPT90001", "OPT90001", "OPT90002", "OPT90002", "OPT90002"]
    assert [call["prev_next"] for call in client.tr_calls] == [0, 2, 0, 2, 0]
    assert sleeps == [1.2, 1.2, 1.2, 1.2]


def test_generator_filters_keywords_and_max_themes(tmp_path):
    client = MockKiwoomClient()
    client.set_market_codes("10", ["123456"])
    client.set_tr_pages(
        "opt90001",
        "",
        [
            {
                "rows": [
                    {"종목코드": "100", "테마명": "전력", "주요종목": ""},
                    {"종목코드": "200", "테마명": "반도체", "주요종목": "삼성전자"},
                    {"종목코드": "300", "테마명": "로봇", "주요종목": ""},
                ]
            }
        ],
    )
    client.set_tr_pages("opt90002", "200", [{"rows": [{"종목코드": "123456", "종목명": "코스닥부품"}]}])

    result = generate_theme_mappings_auto_csv(
        client,
        output_path=tmp_path / "theme_mappings_auto.csv",
        include_keywords=["반도체"],
        max_themes=1,
        now=_fixed_now(),
    )

    assert result.themes_total == 3
    assert result.themes_to_fetch == 1
    assert result.rows_written == 1
    assert [call["inputs"].get("종목코드") for call in client.tr_calls if call["tr_code"] == "OPT90002"] == ["200"]


def test_generator_overwrite_guard_does_not_request_tr(tmp_path):
    client = MockKiwoomClient()
    output = tmp_path / "theme_mappings_auto.csv"
    output.write_text("existing", encoding="utf-8")

    result = generate_theme_mappings_auto_csv(client, output_path=output, overwrite=False)

    assert any(error.startswith("OUTPUT_EXISTS") for error in result.errors)
    assert output.read_text(encoding="utf-8") == "existing"
    assert client.tr_calls == []


def test_tr_runner_timeout_empty_and_response_error_are_reported():
    timeout_client = SilentTrClient()
    clock = MutableClock()
    runner = KiwoomTrRunner(
        timeout_client,
        timeout_sec=1,
        clock=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        process_events=lambda: None,
    )

    timeout_result = runner.request_pages(tr_code="opt90001", rq_name="timeout", inputs={}, fields=["종목코드"])

    assert any(error.startswith("TR_TIMEOUT") for error in timeout_result.errors)

    client = MockKiwoomClient()
    client.set_tr_pages(
        "opt90001",
        "",
        [{"rows": [], "error_code": "1", "message": "bad response"}],
    )
    result = KiwoomTrRunner(client, request_delay_ms=0).request_pages(
        tr_code="opt90001",
        rq_name="error",
        inputs={},
        fields=["종목코드"],
    )

    assert any(error.startswith("TR_RESPONSE_ERROR") for error in result.errors)
    assert any(warning.startswith("TR_PAGE_EMPTY") for warning in result.warnings)


def test_tr_runner_falls_back_repeat_count_record_name():
    client = MockKiwoomClient()
    client.set_tr_pages(
        "opt90001",
        "",
        [{"rows": [{"종목코드": "550"}], "record_name": "테마그룹별"}],
    )
    calls = []

    def repeat_count(tr_code, record_name):
        calls.append(record_name)
        return 1 if record_name == "테마그룹별" else 0

    client.get_repeat_count = repeat_count

    result = KiwoomTrRunner(client, request_delay_ms=0).request_pages(
        tr_code="OPT90001",
        rq_name="OPT90001",
        inputs={},
        fields=["종목코드"],
    )

    assert result.rows == [{"종목코드": "550"}]
    assert "테마그룹별" in calls


def test_tr_runner_extracts_rows_during_tr_event():
    client = EventScopedTrClient()

    result = KiwoomTrRunner(client, request_delay_ms=0).request_pages(
        tr_code="OPT90001",
        rq_name="OPT90001",
        inputs={},
        fields=["code"],
    )

    assert result.rows == [{"code": "550"}]
    assert result.warnings == []


def test_script_defaults_and_next_steps_output(capsys):
    args = parse_args([])

    assert args.output.endswith("theme_mappings_auto.csv")
    assert args.default_enabled == 0

    _print_result(ThemeTemplateResult(output_path="data/theme_mappings_auto.csv", rows_written=2))
    output = capsys.readouterr().out

    assert "manual-review draft" in output
    assert "review and edit data/theme_mappings_auto.csv" in output
    assert "existing CSV import procedure" in output


def test_script_bootstrap_adds_project_root_to_sys_path(monkeypatch):
    script_path = Path("scripts/generate_theme_mappings.py").resolve()
    root = script_path.parents[1]
    monkeypatch.setattr(sys, "path", [str(script_path.parent)])
    spec = importlib.util.spec_from_file_location("generate_theme_mappings_bootstrap_test", script_path)
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    assert str(root) in sys.path


def test_generator_and_script_do_not_import_db_import():
    for path in ["trading/strategy/theme_template.py", "scripts/generate_theme_mappings.py"]:
        source = open(path, encoding="utf-8").read()
        assert "import_theme_mappings_csv" not in source
        assert "TradingDatabase" not in source


@dataclass
class MutableClock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class SilentTrClient:
    def __init__(self) -> None:
        from kiwoom.client import Signal

        self.tr_data_received = Signal()

    def set_input_value(self, input_name: str, value: str) -> None:
        pass

    def comm_rq_data(self, rq_name: str, tr_code: str, prev_next: int, screen_no: str) -> int:
        return 0

    def get_repeat_count(self, tr_code: str, rq_name: str) -> int:
        return 0

    def get_comm_data(self, tr_code: str, rq_name: str, index: int, item_name: str) -> str:
        return ""


class EventScopedTrClient:
    def __init__(self) -> None:
        from kiwoom.client import Signal

        self.tr_data_received = Signal()
        self.in_event = False

    def set_input_value(self, input_name: str, value: str) -> None:
        pass

    def comm_rq_data(self, rq_name: str, tr_code: str, prev_next: int, screen_no: str) -> int:
        self.in_event = True
        try:
            self.tr_data_received.emit(screen_no, rq_name, tr_code, "", "", 0, "", "", "")
        finally:
            self.in_event = False
        return 0

    def get_repeat_count(self, tr_code: str, rq_name: str) -> int:
        return 1 if self.in_event else 0

    def get_comm_data(self, tr_code: str, rq_name: str, index: int, item_name: str) -> str:
        return "550" if self.in_event and item_name == "code" else ""


def _fixed_now():
    from datetime import datetime

    return datetime(2026, 5, 29, 8, 30, 0)
