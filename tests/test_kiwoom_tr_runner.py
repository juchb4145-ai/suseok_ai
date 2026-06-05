from __future__ import annotations

from kiwoom.tr import KiwoomTrRunner


class _Signal:
    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in list(self._callbacks):
            callback(*args)


class _Client:
    def __init__(self, *, repeat_count: int = 0, row: dict[str, str] | None = None, record_name: str = "주식기본정보") -> None:
        self.tr_data_received = _Signal()
        self.repeat_count = repeat_count
        self.row = dict(row or {})
        self.record_name = record_name
        self.inputs: dict[str, str] = {}
        self.comm_calls: list[tuple[str, str, int, str]] = []
        self.field_calls: list[tuple[str, str, int, str]] = []

    def set_input_value(self, key: str, value: str) -> None:
        self.inputs[str(key)] = str(value)

    def comm_rq_data(self, rq_name: str, tr_code: str, prev_next: int, screen_no: str) -> int:
        self.comm_calls.append((rq_name, tr_code, prev_next, screen_no))
        self.tr_data_received.emit(screen_no, rq_name, tr_code, self.record_name, "", 0, "", "", "")
        return 0

    def get_repeat_count(self, tr_code: str, record_name: str) -> int:
        return self.repeat_count

    def get_comm_data(self, tr_code: str, record_name: str, index: int, field_name: str) -> str:
        self.field_calls.append((tr_code, record_name, index, field_name))
        if index != 0:
            return ""
        if record_name != self.record_name:
            return ""
        return self.row.get(str(field_name), "")


def test_tr_runner_reads_single_row_fields_when_repeat_count_is_zero():
    client = _Client(row={"종목명": "테스트", "현재가": "12,340", "기준가": "11,000"})
    runner = KiwoomTrRunner(client, request_delay_ms=0, process_events=lambda: None)

    result = runner.request_pages(
        tr_code="opt10001",
        rq_name="ThemeBackfill_opt10001",
        inputs={"종목코드": "440110"},
        fields=["종목명", "현재가", "기준가"],
        screen_no="8700",
    )

    assert result.errors == []
    assert result.rows == [{"종목명": "테스트", "현재가": "12,340", "기준가": "11,000"}]
    assert any(warning.startswith("TR_SINGLE_ROW_FALLBACK:opt10001") for warning in result.warnings)
    assert client.inputs == {"종목코드": "440110"}


def test_tr_runner_keeps_empty_page_when_repeat_and_single_fields_are_empty():
    client = _Client(row={})
    runner = KiwoomTrRunner(client, request_delay_ms=0, process_events=lambda: None)

    result = runner.request_pages(
        tr_code="opt10001",
        rq_name="ThemeBackfill_opt10001",
        inputs={"종목코드": "440110"},
        fields=["종목명", "현재가", "기준가"],
        screen_no="8700",
    )

    assert result.rows == []
    assert any(warning.startswith("TR_PAGE_EMPTY:opt10001") for warning in result.warnings)
